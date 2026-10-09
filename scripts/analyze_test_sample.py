"""Review the fixed 20 live model outputs, with reproducible synthetic evidence."""
import argparse
import copy
import csv
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from text2query.shared.paths import ROOT
from text2query.shared.fingerprints import implementation_hash
from text2query.shared.generation import parse_output, validate
from text2query.shared.education import expand_prediction, model_query_errors
from text2query.shared.validation import sqlite_connection

# These are review findings, not hidden test gold labels or production overrides.
REVIEWS = {
    442: ('conditional_risk', 'PB遵守知识1624/1625；财报使用全库最大日期，可能漏掉最新报表较早的公司。',
          '按每家公司取最新财报；该风险是否影响真实结果需要数据确认。'),
    1737: ('no_obvious_issue', '日期键不再选2024库；最终SQL合并两年学期并按业务键去重、2025优先。', ''),
    1825: ('ambiguous_business_rule', 'FULL OUTER JOIN已改成MySQL可用的UNION；按2024数量优先排序，再比较2025数量。',
           '若课程数指两年合计，应在外层按两年数量之和排序；题目未明确排序年份，不计作确定错误。'),
    2140: ('no_obvious_issue', '外半径5公里、排除内半径2公里及24小时急诊条件均保留。', ''),
    2399: ('format_error', '首轮aggs位于DSL的query内部；后三轮JSON缺少闭合括号，四次均未通过。',
           '修复提示给出精确JSON错误位置和上次非法内容，并提供正确DSL层级骨架；恢复医院列表及annual_outpatient排序。'),
    2570: ('no_obvious_issue', 'POSTGRESQL货船编码改为70，指定日期内SUM(draught)。', ''),
    2646: ('semantic_error', '先筛选15公里内的历史记录，再DISTINCT ON取最新，不能代表每船真正最新位置。',
           '在独立CTE从全部AIS记录先取每船最新行，再筛油轮80和15公里距离。'),
    2938: ('no_obvious_issue', 'MYSQL查所属公司MMSI，POSTGRESQL用IN(result)查完整AIS记录；时段和10000米半径保留。', ''),
    3033: ('no_obvious_issue', '第一阶段PG保留显式类型70与5000米半径，第二阶段MYSQL按最大载重取10条。', ''),
    3576: ('no_obvious_issue', 'BMI按赛题weight/(height*height)，EXISTS避免全明星赛季重复计权。height的原DDL类型为REAL。', ''),
    3674: ('conditional_logic_defect', '全局COUNT(DISTINCT character_id)/作品数，会把跨作品复用的角色只算一次。',
           '按作品先COUNT(DISTINCT character_id)，再平均；保留零角色作品。是否影响真实分数取决于角色复用情况。'),
    3915: ('no_obvious_issue', '按发行商ID分组、COUNT(DISTINCT game_id)，不合并同名发行商。', ''),
    4123: ('semantic_error', '汽车类别写成Cars；知识149与训练4257对应Automotive，当前召回均未提供这一映射。另添加了题目未要求的两位小数取整。',
           '补召回知识149/汽车类训练示例，使用Automotive；精度保持题目口径。'),
    4207: ('no_obvious_issue', '使用营业时间解析CTE并按小时比较；分钟、午夜、全天24小时与缺失值构造检查通过。', ''),
    4424: ('no_obvious_issue', 'COUNT(DISTINCT business_id)避免同一商家多条长评论重复计数。', ''),
    4682: ('semantic_error', '年龄AnswerText被拿去匹配国家AnswerText=United States，没有关联同一受访者。',
           '年龄与国家答案按UserID和SurveyID关联，分别筛QuestionID=1和3，再对年龄求平均。'),
    4976: ('no_obvious_issue', 'transaction表正确引用，8月使用半开日期区间，按客户分组取交易次数最多者。', ''),
    5029: ('no_obvious_issue', '首轮ContainerType错用T2；自动修复为rootbeer的T1，并以MAX/MIN利润筛选。', ''),
    5686: ('semantic_error', '只查Teams.PIM，未查斯坦利杯决赛TeamsSC.PIM；字段存在但赛事阶段错误。',
           '按year/tmID关联TeamsSC，按TeamsSC.PIM=49筛选，必要时限制题目给出的年份。'),
    5921: ('no_obvious_issue', '首轮月份LIKE缺少末尾通配符，自动修复为8/%/2013%。', ''),
}
LABELS = {'no_obvious_issue': '未见明显问题', 'format_error': '格式错误',
          'semantic_error': '语义错误', 'conditional_logic_defect': '条件触发的逻辑缺陷',
          'conditional_risk': '条件性风险', 'ambiguous_business_rule': '口径待明确'}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def execute_case(case, prediction, catalog):
    database = sqlite_connection(catalog, prediction['query'])
    try:
        for table, rows in case['tables'].items():
            namespace, name = table.split('.', 1)
            for row in rows:
                columns = ','.join('"' + field + '"' for field in row)
                marks = ','.join('?' for _ in row)
                database.execute(f'INSERT INTO "{namespace}"."{name}" ({columns}) VALUES ({marks})',
                                 list(row.values()))
        actual = [list(row) for row in database.execute(prediction['query']).fetchall()]
        error = None
    except sqlite3.Error as exc:
        actual, error = None, str(exc)
    finally:
        database.close()
    return {'question_id': case['question_id'], 'description': case['description'],
            'original_model_query': prediction['query'], 'tables': case['tables'],
            'expected': case['expected'], 'actual': actual, 'error': error,
            'passed': error is None and actual == case['expected'],
            'synthetic_data_only': True, 'engine': 'SQLite with original schema DDL'}


