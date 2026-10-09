"""Compare raw/processed evidence and audit actual first-prompt exact-case coverage."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from text2query.model_pipeline import Context
from text2query.paths import RAW, OUT, REFERENCE
from text2query.pipeline import load, normalize, Retriever
from text2query.react_pipeline import initial_prompt, REACT_SYSTEM
from text2query.react_tools import QueryTools


def save(directory, name, value):
    (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/analysis/react-evidence-v2'))
    parser.add_argument('--ids', default='3040,3041,3044,3090,3099,3877,6204')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    context = Context(False, raw_evidence=True)
    raw = load(RAW/'QA_train.json'); clean = load(OUT/'clean.json')
    by_id = {r['question_id']:r for r in clean}
    missing = [r for r in raw if r['question_id'] not in by_id]
    changed = [r for r in raw if r['question_id'] in by_id and any(
        r.get(k) != by_id[r['question_id']].get(k) for k in ('db','query','db1','query1','db2','query2'))]
    raw_k = load(RAW/'knowledge.json'); old_k = {r['knowledge_id']:r for r in load(OUT/'knowledge.json')}
    changed_k = [r for r in raw_k if r['knowledge_text'] != old_k.get(r['knowledge_id'],{}).get('knowledge_text')]
    old_schema = load(OUT/'catalog.json')
    old_tables = {(t['source'],t.get('database'),t['table']):t for t in old_schema}
    annotated = [t for t in context.catalog if t.get('table_comments')]
    comparisons = []
    old_sr = Retriever([json.dumps(t,ensure_ascii=False) for t in old_schema])
    for table in annotated:
        key = (table['source'],table.get('database'),table['table'])
        previous = old_tables.get(key,{})
        previous_text = json.dumps(previous,ensure_ascii=False)
        for comment in table['table_comments']:
            def ranking(retriever, catalog):
                return next((rank+1 for rank,(score,i) in enumerate(retriever.search(comment,len(catalog)))
                             if score>0 and (catalog[i]['source'],catalog[i].get('database'),catalog[i]['table'])==key),None)
            comparisons.append({'source':table['source'],'database':table.get('database'),'table':table['table'],
                'comment':comment,'present_before':comment in previous_text,'present_after':comment in table['raw_schema'],
                'rank_before':ranking(old_sr,old_schema),'rank_after':ranking(context.sr,context.catalog),
                'raw_schema':table['raw_schema'],'processed_schema':previous})
    raw_docs = context.dictionaries; old_docs = load(OUT/'dictionary_documents.json')
    canon = lambda text:text.replace('\r\n','\n').strip()
    old_texts = {canon(d['text']) for d in old_docs}
    missing_docs = [d for d in raw_docs if canon(d['text']) not in old_texts]
    summary = {'examples':{'raw_count':len(raw),'processed_clean_count':len(clean),'missing_record_count':len(missing),
                   'changed_annotation_count':len(changed),'v2_retained_count':len(context.examples),
                   'v2_status_counts':dict(Counter(r['annotation_status'] for r in context.examples))},
        'knowledge':{'raw_count':len(raw_k),'processed_count':len(old_k),'changed_text_count':len(changed_k),
                     'v2_retains_original_and_reviewed_text':all(r['raw_annotation']==raw_k[i] for i,r in enumerate(context.knowledge))},
        'schema':{'raw_table_count':len(context.catalog),'processed_table_count':len(old_schema),
                  'table_comment_count':len(comparisons),'comments_absent_before':sum(not r['present_before'] for r in comparisons),
                  'comments_retained_after':sum(r['present_after'] for r in comparisons),
                  'comment_top1_before':sum(r['rank_before']==1 for r in comparisons),
                  'comment_top1_after':sum(r['rank_after']==1 for r in comparisons),
                  'comment_top5_before':sum(r['rank_before'] is not None and r['rank_before']<=5 for r in comparisons),
                  'comment_top5_after':sum(r['rank_after'] is not None and r['rank_after']<=5 for r in comparisons)},
        'dictionary':{'raw_insert_count':len(raw_docs),'processed_insert_count':len(old_docs),
                      'raw_inserts_missing_or_changed_in_processed':len(missing_docs)},
        'reference_documents':{'raw_text_file_count':len(context.reference_documents),
                              'previous_context_retained_text_file_count':0}}
    # Stratified deterministic samples: changed, missing, unchanged; field-level originals kept.
    sample = {'examples_missing':missing[:5],
              'examples_changed':[{'raw':r,'processed':by_id[r['question_id']]} for r in changed[:5]],
              'examples_unchanged':[{'raw':r,'processed':by_id[r['question_id']]} for r in raw
                                    if r['question_id'] in by_id and r not in changed][:3],
              'knowledge_changed':[{'raw':r,'processed':old_k[r['knowledge_id']]} for r in changed_k],
              'knowledge_unchanged':[{'raw':r,'processed':old_k[r['knowledge_id']]} for r in raw_k if r not in changed_k][:3],
              'schema_comment_comparisons':comparisons,
              'dictionary_missing_or_changed':missing_docs[:3],
              'dictionary_unchanged_samples':[{'raw':d,'processed':next(o for o in old_docs if canon(o['text'])==canon(d['text']))}
                                               for d in raw_docs if canon(d['text']) in old_texts][:3],
              'reference_documents':context.reference_documents}
    save(args.output,'raw_processed_comparison.json',sample)
    save(args.output,'retention_summary.json',summary)
    audits = {}
    for name,rows in [('raw_train',raw),('qa_test',load(RAW/'QA_test.json'))]:
        exact_available=0; complete=0; literal_available=0; entries=[]
        for i,row in enumerate(rows):
            expected = context.exact_examples(row['question'])
            prompt = initial_prompt(context.build(row['question']))
            injected = prompt['exact_examples']
            expected_ids = {r['question_id'] for r in expected}
            actual_ids = {r['question_id'] for r in injected}
            same_literal = [r for r in expected if r['question']==row['question']]
            if expected:
                exact_available+=1;complete+=int(expected_ids==actual_ids)
            literal_available += bool(same_literal)
            assert expected_ids==actual_ids
            entries.append({'question_id':row['question_id'],'question':row['question'],
                            'expected_exact_ids':sorted(expected_ids),'injected_exact_ids':sorted(actual_ids)})
            if (i+1)%250==0:print(name,i+1,flush=True)
        audits[name]={'question_count':len(rows),'literal_case_available_count':literal_available,
                      'normalized_exact_case_available_count':exact_available,'complete_injection_count':complete,
                      'injection_rate_when_case_exists':complete/exact_available if exact_available else None,
                      'exact_case_availability_rate':exact_available/len(rows),'details':entries}
    save(args.output,'exact_injection_coverage.json',audits)
    selected={r['question_id']:r for r in raw}
    for qid in map(int,args.ids.split(',')):
        row=selected[qid]; ctx=context.build(row['question']); prompt=initial_prompt(ctx)
        save(args.output,f'{qid}.initial_request.json',{'messages':[{'role':'system','content':REACT_SYSTEM},
             {'role':'user','content':json.dumps(prompt,ensure_ascii=False)}]})
        tools=QueryTools(context,ctx)
        examples=tools.dispatch('search_examples',{})
        names=sorted({f"{ref['database']}.{ref['table']}" for example in examples['exact_matches']
                      for ref in example['schema_refs']})
        schema=tools.dispatch('get_schema',{'tables':names}) if names else tools.dispatch('get_schema',{})
        knowledge=tools.dispatch('search_knowledge',{})
        save(args.output,f'{qid}.evidence.json',{'question':row['question'],'initial_prompt':prompt,
             'search_examples':examples,'get_schema':schema,'search_knowledge':knowledge})
    print(json.dumps({'retention':summary,'coverage':{k:{x:y for x,y in v.items() if x!='details'} for k,v in audits.items()}},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
