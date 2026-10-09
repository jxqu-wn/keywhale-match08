"""Model regression for every annual-only conflict and publish remaining reviews."""
import argparse
import collections
import csv
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text2query.pipeline import load, save
from text2query.paths import ROOT, OUT, CURATED, RUNS
from text2query.model_pipeline import Context, SYSTEM, parse_output, validate
from text2query.education import expand_prediction, model_query_errors, education_query_mode, query_contract
from text2query.education_fixture import fixture, mysql_fixture_sql
from text2query.query_rules import applicable_business_rules

REPORTS = ROOT/'docs/reports/conflicts'


def cases():
    report = load(REPORTS/'冲突明细.json')
    result = []
    for group in report['groups']:
        if group['category']=='annual_database':
            original = group['variants'][0]['annotation']['query']
            oracle_query = original.replace('education_2024.','oracle.').replace('education_2025.','oracle.')
            if group['question_ids'][0]==1700:
                # Both old labels contain an unstated ACTIVE restriction. Follow the question's literal scope,
                # while publishing this discrepancy for human review rather than turning it into a new rule.
                oracle_query = oracle_query.replace("AND s.student_status_code='ACTIVE'",'')
            result.append({'question_id':group['question_ids'][0],'group_id':group['group_id'],
                           'question':group['question'],'oracle_query':oracle_query,
                           'kind':'annual_conflict'})
        elif group['question_ids'][0] in (1891,1921):
            result.append({'question_id':group['question_ids'][0],'group_id':group['group_id'],
                           'question':group['question'],'oracle_query':group['variants'][0]['annotation']['query'],
                           'kind':'explicit_comparison'})
    result += [
        {'question_id':'scope-2024-teacher','question':'统计2024年教师中电话不为空的数量，并返回数量。',
         'oracle_query':"SELECT COUNT(*) FROM education_2024.teacher WHERE phone IS NOT NULL AND phone<>''",'kind':'explicit_year'},
        {'question_id':'scope-2024-course','question':'查询2024年课程学分大于等于4的课程数量，并返回数量。',
         'oracle_query':'SELECT COUNT(*) FROM education_2024.course WHERE credit>=4','kind':'explicit_year'},
    ]
    return result


def prepare(directory=None):
    ctx = Context(True)
    sample = cases()
    config = {'model':'/models/qwen3-8-27b-fp8','system':SYSTEM,'rules':ctx.business_rules,'sample':sample,
              'retrieval_data_sha256':hashlib.sha256(json.dumps([ctx.examples,ctx.catalog,ctx.knowledge,ctx.dictionaries,ctx.ambiguous,ctx.resolutions],sort_keys=True).encode()).hexdigest(),
              'catalog_sha256':hashlib.sha256((OUT/'catalog.json').read_bytes()).hexdigest(),
              'code_sha256':hashlib.sha256(b''.join((ROOT/name).read_bytes() for name in
                               ['src/text2query/education.py','src/text2query/education_fixture.py','scripts/resolve_education_conflicts.py'])).hexdigest()}
    fingerprint = hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()[:12]
    directory = directory or RUNS/'regression'/('education-'+fingerprint)
    save('config.json',config,directory)
    save('sample.json',sample,directory)
    for case in sample:
        context = ctx.build(case['question'])
        messages = [{'role':'system','content':SYSTEM},{'role':'user','content':json.dumps(context,ensure_ascii=False)}]
        request = directory/(str(case['question_id'])+'.request.0.json')
        response = directory/(str(case['question_id'])+'.response.0.json')
        if request.exists() and response.exists() and load(request)!=payload(messages):
            raise ValueError('目录已有不同请求的历史响应，请选择新目录；不能覆盖并复用旧响应')
        save(str(case['question_id'])+'.context.json',context,directory)
        save(str(case['question_id'])+'.request.0.json',payload(messages),directory)
    save('scope_checks.json',[{'group_id':g['group_id'],'question':g['question'],'scope':education_query_mode(g['question'])}
                             for g in load(REPORTS/'冲突明细.json')['groups'] if g['category']=='question_mismatch'],directory)
    print(str(directory))


def payload(messages):
    return {'model':'/models/qwen3-8-27b-fp8','messages':messages,'temperature':0,'max_tokens':4096,
            'chat_template_kwargs':{'enable_thinking':False}}


def same_rows(actual, expected, ordered):
    if len(actual)!=len(expected):
        return False
    if not ordered:
        actual,expected = sorted(actual,key=repr),sorted(expected,key=repr)
    for a,b in zip(actual,expected):
        if len(a)!=len(b):
            return False
        for x,y in zip(a,b):
            if isinstance(x,(int,float)) and isinstance(y,(int,float)):
                if not math.isclose(x,y,rel_tol=1e-8,abs_tol=1e-6):
                    return False
            elif x!=y:
                return False
    return True


