"""Consume saved ReAct responses and prepare the next curl batch, without networking."""
import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from text2query.react.pipeline import main as react_main
from text2query.shared.curl_batch import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--provider', choices=['vllm', 'deepseek'], default='vllm')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--max-steps', type=int, default=24)
    parser.add_argument('--auth-header-file', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    log_path = args.output / ('prepare-' + str(time.time_ns()) + '.log')
    original_argv = sys.argv
    sys.argv = ['react', 'predict', '--offline', '--output', str(args.output),
                '--base-url', args.base_url, '--model', args.model,
                '--provider', args.provider, '--workers', str(args.workers),
                '--max-steps', str(args.max_steps)]
    try:
        with log_path.open('w', encoding='utf-8') as log, contextlib.redirect_stdout(log):
            react_main()
    finally:
        sys.argv = original_argv
    final_line = log_path.read_text(encoding='utf-8').splitlines()[-1]
    run = Path(final_line.split(' {', 1)[0])
    metrics = json.loads((run / 'metrics.json').read_text(encoding='utf-8'))
    with log_path.open('a', encoding='utf-8') as log, contextlib.redirect_stdout(log):
        config = build(run, args.base_url, args.auth_header_file)
    status = {'run': str(run.resolve()), 'curl_config': str(config.resolve()),
              'metrics': metrics, 'prepare_log': str(log_path.resolve())}
    (args.output / 'batch_status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(status, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
