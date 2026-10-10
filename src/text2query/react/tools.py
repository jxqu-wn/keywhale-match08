"""Read-only evidence and query-checking tools for model-directed ReAct actions."""
import json
import re
from ..shared.dsl_normalization import normalize_prediction
from ..shared.education import expand_prediction, model_query_errors
from ..shared.generation import parse_output, validate
from ..shared.context import prompt_example
from ..shared.data import references
from ..shared.query_rules import mask_sql
from ..shared.retrieval_context import compatible_example, dictionary_matches, dict_tables_for, select_knowledge, triggered_dictionary

TOOLS = {
    'get_schema': {'purpose': '查真实库表、字段、类型、DDL或ES mapping；未指定表时关键词搜索。可跨候选域查找。',
                   'args': {'tables': ['可选，完整库名.表名或PG裸表名'], 'query': '可选关键词',
                            'source': '可选数据源', 'database': '可选数据库', 'limit': '可选1到12，默认8',
                            'include_source_documents': '可选布尔值；返回对应完整原始Schema文件'}},
    'search_knowledge': {'purpose': '查知识、枚举、公式、单位与字典，保留原题必需的知识。',
                         'args': {'query': '补充检索词', 'limit': '可选1到12，默认10',
                                  'include_dictionary_text': '可选布尔值；返回匹配字典的完整原始INSERT'}},
    'search_examples': {'purpose': '首先检索训练示例及schema_refs；精确匹配保留全部原始候选、重复/冲突标注和附加修订，原始标注不保证正确。',
                        'args': {'query': '补充检索词', 'limit': '可选1到5，默认3',
                                 'include_related': '存在同题案例时默认不混入其他问题；需要比较时设为true'}},
    'check_query': {'purpose': '检查候选的格式、表字段、SQLite编译、ES mapping、教育合并和已知语义错误；自动调整ES层级。返回具体错误和标准化prediction。',
                    'args': {'prediction': '规定的单源或双阶段查询对象，ES query使用对象',
                             'case_review': '可选简短改动说明；独立LLM会按题意和检索证据复核候选，合理修正可以保留'}},
    'finish': {'purpose': '提交与最近一次成功check_query一致的完整prediction；必须先获取所用表Schema和检索知识。',
               'args': {'prediction': '完整查询对象'}},
}

IDENT = r'(?:"[^"]+"|`[^`]+`|[A-Za-z_][\w$]*)'
TABLE_PATTERN = re.compile(r'\b(?:FROM|JOIN)\s+(?:LATERAL\s+)?('+IDENT+r'(?:\s*\.\s*'+IDENT+r')?)', re.I)
CTE_PATTERN = re.compile(r'(?:\bWITH(?:\s+RECURSIVE)?|,)\s*('+IDENT+r')\s*(?:\([^)]*\)\s*)?AS\s+(?:(?:NOT\s+)?MATERIALIZED\s+)?\(', re.I)
RESERVED = set('where join left right full inner outer cross on group order limit offset union having window using fetch for'.split())


def pg_references(sql, catalog):
    """Focused table/explicit alias checks, not a PostgreSQL parser or EXPLAIN."""
    text = mask_sql(sql)
    clean = lambda name: re.sub(r'["`\s]', '', name).lower()
    ctes = {clean(m.group(1)) for m in CTE_PATTERN.finditer(text)}
    known = {t['table'].lower(): t for t in catalog if t['source'] == 'POSTGRESQL'}
    refs, errors, aliases, derived_aliases = set(), [], {}, set()
    for match in TABLE_PATTERN.finditer(text):
        name = clean(match.group(1))
        if text[match.end():].lstrip().startswith('('):
            continue  # table-valued function
        alias = re.match(r'\s+(?:AS\s+)?('+IDENT+r')', text[match.end():], re.I)
        alias_name = clean(alias.group(1)) if alias else ''
        if name in ctes:
            derived_aliases.add(name)
            if alias_name and alias_name not in RESERVED:
                derived_aliases.add(alias_name)
            continue
        table = known.get(name.split('.')[-1])
        if table is None or ('.' in name and name != f"{table.get('database')}.{table['table']}".lower()):
            errors.append('PG catalog中不存在表: ' + name)
            continue
        refs.add(f"{table.get('database')}.{table['table']}".lower())
        if alias_name and alias_name not in RESERVED:
            aliases[alias_name] = table
        aliases[table['table'].lower()] = table
    for match in re.finditer(r'('+IDENT+r')\s*\.\s*('+IDENT+r')', text):
        alias, column = clean(match.group(1)), clean(match.group(2))
        # A reused alias across physical/CTE scopes cannot be resolved by regex.
        if alias in aliases and alias not in derived_aliases and column not in {c['name'].lower() for c in aliases[alias]['columns']}:
            errors.append('PG物理表字段不存在: ' + alias + '.' + column)
    return refs, sorted(set(errors))


