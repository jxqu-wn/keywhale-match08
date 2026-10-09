"""Bounded, resumable ReAct generation with model-selected read-only local tools."""
import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .model_pipeline import Context, SYSTEM, api, parse_output, stratified_sample, write_offline_request
from .paths import ROOT, OUT, RAW, RUNS
from .pipeline import load, queries, source
from .react_tools import QueryTools, TOOLS

REACT_SYSTEM = SYSTEM + '''
本流程采用ReAct工具循环。上文的查询JSON是prediction的格式；每轮最外层只输出以下动作JSON，不输出思维过程：
{"action":"工具名","args":{工具参数}}。
可调用get_schema、search_knowledge、search_examples、check_query、finish，参数见用户提供的tools。
按当前问题决定下一步动作。先检索真实Schema和知识，必要时查示例；不能仅凭候选路由或记忆猜字段。
observation是工具结果，发现缺失表/字段/知识或检查错误时继续检索或修正。check_query用于测试候选；不是数据库真实执行。
get_schema必须覆盖查询所有物理表；跨年edu_*视图对应的两年物理表也要查。一次可查多张表。
search_knowledge即使返回空集合也已完成知识检索；但不能把空集合解释为允许猜造枚举。
check_query的args.prediction使用规定的单源或跨源格式。必须根据errors修正候选并再次检查。
check_query返回ok=true后，使用其candidate调用finish。自动展开的education CTE保留在prediction，由程序导出，无须手抄。
finish前检查当前问题全部返回列、参数、粒度、关联、分母和排序，不增加题目没有的条件。若要改查询先再次check_query。
每轮只调用一个工具；不要在args里夹带其他动作或把多个动作合成数组。'''


def initial_prompt(ctx):
    pinned = {key: ctx[key] for key in ('question', 'requirements', 'business_rules', 'routing',
              'education_merge', 'education_scope', 'schema_adjudications')}
    pinned['schema_candidates'] = [{'source': t['source'], 'table': f"{t.get('database')}.{t['table']}"}
                                   for t in ctx['schema']]
    pinned['tools'] = TOOLS
    pinned['instruction'] = '候选表仅是检索线索；用get_schema获取完整定义，用search_knowledge确认知识。'
    return pinned


def run_question(row, context, directory, model, base_url, max_steps=10, offline=False, call=None):
    path = directory / f"{row['question_id']}.json"
    if path.exists():
        cached = load(path)
        if cached.get('prediction') and not cached.get('errors') and cached.get('question') == row['question']:
            return cached
    ctx = context.build(row['question'])
    tools = QueryTools(context, ctx)
    messages = [{'role': 'system', 'content': REACT_SYSTEM},
                {'role': 'user', 'content': json.dumps(initial_prompt(ctx), ensure_ascii=False)}]
    record = {'question_id': row['question_id'], 'question': row['question'], 'context': ctx,
              'strategy': 'react', 'trace': [], 'attempts': [], 'prediction': None,
              'errors': [], 'manual_query_edits': False}
    started = time.monotonic()
    for step in range(max_steps):
        payload = {'model': model, 'messages': messages, 'temperature': 0, 'max_tokens': 4096,
                   'response_format': {'type': 'json_object'}, 'chat_template_kwargs': {'enable_thinking': False}}
        request_path = directory / f"{row['question_id']}.request.{step}.json"
        response_path = directory / f"{row['question_id']}.response.{step}.json"
        write_offline_request(request_path, response_path, payload)
        try:
            if response_path.exists():
                response = load(response_path)
            elif offline:
                record['errors'] = ['pending_request']
                break
            else:
                response = (call or api)(base_url, '/chat/completions', payload)
                response_path.write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
            choice = response['choices'][0]
            content = choice['message'].get('content') or ''
            if not isinstance(content, str):
                raise ValueError('模型content不是字符串')
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            # Transport failures retain the same pending step; never skip a tool turn.
            record['errors'] = ['pending_request']
            record['transport_error'] = str(exc)
            break
        record['attempts'].append({'content': content, 'finish_reason': choice.get('finish_reason'), 'usage': response.get('usage')})
        action, args = None, None
        try:
            if choice.get('finish_reason') == 'length':
                raise ValueError('输出被截断；缩短本轮动作，重新输出完整JSON')
            envelope = parse_output(content)
            if set(envelope) != {'action', 'args'} or not isinstance(envelope['action'], str):
                raise ValueError('必须输出仅包含action和args的单个动作JSON')
            action, args = envelope['action'], envelope['args']
            observation = tools.dispatch(action, args)
        except (ValueError, KeyError, TypeError) as exc:
            observation = {'ok': False, 'errors': [str(exc)], 'available_actions': list(TOOLS)}
        record['trace'].append({'step': step, 'action': action, 'args': args, 'observation': observation})
        if observation.get('finished') and observation.get('ok'):
            record.update(prediction=observation['prediction'], model_prediction=observation['candidate'],
                          errors=[], prediction_source='react_model_with_tools',
                          automatic_transforms=observation['automatic_transforms'])
            break
        messages += [{'role': 'assistant', 'content': content},
                     {'role': 'user', 'content': json.dumps({'observation': observation,
                       'remaining_steps': max_steps - step - 1,
                       'instruction': '根据工具结果选择下一步动作；错误必须解决后再次check_query。'}, ensure_ascii=False)}]
    else:
        record['errors'] = ['step_budget_exhausted']
        record['last_observation'] = record['trace'][-1]['observation'] if record['trace'] else None
    record['seconds'] = round(time.monotonic() - started, 3)
    record['api_calls'] = len(record['attempts'])
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return record


