"""Implement user-authorized conflict corrections and verify local execution."""
import collections
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text2query.paths import ROOT, OUT, CURATED, RUNS
from text2query.pipeline import load, save, ambiguous_examples
from text2query.model_pipeline import Context, corrected_prediction
from text2query.education_fixture import fixture, student_cohort_rows, mysql_fixture_sql

REPORTS = ROOT/'docs/reports/conflicts'
RUN = RUNS/'regression/conflict-corrections-20261009'
SPECS = {
 1674: ('MYSQL',"""SELECT s.student_no,s.student_name FROM edu_student s
JOIN edu_class cl ON s._class_code=cl.class_code
WHERE s.student_status_code='ACTIVE' AND cl.grade_year=2023
ORDER BY s.student_no ASC LIMIT 25""",
        '按用户明确指定，用所属班级grade_year=2023替代学号LIKE；保留在读、学号升序和前25条。'),
 1684: ('MYSQL',"""SELECT cl.class_id,cl.class_name FROM edu_class cl
WHERE cl.grade_year=2022 ORDER BY cl.class_id ASC,cl.class_code ASC LIMIT 20""",
        '按问题入学年份2022筛选，不把字段参数改为数据库年份；升序取20条。'),
 1700: ('MYSQL',"""WITH course_enroll AS (
 SELECT c._department_code,c.department_id,c.course_code,c.course_id,c.course_name,
        COUNT(DISTINCT e.student_no) AS enroll_cnt
 FROM edu_course c JOIN edu_enrollment e ON e.course_code=c.course_code
 WHERE e.semester_id=1 AND e.is_withdraw=0
 GROUP BY c._department_code,c.department_id,c.course_code,c.course_id,c.course_name
), ranked AS (
 SELECT *,ROW_NUMBER() OVER(PARTITION BY _department_code
   ORDER BY enroll_cnt DESC,course_id ASC,course_code ASC) AS rn FROM course_enroll
)
SELECT department_id,course_id,course_name,enroll_cnt FROM ranked WHERE rn<=3
ORDER BY department_id DESC,enroll_cnt DESC,course_id ASC,course_code ASC""",
        '统计全部未退课选课学生，不加题目未要求的ACTIVE；按业务院系分区取前三，院系与人数均降序，并列按课程编号/业务代码稳定选择。'),
 1703: ('MYSQL',"""SELECT AVG(c.credit) AS avg_credit FROM edu_course c HAVING AVG(c.credit)>=3""",
        '先按业务课程合并，再计算明细AVG；用未四舍五入的平均值比较>=3，返回原平均值。低于3时返回空结果。'),
 1708: ('MYSQL',"""SELECT sem.semester_code FROM edu_semester sem
WHERE sem.semester_code LIKE '%2024%' ORDER BY sem.semester_code ASC,sem.academic_year ASC LIMIT 20""",
        '保留问题字符串2024，合并两年学期后筛选代码包含2024、升序取20条；数字属于代码参数。'),
 1891: ('MYSQL',"""SELECT '2024' AS year,COUNT(*) AS credit_zero_course_cnt FROM education_2024.course WHERE credit=0
UNION ALL SELECT '2025' AS year,COUNT(*) AS credit_zero_course_cnt FROM education_2025.course WHERE credit=0
ORDER BY year ASC""",
        '两年分别统计credit=0，统一列别名；按2024、2025稳定输出，不采用最新合并快照替代年度对比。'),
 1921: ('MYSQL',"""WITH a AS (SELECT COUNT(*) AS cnt_2024 FROM education_2024.course WHERE course_type='必修' AND credit>=4),
b AS (SELECT COUNT(*) AS cnt_2025 FROM education_2025.course WHERE course_type='必修' AND credit>=4)
SELECT b.cnt_2025-a.cnt_2024 AS diff_2025_minus_2024,a.cnt_2024,b.cnt_2025 FROM a CROSS JOIN b""",
        '统一等价表达式；保留2025减2024、必修且credit>=4及差值/两年数量三个返回列。'),
 3803: ('SQLite',"""SELECT p.publisher_name FROM video_games.publisher p
JOIN video_games.game_publisher gp ON gp.publisher_id=p.id
GROUP BY p.id,p.publisher_name ORDER BY COUNT(DISTINCT gp.game_id) DESC,p.id ASC LIMIT 1""",
        '修复候选A外层未定义的T别名；以发行商ID区分同名实体，按去重游戏数排名，第一名并列时取ID最小者。'),
 3851: ('SQLite',"""SELECT COUNT(DISTINCT gp.game_id) AS game_count FROM video_games.game_platform gpl
JOIN video_games.game_publisher gp ON gp.id=gpl.game_publisher_id WHERE gpl.release_year=2001""",
        '游戏款数按game_id去重；沿game_platform.game_publisher_id关联，避免把跨平台发行记录当作不同游戏。'),
 4009: ('SQLite',"""SELECT COUNT(p.Id) AS unpublished_count FROM authors.Paper p WHERE p.Year=0""",
        '知识129/134/405支持Year=0，134明确Year>0已发表；选择Year为主判断，避免仅因载体ID均0误计已发表论文。NULL或负年份未被定义为未发表，不自行纳入。知识85/407的载体缺失信号不覆盖正年份。'),
 5196: ('SQLite',"""SELECT p.publisher_name FROM books.publisher p JOIN books.book b ON b.publisher_id=p.publisher_id
GROUP BY p.publisher_id,p.publisher_name ORDER BY COUNT(b.book_id) DESC,p.publisher_id ASC LIMIT 1""",
        '以出版社ID区分同名实体，统一按书籍主键计数；并列取出版社ID最小者，返回名称。')
}


