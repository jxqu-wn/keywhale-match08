import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from text2query.dsl_normalization import normalize_es, normalize_prediction
from text2query.model_pipeline import Context
from text2query.paths import RAW
from text2query.pipeline import load
from text2query.react_pipeline import run_question, summarize
from text2query.react_tools import QueryTools, pg_references


class ReactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = Context(False)
        cls.rows = {r['question_id']: r for r in load(RAW / 'QA_test.json')}

    def tools(self, qid):
        tools = QueryTools(self.context, self.context.build(self.rows[qid]['question']))
        tools.dispatch('search_examples', {})
        return tools

    def test_normalization_preserves_conditions_and_rejects_conflicts(self):
        raw = {'db': 'Elasticsearch', 'query': {'query': {'bool': {'filter': [{'range': {'rating': {'gte': 4.6}}}]},
            'aggs': {'beds': {'sum': {'field': 'bed_count'}}}}}, 'size': 0}
        original = copy.deepcopy(raw)
        fixed = json.loads(normalize_es(raw)['query'])
        self.assertEqual(fixed['query'], {'bool': raw['query']['query']['bool']})
        self.assertEqual(fixed['aggs'], raw['query']['query']['aggs'])
        self.assertEqual(raw, original)
        raw['query']['size'] = 2
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            normalize_es(raw)

    def test_federated_es_preserves_sql(self):
        raw = {'db1': 'MYSQL', 'query1': 'SELECT city_code FROM common.city', 'db2': 'Elasticsearch',
               'query2': {'query': {'terms': {'city_code': ['result']}, 'size': 20}}}
        fixed, transforms = normalize_prediction(raw)
        self.assertEqual(fixed['query1'], raw['query1'])
        self.assertEqual(json.loads(fixed['query2'])['size'], 20)
        self.assertEqual(transforms, ['es_dsl_level_normalization'])

    def test_pg_unknown_table_and_physical_field_with_valid_ctes(self):
        sql = "WITH latest AS (SELECT DISTINCT ON (mmsi) * FROM public.vessel_ais_data ORDER BY mmsi, timestamp DESC) SELECT a.* FROM latest a JOIN vessel_info v ON a.mmsi=v.mmsi"
        refs, errors = pg_references(sql, self.context.catalog)
        self.assertEqual(refs, {'public.vessel_ais_data'})
        self.assertTrue(any('vessel_info' in x for x in errors))
        self.assertEqual(pg_references('SELECT a.captain_name FROM vessel_ais_data a', self.context.catalog)[1], ['PG物理表字段不存在: a.captain_name'])
        valid = 'WITH latest(x) AS NOT MATERIALIZED (SELECT mmsi FROM "public"."vessel_ais_data") SELECT x FROM latest'
        self.assertEqual(pg_references(valid, self.context.catalog)[1], [])

    def test_finish_requires_successful_check_and_retrieved_evidence(self):
        tools = self.tools(2990)
        pred = {'db1': 'MYSQL', 'query1': "SELECT mmsi FROM transportation.vessel WHERE captain_name='李刚'",
                'db2': 'POSTGRESQL', 'query2': 'SELECT * FROM vessel_ais_data WHERE mmsi IN (result) ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint(113.5,22.2),4326)::geography LIMIT 1'}
        self.assertFalse(tools.dispatch('finish', {'prediction': pred})['ok'])
        self.assertFalse(tools.dispatch('check_query', {'prediction': pred})['ok'])
        self.assertTrue(tools.dispatch('get_schema', {'tables': ['transportation.vessel', 'public.vessel_ais_data']})['ok'])
        tools.dispatch('search_knowledge', {})
        self.assertTrue(tools.dispatch('check_query', {'prediction': pred})['ok'])
        changed = {**pred, 'query2': pred['query2'].replace('LIMIT 1', 'LIMIT 2')}
        self.assertFalse(tools.dispatch('finish', {'prediction': changed})['ok'])
        tools.dispatch('check_query', {'prediction': pred})
        self.assertTrue(tools.dispatch('finish', {'prediction': pred})['finished'])

    def test_pg_derived_alias_shadowing_does_not_invent_a_field_error(self):
        sql = 'WITH latest AS (SELECT a.*, ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY timestamp DESC) AS rn FROM vessel_ais_data a) SELECT a.rn FROM latest a'
        self.assertEqual(pg_references(sql, self.context.catalog)[1], [])

    def test_mandatory_knowledge_survives_short_search(self):
        tools = self.tools(4123)
        tools.dispatch('get_schema', {'tables':['public_review_platform.Business', 'public_review_platform.Business_Hours']})
        result = tools.dispatch('search_knowledge', {'query': '营业时间', 'limit': 1})
        self.assertIn(149, {x['knowledge_id'] for x in result['knowledge']})
        self.assertTrue(any('Automotive' in x['knowledge_text'] for x in result['knowledge']))

    def test_education_final_ctes_are_not_expanded_twice(self):
        tools = self.tools(1737)
        tools.dispatch('get_schema', {'tables': ['education_2024.semester', 'education_2025.semester']})
        tools.dispatch('search_knowledge', {})
        raw = {'db': 'MYSQL', 'query': 'SELECT semester_id, semester_code, semester_name FROM edu_semester WHERE start_date_key>20250101'}
        checked = tools.dispatch('check_query', {'prediction': raw})
        self.assertTrue(checked['ok'], checked['errors'])
        self.assertNotEqual(checked['prediction'], raw)
        final = tools.dispatch('finish', {'prediction': checked['prediction']})
        self.assertTrue(final['finished'])
        self.assertEqual(final['prediction'], checked['prediction'])

    def test_invalid_actions_and_conflicting_dsl_do_not_crash(self):
        tools = self.tools(2399)
        self.assertFalse(tools.dispatch('execute_sql', {})['ok'])
        self.assertFalse(tools.dispatch([], {})['ok'])
        self.assertFalse(tools.dispatch('get_schema', {'limit': 0})['ok'])
        bad = {'db': 'Elasticsearch', 'query': {'size': 2}, 'size': 3}
        self.assertFalse(tools.dispatch('check_query', {'prediction': bad})['ok'])

    def test_model_directed_repair_and_offline_resume(self):
        good = {'db': 'POSTGRESQL', 'query': 'SELECT * FROM vessel_ais_data WHERE vessel_type=70'}
        bad = {'db': 'POSTGRESQL', 'query': 'SELECT * FROM vessel_info WHERE vessel_type=70'}
        actions = [
            {'action': 'search_examples', 'args': {}},
            {'action': 'get_schema', 'args': {'tables': ['public.vessel_ais_data']}},
            {'action': 'search_knowledge', 'args': {}},
            {'action': 'check_query', 'args': {'prediction': bad}},
            {'action': 'check_query', 'args': {'prediction': good}},
            {'action': 'finish', 'args': {'prediction': good}},
        ]
        calls = []

        def fake(base, endpoint, payload):
            index = len(calls)
            calls.append(copy.deepcopy(payload))
            return {'choices': [{'message': {'content': json.dumps(actions[index])}, 'finish_reason': 'stop'}]}

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            row = {'question_id': 999, 'question': '查询所有货船'}
            finished = run_question(row, self.context, directory, 'mock', 'local', max_steps=6, call=fake)
            self.assertEqual(finished['prediction'], good)
            self.assertEqual(summarize([finished])['check_failures'], 1)
            self.assertIn('vessel_info', calls[4]['messages'][-1]['content'])
            (directory / '999.json').unlink()
            resumed = run_question(row, self.context, directory, 'mock', 'local', max_steps=6, offline=True)
            self.assertEqual(resumed['prediction'], good)
            self.assertEqual(resumed['trace'], finished['trace'])
            self.assertEqual(len(calls), 6)

    def test_step_budget_does_not_export_unfinished_candidate(self):
        calls = []
        def fake(base, endpoint, payload):
            calls.append(payload)
            return {'choices': [{'message': {'content': json.dumps({'action':'get_schema', 'args':{'tables':[str(len(calls))]}})}, 'finish_reason': 'stop'}]}
        with tempfile.TemporaryDirectory() as temp:
            result = run_question(self.rows[2990], self.context, Path(temp), 'mock', 'local', max_steps=4, call=fake)
            self.assertIsNone(result['prediction'])
            self.assertEqual(result['errors'], ['step_budget_exhausted'])

    def test_same_failure_stops_without_consuming_all_steps(self):
        calls = []
        def fake(base, endpoint, payload):
            calls.append(payload)
            return {'choices': [{'message': {'content': '{"action":"search_knowledge","args":{}}'}, 'finish_reason': 'stop'}]}
        with tempfile.TemporaryDirectory() as temp:
            result = run_question(self.rows[2990], self.context, Path(temp), 'mock', 'local', max_steps=16, call=fake)
            self.assertIsNone(result['prediction'])
            self.assertEqual(result['errors'], ['repeated_error_without_progress'])
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(result['trace']), 2)


if __name__ == '__main__':
    unittest.main()
