"""Generate all raw-training SQLite queries with resumable DeepSeek ReAct."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from text2query.shared.context import Context
from text2query.shared.generation import api
from text2query.shared.paths import RAW
from text2query.shared.data import load
from text2query.react.pipeline import REACT_SYSTEM, WORKFLOW_VERSION, run_question, summarize


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(f'artifacts/runs/sqlite-train-react-v{WORKFLOW_VERSION}-deepseek-flash'))
    parser.add_argument('--workers', type=int, default=24)
    parser.add_argument('--max-steps', type=int, default=16)
    parser.add_argument('--sample-file', type=Path,
                        help='JSON list of raw-training question IDs or question_id/question objects')
    args = parser.parse_args()
    if args.workers < 1 or not 5 <= args.max_steps <= 24:
        parser.error('workers must be positive; max-steps must be 5..24')
    args.output.mkdir(parents=True, exist_ok=True)
    detail = args.output / 'questions'
    detail.mkdir(exist_ok=True)
    rows = [r for r in load(RAW / 'QA_train.json') if 'SQLite' in (r.get('db'), r.get('db1'), r.get('db2'))]
    if args.sample_file:
        requested = load(args.sample_file)
        ids = [r['question_id'] if isinstance(r, dict) else r for r in requested]
        by_id = {r['question_id']: r for r in rows}
        if len(ids) != len(set(ids)) or any(qid not in by_id for qid in ids):
            parser.error('sample-file IDs must be unique SQLite raw-training IDs')
        if any(isinstance(r, dict) and 'question' in r and r['question'] != by_id[r['question_id']]['question']
               for r in requested):
            parser.error('sample-file question text does not match raw training input')
        rows = [by_id[qid] for qid in ids]
    context = Context(False, raw_evidence=True)
    config = {'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com',
              'provider': 'deepseek', 'strategy': 'react', 'thinking': {'type': 'disabled'},
              'input': str(RAW / 'QA_train.json'), 'count': len(rows),
              'sample_file':str(args.sample_file) if args.sample_file else None,
              'context': 'Context(False, raw_evidence=True)', 'workflow_version':WORKFLOW_VERSION,
              'evidence_manifest': context.evidence_manifest,
              'workers': args.workers, 'max_steps': args.max_steps,
              'retry_step_budget_exhausted': False, 'system': REACT_SYSTEM,
              'retry_failed_questions': False, 'api_transport_retries': 0,
              'input_sha256': hashlib.sha256((RAW / 'QA_train.json').read_bytes()).hexdigest()}
    save(args.output / 'config.json', config)
    save(args.output / 'questions.json', [{'question_id': r['question_id'], 'question': r['question']} for r in rows])
    records = {}
    started = time.monotonic()

    def process(row):
        existing = detail / f"{row['question_id']}.json"
        if existing.exists():
            previous = load(existing)
            if previous.get('errors'):
                return previous
        return run_question({'question_id': row['question_id'], 'question': row['question']},
            context, detail, config['model'], config['base_url'], max_steps=args.max_steps,
            provider='deepseek', call=api)

    def export():
        ordered = [records[r['question_id']] for r in rows if r['question_id'] in records]
        save(args.output / 'predictions.json', [{'question_id': r['question_id'], **r['prediction']}
             for r in ordered if r.get('prediction') and not r.get('errors')])
        save(args.output / 'answers.json', [{'question_id': r['question_id'], 'question': r['question'],
             'prediction': r.get('prediction'), 'errors': r.get('errors', [])} for r in ordered])
        metrics = summarize(ordered)
        metrics.update(total_count=len(rows), completed_count=len(ordered), elapsed_seconds=round(time.monotonic()-started, 1))
        save(args.output / 'metrics.json', metrics)
        save(args.output / 'failures.json', [{'question_id': r['question_id'], 'question': r['question'],
             'errors': r['errors'], 'transport_error': r.get('transport_error'),
             'last_observation': r.get('last_observation')} for r in ordered if r.get('errors')])
        save(args.output / 'errors.json', [{'question_id': r['question_id'], 'question': r['question'],
             'terminal_errors': r.get('errors', []), 'transport_error': r.get('transport_error'),
             'tool_errors': [t for t in r.get('trace', []) if not t['observation'].get('ok')]}
             for r in ordered if r.get('errors') or any(not t['observation'].get('ok') for t in r.get('trace', []))])
        return metrics

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process, row): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {'question_id': row['question_id'], 'question': row['question'],
                          'prediction': None, 'errors': [type(exc).__name__ + ': ' + str(exc)], 'trace': []}
                save(detail / f"{row['question_id']}.json", result)
            records[row['question_id']] = result
            if len(records) % 25 == 0 or len(records) == len(rows):
                print(json.dumps(export(), ensure_ascii=False), flush=True)
    print(json.dumps(export(), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