def build():
    original = load(REPORTS/'冲突明细.json')
    corrections = []
    for group in original['groups']:
        ident = group['question_ids'][0]
        if ident not in SPECS:
            continue
        db,query,decision = SPECS[ident]
        tables = group['tables'][:]
        if ident in (1674,):
            tables += ['education_2024.class','education_2025.class']
        if ident==3851:
            tables.append('video_games.game_publisher')
        corrections.append({'group_id':group['group_id'],'question_ids':group['question_ids'],
            'question':group['question'],'db':db,'query':query,'tables':sorted(set(tables)),
            'decision':decision,'status':'implemented','source':'用户授权根据问题、Schema和知识自主修正；独立于官方原标注',
            'knowledge_ids':[x['knowledge_id'] for x in group['knowledge']],
            'implementation_assumptions':('并列取ID最小者；同名主体按主键区分' if ident in (3803,5196) else
                                         '原始平均值作为阈值比较及返回值' if ident==1703 else
                                         '相冲突的专家知识优先采用明确发表年份定义' if ident==4009 else '')})
    assert len(corrections)==11
    save('question_corrections.json',corrections,CURATED)
    return corrections


def correction_rows(catalog):
    rows = student_cohort_rows(catalog)
    for index in range(25):
        rows[2025]['class'].append({**rows[2025]['class'][0],'class_id':200+index,
            'class_code':f'CLX{index:03d}','class_name':f'2022级班级{index:03d}','grade_year':2022})
        rows[2025]['semester'].append({**rows[2025]['semester'][0],'semester_id':200+index,
            'semester_code':f'2024-X{index:03d}','academic_year':2024})
    return rows


def extra_databases(db):
    db.executescript("""
ATTACH DATABASE ':memory:' AS video_games;
CREATE TABLE video_games.publisher (id INTEGER PRIMARY KEY,publisher_name TEXT);
CREATE TABLE video_games.game_publisher (id INTEGER PRIMARY KEY,game_id INTEGER,publisher_id INTEGER);
CREATE TABLE video_games.game_platform (id INTEGER PRIMARY KEY,game_publisher_id INTEGER,platform_id INTEGER,release_year INTEGER);
INSERT INTO video_games.publisher VALUES (1,'同名'),(2,'同名'),(3,'正确发行商'),(4,'并列发行商');
INSERT INTO video_games.game_publisher VALUES (1,1,1),(2,2,1),(3,3,2),(4,10,2),
 (5,4,3),(6,5,3),(7,6,3),(8,7,4),(9,8,4),(10,9,4),(11,4,3),(12,NULL,3);
INSERT INTO video_games.game_platform VALUES (1,1,1,2001),(2,1,2,2001),(3,5,1,2001),
 (4,11,2,2001),(5,6,1,2001),(6,12,1,2001),(7,7,1,2002);
ATTACH DATABASE ':memory:' AS authors;
CREATE TABLE authors.Paper (Id INTEGER PRIMARY KEY,Title TEXT,Year INTEGER,ConferenceId INTEGER,JournalId INTEGER,Keyword TEXT);
INSERT INTO authors.Paper VALUES (1,'零年零载体',0,0,0,NULL),(2,'零年有载体',0,9,0,NULL),
 (3,'零年未知载体',0,NULL,NULL,NULL),(4,'正年无载体',2020,0,0,NULL),
 (5,'年份未知',NULL,0,0,NULL),(6,'负年份',-1,0,0,NULL),(7,'已发表',2021,9,0,NULL);
ATTACH DATABASE ':memory:' AS books;
CREATE TABLE books.publisher (publisher_id INTEGER PRIMARY KEY,publisher_name TEXT);
CREATE TABLE books.book (book_id INTEGER PRIMARY KEY,publisher_id INTEGER);
INSERT INTO books.publisher VALUES (1,'同名出版社'),(2,'同名出版社'),(3,'正确出版社'),(4,'并列出版社');
INSERT INTO books.book VALUES (1,1),(2,1),(3,2),(4,2),(5,3),(6,3),(7,3),(8,4),(9,4),(10,4);
""")


