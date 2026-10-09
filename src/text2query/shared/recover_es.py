"""Ask the model for native DSL objects to avoid nested JSON escaping errors."""
import argparse
import json
from pathlib import Path
from .data import OUT, load
from .generation import parse_output, validate


def run(directory):
    catalog = load(OUT/'catalog.json')
    config = load(directory/'config.json')
    prepared = recovered = 0
    for path in directory.glob('*.json'):
        if not path.stem.isdigit():
            continue
        record = load(path)
        if not record.get('errors') or record.get('prediction',{}).get('db')!='Elasticsearch':
            continue
        request = directory/f'{path.stem}.request.recovery.json'
        response = directory/f'{path.stem}.response.recovery.json'
        if not response.exists():
            old = record['prediction']['query']
            try:
                json.loads(old)
                detail = '检查根级query、aggs、sort、_source结构是否正确'
            except ValueError as exc:
                detail = str(exc)
            payload = {'model':config['model'],'temperature':0,'max_tokens':4096,
                       'chat_template_kwargs':{'enable_thinking':False},
                       'messages':[{'role':'system','content':'你是Elasticsearch查询生成器。只输出JSON对象，格式为{"db":"Elasticsearch","query":{合法DSL对象}}。query必须是嵌套JSON对象，不要把它编码为字符串。确保query和aggs/_source/sort为DSL的根级字段。根据当前问题、Schema和知识生成，示例只供参考。'},
                                   {'role':'user','content':json.dumps({'context':record['context'],'invalid_previous_query':old,'parse_error':detail},ensure_ascii=False)}]}
            request.write_text(json.dumps(payload,ensure_ascii=False)+'\n')
            prepared += 1
            continue
        raw = load(response)
        obj = parse_output(raw['choices'][0]['message']['content'])
        if isinstance(obj.get('query'),dict):
            obj['query'] = json.dumps(obj['query'],ensure_ascii=False,separators=(',',':'))
        errors = validate(obj,catalog)
        if raw['choices'][0].get('finish_reason')=='length':
            errors.append('truncated recovery output')
        record['attempts'].append({'stage':'native_es_object_recovery','response':raw,'request_file':str(request)})
        record['prediction'],record['errors']=obj,errors
        path.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
        recovered += not errors
    print(json.dumps({'prepared_recovery':prepared,'recovered':recovered}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    run(parser.parse_args().directory)
