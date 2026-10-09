"""Audit ReAct SQLite outputs against the raw training answers.

For every SQLite question in ``data/raw/subject/QA_train.json`` this script
aligns the ReAct prediction from ``answers.json`` with the gold query and
records where the two differ semantically.

Two layers are produced:

* deterministic: literal-masked, alias-normalised comparison plus table and
  column signals taken from the original DDL catalog;
* semantic judge: an optional DeepSeek/OpenAI-compatible model marks each
  remaining pair as semantically equivalent or as a deviation with a category.

The judge is cached per question so the command can be interrupted and rerun.
Both layers are descriptive: agreement with the gold query is not a competition
score, and the gold itself is not re-validated against real data.
"""
import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from text2query.pipeline import load, references
from text2query.validation import sqlite_connection

DEFAULT_RUN = ROOT / 'artifacts/runs/sqlite-train-react-deepseek-flash'
GOLD = ROOT / 'data/raw/subject/QA_train.json'

KEYWORDS = {
    'on', 'where', 'group', 'order', 'left', 'right', 'inner', 'outer', 'full',
    'cross', 'join', 'union', 'limit', 'having', 'as', 'and', 'or', 'using',
    'natural', 'select', 'from', 'with', 'by', 'distinct', 'case', 'when',
    'then', 'else', 'end', 'asc', 'desc', 'over', 'partition', 'not', 'in',
    'exists', 'like', 'between', 'is', 'null', 'offset', 'all', 'except',
    'intersect', 'values', 'set', 'insert', 'update', 'delete', 'window',
}


def strip_comments(sql):
    sql = re.sub(r'--[^\n]*', ' ', sql)
    return re.sub(r'/\*.*?\*/', ' ', sql, flags=re.S)


def alias_map(sql):
    """Map table aliases to their fully qualified table name."""
    mapping = {}
    pattern = re.compile(
        r'\b(?:from|join)\s+([A-Za-z_][\w]*(?:\s*\.\s*[A-Za-z_][\w]*)?)'
        r'(?:\s+(?:as\s+)?([A-Za-z_]\w*))?', re.I)
    for match in pattern.finditer(sql):
        table = re.sub(r'\s+', '', match.group(1))
        alias = match.group(2)
        if alias and alias.lower() not in KEYWORDS:
            mapping[alias.lower()] = table
        # A table reference may itself be used as a qualifier (db.table).
        mapping[table.split('.')[-1].lower()] = table
    return mapping


def normalize_sql(sql):
    """Conservative canonical form: aliases and syntax noise removed.

    String literals are stashed so alias replacement cannot touch their
    contents, then restored verbatim: different literal values must remain a
    difference rather than being normalised away.
    """
    literals = []

    def stash(match):
        literals.append(match.group(0))
        return '\x00' + str(len(literals) - 1) + '\x00'

    text = strip_comments(re.sub(r"'(?:''|[^'])*'", stash, sql)).lower()
    amap = alias_map(text)

    def qualified(match):
        prefix, column = match.group(1), match.group(2)
        return amap.get(prefix.lower(), prefix) + '.' + column

    text = re.sub(r'([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)', qualified, text)
    # CAST(... AS type) is semantic; remove only aliases before clause boundaries.
    text = re.sub(r'\s+as\s+[A-Za-z_]\w*(?=\s*(?:[,;]|$|\b(?:from|join|left|right|inner|outer|full|cross|on|using|where|group|order|having|limit|offset|union)\b))', '', text)
    text = re.sub(r'\binner\s+join\b', 'join', text)
    text = re.sub(r'\bouter\s+join\b', 'join', text)
    text = re.sub(r'<>', '!=', text)
    text = re.sub(r'\bcount\s*\(\s*1\s*\)', 'count(*)', text)
    text = re.sub(r'\s+', ' ', text).strip().rstrip(';').strip()
    text = re.sub(r'\s*([(),=<>+\-*/])\s*', r'\1', text)
    text = re.sub(r'\s+', ' ', text)
    return re.sub(r'\x00(\d+)\x00', lambda match: literals[int(match.group(1))], text)


