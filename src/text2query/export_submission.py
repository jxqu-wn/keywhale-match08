"""Export model records only when every test question passes the basic checks."""
import argparse
import hashlib
import json
from pathlib import Path
from .pipeline import load
from .paths import RAW, OUT, SUBMISSION
from .model_pipeline import validate


def export(directory, output):
    test = load(RAW/'QA_test.json')
    catalog = load(OUT/'catalog.json')
    predictions, problems = [], []
    for row in test:
        path = directory/f"{row['question_id']}.json"
        if not path.exists():
            problems.append({'question_id':row['question_id'],'errors':['missing record']})
            continue
        record = load(path)
        pred = record.get('prediction')
        errors = validate(pred,catalog,row['question'],record.get('context',{}).get('business_rules',[])) if isinstance(pred,dict) else ['missing prediction']
        if record.get('errors'):
            errors += record['errors']
        if record.get('question') != row['question']:
            errors.append('question mismatch')
        if errors:
            problems.append({'question_id':row['question_id'],'errors':errors})
        else:
            predictions.append({'question_id':row['question_id'],**pred})
    if problems:
        (directory/'export_errors.json').write_text(json.dumps(problems,ensure_ascii=False,indent=2)+'\n')
        raise ValueError(f'{len(problems)} records unresolved; result.json was not exported')
    ids = [r['question_id'] for r in predictions]
    if len(ids)!=len(set(ids)) or set(ids)!={r['question_id'] for r in test}:
        raise ValueError('ID coverage mismatch')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(predictions,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    report = {'count':len(predictions),'unique_ids':len(set(ids)),'coverage':'complete',
              'encoding':'UTF-8','sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
              'source_counts':{},'execution_validated':False,
              'model_run':str(directory),'manual_query_edits':False}
    for row in predictions:
        key = row.get('db') or row['db1']+'->'+row['db2']
        report['source_counts'][key] = report['source_counts'].get(key,0)+1
    (output.parent/'submission_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    parser.add_argument('--output',type=Path,default=SUBMISSION/'result.json')
    args = parser.parse_args()
    export(args.directory,args.output)
