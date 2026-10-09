"""Run a fixed test-set smoke sample through the production pipeline using curl."""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from text2query.paths import ROOT, SERVICE
from text2query.curl_batch import build

DEFAULT_IDS = [442, 1737, 1825, 2140, 2399, 2570, 2646, 2938, 3033,
               3576, 3674, 3915, 4123, 4207, 4424, 4682, 4976, 5029, 5686, 5921]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://192.168.15.119:28081/v1')
    parser.add_argument('--model', default='contest-qwen')
    parser.add_argument('--ids', default=','.join(map(str, DEFAULT_IDS)))
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--offline', action='store_true',
                        help='Prepare/consume files only; issue curl separately when child processes cannot access the service')
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'artifacts/runs/regression/test20-20261009')
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('--workers must be positive')
    ids = [int(value) for value in args.ids.split(',')]
    if len(ids) != len(set(ids)):
        parser.error('Question IDs must be unique')
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    original_submission = ROOT / 'outputs/submission/result.json'
    submission_hash = hashlib.sha256(original_submission.read_bytes()).hexdigest()

    cache = SERVICE / 'models.json'
    if args.offline:
        model_text = cache.read_text(encoding='utf-8')
    else:
        probe = subprocess.run(
            ['curl', '--noproxy', '*', '--connect-timeout', '3', '--max-time', '10',
             '--silent', '--show-error', '--fail', args.base_url.rstrip('/') + '/models'],
            capture_output=True, text=True, timeout=15)
        if probe.returncode:
            raise SystemExit(probe.stderr)
        model_text = probe.stdout
    models = json.loads(model_text)
    if args.model not in {item['id'] for item in models['data']}:
        raise SystemExit(f'Model unavailable: {args.model}')
    # Keep the earlier service discovery record before updating the offline cache.
    previous = directory / 'previous_service_models.json'
    if not args.offline and cache.exists() and not previous.exists():
        previous.write_bytes(cache.read_bytes())
    (directory / 'service_models.json').write_text(model_text, encoding='utf-8')
    if not args.offline:
        cache.write_text(model_text, encoding='utf-8')
    manifest = {'question_ids': ids, 'selection': 'fixed diagnostic test questions',
                'base_url': args.base_url, 'model': args.model,
                'original_submission_sha256': submission_hash,
                'official_execution_accuracy': None}
    (directory / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    run = None
    transport_rounds = []
    for round_id in range(5):
        command = [sys.executable, str(ROOT / 'scripts/run.py'), 'model', 'predict',
                   '--ids', args.ids, '--offline', '--workers', str(args.workers),
                   '--base-url', args.base_url, '--model', args.model,
                   '--output', str(directory)]
        prepared = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        (directory / f'prepare_round_{round_id}.log').write_text(
            prepared.stdout + prepared.stderr, encoding='utf-8')
        if prepared.returncode:
            raise SystemExit(prepared.stderr or prepared.stdout)
        line = next(line for line in reversed(prepared.stdout.splitlines())
                    if line.startswith(str(directory)))
        run = Path(line.rsplit(' {', 1)[0])
        metrics = json.loads((run / 'metrics.json').read_text())
        print(json.dumps({'round': round_id, 'run': str(run), 'metrics': metrics},
                         ensure_ascii=False), flush=True)
        if not metrics['pending_count'] or round_id == 4:
            break
        config = build(run, args.base_url)
        if args.offline:
            break
        print('Calling pending model requests with curl', flush=True)
        started = time.monotonic()
        with (directory / f'curl_round_{round_id}.log').open('w', encoding='utf-8') as log:
            result = subprocess.run(
                ['curl', '--noproxy', '*', '--parallel', '--parallel-max', str(args.workers),
                 '--silent', '--show-error', '--config', str(config)], cwd=ROOT,
                stdout=log, stderr=subprocess.STDOUT)
        transport_rounds.append({'round': round_id, 'returncode': result.returncode,
                                 'seconds': round(time.monotonic() - started, 3)})
        print(json.dumps(transport_rounds[-1]), flush=True)
        # Always consume completed responses on the next preparation pass, even
        # when one transfer failed; already successful answers remain resumable.
        if result.returncode:
            print('Some transfers failed; retaining responses and preparing remaining requests.',
                  flush=True)
    sample = json.loads((run / 'sample.json').read_text())
    assert {row['question_id'] for row in sample} == set(ids)
    metrics = json.loads((run / 'metrics.json').read_text())
    unchanged = hashlib.sha256(original_submission.read_bytes()).hexdigest() == submission_hash
    assert unchanged
    summary = {'status': 'complete' if not metrics['pending_count'] else 'pending',
               'run_directory': str(run.relative_to(ROOT)), 'metrics': metrics,
               'transport_rounds': transport_rounds,
               'wall_seconds': round(time.monotonic() - start, 3),
               'original_submission_unchanged': unchanged,
               'official_execution_accuracy': None}
    (directory / 'run_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