def token_signature(sql):
    return tuple(token if token.startswith("'") else token.lower()
                 for token in re.findall(r"'(?:''|[^'])*'|[\w.]+|[^\s]", sql.rstrip().rstrip(';')))


def column_count(db, query, catalog):
    if db != 'SQLite' or not isinstance(query, str):
        return None
    try:
        connection = sqlite_connection(catalog, query)
        try:
            prepared = re.sub(r'\bIN\s*\(\s*result\s*\)', 'IN (SELECT NULL WHERE 0)', query, flags=re.I)
            cursor = connection.execute('EXPLAIN ' + prepared)
            description = connection.execute(prepared).description
            return len(description) if description else None
        finally:
            connection.close()
    except Exception:
        return None


def sql_tables(db, query):
    if not isinstance(query, str):
        return []
    return references(query)


def judge_system_prompt():
    return (
        '你是SQL语义审核专家。给定中文问题、训练集标准查询gold和模型生成查询prediction，'
        '判断预测查询相对标准查询是否存在语义偏差。\n'
        '语义等价包括：大小写、空白、别名、等价JOIN写法、COUNT(1)/COUNT(*)等不影响结果的差异。\n'
        '存在偏差包括：数据源或表不同、返回列含义不同、筛选条件不同（多/少/改）、'
        '聚合或统计粒度不同、分组不同、关联/去重不同、排序口径不同、LIMIT不同、字面值/枚举不同。\n'
        '只根据问题和两条SQL判断，不要臆造数据库内容；gold未必唯一，但以gold为比较基准。\n'
        '只输出JSON：{"results":[{"id":整数,"semantic_deviation":布尔,'
        '"category":"none|table|projection|filter|aggregation|grouping|join|ordering|limit|value|other",'
        '"model_only":布尔,"explanation":"不超过60字中文说明"}]}。'
        'semantic_deviation为false时category必须是none。model_only表示偏差只出在prediction一侧。'
    )


def comparison_hash(record, model, base_url):
    value = {key:record.get(key) for key in ('question_id','question','gold_query','prediction_query')}
    value.update(model=model, base_url=base_url, system=judge_system_prompt())
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def judge_batch(items, api_key, base_url, model, max_retries=4, artifacts=None):
    user = json.dumps({'items': [
        {'id': item['question_id'], 'question': item['question'],
         'gold': item['gold_query'], 'prediction': item['prediction_query']}
        for item in items]}, ensure_ascii=False)
    payload = {
        'model': model,
        'messages': [{'role': 'system', 'content': judge_system_prompt()},
                     {'role': 'user', 'content': user}],
        'temperature': 0,
        'max_tokens': 4096,
        'response_format': {'type': 'json_object'},
        'thinking': {'type': 'disabled'},
    }
    batch_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
    if artifacts:
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / f'{batch_hash}.request.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    request = urllib.request.Request(
        base_url.rstrip('/') + '/chat/completions',
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    last = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                body = json.load(response)
            if artifacts:
                (artifacts / f'{batch_hash}.response.{attempt}.json').write_text(json.dumps(body,ensure_ascii=False,indent=2)+'\n')
            content = body['choices'][0]['message']['content']
            parsed = json.loads(content)
            rows = parsed.get('results', parsed if isinstance(parsed, list) else [])
            indexed = {int(row['id']): row for row in rows if 'id' in row}
            missing = [item['question_id'] for item in items if item['question_id'] not in indexed]
            if missing:
                raise ValueError('judge omitted ids: ' + str(missing))
            for item in items:
                result = indexed[item['question_id']]
                if type(result.get('semantic_deviation')) is not bool or type(result.get('model_only')) is not bool:
                    raise ValueError('judge flags must be JSON booleans')
                if not result['semantic_deviation'] and result.get('category') != 'none':
                    raise ValueError('equivalent judgment must use category none')
                result['comparison_sha256'] = comparison_hash(item, model, base_url)
            return indexed
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError,
                KeyError, json.JSONDecodeError) as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 401, 403):
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError('judge failed: ' + repr(last))


