"""Verify the user-requested 2023 class-cohort trial with actual model SQL."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text2query.shared.context import Context
from text2query.shared.prompts import SYSTEM
from text2query.shared.generation import parse_output, validate
from text2query.shared.data import load, save
from text2query.shared.paths import RUNS
from text2query.shared.education import expand_prediction, model_query_errors
from text2query.shared.education_fixture import fixture, student_cohort_rows, mysql_fixture_sql

QUESTION = '查询学号前缀为2023的在读学生，返回学号与姓名，并按学号升序取前25名。'
QUERY = '''SELECT s.student_no,s.student_name FROM edu_student s
JOIN edu_class cl ON s._class_code=cl.class_code
WHERE s.student_status_code='ACTIVE' AND cl.grade_year=2023
ORDER BY s.student_no ASC LIMIT 25'''


def payload(messages):
    return {'model':'/models/qwen3-8-27b-fp8','temperature':0,'max_tokens':4096,
            'chat_template_kwargs':{'enable_thinking':False},'messages':messages}


def prepare(directory):
    ctx = Context(True, evidence_mode='reviewed').build(QUESTION)
    request = directory/'1674.request.0.json'
    response = directory/'1674.response.0.json'
    data = payload([{'role':'system','content':SYSTEM},
                    {'role':'user','content':json.dumps(ctx,ensure_ascii=False)}])
    if request.exists() and response.exists() and load(request)!=data:
        raise ValueError('历史响应对应不同请求，请使用新的--directory')
    save('1674.context.json',ctx,directory)
    save(request.name,data,directory)
    catalog = Context(True, evidence_mode='reviewed').catalog
    reference = expand_prediction({'db':'MYSQL','query':QUERY},ctx,catalog)
    save('reference.json',{'question':QUESTION,'model_query':QUERY,'prediction':reference,
        'source':'用户授权的2023级试验，仅作为本地构造验证参考；不发送给模型'},directory)
    (directory/'reference.sql').write_text(reference['query']+';\n',encoding='utf-8')
    print(directory)


def check_local(directory):
    ctx = load(directory/'1674.context.json')
    prediction = load(directory/'reference.json')['prediction']
    catalog = Context(True, evidence_mode='reviewed').catalog
    db,oracle = fixture(catalog,student_cohort_rows(catalog))
    classes = {row['class_id']:row for row in oracle['class'].values()}
    eligible = [row for row in oracle['student'].values() if row['student_status_code']=='ACTIVE'
                and classes.get(row['class_id'],{}).get('grade_year')==2023]
    expected = [(row['student_no'],row['student_name']) for row in sorted(eligible,key=lambda row:row['student_no'])[:25]]
    errors = model_query_errors({'db':'MYSQL','query':QUERY},ctx)+validate(prediction,catalog,QUESTION,ctx['business_rules'])
    actual = db.execute(mysql_fixture_sql(prediction['query'])).fetchall() if not errors else []
    db.close()
    result = {'passed':not errors and actual==expected,'question_ids':[1674,1724],
              'source':'local SQL implementing the user-requested trial, not model-generated',
              'model_inference_verified':False,'synthetic_execution_only':True,
              'candidate_count':len(eligible),'returned_count':len(actual),'actual':actual,'expected':expected,'errors':errors,
              'checks':['非2023前缀但2023级的在读学生被包含','2023前缀但其他届别被排除',
                        '最新非在读被排除','最新班级届别优先','旧年独有学生被补充',
                        '同年学生修订使用最新姓名','按学号升序取25条']}
    save('local_verification.json',result,directory)
    print(json.dumps({key:result[key] for key in ['passed','candidate_count','returned_count','model_inference_verified','errors']},ensure_ascii=False,indent=2))
    if not result['passed']:
        raise SystemExit(1)


def check(directory):
    context = load(directory/'1674.context.json')
    catalog = Context(True, evidence_mode='reviewed').catalog
    db,oracle = fixture(catalog,student_cohort_rows(catalog))
    classes = {row['class_id']:row for row in oracle['class'].values()}
    eligible = [row for row in oracle['student'].values() if row['student_status_code']=='ACTIVE'
                and classes.get(row['class_id'],{}).get('grade_year')==2023]
    expected = [(row['student_no'],row['student_name']) for row in sorted(eligible,key=lambda row:row['student_no'])[:25]]
    result = {'question_id':1674,'related_annotation_ids':[1674,1724],'question':QUESTION,
              'user_trial':True,'synthetic_execution_only':True,'fixture_expected_values_sent_to_model':False,
              'candidate_count':len(eligible),'expected':expected,'attempts':[],'passed':False}
    for attempt in range(3):
        request = directory/f'1674.request.{attempt}.json'
        response_path = directory/f'1674.response.{attempt}.json'
        if not request.exists() or not response_path.exists():
            result['pending_attempt'] = attempt
            break
        content = ''
        errors = []
        try:
            choice = load(response_path)['choices'][0]
            content = choice['message']['content']
            raw = parse_output(content)
            prediction = expand_prediction(raw,context,catalog)
            errors = model_query_errors(raw,context)+validate(prediction,catalog,QUESTION,context['business_rules'])
            if choice.get('finish_reason')=='length':
                errors.append('输出被截断')
            result.update({'model_prediction':raw,'prediction':prediction})
            if not errors:
                actual = db.execute(mysql_fixture_sql(prediction['query'])).fetchall()
                result['actual'] = actual
                if actual!=expected:
                    errors.append('构造执行不符合用户试验口径：检查最新班级grade_year=2023、在读条件、业务键关联、学号升序与LIMIT 25；不得按学号前缀筛选')
        except Exception as exc:
            errors.append(str(exc))
        result['attempts'].append({'attempt':attempt,'errors':errors,'response_file':response_path.name})
        if not errors:
            result['passed'] = True
            (directory/'generated.sql').write_text(prediction['query']+';\n',encoding='utf-8')
            break
        if attempt<2:
            messages = load(request)['messages']
            if content:
                messages.append({'role':'assistant','content':content})
            messages.append({'role':'user','content':'修正以下错误，只输出完整JSON：'+json.dumps(errors,ensure_ascii=False)})
            save(f'1674.request.{attempt+1}.json',payload(messages),directory)
    db.close()
    save('verification.json',result,directory)
    print(json.dumps({key:result.get(key) for key in ['passed','candidate_count','attempts','pending_attempt']},ensure_ascii=False,indent=2))
    if not result['passed']:
        raise SystemExit(1)


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['prepare','check-local','check'])
    parser.add_argument('--directory',type=Path,default=RUNS/'regression'/'student-cohort-2023-trial')
    args = parser.parse_args()
    {'prepare':prepare,'check-local':check_local,'check':check}[args.command](args.directory)
