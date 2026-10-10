"""Run a reproducible SQLite training sample with the existing ReAct workflow."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import hashlib
import json
from pathlib import Path
import random
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from text2query.shared.context import Context
from text2query.shared.data import load, source
from text2query.shared.fingerprints import implementation_hash
from text2query.shared.paths import RAW
from text2query.react.pipeline import react_system, WORKFLOW_VERSION, run_question, summarize
from sample_sqlite_train_flash import normalize_layout, save


def compact_evidence(value):
    """Remove duplicate representations, retaining question/SQL/DDL/knowledge text."""
    if isinstance(value, list):
        return [compact_evidence(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: compact_evidence(item) for key, item in value.items()}
    original = value.get('raw_annotation')
    if isinstance(original, dict) and all(value.get(key) == item for key, item in original.items()):
        result.pop('raw_annotation', None)
    raw_schema = value.get('raw_schema')
    ddl = value.get('ddl')
    if isinstance(raw_schema, str) and isinstance(ddl, str) and ddl in raw_schema:
        result.pop('ddl', None)
        result.pop('columns', None)
        for key in ('table_comments', 'foreign_keys'):
            if all(item in raw_schema for item in value.get(key, [])):
                result.pop(key, None)
    if isinstance(value.get('provenance'), dict):
        result['provenance'] = {key: item for key, item in result['provenance'].items()
                                if key not in ('sha256', 'encoding')}
    if value.get('candidate') is not None and value.get('candidate') == value.get('prediction'):
        result.pop('prediction', None)
    return result


def compact_messages(messages):
    result = []
    for message in messages:
        item = dict(message)
        if item.get('role') == 'user':
            try:
                content = json.loads(item['content'])
            except (ValueError, TypeError):
                pass
            else:
                item['content'] = json.dumps(compact_evidence(content), ensure_ascii=False,
                                            separators=(',', ':'))
        result.append(item)
    return result


def request_json(url, payload=None):
    # This experiment's local service needs no bearer token. Do not forward an
    # unrelated provider's MODEL_API_KEY from the environment.
    request = urllib.request.Request(url, headers={'Content-Type': 'application/json'},
        data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode())
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise OSError(f'HTTP {exc.code}: {detail}') from exc


class BudgetedClient:
    """Measure actual input and fit the output allowance to the server limit."""
    def __init__(self, base_url, directory, question_id, context_limit, max_output_tokens,
                 compact_context=False):
        self.base_url = base_url.rstrip('/')
        self.tokenize_url = self.base_url.removesuffix('/v1') + '/tokenize'
        self.directory = directory
        self.question_id = question_id
        self.context_limit = context_limit
        self.max_output_tokens = max_output_tokens
        self.compact_context = compact_context
        self.calls = []

    def __call__(self, base_url, path, payload):
        if base_url.rstrip('/') != self.base_url or path != '/chat/completions':
            raise ValueError('Unexpected endpoint for experiment client')
        index = len(self.calls)
        actual = copy.deepcopy(payload)
        if self.compact_context:
            actual['messages'] = compact_messages(payload['messages'])
        tokenized = request_json(self.tokenize_url, {
            'model': actual['model'], 'messages': actual['messages'],
            'add_generation_prompt': True,
            'chat_template_kwargs': payload.get('chat_template_kwargs', {})})
        count = tokenized['count']
        limit = min(self.context_limit, tokenized['max_model_len'])
        remaining = limit - count - 32
        entry = {'index': index, 'input_tokens': count, 'context_limit': limit,
                 'requested_max_tokens': payload['max_tokens'],
                 'messages_unchanged': actual['messages'] == payload['messages'],
                 'duplicate_representation_compaction': self.compact_context}
        self.calls.append(entry)
        stem = self.directory / f'{self.question_id}.call.{index}'
        if remaining < 1:
            entry['error'] = f'context_budget_exhausted: {count} input tokens, {limit} context limit'
            save(Path(str(stem) + '.budget.json'), entry)
            raise OSError(entry['error'])
        actual['max_tokens'] = min(payload['max_tokens'], self.max_output_tokens, remaining)
        entry['actual_max_tokens'] = actual['max_tokens']
        request_path = Path(str(stem) + '.request.json')
        response_path = Path(str(stem) + '.response.json')
        save(request_path, actual)
        started = time.monotonic()
        try:
            response = request_json(self.base_url + path, actual)
            save(response_path, response)
            entry['usage'] = response.get('usage')
            entry['finish_reason'] = response['choices'][0].get('finish_reason')
            return response
        except Exception as exc:
            entry['error'] = str(exc)
            raise
        finally:
            entry['seconds'] = round(time.monotonic() - started, 3)
            save(Path(str(stem) + '.budget.json'), entry)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=20261010)
    parser.add_argument('--count', type=int, default=40)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--max-steps', type=int, default=24)
    parser.add_argument('--semantic-review', action='store_true', help='启用独立LLM语义复核；当前默认关闭')
    parser.add_argument('--max-output-tokens', type=int, default=4096)
    parser.add_argument('--compact-context', action='store_true',
                        help='Remove duplicate raw annotations, DDL copies and source hashes from wire messages')
    args = parser.parse_args()
    if args.workers < 1 or not 5 <= args.max_steps <= 24 or args.max_output_tokens < 1:
        parser.error('workers/output tokens must be positive; max-steps must be 5..24')
    models = request_json(args.base_url.rstrip('/') + '/models')
    model = next((item for item in models['data'] if item['id'] == args.model), None)
    if model is None:
        parser.error('Requested model not found on the service')
    candidates = [row for row in load(RAW / 'QA_train.json') if source(row) == 'SQLite']
    if not 0 < args.count <= len(candidates):
        parser.error('Invalid sample count')
    rows = random.Random(args.seed).sample(candidates, args.count)
    context = Context(False, raw_evidence=True, evidence_mode='raw-only')
    args.output.mkdir(parents=True, exist_ok=True)
    directory = args.output / 'questions'
    wire = args.output / 'wire'
    directory.mkdir(exist_ok=True)
    wire.mkdir(exist_ok=True)
    config = {'model': args.model, 'base_url': args.base_url, 'provider': 'vllm',
        'strategy': 'react', 'workflow_version': WORKFLOW_VERSION, 'evidence_mode': 'raw-only',
        'semantic_review':args.semantic_review,
        'seed': args.seed, 'count': len(rows), 'population_count': len(candidates),
        'sample_ids': [row['question_id'] for row in rows], 'workers': args.workers,
        'max_steps': args.max_steps, 'temperature': 0, 'enable_thinking': False,
        'context_limit': model['max_model_len'], 'max_output_tokens': args.max_output_tokens,
        'context_budgeting': 'Tokenize wire messages; reduce max_tokens to remaining budget',
        'compact_context': args.compact_context,
        'compaction_policy': 'Remove duplicate raw annotation/DDL/columns and repeated source hashes; preserve original evidence text',
        'gold_excluded_from_retrieval': False, 'held_out_example_count': 0,
        'sampling': 'random.Random(seed).sample(raw SQLite QA_train rows, count)',
        'gold': 'Unmodified original QA_train annotations', 'system': react_system(True, args.semantic_review),
        'evidence_manifest': context.evidence_manifest,
        'input_sha256': hashlib.sha256((RAW / 'QA_train.json').read_bytes()).hexdigest(),
        'implementation_hash': implementation_hash(),
        'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'transport_retries': 0, 'failed_question_retries': 0}
    config_path = args.output / 'config.json'
    if config_path.exists() and load(config_path) != config:
        parser.error('Existing output configuration differs; use a new directory')
    save(config_path, config)
    save(args.output / 'models.json', models)
    save(args.output / 'sample.json', rows)
    records = {}
    started = time.monotonic()

    def process(row):
        client = BudgetedClient(args.base_url, wire, row['question_id'],
                                model['max_model_len'], args.max_output_tokens, args.compact_context)
        record = run_question({'question_id': row['question_id'], 'question': row['question']},
            context, directory, args.model, args.base_url, args.max_steps, provider='vllm', call=client,
            semantic_review=args.semantic_review)
        if client.calls:
            save(wire / f"{row['question_id']}.calls.json", client.calls)
        return record

    def export():
        comparisons = []
        ordered = [records[row['question_id']] for row in rows if row['question_id'] in records]
        for row, record in ((row, records[row['question_id']]) for row in rows
                            if row['question_id'] in records):
            prediction = record.get('prediction')
            successful = bool(prediction) and not record.get('errors')
            exact = successful and prediction == {'db': row['db'], 'query': row['query']}
            layout_equal = (successful and prediction.get('db') == row['db'] and
                isinstance(prediction.get('query'), str) and
                normalize_layout(prediction['query']) == normalize_layout(row['query']))
            comparisons.append({'question_id': row['question_id'], 'question': row['question'],
                'gold': {'db': row['db'], 'query': row['query']}, 'prediction': prediction,
                'successful': bool(successful), 'exact_equal': bool(exact),
                'layout_normalized_equal': bool(layout_equal), 'errors': record.get('errors', []),
                'transport_error': record.get('transport_error')})
        successful = sum(item['successful'] for item in comparisons)
        exact = sum(item['exact_equal'] for item in comparisons)
        layout = sum(item['layout_normalized_equal'] for item in comparisons)
        complete = len(ordered) == len(rows)
        summary = {**summarize(ordered), 'total_count': len(rows), 'completed_count': len(ordered),
            'seed': args.seed, 'population_count': len(candidates),
            'elapsed_seconds': round(time.monotonic() - started, 1),
            'exact_equal_count': exact, 'exact_different_count': successful - exact,
            'exact_different_ratio_among_successes': (successful - exact) / successful if successful else None,
            'layout_normalized_equal_count': layout, 'layout_normalized_different_count': successful - layout,
            'layout_normalized_different_ratio_among_successes': (successful - layout) / successful if successful else None,
            'generation_failure_count': len(ordered) - successful,
            'differences_plus_failures_ratio': (len(rows) - exact) / len(rows) if complete else None,
            'gold_excluded_from_retrieval': False, 'execution_accuracy': None}
        save(args.output / 'metrics.json', summary)
        save(args.output / 'comparison.json', {'summary': summary, 'questions': comparisons})
        save(args.output / 'predictions.json', [{'question_id': row['question_id'], **row['prediction']}
             for row in ordered if row.get('prediction') and not row.get('errors')])
        return summary

    print(json.dumps({'event': 'started', 'model': args.model, 'count': len(rows),
                      'seed': args.seed, 'context_limit': model['max_model_len']}), flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process, row): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                record = future.result()
            except Exception as exc:
                record = {'question_id': row['question_id'], 'question': row['question'],
                          'prediction': None, 'errors': [str(exc)], 'trace': []}
                save(directory / f"{row['question_id']}.json", record)
            records[row['question_id']] = record
            summary = export()
            print(json.dumps({'event': 'question_completed', 'question_id': row['question_id'],
                'completed': len(records), 'successes': summary['valid_count'],
                'errors': record.get('errors', [])}, ensure_ascii=False), flush=True)
    print(json.dumps({'event': 'finished', **export()}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
