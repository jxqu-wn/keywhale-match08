"""Retrieval augmented remote generation with resumable per-question artifacts."""
import argparse
import hashlib
import json
import os
import re
import random
import math
import time
from collections import defaultdict
from pathlib import Path
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from .pipeline import load, Retriever, references, queries, source, normalize, ambiguous_examples
from .paths import ROOT, OUT, RAW, SERVICE, RUNS, CURATED
from .query_rules import requirements, semantic_errors, mask_sql, applicable_business_rules
from .education import merge_context, expand_prediction, education_query_mode, model_query_errors
from .curation import load_overlays, apply_examples, apply_knowledge, hours_cte
from .retrieval_context import infer_scope, evidence_scope, compatible_example, select_knowledge, dictionary_matches
from .validation import sql_structure_errors, es_structure_errors

SYSTEM = '''你是多源查询指令生成器。只输出一个JSON对象，不输出Markdown或推理文本。
SQL单源格式 {"db":"MYSQL|POSTGRESQL|SQLite","query":"SQL字符串"}。
ES单源格式 {"db":"Elasticsearch","query":{合法DSL对象}}，query必须是对象，不要编码为字符串。
跨源格式 {"db1":"源类型","query1":"指令","db2":"源类型","query2":"指令"}。
跨源第二阶段使用 IN (result) 引用第一阶段结果。MySQL/SQLite必须使用库名.表名。
根据当前问题修改示例参数及逻辑，示例不等于答案。遵守提供Schema、知识和字典。
仅生成只读查询。不得臆造表字段。当前问题没有要求时不要添加LIMIT。
按每船最新位置与历史记录区分粒度。空间半径转换为米，经度在前纬度在后。
年份库与日期条件准确匹配。business_rules是明确业务口径，优先于检索示例；当前问题明确查询年份时按问题年份查询。学号前缀、入学年份、代码包含的数字是筛选参数，不等于查询年度库。
提供education_merge时，使用其中edu_*逻辑视图写SQL，允许不加库名；程序自动注入合并CTE，最终物理表都有库名。模型不要重新定义edu_*或_edu_*视图。查询条件、聚合、排序和LIMIT写在逻辑视图之外；不再照搬只查一年物理表的旧示例。
跨年关联和分组使用视图提供的业务键：学生用student_no，课程用course_code，选课与成绩用student_no/course_code/academic_year/semester_code四字段；不能只连接自增ID。题目要求输出编号时输出视图保留的ID。
ambiguous_examples是存在歧义的原始候选标注，只用于了解可能涉及的库表和逻辑，不是正确答案；不要任意选其中一个作为答案。
corrected_examples是经当前问题、Schema和知识核对的修正示例，优先于原始候选；仍须按当前问题修改参数。question_correction非空表示完整题目精确匹配用户授权的修正，可直接采用其db和query。
schema_adjudications记录数据源编码及实际字段定义的冲突处理，优先于过时字段注释；按当前数据源解释编码，不把跨源枚举直接等同。
每个示例的schema_refs标明其引用的库表；完整字段、类型、主键和外键定义见顶层schema。
ES的query请返回原生JSON对象，由程序编码成官方要求的字符串；aggs、sort、_source、size属于DSL根级，不能放入query内部。
只涉及同一个数据源的多问句必须合为一条SQL，不能伪装成跨源两阶段。
先核对结果粒度、返回列、分组和每层排名口径，再生成查询。总销量按主体SUM数量，最少销量按商品SUM数量，不能取单笔最小订单。
跨源补充船舶属性时保留mmsi与请求的属性；查询AIS记录时保留记录粒度，除非明确要求只返回编号或去重。
时段内最大观测航速使用MAX(sog)，基础表max_speed是额定属性，不能混用。'''

