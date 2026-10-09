"""Reviewed retrieval context shared by the existing two generation strategies."""
import json
import re
from .data import load, Retriever, references, queries, source, normalize, ambiguous_examples
from .paths import OUT, CURATED
from .query_rules import requirements, mask_sql, applicable_business_rules
from .education import merge_context, expand_prediction, education_query_mode, model_query_errors
from .curation import load_overlays, apply_examples, apply_knowledge, hours_cte
from .retrieval_context import infer_scope, compatible_example, select_knowledge, dictionary_matches, dict_tables_for, triggered_dictionary
from .generation import validate

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
    def __init__(self, evaluate):
        self.catalog = load(OUT/'catalog.json')
        self.curation = load_overlays()
        # Do not teach malformed DSL through retrieved examples. Original labels remain unchanged.
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
        self.ar = Retriever([x['question'] for x in self.ambiguous])
        self.kr = Retriever([x['knowledge_text'] for x in self.knowledge])
        self.sr = Retriever([json.dumps(x,ensure_ascii=False) for x in self.catalog])
        self.cr = Retriever([x['question'] for x in self.corrections])

    def build(self, question):
        candidates = [self.examples[i] for s,i in self.er.search(question,30) if s>0]
        scope = infer_scope(question,candidates[:5],self.catalog)
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
        # 'common' is a shared dictionary database: include only tables that are actually referenced
        # (or reached by foreign keys / hypernym triggers) instead of the whole 32-table database.
        databases = {r.split('.')[0] for r in refs} - {'common'}
        pinned = set()
        selected = [x for x in self.catalog if x.get('database','') in databases or
                    (x['source'],x.get('database'),x['table']) in schema_keys or
                    (x['source'] in {source(e) for e in examples} and x['source'] in ('POSTGRESQL','Elasticsearch'))]
        for ref in refs:
            if ref.split('.')[0] != 'common':
                continue
            target = next((x for x in self.catalog if f"{x.get('database')}.{x['table']}".lower()==ref), None)
            if target is not None and target not in selected:
                selected.append(target)
                pinned.add((target['source'],target.get('database'),target['table']))
        # Add independently retrieved Schema candidates so examples don't exclusively constrain routing.
        for s,i in self.sr.search(question,8):
            if s>0 and self.catalog[i] not in selected:
                selected.append(self.catalog[i])
        # Complete explicitly qualified foreign-key targets.
        for table in list(selected):
            for db,name in re.findall(r'REFERENCES\s+(\w+)\.(\w+)', table.get('ddl',''),re.I):
                target = next((x for x in self.catalog if x.get('database')==db and x['table']==name),None)
                if target is not None and target not in selected:
                    selected.append(target)
                    pinned.add((target['source'],target.get('database'),target['table']))
        # Hypernym triggers pin the dictionary tables a keyword like 车辆/船舶 should expose.
        for target in dict_tables_for(question,self.catalog):
            if target not in selected:
                selected.append(target)
            pinned.add((target['source'],target.get('database'),target['table']))
        knowledge = select_knowledge(question,self.knowledge,self.kr,scope,self.catalog)
        dictionary = dictionary_matches(question,self.dictionaries,scope)
        for item in triggered_dictionary(question,self.dictionaries,scope,self.catalog):
            if not any(doc['id']==item['id'] for doc in dictionary):
                dictionary.append(item)
        if scope['confidence'] != 'weak':
            selected = [table for table in selected if (table['source'],table.get('database'),table['table']) in pinned or
                        (table['source'] in scope['sources'] and
                         (not table.get('database') or table['database'] in scope['databases']))]
        prompt_candidates = [{**group,'candidate_annotations':[prompt_example(row,self.catalog) for row in group['candidate_annotations']]} for group in candidate_groups]
        adjudications = [rule for rule in self.schema_adjudications
                        if refs.intersection(table.lower() for table in rule['tables']) or re.search(rule['trigger_pattern'],question)]
        return {'question':question,'requirements':requirements(question),'business_rules':business_rules,
                'routing':scope,
                'education_merge':merge_context(question,self.catalog,business_rules),
                'education_scope':education_query_mode(question) if any(ref.startswith('education_') for ref in refs) else {},
                'schema':selected,'knowledge':knowledge,'dictionary':dictionary,
                'examples':[prompt_example(row,self.catalog) for row in examples],
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


