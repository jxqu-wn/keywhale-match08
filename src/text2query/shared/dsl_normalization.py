"""Conservative ES level normalization; never invent conditions or overwrite values."""
import copy
import json

SECTIONS = {'aggs', 'aggregations', 'sort', '_source', 'size', 'from',
            'track_total_hits', 'post_filter', 'script_fields', 'stored_fields',
            'docvalue_fields', 'fields', 'runtime_mappings'}


def normalize_es(raw):
    if raw.get('db') != 'Elasticsearch' or set(raw) - {'db', 'query'} - SECTIONS:
        raise ValueError('Not a supported single-source ES object')
    dsl = json.loads(raw['query']) if isinstance(raw['query'], str) else copy.deepcopy(raw['query'])
    if not isinstance(dsl, dict):
        raise ValueError('DSL is not an object')

    def merge(key, value):
        if key in dsl and dsl[key] != value:
            raise ValueError('Conflicting DSL section: ' + key)
        dsl[key] = value

    for key in sorted(set(raw) & SECTIONS):
        merge(key, raw[key])
    clause = dsl.get('query')
    if isinstance(clause, dict):
        for key in list(clause):
            if key in SECTIONS:
                merge(key, clause.pop(key))
    return {'db': 'Elasticsearch', 'query': json.dumps(dsl, ensure_ascii=False, separators=(',', ':'))}


def normalize_prediction(raw):
    """Normalize supported single/federated ES wrappers; preserve SQL verbatim."""
    result = copy.deepcopy(raw)
    transforms = []
    if result.get('db') == 'Elasticsearch':
        normalized = normalize_es(result)
        if normalized != result:
            transforms.append('es_dsl_level_normalization')
        result = normalized
    else:
        for db_key, query_key in [('db1', 'query1'), ('db2', 'query2')]:
            if result.get(db_key) == 'Elasticsearch':
                before = result[query_key]
                result[query_key] = normalize_es({'db': 'Elasticsearch', 'query': before})['query']
                if before != result[query_key]:
                    transforms.append('es_dsl_level_normalization')
    return result, sorted(set(transforms))