SYSTEM += '''
当前题目显式参数及用户business_rules优先；Schema确定真实字段/类型/关联，当前数据源知识确定枚举、单位与计算口径。经核对的示例优先于旧示例，但必须修改为当前题目参数。竞赛明确公式即使不同于一般常识也不得擅自替换。
routing是问题领域与数据源候选；知识scope和字典source/database限定其适用范围，不能借用另一个数据库的字段含义或枚举。不得因缺少知识猜造题干、类别值或字段。
先确定统计对象及主键，再决定关联和聚合。统计商家/球员/游戏/角色人数或数量时不能把评论/赛季/发行/台词关系条数当实体数；用EXISTS表达存在性，或先按对象主键去重后聚合。不能用AVG(DISTINCT 属性值)代替对象去重，同值的不同对象都应参与平均。
百分比的分母按当前题目总体定义，从总体实体表统计，包含无关系记录的实体；分子与分母采用相同对象粒度。每部作品角色数先按作品去重角色，再按作品平均。完整年份区间平均包含零记录年份，不能只除以出现记录的年份数。
奖项年份与执教/球队赛季按题意匹配；明确要求职业生涯全部球队才扩展年份。代理键year_id关联Years取actual_year，不能把代理键当实际年份。
时间比较采用统一表示：Business_Hours的AM/PM文本使用query_helpers中的解析CTE，处理分钟、跨午夜、相等为24小时；不得文本直接相减。自然日使用>=当日 AND <次日半开区间；带时间的日期文本月份LIKE保留末尾%。日期值/日期键中的年份不选择教育年度库。
BMI和PB必须同时核对单位及依赖公式；全明星按球员去重。本赛题BMI按知识83，PB的BPS按知识1624。股价/资产等除数为0或NULL时不要编造结果。
MySQL不能使用FULL OUTER JOIN，保留单边实体可用键集合UNION后LEFT JOIN。SQLite保留字表名（如transaction）必须引用；UNION每分支若需ORDER BY/LIMIT要包成子查询；JOIN后共同列必须有表别名。
ES的bool每个must/filter/should/must_not子句是单个查询对象，多条件用数组；检查所有嵌套层级。过滤、排序、聚合字段必须出现在mapping中。日均门诊量若按固定天数由annual_outpatient换算，其排序与annual_outpatient相同，不存在daily_outpatient字段。derived_constants是程序从知识文本计算的坐标，优先使用，不能把矩形包络误当真实航道形状。
最后核对返回列、对象粒度、筛选参数、总体分母、排序和LIMIT，只输出规定JSON。'''

SYSTEM += '''
最新位置/最新记录的空间筛选：先在独立latest CTE中从全部记录按mmsi取timestamp最新行，再在外层筛选距离和状态；不得先按距离筛历史记录后DISTINCT ON。
心理健康调查的年龄和国家是同一受访者的不同答案行，按UserID及SurveyID关联，年龄QuestionID=1、国家QuestionID=3；不能比较年龄AnswerText与国家AnswerText。
斯坦利杯决赛球队指标来自hockey.TeamsSC，Teams仅用于名称等球队信息；按year和tmID关联，不能用Teams的同名PIM代替决赛PIM。
平均每部莎士比亚作品角色数：先按作品ID统计COUNT(DISTINCT character_id)，再AVG；不同作品的同一角色ID须分别计入，不用全局角色去重数除作品数。'''


def repair_feedback(errors):
    text = '修正以下错误，保留当前问题的筛选参数和返回要求，重新输出完整JSON：'+json.dumps(errors,ensure_ascii=False)
    text += '\nES的最终对象有两层query：外层query的值是DSL，DSL内部query只能包含bool等查询类型。aggs、sort、size与DSL内部query平级。正确层级示意：'+json.dumps({'db':'Elasticsearch','query':{'query':{'bool':{'filter':[]}},'aggs':{},'sort':[]}},ensure_ascii=False)
    text += '。示意中的空条件必须替换为当前问题真实条件；检查所有括号，不输出非法JSON。'
    return text


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


def example_schema_refs(row, catalog):
    result = []
    for db_key, query_key in [('db','query'),('db1','query1'),('db2','query2')]:
        if db_key not in row:
            continue
        db, query = row[db_key], row[query_key]
        qualified = {ref.lower() for ref in references(query)} if isinstance(query,str) else set()
        unqualified = set(re.findall(r'\b(?:FROM|JOIN)\s+["`]?([\w]+)["`]?(?![\w"`]|\s*\.)',mask_sql(query),re.I)) if isinstance(query,str) else set()
        for table in catalog:
            full_name = f"{table.get('database')}.{table['table']}".lower()
            if table['source']==db and (full_name in qualified or
                    (db=='POSTGRESQL' and table['table'] in unqualified) or db=='Elasticsearch'):
                result.append({'query_key':query_key,'source':db,'database':table.get('database'),'table':table['table']})
    return result


