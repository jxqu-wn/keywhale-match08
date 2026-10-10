"""Random SQLite training sample, full-evidence DeepSeek ReAct, label comparison."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sys
import time
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from text2query.shared.context import Context
from text2query.shared.data import load, source
from text2query.shared.fingerprints import implementation_hash
from text2query.shared.generation import api
from text2query.shared.paths import RAW
from text2query.react.pipeline import react_system, WORKFLOW_VERSION, run_question, summarize


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def normalize_layout(sql):
    # Preserve quoted literals/identifiers, operators, keywords and alias names.
    # Only collapse whitespace outside quotes and drop a final semicolon.
    tokens = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\s+|[^\s'\"`]+", sql)
    return ''.join(' ' if token.isspace() else token for token in tokens).strip().rstrip(';').strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=20261010)
    parser.add_argument('--count', type=int, default=40)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--max-steps', type=int, default=24)
    parser.add_argument('--evidence-mode', choices=['raw-only','reviewed'], default='raw-only')
    parser.add_argument('--semantic-review', action='store_true', help='启用独立LLM语义复核；当前默认关闭')
    parser.add_argument('--auth-header-file', type=Path)
    parser.add_argument('--reference-run', type=Path,
                        help='Reuse and verify a prior run\'s frozen sample and evidence configuration')
    parser.add_argument('--compact-context', action='store_true',
                        help='Apply the same duplicate-evidence compaction used for the Qwen sample')
    parser.add_argument('--transport-retry-count', type=int, default=2)
    args = parser.parse_args()
    if args.workers < 1 or not 5 <= args.max_steps <= 24 or args.transport_retry_count < 0:
        parser.error('workers must be positive; max-steps must be 5..24')
    if args.auth_header_file:
        lines = args.auth_header_file.read_text().splitlines()
        authorization = next(line for line in lines if line.startswith('Authorization: Bearer '))
        os.environ['MODEL_API_KEY'] = authorization.removeprefix('Authorization: Bearer ').strip()
    elif not os.getenv('MODEL_API_KEY'):
        parser.error('MODEL_API_KEY or --auth-header-file is required')
    candidates = [row for row in load(RAW / 'QA_train.json') if source(row) == 'SQLite']
    if not 0 < args.count <= len(candidates):
        parser.error('invalid sample count')
    rows = random.Random(args.seed).sample(candidates, args.count)
    context = Context(False, raw_evidence=True, evidence_mode=args.evidence_mode)
    reference_config = None
    if args.reference_run:
        reference_config = load(args.reference_run / 'config.json')
        reference_rows = load(args.reference_run / 'sample.json')
        if rows != reference_rows:
            parser.error('Seed/count/current training data do not match the frozen reference sample')
        for key, value in {'implementation_hash': implementation_hash(),
                           'workflow_version': WORKFLOW_VERSION,
                           'system': react_system(context.raw_only, args.semantic_review),
                           'semantic_review': args.semantic_review,
                           'evidence_manifest': context.evidence_manifest,
                           'max_steps': args.max_steps,
                           'compact_context': args.compact_context}.items():
            if reference_config.get(key) != value:
                parser.error('Reference configuration differs for ' + key)
        rows = reference_rows
    models = api('https://api.deepseek.com', '/models')
    if not any(model['id'] == 'deepseek-flash' for model in models['data']):
        parser.error('deepseek-flash is not available from the service')
    args.output.mkdir(parents=True, exist_ok=True)
    detail = args.output / 'questions'
    detail.mkdir(exist_ok=True)
    wire = args.output / 'wire'
    wire.mkdir(exist_ok=True)
    config = {'model':'deepseek-flash', 'provider':'deepseek', 'base_url':'https://api.deepseek.com',
        'strategy':'react', 'workflow_version':WORKFLOW_VERSION, 'thinking':{'type':'disabled'},
        'seed':args.seed, 'sample_count':len(rows), 'population_count':len(candidates),
        'sample_ids':[row['question_id'] for row in rows],
        'sampling':'random.Random(seed).sample(raw SQLite QA_train rows, count)',
        'gold':'original QA_train annotations, no curation applied to evaluation targets',
        'retrieval':'production preprocessing and retrieval; all training cases retained',
        'evidence_mode':args.evidence_mode,
        'semantic_review':args.semantic_review,
        'max_steps':args.max_steps, 'evidence_manifest':context.evidence_manifest,
        'input_sha256':hashlib.sha256((RAW/'QA_train.json').read_bytes()).hexdigest(),
        'implementation_hash':implementation_hash(), 'system':react_system(context.raw_only, args.semantic_review),
        'temperature':0, 'max_output_tokens':4096, 'workers':args.workers,
        'compact_context':args.compact_context, 'transport_retry_count':args.transport_retry_count,
        'gold_excluded_from_retrieval':False, 'held_out_example_count':0,
        'reference_run':str(args.reference_run) if args.reference_run else None,
        'reference_config_sha256':hashlib.sha256((args.reference_run/'config.json').read_bytes()).hexdigest()
            if args.reference_run else None,
        'sample_sha256':hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    config_path = args.output / 'config.json'
    if config_path.exists() and load(config_path) != config:
        parser.error('existing output config differs; choose a new output directory')
    save(config_path, config)
    save(args.output / 'models.json', models)
    save(args.output / 'sample.json', rows)
    records = {}
    started = time.monotonic()
    transport_retries = []

    def make_call(question_id):
        calls = []

        def call(base, path, payload):
            actual = copy.deepcopy(payload)
            if args.compact_context:
                from sample_sqlite_train_react import compact_messages
                actual['messages'] = compact_messages(payload['messages'])
            if args.reference_run:
                allowed_files = {'data/raw/subject/QA_train.json',
                                 'data/raw/subject/Schema/bird_schemas.txt',
                                 'data/raw/subject/knowledge.json'}

                def verify_sources(value):
                    if isinstance(value, dict):
                        provenance = value.get('provenance')
                        if isinstance(provenance, dict) and provenance.get('file') not in allowed_files:
                            raise ValueError('Evidence provenance outside approved SQLite evaluation scope')
                        for item in value.values():
                            verify_sources(item)
                    elif isinstance(value, list):
                        for item in value:
                            verify_sources(item)

                for message in actual['messages']:
                    if message['role'] == 'user':
                        verify_sources(json.loads(message['content']))
            index = len(calls)
            stem = wire / f'{question_id}.call.{index}'
            entry = {'index':index, 'requested_max_tokens':payload['max_tokens'],
                     'actual_max_tokens':actual['max_tokens'],
                     'duplicate_representation_compaction':args.compact_context,
                     'messages_unchanged':actual['messages'] == payload['messages']}
            calls.append(entry)
            save(Path(str(stem) + '.request.json'), actual)
            started_call = time.monotonic()
            try:
                for attempt in range(args.transport_retry_count + 1):
                    try:
                        response = api(base, path, actual)
                        break
                    except urllib.error.HTTPError as exc:
                        if exc.code not in (429, 500, 502, 503, 504) or attempt == args.transport_retry_count:
                            raise
                        transport_retries.append({'question_id':question_id, 'call':index,
                                                  'status':exc.code, 'attempt':attempt+1})
                        time.sleep(2 ** attempt)
                save(Path(str(stem) + '.response.json'), response)
                entry['usage'] = response.get('usage')
                entry['finish_reason'] = response['choices'][0].get('finish_reason')
                return response
            except Exception as exc:
                entry['error'] = str(exc)
                raise
            finally:
                entry['seconds'] = round(time.monotonic() - started_call, 3)
                save(Path(str(stem) + '.budget.json'), entry)
                save(wire / f'{question_id}.calls.json', calls)

        return call

    def export():
        comparisons = []
        ordered = [records[row['question_id']] for row in rows if row['question_id'] in records]
        for row in rows:
            if row['question_id'] not in records:
                continue
            record = records[row['question_id']]
            prediction = record.get('prediction')
            successful = bool(prediction) and not record.get('errors')
            exact = successful and prediction.get('db') == row['db'] and prediction.get('query') == row['query']
            layout_equal = (successful and prediction.get('db') == row['db'] and
                            isinstance(prediction.get('query'), str) and
                            normalize_layout(prediction['query']) == normalize_layout(row['query']))
            comparisons.append({'question_id':row['question_id'], 'question':row['question'],
                'gold':{'db':row['db'], 'query':row['query']}, 'prediction':prediction,
                'successful':successful, 'exact_equal':bool(exact),
                'layout_normalized_equal':bool(layout_equal), 'errors':record.get('errors', [])})
        successes = sum(row['successful'] for row in comparisons)
        complete = len(ordered) == len(rows)
        exact_different = sum(row['successful'] and not row['exact_equal'] for row in comparisons)
        layout_different = sum(row['successful'] and not row['layout_normalized_equal'] for row in comparisons)
        summary = {**summarize(ordered), 'total_count':len(rows), 'completed_count':len(ordered),
            'seed':args.seed, 'population_count':len(candidates),
            'elapsed_seconds':round(time.monotonic()-started, 1),
            'exact_equal_count':sum(row['exact_equal'] for row in comparisons),
            'exact_different_count':exact_different,
            'exact_different_ratio':exact_different/len(rows) if complete and successes == len(rows) else None,
            'layout_normalized_equal_count':sum(row['layout_normalized_equal'] for row in comparisons),
            'layout_normalized_different_count':layout_different,
            'layout_normalized_different_ratio':layout_different/len(rows) if complete and successes == len(rows) else None,
            'generation_failure_count':len(ordered)-successes,
            'differences_plus_failures_ratio':(len(rows)-sum(row['exact_equal'] for row in comparisons))/len(rows) if complete else None,
            'transport_retries':list(transport_retries),
            'source_counts':dict(Counter(source(row['prediction']) for row in comparisons if row['successful'])),
            'evaluation_scope':'query string comparison, not production execution accuracy',
            'gold_excluded_from_retrieval':False,
            'held_out_example_count':0}
        save(args.output/'predictions.json', [{'question_id':record['question_id'], **record['prediction']}
             for record in ordered if record.get('prediction') and not record.get('errors')])
        save(args.output/'comparison.json', {'summary':summary, 'questions':comparisons})
        save(args.output/'metrics.json', summary)
        return summary

    def process(row):
        return run_question({'question_id':row['question_id'], 'question':row['question']},
            context, detail, config['model'], config['base_url'], max_steps=args.max_steps,
            provider='deepseek', call=make_call(row['question_id']), semantic_review=args.semantic_review)

    print(json.dumps({'event':'started', 'count':len(rows), 'seed':args.seed,
        'held_out_examples':0}, ensure_ascii=False), flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process, row):row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {'question_id':row['question_id'], 'question':row['question'],
                          'prediction':None, 'errors':[type(exc).__name__], 'trace':[]}
                save(detail/f"{row['question_id']}.json", result)
            records[row['question_id']] = result
            print(json.dumps({'event':'question_completed', 'question_id':row['question_id'],
                'completed':len(records), 'errors':result.get('errors', [])}, ensure_ascii=False), flush=True)
            export()
    print(json.dumps({'event':'finished', **export()}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
