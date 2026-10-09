"""Nearest-example retrieval baseline; no model calls."""
import argparse
import json
from ..shared.data import load, save, Retriever, source, queries, references
from ..shared.paths import OUT, RAW, BASELINE

def baseline(evaluate=False):
    train = load(OUT/'train.json' if evaluate else OUT/'clean.json')
    inputs = load(OUT/'dev.json' if evaluate else RAW/'QA_test.json')
    retriever = Retriever([x['question'] for x in train])
    knowledge = load(OUT/'knowledge.json')
    kr = Retriever([x['knowledge_text'] for x in knowledge])
    predictions, contexts = [], []
    route_hits = query_hits = 0
    for row in inputs:
        hits = retriever.search(row['question'])
        best = train[hits[0][1]]
        prediction = {'question_id':row['question_id'], **{k:v for k,v in best.items() if k not in ('question','question_id')}}
        predictions.append(prediction)
        context = {'question_id':row['question_id'],'question':row['question'],
                   'mode':'retrieval_copy_baseline', 'examples':[{'score':round(s,4),**train[i]} for s,i in hits],
                   'knowledge':[knowledge[i] for s,i in kr.search(row['question']) if s>0],
                   'candidate_tables':sorted(set(r for _,i in hits for q in queries(train[i]) for r in references(q)))}
        contexts.append(context)
        if evaluate:
            route_hits += source(best)==source(row)
            query_hits += queries(best)==queries(row) and source(best)==source(row)
    prefix = 'dev' if evaluate else 'test'
    save(prefix+'_baseline_predictions.json', predictions, BASELINE)
    save(prefix+'_contexts.json', contexts, BASELINE)
    if evaluate:
        metrics = {'count':len(inputs),'source_accuracy':route_hits/max(1,len(inputs)),
                   'query_exact_match':query_hits/max(1,len(inputs)),
                   'limitations':'Copies nearest training query without parameter adaptation; no execution accuracy measured.'}
        save('baseline_metrics.json',metrics, BASELINE)
        print(json.dumps(metrics,ensure_ascii=False))
    else:
        print(f'Wrote {len(predictions)} provisional predictions and retrieval contexts')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['evaluate','predict'])
    args=parser.parse_args()
    baseline(args.command=='evaluate')