def latest_position_case(query):
    # Translate DISTINCT ON into a window over exactly the same FROM/WHERE.
    # Positions are one-dimensional metre values solely to test operation order.
    match = re.fullmatch(r'(WITH target AS \(.*?\)) SELECT DISTINCT ON \(v\.mmsi\) v\.\* (FROM .*?) ORDER BY v\.mmsi, v\.timestamp DESC',
                         ' '.join(query.split()), re.I)
    if not match:
        raise ValueError('Unexpected 2646 query shape; review the simulation before reuse')
    simulated = (match[1] + ', ranked AS (SELECT v.*, ROW_NUMBER() OVER '
                 '(PARTITION BY v.mmsi ORDER BY v.timestamp DESC) AS rn ' + match[2] + ') '
                 'SELECT mmsi, timestamp FROM ranked WHERE rn=1 ORDER BY mmsi')
    simulated = simulated.replace('::geography', '')
    rows = [(10, 8765432, '2024-06-01 10:00:00', 70, 0),
            (20, 1234567, '2024-06-01 08:00:00', 80, 5000),
            (20, 1234567, '2024-06-01 10:00:00', 80, 30000)]
    database = sqlite3.connect(':memory:')
    try:
        database.execute('CREATE TABLE vessel_ais_data(mmsi INTEGER, imo INTEGER, timestamp TEXT, vessel_type INTEGER, geom REAL)')
        database.executemany('INSERT INTO vessel_ais_data VALUES(?,?,?,?,?)', rows)
        database.create_function('ST_DWithin', 3, lambda a, b, distance: abs(a-b) <= distance)
        actual = [list(row) for row in database.execute(simulated)]
    finally:
        database.close()
    assert actual == [[20, '2024-06-01 08:00:00']]
    return {'question_id': 2646, 'description': '油轮旧位置5公里、最新位置30公里，15公里查询应排除该船，当前逻辑返回旧记录。',
            'original_model_query': query, 'simulation_query': simulated,
            'columns': ['mmsi', 'imo', 'timestamp', 'vessel_type', 'geom_metres'],
            'rows': rows, 'expected': [], 'actual': actual, 'passed': False,
            'synthetic_data_only': True,
            'engine': 'SQLite window simulation; NOT native PostgreSQL/PostGIS execution'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--report', type=Path,
                        default=ROOT / 'docs/reports/测试集20条推理检查.md')
    args = parser.parse_args()
    directory = args.directory.resolve()
    sample = json.loads((directory / 'sample.json').read_text())
    catalog = json.loads((ROOT / 'data/processed/catalog.json').read_text())
    assert {row['question_id'] for row in sample} == set(REVIEWS)
    config = json.loads((directory / 'config.json').read_text())
    current_hash = implementation_hash()
    assert current_hash == config['implementation_hash'], 'Production code changed after inference'
    records, outputs = [], []
    for item in sample:
        qid = item['question_id']
        record = json.loads((directory / f'{qid}.json').read_text())
        assert record.get('prediction_source') != 'curated_question_correction'
        attempts = []
        for number, attempt in enumerate(record['attempts']):
            response = json.loads((directory / f'{qid}.response.{number}.json').read_text())
            assert response['model'] == config['model']
            try:
                raw = parse_output(attempt['content'])
                prediction = expand_prediction(raw, record['context'], catalog)
                errors = model_query_errors(raw, record['context']) + validate(
                    prediction, catalog, item['question'], record['context']['business_rules'])
            except (ValueError, KeyError) as exc:
                prediction, errors = None, [str(exc)]
            attempts.append({'attempt': number, 'content': attempt['content'],
                             'prediction': prediction, 'errors': errors,
                             'finish_reason': attempt['finish_reason'], 'usage': attempt['usage']})
        status, note, recommendation = REVIEWS[qid]
        outputs.append({'question_id': qid, 'question': item['question'], 'attempts': attempts,
                        'final_prediction': record['prediction'] if not record['errors'] else None,
                        'final_errors': record['errors']})
        records.append({'question_id': qid, 'question': item['question'],
                        'source': record['prediction'].get('db', '跨源'),
                        'attempt_count': len(attempts), 'preflight_passed': not record['errors'],
                        'review_status': status, 'review_label': LABELS[status],
                        'finding': note, 'recommendation': recommendation,
                        'knowledge_ids': [k['knowledge_id'] for k in record['context']['knowledge']],
                        'example_ids': [e['question_id'] for e in record['context']['examples']]})
    by_id = {item['question_id']: item for item in outputs}
    old_cases = json.loads((ROOT / 'artifacts/analysis/submission-210e08596136/counterexamples.json').read_text())
    cases = [copy.deepcopy(case) for case in old_cases if case['question_id'] in
             {3576, 3674, 3915, 4123, 4207, 4424, 4682, 5921}]
    shared = copy.deepcopy(next(case for case in cases if case['question_id'] == 3674))
    for row in shared['tables']['shakespeare.paragraphs']:
        row['character_id'] = 1
    shared.update(description='同一角色ID出现在两部作品，每部各1个角色，平均应为1，当前全局去重得到0.5。',
                  expected=[[1.0]])
    cases.append(shared)
    cases.append({'question_id': 5686,
                  'description': '常规Teams的1918罚时49，决赛TeamsSC的1923罚时49，应返回1923。',
                  'tables': {'hockey.Teams': [
                      {'year': 1918, 'tmID': 'MON', 'name': 'Montreal Canadiens', 'PIM': '49'},
                      {'year': 1923, 'tmID': 'MON', 'name': 'Montreal Canadiens', 'PIM': '200'},
                      {'year': 1924, 'tmID': 'MON', 'name': 'Montreal Canadiens', 'PIM': '300'}],
                      'hockey.TeamsSC': [{'year': 1918, 'tmID': 'MON', 'PIM': '10'},
                                        {'year': 1923, 'tmID': 'MON', 'PIM': '49'},
                                        {'year': 1924, 'tmID': 'MON', 'PIM': '24'}]},
                  'expected': [[1923]]})
    hours = [('8:30AM', '9:15PM'), ('11PM', '12PM'), ('10AM', '10AM'),
             ('08:30', '21:15'), (None, '9PM'), ('8AM', '8PM'), ('11AM', '1PM')]
    cases.append({'question_id': 4207,
                  'description': '真实模型SQL的分钟、跨午夜、24小时、24小时制、缺失值及严格超过12小时边界。',
                  'tables': {'public_review_platform.Business': [{'business_id': i} for i in range(1, 8)],
                             'public_review_platform.Days': [{'day_id': 1, 'day_of_week': 'Sunday'}],
                             'public_review_platform.Business_Hours': [
                                 {'business_id': i, 'day_id': 1, 'opening_time': opening, 'closing_time': closing}
                                 for i, (opening, closing) in enumerate(hours, 1)]},
                  'expected': [[1], [2], [3], [4]]})
    executions = [execute_case(case, by_id[case['question_id']]['final_prediction'], catalog) for case in cases]
    executions.append(latest_position_case(by_id[2646]['final_prediction']['query']))
    assert len(executions) == 12
    assert {case['question_id'] for case in executions if not case['passed']} == {2646, 3674, 4123, 4682, 5686}
    assert all(case.get('error') is None for case in executions)
    metrics = json.loads((directory / 'metrics.json').read_text())
    summary = {'question_count': len(sample), 'live_model_call_count': sum(len(o['attempts']) for o in outputs),
               'first_attempt_preflight_passed': sum(not o['attempts'][0]['errors'] for o in outputs),
               'final_preflight_passed': metrics['valid_count'],
               'review_category_counts': dict(Counter(r['review_status'] for r in records)),
               'synthetic_case_count': len(executions),
               'synthetic_case_passed': sum(case['passed'] for case in executions),
               'native_sqlite_synthetic_cases': len(executions)-1,
               'postgresql_order_simulation_cases': 1,
               'no_official_test_gold_or_live_databases': True,
               'official_execution_accuracy': None,
               'production_code_matches_inference_fingerprint': True,
               'model': config['model'], 'base_url': config['base_url']}
    write_json(directory / 'model_outputs.json', outputs)
    write_json(directory / 'synthetic_checks.json', executions)
    write_json(directory / 'review.json', {'summary': summary, 'questions': records})
    with (directory / 'review.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        fields = ['question_id', 'question', 'source', 'attempt_count', 'preflight_passed',
                  'review_label', 'finding', 'recommendation']
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(records)
    relative = Path('../../') / directory.relative_to(ROOT)
    lines = ['# 测试集20条真实推理检查', '',
             f'模型：`{config["model"]}`；接口：`{config["base_url"]}`。', '',
             '固定选择测试集中的20道诊断题，覆盖四种单源和跨源查询，重点复查历史缺陷。不是随机代表性样本；未提供测试金标及真实业务数据库，不能计算官方准确率。', '',
             f'20题均实际调用LLM，没有命中人工直接答案规则；共{summary["live_model_call_count"]}次模型调用。首轮17/20通过当前校验，自动修复后19/20（95%）通过，1题失败。95%只是检查通过率。', '',
             '人工发现4条明确语义错误、1条格式错误和1条在角色跨作品复用时触发的逻辑缺陷。另外2条属于条件性风险或排序口径待明确，12条暂未发现明显问题；这些数量不是正确题数或比赛分数。', '',
             f'[20条完整输出与各次校验]({relative}/model_outputs.json) · '
             f'[逐题审查CSV]({relative}/review.csv) · '
             f'[构造执行证据]({relative}/synthetic_checks.json) · '
             f'[19条通过校验的查询]({relative}/predictions.json)', '',
             '## 逐题结论', '',
             '| 题号 | 数据源 | 调用次数 | 校验 | 人工检查 | 发现 |',
             '|---|---|---:|---|---|---|']
    for row in records:
        lines.append(f'| {row["question_id"]} | {row["source"]} | {row["attempt_count"]} | '
                     f'{"通过" if row["preflight_passed"] else "失败"} | {row["review_label"]} | {row["finding"]} |')
    lines += ['', '## 重点问题及原因', '',
              '1. **2399：ES结构/JSON反复失败。** 首轮aggs落在DSL query内部，后续三次缺少闭合括号。三次修复输出相同，均以stop结束，未发生token截断。JSON异常分支只追加“严格输出规定JSON”，未把本次非法输出及具体错误位置反馈给模型，因此后续修复信息不足。首轮size=0不返回根级医院列表，top_hits还按_score排序，即使只修括号也须核对医院列表与日均门诊量排序。', '',
              '2. **2646：最新记录的筛选顺序错误。** 油轮80编码已经修正，但WHERE先选15公里内的历史位置，DISTINCT ON再取其中最新一条。首个召回训练2640使用同一错误模板；召回同时存在2671/2645/2642的正确先取最新CTE。修正数据尚未覆盖2640，该例说明错误近邻可以压过正确提示。', '',
              '3. **4123：Cars替代Automotive。** 分子已按商家去重，分母已用全部Business；但知识149明确汽车类别为Automotive，训练4257也采用该值。本题召回没有149或4257，模型自行翻译为Cars。领域召回正确，不代表具体类别映射完整。另ROUND(...,2)是题目未要求的精度变化。', '',
              '4. **4682：年龄与国家未按受访者关联。** 知识188/189已提供正确QuestionID，但SQL把年龄答案与United States国家文本直接做IN比较。应按UserID、SurveyID关联两类答案；该问题属于关系逻辑错误，补齐知识还不够。', '',
              '5. **5686：同名PIM用了错误赛事表。** Teams和TeamsSC都有PIM，模型只查Teams，丢掉“斯坦利杯决赛”范围。召回5684/5689本来正确关联TeamsSC，因此这是输出未保持题目限定，不是Schema未提供。', '',
              '6. **3674：角色去重层级仍错误。** 模型已避免按段落计数，但改为全局角色去重除以作品数；角色ID复用于多部作品时漏计。应按作品去重角色再平均，纳入零角色作品。普通不共享角色的构造数据通过，新增共享角色数据失败，证明单一反例通过并不足够。', '',
              '## 构造数据验证', '',
              f'共{len(executions)}组，{sum(c["passed"] for c in executions)}组通过、{sum(not c["passed"] for c in executions)}组出现预期与实际差异。11组直接运行新模型SQLite查询及原DDL；2646采用保留FROM/WHERE顺序的SQLite窗口等价模拟，以一维米坐标隔离筛选顺序问题，不是原生PG/PostGIS空间执行。所有数据均人为构造，不是实际测试库数据。', '',
              '| 题号 | 场景 | 期望 | 实际 |', '|---|---|---|---|']
    for case in executions:
        lines.append(f'| {case["question_id"]} | {case["description"]} | '
                     f'`{json.dumps(case["expected"], ensure_ascii=False)}` | '
                     f'`{json.dumps(case["actual"], ensure_ascii=False)}` |')
    lines += ['', '## 其他口径与风险', '',
              '- **442：** PB公式已符合赛题，最新价使用current_price；以全库最大report_date筛财报，如果各公司最新报告日期不同会漏公司。没有真实财报数据，暂列条件性风险。',
              '- **1825：** 不再使用MySQL不支持的FULL JOIN，但排序为2024课程数优先、2025课程数次之。若按两年合计，(10,0)会错误排在(8,8)之前；题目未指定按哪年或合计，需统一口径，不能据此断言官方判错。', '',
              '## 已体现的改进', '',
              '3576的BMI单位及对象去重、3915发行商不同游戏数、4424商家去重、4207营业时间解析、2570货船70、2646油轮80、1737日期键与年度库区分均已体现。5029的错误列、5921的日期通配符被校验发现并在第二次调用修复；4976引用transaction正确。新模型仍会生成未覆盖的关系与粒度问题。', '',
              '## 建议处理顺序', '',
              '先增强2399的JSON错误反馈与ES层级修复；再修正2640等“先筛历史、后取最新”的训练模板；补齐149类别映射及年龄/国家、决赛PIM的定向关联检查；修正角色按作品聚合的示例。完成这些通用改动后固定重跑本20题，再进行200题验证。不要把本20题答案直接写入RAG。', '',
              '本次只新增运行与审查脚本及证据，没有改训练覆盖层、提示词或生产校验代码。原400条提交result.json未覆盖；20题诊断结果不会导出为完整提交。', '',
              '## 20题实际模型输出', '',
              '下面是本次生成结果，供审阅，不作为修正答案。ES2399保留最后原始非法JSON，不采用记录中残留的首轮解析对象。']
    for output in outputs:
        lines += ['', f'### {output["question_id"]}', '', output['question'], '']
        prediction = output['final_prediction']
        if prediction is None:
            lines += ['最终校验失败：' + '; '.join(output['final_errors']), '', '```text',
                      output['attempts'][-1]['content'], '```']
        elif 'query' in prediction:
            if prediction['db'] == 'Elasticsearch':
                lines += ['```json', json.dumps(json.loads(prediction['query']), ensure_ascii=False, indent=2), '```']
            else:
                lines += ['```sql', prediction['query'], '```']
        else:
            for phase in (1, 2):
                lines += [f'阶段{phase}：{prediction[f"db{phase}"]}', '', '```sql', prediction[f'query{phase}'], '```', '']
    args.report.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(args.report)


if __name__ == '__main__':
    main()