def summarize(records):
    pending = sum(r.get('errors') == ['pending_request'] for r in records)
    finished = sum(bool(r.get('prediction')) and not r.get('errors') for r in records)
    return {'count': len(records), 'valid_count': finished, 'pending_count': pending,
            'unresolved_count': len(records) - finished - pending,
            'format_and_lexical_check_rate': None if pending else finished / max(1, len(records)),
            'api_calls': sum(r.get('api_calls', 0) for r in records),
            'check_failures': sum(t['action'] == 'check_query' and not t['observation'].get('ok') for r in records for t in r['trace']),
            'automatic_es_normalizations': sum('es_dsl_level_normalization' in r.get('automatic_transforms', []) for r in records),
            'manual_query_edits': 0, 'execution_accuracy': None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['predict', 'evaluate'])
    parser.add_argument('--base-url', default='http://192.168.15.119:28081/v1')
    parser.add_argument('--model', default='contest-qwen')
    parser.add_argument('--ids', help='逗号分隔的问题ID')
    parser.add_argument('--sample', type=int, default=0)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--max-steps', type=int, default=10)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--offline', action='store_true', help='写请求、读取响应；使用curl-batch调用模型后重复本命令')
    args = parser.parse_args()
    if args.workers < 1 or args.max_steps < 4 or args.max_steps > 24:
        parser.error('workers需为正数，max-steps需为4到24')
    if args.ids and args.sample:
        parser.error('--ids和--sample不能同时指定')
    context = Context(args.command == 'evaluate')
    rows = load(OUT / 'dev.json' if args.command == 'evaluate' else RAW / 'QA_test.json')
    if args.ids:
        requested = [int(x) for x in args.ids.split(',')]
        wanted = set(requested)
        rows = [r for r in rows if r['question_id'] in wanted]
        if len(requested) != len(wanted) or {r['question_id'] for r in rows} != wanted:
            parser.error('ID有重复或不在当前数据集中')
    if args.sample:
        rows = stratified_sample(rows, args.sample, args.seed)
    config = {'strategy': 'react', 'command': args.command, 'model': args.model, 'base_url': args.base_url,
              'sample_ids': [r['question_id'] for r in rows], 'seed': args.seed, 'max_steps': args.max_steps,
              'sample_hash': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
              'data_hash': hashlib.sha256(json.dumps([context.examples, context.catalog, context.knowledge,
                 context.dictionaries, context.ambiguous, context.business_rules, context.resolutions,
                 context.corrections, context.schema_adjudications, context.curation], sort_keys=True).encode()).hexdigest(),
              'implementation_hash': hashlib.sha256(b''.join(p.read_bytes() for p in sorted(Path(__file__).parent.glob('*.py')))).hexdigest(),
              'system': REACT_SYSTEM}
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
    parent = args.output or RUNS / ('evaluation' if args.command == 'evaluate' else 'prediction')
    directory = parent / ('react-' + args.command + '-' + fingerprint)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    (directory / 'sample.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')

    def process(row):
        result = run_question(row, context, directory, args.model, args.base_url, args.max_steps, args.offline)
        print(f"{row['question_id']}: {'OK' if not result.get('errors') else result['errors']}", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        records = list(pool.map(process, rows))
    predictions = [{'question_id': r['question_id'], **r['prediction']} for r in records if r['prediction'] and not r['errors']]
    (directory / 'predictions.json').write_text(json.dumps(predictions, ensure_ascii=False, indent=2) + '\n')
    metrics = summarize(records)
    if args.command == 'evaluate':
        gold = {r['question_id']: r for r in rows}
        metrics['query_exact_match'] = None if metrics['pending_count'] else sum(
            source(p) == source(gold[p['question_id']]) and queries(p) == queries(gold[p['question_id']]) for p in predictions) / len(rows)
    (directory / 'metrics.json').write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + '\n')
    print(directory, json.dumps(metrics, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
