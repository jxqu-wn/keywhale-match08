"""Bounded, resumable ReAct generation with model-selected read-only local tools."""
import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from ..shared.context import Context
from ..shared.generation import api, parse_output, stratified_sample, write_offline_request, completion_payload
from ..shared.paths import ROOT, OUT, RAW, RUNS
from ..shared.data import load, queries, source
from .tools import QueryTools, TOOLS, tool_descriptions
from ..shared.fingerprints import implementation_hash

WORKFLOW_VERSION = 6

REACT_SYSTEM = '''你是多源只读查询生成器，使用当前问题和按需检索的证据生成查询。
单源prediction格式为 {"db":"MYSQL|POSTGRESQL|SQLite|Elasticsearch","query":"SQL或原生ES DSL对象"}。
跨源格式为 {"db1":"源类型","query1":"指令","db2":"源类型","query2":"指令"}；第二阶段用IN (result)引用第一阶段结果。
MySQL/SQLite物理表必须使用库名.表名；ES query使用原生DSL对象。
优先确定当前问题的统计对象、返回列、条件、粒度、关联和排序。没有要求时不要添加LIMIT。
同题案例作为初始候选：没有问题、Schema或具体知识证据支持时，保留其领域、字段、关联、表达式及业务语义。
案例存在明确错误时可以修正；符合题意的修正不应为追求标注一致而撤回。
题意和明确业务规则优先；Schema确定表字段及关联；表/字段具体证据优先于库级泛化描述。
知识列出多种格式或口径但未说明所属字段时，不得随意换掉案例采用的格式；补查证据，仍不确定则保留并在case_review说明。
多个同题案例发生冲突时逐项比较，不任意选择；routing只是证据支持的候选范围，可跨域查证。
复用与修正均由你判断，程序检查不证明语义正确。check_query中独立LLM按题意及证据复核实际SQL，不以原始案例为正确性标准。
本流程采用ReAct工具循环。上文的查询JSON是prediction的格式；每轮最外层只输出以下动作JSON，不输出思维过程：
{"action":"工具名","args":{工具参数}}。
可调用get_schema、search_knowledge、search_examples、check_query、finish，参数见用户提供的tools。
默认必备顺序：search_examples → get_schema → search_knowledge → check_query → finish。
首轮exact_examples保留同题原始案例及修订；必须先用search_examples核对重复/冲突和其他相关案例，再按schema_refs查Schema，按真实表范围查知识。
原始案例与修订同时保留；先核对同题案例，再按需查其他案例。不可只凭案例直接finish。
workflow.next_action给出下一步；上述依赖由程序检查。没有案例或知识时也须完成相应检索，空结果不允许编造信息。
发现新表先补查get_schema，再search_knowledge；重新检索示例后也要再次检索知识。任何证据检索都会使上次成功check_query失效。
observation是工具结果，发现缺失表/字段/知识或检查错误时继续检索或修正。check_query用于测试候选；不是数据库真实执行。
get_schema必须覆盖查询所有物理表；跨年edu_*视图对应的两年物理表也要查。一次可查多张表。
search_knowledge即使返回空集合也已完成知识检索；但不能把空集合解释为允许猜造枚举。
check_query的args.prediction使用规定的单源或跨源格式；case_review可选简短字符串，说明实质修改及依据。
不要编造证据，不以一般常识覆盖具体数据证据。独立复核明确指出案例错误时按具体问题修正，不能再次提交相同错误SQL。
必须根据errors修正候选并再次检查。输出完整闭合的动作JSON，尤其检查args和最外层对象的结束括号。
check_query返回ok=true后，使用其candidate调用finish。自动展开的education CTE保留在prediction，由程序导出，无须手抄。
finish前检查当前问题全部返回列、参数、粒度、关联、分母和排序，不增加题目没有的条件。若要改查询先再次check_query。
每轮只调用一个工具；不要在args里夹带其他动作或把多个动作合成数组。'''

# Keep the established general workflow; omit instructions whose objects are
# curated business rules, revised cases or automatically expanded education views.
RAW_REACT_SYSTEM = REACT_SYSTEM.replace('题意和明确业务规则优先；', '题意优先；').replace(
    '首轮exact_examples保留同题原始案例及修订；', '首轮exact_examples保留同题训练案例；').replace(
    '原始案例与修订同时保留；先核对同题案例，再按需查其他案例。不可只凭案例直接finish。',
    '先核对同题案例，再按需查其他案例。不可只凭案例直接finish。').replace(
    '；跨年edu_*视图对应的两年物理表也要查', '').replace(
    '自动展开的education CTE保留在prediction，由程序导出，无须手抄。', '')