def prompt_example(row, catalog):
    item = dict(row)
    item['schema_refs'] = example_schema_refs(row,catalog)
    for db_key,query_key in [('db','query'),('db1','query1'),('db2','query2')]:
        if item.get(db_key)=='Elasticsearch' and isinstance(item[query_key],str):
            try:
                item[query_key] = json.loads(item[query_key])
            except ValueError:
                item['annotation_warning'] = '原始候选DSL格式无效，不可直接照搬'
    return item


class Context:
    def __init__(self, evaluate, raw_evidence=False):
        self.curation = load_overlays()
        self.raw_evidence = raw_evidence
        self.evidence_manifest = {'mode': 'processed'}
        self.reference_documents = []
        if raw_evidence:
            from .raw_evidence import load_raw_evidence
            for key, value in load_raw_evidence(evaluate, self.curation).items():
                setattr(self, key, value)
        else:
            self.catalog = load(OUT/'catalog.json')
            # Ordinary RAG keeps its existing filtered retrieval behavior.
            self.examples = []
            for row in apply_examples(load(OUT/('train.json' if evaluate else 'clean.json')), self.curation):
                if row.get('db') == 'Elasticsearch':
                    pred = {'db':row['db'],'query':row['query']}
                    if validate(pred,self.catalog):
                        continue
                self.examples.append(row)
            self.knowledge = apply_knowledge(load(OUT/'knowledge.json'), self.curation)
            self.dictionaries = load(OUT/'dictionary_documents.json')
            ambiguous_path = OUT/'ambiguous_examples.json'
            self.ambiguous = load(ambiguous_path) if ambiguous_path.exists() else ambiguous_examples(load(OUT/'audit.json')['conflicting_groups'])
        rules_path = CURATED/'business_rules.json'
        self.business_rules = load(rules_path) if rules_path.exists() else []
        resolution_path = CURATED/'education_conflict_resolutions.json'
        self.resolutions = load(resolution_path) if resolution_path.exists() else {}
        correction_path = CURATED/'question_corrections.json'
        self.corrections = load(correction_path) if correction_path.exists() else []
        adjudication_path = CURATED/'schema_adjudications.json'
        self.schema_adjudications = load(adjudication_path) if adjudication_path.exists() else []
        for group in self.ambiguous:
            resolution = self.resolutions.get('groups',{}).get(group['group_id'])
            if resolution:
                group['resolution'] = resolution
                if resolution.get('status')=='resolved_by_year_policy':
                    group['status'] = 'business_rule_resolved'
                    group['instruction'] = '年度口径已由用户确认；原始候选是旧的单年度写法，当前查询应使用education_merge中的合并视图。'
                elif resolution.get('status')=='resolved_by_query_correction':
                    group['status'] = 'query_correction_resolved'
                    group['instruction'] = '此组已按问题、Schema和知识修正；原始候选只保留证据，优先使用question_correction或corrected_examples。'
        self.er = Retriever([x['question'] for x in self.examples])
        self.examples_by_question = defaultdict(list)
        for row in self.examples:
            self.examples_by_question[normalize(row['question'])].append(row)
        self.ar = Retriever([x['question'] for x in self.ambiguous])
        self.kr = Retriever([x['knowledge_text'] for x in self.knowledge])
        self.sr = Retriever([json.dumps(x,ensure_ascii=False) for x in self.catalog])
        self.cr = Retriever([x['question'] for x in self.corrections])

    def exact_examples(self, question):
        rows = self.examples_by_question.get(normalize(question), [])
        return sorted(rows, key=lambda row: row['question'] != question)

    def build(self, question):
        exact = self.exact_examples(question)
        ranked = [(s, self.examples[i]) for s,i in self.er.search(question,30) if s>0]
        candidates = [row for _, row in ranked]
        if self.raw_evidence:
            candidates = exact + [row for row in candidates if row not in exact]
            ranked = [(ranked[0][0] if ranked else 1, row) for row in exact] + [
                (score, row) for score, row in ranked if row not in exact]
        scope = (evidence_scope(question, ranked, self.catalog) if self.raw_evidence
                 else infer_scope(question,candidates[:5],self.catalog))
        examples = [row for row in candidates if compatible_example(row,scope)][:5]
        refs = {r.lower() for x in examples for q in queries(x) for r in references(q)}
        exact_correction = next((x for x in self.corrections if normalize(x['question'])==normalize(question)),None)
        corrected = [self.corrections[i] for score,i in self.cr.search(question,3) if score>0
                     and (self.corrections[i] is exact_correction or refs.intersection(t.lower() for t in self.corrections[i]['tables']))]
        if exact_correction and exact_correction not in corrected:
            corrected.insert(0,exact_correction)
        refs.update(t.lower() for item in corrected for t in item['tables'])
        routing_rules = applicable_business_rules(question,self.business_rules,refs)
        refs.update(t.lower() for rule in routing_rules for t in rule['tables'])
        candidate_groups = []
        for score,index in self.ar.search(question,10):
            group = self.ambiguous[index]
            if score>0 and (normalize(group['question'])==normalize(question) or refs.intersection(t.lower() for t in group['tables'])):
                candidate_groups.append(group)
                if len(candidate_groups)==3:
                    break
        refs.update(t.lower() for group in candidate_groups for t in group['tables'])
        business_rules = applicable_business_rules(question,self.business_rules,refs)
        refs.update(t.lower() for rule in business_rules for t in rule['tables'])
        annotated_rows = examples + [row for group in candidate_groups for row in group['candidate_annotations']]
        schema_keys = {(ref['source'],ref['database'],ref['table']) for row in annotated_rows for ref in example_schema_refs(row,self.catalog)}
        databases = {r.split('.')[0] for r in refs}
        selected = [x for x in self.catalog if x.get('database','') in databases or
                    (x['source'],x.get('database'),x['table']) in schema_keys or
                    (x['source'] in {source(e) for e in examples} and x['source'] in ('POSTGRESQL','Elasticsearch'))]
        if self.raw_evidence:
            selected = [x for x in selected if f"{x.get('database')}.{x['table']}".lower() in refs
                        or (x['source'],x.get('database'),x['table']) in schema_keys]
        # Add independently retrieved Schema candidates so examples don't exclusively constrain routing.
        for s,i in self.sr.search(question,8):
            if s>0 and self.catalog[i] not in selected:
                selected.append(self.catalog[i])
        # Complete explicitly qualified foreign-key targets.
        for table in list(selected):
            for db,name in re.findall(r'REFERENCES\s+(\w+)\.(\w+)', table.get('ddl',''),re.I):
                target = next((x for x in self.catalog if x.get('database')==db and x['table']==name),None)
                if target and target not in selected:
                    selected.append(target)
        knowledge = select_knowledge(question,self.knowledge,self.kr,scope,self.catalog)
        dictionary = dictionary_matches(question,self.dictionaries,scope)
        if scope['confidence'] != 'weak':
            selected = [table for table in selected if table['source'] in scope['sources'] and
                        (not table.get('database') or table['database'] in scope['databases'])]
        prompt_candidates = [{**group,'candidate_annotations':[prompt_example(row,self.catalog) for row in group['candidate_annotations']]} for group in candidate_groups]
        adjudications = [rule for rule in self.schema_adjudications
                        if refs.intersection(table.lower() for table in rule['tables']) or re.search(rule['trigger_pattern'],question)]
        return {'question':question,'requirements':requirements(question),'business_rules':business_rules,
                'evidence_mode':self.evidence_manifest['mode'],
                'routing':scope,
                'education_merge':merge_context(question,self.catalog,business_rules),
                'education_scope':education_query_mode(question) if any(ref.startswith('education_') for ref in refs) else {},
                'schema':selected,'knowledge':knowledge,'dictionary':dictionary,
                'examples':[prompt_example(row,self.catalog) for row in examples],
                'exact_examples':[prompt_example(row,self.catalog) for row in exact],
                'corrected_examples':corrected,'question_correction':exact_correction,
                'schema_adjudications':adjudications,
                'query_helpers':[{'purpose':'营业时间文本解析、跨午夜及全天24小时', 'cte_sql':hours_cte()}]
                    if 'public_review_platform' in scope['databases'] and re.search(r'营业|小时',question) else [],
                'ambiguous_examples':prompt_candidates}


