"""Raw evidence retention, exact-case injection, and ReAct dependency regressions."""
import json
import tempfile
import unittest
from pathlib import Path
from text2query.model_pipeline import Context
from text2query.paths import RAW, OUT
from text2query.pipeline import load, normalize, template
from text2query.react_pipeline import initial_prompt, run_question
from text2query.react_tools import QueryTools


class ReactEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = Context(False, raw_evidence=True)
        cls.rows = load(RAW / 'QA_train.json')

    def tools(self, question='列出所有书籍的书名'):
        return QueryTools(self.context, self.context.build(question))

    def test_all_raw_records_and_annotations_are_retained(self):
        actual = {r['question_id']:r for r in self.context.examples}
        self.assertEqual(len(actual), len(self.rows))
        for row in self.rows:
            self.assertEqual(actual[row['question_id']]['raw_annotation'], row)
        knowledge = {r['knowledge_id']:r for r in self.context.knowledge}
        for row in load(RAW / 'knowledge.json'):
            self.assertEqual(knowledge[row['knowledge_id']]['raw_annotation'], row)

    def test_exact_index_retains_all_duplicates_and_conflicts(self):
        expected = {}
        for row in self.rows:
            expected.setdefault(normalize(row['question']), set()).add(row['question_id'])
        for question, ids in expected.items():
            self.assertEqual({r['question_id'] for r in self.context.exact_examples(question)}, ids)
        group = max(self.context.examples_by_question.values(), key=len)
        self.assertGreater(len(group), 1)
        result = self.tools(group[0]['question']).dispatch('search_examples', {'query':'完全无关的检索词', 'limit':1})
        self.assertEqual(len(result['exact_matches']), len(group))
        self.assertEqual(result['examples'], [])
        self.assertGreater(result['related_examples_available'], 0)

    def test_selected_same_questions_are_in_actual_initial_prompts(self):
        for qid in (3040,3041,3044,3090,3099,3877,6204):
            row = next(r for r in self.rows if r['question_id'] == qid)
            with tempfile.TemporaryDirectory() as directory:
                result = run_question(row, self.context, Path(directory), 'mock', 'local', offline=True)
                request = load(Path(directory) / f'{qid}.request.0.json')
                prompt = json.loads(request['messages'][1]['content'])
                example = next(r for r in prompt['exact_examples'] if r['question_id'] == qid)
                self.assertEqual(example['raw_annotation'], row)
                self.assertEqual(prompt['workflow']['next_action'], 'search_examples')
                self.assertEqual(result['errors'], ['pending_request'])

    def test_schema_chinese_comments_are_returned_and_retrievable(self):
        for query, name in [('销售平台字典','platform_dict'), ('船舶类型字典','vessel_type_dict'),
                            ('统一行政区划表','administrative_division')]:
            tools = self.tools('查询' + query)
            tools.dispatch('search_examples', {})
            result = tools.dispatch('get_schema', {'query':query, 'database':'common', 'limit':1})
            self.assertEqual(result['tables'][0]['table'], name)
            self.assertIn(query, result['tables'][0]['raw_schema'])
            self.assertIn(query, result['tables'][0]['table_comments'])

    def test_required_order_and_evidence_invalidation(self):
        tools = self.tools()
        prediction = {'db':'SQLite','query':'SELECT title FROM book_publishing_company.titles'}
        self.assertFalse(tools.dispatch('get_schema', {'tables':['book_publishing_company.titles']})['ok'])
        self.assertFalse(tools.dispatch('search_knowledge', {})['ok'])
        self.assertFalse(tools.dispatch('check_query', {'prediction':prediction})['ok'])
        self.assertTrue(tools.dispatch('search_examples', {})['ok'])
        tools.dispatch('get_schema', {'tables':['book_publishing_company.titles']})
        tools.dispatch('search_knowledge', {})
        self.assertTrue(tools.dispatch('check_query', {'prediction':prediction})['ok'])
        tools.dispatch('get_schema', {'tables':['book_publishing_company.sales']})
        self.assertFalse(tools.dispatch('finish', {'prediction':prediction})['ok'])
        self.assertFalse(tools.dispatch('check_query', {'prediction':prediction})['ok'])
        tools.dispatch('search_knowledge', {})
        tools.dispatch('check_query', {'prediction':prediction})
        self.assertTrue(tools.dispatch('finish', {'prediction':prediction})['ok'])
        tools.dispatch('search_examples', {})
        self.assertFalse(tools.dispatch('finish', {'prediction':prediction})['ok'])

    def test_knowledge_scope_follows_schema_instead_of_initial_route(self):
        row = next(r for r in self.rows if r['question_id']==3040)
        tools = self.tools(row['question'])
        tools.dispatch('search_examples', {})
        tools.dispatch('get_schema', {'tables':['books.cust_order']})
        result = tools.dispatch('search_knowledge', {'query':'订单日期 cust_order order_date'})
        self.assertEqual(result['scope']['databases'], ['books'])
        self.assertTrue(result['knowledge'])
        for item in result['knowledge']:
            self.assertNotIn('book_publishing_company', item['scope']['databases'])

    def test_evaluation_does_not_restore_held_out_answers(self):
        context = Context(True, raw_evidence=True)
        held_out = {template(r['question']) for r in load(OUT / 'dev.json')}
        self.assertFalse(any(template(r['question']) in held_out for r in context.examples))
        for row in load(OUT / 'dev.json')[:10]:
            self.assertFalse(context.exact_examples(row['question']))

    def test_exact_domain_evidence_outranks_ambiguous_topic_words(self):
        for qid in (5597, 5651):
            row = next(r for r in self.rows if r['question_id'] == qid)
            ctx = self.context.build(row['question'])
            self.assertEqual(ctx['routing']['databases'], ['hockey'])
            prompt = initial_prompt(ctx)
            self.assertTrue(prompt['schema_candidates'])
            self.assertTrue(all(t['table'].startswith('hockey.') for t in prompt['schema_candidates']))

    def test_field_knowledge_does_not_mix_uninspected_tables(self):
        row = next(r for r in self.rows if r['question_id'] == 4726)
        tools = self.tools(row['question'])
        tools.dispatch('search_examples', {})
        tools.dispatch('get_schema', {'tables':['disney.characters', 'disney.director']})
        result = tools.dispatch('search_knowledge', {'query':'release_date 12月 上映日期', 'limit':10})
        texts = [item['knowledge_text'] for item in result['knowledge']]
        self.assertTrue(any('两种' in text or "'DD-Mon-YY'" in text for text in texts))
        self.assertFalse(any('的表revenue中' in text or '的表movies_total_gross中' in text for text in texts))

    def test_better_question_boundary_is_allowed_with_model_review(self):
        row = next(r for r in self.rows if r['question_id'] == 6054)
        tools = self.tools(row['question'])
        tools.dispatch('search_examples', {})
        tools.dispatch('get_schema', {'tables':['movie_3.customer']})
        tools.dispatch('search_knowledge', {})
        prediction = {'db':'SQLite', 'query':row['query'].replace('< 100', '<= 100')}
        reviewed = []
        def reviewer(evidence):
            reviewed.append(evidence)
            return {'verdict':'accept', 'issues':[], 'guidance':'题意包含100，修正合理'}
        tools.semantic_reviewer = reviewer
        checked = tools.dispatch('check_query', {'prediction':prediction, 'case_review':{
            'case_ids':[6054], 'decision':'repair', 'changes':[{'aspect':'边界',
                'reason':'题目包含100，原案例排除了100', 'evidence':'当前问题：100及以下'}]}})
        self.assertTrue(checked['ok'], checked['errors'])
        self.assertEqual(reviewed[0]['candidate'], prediction)
        self.assertEqual(reviewed[0]['same_question_cases'][0]['question_id'], 6054)
        final = tools.dispatch('finish', {'prediction':prediction})
        self.assertEqual(final['prediction'], prediction)

    def test_independent_review_can_reject_a_copied_case(self):
        row = next(r for r in self.rows if r['question_id'] == 3491)
        tools = self.tools(row['question'])
        tools.dispatch('search_examples', {})
        tools.dispatch('get_schema', {'tables':['genes.Genes', 'genes.Interactions']})
        tools.dispatch('search_knowledge', {})
        tools.semantic_reviewer = lambda evidence: {'verdict':'revise', 'issues':[{
            'aspect':'返回列', 'problem':'返回GeneID而非Motif', 'evidence':'当前问题要求基序，Genes.Motif是基序'}],
            'guidance':'输出Motif'}
        checked = tools.dispatch('check_query', {'prediction':{'db':row['db'], 'query':row['query']}})
        self.assertFalse(checked['ok'])
        self.assertFalse(checked['modified_exact_case'])
        self.assertFalse(tools.dispatch('finish', {'prediction':checked['candidate']})['ok'])


if __name__ == '__main__':
    unittest.main()
