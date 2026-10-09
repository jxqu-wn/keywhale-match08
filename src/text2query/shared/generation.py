"""Provider requests, response parsing, common query validation and sampling."""
import json
import math
import os
import random
import re
import time
import urllib.request
from .data import load, references, source
from .query_rules import semantic_errors, mask_sql
from .validation import sql_structure_errors, es_structure_errors

def api(base, path, payload=None):
    headers = {'Content-Type':'application/json'}
    if os.getenv('MODEL_API_KEY'):
        headers['Authorization'] = 'Bearer '+os.environ['MODEL_API_KEY']
    request = urllib.request.Request(base.rstrip('/')+path,
        data=None if payload is None else json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def completion_payload(model, messages, provider='vllm'):
    payload = {'model':model,'messages':messages,'temperature':0,
               'max_tokens':4096,'response_format':{'type':'json_object'}}
    if provider == 'deepseek':
        payload['thinking'] = {'type':'disabled'}
    elif provider == 'vllm':
        payload['chat_template_kwargs'] = {'enable_thinking':False}
    else:
        raise ValueError(f'Unknown model provider: {provider}')
    return payload


def parse_output(content):
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.S).strip()
    content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content).strip()
    obj = json.loads(content)
    if not isinstance(obj, dict):
        raise ValueError('Output must be JSON object')
    # Serialize DSL objects mechanically; never infer or edit query semantics here.
    for db_key, query_key in [('db','query'),('db1','query1'),('db2','query2')]:
        if obj.get(db_key) == 'Elasticsearch' and isinstance(obj.get(query_key),dict):
            obj[query_key] = json.dumps(obj[query_key],ensure_ascii=False,separators=(',',':'))
    return obj


def write_offline_request(request_path, response_path, payload):
    # Repair messages depend on previous responses. Never consume a response
    # generated for a different request, even within the same run/attempt.
    if request_path.exists() and response_path.exists() and load(request_path) != payload:
        archived = request_path.parent/'stale_responses'/str(time.time_ns())
        archived.mkdir(parents=True,exist_ok=True)
        (archived/request_path.name).write_bytes(request_path.read_bytes())
        response_path.rename(archived/response_path.name)
    request_path.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')


def validate(obj, catalog, question=None, business_rules=(), structural=True):
    errors = []
    allowed = {'MYSQL','POSTGRESQL','Elasticsearch','SQLite'}
    if set(obj) == {'db','query'}:
        pairs = [(obj['db'], obj['query'])]
    elif set(obj) == {'db1','query1','db2','query2'}:
        pairs = [(obj['db1'],obj['query1']),(obj['db2'],obj['query2'])]
        if obj['db1'] == obj['db2']:
            errors.append('同一数据源必须使用单源格式和一条查询，不能拆为跨源两阶段')
        if isinstance(obj['query1'],str) and re.search(r'\bresult\b',mask_sql(obj['query1']),re.I):
            errors.append('第一阶段不能引用尚不存在的result')
        if isinstance(obj['query2'],str) and not re.search(r'\bIN\s*\(\s*result\s*\)', mask_sql(obj['query2']),re.I):
            errors.append('跨源第二阶段缺少result依赖')
    else:
        return ['输出键必须符合单源或双阶段格式']
    for db, query in pairs:
        if db not in allowed or not isinstance(query,str) or not query.strip():
            errors.append('无效数据源或空指令'); continue
        if db == 'Elasticsearch':
            try:
                dsl = json.loads(query)
                if not isinstance(dsl,dict):
                    errors.append('ES DSL必须为对象')
                elif 'query' in dsl and (not isinstance(dsl['query'],dict) or len(dsl['query']) != 1):
                    errors.append('ES query必须只有一个查询类型；aggs/_source/sort应位于根级')
                elif structural:
                    errors.extend(es_structure_errors(dsl,catalog))
            except ValueError as exc:
                errors.append('ES DSL不是合法JSON: '+str(exc))
        else:
            # Lexical screen only; not a full parser or execution authorization boundary.
            sql = re.sub(r"'(?:''|[^'])*'", "''",query)
            sql = re.sub(r'--[^\n]*|/\*.*?\*/','',sql,flags=re.S)
            if not re.match(r'^\s*(SELECT|WITH)\b',sql,re.I):
                errors.append('只允许SELECT/WITH查询')
            if re.search(r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|ATTACH|PRAGMA|COPY)\b',sql,re.I):
                errors.append('出现非只读关键词')
            if ';' in sql.rstrip().rstrip(';'):
                errors.append('不允许多语句')
            known = {f"{t['database']}.{t['table']}".lower() for t in catalog if t['source']==db and t.get('database')}
            for ref in references(query):
                if ref.lower() not in known:
                    errors.append('未知表或错误数据源: '+ref)
            if db in ('MYSQL','SQLite') and not references(query):
                errors.append('缺少库名限定的物理表')
            if structural and not errors:
                errors.extend(sql_structure_errors(db,query,catalog))
    if question is not None:
        errors.extend(semantic_errors(question,obj,business_rules))
    return errors


def stratified_sample(rows, count, seed=42):
    if count <= 0 or count > len(rows):
        raise ValueError('Sample size must be between 1 and dataset size')
    buckets = {}
    for row in rows:
        buckets.setdefault(source(row), []).append(row)
    quotas = {key: math.floor(len(bucket)*count/len(rows)) for key,bucket in buckets.items()}
    remaining = count-sum(quotas.values())
    order = sorted(buckets, key=lambda key: (-(len(buckets[key])*count/len(rows)-quotas[key]),key))
    for key in order[:remaining]:
        quotas[key] += 1
    rng = random.Random(seed)
    result = []
    for key in sorted(buckets):
        result.extend(rng.sample(buckets[key], quotas[key]))
    return sorted(result,key=lambda row:row['question_id'])


