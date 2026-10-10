"""Raw-derived retrieval must not inject curated business knowledge."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from text2query.shared.context import Context
from text2query.shared.data import load, read_text
from text2query.shared.paths import RAW, ROOT
from text2query.react.pipeline import initial_prompt, run_question, react_system
from text2query.react.tools import QueryTools
from text2query.react.semantic_review import REVIEW_SYSTEM, RAW_REVIEW_SYSTEM


class RawOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch('text2query.shared.context.load_overlays', side_effect=AssertionError('curation loaded')):
            cls.context = Context(False)
        cls.rows = {r['question_id']: r for r in load(RAW / 'QA_train.json')}

    def test_original_cases_and_knowledge_are_used_without_exclusions(self):
        self.assertTrue(self.context.raw_only)
        self.assertEqual(len(self.context.examples), len(self.rows))
        for example in self.context.examples:
            original = self.rows[example['question_id']]
            self.assertEqual({k: example[k] for k in original}, original)
            self.assertNotIn('reviewed_annotation', example)
            self.assertNotIn('annotation_origin', example)
        for original, item in zip(load(RAW / 'knowledge.json'), self.context.knowledge):
            self.assertEqual({k: item[k] for k in original}, original)
            self.assertNotIn('reviewed_knowledge_text', item)
            self.assertNotIn('declared_scope', item)

    def test_provenance_is_raw_and_table_text_is_a_preprocessed_slice(self):
        hashes = {}
        for item in (self.context.evidence_manifest['files'] + self.context.examples +
                     self.context.knowledge + self.context.catalog + self.context.dictionaries):
            provenance = item.get('provenance', item)
            path = ROOT / provenance['file']
            self.assertTrue(path.is_relative_to(ROOT / 'data/raw'))
            if path not in hashes:
                hashes[path] = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(provenance['sha256'], hashes[path])
        for table in self.context.catalog:
            content = read_text(ROOT / table['provenance']['file'])[0]
            self.assertIn(table['raw_schema'], content)
        self.assertEqual(self.context.reference_documents, [])

    def test_business_rules_corrections_and_helpers_are_absent(self):
        self.assertFalse(self.context.business_rules)
        self.assertFalse(self.context.corrections)
        self.assertFalse(self.context.schema_adjudications)
        for question in ('统计教师中电话不为空的数量，并返回数量。',
                         '计算篮球球员的BMI', '统计商家营业时间', '医院日均门诊量'):
            ctx = self.context.build(question)
            for key in ('requirements','business_rules','education_merge','education_scope',
                        'corrected_examples','schema_adjudications','query_helpers'):
                self.assertFalse(ctx[key], key)
            self.assertIsNone(ctx['question_correction'])
            self.assertTrue(all('resolution' not in group for group in ctx['ambiguous_examples']))

    def test_actual_prompt_uses_retrieval_protocol_without_whole_file_tools(self):
        row = self.rows[3772]
        with tempfile.TemporaryDirectory() as directory:
            result = run_question(row, self.context, Path(directory), 'mock', 'local', offline=True)
            request = load(Path(directory) / '3772.request.0.json')
            self.assertEqual(result['errors'], ['pending_request'])
            self.assertEqual(request['messages'][0]['content'], react_system(True))
            self.assertNotIn('data/raw', request['messages'][0]['content'])
            prompt = json.loads(request['messages'][1]['content'])
            for key in ('business_rules','requirements','education_merge','schema_adjudications'):
                self.assertNotIn(key, prompt)
            self.assertNotIn('include_source_documents', prompt['tools']['get_schema']['args'])
            self.assertEqual(next(e['query'] for e in prompt['exact_examples'] if e['question_id']==3772), row['query'])
        self.assertEqual(RAW_REVIEW_SYSTEM, REVIEW_SYSTEM)

    def test_tool_returns_contain_only_retrieved_preprocessed_evidence(self):
        row = self.rows[3772]
        tools = QueryTools(self.context, self.context.build(row['question']))
        cases = tools.dispatch('search_examples', {})
        self.assertNotIn('corrected_examples', cases)
        refs = cases['exact_matches'][0]['schema_refs']
        schema = tools.dispatch('get_schema', {'tables':[r['database']+'.'+r['table'] for r in refs],
                                              'include_source_documents':True})
        self.assertTrue(schema['ok'], schema.get('errors'))
        self.assertEqual(schema['source_documents'], [])
        self.assertEqual(schema['reference_documents'], [])
        knowledge = tools.dispatch('search_knowledge', {})
        self.assertNotIn('schema_adjudications', knowledge)
        self.assertNotIn('query_helpers', knowledge)
        for item in knowledge['knowledge']:
            self.assertNotIn('derived_constants', item)
            self.assertNotEqual(item['retrieval_reason'], '字段/公式依赖，必须同时提供')

    def test_manual_year_policy_disabled_but_structural_checks_still_run(self):
        question = '统计教师中电话不为空的数量，并返回数量。'
        tools = QueryTools(self.context, self.context.build(question))
        tools.dispatch('search_examples', {})
        tools.dispatch('get_schema', {'tables':['education_2024.teacher']})
        tools.dispatch('search_knowledge', {})
        prediction = {'db':'MYSQL','query':"SELECT COUNT(*) FROM education_2024.teacher WHERE phone IS NOT NULL"}
        result = tools.dispatch('check_query', {'prediction':prediction})
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['prediction'], prediction)
        self.assertNotIn('question rules', result['checks'])
        invalid = tools.dispatch('check_query', {'prediction':{'db':'MYSQL','query':'DELETE FROM education_2024.teacher'}})
        self.assertFalse(invalid['ok'])
        sqlite = tools.dispatch('check_query', {'prediction':{'db':'SQLite','query':'SELECT missing_field FROM video_games.games'}})
        self.assertFalse(sqlite['ok'])

    def test_review_receives_no_curated_evidence_and_keeps_source_ids(self):
        captured = []
        reviewer = lambda evidence: captured.append(evidence) or {'verdict':'accept','issues':[]}
        row = self.rows[3772]
        tools = QueryTools(self.context, self.context.build(row['question']), semantic_reviewer=reviewer)
        tools.dispatch('search_examples', {})
        refs = initial_prompt(tools.ctx)['exact_examples'][0]['schema_refs']
        tools.dispatch('get_schema', {'tables':[r['database']+'.'+r['table'] for r in refs]})
        tools.dispatch('search_knowledge', {})
        result = tools.dispatch('check_query', {'prediction':{'db':row['db'],'query':row['query']}})
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(len(captured), 1)
        for key in ('business_rules','education_scope','schema_adjudications'):
            self.assertNotIn(key, captured[0])
        self.assertTrue(all(e['provenance']['file'].startswith('data/raw/') for e in captured[0]['same_question_cases']))

    def test_reviewed_mode_can_restore_existing_curation(self):
        reviewed = Context(False, raw_evidence=True, evidence_mode='reviewed')
        self.assertTrue(reviewed.business_rules)
        patched = next(e for e in reviewed.examples if e['question_id']==3772)
        self.assertNotEqual(patched['query'], self.rows[3772]['query'])
        self.assertIn('reviewed_annotation', patched)
        self.assertEqual(next(e for e in self.context.examples if e['question_id']==3772)['query'], self.rows[3772]['query'])


if __name__ == '__main__':
    unittest.main()
