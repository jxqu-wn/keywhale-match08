"""Compare local retrieval/preflight checks with the frozen pre-change snapshots."""
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text2query.shared.context import Context
from text2query.shared.generation import validate
from text2query.shared.curation import load_overlays, hours_cte
from text2query.shared.paths import ROOT, RAW
from text2query.shared.validation import sqlite_connection

directory = ROOT/'artifacts/runs/regression/rag-improvements-20261009'
baseline = json.loads((directory/'baseline.json').read_text())
before = {r['question_id']:r for r in json.loads((directory/'before_contexts.json').read_text())}
questions = {r['question_id']:r['question'] for r in json.loads((RAW/'QA_test.json').read_text())}
ctx = Context(True, evidence_mode='reviewed')
after = {qid:ctx.build(questions[qid]) for qid in before}
(directory/'after_contexts.json').write_text(json.dumps([dict(question_id=qid,**c) for qid,c in after.items()],ensure_ascii=False,indent=2)+'\n')
required = {3576:[51,83,1248,1249],4682:[188,189],442:[1624,1625],4207:[159,163]}
facts = []
for qid, kids in required.items():
    for kid in kids:
        facts.append({'question_id':qid,'knowledge_id':kid,
                      'before_present':kid in {k['knowledge_id'] for k in before[qid]['knowledge']},
                      'after_present':kid in {k['knowledge_id'] for k in after[qid]['knowledge']}})
assert all(f['after_present'] for f in facts)
rows = json.loads((ROOT/'outputs/submission/result.json').read_text())
structure = []
for row in rows:
    prediction = {k:v for k,v in row.items() if k!='question_id'}
    structure.append({'question_id':row['question_id'],'errors':validate(prediction,ctx.catalog)})
flagged = {r['question_id'] for r in structure if r['errors']}
assert {4976,5029,5686,1825,2399} <= flagged
overlays = {p['question_id']:p for p in load_overlays()['example_patches']}
candidates = {
    4123:overlays[4298]['replacement']['query'].replace("'Pets'","'Automotive'"),
    3915:overlays[3772]['replacement']['query'],
    4207:hours_cte()+" SELECT h.business_id FROM normalized_hours h JOIN public_review_platform.Days d ON d.day_id=h.day_id WHERE d.day_of_week='Sunday' AND h.hours>12",
    4324:overlays[4401]['replacement']['query'].replace('2006','2012').split(' ORDER BY')[0],
}
cases = {c['question_id']:c for c in json.loads((ROOT/'artifacts/analysis/submission-210e08596136/counterexamples.json').read_text())}
executions = []
for qid, sql in candidates.items():
    c = cases[qid]; db = sqlite_connection(ctx.catalog,sql)
    try:
        for table, records in c['tables'].items():
            database,name = table.split('.',1)
            for row in records:
                columns=','.join('"'+col+'"' for col in row)
                values=','.join('?' for _ in row)
                db.execute(f'INSERT INTO "{database}"."{name}" ({columns}) VALUES ({values})',list(row.values()))
        actual = [list(row) for row in db.execute(sql).fetchall()]
    finally:
        db.close()
    passed = actual==c['expected']
    assert passed, qid
    executions.append({'question_id':qid,'before':c['actual'],'after':actual,'expected':c['expected'],
                       'query':sql,'passed':passed,'source':'reviewed training-pattern adaptation or helper, NOT model output',
                       'synthetic_data_only':True})
unchanged = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest for name,digest in baseline['raw_sha256'].items()}
assert all(unchanged.values())
assert hashlib.sha256((ROOT/'data/processed/dev.json').read_bytes()).hexdigest()==baseline['dev_sha256']
assert hashlib.sha256((ROOT/'outputs/submission/result.json').read_bytes()).hexdigest()==baseline['submission_sha256']
result = {'required_fact_count':len(facts),'required_facts_before':sum(f['before_present'] for f in facts),
          'required_facts_after':sum(f['after_present'] for f in facts),'facts':facts,
          'old_submission_structure_flags':structure,'five_known_structure_errors_blocked':True,
          'pattern_executions':executions,'synthetic_pattern_passed':len(executions),
          'raw_files_unchanged':unchanged,'dev_targets_unchanged':True,'submission_unchanged':True,
          'model_inference_verified':False,'official_execution_accuracy':None}
(directory/'local_verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:result[k] for k in ('required_fact_count','required_facts_before','required_facts_after','five_known_structure_errors_blocked','synthetic_pattern_passed','dev_targets_unchanged','submission_unchanged')},ensure_ascii=False))
