"""Read-only evidence and query-checking tools for model-directed ReAct actions."""
import json
import re
from .dsl_normalization import normalize_prediction
from .education import expand_prediction, model_query_errors
from .model_pipeline import parse_output, prompt_example, validate
from .pipeline import references
from .query_rules import mask_sql
from .retrieval_context import compatible_example, dictionary_matches, select_knowledge

TOOLS = {
    'get_schema': {'purpose': '查真实库表、字段、类型、DDL或ES mapping；未指定表时关键词搜索。可跨候选域查找。',
                   'args': {'tables': ['可选，完整库名.表名或PG裸表名'], 'query': '可选关键词',
                            'source': '可选数据源', 'database': '可选数据库', 'limit': '可选1到12，默认8'}},
    'search_knowledge': {'purpose': '查知识、枚举、公式、单位与字典，保留原题必需的知识。',
                         'args': {'query': '补充检索词', 'limit': '可选1到12，默认10'}},
    'search_examples': {'purpose': '查已修正训练示例及其schema_refs；必须按当前题目修改参数。',
                        'args': {'query': '补充检索词', 'limit': '可选1到5，默认3'}},
    'check_query': {'purpose': '检查候选的格式、表字段、SQLite编译、ES mapping、教育合并和已知语义错误；自动调整ES层级。返回具体错误和标准化prediction。',
                    'args': {'prediction': '规定的单源或双阶段查询对象，ES query使用对象'}},
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
    def __init__(self, context, question_context):
        self.context = context
        self.ctx = question_context
        self.scope = self.ctx['routing']
        self.inspected = set()
        self.knowledge_searched = False
        self.accepted = None
        self.sr = context.sr

    @staticmethod
    def limit(args, default, maximum):
        number = args.get('limit', default)
        if type(number) is not int or not 1 <= number <= maximum:
            raise ValueError('limit超出工具允许范围')
        return number

    def get_schema(self, args):
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
            # Keep exact original-question domain evidence before generic matches.
            preferred = [t for t in ranked if t in self.ctx['schema']]
            tables = [t for t in preferred + [t for t in ranked if t not in preferred] if t in candidates][:limit]
        missing = sorted(name for name in names if not any(name in (t['table'].lower(), f"{t.get('database')}.{t['table']}".lower()) for t in tables))
        for table in tables:
            self.inspected.add((table['source'], f"{table.get('database')}.{table['table']}".lower()))
        return {'ok': not missing and bool(tables), 'tables': tables, 'missing_tables': missing,
                'instruction': '表名和字段以本工具返回为准；缺失表可继续搜索其他数据源，不能臆造。'}

    def search_knowledge(self, args):
        limit = self.limit(args, 10, 12)
        extra = args.get('query', '')
        if not isinstance(extra, str):
            raise ValueError('query必须为字符串')
        query = self.ctx['question'] + ' ' + extra
        items = select_knowledge(query, self.context.knowledge, self.context.kr, self.scope, self.context.catalog)
        # Mandatory facts are retained even if a caller asks for a short result.
        mandatory = [x for x in items if x['retrieval_reason'] == '字段/公式依赖，必须同时提供']
        others = [x for x in items if x not in mandatory]
        self.knowledge_searched = True
        return {'ok': True, 'knowledge': mandatory + others[:max(0, limit - len(mandatory))],
                'dictionary': dictionary_matches(query, self.context.dictionaries, self.scope),
                'schema_adjudications': self.ctx['schema_adjudications'],
                'query_helpers': self.ctx['query_helpers']}

    def search_examples(self, args):
        limit = self.limit(args, 3, 5)
        extra = args.get('query', '')
        if not isinstance(extra, str):
            raise ValueError('query必须为字符串')
        candidates = [self.context.examples[i] for score, i in self.context.er.search(self.ctx['question'] + ' ' + extra, 40)
                      if score > 0 and compatible_example(self.context.examples[i], self.scope)]
        return {'ok': True, 'examples': [prompt_example(x, self.context.catalog) for x in candidates[:limit]],
                'corrected_examples': self.ctx['corrected_examples'],
                'question_correction': self.ctx['question_correction'],
                'instruction': '仅供参考，不复制原参数；Schema、业务规则和当前问题优先。'}

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
        result = {'ok': not errors, 'errors': sorted(set(errors)), 'candidate': normalized, 'prediction': prediction,
                  'automatic_transforms': sorted(set(transforms)), 'execution_validated': False,
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
            return getattr(self, action)(args)
        except (ValueError, TypeError, KeyError) as exc:
            self.accepted = None
            return {'ok': False, 'errors': [str(exc)]}
