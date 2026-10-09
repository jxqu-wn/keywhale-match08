import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text2query.shared.data import load
from text2query.shared.paths import OUT, CURATED
from text2query.shared.education import education_query_mode, view_definitions, expand_prediction, model_query_errors, entity_columns
from text2query.shared.education_fixture import fixture, student_cohort_rows
from text2query.shared.context import Context
from text2query.shared.generation import validate


class EducationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load(OUT/'catalog.json')

    def test_all_views_match_independent_python_oracle(self):
        db,oracle = fixture(self.catalog)
        try:
            for entity,items in oracle.items():
                cols = entity_columns(self.catalog,entity)
                actual = db.execute('WITH '+','.join(view_definitions(self.catalog,[entity]))+' SELECT '+','.join(cols)+' FROM edu_'+entity).fetchall()
                expected = [tuple(row[col] for col in cols) for row in items.values()]
                self.assertEqual(sorted(actual,key=repr),sorted(expected,key=repr),entity)
        finally:
            db.close()

    def test_field_year_values_do_not_select_annual_database(self):
        for question in ('查询学号前缀为2023的在读学生','查询入学年份为2022的班级',
                         '查询学期代码包含“2024”的学期','查询2022年入学的学生'):
            self.assertEqual(education_query_mode(question)['mode'],'merged_latest')
        self.assertEqual(education_query_mode('统计2024年教师数量')['mode'],'explicit_year')
        self.assertEqual(education_query_mode('比较2024与2025课程数量')['mode'],'separate_years')
        self.assertEqual(education_query_mode('统计去年教师数量')['mode'],'explicit_time')

    def test_expansion_with_model_ctes_and_identity_checks(self):
        ctx = Context(True).build('统计每个班级的在读学生数量')
        pred = {'db':'MYSQL','query':'WITH active AS (SELECT * FROM edu_student WHERE student_status_code=\'ACTIVE\') SELECT COUNT(*) FROM active'}
        expanded = expand_prediction(pred,ctx,self.catalog)
        self.assertIn('active AS',expanded['query'])
        self.assertEqual(validate(expanded,self.catalog,ctx['question'],ctx['business_rules']),[])
        db,_ = fixture(self.catalog)
        try:
            self.assertEqual(db.execute(expanded['query']).fetchone(),(6,))
        finally:
            db.close()
        bad = {'db':'MYSQL','query':'SELECT * FROM edu_student s JOIN edu_enrollment e ON s.student_id=e.student_id'}
        self.assertTrue(model_query_errors(bad,ctx))
        good = {'db':'MYSQL','query':'SELECT s.student_no FROM edu_student s JOIN edu_enrollment e ON s.student_no=e.student_no'}
        self.assertEqual(model_query_errors(good,ctx),[])

    def test_year_missing_record_backfill_and_filter_after_latest(self):
        db,_ = fixture(self.catalog)
        try:
            query = 'WITH '+','.join(view_definitions(self.catalog,['teacher']))+" SELECT COUNT(*) FROM edu_teacher WHERE phone IS NOT NULL AND phone<>''"
            self.assertEqual(db.execute(query).fetchone(),(4,))
            self.assertEqual(db.execute('WITH '+','.join(view_definitions(self.catalog,['student']))+" SELECT student_status_code FROM edu_student WHERE student_no='2023001'").fetchone(),('INACTIVE',))
            events = db.execute('WITH '+','.join(view_definitions(self.catalog,['enrollment']))+" SELECT academic_year FROM edu_enrollment WHERE student_no='2023002' AND course_code='C1' ORDER BY academic_year").fetchall()
            self.assertEqual(events,[(2024,),(2025,),(2025,)])
        finally:
            db.close()

    def test_scope_guard_does_not_apply_education_rules_to_other_sources(self):
        from text2query.shared.query_rules import applicable_business_rules
        self.assertEqual(applicable_business_rules('查询所有学生',load(CURATED/'business_rules.json'),['college.student']),[])
        ctx = Context(True).build('加泰罗尼亚语中出现次数最多的单词是哪个？')
        self.assertEqual(ctx['business_rules'],[])
        self.assertFalse(ctx['education_merge'])

    def test_colliding_yearly_ids_remain_distinct_business_entities(self):
        db,_ = fixture(self.catalog)
        try:
            db.execute("UPDATE education_2025.teacher SET teacher_id=9 WHERE teacher_no='T3'")
            sql = 'WITH '+','.join(view_definitions(self.catalog,['teacher']))+" SELECT teacher_no FROM edu_teacher WHERE teacher_id=9 ORDER BY teacher_no"
            self.assertEqual(db.execute(sql).fetchall(),[('T3',),('T9',)])
        finally:
            db.close()
        ctx = Context(True).build('统计教师数量')
        bad = {'db':'MYSQL','query':'SELECT COUNT(DISTINCT teacher_id) FROM edu_teacher'}
        self.assertTrue(model_query_errors(bad,ctx))

    def test_display_identifiers_and_semester_filters_are_not_business_codes(self):
        ctx = Context(True).build('查询学期编号为1中选课人数排名第一的课程，返回课程编号与课程名。')
        bad = {'db':'MYSQL','query':"SELECT c.course_code,c.course_name FROM edu_course c JOIN edu_enrollment e ON e.course_code=c.course_code WHERE e.semester_code='1'"}
        errors = model_query_errors(bad,ctx)
        self.assertTrue(any('course_id' in error for error in errors))
        self.assertTrue(any('semester_id' in error for error in errors))

    def test_default_score_statistics_exclude_withdrawals(self):
        ctx = Context(True).build('统计学期一中得分分项出现次数最多的分项名称')
        self.assertTrue(ctx['education_merge'])
        self.assertTrue(any('education_2025.exam_score' in group['tables'] for group in ctx['ambiguous_examples']))
        bad = {'db':'MYSQL','query':'SELECT exam_item,COUNT(*) FROM edu_exam_score GROUP BY exam_item'}
        self.assertTrue(any('1575' in error for error in model_query_errors(bad,ctx)))
        good = {'db':'MYSQL','query':'''SELECT es.exam_item,COUNT(*) FROM edu_exam_score es
            JOIN edu_enrollment e ON es.student_no=e.student_no AND es.course_code=e.course_code
                AND es.academic_year=e.academic_year AND es.semester_code=e.semester_code
            WHERE e.semester_id=1 AND e.is_withdraw=0 GROUP BY es.exam_item'''}
        self.assertEqual(model_query_errors(good,ctx),[])

    def test_user_cohort_trial_replaces_prefix_only_for_the_requested_question(self):
        question = '查询学号前缀为2023的在读学生，返回学号与姓名，并按学号升序取前25名。'
        retriever = Context(True)
        ctx = retriever.build(question)
        self.assertTrue(ctx['education_merge']['query_contract']['question_overrides'])
        other = retriever.build('查询学号前缀为2024的在读学生，返回学号与姓名，并按学号升序取前25名。')
        self.assertEqual(other['education_merge']['query_contract']['question_overrides'],[])
        good = {'db':'MYSQL','query':'''SELECT s.student_no,s.student_name FROM edu_student s
            JOIN edu_class cl ON s._class_code=cl.class_code
            WHERE cl.grade_year=2023 AND s.student_status_code='ACTIVE'
            ORDER BY s.student_no ASC LIMIT 25'''}
        self.assertEqual(model_query_errors(good,ctx),[])
        bad = {**good,'query':good['query'].replace('ORDER BY',"AND s.student_no LIKE '2023%' ORDER BY")}
        self.assertTrue(any('LIKE' in error for error in model_query_errors(bad,ctx)))
        wrong_cohort = {**good,'query':good['query'].replace('grade_year=2023','grade_year=2024')}
        self.assertTrue(any('grade_year' in error for error in model_query_errors(wrong_cohort,ctx)))
        db,oracle = fixture(self.catalog,student_cohort_rows(self.catalog))
        try:
            classes = {row['class_id']:row for row in oracle['class'].values()}
            eligible = [row for row in oracle['student'].values() if row['student_status_code']=='ACTIVE'
                        and classes.get(row['class_id'],{}).get('grade_year')==2023]
            expected = [(row['student_no'],row['student_name']) for row in sorted(eligible,key=lambda row:row['student_no'])[:25]]
            expanded = expand_prediction(good,ctx,self.catalog)
            actual = db.execute(expanded['query']).fetchall()
            self.assertEqual(actual,expected)
            self.assertEqual(len(actual),25)
            self.assertIn(('2023003','仅旧年存在的2023级学生'),actual)
            self.assertTrue(any(number.startswith('S') for number,name in actual))
            for excluded in ('2023000','2023006','2023999'):
                self.assertFalse(any(number==excluded for number,name in actual))
            self.assertIn(('S2023000','同年修订姓名'),actual)
        finally:
            db.close()


if __name__=='__main__':
    unittest.main()