def expected_rows(ident,oracle,rows,db):
    if ident==1674:
        classes = {r['class_id']:r for r in oracle['class'].values()}
        students = [r for r in oracle['student'].values() if r['student_status_code']=='ACTIVE'
                    and classes.get(r['class_id'],{}).get('grade_year')==2023]
        return [(r['student_no'],r['student_name']) for r in sorted(students,key=lambda r:r['student_no'])[:25]]
    if ident==1684:
        items = [r for r in oracle['class'].values() if r['grade_year']==2022]
        return [(r['class_id'],r['class_name']) for r in sorted(items,key=lambda r:(r['class_id'],r['class_code']))[:20]]
    if ident==1700:
        departments = {r['department_id']:code[0] for code,r in oracle['department'].items()}
        courses = {r['course_id']:r for r in oracle['course'].values()}
        students = {r['student_id']:r['student_no'] for r in oracle['student'].values()}
        counts = collections.defaultdict(set)
        for row in oracle['enrollment'].values():
            if row['semester_id']==1 and row['is_withdraw']==0:
                course = courses[row['course_id']]
                counts[(departments[course['department_id']],course['course_code'])].add(students[row['student_id']])
        by_department = collections.defaultdict(list)
        for (dept,code),student_numbers in counts.items():
            course = next(r for r in courses.values() if r['course_code']==code)
            by_department[dept].append((course['department_id'],course['course_id'],course['course_name'],len(student_numbers),code))
        selected = [row for items in by_department.values() for row in sorted(items,key=lambda r:(-r[3],r[1],r[4]))[:3]]
        return [row[:4] for row in sorted(selected,key=lambda r:(-r[0],-r[3],r[1],r[4]))]
    if ident==1703:
        credits = [r['credit'] for r in oracle['course'].values() if r['credit'] is not None]
        average = sum(credits)/len(credits) if credits else None
        return [(average,)] if average is not None and average>=3 else []
    if ident==1708:
        items = [r for r in oracle['semester'].values() if '2024' in r['semester_code']]
        return [(r['semester_code'],) for r in sorted(items,key=lambda r:(r['semester_code'],r['academic_year']))[:20]]
    if ident==1891:
        return [(str(year),sum(r['credit']==0 for r in rows[year]['course'])) for year in (2024,2025)]
    if ident==1921:
        a,b = [sum(r['course_type']=='必修' and r['credit'] is not None and r['credit']>=4 for r in rows[year]['course']) for year in (2024,2025)]
        return [(b-a,a,b)]
    if ident==3803:
        publishers = dict(db.execute('SELECT id,publisher_name FROM video_games.publisher'))
        counts = collections.defaultdict(set)
        for game,publisher in db.execute('SELECT game_id,publisher_id FROM video_games.game_publisher'):
            if game is not None:
                counts[publisher].add(game)
        winner = min(counts,key=lambda key:(-len(counts[key]),key))
        return [(publishers[winner],)]
    if ident==3851:
        games = dict(db.execute('SELECT id,game_id FROM video_games.game_publisher'))
        eligible = {games[gp] for gp,year in db.execute('SELECT game_publisher_id,release_year FROM video_games.game_platform') if year==2001 and games.get(gp) is not None}
        return [(len(eligible),)]
    if ident==4009:
        return [(sum(year==0 for year, in db.execute('SELECT Year FROM authors.Paper')),)]
    if ident==5196:
        publishers = dict(db.execute('SELECT publisher_id,publisher_name FROM books.publisher'))
        counts = collections.Counter(publisher for publisher, in db.execute('SELECT publisher_id FROM books.book'))
        winner = min(counts,key=lambda key:(-counts[key],key))
        return [(publishers[winner],)]
    raise ValueError(ident)


