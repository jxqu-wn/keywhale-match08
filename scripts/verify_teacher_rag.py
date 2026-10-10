"""Prepare real-model requests and execute returned queries on a synthetic fixture."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text2query.shared.context import Context
from text2query.shared.prompts import SYSTEM
from text2query.shared.generation import parse_output, validate
from text2query.shared.data import load, save
from text2query.shared.paths import RUNS
from text2query.shared.education import expand_prediction, model_query_errors

CASES = [
    {'question_id':'teacher-default','question':'统计教师中电话不为空的数量，并返回数量。','expected_count':2},
    {'question_id':'teacher-2024','question':'统计2024年教师中电话不为空的数量，并返回数量。','expected_count':3},
]


def fixture():
    conn = sqlite3.connect(':memory:')
    for year in ('2024','2025'):
        conn.execute(f"ATTACH DATABASE ':memory:' AS education_{year}")
        conn.execute(f'CREATE TABLE education_{year}.department (department_id INTEGER PRIMARY KEY, department_code TEXT, department_name TEXT)')
        conn.execute(f"INSERT INTO education_{year}.department VALUES (1,'D1','院系1')")
    conn.create_function('CONCAT',-1,lambda *args: ''.join(map(str,args)))
    for year in ('2024','2025'):
        conn.execute(f'''CREATE TABLE education_{year}.teacher (
            teacher_id INTEGER PRIMARY KEY, teacher_no TEXT NOT NULL,
            teacher_name TEXT NOT NULL, department_id INTEGER NOT NULL,
            gender_code TEXT, phone TEXT, region_id INTEGER)''')
    rows = {
        '2024':[(1,'T1','100'),(2,'T2','200'),(3,'T3','300')],
        '2025':[(99,'T1',None),(100,'T3',''),(101,'T4','400'),(102,'T4','400')],
    }
    for year, teachers in rows.items():
        conn.executemany(f'INSERT INTO education_{year}.teacher VALUES (?,?,?,1,NULL,?,NULL)',
                         [(ident,number,number,phone) for ident,number,phone in teachers])
    return conn


def prepare(directory):
    context = Context(True, evidence_mode='reviewed')
    for case in CASES:
        ctx = context.build(case['question'])
        payload = {'model':'/models/qwen3-8-27b-fp8','temperature':0,'max_tokens':4096,
                   'chat_template_kwargs':{'enable_thinking':False},
                   'messages':[{'role':'system','content':SYSTEM},
                               {'role':'user','content':json.dumps(ctx,ensure_ascii=False)}]}
        existing = directory/(case['question_id']+'.request.0.json')
        response = directory/(case['question_id']+'.response.0.json')
        if existing.exists() and response.exists() and load(existing)!=payload:
            raise ValueError('目录已有不同上下文的历史响应，请用--directory选择新的验证目录')
        save(case['question_id']+'.context.json',ctx,directory)
        save(case['question_id']+'.request.0.json',payload,directory)
    save('fixture_description.json',{'cases':CASES,'checks':['两年合并','teacher_no去重','2025年NULL/空串不回退旧电话','明确2024年时只查2024']},directory)
    print(str(directory))


def check(directory):
    catalog = Context(True, evidence_mode='reviewed').catalog
    results = []
    with fixture() as conn:
        for case in CASES:
            response = load(directory/(case['question_id']+'.response.0.json'))
            choice = response['choices'][0]
            raw = parse_output(choice['message']['content'])
            ctx = load(directory/(case['question_id']+'.context.json'))
            pred = expand_prediction(raw,ctx,catalog)
            errors = model_query_errors(raw,ctx)+validate(pred,catalog,case['question'],ctx['business_rules'])
            if choice.get('finish_reason')=='length':
                errors.append('输出被截断')
            rows = None
            try:
                if not errors:
                    rows = conn.execute(pred['query']).fetchall()
            except sqlite3.Error as exc:
                errors.append(str(exc))
            passed = not errors and rows==[(case['expected_count'],)]
            results.append({**case,'prediction':pred,'fixture_rows':rows,'errors':errors,'passed':passed})
    report = {'model':'/models/qwen3-8-27b-fp8','synthetic_execution_only':True,'passed':sum(r['passed'] for r in results),
              'total':len(results),'results':results}
    save('verification.json',report,directory)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if report['passed']!=report['total']:
        raise SystemExit(1)


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['prepare','check'])
    parser.add_argument('--directory',type=Path,default=RUNS/'regression'/'teacher-rag-final')
    args = parser.parse_args()
    (prepare if args.command=='prepare' else check)(args.directory)