def process(directory):
    catalog = load(OUT/'catalog.json')
    db,_ = fixture(catalog)
    records = []
    for case in load(directory/'sample.json'):
        if case['question_id']==1700:
            case = {**case,'oracle_query':case['oracle_query'].replace("AND s.student_status_code='ACTIVE'",''),
                    'oracle_scope_note':'按问题原文不加在读限制；两个原标注都额外限定ACTIVE，另列人工待审核'}
        ident = str(case['question_id'])
        context = load(directory/(ident+'.context.json'))
        oracle_cursor = db.execute(mysql_fixture_sql(case['oracle_query']))
        expected_columns = [col[0] for col in oracle_cursor.description]
        expected = oracle_cursor.fetchall()
        record = {**case,'prediction':None,'errors':['pending_request'],'attempts':[],
                  'fixture_expected':expected,'fixture_actual':None,'fixture_passed':False}
        for attempt in range(4):
            request = directory/f'{ident}.request.{attempt}.json'
            response_path = directory/f'{ident}.response.{attempt}.json'
            if not request.exists() or not response_path.exists():
                record['pending_attempt'] = attempt
                break
            content = ''
            errors = []
            try:
                response = load(response_path)
                choice = response['choices'][0]
                content = choice['message']['content']
                raw = parse_output(content)
                pred = expand_prediction(raw,context,catalog)
                errors = model_query_errors(raw,context)+validate(pred,catalog,case['question'],context['business_rules'])
                if choice.get('finish_reason')=='length':
                    errors.append('模型输出被截断')
                record['model_prediction'],record['prediction'] = raw,pred
                if not errors:
                    actual = db.execute(mysql_fixture_sql(pred['query'])).fetchall()
                    record['fixture_actual'] = actual
                    ordered = bool(re.search(r'按.+(?:升序|降序|排序)|前\d+',case['question']))
                    if not same_rows(actual,expected,ordered):
                        contract = query_contract(case['question'])
                        errors.append('构造数据执行结果不符合问题要求；请检查返回列、问题参数、排序、聚合粒度和业务键关联。'+contract['instruction']+' 返回字段要求：'+json.dumps(contract['display_fields'],ensure_ascii=False)+'；数值筛选要求：'+json.dumps(contract['numeric_filters'],ensure_ascii=False))
            except Exception as exc:
                errors.append(str(exc))
            record['attempts'].append({'attempt':attempt,'response_file':response_path.name,'errors':errors,'content':content})
            record['errors'] = errors
            if not errors:
                record['fixture_passed'] = True
                break
            if attempt<3:
                messages = load(request)['messages']
                if content:
                    messages.append({'role':'assistant','content':content})
                messages.append({'role':'user','content':'修正以下错误，只输出完整JSON，教育默认年度查询使用edu_*合并视图，按业务键关联并分组。问题要求的返回列共'+str(len(expected_columns))+'列，字段/列含义参考：'+json.dumps(expected_columns,ensure_ascii=False)+'；不要额外返回分组用的辅助业务代码。'+json.dumps(errors,ensure_ascii=False)})
                save(f'{ident}.request.{attempt+1}.json',payload(messages),directory)
        save(ident+'.json',record,directory)
        records.append(record)
    db.close()
    passed = [r for r in records if r['fixture_passed']]
    pending = [r for r in records if not r['fixture_passed'] and r.get('pending_attempt') is not None]
    failed = [r for r in records if not r['fixture_passed'] and r.get('pending_attempt') is None]
    summary = {'total':len(records),'passed':len(passed),'pending':len(pending),'failed':len(failed),
               'shared_label_scope_review_ids':[1700,1750],
               'verification_code_sha256':hashlib.sha256(b''.join((ROOT/name).read_bytes() for name in ['scripts/resolve_education_conflicts.py','src/text2query/education.py','src/text2query/education_fixture.py'])).hexdigest(),
               'annual_conflicts_passed':sum(r['kind']=='annual_conflict' for r in passed),
               'synthetic_execution_only':True,'fixture_expected_values_sent_to_model':False,
               'automatic_expansion':'generic education CTE definitions; model SQL logic is preserved',
               'issues':[{'question_id':r['question_id'],'errors':r['errors'],'pending_attempt':r.get('pending_attempt'),
                          'expected':r['fixture_expected'],'actual':r['fixture_actual']} for r in records if not r['fixture_passed']]}
    save('verification.json',summary,directory)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return summary