def verify(corrections):
    ctx = Context(True)
    rows = correction_rows(ctx.catalog)
    db,oracle = fixture(ctx.catalog,rows)
    extra_databases(db)
    records = []
    for correction in corrections:
        ident = correction['question_ids'][0]
        context = ctx.build(correction['question'])
        prediction = corrected_prediction(context,ctx.catalog)
        expected = expected_rows(ident,oracle,rows,db)
        actual = db.execute(mysql_fixture_sql(prediction['query'])).fetchall()
        record = {'question_ids':correction['question_ids'],'context':context,'prediction':prediction,
                  'expected':expected,'actual':actual,'passed':actual==expected,
                  'prediction_source':'curated_question_correction','model_inference_verified':False}
        save(str(ident)+'.json',record,RUN)
        (RUN/(str(ident)+'.sql')).write_text(prediction['query']+';\n',encoding='utf-8')
        records.append(record)
    boundary = []
    for credit in (2.999,3.0):
        for year in (2024,2025):
            db.execute(f'UPDATE education_{year}.course SET credit=?',(credit,))
        prediction = corrected_prediction(ctx.build(next(c['question'] for c in corrections if c['question_ids'][0]==1703)),ctx.catalog)
        actual = db.execute(prediction['query']).fetchall()
        expected = [] if credit<3 else [(credit,)]
        boundary.append({'credit':credit,'actual':actual,'expected':expected,'passed':actual==expected})
    db.close()
    result = {'groups':len(records),'passed_groups':sum(r['passed'] for r in records),
              'execution_checks':len(records)+len(boundary),'passed_checks':sum(r['passed'] for r in records)+sum(r['passed'] for r in boundary),
              'synthetic_execution_only':True,'new_model_inference_verified':False,'boundaries':boundary,
              'failures':[r for r in records if not r['passed']],
              'correction_data_sha256':hashlib.sha256((CURATED/'question_corrections.json').read_bytes()).hexdigest(),
              'implementation_sha256':hashlib.sha256(b''.join((ROOT/name).read_bytes() for name in
                  ['scripts/correct_conflicts.py','src/text2query/model_pipeline.py','src/text2query/education.py','src/text2query/education_fixture.py'])).hexdigest()}
    save('verification.json',result,RUN)
    if result['passed_checks']!=result['execution_checks']:
        raise ValueError(json.dumps(result,ensure_ascii=False))
    return result


