import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from text2query.react.semantic_review import SemanticReviewer
from text2query.react.semantic_review import REVIEW_SYSTEM
from text2query.react.pipeline import run_question, initial_prompt, react_system, summarize
from text2query.shared.context import Context


class SemanticReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw_context = Context(False)

    def actions(self, candidates):
        return [
            {'action':'search_examples','args':{}},
            {'action':'get_schema','args':{'tables':['book_publishing_company.titles']}},
            {'action':'search_knowledge','args':{}},
            *[{'action':'check_query','args':{'prediction':candidate}} for candidate in candidates],
            {'action':'finish','args':{'prediction':candidates[-1]}},
        ]

    def test_default_disables_review_and_allows_checked_candidate_to_finish(self):
        candidate = {'db':'SQLite','query':'SELECT title FROM book_publishing_company.titles'}
        actions = iter(self.actions([candidate]))
        calls = []
        def generator(base, endpoint, payload):
            calls.append(json.loads(json.dumps(payload)))
            return {'choices':[{'message':{'content':json.dumps(next(actions))}, 'finish_reason':'stop'}]}
        with tempfile.TemporaryDirectory() as directory, patch(
                'text2query.react.semantic_review.SemanticReviewer', side_effect=AssertionError('reviewer created')):
            record = run_question({'question_id':99001,'question':'列出所有书籍的书名'}, self.raw_context,
                                  Path(directory),'mock','local',call=generator)
            self.assertEqual(record['prediction'], candidate)
            self.assertEqual(record['errors'], [])
            self.assertFalse(record['semantic_review_enabled'])
            self.assertEqual(record['semantic_review_api_calls'], 0)
            self.assertEqual(record['api_calls'], 5)
            self.assertEqual(summarize([record])['semantic_review_api_calls'], 0)
            self.assertEqual(list(Path(directory).glob('*.semantic_review.*')), [])
            check = next(t['observation'] for t in record['trace'] if t['action']=='check_query')
            self.assertIsNone(check['semantic_review'])
            self.assertFalse(check['semantics_reviewed'])
            prompt = json.loads(calls[0]['messages'][1]['content'])
            self.assertFalse(prompt['semantic_review_enabled'])
            self.assertNotIn('独立LLM', calls[0]['messages'][0]['content'])
            self.assertNotIn('复核', json.dumps(prompt['tools'],ensure_ascii=False))

    def test_disabled_review_still_rejects_sqlite_field_errors(self):
        bad = {'db':'SQLite','query':'SELECT missing_field FROM book_publishing_company.titles'}
        good = {'db':'SQLite','query':'SELECT title FROM book_publishing_company.titles'}
        actions = iter(self.actions([bad, good]))
        def generator(base, endpoint, payload):
            return {'choices':[{'message':{'content':json.dumps(next(actions))}, 'finish_reason':'stop'}]}
        with tempfile.TemporaryDirectory() as directory:
            record = run_question({'question_id':99002,'question':'列出所有书籍的书名'}, self.raw_context,
                                  Path(directory),'mock','local',max_steps=6,call=generator)
            self.assertEqual(record['prediction'], good)
            checks = [t['observation'] for t in record['trace'] if t['action']=='check_query']
            self.assertFalse(checks[0]['ok'])
            self.assertTrue(any('SQLite原始DDL编译失败' in error for error in checks[0]['errors']))
            self.assertTrue(checks[1]['ok'])
            self.assertEqual(record['semantic_review_api_calls'], 0)

    def test_review_can_be_explicitly_restored_and_blocks_rejected_candidate(self):
        bad = {'db':'SQLite','query':'SELECT title FROM book_publishing_company.titles'}
        good = {'db':'SQLite','query':'SELECT title FROM book_publishing_company.titles WHERE title IS NOT NULL'}
        actions = self.actions([bad, good])
        actions.insert(4, {'action':'finish','args':{'prediction':bad}})
        actions = iter(actions)
        reviews = []
        def service(base, endpoint, payload):
            if payload['messages'][0]['content'] == REVIEW_SYSTEM:
                reviews.append(payload)
                result = ({'verdict':'revise','issues':[{'aspect':'条件','problem':'未排除空书名',
                            'evidence':'题目要求排除没有书名的记录'}],'guidance':'增加title IS NOT NULL'}
                          if len(reviews)==1 else {'verdict':'accept','issues':[],'guidance':'条件符合题意'})
            else:
                result = next(actions)
            return {'choices':[{'message':{'content':json.dumps(result)}, 'finish_reason':'stop'}]}
        with tempfile.TemporaryDirectory() as directory:
            record = run_question({'question_id':99003,'question':'列出所有书籍的书名，排除没有书名的记录'},
                                  self.raw_context, Path(directory),'mock','local',max_steps=7,call=service,
                                  semantic_review=True)
            self.assertEqual(record['prediction'], good)
            self.assertTrue(record['semantic_review_enabled'])
            self.assertEqual(record['semantic_review_api_calls'], 2)
            self.assertEqual(record['api_calls'], 9)
            premature = next(t['observation'] for t in record['trace'] if t['action']=='finish')
            self.assertFalse(premature['ok'])
            self.assertEqual(len(list(Path(directory).glob('*.semantic_review.*.request.json'))), 2)

    def test_review_setting_cannot_reuse_opposite_mode_record(self):
        candidate = {'db':'SQLite','query':'SELECT title FROM book_publishing_company.titles'}
        actions = iter(self.actions([candidate]))
        def generator(base, endpoint, payload):
            return {'choices':[{'message':{'content':json.dumps(next(actions))}, 'finish_reason':'stop'}]}
        row = {'question_id':99004,'question':'列出所有书籍的书名'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            run_question(row, self.raw_context, path, 'mock','local',call=generator)
            original = (path/'99004.json').read_bytes()
            def unexpected(*args):
                self.fail('cached mode mismatch must not call a model')
            with self.assertRaisesRegex(ValueError, '语义复核设置'):
                run_question(row, self.raw_context, path, 'mock','local',call=unexpected,semantic_review=True)
            self.assertEqual((path/'99004.json').read_bytes(), original)

    def test_prompt_descriptions_follow_the_review_toggle(self):
        for raw_only in (False, True):
            self.assertNotIn('独立LLM', react_system(raw_only))
            self.assertNotIn('独立复核', react_system(raw_only))
            self.assertIn('独立LLM', react_system(raw_only, True))
        ctx = self.raw_context.build('列出所有书籍的书名')
        self.assertNotIn('复核', json.dumps(initial_prompt(ctx)['tools'], ensure_ascii=False))
        self.assertIn('独立LLM', json.dumps(initial_prompt(ctx, True)['tools'], ensure_ascii=False))

    def test_review_cache_is_tied_to_actual_candidate_and_preserves_uncertainty(self):
        calls = []
        def api(base, endpoint, payload):
            calls.append(payload)
            return {'choices':[{'message':{'content':json.dumps({
                'verdict':'uncertain', 'issues':[], 'guidance':'日期样例不足'})}}]}
        evidence = {'question':'十二月的电影数量', 'candidate':{'db':'SQLite','query':'SELECT 1'},
                    'case_review':None, 'same_question_cases':[], 'schema':[], 'knowledge':[], 'business_rules':[]}
        with tempfile.TemporaryDirectory() as directory:
            reviewer = SemanticReviewer(Path(directory), 1, 'mock', 'local', 'deepseek', api)
            self.assertEqual(reviewer(evidence)['verdict'], 'uncertain')
            self.assertEqual(reviewer(evidence)['verdict'], 'uncertain')
            changed = {**evidence, 'candidate':{'db':'SQLite', 'query':'SELECT 2'}}
            reviewer(changed)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(list(Path(directory).glob('*.request.json'))), 2)
            self.assertEqual(len(list(Path(directory).glob('*.response.json'))), 2)

    def test_existing_failure_is_returned_unchanged_without_api_calls(self):
        failure = {'question_id':999, 'question':'问题', 'prediction':None,
                   'errors':['step_budget_exhausted'], 'trace':[{'original':'evidence'}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '999.json'
            path.write_text(json.dumps(failure))
            original = path.read_bytes()
            def unexpected(*args):
                self.fail('A failed question must not call the API again')
            result = run_question({'question_id':999,'question':'问题'}, Context(False, evidence_mode='reviewed'),
                                  Path(directory),'mock','local',call=unexpected)
            self.assertEqual(result, failure)
            self.assertEqual(path.read_bytes(), original)
