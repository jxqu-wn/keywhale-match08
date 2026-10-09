"""Dispatch project commands without requiring installation or third-party packages."""
import argparse
import runpy
import sys

COMMANDS = {
    'prepare': ('pipeline', ['prepare']),
    'baseline': ('pipeline', []),
    'model': ('model_pipeline', []),
    'react': ('react_pipeline', []),
    'curl-batch': ('curl_batch', []),
    'recover-es': ('recover_es', []),
    'score': ('score_sample', []),
    'export': ('export_submission', []),
    'verify-fixes': ('verify_fixes', []),
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
