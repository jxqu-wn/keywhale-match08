"""Compare two frozen ReAct samples and audit the actual evidence sent."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sample_sqlite_train_flash import normalize_layout, save
from sample_sqlite_train_react import compact_messages
from text2query.shared.data import load, schema_catalog
from text2query.shared.paths import RAW


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from dictionaries(item)
    elif isinstance(value, list):
        for item in value:
            yield from dictionaries(item)


def messages_hash(messages):
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def audit(directory):
    catalog, _ = schema_catalog(preserve_raw=True)
    schemas = {(s['source'], s.get('database'), s['table']): s for s in catalog}
    cases = {r['question_id']: r for r in load(RAW / 'QA_train.json')}
    knowledge = {r['knowledge_id']: r for r in load(RAW / 'knowledge.json')}
    counts = Counter()
    sources = Counter()
    issues = []
    budgets = []
    config = load(directory / 'config.json')
    for row in load(directory / 'sample.json'):
        qid = row['question_id']
        original_messages = {messages_hash(compact_messages(load(path)['messages']))
                             for path in (directory / 'questions').glob(f'{qid}.*request*.json')}
        first = load(directory / 'wire' / f'{qid}.call.0.request.json')
        first_user = json.loads(first['messages'][1]['content'])
        if any(item['question_id'] == qid and item['query'] == row['query']
               for item in first_user.get('exact_examples', [])):
            counts['first_request_contains_own_gold_count'] += 1
        for path in (directory / 'wire').glob(f'{qid}.call.*.request.json'):
            request = load(path)
            counts['wire_requests'] += 1
            if messages_hash(request['messages']) in original_messages:
                counts['compaction_verified_requests'] += 1
            else:
                issues.append({'request':path.name, 'issue':'unexpected message change'})
            for message in request['messages']:
                if message['role'] != 'user':
                    continue
                try:
                    content = json.loads(message['content'])
                except ValueError:
                    continue
                for item in dictionaries(content):
                    if isinstance(item.get('provenance'), dict):
                        file = item['provenance']['file']
                        sources[file] += 1
                        if not file.startswith('data/raw/'):
                            issues.append({'request':path.name, 'issue':'non-raw provenance', 'source':file})
                    if 'knowledge_id' in item and 'knowledge_text' in item:
                        counts['knowledge_occurrences'] += 1
                        if item['knowledge_text'] != knowledge[item['knowledge_id']]['knowledge_text']:
                            issues.append({'request':path.name, 'issue':'modified knowledge'})
                    if 'question_id' in item and 'question' in item and 'query' in item:
                        counts['example_occurrences'] += 1
                        if item['query'] != cases[item['question_id']]['query']:
                            issues.append({'request':path.name, 'issue':'modified example'})
                    if 'raw_schema' in item and 'table' in item:
                        counts['schema_occurrences'] += 1
                        expected = schemas[(item['source'], item.get('database'), item['table'])]
                        if item['raw_schema'] != expected['raw_schema']:
                            issues.append({'request':path.name, 'issue':'modified schema'})
                    for key in ('business_rules', 'corrected_examples', 'schema_adjudications', 'query_helpers'):
                        if item.get(key):
                            issues.append({'request':path.name, 'issue':'reviewed evidence', 'field':key})
        budgets.extend(load(path) for path in (directory / 'wire').glob(f'{qid}.call.*.budget.json'))
    root = Path(__file__).resolve().parents[1]
    hashes_match = all(hashlib.sha256((root / r['file']).read_bytes()).hexdigest() == r['sha256']
                       for r in config['evidence_manifest']['files'])
    result = {**counts, 'source_occurrences':dict(sources), 'source_hashes_match':hashes_match,
        'usage':{key:sum((r.get('usage') or {}).get(key, 0) for r in budgets)
                 for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')},
        'finish_reasons':dict(Counter(r.get('finish_reason') for r in budgets)), 'issues':issues}
    if issues or not hashes_match or counts['compaction_verified_requests'] != counts['wire_requests']:
        raise ValueError('Evidence audit failed: ' + json.dumps(result, ensure_ascii=False))
    save(directory / 'comparison_evidence_audit.json', result)
    return result


def trace_summary(directory, qid, gold):
    record = load(directory / 'questions' / f'{qid}.json')
    checks = [t for t in record['trace'] if t['action'] == 'check_query']
    first = checks[0]['args']['prediction'] if checks else None
    verdicts = [(t['observation'].get('semantic_review') or {}).get('verdict') for t in checks]
    if record.get('prediction') == gold:
        origin = 'none'
    elif first != gold:
        origin = 'generator_changed_before_review'
    elif 'revise' in verdicts:
        origin = 'semantic_review_requested'
    elif 'uncertain' in verdicts:
        origin = 'semantic_review_uncertain_guidance'
    else:
        origin = 'generator_changed_after_review'
    return {'check_count':len(checks),
        'check_failure_count':sum(not t['observation']['ok'] for t in checks),
        'first_checked_candidate':first,
        'first_checked_candidate_equals_gold':first == gold,
        'change_origin':origin,
        'api_calls':record.get('api_calls'),
        'semantic_review_api_calls':record.get('semantic_review_api_calls'),
        'actions':[t['action'] for t in record['trace']],
        'checks':[{'step':t['step'], 'candidate':t['args']['prediction'],
                   'case_review':t['args'].get('case_review'),
                   'semantic_review':t['observation'].get('semantic_review'),
                   'errors':t['observation'].get('errors', [])} for t in checks]}


def compare(reference, candidate):
    reference_sample, candidate_sample = load(reference / 'sample.json'), load(candidate / 'sample.json')
    if reference_sample != candidate_sample:
        raise ValueError('Samples differ')
    configs = {'reference':load(reference / 'config.json'), 'candidate':load(candidate / 'config.json')}
    keys = ('implementation_hash','workflow_version','system','evidence_manifest','max_steps',
            'compact_context','temperature','max_output_tokens','workers','sample_ids')
    parity = {key:configs['reference'][key] == configs['candidate'][key] for key in keys}
    if not all(parity.values()):
        raise ValueError('Comparison settings differ: ' + str(parity))
    audits = {key:audit(path) for key, path in [('reference',reference), ('candidate',candidate)]}
    originals = {key:{r['question_id']:r for r in load(path / 'comparison.json')['questions']}
                 for key,path in [('reference',reference), ('candidate',candidate)]}
    counts = Counter()
    origin_counts = {'reference':Counter(), 'candidate':Counter()}
    questions = []
    for row in reference_sample:
        qid = row['question_id']
        qr, dr = originals['reference'][qid], originals['candidate'][qid]
        if not qr['successful'] or not dr['successful']:
            raise ValueError('A question has not completed successfully: ' + str(qid))
        qsql, dsql = qr['prediction']['query'], dr['prediction']['query']
        equal = qr['prediction'] == dr['prediction']
        layout_equal = (qr['prediction']['db'] == dr['prediction']['db'] and
                        normalize_layout(qsql) == normalize_layout(dsql))
        counts['between_models_exact_equal'] += equal
        counts['between_models_layout_equal'] += layout_equal
        initial_messages_equal = (load(reference / 'wire' / f'{qid}.call.0.request.json')['messages']
                                  == load(candidate / 'wire' / f'{qid}.call.0.request.json')['messages'])
        counts['identical_initial_messages'] += initial_messages_equal
        outcome = ('both_match' if qr['exact_equal'] and dr['exact_equal'] else
                   'reference_only_matches' if qr['exact_equal'] else
                   'candidate_only_matches' if dr['exact_equal'] else 'neither_matches')
        counts[outcome] += 1
        qt, dt = trace_summary(reference,qid,qr['gold']), trace_summary(candidate,qid,dr['gold'])
        for key,r,t in [('reference',qr,qt),('candidate',dr,dt)]:
            if not r['exact_equal']:
                origin_counts[key][t['change_origin']] += 1
        questions.append({'question_id':qid, 'question':row['question'], 'gold':qr['gold'],
            'reference':{'model':configs['reference']['model'], 'prediction':qr['prediction'],
                         'gold_exact_equal':qr['exact_equal'],
                         'gold_layout_equal':qr['layout_normalized_equal'], 'trace_summary':qt},
            'candidate':{'model':configs['candidate']['model'], 'prediction':dr['prediction'],
                         'gold_exact_equal':dr['exact_equal'],
                         'gold_layout_equal':dr['layout_normalized_equal'], 'trace_summary':dt},
            'models_exact_equal':equal, 'models_layout_equal':layout_equal,
            'initial_messages_equal':initial_messages_equal,'gold_match_outcome':outcome})
    count = len(questions)
    summary = {'sample_count':count,'settings_parity':parity, **counts,
        'between_models_exact_different':count-counts['between_models_exact_equal'],
        'between_models_exact_difference_ratio':(count-counts['between_models_exact_equal'])/count,
        'between_models_layout_different':count-counts['between_models_layout_equal'],
        'between_models_layout_difference_ratio':(count-counts['between_models_layout_equal'])/count,
        'difference_origins':{key:dict(value) for key,value in origin_counts.items()},
        'reference_metrics':load(reference/'metrics.json'),
        'candidate_metrics':load(candidate/'metrics.json'),
        'reference_run':str(reference),'candidate_run':str(candidate),
        'gold_excluded_from_retrieval':False, 'execution_accuracy':None,
        'limitation':'Same gold cases retained; each model generates AND reviews itself. Provider context limits and output budgets differ.'}
    return {'summary':summary,'questions':questions,'evidence_audits':audits}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-run', type=Path, required=True)
    parser.add_argument('--candidate-run', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = compare(args.reference_run, args.candidate_run)
    save(args.output or args.candidate_run / 'paired_comparison.json', result)
    print(json.dumps(result['summary'], ensure_ascii=False, indent=2))
