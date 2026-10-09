import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text2query.model_pipeline import parse_output, validate, Context
from text2query.pipeline import schema_catalog
from text2query.query_rules import applicable_business_rules

class ModelTests(unittest.TestCase):
    def test_parse_wrappers(self):
        self.assertEqual(parse_output('<think>hidden</think>\n```json\n{"db":"MYSQL","query":"SELECT 1"}\n```')['db'],'MYSQL')
        with self.assertRaises(ValueError):
            parse_output('[]')

    def test_invalid_and_cross_source(self):
        catalog,_ = schema_catalog()
        self.assertTrue(validate({'db':'MYSQL','query':'DELETE FROM transportation.vessel'},catalog))
        self.assertTrue(validate({'db':'MYSQL','query':'SELECT * FROM missing.table'},catalog))
        obj={'db1':'MYSQL','query1':'SELECT mmsi FROM transportation.vessel','db2':'POSTGRESQL','query2':'SELECT * FROM vessel_ais_data WHERE mmsi IN (result)'}
        self.assertEqual(validate(obj,catalog),[])
        obj['query2']='SELECT * FROM vessel_ais_data'
        self.assertTrue(validate(obj,catalog))

    def test_es_requires_json_object(self):
        self.assertTrue(validate({'db':'Elasticsearch','query':'[]'},[]))
        self.assertTrue(validate({'db':'Elasticsearch','query':'{"query":{"match_all":{},"aggs":{}}}'},[]))
        self.assertEqual(validate({'db':'Elasticsearch','query':'{"query":{"match_all":{}}}'},[]),[])

    def test_native_dsl_serialization(self):
        obj = parse_output('{"db":"Elasticsearch","query":{"query":{"match_all":{}}}}')
        self.assertIsInstance(obj['query'],str)
        self.assertEqual(validate(obj,[]),[])

    def test_same_source_and_fake_result_are_rejected(self):
        catalog,_ = schema_catalog()
        obj = {'db1':'MYSQL','query1':'SELECT mmsi FROM transportation.vessel',
               'db2':'MYSQL','query2':'SELECT mmsi FROM transportation.vessel WHERE mmsi IN (result)'}
        self.assertTrue(any('同一数据源' in e for e in validate(obj,catalog)))
        obj['db2']='POSTGRESQL'
        obj['query2']="SELECT * FROM vessel_ais_data WHERE destination = 'result'"
        self.assertTrue(any('缺少result' in e for e in validate(obj,catalog)))

    def test_total_sales_ranking_checks_the_current_question(self):
        catalog,_ = schema_catalog()
        obj={'db':'SQLite','query':'SELECT title_id FROM book_publishing_company.sales ORDER BY qty ASC LIMIT 1'}
        question='销量最高的商店中订购数量最少的书名是什么？'
        self.assertTrue(any('单笔' in e for e in validate(obj,catalog,question)))
        # A genuinely single-order question should not trigger the aggregation rule.
        self.assertEqual(validate(obj,catalog,'按单笔订购数量升序取一条'),[])
        obj['query']='SELECT title_id FROM book_publishing_company.sales GROUP BY title_id ORDER BY SUM(qty) ASC LIMIT 1'
        self.assertEqual(validate(obj,catalog,question),[])

    def test_conflicting_year_candidates_keep_their_schemas(self):
        context = Context(True)
        ctx = context.build('统计教师中电话不为空的数量，并返回数量。')
        self.assertEqual([r['rule_id'] for r in ctx['business_rules']],['education_teacher_latest_union'])
        schemas = {(t.get('database'),t['table']):t for t in ctx['schema']}
        for year in ('2024','2025'):
            table = schemas[('education_'+year,'teacher')]
            self.assertIn('teacher_no',[c['name'] for c in table['columns']])
        group = next(g for g in ctx['ambiguous_examples'] if g['group_id']=='ambiguous-1669')
        self.assertEqual({r['question_id'] for r in group['candidate_annotations']},{1669,1682,1732})
        self.assertFalse({1669,1682,1732} & {r['question_id'] for r in context.examples})
        self.assertTrue(all(r['schema_refs'] for r in group['candidate_annotations']))
        self.assertTrue(all(r['schema_refs'] for r in ctx['examples']))

    def test_explicit_year_overrides_default_merge_rule(self):
        context = Context(True)
        for question in ('统计2024年教师电话不为空的数量','统计2025年教师数量','统计去年教师数量'):
            self.assertEqual(applicable_business_rules(question,context.business_rules),[])
        obj = {'db':'MYSQL','query':"SELECT COUNT(*) FROM `education_2024`.`teacher` WHERE phone IS NOT NULL AND phone <> ''"}
        question = '统计教师中电话不为空的数量'
        self.assertTrue(any('同时访问' in e for e in validate(obj,context.catalog,question,context.business_rules)))
        self.assertEqual(validate(obj,context.catalog,'统计2024年教师电话不为空的数量',context.business_rules),[])

    def test_teacher_count_checks_same_year_duplicates(self):
        context = Context(True)
        question = '统计教师中电话不为空的数量'
        sql = '''SELECT COUNT(*) FROM (
            SELECT teacher_no,phone FROM education_2025.teacher
            UNION ALL SELECT teacher_no,phone FROM education_2024.teacher t
            WHERE NOT EXISTS (SELECT 1 FROM education_2025.teacher newer WHERE newer.teacher_no=t.teacher_no)
        ) teachers WHERE phone IS NOT NULL AND phone<>'' '''
        self.assertTrue(any('同年重复' in e for e in validate({'db':'MYSQL','query':sql},context.catalog,question,context.business_rules)))
        sql = sql.replace('COUNT(*)','COUNT(DISTINCT teacher_no)')
        self.assertEqual(validate({'db':'MYSQL','query':sql},context.catalog,question,context.business_rules),[])

if __name__=='__main__':
    unittest.main()