def react_system(raw_only=False, semantic_review=False):
    system = RAW_REACT_SYSTEM if raw_only else REACT_SYSTEM
    if not semantic_review:
        system = system.replace(
            'check_query中独立LLM按题意及证据复核实际SQL，不以原始案例为正确性标准。', '')
        system = system.replace(
            '独立复核明确指出案例错误时按具体问题修正，不能再次提交相同错误SQL。', '')
    return system


def initial_prompt(ctx, semantic_review=False):
    pinned = {key: ctx[key] for key in ('question', 'requirements', 'business_rules', 'routing',
              'education_merge', 'education_scope', 'schema_adjudications')}
    exact_refs = {(ref['source'], f"{ref['database']}.{ref['table']}".lower())
                  for example in ctx.get('exact_examples', []) for ref in example['schema_refs']}
    pinned['schema_candidates'] = [{'source': t['source'], 'table': f"{t.get('database')}.{t['table']}"}
                                   for t in ctx['schema'] if not exact_refs or
                                   (t['source'], f"{t.get('database')}.{t['table']}".lower()) in exact_refs]
    pinned['schema_adjudications'] = [rule for rule in pinned['schema_adjudications'] if any(
        t['source'] in ctx['routing']['sources'] and
        t.get('database') in ctx['routing']['databases'] and
        f"{t.get('database')}.{t['table']}".lower() in {name.lower() for name in rule['tables']}
        for t in ctx['schema'])]
    raw_only = ctx.get('evidence_mode') == 'raw_only'
    if raw_only:
        for key in ('requirements', 'business_rules', 'education_merge', 'education_scope', 'schema_adjudications'):
            pinned.pop(key, None)
    pinned['tools'] = tool_descriptions(raw_only, semantic_review)
    pinned['semantic_review_enabled'] = semantic_review
    pinned['evidence_mode'] = ctx.get('evidence_mode', 'processed')
    pinned['exact_examples'] = ctx.get('exact_examples', [])
    pinned['workflow'] = {'next_action':'search_examples',
                          'required_order':['search_examples','get_schema','search_knowledge','check_query','finish']}
    pinned['instruction'] = '同题案例完整提供且不受Top-K限制；先search_examples，再查Schema和知识，检查成功后finish。'
    if ctx.get('education_merge'):
        pinned['education_instruction'] = ('使用education_merge提供的edu_*逻辑视图，程序自动展开两年物理表；'
            '不能照搬只查单年的旧案例。学生用student_no、课程用course_code，选课和成绩关联使用'
            'student_no/course_code/academic_year/semester_code；题目要求编号时保留ID。')
    return pinned


def run_question(row, context, directory, model, base_url, max_steps=10, offline=False, call=None, provider='vllm', semantic_review=False):
    review_enabled = bool(semantic_review and context.raw_evidence)
    path = directory / f"{row['question_id']}.json"
    if path.exists():
        cached = load(path)
        if 'semantic_review_enabled' in cached and cached['semantic_review_enabled'] != review_enabled:
            raise ValueError('语义复核设置与已有运行记录不同，请使用新的输出目录')
        if cached.get('errors') and not offline:
            # A fresh experiment must use a fresh output directory. Failed
            # records are evidence, not an invitation to rerun automatically.
            return cached
        if (cached.get('workflow_version') == WORKFLOW_VERSION and cached.get('prediction') and not cached.get('errors')
                and cached.get('question') == row['question']
                and cached.get('context', {}).get('evidence_mode') == context.evidence_manifest['mode']
                and cached.get('semantic_review_enabled') == review_enabled):
            return cached
    ctx = context.build(row['question'])
    reviewer = None
    if review_enabled:
        from .semantic_review import SemanticReviewer
        reviewer = SemanticReviewer(directory, row['question_id'], model, base_url, provider, call or api, offline,
                                    raw_only=context.raw_only)
    tools = QueryTools(context, ctx, semantic_reviewer=reviewer)
    system = react_system(context.raw_only, review_enabled)
    messages = [{'role': 'system', 'content': system},
                {'role': 'user', 'content': json.dumps(initial_prompt(ctx, review_enabled), ensure_ascii=False)}]
    record = {'question_id': row['question_id'], 'question': row['question'], 'context': ctx,
              'strategy': 'react', 'workflow_version': WORKFLOW_VERSION, 'provider': provider,
              'trace': [], 'attempts': [], 'prediction': None,
              'errors': [], 'manual_query_edits': False, 'semantic_review_enabled':review_enabled}
    started = time.monotonic()
    last_failure = None
    for step in range(max_steps):
        payload = completion_payload(model, messages, provider)
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
        except OSError as exc:
            record['errors'] = ['semantic_review_transport_error']
            record['transport_error'] = str(exc)
            record['trace'].append({'step':step, 'action':action, 'args':args,
                                    'observation':{'ok':False, 'errors':[str(exc)]}})
            break
        except (ValueError, KeyError, TypeError) as exc:
            observation = {'ok': False, 'errors': [str(exc)], 'available_actions': list(TOOLS)}
        record['trace'].append({'step': step, 'action': action, 'args': args, 'observation': observation})
        failure = json.dumps({'action':action, 'args':args, 'errors':observation.get('errors')},
                             sort_keys=True, ensure_ascii=False) if not observation.get('ok') else None
        if failure is not None and failure == last_failure:
            record['errors'] = ['repeated_error_without_progress']
            record['last_observation'] = observation
            break
        last_failure = failure
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
    record['semantic_review_api_calls'] = reviewer.calls if reviewer is not None else 0
    record['api_calls'] = len(record['attempts']) + record['semantic_review_api_calls']
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return record


