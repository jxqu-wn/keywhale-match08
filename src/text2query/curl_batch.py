"""Build a resumable curl config; performs no network access itself."""
import argparse
import json
from pathlib import Path


def quoted(value):
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"')+'"'


def build(directory, url, auth_header_file=None):
    pending = []
    latest = {}
    for request in directory.glob('*.request.*.json'):
        key, suffix = request.name.split('.request.',1)
        attempt = int(suffix.split('.',1)[0])
        if key not in latest or attempt>latest[key][0]:
            latest[key] = (attempt,request)
    # A consumed failed attempt must not race with its newer repair request.
    for _, request in sorted(latest.values(),key=lambda item:item[1].name):
        response = request.with_name(request.name.replace('.request.', '.response.'))
        try:
            existing = json.loads(response.read_text())
            if isinstance(existing.get('choices'),list) and existing['choices']:
                continue
        except (OSError, ValueError, AttributeError):
            pass
        pending.append((request,response))
    lines = []
    for index,(request,response) in enumerate(pending):
        if index:
            lines.append('next')
        lines += ['url = '+quoted(url.rstrip('/')+'/chat/completions'),
                  'header = "Content-Type: application/json"',
                  'data-binary = '+quoted('@'+str(request.resolve())),
                  'output = '+quoted(response.resolve()),
                  'connect-timeout = 5', 'max-time = 180', 'fail-with-body',
                  'retry = 2', 'retry-delay = 2',
                  'write-out = '+quoted(request.name+' status=%{http_code} seconds=%{time_total}\\n')]
        if auth_header_file:
            # Store only the private file reference, never the credential itself.
            lines.append('header = '+quoted('@'+str(Path(auth_header_file).resolve())))
    config = directory/'batch.curl.conf'
    config.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'pending_requests':len(pending),'config':str(config)},ensure_ascii=False))
    return config


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    parser.add_argument('--base-url',default='http://192.168.15.119:28081/v1')
    parser.add_argument('--auth-header-file',type=Path,help='Private file containing the Authorization header')
    args = parser.parse_args()
    build(args.directory,args.base_url,args.auth_header_file)
