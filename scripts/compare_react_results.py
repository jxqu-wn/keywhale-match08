"""Compare all test predictions and replay previously reviewed synthetic cases.

No hidden test labels or production business records are available. Textual
changes, compilation and the targeted fixtures are reported separately.
"""
import argparse
import collections
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from text2query.shared.context import Context
from text2query.shared.data import queries, references
from text2query.shared.generation import validate
from text2query.shared.validation import sqlite_connection
from text2query.react.tools import pg_references
from compare_deepseek_sqlite import fixture_cases as flash_fixtures, insert_rows, equivalent, check_sql
from analyze_last_submission import fixture_cases as initial_fixtures, equivalent as unordered_equivalent

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def prediction(row):
    return {key: value for key, value in row.items() if key != 'question_id'}


def source(row):
    return row.get('db') or row['db1'] + '->' + row['db2']


def domain(row, catalog):
    domains = set()
    pairs = [(row.get('db'), row.get('query'))] if 'db' in row else [
        (row.get('db1'), row.get('query1')), (row.get('db2'), row.get('query2'))]
    for db, query in pairs:
        if db == 'POSTGRESQL':
            refs = pg_references(query, catalog)[0]
        elif db == 'Elasticsearch':
            refs = {'Elasticsearch.medical_institutions'}
        else:
            refs = references(query)
        domains.update(ref.split('.', 1)[0] for ref in refs)
    return sorted(domains)


def check(row, question, context, rules):
    errors = validate(prediction(row), context.catalog, question, rules)
    pairs = [(row.get('db'), row.get('query'))] if 'db' in row else [
        (row.get('db1'), row.get('query1')), (row.get('db2'), row.get('query2'))]
    for db, query in pairs:
        if db == 'POSTGRESQL':
            errors += pg_references(query, context.catalog)[1]
    return {'errors': sorted(set(errors)), 'sqlite': check_sql(row, context.catalog)}