def summarize(records):
    pending = sum(r.get('errors') == ['pending_request'] for r in records)
    finished = sum(bool(r.get('prediction')) and not r.get('errors') for r in records)
    return {'count': len(records), 'valid_count': finished, 'pending_count': pending,
            'unresolved_count': len(records) - finished - pending,
            'format_and_lexical_check_rate': None if pending else finished / max(1, len(records)),
            'api_calls': sum(r.get('api_calls', 0) for r in records),
            'semantic_review_api_calls': sum(r.get('semantic_review_api_calls', 0) for r in records),
            'check_failures': sum(t['action'] == 'check_query' and not t['observation'].get('ok') for r in records for t in r['trace']),
            'automatic_es_normalizations': sum('es_dsl_level_normalization' in r.get('automatic_transforms', []) for r in records),
            'manual_query_edits': 0, 'execution_accuracy': None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['predict', 'evaluate'])
    parser.add_argument('--base-url', default='http://192.168.15.119:28081/v1')
    parser.add_argument('--model', default='contest-qwen')
    parser.add_argument('--provider', choices=['vllm', 'deepseek'], default='vllm')
    parser.add_argument('--evidence-mode', choices=['raw-only', 'reviewed'], default='raw-only')
    parser.add_argument('--semantic-review', action='store_true', help='启用独立LLM语义复核；当前默认关闭')
    parser.add_argument('--ids', help='逗号分隔的问题ID')
    parser.add_argument('--sample', type=int, default=0)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--max-steps', type=int, default=10)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--offline', action='store_true', help='写请求、读取响应；使用curl-batch调用模型后重复本命令')
    args = parser.parse_args()
    if args.workers < 1 or args.max_steps < 5 or args.max_steps > 24:
        parser.error('workers需为正数，max-steps需为5到24')
    if args.ids and args.sample:
        parser.error('--ids和--sample不能同时指定')
    context = Context(args.command == 'evaluate', raw_evidence=True, evidence_mode=args.evidence_mode)
    rows = load(OUT / 'dev.json' if args.command == 'evaluate' else RAW / 'QA_test.json')
    if args.ids:
        requested = [int(x) for x in args.ids.split(',')]
        wanted = set(requested)
        rows = [r for r in rows if r['question_id'] in wanted]
        if len(requested) != len(wanted) or {r['question_id'] for r in rows} != wanted:
            parser.error('ID有重复或不在当前数据集中')
    if args.sample:
        rows = stratified_sample(rows, args.sample, args.seed)
    config = {'strategy': 'react', 'workflow_version':WORKFLOW_VERSION, 'command': args.command, 'model': args.model, 'base_url': args.base_url,
              'provider':args.provider, 'evidence_mode':args.evidence_mode, 'evidence_manifest':context.evidence_manifest,
              'semantic_review':args.semantic_review,
              'sample_ids': [r['question_id'] for r in rows], 'seed': args.seed, 'max_steps': args.max_steps,
              'sample_hash': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
              'data_hash': hashlib.sha256(json.dumps([context.examples, context.catalog, context.knowledge,
                 context.dictionaries, context.ambiguous, context.business_rules, context.resolutions,
                 context.corrections, context.schema_adjudications, context.curation], sort_keys=True).encode()).hexdigest(),
              'implementation_hash': implementation_hash(),
              'system': react_system(context.raw_only, args.semantic_review)}
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
    parent = args.output or RUNS / ('evaluation' if args.command == 'evaluate' else 'prediction')
    directory = parent / ('react-' + args.command + '-' + fingerprint)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    (directory / 'sample.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')

    def process(row):
        result = run_question(row, context, directory, args.model, args.base_url, args.max_steps, args.offline,
                              provider=args.provider, semantic_review=args.semantic_review)
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