def chunked(rows, size):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=DEFAULT_RUN)
    parser.add_argument('--gold', type=Path, default=GOLD)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'data/processed/catalog.json')
    parser.add_argument('--output', type=Path, default=None,
                        help='output directory; defaults to <run>/semantic_audit')
    parser.add_argument('--judge', action='store_true',
                        help='call the model to judge non-identical pairs')
    parser.add_argument('--model', default='deepseek-flash')
    parser.add_argument('--base-url', default='https://api.deepseek.com')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--batch-size', type=int, default=10)
    parser.add_argument('--limit', type=int, default=None, help='debug: only first N pairs')
    parser.add_argument('--api-key-file', type=Path, default=None,
                        help='file containing a raw API key; otherwise MODEL_API_KEY is used')
    args = parser.parse_args()

    run = args.run
    output = args.output or run / 'semantic_audit'
    output.mkdir(parents=True, exist_ok=True)
    cache_path = output / 'judge_cache.json'
    cache = load(cache_path) if cache_path.exists() else {}

    gold = {row['question_id']: row for row in load(args.gold)
            if 'SQLite' in (row.get('db'), row.get('db1'), row.get('db2'))}
    answers = load(run / 'answers.json')
    catalog = load(args.catalog)

    records = {}
    for row in answers:
        qid = row['question_id']
        source = gold.get(qid, {})
        prediction = row.get('prediction') or {}
        gold_query = source.get('query')
        prediction_query = prediction.get('query')
        detail_path = run / 'questions' / f'{qid}.json'
        detail = load(detail_path) if detail_path.exists() else {}
        examples = detail.get('context', {}).get('examples', [])
        example_ids = [example.get('question_id') for example in examples]
        request_path = run / 'questions' / f'{qid}.request.0.json'
        initial_prompt = {}
        if request_path.exists():
            request = load(request_path)
            initial_prompt = json.loads(request['messages'][1]['content'])
        initial_exact_ids = [r.get('question_id') for r in initial_prompt.get('exact_examples', [])]
        tool_case_ids = {example.get('question_id') for trace in detail.get('trace', [])
                         if trace.get('action') == 'search_examples'
                         for key in ('examples','exact_matches')
                         for example in trace.get('observation', {}).get(key, [])}
        record = {
            'question_id': qid,
            'question': row.get('question') or source.get('question'),
            'gold_example_rank': example_ids.index(qid) if qid in example_ids else None,
            'exact_example_in_initial_prompt':qid in initial_exact_ids,
            'exact_example_in_tool_results':qid in tool_case_ids,
            'example_count': len(examples),
            'knowledge_count': len(detail.get('context', {}).get('knowledge', [])),
            'prediction_source': detail.get('prediction_source'),
            'gold_db': source.get('db'),
            'prediction_db': prediction.get('db'),
            'gold_query': gold_query,
            'prediction_query': prediction_query,
            'terminal_errors': row.get('errors', []),
            'same_db': source.get('db') == prediction.get('db'),
            'exact_equal': gold_query == prediction_query,
            'token_equal': (source.get('db') == prediction.get('db')
                            and isinstance(gold_query, str) and isinstance(prediction_query, str)
                            and token_signature(gold_query) == token_signature(prediction_query)),
            'canonical_equal': (isinstance(gold_query, str) and isinstance(prediction_query, str)
                                and normalize_sql(gold_query) == normalize_sql(prediction_query)),
            'canonical_gold': normalize_sql(gold_query) if isinstance(gold_query, str) else None,
            'canonical_prediction': normalize_sql(prediction_query) if isinstance(prediction_query, str) else None,
            'gold_tables': sql_tables(source.get('db'), gold_query),
            'prediction_tables': sql_tables(prediction.get('db'), prediction_query),
            'gold_columns': column_count(source.get('db'), gold_query, catalog),
            'prediction_columns': column_count(prediction.get('db'), prediction_query, catalog),
        }
        records[qid] = record

    # Deterministic pre-classification: identical forms need no model call.
    pending = []
    for qid, record in records.items():
        if record['prediction_query'] is None:
            record['comparison_level'] = 'missing_prediction'
        elif not record['same_db']:
            record['comparison_level'] = 'db_differs'
        elif record['token_equal']:
            record['comparison_level'] = 'identical_tokens'
        elif record['canonical_equal']:
            record['comparison_level'] = 'equivalent_canonical'
        else:
            record['comparison_level'] = 'needs_judge'
            pending.append(record)

    if args.limit:
        pending = pending[:args.limit]

    if args.judge:
        if args.api_key_file:
            api_key = args.api_key_file.read_text().strip()
        else:
            api_key = os.environ.get('MODEL_API_KEY', '')
        if not api_key:
            parser.error('--judge requires MODEL_API_KEY or --api-key-file')
        # Old ID-only cache entries are reusable only when the saved pair is identical.
        previous_path = output / 'semantic_audit.json'
        previous = {r['question_id']:r for r in load(previous_path)['records']} if previous_path.exists() else {}
        for record in pending:
            key = str(record['question_id'])
            if key not in cache:
                continue
            old = cache[key]
            expected = comparison_hash(record, args.model, args.base_url)
            legacy = previous.get(record['question_id'])
            if old.get('comparison_sha256') != expected and not (
                'comparison_sha256' not in old and legacy and all(
                    record.get(k) == legacy.get(k) for k in ('question','gold_query','prediction_query'))):
                del cache[key]
        todo = [record for record in pending if str(record['question_id']) not in cache]
        lock = threading.Lock()
        print(json.dumps({'judge_pending': len(pending), 'judge_cached': len(pending) - len(todo),
                          'to_call': len(todo)}, ensure_ascii=False), flush=True)

        def persist():
            temporary = cache_path.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temporary.replace(cache_path)

        failures = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(judge_batch, batch, api_key, args.base_url, args.model,
                                   artifacts=output/'judge_calls'): batch
                       for batch in chunked(todo, args.batch_size)}
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    judged = future.result()
                except Exception as exc:
                    failures.append({'ids': [item['question_id'] for item in batch], 'error': repr(exc)})
                    print(json.dumps({'judge_error': repr(exc)}, ensure_ascii=False), flush=True)
                    continue
                with lock:
                    for item in batch:
                        cache[str(item['question_id'])] = judged[item['question_id']]
                    persist()
                print(json.dumps({'judged': sum(str(r['question_id']) in cache for r in pending),
                                  'remaining': sum(str(r['question_id']) not in cache for r in pending)},
                                 ensure_ascii=False), flush=True)
        if failures:
            (output / 'judge_failures.json').write_text(
                json.dumps(failures, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    for qid, record in records.items():
        judgment = cache.get(str(qid))
        if judgment is None and record['comparison_level'] in ('identical_tokens',):
            judgment = {'semantic_deviation': False, 'category': 'none',
                        'model_only': False, 'explanation': '字面量、空白外完全一致'}
        elif judgment is None and record['comparison_level'] == 'equivalent_canonical':
            judgment = {'semantic_deviation': False, 'category': 'none',
                        'model_only': False, 'explanation': '别名/写法规范化后一致'}
        record['judge'] = judgment

    deviations = [record for record in records.values()
                  if record['judge'] and record['judge'].get('semantic_deviation')]
    deviations.sort(key=lambda record: record['question_id'])
    summary = {
        'run': str(run),
        'gold_file': str(args.gold),
        'total': len(records),
        'with_prediction': sum(record['prediction_query'] is not None for record in records.values()),
        'missing_prediction': sum(record['prediction_query'] is None for record in records.values()),
        'same_db': sum(record['same_db'] for record in records.values()),
        'exact_equal': sum(record['exact_equal'] for record in records.values()),
        'token_equal': sum(record['token_equal'] for record in records.values()),
        'canonical_equal': sum(record['canonical_equal'] for record in records.values()),
        'needs_judge': len(pending),
        'judged': sum(1 for record in records.values() if record['judge'] is not None),
        'semantic_deviations': len(deviations),
        'equivalent_after_judge': sum(1 for record in records.values()
                                      if record['judge'] and not record['judge'].get('semantic_deviation')),
        'deviation_categories': {},
        'model_only': sum(1 for record in deviations if record['judge'].get('model_only')),
        'gold_example_given': sum(record['gold_example_rank'] is not None for record in records.values()),
        'exact_case_in_actual_initial_prompt':sum(record['exact_example_in_initial_prompt'] for record in records.values()),
        'exact_case_in_actual_tool_results':sum(record['exact_example_in_tool_results'] for record in records.values()),
        'deviations_with_gold_example_given': sum(record['gold_example_rank'] is not None for record in deviations),
    }
    for record in deviations:
        category = record['judge'].get('category', 'other')
        summary['deviation_categories'][category] = summary['deviation_categories'].get(category, 0) + 1

    ordered = [records[qid] for qid in sorted(records)]
    payload = {'summary': summary, 'records': ordered, 'deviations': deviations}
    (output / 'semantic_audit.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    lines = [
        '# SQLite 训练集 ReAct 结果语义对比',
        '',
        f"运行目录：`{run}`；标准答案：`{args.gold}`。",
        '',
        f"- 对齐题目：{summary['total']}；有预测：{summary['with_prediction']}；无预测：{summary['missing_prediction']}",
        f"- 完全一致（含空白/大小写）：{summary['exact_equal']}；token一致：{summary['token_equal']}；别名/写法规范化后一致：{summary['canonical_equal']}",
        f"- 送模型判定：{summary['needs_judge']}；已判定：{summary['judged']}",
        f"- 语义存在偏差：{summary['semantic_deviations']}（其中仅模型侧偏差 {summary['model_only']}）",
        f"- 判定等价：{summary['equivalent_after_judge']}",
        f"- 检索上下文含本题标准查询示例：{summary['gold_example_given']}；其中仍出现语义偏差：{summary['deviations_with_gold_example_given']}",
        f"- 实际首轮提示词包含本题案例：{summary['exact_case_in_actual_initial_prompt']}；实际示例工具结果包含本题案例：{summary['exact_case_in_actual_tool_results']}",
        '',
        '## 偏差分类',
        '',
        '| 类别 | 数量 |',
        '|---|---|',
    ]
    for category, count in sorted(summary['deviation_categories'].items(), key=lambda item: -item[1]):
        lines.append(f'| {category} | {count} |')

    def cell(value):
        return (value or '').replace('|', '\\|').replace('\n', ' ')[:180]

    failed = [record for record in records.values()
              if record['prediction_query'] is None or record['terminal_errors']]
    if failed:
        lines += ['', '## 无预测或终止错误', '', '| 题号 | 问题 | 终止错误 | 标准查询 |', '|---|---|---|---|']
        for record in sorted(failed, key=lambda item: item['question_id']):
            lines.append('| {qid} | {question} | {errors} | `{gold}` |'.format(
                qid=record['question_id'], question=cell(record['question'])[:120],
                errors='; '.join(record['terminal_errors']),
                gold=cell(record['gold_query'])))
    lines += ['', '## 偏差样本', '', '| 题号 | 类别 | 问题 | 模型预测 | 标准查询 | 说明 |', '|---|---|---|---|---|---|']
    for record in deviations:
        judge = record['judge']
        lines.append('| {qid} | {category} | {question} | `{prediction}` | `{gold}` | {note} |'.format(
            qid=record['question_id'], category=judge.get('category'), question=cell(record['question']),
            prediction=cell(record['prediction_query']), gold=cell(record['gold_query']),
            note=cell(judge.get('explanation'))))
    (output / 'semantic_audit.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('wrote', output / 'semantic_audit.json', output / 'semantic_audit.md')


if __name__ == '__main__':
    main()