def corrected_prediction(context, catalog):
    correction = context.get('question_correction')
    if not correction:
        return None
    raw = {key:correction[key] for key in ('db','query')}
    prediction = expand_prediction(raw,context,catalog)
    errors = model_query_errors(raw,context)+validate(prediction,catalog,context['question'],context['business_rules'])
    if errors:
        raise ValueError('修正查询校验失败：'+json.dumps(errors,ensure_ascii=False))
    return prediction


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['evaluate','predict'])
    parser.add_argument('--base-url',default='http://192.168.15.119:28081/v1')
    parser.add_argument('--model',default='contest-qwen')
    parser.add_argument('--provider',choices=['vllm','deepseek'],default='vllm')
    parser.add_argument('--models-file',type=Path,help='Provider-specific model discovery JSON for offline mode')
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--sample',type=int,default=0)
    parser.add_argument('--sample-file',type=Path,help='Immutable evaluation sample JSON, for paired comparisons')
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--ids',help='Comma-separated question IDs for focused regression runs')
    parser.add_argument('--output',help='Run parent directory; default depends on evaluate/predict')
    parser.add_argument('--offline',action='store_true',help='Write requests and consume response files; use when Python sockets are restricted')
    args = parser.parse_args()
    models = load(args.models_file or SERVICE/'models.json')['data'] if args.offline else api(args.base_url,'/models')['data']
    if args.model not in {x['id'] for x in models}:
        raise ValueError(f'Model unavailable: {args.model}')
    context = Context(args.command=='evaluate')
    rows = load(OUT/'dev.json' if args.command=='evaluate' else RAW/'QA_test.json')
    if args.sample_file:
        if args.command != 'evaluate' or args.sample or args.ids or args.limit:
            parser.error('--sample-file只用于evaluate，不与sample/ids/limit混用')
        frozen = load(args.sample_file)
        current = {row['question_id']:row for row in rows}
        if len({row['question_id'] for row in frozen}) != len(frozen) or any(current.get(row['question_id']) != row for row in frozen):
            parser.error('固定样本必须与当前原始验证题/金标一致，且不能重复')
        rows = frozen
    if args.ids:
        if args.sample or args.limit:
            parser.error('--ids cannot be combined with --sample or --limit')
        wanted = {int(x) for x in args.ids.split(',')}
        rows = [r for r in rows if r['question_id'] in wanted]
        if {r['question_id'] for r in rows} != wanted:
            parser.error('Some requested IDs are absent from this dataset')
    if args.sample:
        if args.limit:
            parser.error('--sample and --limit are mutually exclusive')
        rows = stratified_sample(rows,args.sample,args.seed)
    # Fixed mixed-source smoke set, not the first N MySQL questions.
    if args.limit:
        if args.command=='evaluate':
            buckets = {}
            for row in rows:
                buckets.setdefault(source(row),[]).append(row)
            rows = [bucket[i] for i in range(max(map(len,buckets.values()))) for bucket in buckets.values() if i<len(bucket)][:args.limit]
        else:
            rows = rows[:args.limit]
    run_config = {'sample_ids':[r['question_id'] for r in rows], 'sampling_seed':args.seed, 'model':args.model,'base_url':args.base_url,'system':SYSTEM,
                  'provider':args.provider,
                  'sample_target_hash':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),
                  'data_hash':hashlib.sha256(json.dumps([context.examples,context.catalog,context.knowledge,context.dictionaries,context.ambiguous,context.business_rules,context.resolutions,context.corrections,context.schema_adjudications,context.curation],sort_keys=True).encode()).hexdigest(),
                  'implementation_hash':hashlib.sha256(b''.join(p.read_bytes() for p in sorted(Path(__file__).parent.glob('*.py')))).hexdigest()}
    fingerprint = hashlib.sha256(json.dumps(run_config,sort_keys=True).encode()).hexdigest()[:12]
    parent = ROOT / args.output if args.output else RUNS / ('evaluation' if args.command=='evaluate' else 'prediction')
    dest = parent / (args.command+'-'+fingerprint)
    dest.mkdir(parents=True,exist_ok=True)
    (dest/'sample.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    (dest/'config.json').write_text(json.dumps(run_config,ensure_ascii=False,indent=2))
    def process(row):
        path = dest/f"{row['question_id']}.json"
        if path.exists():
            cached = load(path)
            if cached.get('prediction') and not cached.get('errors') and cached['question']==row['question']:
                return cached
        ctx = context.build(row['question'])
        messages = [{'role':'system','content':SYSTEM},{'role':'user','content':json.dumps(ctx,ensure_ascii=False)}]
        record = {'question_id':row['question_id'],'question':row['question'],'context':ctx,'attempts':[],'prediction':None}
        corrected = corrected_prediction(ctx,context.catalog)
        if corrected:
            record.update({'prediction':corrected,'errors':[],'prediction_source':'curated_question_correction',
                           'correction_group_id':ctx['question_correction']['group_id'],'seconds':0})
            save(path.name,record,dest)
            return record
        start = time.monotonic()
        for attempt in range(4):
            content = None
            try:
                payload = completion_payload(args.model,messages,args.provider)
                if args.offline:
                    request_path = dest/f"{row['question_id']}.request.{attempt}.json"
                    response_path = dest/f"{row['question_id']}.response.{attempt}.json"
                    write_offline_request(request_path,response_path,payload)
                    if not response_path.exists():
                        record['errors'] = ['pending_request']
                        break
                    response = load(response_path)
                else:
                    response = api(args.base_url,'/chat/completions',payload)
                choice = response['choices'][0]
                content = choice['message'].get('content') or ''
                record['attempts'].append({'content':content,'finish_reason':choice.get('finish_reason'),'usage':response.get('usage')})
                raw_prediction = parse_output(content)
                prediction = expand_prediction(raw_prediction,ctx,context.catalog)
                record['model_prediction'] = raw_prediction
                record['automatic_transforms'] = ['education_view_cte_expansion'] if prediction!=raw_prediction else []
                errors = model_query_errors(raw_prediction,ctx)+validate(prediction,context.catalog,row['question'],ctx['business_rules'])
                if choice.get('finish_reason')=='length':
                    errors.append('输出被截断')
                record['prediction'], record['errors'] = prediction, errors
                if not errors:
                    break
                messages += [{'role':'assistant','content':content},{'role':'user','content':repair_feedback(errors)}]
            except (ValueError, OSError, KeyError) as exc:
                record['errors'] = [str(exc)]
                if content:
                    messages.append({'role':'assistant','content':content})
                messages.append({'role':'user','content':repair_feedback(record['errors'])})
        record['seconds'] = round(time.monotonic()-start,2)
        path.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
        print(f"{row['question_id']}: {'OK' if not record.get('errors') else record['errors']}",flush=True)
        return record
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        records = list(pool.map(process,rows))
    predictions = [{'question_id':r['question_id'],**r['prediction']} for r in records if r['prediction'] and not r.get('errors')]
    (dest/'predictions.json').write_text(json.dumps(predictions,ensure_ascii=False,indent=2)+'\n')
    pending = sum(record.get('errors')==['pending_request'] for record in records)
    metrics = {'count':len(rows),'pending_count':pending,'evaluated_count':len(rows)-pending,
               'valid_count':len(predictions),'format_and_lexical_check_rate':None if pending else len(predictions)/max(1,len(rows)),
               'validation_checks':['format/read-only/table references','SQLite raw-DDL EXPLAIN','MySQL FULL JOIN restriction','ES nested clauses/mapping','question-derived rules'],
               'execution_accuracy':None}
    if args.command=='evaluate':
        targets = {r['question_id']:r for r in rows}
        metrics['source_accuracy'] = None if pending else sum(source(p)==source(targets[p['question_id']]) for p in predictions)/len(rows)
        metrics['query_exact_match'] = None if pending else sum(source(p)==source(targets[p['question_id']]) and queries(p)==queries(targets[p['question_id']]) for p in predictions)/len(rows)
    (dest/'metrics.json').write_text(json.dumps(metrics,indent=2)+'\n')
    print(dest, json.dumps(metrics),flush=True)


if __name__=='__main__':
    from pathlib import Path
    main()