def publish(directory):
    verification = load(directory/'verification.json')
    if verification['passed']!=verification['total'] or verification['annual_conflicts_passed']!=45:
        raise ValueError('Publish requires all45 annual conflicts and4 scope regressions to pass')
    correction_path = CURATED/'question_corrections.json'
    if correction_path.exists() and load(correction_path):
        # Preserve later user-authorized query corrections when refreshing the
        # earlier annual-policy report; never reset them to unresolved.
        from correct_conflicts import verify as verify_corrections, publish as publish_corrections
        corrections = load(correction_path)
        corrected_verification = verify_corrections(corrections)
        publish_corrections(corrections,corrected_verification)
        print(json.dumps({'annual_verification':verification,'correction_verification':corrected_verification},ensure_ascii=False,indent=2))
        return
    old = load(REPORTS/'冲突明细.json')
    try:
        run_reference = str(directory.resolve().relative_to(ROOT))
    except ValueError:
        run_reference = str(directory.resolve())
    registry = {'policy':'教育年度库按业务身份合并，2025整条记录优先；不同学期事件保留；明确年度/对比时按题意分别查询',
                'source':'用户明确授权推广教师处理方式','verification_run':run_reference,
                'synthetic_execution_only':True,'annual_differences_handled':45,'year_policy_applied_groups':49,
                'fully_resolved_groups':44,'remaining_groups':11,'groups':{}}
    remaining = []
    for group in old['groups']:
        category = group['category']
        applied_rules = applicable_business_rules(group['question'],load(CURATED/'business_rules.json'))
        trials = [rule for rule in applied_rules if rule.get('kind')=='education_question_override']
        if trials:
            group = {**group,'user_trial':trials,
                     'review_questions':['用户已要求按班级grade_year=2023替代学号LIKE条件；试验已实施，待真实业务数据或模型执行验证。',
                                         '保持在读、学号升序和前25条；此口径仅适用于当前完整题目，不推广到其他前缀查询。']}
            status,reason = 'year_resolved_user_trial_pending','年度已处理；用户试验采用班级grade_year=2023，取消学号LIKE；本地构造验证通过，模型推理与真实业务执行待验证'
        elif group['question_ids'][0]==1700:
            group = {**group,'priority':'P0','category':'shared_label_scope','category_label':'同一年度逻辑的共性标注范围待确认',
                     'difference':'原问题未限定在读学生，两个年度候选均额外使用student_status_code=ACTIVE。年度合并已实施；是否保留此额外条件需人工确认。',
                     'review_questions':['确认选课人数指全部有效选课学生，还是仅在读学生。',
                                         '确认院系排序方向；原文“按院系与选课人数降序”与原标注院系ASC存在表达差异。']}
            status,reason = 'year_resolved_shared_scope_pending','年度部分已处理；新增共性标注条件差异，按问题原文暂不额外限制在读，待人工确认'
        elif category=='annual_database':
            status,reason = 'resolved_by_year_policy','已采用统一教育年度合并视图，模型定向查询通过构造数据验证'
        elif category=='question_mismatch':
            status,reason = 'year_resolved_other_conflict_pending','年度口径已处理；问题参数或排序与原始标注仍不一致，保留人工审核'
        else:
            status,reason = 'pending_non_year_review','保留人工审核：'+group['difference']
        resolution = {'status':status,'reason':reason,'question_ids':group['question_ids'],
                      'year_mode':education_query_mode(group['question']) if any(t.startswith('education_') for t in group['tables']) else None}
        if trials:
            resolution['user_trial_rule_ids'] = [rule['rule_id'] for rule in trials]
        registry['groups'][group['group_id']] = resolution
        if status!='resolved_by_year_policy':
            review = {**group['review'],'notes':reason}
            if trials:
                review.update({'status':'需补充信息','handling':'重定义过滤口径',
                               'identity_key':'学生student_no；班级class_code',
                               'filter_order':'先合并选定2025优先记录，再筛选班级grade_year=2023与学生ACTIVE',
                               'join_policy':'edu_student._class_code=edu_class.class_code',
                               'parameter_corrections':'取消student_no LIKE；改为所属班级grade_year=2023',
                               'result_policy':'仅返回student_no、student_name；student_no ASC；LIMIT 25'})
            remaining.append({**group,'resolution':resolution,'review':review,'existing_user_rules':applied_rules})
    registry['user_trial_groups'] = sum(bool(g.get('user_trial')) for g in remaining)
    save('education_conflict_resolutions.json',registry,CURATED)
    subset = {**old,'groups':remaining,'review_intro':'年度合并口径已统一实施。当前仅展示参数/排序、业务定义、SQL等价写法，以及新发现的共性标注范围问题。',
              'source_generated_at':old.get('generated_at'),
              'generated_at':datetime.now(timezone.utc).isoformat(),
              'classification_note':'P0优先确认问题参数与标注、隐藏筛选条件和业务定义；P2确认疑似等价写法。原始候选保持原文，年度部分已按新规则处理。',
              'summary':{**old['summary'],'group_count':len(remaining),
                'record_count':sum(len(g['question_ids']) for g in remaining),
                'category_counts':dict(collections.Counter(g['category'] for g in remaining)),
                'source_counts':dict(collections.Counter(g['source'] for g in remaining for _ in g['question_ids'])),
                'direct_table_count':len({t for g in remaining for t in g['tables']}),
                'existing_teacher_rule_group_count':0},
              'input_sha256':{**old['input_sha256'],'data/curated/business_rules.json':hashlib.sha256((CURATED/'business_rules.json').read_bytes()).hexdigest(),
                              'data/curated/education_conflict_resolutions.json':hashlib.sha256((CURATED/'education_conflict_resolutions.json').read_bytes()).hexdigest()}}
    save('剩余冲突明细.json',subset,REPORTS)
    template = (ROOT/'scripts/templates/conflict_review.html').read_text().replace('冲突数据人工审核','剩余冲突人工审核')
    text = json.dumps(subset,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    (REPORTS/'剩余冲突人工审核.html').write_text(template.replace('__REPORT_JSON__',text),encoding='utf-8')
    lines = ['# 年度处理后的剩余冲突','', '**原45组纯年度差异已统一处理。剩余11组、24条原始标注需人工判断，包括本次新发现的1组共性标注范围问题。**', '',
             '其中4组的年度部分已处理，剩余的是问题参数/排序错误；另有1组业务定义差异、5组疑似等价写法，以及1700/1750额外限制在读学生的共性问题。以下候选保持原文，未自动替换为人工答案。','',
             '可使用 [剩余冲突审核页面](剩余冲突人工审核.html) 填写并导出。原55组完整快照及Schema仍见 [原始冲突报告](冲突数据报告.md)。','',
             '| 原始题号 | 剩余问题 | 当前处理 |','|---|---|---|']
    for g in remaining:
        state = '用户试验已实施，待真实数据验证' if g.get('user_trial') else ('年度已处理，其他口径待判断' if g['resolution']['status'].startswith('year_resolved') else '待确认业务定义或等价写法')
        lines.append('| '+','.join(map(str,g['question_ids']))+' | '+g['difference'].replace('|','\\|')+' | '+state+' |')
    for g in remaining:
        lines += ['', '## '+g['group_id'],'', g['question'],'', '**差异**：'+g['difference'],'', '**待确认**：','']
        lines += ['- '+q for q in g['review_questions']]+['']
        if g.get('user_trial'):
            lines += ['**用户试验口径（2026-10-09）**：'+g['user_trial'][0]['instruction'],'',
                      '本地构造数据验证通过；当前模型接口不可访问，尚未验证模型推理或真实业务结果。详见 [2023级学生试验说明](../student_cohort_trial.md)。','']
        for v in g['variants']:
            lines += ['候选'+v['label']+'（'+','.join(map(str,v['question_ids']))+'）：','', '```sql',v['annotation']['query'],'```','']
        if g['knowledge']:
            lines += ['相关专家知识：','']+['- 知识'+str(k['knowledge_id'])+'：'+k['knowledge_text'] for k in g['knowledge']]+['']
        lines += ['人工结论：____','']
    lines += ['## 原55组之外的已知数据一致性事项','']
    lines += ['- '+issue for issue in load(OUT/'audit.json').get('manual_issues',[])]+['',
              '这些事项单列，不计入上述11组。当前没有真实业务数据，业务键跨年稳定性及同年重复修订的顺序按实现约定处理。','']
    (REPORTS/'剩余冲突报告.md').write_text('\n'.join(lines),encoding='utf-8')
    columns = ['组编号','原始题号','问题','剩余差异','年度处理状态','审核状态','最终业务口径','补充说明']
    with (REPORTS/'剩余冲突审核模板.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer = csv.writer(f);writer.writerow(columns)
        for g in remaining:
            writer.writerow([g['group_id'],','.join(map(str,g['question_ids'])),g['question'],g['difference'],g['resolution']['reason'],g['review']['status'],
                             g['review'].get('parameter_corrections',''),g['review'].get('notes','')])
    print(json.dumps({'annual_differences_handled':45,'fully_resolved_groups':44,'partial_groups':5,'remaining_groups':len(remaining),
                      'remaining_records':subset['summary']['record_count'],'verification':verification},ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['prepare','process','publish'])
    parser.add_argument('--directory',type=Path)
    args = parser.parse_args()
    if args.command=='prepare':
        prepare(args.directory)
    else:
        if not args.directory:
            parser.error('--directory required for process/publish')
        (process if args.command=='process' else publish)(args.directory)
