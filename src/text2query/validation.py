"""Offline SQL preparation and focused ES structure/mapping checks.

SQL compilation uses raw, trusted schema DDL and EXPLAIN in isolated memory.
It is not real-data execution. ES checks cover clause arity and known fields,
not the entire server DSL or business semantics.
"""
import fnmatch
import re
import sqlite3
from .pipeline import references


def sqlite_connection(catalog, sql):
    connection = sqlite3.connect(':memory:')
    try:
        for database in sorted({ref.split('.',1)[0] for ref in references(sql)}):
            connection.execute(f'ATTACH DATABASE ":memory:" AS "{database}"')
            for table in catalog:
                if table['source'] != 'SQLite' or table.get('database') != database:
                    continue
                pattern = r'^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?("[^"]+"|`[^`]+`|\[[^\]]+\]|\w+)'
                match = re.match(pattern,table['ddl'],re.I)
                if not match:
                    raise ValueError('无法读取原始DDL: '+database+'.'+table['table'])
                name = match.group(1).strip('"`[]').replace('"','""')
                ddl = re.sub(pattern,lambda _: f'CREATE TABLE "{database}"."{name}"',table['ddl'],count=1,flags=re.I)
                connection.execute(ddl)
        return connection
    except Exception:
        connection.close()
        raise


def sql_structure_errors(db, sql, catalog):
    if db == 'MYSQL':
        # Do not interpret SQL keywords inside literals/comments as syntax.
        masked = re.sub(r"'(?:''|[^'])*'|--[^\n]*|/\*.*?\*/",' ',sql,flags=re.S)
        return ['MySQL不支持FULL OUTER JOIN；使用UNION键集合配合LEFT JOIN等价表达'] if re.search(r'\bFULL\s+(?:OUTER\s+)?JOIN\b',masked,re.I) else []
    if db != 'SQLite':
        return []
    connection = None
    try:
        connection = sqlite_connection(catalog,sql)
        # Federation placeholder is a query dependency, not a real table.
        prepared = re.sub(r'\bIN\s*\(\s*result\s*\)', 'IN (SELECT NULL WHERE 0)',sql,flags=re.I)
        connection.execute('EXPLAIN '+prepared)
        return []
    except (sqlite3.Error,ValueError) as error:
        return ['SQLite原始DDL编译失败: '+str(error)]
    finally:
        if connection:
            connection.close()


def es_structure_errors(dsl, catalog):
    errors = []
    tables = [table for table in catalog if table['source']=='Elasticsearch']
    mapping = tables[0]['mapping'] if tables else {}
    fields = set(mapping)
    for name, spec in mapping.items():
        fields.update(name+'.'+suffix for suffix in spec.get('fields',{}))

    def field(name, path):
        if fields and isinstance(name,str) and name not in fields and not any(fnmatch.fnmatchcase(known,name) for known in fields):
            errors.append(path+': mapping中不存在字段'+name)

    def clause(value, path):
        if isinstance(value,list):
            for i, item in enumerate(value):
                clause(item,f'{path}[{i}]')
            return
        if not isinstance(value,dict) or len(value)!=1:
            errors.append(path+': 每个查询子句必须只有一个查询类型，多条件用数组')
            return
        kind, body = next(iter(value.items()))
        if not isinstance(body,dict):
            errors.append(path+'.'+kind+': 查询体必须是对象')
            return
        if kind=='bool':
            for name in ('must','should','filter','must_not'):
                if name in body:
                    clause(body[name],path+'.bool.'+name)
        elif kind in ('nested','constant_score','has_child','has_parent'):
            for name in ('query','filter'):
                if name in body:
                    clause(body[name],path+'.'+kind+'.'+name)
        elif kind=='dis_max':
            clause(body.get('queries',[]),path+'.dis_max.queries')
        elif kind in ('term','terms','range','match','match_phrase','prefix','wildcard','regexp','fuzzy'):
            for name in body:
                if name not in ('boost','_name'):
                    field(name,path+'.'+kind)
        elif kind=='exists':
            field(body.get('field'),path+'.exists')
        elif kind in ('geo_distance','geo_polygon','geo_bounding_box'):
            for name in body:
                if name not in ('distance','distance_type','validation_method','ignore_unmapped','type','boost','_name'):
                    field(name,path+'.'+kind)

    def sorts(value, path):
        if not isinstance(value,list):
            value = [value]
        for item in value:
            if isinstance(item,str):
                if not item.startswith('_'):
                    field(item,path)
            elif isinstance(item,dict):
                for name in item:
                    if not name.startswith('_'):
                        field(name,path)
                    elif name=='_geo_distance' and isinstance(item[name],dict):
                        for key in item[name]:
                            if key not in ('order','unit','mode','distance_type','ignore_unmapped'):
                                field(key,path+'._geo_distance')

    def aggregations(value, path):
        if not isinstance(value,dict):
            errors.append(path+': 聚合必须为对象')
            return
        for name, spec in value.items():
            if not isinstance(spec,dict):
                errors.append(path+'.'+name+': 聚合定义必须为对象')
                continue
            for kind, body in spec.items():
                if kind in ('aggs','aggregations'):
                    aggregations(body,path+'.'+name+'.'+kind)
                elif kind=='filter':
                    clause(body,path+'.'+name+'.filter')
                elif isinstance(body,dict):
                    if 'field' in body:
                        field(body['field'],path+'.'+name+'.'+kind)
                    if kind=='top_hits' and 'sort' in body:
                        sorts(body['sort'],path+'.'+name+'.top_hits.sort')
    for key in ('query','post_filter'):
        if key in dsl:
            clause(dsl[key],key)
    if 'sort' in dsl:
        sorts(dsl['sort'],'sort')
    for key in ('aggs','aggregations'):
        if key in dsl:
            aggregations(dsl[key],key)
    selection = dsl.get('_source')
    if isinstance(selection,str):
        selection = [selection]
    if isinstance(selection,list):
        for name in selection:
            field(name,'_source')
    if 'size' in dsl and (type(dsl['size']) is not int or dsl['size']<0):
        errors.append('size必须为非负整数')
    return list(dict.fromkeys(errors))