class QueryTools:
    def __init__(self, context, question_context, semantic_reviewer=None):
        self.context = context
        self.ctx = question_context
        self.scope = self.ctx['routing']
        self.inspected = set()
        self.examples_searched = False
        self.knowledge_searched = False
        self.accepted = None
        self.sr = context.sr
        self.semantic_reviewer = semantic_reviewer
        self.retrieved_knowledge = []
        self.retrieved_dictionaries = []

    def workflow(self):
        next_action = ('search_examples' if not self.examples_searched else
                       'get_schema' if not self.inspected else
                       'search_knowledge' if not self.knowledge_searched else
                       'finish' if self.accepted is not None else 'check_query')
        return {'next_action':next_action,
                'required_order':['search_examples','get_schema','search_knowledge','check_query','finish'],
                'completed':{'search_examples':self.examples_searched, 'get_schema':bool(self.inspected),
                             'search_knowledge':self.knowledge_searched,
                             'check_query':self.accepted is not None},
                'scope':self.scope,
                'instruction':'可重复检索；补查Schema或重新检索示例后必须重新查知识；任何证据检索后必须重新check_query才能finish。'}

    def invalidate(self, knowledge=False):
        self.accepted = None
        if knowledge:
            self.knowledge_searched = False

    @staticmethod
    def limit(args, default, maximum):
        number = args.get('limit', default)
        if type(number) is not int or not 1 <= number <= maximum:
            raise ValueError('limit超出工具允许范围')
        return number

    def get_schema(self, args):
        if not self.examples_searched:
            return {'ok':False, 'errors':['请先调用search_examples获取原始示例与候选表，再get_schema']}
        limit = self.limit(args, 8, 12)
        requested = args.get('tables', [])
        if not isinstance(requested, list) or any(not isinstance(x, str) for x in requested) or len(requested) > 12:
            raise ValueError('tables必须为最多12项的表名列表')
        query = args.get('query') or self.ctx['question']
        if not isinstance(query, str):
            raise ValueError('query必须为字符串')
        names = {x.lower() for x in requested}
        candidates = [t for t in self.context.catalog
                      if (not args.get('source') or t['source'] == args['source'])
                      and (not args.get('database') or t.get('database') == args['database'])]
        if names:
            tables = [t for t in candidates if t['table'].lower() in names or
                      f"{t.get('database')}.{t['table']}".lower() in names]
        else:
            ranked = [self.context.catalog[i] for score, i in self.sr.search(query, len(self.context.catalog)) if score > 0]
            # A hypernym (车辆/船舶/驾照...) or an explicit table-name mention outranks generic BM25 hits.
            triggers = {(t['source'], t.get('database'), t['table']) for t in dict_tables_for(query, self.context.catalog)}
            ql = query.lower()
            def evidence(table):
                key = (table['source'], table.get('database'), table['table'])
                name = table['table'].lower()
                return (0 if key in triggers else 1,
                        0 if (name in ql or f"{table.get('database')}.{name}".lower() in ql) else 1)
            ranked.sort(key=evidence)
            # Keep exact original-question domain evidence before generic matches.
            preferred = [t for t in ranked if t in self.ctx['schema']]
            tables = [t for t in preferred + [t for t in ranked if t not in preferred] if t in candidates][:limit]
        missing = sorted(name for name in names if not any(name in (t['table'].lower(), f"{t.get('database')}.{t['table']}".lower()) for t in tables))
        previous = set(self.inspected)
        for table in tables:
            self.inspected.add((table['source'], f"{table.get('database')}.{table['table']}".lower()))
        if tables:
            self.invalidate(knowledge=self.inspected != previous)
            inspected = [t for t in self.context.catalog
                         if (t['source'], f"{t.get('database')}.{t['table']}".lower()) in self.inspected]
            self.scope = {'sources':sorted({t['source'] for t in inspected}),
                          'databases':sorted({t['database'] for t in inspected if t.get('database')}),
                          'confidence':'schema', 'reason':'已获取的真实Schema，知识按此范围检索'}
        source_documents = []
        if args.get('include_source_documents'):
            from ..shared.paths import RAW, ROOT
            from ..shared.data import read_text
            source_documents = [{'file':str((RAW/'Schema'/name).relative_to(ROOT)),
                                 'text':read_text(RAW/'Schema'/name)[0]}
                                for name in sorted({t['file'] for t in tables})]
        return {'ok': not missing and bool(tables), 'tables': tables, 'missing_tables': missing,
                'scope':self.scope, 'source_documents':source_documents,
                'reference_documents':self.context.reference_documents if args.get('include_source_documents') else [],
                'instruction': '表名和字段以本工具返回为准；缺失表可继续搜索其他数据源，不能臆造。'}

    def search_knowledge(self, args):
        if not self.examples_searched or not self.inspected:
            return {'ok':False, 'errors':['请先完成search_examples和get_schema，再search_knowledge']}
        limit = self.limit(args, 10, 12)
        extra = args.get('query', '')
        if not isinstance(extra, str):
            raise ValueError('query必须为字符串')
        query = self.ctx['question'] + ' ' + extra
        items = select_knowledge(query, self.context.knowledge, self.context.kr, self.scope, self.context.catalog,
                                 inspected_tables={name for _, name in self.inspected}
                                 if self.context.raw_evidence else None)
        # Mandatory facts are retained even if a caller asks for a short result.
        mandatory = [x for x in items if x['retrieval_reason'] == '字段/公式依赖，必须同时提供']
        others = [x for x in items if x not in mandatory]
        self.knowledge_searched = True
        self.invalidate()
        dictionaries = dictionary_matches(query, self.context.dictionaries, self.scope)
        for item in triggered_dictionary(query, self.context.dictionaries, self.scope, self.context.catalog):
            if not any(doc['id'] == item['id'] for doc in dictionaries):
                dictionaries.append(item)
        if args.get('include_dictionary_text'):
            by_id = {d['id']:d for d in self.context.dictionaries}
            for item in dictionaries:
                if item['id'] in by_id:
                    item['raw_document'] = by_id[item['id']]
                else:
                    # A triggered table can combine multiple original INSERT documents.
                    item['raw_documents'] = [doc for doc in self.context.dictionaries
                        if doc['table'].lower() == item['id'].removeprefix('trigger:')
                        and doc.get('source', 'MYSQL') == item['source']
                        and doc.get('database') == item['database']]
        self.retrieved_dictionaries = dictionaries
        adjudications = [rule for rule in self.ctx['schema_adjudications'] if any(
            name.lower() == table.lower() for _, name in self.inspected for table in rule['tables'])]
        self.retrieved_knowledge = mandatory + others[:max(0, limit - len(mandatory))]
        return {'ok': True, 'knowledge': self.retrieved_knowledge,
                'dictionary': dictionaries, 'scope':self.scope,
                'schema_adjudications': adjudications,
                'query_helpers': self.ctx['query_helpers'],
                'instruction': '知识未区分表/字段却列出多种格式时视为未消解歧义，不能据此随意修改同题案例；必要时补查具体字段证据。'}

    def search_examples(self, args):
        limit = self.limit(args, 3, 5)
        extra = args.get('query', '')
        if not isinstance(extra, str):
            raise ValueError('query必须为字符串')
        candidates = [self.context.examples[i] for score, i in self.context.er.search(self.ctx['question'] + ' ' + extra, 40)
                      if score > 0 and compatible_example(self.context.examples[i], self.scope)]
        exact = self.context.exact_examples(self.ctx['question'])
        candidates = exact + [row for row in candidates if row not in exact]
        self.examples_searched = True
        self.invalidate(knowledge=True)
        related = [row for row in candidates if row not in exact]
        examples = [] if exact and not args.get('include_related') else candidates
        return {'ok': True, 'examples': [prompt_example(x, self.context.catalog) for x in examples[:limit]],
                'exact_matches':[prompt_example(x, self.context.catalog) for x in exact],
                'exact_match_count':len(exact),
                'corrected_examples': self.ctx['corrected_examples'],
                'question_correction': self.ctx['question_correction'],
                'related_examples_available': len(related),
                'instruction': '精确匹配优先核对，但原始标注不保证正确；保留重复与冲突，区分raw_annotation和reviewed_annotation。按schema_refs补查物理表，再检索知识。'}

    def check_query(self, args):
        self.accepted = None
        raw = args.get('prediction')
        if not isinstance(raw, dict):
            raise ValueError('prediction必须为查询对象')
        raw = parse_output(json.dumps(raw, ensure_ascii=False))
        normalized, transforms = normalize_prediction(raw)
        prediction = expand_prediction(normalized, self.ctx, self.context.catalog)
        if prediction != normalized:
            transforms.append('education_view_cte_expansion')
        errors = model_query_errors(normalized, self.ctx) + validate(prediction, self.context.catalog, self.ctx['question'], self.ctx['business_rules'])
        pairs = [(prediction.get('db'), prediction.get('query'))] if 'db' in prediction else [
            (prediction.get('db1'), prediction.get('query1')), (prediction.get('db2'), prediction.get('query2'))]
        for db, sql in pairs:
            if not isinstance(sql, str):
                continue
            if db == 'Elasticsearch':
                refs = {f"{t.get('database')}.{t['table']}".lower() for t in self.context.catalog if t['source'] == db}
            else:
                refs = {r.lower() for r in references(sql)}
                if db == 'POSTGRESQL':
                    pg_refs, pg_errors = pg_references(sql, self.context.catalog)
                    refs.update(pg_refs)
                    errors.extend(pg_errors)
            unseen = sorted(ref for ref in refs if (db, ref) not in self.inspected)
            if unseen:
                errors.append('请先用get_schema核对这些物理表: ' + ', '.join(unseen))
        if not self.knowledge_searched:
            errors.append('请先调用search_knowledge核对知识与字典')
        if not self.examples_searched:
            errors.append('请先调用search_examples核对原始示例和冲突候选')
        exact = self.context.exact_examples(self.ctx['question']) if self.context.raw_evidence else []
        baseline_predictions = [{key: row[key] for key in ('db', 'query', 'db1', 'query1', 'db2', 'query2') if key in row}
                                for row in exact]
        changed = bool(exact) and normalized not in baseline_predictions
        review = args.get('case_review')
        semantic_review = None
        if not errors and self.semantic_reviewer is not None:
            semantic_review = self.semantic_reviewer({
                'question': self.ctx['question'], 'candidate': prediction,
                'case_review': review,
                'same_question_cases': [prompt_example(row, self.context.catalog) for row in exact],
                'schema': [table for table in self.context.catalog if
                           (table['source'], f"{table.get('database')}.{table['table']}".lower()) in self.inspected],
                'knowledge': self.retrieved_knowledge,
                'dictionary': self.retrieved_dictionaries,
                'schema_adjudications': self.ctx['schema_adjudications'],
                'education_scope': self.ctx['education_scope'],
                'business_rules': self.ctx['business_rules']})
            if semantic_review['verdict'] == 'revise':
                errors.append('独立LLM语义复核要求修正：' + json.dumps(semantic_review, ensure_ascii=False))
        result = {'ok': not errors, 'errors': sorted(set(errors)), 'candidate': normalized, 'prediction': prediction,
                  'automatic_transforms': sorted(set(transforms)), 'execution_validated': False,
                  'case_review': review, 'modified_exact_case': changed,
                  'semantic_review': semantic_review,
                  'semantics_reviewed': semantic_review is not None,
                  'checks': ['format/read-only', 'catalog references', 'PG physical aliases', 'SQLite raw-DDL EXPLAIN', 'ES mapping/clauses', 'question rules', 'retrieved evidence']}
        if result['ok']:
            self.accepted = result
        return result

    def finish(self, args):
        previous = self.accepted
        raw = args.get('prediction')
        if not isinstance(raw, dict):
            raise ValueError('prediction必须为查询对象')
        normalized, _ = normalize_prediction(parse_output(json.dumps(raw, ensure_ascii=False)))
        if previous is not None and normalized in (previous['candidate'], previous['prediction']):
            # Expanded education CTEs must not be expanded a second time.
            return {**previous, 'finished': True}
        self.accepted = None
        return {'ok': False, 'errors': ['finish必须使用最近一次成功check_query的同一候选；请重新check_query']}

    def dispatch(self, action, args):
        if not isinstance(action, str) or action not in TOOLS or not isinstance(args, dict):
            return {'ok': False, 'errors': ['未知action或args不是对象'], 'available_actions': list(TOOLS)}
        try:
            result = getattr(self, action)(args)
            return {**result, 'workflow':self.workflow()}
        except (ValueError, TypeError, KeyError) as exc:
            self.accepted = None
            return {'ok': False, 'errors': [str(exc)], 'workflow':self.workflow()}
