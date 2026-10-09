"""Non-ReAct retrieval augmented generation with resumable repair requests."""
import argparse
import hashlib
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from ..shared.context import Context, corrected_prediction
from ..shared.data import load, save, source, queries
from ..shared.paths import ROOT, OUT, RAW, SERVICE, RUNS
from ..shared.prompts import SYSTEM, repair_feedback
from ..shared.generation import api, completion_payload, parse_output, validate, stratified_sample, write_offline_request
from ..shared.education import expand_prediction, model_query_errors
from ..shared.fingerprints import implementation_hash

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['evaluate','predict'])
    parser.add_argument('--base-url',default='http://192.168.15.119:28081/v1')
    parser.add_argument('--model',default='contest-qwen')
    parser.add_argument('--provider',choices=['vllm','deepseek'],default='vllm')
    parser.add_argument('--models-file',type=Path,help='Provider-specific model discovery JSON for offline mode')
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--sample',type=int,default=0)
    parser.add_argument('--sample-file',type=Path,help='Immutable evaluation sample JSON, for paired comparisons')
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--ids',help='Comma-separated question IDs for focused regression runs')
    parser.add_argument('--output',help='Run parent directory; default depends on evaluate/predict')
    parser.add_argument('--offline',action='store_true',help='Write requests and consume response files; use when Python sockets are restricted')
    args = parser.parse_args()
    models = load(args.models_file or SERVICE/'models.json')['data'] if args.offline else api(args.base_url,'/models')['data']
    if args.model not in {x['id'] for x in models}:
        raise ValueError(f'Model unavailable: {args.model}')
    context = Context(args.command=='evaluate')
    rows = load(OUT/'dev.json' if args.command=='evaluate' else RAW/'QA_test.json')
    if args.sample_file:
        if args.command != 'evaluate' or args.sample or args.ids or args.limit:
            parser.error('--sample-file只用于evaluate，不与sample/ids/limit混用')
        frozen = load(args.sample_file)
        current = {row['question_id']:row for row in rows}
        if len({row['question_id'] for row in frozen}) != len(frozen) or any(current.get(row['question_id']) != row for row in frozen):
            parser.error('固定样本必须与当前原始验证题/金标一致，且不能重复')
        rows = frozen
    if args.ids:
        if args.sample or args.limit:
            parser.error('--ids cannot be combined with --sample or --limit')
        wanted = {int(x) for x in args.ids.split(',')}
        rows = [r for r in rows if r['question_id'] in wanted]
        if {r['question_id'] for r in rows} != wanted:
            parser.error('Some requested IDs are absent from this dataset')
    if args.sample:
        if args.limit:
            parser.error('--sample and --limit are mutually exclusive')
        rows = stratified_sample(rows,args.sample,args.seed)
    # Fixed mixed-source smoke set, not the first N MySQL questions.
    if args.limit:
        if args.command=='evaluate':
            buckets = {}
            for row in rows:
                buckets.setdefault(source(row),[]).append(row)
            rows = [bucket[i] for i in range(max(map(len,buckets.values()))) for bucket in buckets.values() if i<len(bucket)][:args.limit]
        else:
            rows = rows[:args.limit]
    run_config = {'sample_ids':[r['question_id'] for r in rows], 'sampling_seed':args.seed, 'model':args.model,'base_url':args.base_url,'system':SYSTEM,
                  'provider':args.provider,
                  'sample_target_hash':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),
                  'data_hash':hashlib.sha256(json.dumps([context.examples,context.catalog,context.knowledge,context.dictionaries,context.ambiguous,context.business_rules,context.resolutions,context.corrections,context.schema_adjudications,context.curation],sort_keys=True).encode()).hexdigest(),
                  'implementation_hash':implementation_hash()}
    fingerprint = hashlib.sha256(json.dumps(run_config,sort_keys=True).encode()).hexdigest()[:12]
    parent = ROOT / args.output if args.output else RUNS / ('evaluation' if args.command=='evaluate' else 'prediction')
    dest = parent / (args.command+'-'+fingerprint)
    dest.mkdir(parents=True,exist_ok=True)
    (dest/'sample.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    (dest/'config.json').write_text(json.dumps(run_config,ensure_ascii=False,indent=2))
    def process(row):
        path = dest/f"{row['question_id']}.json"
        if path.exists():
            cached = load(path)
            if cached.get('prediction') and not cached.get('errors') and cached['question']==row['question']:
                return cached
        ctx = context.build(row['question'])
        messages = [{'role':'system','content':SYSTEM},{'role':'user','content':json.dumps(ctx,ensure_ascii=False)}]
        record = {'question_id':row['question_id'],'question':row['question'],'context':ctx,'attempts':[],'prediction':None}
        corrected = corrected_prediction(ctx,context.catalog)
        if corrected:
            record.update({'prediction':corrected,'errors':[],'prediction_source':'curated_question_correction',
                           'correction_group_id':ctx['question_correction']['group_id'],'seconds':0})
            save(path.name,record,dest)
            return record
        start = time.monotonic()
        for attempt in range(4):
            content = None
            try:
                payload = completion_payload(args.model,messages,args.provider)
                if args.offline:
                    request_path = dest/f"{row['question_id']}.request.{attempt}.json"
                    response_path = dest/f"{row['question_id']}.response.{attempt}.json"
                    write_offline_request(request_path,response_path,payload)
                    if not response_path.exists():
                        record['errors'] = ['pending_request']
                        break
                    response = load(response_path)
                else:
                    response = api(args.base_url,'/chat/completions',payload)
                choice = response['choices'][0]
                content = choice['message'].get('content') or ''
                record['attempts'].append({'content':content,'finish_reason':choice.get('finish_reason'),'usage':response.get('usage')})
                raw_prediction = parse_output(content)
                prediction = expand_prediction(raw_prediction,ctx,context.catalog)
                record['model_prediction'] = raw_prediction
                record['automatic_transforms'] = ['education_view_cte_expansion'] if prediction!=raw_prediction else []
                errors = model_query_errors(raw_prediction,ctx)+validate(prediction,context.catalog,row['question'],ctx['business_rules'])
                if choice.get('finish_reason')=='length':
                    errors.append('输出被截断')
                record['prediction'], record['errors'] = prediction, errors
                if not errors:
                    break
                messages += [{'role':'assistant','content':content},{'role':'user','content':repair_feedback(errors)}]
            except (ValueError, OSError, KeyError) as exc:
                record['errors'] = [str(exc)]
                if content:
                    messages.append({'role':'assistant','content':content})
                messages.append({'role':'user','content':repair_feedback(record['errors'])})
        record['seconds'] = round(time.monotonic()-start,2)
        path.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
        print(f"{row['question_id']}: {'OK' if not record.get('errors') else record['errors']}",flush=True)
        return record
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        records = list(pool.map(process,rows))
    predictions = [{'question_id':r['question_id'],**r['prediction']} for r in records if r['prediction'] and not r.get('errors')]
    (dest/'predictions.json').write_text(json.dumps(predictions,ensure_ascii=False,indent=2)+'\n')
    pending = sum(record.get('errors')==['pending_request'] for record in records)
    metrics = {'count':len(rows),'pending_count':pending,'evaluated_count':len(rows)-pending,
               'valid_count':len(predictions),'format_and_lexical_check_rate':None if pending else len(predictions)/max(1,len(rows)),
               'validation_checks':['format/read-only/table references','SQLite raw-DDL EXPLAIN','MySQL FULL JOIN restriction','ES nested clauses/mapping','question-derived rules'],
               'execution_accuracy':None}
    if args.command=='evaluate':
        targets = {r['question_id']:r for r in rows}
        metrics['source_accuracy'] = None if pending else sum(source(p)==source(targets[p['question_id']]) for p in predictions)/len(rows)
        metrics['query_exact_match'] = None if pending else sum(source(p)==source(targets[p['question_id']]) and queries(p)==queries(targets[p['question_id']]) for p in predictions)/len(rows)
    (dest/'metrics.json').write_text(json.dumps(metrics,indent=2)+'\n')
    print(dest, json.dumps(metrics),flush=True)


if __name__ == '__main__':
    main()