def publish(corrections,verification):
    registry = load(CURATED/'education_conflict_resolutions.json')
    for correction in corrections:
        registry['groups'][correction['group_id']].update({'status':'resolved_by_query_correction',
            'reason':correction['decision'],'correction_file':'data/curated/question_corrections.json',
            'verification_run':str(RUN.relative_to(ROOT)),'synthetic_execution_only':True})
    registry.update({'fully_resolved_groups':55,'remaining_groups':0,'query_corrected_groups':11,
                     'user_trial_groups':0,'query_correction_verification_run':str(RUN.relative_to(ROOT))})
    save('education_conflict_resolutions.json',registry,CURATED)
    save('ambiguous_examples.json',ambiguous_examples(load(OUT/'audit.json')['conflicting_groups']))
    original = load(REPORTS/'冲突明细.json')
    groups = {g['group_id']:g for g in original['groups']}
    lines = ['# 已修正的11组冲突','',
             '依据用户授权，按问题、Schema和专家知识修正原11组待审冲突。加上此前44组年度已处理，原55组均已有明确实现口径。原始候选保留在完整快照中。','',
             '**验证：11/11组、13/13项构造执行检查通过；未重新调用模型，也未在真实业务数据库执行。**','',
             '| 原始题号 | 修正结论 |','|---|---|']
    for c in corrections:
        lines.append('| '+','.join(map(str,c['question_ids']))+' | '+c['decision']+' |')
    for c in corrections:
        ident = c['question_ids'][0]
        lines += ['', '## '+c['group_id'],'',c['question'],'','**采用口径**：'+c['decision'],'',
                  '```sql',c['query']+';','```','',
                  '[完整可执行SQL](../../../artifacts/runs/regression/conflict-corrections-20261009/'+str(ident)+'.sql)。','',
                  '**原始候选**：','']
        for variant in groups[c['group_id']]['variants']:
            lines += ['候选'+variant['label']+'（'+','.join(map(str,variant['question_ids']))+'）：','',
                      '```sql',variant['annotation']['query'],'```','']
        if c['implementation_assumptions']:
            lines += ['**实施约定**：'+c['implementation_assumptions'],'']
    lines += ['## 使用与局限','',
              '修正记录保存在`data/curated/question_corrections.json`。完整题目精确匹配时生成器直接采用已校验修正SQL，来源记录为`curated_question_correction`；其他题目可召回修正示例，由模型按当前参数生成。原训练/验证划分及标注未改写。','',
              '论文题的专家知识自身存在冲突，本次选择发表年份优先；同名发行商/出版社按主键区分，最高值并列时取最小ID。这些是明确的实施选择，不冒充官方判分结论。','',
              '原400条提交文件未重新生成。原55组之外的油轮代码、行政区划字段问题已采用按数据源区分编码、按实际Schema使用id的口径，见[数据一致性处理](../data_consistency.md)，不计入本次11组。','']
    (REPORTS/'已修正冲突报告.md').write_text('\n'.join(lines),encoding='utf-8')
    current = load(REPORTS/'剩余冲突明细.json')
    current.update({'groups':[],'generated_at':datetime.now(timezone.utc).isoformat(),
                    'review_intro':'原55组冲突均已有处理口径；原11组待审项已按用户授权修正，详见已修正冲突报告。',
                    'classification_note':'当前没有待人工决定的冲突组。已修正SQL仅通过本地构造验证，尚无新模型或官方执行成绩。'})
    current['summary'].update({'group_count':0,'record_count':0,'category_counts':{},'source_counts':{},'direct_table_count':0})
    for path in ('data/curated/business_rules.json','data/curated/education_conflict_resolutions.json','data/curated/question_corrections.json','data/curated/schema_adjudications.json'):
        current['input_sha256'][path] = hashlib.sha256((ROOT/path).read_bytes()).hexdigest()
    save('剩余冲突明细.json',current,REPORTS)
    embedded = json.dumps(current,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    template = (ROOT/'scripts/templates/conflict_review.html').read_text().replace('冲突数据人工审核','剩余冲突人工审核')
    (REPORTS/'剩余冲突人工审核.html').write_text(template.replace('__REPORT_JSON__',embedded),encoding='utf-8')
    (REPORTS/'剩余冲突报告.md').write_text('# 当前剩余冲突\n\n当前待人工决定的冲突组为 **0**。原11组已按用户授权修正，见[已修正冲突报告](已修正冲突报告.md)。\n\n全部55组保留原始候选，11组修正记录独立保存。构造验证通过不代表官方执行准确率；原400条提交未重新生成。\n',encoding='utf-8')
    (REPORTS/'剩余冲突审核模板.csv').write_text('组编号,原始题号,问题,剩余差异,年度处理状态,审核状态,最终业务口径,补充说明\n',encoding='utf-8-sig')
    manifest = load(REPORTS/'年度处理校验.json')
    manifest.update({'fully_resolved_groups':55,'remaining_groups':0,'remaining_raw_records':0,
                     'query_corrected_groups':11,'correction_execution_checks':verification['passed_checks'],
                     'new_model_inference_verified':False,'model_fixture_is_historical':True,
                     'correction_verification_run':str(RUN.relative_to(ROOT)),
                     'routing_code_sha256':hashlib.sha256(b''.join((ROOT/name).read_bytes() for name in
                        ['src/text2query/query_rules.py','src/text2query/model_pipeline.py'])).hexdigest()})
    manifest['files'] = {name:hashlib.sha256((REPORTS/name).read_bytes()).hexdigest() for name in
                        ['剩余冲突明细.json','剩余冲突人工审核.html','剩余冲突审核模板.csv','剩余冲突报告.md','已修正冲突报告.md']}
    save('年度处理校验.json',manifest,REPORTS)


if __name__=='__main__':
    corrections = build()
    verification = verify(corrections)
    publish(corrections,verification)
    print(json.dumps(verification,ensure_ascii=False,indent=2))
