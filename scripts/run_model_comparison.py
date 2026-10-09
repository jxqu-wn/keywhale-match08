"""Prepare or resume the same 200 validation questions with automated curl rounds.

If the endpoint cannot be reached, all requests/configs remain ready on disk.
Old responses are never used as new-model outputs. Run again after connectivity
returns to consume real responses and automatically request validation repairs.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text2query.paths import ROOT
from text2query.curl_batch import build

parser=argparse.ArgumentParser()
parser.add_argument('--baseline',type=Path,default=ROOT/'artifacts/runs/evaluation/evaluate-a1a6239d66af')
parser.add_argument('--base-url',default='http://192.168.15.119:28081/v1')
parser.add_argument('--model',default='contest-qwen')
parser.add_argument('--offline',action='store_true')
parser.add_argument('--workers',type=int,default=2)
args=parser.parse_args()
baseline=args.baseline.resolve()
comparison=ROOT/'artifacts/runs/regression/rag-improvements-20261009'
comparison.mkdir(parents=True,exist_ok=True)
online=False
probe=None
if not args.offline:
    completed=subprocess.run(['curl','--noproxy','*','--connect-timeout','3','--max-time','8','--silent','--show-error','--fail',args.base_url.rstrip('/')+'/models'],capture_output=True,text=True,timeout=12)
    probe={'returncode':completed.returncode,'stderr':completed.stderr.strip()}
    if completed.returncode==0:
        try:
            models=json.loads(completed.stdout)
            if isinstance(models.get('data'),list):
                (ROOT/'artifacts/service/models.json').write_text(json.dumps(models,ensure_ascii=False,indent=2)+'\n')
                online=True
        except ValueError:
            probe['error']='模型列表不是合法JSON'
run=None
for round_id in range(5):
    cmd=[sys.executable,str(ROOT/'scripts/run.py'),'model','evaluate','--sample-file',str(baseline/'sample.json'),'--offline','--workers',str(args.workers),'--base-url',args.base_url,'--model',args.model]
    prepared=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True)
    (comparison/f'prepare_round_{round_id}.log').write_text(prepared.stdout+prepared.stderr)
    if prepared.returncode:
        raise SystemExit(prepared.stderr or prepared.stdout)
    line=next(line for line in reversed(prepared.stdout.splitlines()) if line.startswith(str(ROOT)))
    run=Path(line.rsplit(' {',1)[0])
    metrics=json.loads((run/'metrics.json').read_text())
    if metrics['pending_count']==0:
        break
    config=build(run,args.base_url)
    if not online or round_id==4:
        break
    transport=subprocess.run(['curl','--noproxy','*','--parallel','--parallel-max',str(args.workers),'--silent','--show-error','--config',str(config)],cwd=ROOT,capture_output=True,text=True)
    (comparison/f'curl_round_{round_id}.log').write_text(transport.stdout+transport.stderr)
    if transport.returncode:
        probe['batch_returncode']=transport.returncode
        break
old=json.loads((baseline/'metrics.json').read_text())
old_sample=json.loads((baseline/'sample.json').read_text())
new_sample=json.loads((run/'sample.json').read_text())
assert old_sample==new_sample
new=json.loads((run/'metrics.json').read_text())
status='complete' if not new['pending_count'] else ('pending_offline' if args.offline else 'pending_endpoint_unreachable')
result={'status':status,'baseline_directory':str(baseline.relative_to(ROOT)),
        'new_directory':str(run.relative_to(ROOT)),'same_200_questions_and_targets':True,
        'baseline_metrics':old,'new_metrics':new,'endpoint_probe':probe,
        'official_execution_accuracy':None,'notes':'字符串匹配/本地校验不等于官方执行准确率；pending的题目没有新模型结果。'}
(comparison/'model_comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
