import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from text2query.shared.data import schema_catalog
from text2query.shared.dsl_normalization import normalize_es
from text2query.shared.generation import validate
from text2query.shared.context import Context


class ESRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog, _ = schema_catalog()

    def check(self, dsl, question=None):
        return validate({'db': 'Elasticsearch', 'query': json.dumps(dsl)}, self.catalog, question)

    def runtime_query(self):
        # 365 is an explicit test assumption, not a new contest business rule.
        return {
            'runtime_mappings': {'daily_outpatient': {
                'type': 'double', 'script': {
                    'source': "if (doc['annual_outpatient'].size() != 0) { emit(doc['annual_outpatient'].value / params.days); }",
                    'params': {'days': 365.0},
                }}},
            'query': {'range': {'daily_outpatient': {'gt': 10000}}},
            '_source': ['name'], 'fields': [{'field': 'daily_outpatient'}],
            'sort': [{'daily_outpatient': 'desc'}],
            'aggs': {'daily_stats': {'stats': {'field': 'daily_outpatient'}}},
        }

    def test_explicit_runtime_field_works_in_query_sort_aggregation_and_fields(self):
        self.assertEqual(self.check(self.runtime_query()), [])

    def test_daily_field_without_runtime_definition_is_rejected(self):
        dsl = self.runtime_query()
        del dsl['runtime_mappings']
        errors = self.check(dsl)
        for section in ('query.range', 'sort', 'aggs', 'fields'):
            self.assertTrue(any(section in error and 'daily_outpatient' in error for error in errors), errors)

    def test_runtime_field_cannot_be_returned_in_source_including_object_form(self):
        for selection in (['name', 'daily_outpatient'], {'includes': ['name', 'daily_outpatient']}):
            dsl = self.runtime_query()
            dsl['_source'] = selection
            self.assertTrue(any('_source' in error and 'daily_outpatient' in error for error in self.check(dsl)))
        dsl['_source'] = {'includes': ['name'], 'excludes': ['daily_outpatient']}
        self.assertEqual(self.check(dsl), [])

    def test_script_cannot_depend_on_nonexistent_field(self):
        dsl = self.runtime_query()
        dsl['runtime_mappings']['daily_outpatient']['script']['source'] = "emit(doc['missing_annual'].value / params.days)"
        self.assertTrue(any('missing_annual' in error for error in self.check(dsl)))

    def test_invalid_runtime_and_unknown_returned_fields_are_rejected(self):
        for runtime in ([], None, {'daily_outpatient': {'type': 'float'}}, {'daily_outpatient': 'double'}):
            dsl = self.runtime_query()
            dsl['runtime_mappings'] = runtime
            self.assertTrue(self.check(dsl, '查询日均门诊量大于10000的医院'))
        for requested in (['unknown_count'], [{'field': 'unknown_count'}], 'daily_outpatient'):
            dsl = self.runtime_query()
            dsl['fields'] = requested
            self.assertTrue(self.check(dsl))

    def test_daily_threshold_on_annual_field_is_a_semantic_error(self):
        dsl = {'query': {'bool': {'filter': [{'range': {'annual_outpatient': {'gt': 10000}}}]}}}
        self.assertEqual(self.check(dsl), [])  # The field itself exists.
        for question in ('上海静安寺周围4公里内，找出日均门诊量大于10000的医院。',
                         '统计所有日均门诊量大于1万的医院数量。'):
            self.assertTrue(any('年/日单位混用' in error for error in self.check(dsl, question)))
        self.assertEqual(self.check(dsl, '查询年门诊量大于10000的医院'), [])
        dsl['query']['bool']['filter'][0]['range']['annual_outpatient']['gt'] = 3650000
        self.assertEqual(self.check(dsl, '按365天折算，查询日均门诊量大于10000的医院'), [])
        dsl['query']['bool']['filter'][0]['range']['annual_outpatient']['gt'] = 0
        self.assertEqual(self.check(dsl, '查询日均门诊量大于0的医院'), [])

    def test_normalization_keeps_runtime_definitions_and_rejects_conflicts(self):
        dsl = self.runtime_query()
        raw = {'db': 'Elasticsearch', 'query': {'query': dsl['query']},
               **{key: value for key, value in dsl.items() if key != 'query'}}
        before = copy.deepcopy(raw)
        fixed = json.loads(normalize_es(raw)['query'])
        self.assertEqual(fixed, dsl)
        self.assertEqual(raw, before)
        raw['query']['runtime_mappings'] = {}
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            normalize_es(raw)

    def test_context_exposes_missing_formula_and_excludes_invalid_training_examples(self):
        context = Context(False, evidence_mode='reviewed')
        ctx = context.build('上海静安寺周围4公里内，找出日均门诊量大于10000的医院。')
        rule = next(item for item in ctx['schema_adjudications']
                    if item['rule_id'] == 'es_daily_outpatient_missing_formula')
        self.assertEqual(rule['status'], 'pending_business_formula')
        self.assertTrue(any(item['rule'] == 'outpatient_units' for item in ctx['requirements']))
        self.assertFalse({2079, 2239, 2288, 2347, 2367, 2386, 2429, 2434, 2444}
                         & {row['question_id'] for row in context.examples})


if __name__ == '__main__':
    unittest.main()
