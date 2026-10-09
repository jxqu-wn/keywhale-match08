import json
from pathlib import Path
import tempfile
import unittest
from text2query.semantic_review import SemanticReviewer
from text2query.react_pipeline import run_question
from text2query.model_pipeline import Context


class SemanticReviewTests(unittest.TestCase):
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
            result = run_question({'question_id':999,'question':'问题'}, Context(False),
                                  Path(directory),'mock','local',call=unexpected)
            self.assertEqual(result, failure)
            self.assertEqual(path.read_bytes(), original)
