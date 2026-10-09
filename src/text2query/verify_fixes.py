"""Targeted behavior regression, with synthetic SQLite data (not official accuracy)."""
import argparse
import json
import re
import sqlite3
from pathlib import Path
from .pipeline import ROOT, OUT, load
from .model_pipeline import validate


def fixture():
    con = sqlite3.connect(':memory:')
    con.executescript('''
    ATTACH DATABASE ':memory:' AS book_publishing_company;
    CREATE TABLE book_publishing_company.stores(stor_id TEXT,stor_name TEXT);
    CREATE TABLE book_publishing_company.titles(title_id TEXT,title TEXT);
    CREATE TABLE book_publishing_company.sales(stor_id TEXT,title_id TEXT,qty INTEGER);
    INSERT INTO book_publishing_company.stores VALUES ('s1','Alpha'),('s2','Beta');
    INSERT INTO book_publishing_company.titles VALUES ('a','A'),('b','B'),('c','C');
    INSERT INTO book_publishing_company.sales VALUES
      ('s1','a',1),('s1','a',100),('s1','b',20),('s1','c',12),('s1','c',12),('s2','b',50);
    ATTACH DATABASE ':memory:' AS image_and_language;
    CREATE TABLE image_and_language.IMG_OBJ(IMG_ID INTEGER,OBJ_SAMPLE_ID INTEGER,OBJ_CLASS_ID INTEGER,X INTEGER,Y INTEGER,W INTEGER,H INTEGER);
    CREATE TABLE image_and_language.OBJ_CLASSES(OBJ_CLASS_ID INTEGER,OBJ_CLASS TEXT);
    INSERT INTO image_and_language.OBJ_CLASSES VALUES(1,'cat'),(2,'dog');
    INSERT INTO image_and_language.IMG_OBJ VALUES
      (72,1,1,341,27,42,51),(72,2,2,0,0,10,10),(72,3,2,1,1,20,20),
      (1,1,2,341,27,42,51);
    ''')
    # Generated queries can only read these in-memory fixtures.
    allowed = {sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION}
    con.set_authorizer(lambda action,*args: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
    return con


def checks(records):
    results = {}
    p = records[2394]['prediction']
    dsl = json.loads(p['query'])
    results['2394_es_root_structure'] = isinstance(dsl.get('query'),dict) and len(dsl['query'])==1 and bool(dsl.get('aggs'))
    p = records[2518]['prediction']
    results['2518_observed_max_speed'] = p.get('db')=='POSTGRESQL' and bool(re.search(r'MAX\s*\(\s*sog\s*\)',p.get('query',''),re.I)) and bool(re.search(r'GROUP\s+BY\s+mmsi',p.get('query',''),re.I))
    p = records[2937]['prediction']
    results['2937_ais_record_grain'] = p.get('db2')=='POSTGRESQL' and bool(re.match(r'\s*SELECT\s+\*\s+FROM',p.get('query2',''),re.I))
    p = records[2945]['prediction']
    projection = p.get('query2','').split('FROM',1)[0].lower()
    results['2945_federated_projection'] = all(name in projection for name in ('mmsi','endurance','company_name'))
    with fixture() as con:
        p = records[3049]['prediction']
        values = con.execute(p['query']).fetchall()
        # Top store s1 has total145; its book B totals20, versus A101 and C24.
        results['3049_total_sales_fixture'] = len(values)==1 and 'B' in values[0] and ('Alpha' in values[0] or 's1' in values[0])
        p = records[5441]['prediction']
        values = con.execute(p['query']).fetchall() if p.get('db')=='SQLite' else []
        results['5441_single_source_image_fixture'] = len(values)==1 and 3 in values[0] and any('cat' in str(value) for value in values[0])
    return results


def main(directory):
    catalog = load(OUT/'catalog.json')
    records = {i:load(directory/f'{i}.json') for i in (6,2394,2518,2937,2945,2953,3049,5441)}
    report = {'basic_checks':{i:not validate(r['prediction'],catalog,r['question']) and not r.get('errors') for i,r in records.items()},
              'targeted_checks':checks(records),'execution_scope':'synthetic SQLite fixtures only; PostgreSQL and ES checks are static',
              'official_accuracy':None,'selection':'known failure cases and two controls, not an unbiased validation sample'}
    # Prove the fixture exposes the old defect rather than merely accepting any query.
    old = load(ROOT/'artifacts/runs/evaluation/evaluate-b017d2ae0342/3049.json')
    with fixture() as con:
        report['old_sales_query_fixture_result'] = con.execute(old['prediction']['query']).fetchall()
    report['passed'] = all(report['basic_checks'].values()) and all(report['targeted_checks'].values())
    (directory/'fixes_verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))
    if not report['passed']:
        raise SystemExit('Targeted regression failed')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    main(parser.parse_args().directory)
