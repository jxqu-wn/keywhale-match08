import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text2query.shared.curation import load_overlays, hours_cte
from text2query.shared.education import education_query_mode
from text2query.shared.context import Context
from text2query.shared.generation import validate
from text2query.shared.generation import write_offline_request
from text2query.shared.prompts import repair_feedback
from text2query.shared.curl_batch import build
from text2query.shared.paths import ROOT, RAW
from text2query.shared.data import schema_catalog, ddl_columns, references
from text2query.shared.validation import sqlite_connection


class ImprovementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = Context(True)
        cls.predictions = {r['question_id']:r for r in json.loads((ROOT/'artifacts/runs/prediction/predict-ee19c0abd953/predictions.json').read_text())}
        cls.questions = {r['question_id']:r['question'] for r in json.loads((RAW/'QA_test.json').read_text())}

    def test_all_five_historical_structure_failures_are_rejected(self):
        for qid in (4976,5029,5686,1825,2399):
            with self.subTest(qid=qid):
                prediction = {k:v for k,v in self.predictions[qid].items() if k!='question_id'}
                self.assertTrue(validate(prediction,self.context.catalog))
                self.assertFalse(validate(prediction,self.context.catalog,structural=False))

    def test_valid_es_nested_arrays_and_real_fields_pass(self):
        dsl = {'query':{'bool':{'filter':[{'geo_distance':{'distance':'8km','location':{'lat':31.2304,'lon':121.4737}}},{'range':{'rating':{'gte':4.6}}}]}},
               'sort':[{'annual_outpatient':'desc'}],'aggs':{'beds':{'sum':{'field':'bed_count'}}}}
        self.assertEqual(validate({'db':'Elasticsearch','query':json.dumps(dsl)},self.context.catalog),[])
        dsl['aggs']['beds']['sum']['field']='missing_beds'
        self.assertTrue(any('missing_beds' in e for e in validate({'db':'Elasticsearch','query':json.dumps(dsl)},self.context.catalog)))

    def test_missing_domain_facts_are_now_present(self):
        expected = {3576:{51,83,1248,1249},4682:{188,189},442:{1624,1625},4207:{150,159,163},2923:{1757}}
        for qid, ids in expected.items():
            ctx = self.context.build(self.questions[qid])
            self.assertTrue(ids <= {k['knowledge_id'] for k in ctx['knowledge']})
            if qid==4682:
                self.assertTrue(all(k['scope']['databases']==['mental_health_survey'] for k in ctx['knowledge']))
            if qid==2923:
                item=next(k for k in ctx['knowledge'] if k['knowledge_id']==1757)
                coordinates=[v['decimal_degrees'] for v in item['derived_constants']['dms_to_decimal']]
                self.assertAlmostEqual(coordinates[2],24.4666666667)

    def test_cross_source_dictionary_and_code_overrides(self):
        for qid in (2570,2646):
            ctx = self.context.build(self.questions[qid])
            self.assertEqual(ctx['routing']['sources'],['POSTGRESQL'])
            self.assertFalse(ctx['dictionary'])
            self.assertTrue(any(r['rule_id']=='vessel_type_source_specific_codes' for r in ctx['schema_adjudications']))
        mysql = {'db':'MYSQL','query':'SELECT * FROM transportation.vessel WHERE vessel_type=30'}
        pg = {'db':'POSTGRESQL','query':'SELECT * FROM vessel_ais_data WHERE vessel_type=30'}
        self.assertEqual(validate(mysql,self.context.catalog,'查询所有货船'),[])
        self.assertTrue(validate(pg,self.context.catalog,'查询所有货船'))
        self.assertEqual(validate(pg,self.context.catalog,'查询类型30的所有货船'),[])

    def test_single_line_administrative_values_are_recalled(self):
        for qid in (649,846):
            ctx = self.context.build(self.questions[qid])
            self.assertTrue(any('632801' in row and '格尔木市' in row for d in ctx['dictionary'] for row in d['rows']))

    def test_date_key_is_not_a_database_year(self):
        for question in (self.questions[1737], '查询起始日期为2024-01-01的学期', '查询起始日期为2024/01/01的学期'):
            self.assertEqual(education_query_mode(question)['mode'],'merged_latest')
        self.assertEqual(education_query_mode('查询2024年数据库中起始日期键大于20250101的学期')['years'],['2024'])
        ctx = self.context.build(self.questions[1737])
        self.assertEqual(ctx['education_merge']['mode'],'merged_latest')

    def test_quoted_and_inline_schema_identifiers(self):
        columns = ddl_columns('CREATE TABLE words(wid INTEGER PRIMARY KEY, "Disney revenue[NI 1]" REAL, amount DECIMAL(10,2))')
        self.assertEqual([c['name'] for c in columns],['wid','Disney revenue[NI 1]','amount'])
        catalog,_ = schema_catalog()
        words = next(t for t in catalog if t.get('database')=='language_corpus' and t['table']=='words')
        self.assertIn('wid',[c['name'] for c in words['columns']])
        self.assertIn('disney.voice-actors', references('SELECT * FROM disney."voice-actors"'))

    def test_training_overlays_compile_without_using_validation_targets(self):
        dev = json.loads((ROOT/'data/processed/dev.json').read_text())
        self.assertFalse({r['question_id'] for r in dev} & {r['question_id'] for r in self.context.examples})
        baseline = json.loads((ROOT/'artifacts/runs/regression/rag-improvements-20261009/baseline.json').read_text())
        self.assertEqual(hashlib.sha256((ROOT/'data/processed/dev.json').read_bytes()).hexdigest(),baseline['dev_sha256'])
        for patch in load_overlays()['example_patches']:
            self.assertEqual(validate(patch['replacement'],self.context.catalog,patch['question']),[],patch['question_id'])

    def test_hours_parse_minutes_midnight_unknown_and_full_day(self):
        sql = hours_cte()+' SELECT business_id,hours FROM normalized_hours ORDER BY business_id'
        db = sqlite_connection(self.context.catalog,sql)
        try:
            db.executemany('INSERT INTO public_review_platform.Business_Hours (business_id,day_id,opening_time,closing_time) VALUES (?,1,?,?)',
                           [(1,'8AM','9PM'),(2,'10PM','2AM'),(3,'12AM','12AM'),(4,'8:30AM','9:15PM'),(5,None,'9PM'),(6,'08:30','21:15')])
            self.assertEqual(db.execute(sql).fetchall(),[(1,13.0),(2,4.0),(3,24.0),(4,12.75),(5,None),(6,12.75)])
        finally:
            db.close()

    def test_corrected_ratio_counts_businesses_with_many_categories(self):
        patch = next(p for p in load_overlays()['example_patches'] if p['question_id']==4298)
        sql = patch['replacement']['query']; db = sqlite_connection(self.context.catalog,sql)
        try:
            db.executescript("INSERT INTO public_review_platform.Business(business_id) VALUES(1),(2); INSERT INTO public_review_platform.Categories VALUES(1,'Pets'),(2,'Food'); INSERT INTO public_review_platform.Business_Categories VALUES(1,1),(1,2),(2,2);")
            self.assertEqual(db.execute(sql).fetchone(),(50.0,))
        finally:
            db.close()

    def test_exists_entity_count_is_not_mistaken_for_join_duplication(self):
        sql = "SELECT COUNT(b.business_id) FROM public_review_platform.Business b WHERE b.city='Phoenix' AND EXISTS (SELECT 1 FROM public_review_platform.Reviews r WHERE r.business_id=b.business_id AND r.review_length='Long')"
        self.assertEqual(validate({'db':'SQLite','query':sql},self.context.catalog,'凤凰城有多少商家有长评论'),[])

    def test_offline_response_is_bound_to_its_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); request=root/'1.request.1.json';response=root/'1.response.1.json'
            old={'messages':['old']};new={'messages':['new']}
            request.write_text(json.dumps(old));response.write_text('{"choices":[{}]}')
            write_offline_request(request,response,old)
            self.assertTrue(response.exists())
            write_offline_request(request,response,new)
            self.assertFalse(response.exists())
            self.assertEqual(json.loads(request.read_text()),new)
            archived=list((root/'stale_responses').glob('*/1.request.1.json'))
            self.assertEqual(json.loads(archived[0].read_text()),old)

    def test_curl_queues_only_the_current_repair_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'1.request.0.json').write_text('{}');(root/'1.response.0.json').write_text('{"error":"failed"}')
            (root/'1.request.1.json').write_text('{}')
            config=build(root,'http://example.invalid/v1').read_text()
            self.assertNotIn('@'+str(root/'1.request.0.json'),config)
            self.assertIn('@'+str(root/'1.request.1.json'),config)

    def test_live_sample_errors_are_rejected_and_correct_relations_pass(self):
        run = ROOT/'artifacts/runs/regression/test20-20261009/predict-94f84ad400f2'
        for qid in (3674,4123,4682,5686):
            row = json.loads((run/f'{qid}.json').read_text())
            self.assertTrue(validate(row['prediction'],self.context.catalog,row['question']),qid)
        query = "SELECT AVG(CAST(a.AnswerText AS REAL)) FROM mental_health_survey.Answer a WHERE a.QuestionID=1 AND EXISTS (SELECT 1 FROM mental_health_survey.Answer c WHERE c.UserID=a.UserID AND c.SurveyID=a.SurveyID AND c.QuestionID=3 AND c.AnswerText='United States')"
        self.assertEqual(validate({'db':'SQLite','query':query},self.context.catalog,self.questions[4682]),[])
        query = "SELECT AVG(role_count) FROM (SELECT w.id,COUNT(DISTINCT p.character_id) AS role_count FROM shakespeare.works w LEFT JOIN shakespeare.chapters c ON c.work_id=w.id LEFT JOIN shakespeare.paragraphs p ON p.chapter_id=c.id GROUP BY w.id)"
        self.assertEqual(validate({'db':'SQLite','query':query},self.context.catalog,self.questions[3674]),[])

    def test_automotive_mapping_and_latest_position_template_are_recalled(self):
        context = self.context.build(self.questions[4123])
        self.assertIn(149,{row['knowledge_id'] for row in context['knowledge']})
        patch = next(p for p in load_overlays()['example_patches'] if p['question_id']==2640)
        self.assertIn('FROM latest',patch['replacement']['query'])

    def test_json_repair_includes_specific_failure(self):
        message=repair_feedback(['Expecting delimiter at column 363'])
        self.assertIn('column 363',message)
        self.assertIn('aggs',message)

    def test_author_name_is_not_an_explicit_database(self):
        context=self.context.build('Penguin Classics出版了William Shakespeare的多少本书？')
        self.assertEqual(context['routing']['databases'],['books'])
        self.assertTrue(context['examples'])
        self.assertTrue(all(table.get('database')=='books' for table in context['schema']))
        context=self.context.build('查询数据库shakespeare中的作品数')
        self.assertEqual(context['routing']['databases'],['shakespeare'])

    def test_captain_is_a_mysql_attribute_for_ais_federation(self):
        context=self.context.build(self.questions[2990])
        self.assertEqual(set(context['routing']['sources']),{'MYSQL','POSTGRESQL'})
        self.assertTrue(any(t.get('database')=='transportation' and t['table']=='vessel'
                            for t in context['schema']))


if __name__=='__main__':
    unittest.main()