def execute_fixture(case, row, catalog):
    if row.get('db') != 'SQLite':
        return {'matches_expected': False, 'error': '该SQLite构造案例的预测选择了其他数据源'}
    connection = None
    try:
        needed = row['query'] + ''.join(' SELECT * FROM ' + table for table in case['rows'])
        connection = sqlite_connection(catalog, needed)
        connection.set_progress_handler(lambda: 1, 500000)
        for table, rows in case['rows'].items():
            insert_rows(connection, table, rows)
        actual = [list(value) for value in connection.execute(row['query']).fetchall()]
        comparator = equivalent if case.get('ordered') else unordered_equivalent
        return {'actual': actual, 'matches_expected': comparator(actual, case['expected'])}
    except Exception as error:
        return {'matches_expected': False, 'error': str(error)}
    finally:
        if connection is not None:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--initial', type=Path, required=True)
    parser.add_argument('--corrected-qwen', type=Path, required=True)
    parser.add_argument('--non-react', type=Path, required=True)
    parser.add_argument('--current', type=Path, required=True)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    files = {'qwen_initial': args.initial, 'qwen_corrected': args.corrected_qwen,
             'flash_non_react': args.non_react, 'flash_react': args.current}
    datasets = {label: {row['question_id']: row for row in load(path)} for label, path in files.items()}
    questions = {row['question_id']: row['question'] for row in load(ROOT / 'data/raw/subject/QA_test.json')}
    for dataset in datasets.values():
        if len(dataset) != 400 or set(dataset) != set(questions):
            raise ValueError('比较输入必须完整覆盖同一400题')
    context = Context(False)
    checks, pairs = {}, []
    for qid, question in questions.items():
        record = load(args.run / f'{qid}.json')
        checks[qid] = {label: check(dataset[qid], question, context, record['context']['business_rules'])
                       for label, dataset in datasets.items()}
        current = datasets['flash_react'][qid]
        comparisons = {label: {
            'text_equal': dataset[qid] == current,
            'source_changed': source(dataset[qid]) != source(current),
            'domain_changed': domain(dataset[qid], context.catalog) != domain(current, context.catalog),
            'column_count_changed': checks[qid][label]['sqlite']['column_count'] != checks[qid]['flash_react']['sqlite']['column_count'],
        } for label, dataset in datasets.items() if label != 'flash_react'}
        pairs.append({'question_id': qid, 'question': question,
            'predictions': {label: dataset[qid] for label, dataset in datasets.items()},
            'checks': checks[qid], 'comparisons': comparisons,
            'react_api_calls': record['api_calls'], 'react_actions': [turn['action'] for turn in record['trace']],
            'react_automatic_transforms': record.get('automatic_transforms', [])})
    cases = [{**case, 'family': 'previous_flash_comparison', 'ordered': True} for case in flash_fixtures()]
    cases += [{'id': qid, 'family': 'initial_qwen_audit', 'title': case['description'],
               'rows': case['tables'], 'expected': case['expected'], 'ordered': False}
              for qid, case in initial_fixtures().items()]
    for case in cases:
        case['primary_scope_comparable'] = not (case['family'] == 'previous_flash_comparison' and case['id'] in (3069, 5215))
        if not case['primary_scope_comparable']:
            case['assumption_note'] = '该历史案例只填充books订单库；初轮使用book_publishing_company，不能据其空结果判定初轮错误。另加出版库案例核对知识2/574规定的ytd_sales。'
        if case['id'] == 4424:
            for row in case['rows']['public_review_platform.Business']:
                row['active'] = 'True'
            case['assumption_note'] = "历史反例使用小写true；此比较按知识139/1342的'True'填充，保留各轮原SQL以同时检查枚举与去重。"
    year = datetime.now(timezone.utc).year
    publication_rows = {
        'book_publishing_company.publishers': [dict(pub_id='0001', pub_name='Publisher')],
        'book_publishing_company.titles': [dict(title_id='YTD001', title='YTD Leader', type='business', pubdate=f'{year}-01-01', pub_id='0001', ytd_sales=100),
                                           dict(title_id='YTD002', title='Order Leader', type='business', pubdate=f'{year}-01-01', pub_id='0001', ytd_sales=10)],
        'books.publisher': [dict(publisher_id=1, publisher_name='Publisher')],
        'books.book': [dict(book_id=1, title='YTD Leader', publisher_id=1), dict(book_id=2, title='Order Leader', publisher_id=1)],
        'books.cust_order': [dict(order_id=1, order_date=f'{year}-01-01')],
        'books.order_line': [dict(line_id=1, order_id=1, book_id=1, price=10),
                            dict(line_id=2, order_id=1, book_id=2, price=100),
                            dict(line_id=3, order_id=1, book_id=2, price=100)],
    }
    cases += [{'id': qid, 'family': 'ytd_knowledge_scope', 'primary_scope_comparable': True,
               'title': title, 'rows': publication_rows, 'expected': [['YTD Leader']], 'ordered': False,
               'assumption_note': '同时填充两个出版领域，令ytd_sales与订单汇总排名不同；3069按知识2/574，5215以初轮出版库的年初至今销量为构造口径。'}
              for qid, title in [(3069, '年初至今销售额以知识明确的titles.ytd_sales为依据'),
                                 (5215, '出版库畅销书与另一订单库畅销书不同，检查源域选择')]]
    for case in cases:
        case['outputs'] = {label: execute_fixture(case, dataset[case['id']], context.catalog)
                           for label, dataset in datasets.items()}
        case['synthetic'] = True
    summary = {'count': len(questions), 'runs': {}, 'comparisons': {}, 'fixture_families': {},
               'official_current_accuracy': None, 'official_current_score': None,
               'scope': '当前规则静态检查及有目的构造案例，不能外推隐藏测试准确率。'}
    for label, dataset in datasets.items():
        summary['runs'][label] = {
            'source_counts': dict(collections.Counter(source(row) for row in dataset.values())),
            'static_error_ids': [qid for qid in questions if checks[qid][label]['errors']],
            'sqlite_count': sum(row.get('db') == 'SQLite' for row in dataset.values()),
            'sqlite_compiled': sum(checks[qid][label]['sqlite']['compiled'] is True for qid in questions),
        }
    for label in ('qwen_initial', 'qwen_corrected', 'flash_non_react'):
        summary['comparisons'][label] = {
            'text_equal': sum(pair['comparisons'][label]['text_equal'] for pair in pairs),
            'text_different': sum(not pair['comparisons'][label]['text_equal'] for pair in pairs),
            'source_changed_ids': [pair['question_id'] for pair in pairs if pair['comparisons'][label]['source_changed']],
            'domain_changed_ids': [pair['question_id'] for pair in pairs if pair['comparisons'][label]['domain_changed']],
            'column_count_changed_ids': [pair['question_id'] for pair in pairs if pair['comparisons'][label]['column_count_changed']],
        }
    for family in sorted({case['family'] for case in cases}):
        selected = [case for case in cases if case['family'] == family]
        summary['fixture_families'][family] = {
            'case_count': len(selected), 'unique_question_count': len({case['id'] for case in selected}),
            'matches_expected': {label: sum(case['outputs'][label]['matches_expected'] for case in selected) for label in datasets},
            'comparisons': {label: {
                'improvement_ids': [case['id'] for case in selected if not case['outputs'][label]['matches_expected'] and case['outputs']['flash_react']['matches_expected']],
                'regression_ids': [case['id'] for case in selected if case['outputs'][label]['matches_expected'] and not case['outputs']['flash_react']['matches_expected']],
            } for label in ('qwen_initial', 'qwen_corrected', 'flash_non_react')},
        }
        comparable = [case for case in selected if case['primary_scope_comparable']]
        summary['fixture_families'][family]['primary_scope_comparable'] = {
            'case_count': len(comparable),
            'initial_matches_expected': sum(case['outputs']['qwen_initial']['matches_expected'] for case in comparable),
            'react_matches_expected': sum(case['outputs']['flash_react']['matches_expected'] for case in comparable),
            'improvement_ids': [case['id'] for case in comparable if not case['outputs']['qwen_initial']['matches_expected'] and case['outputs']['flash_react']['matches_expected']],
            'regression_ids': [case['id'] for case in comparable if case['outputs']['qwen_initial']['matches_expected'] and not case['outputs']['flash_react']['matches_expected']],
            'excluded_scope_mismatch_ids': [case['id'] for case in selected if not case['primary_scope_comparable']],
        }
    records = [load(args.run / f'{qid}.json') for qid in questions]
    summary['react_run_metrics'] = load(args.run / 'metrics.json')
    summary['automatic_json_closure_ids'] = [record['question_id'] for record in records if record.get('format_recovery')]
    summary['check_repair_question_ids'] = [record['question_id'] for record in records
        if any(turn['action'] == 'check_query' and not turn['observation'].get('ok') for turn in record['trace'])]
    summary['model_json_error_question_ids'] = [record['question_id'] for record in records
        if any(turn['action'] is None for turn in record['trace'])]
    summary['usage'] = dict(collections.Counter())
    usage = collections.Counter()
    for path in args.run.glob('*.response.*.json'):
        for key, value in load(path).get('usage', {}).items():
            if isinstance(value, int):
                usage[key] += value
    summary['usage'] = dict(usage)
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = {'files': {label: str(path) for label, path in files.items()},
                'sha256': {label: hashlib.sha256(path.read_bytes()).hexdigest() for label, path in files.items()},
                'run': str(args.run), 'summary': summary, 'pairs': pairs, 'synthetic_cases': cases}
    (args.output / 'comparison.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
    (args.output / 'synthetic_cases.json').write_text(json.dumps(cases, ensure_ascii=False, indent=2) + '\n')
    with (args.output / 'question_changes.csv').open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['question_id', 'question', 'initial_source', 'react_source', 'text_changed',
                         'domain_changed', 'initial_static_errors', 'react_static_errors', 'react_api_calls'])
        for pair in pairs:
            writer.writerow([pair['question_id'], pair['question'], source(pair['predictions']['qwen_initial']),
                source(pair['predictions']['flash_react']), not pair['comparisons']['qwen_initial']['text_equal'],
                pair['comparisons']['qwen_initial']['domain_changed'], '; '.join(pair['checks']['qwen_initial']['errors']),
                '; '.join(pair['checks']['flash_react']['errors']), pair['react_api_calls']])
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
