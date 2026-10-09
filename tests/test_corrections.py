import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text2query.shared.context import Context, corrected_prediction


class CorrectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = Context(True)

    def test_only_exact_questions_use_deterministic_corrections(self):
        question = '查询入学年份为2022的班级，返回班级编号与名称，并按班级编号升序取前20条。'
        exact = self.context.build(question.replace('，',','))
        self.assertEqual(exact['question_correction']['group_id'],'ambiguous-1684')
        prediction = corrected_prediction(exact,self.context.catalog)
        self.assertIn('grade_year=2022',prediction['query'])
        self.assertIn('education_2024.class',prediction['query'])
        self.assertIn('education_2025.class',prediction['query'])
        changed = self.context.build(question.replace('2022','2021'))
        self.assertIsNone(changed['question_correction'])
        self.assertIsNone(corrected_prediction(changed,self.context.catalog))
        self.assertTrue(changed['corrected_examples'])

    def test_invalid_curated_queries_are_rejected(self):
        ctx = self.context.build('有多少篇论文是预印本或未发表的？')
        ctx['question_correction'] = {**ctx['question_correction'],'query':'DELETE FROM authors.Paper'}
        with self.assertRaises(ValueError):
            corrected_prediction(ctx,self.context.catalog)

    def test_schema_adjudications_reference_real_tables_and_follow_domain(self):
        known = {f"{t['database']}.{t['table']}" if t.get('database') else t['table']
                 for t in self.context.catalog}
        for rule in self.context.schema_adjudications:
            self.assertTrue(set(rule['tables'])<=known)
        ais = self.context.build('查询所有油轮的AIS记录')
        self.assertTrue(any(rule['rule_id']=='vessel_type_source_specific_codes' for rule in ais['schema_adjudications']))
        self.assertFalse(ais['education_merge'])
        address = self.context.build('查询行政区划中的城市名称')
        self.assertTrue(any(rule['rule_id']=='administrative_division_actual_key' for rule in address['schema_adjudications']))


if __name__=='__main__':
    unittest.main()
