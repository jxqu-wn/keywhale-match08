"""Dispatch project commands without requiring installation or third-party packages."""
import argparse
import runpy
import sys

COMMANDS = {
    'prepare': ('shared.data', ['prepare']),
    'baseline': ('non_react.baseline', []),
    'model': ('non_react.pipeline', []),
    'react': ('react.pipeline', []),
    'curl-batch': ('shared.curl_batch', []),
    'recover-es': ('shared.recover_es', []),
    'score': ('shared.score_sample', []),
    'export': ('shared.export_submission', []),
    'verify-fixes': ('shared.verify_fixes', []),
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description='数据准备、查询生成、评测与提交导出')
    parser.add_argument('command', choices=COMMANDS)
    selected = parser.parse_args(argv[:1])
    module, prefix = COMMANDS[selected.command]
    previous = sys.argv
    try:
        sys.argv = [f'text2query.{module}', *prefix, *argv[1:]]
        runpy.run_module('text2query.'+module,run_name='__main__')
    finally:
        sys.argv = previous
