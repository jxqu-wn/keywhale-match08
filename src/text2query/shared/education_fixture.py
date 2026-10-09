"""Independent Python merge oracle and adversarial synthetic education data."""
import sqlite3
import re
from .education import ENTITY_INFO, PARENTS, EVENT_KEY, entity_columns


def mysql_fixture_sql(sql):
    """Adapt integer-column = '' comparisons to MySQL's numeric comparison semantics."""
    return re.sub(r"\b((?:\w+\.)?(?:class_id|home_region_id|department_id|semester_id))\s*=\s*''",r'\1=0',sql,flags=re.I)


def data_rows(catalog):
    rows = {year:{entity:[] for entity in ENTITY_INFO} for year in (2024,2025)}
    def add(year,entity,**values):
        row = {name:None for name in entity_columns(catalog,entity)}
        row.update(values)
        rows[year][entity].append(row)
    for ident in (1,2,3,4,5,6,9):
        code = f'D{ident}'
        add(2024,'department',department_id=ident,department_code=code,department_name=code)
        if ident!=9:
            latest_ident = {1:2,2:1}.get(ident,ident)
            add(2025,'department',department_id=latest_ident,department_code=code,department_name=code+'新')
    for year,ident,code,academic in [(2024,1,'2025-1',2025),(2024,2,'2025-2',2025),(2024,9,'2024-1',2024),
                                     (2025,2,'2025-1',2025),(2025,1,'2025-2',2025),(2025,3,'2026-1',2026)]:
        add(year,'semester',semester_id=ident,semester_code=code,academic_year=academic,start_date_key=academic*10000+101,end_date_key=academic*10000+630)
    for year,ident,no,dept,phone in [(2024,1,'T1',1,'100'),(2024,2,'T2',3,'200'),(2024,9,'T9',5,'900'),
                                   (2025,2,'T1',2,'100'),(2025,99,'T1',2,None),(2025,1,'T3',5,'300'),
                                   (2025,4,'T4',4,'400'),(2025,5,'T4',4,'400')]:
        add(year,'teacher',teacher_id=ident,teacher_no=no,teacher_name='师'+no,department_id=dept,gender_code='F',phone=phone)
    for year,ident,code,dept,grade in [(2024,1,'CL1',1,2022),(2024,2,'CL2',2,2023),(2024,9,'CL9',9,2022),
                                      (2025,2,'CL1',2,2022),(2025,1,'CL2',1,2023),(2025,3,'CL3',6,2022)]:
        add(year,'class',class_id=ident,class_code=code,class_name=code,department_id=dept,grade_year=grade)
    students = {
        2024:[(1,'2023001','张甲','F',1,1,1,'ACTIVE'),(2,'2023002','张乙','F',2,2,2,'ACTIVE'),
              (3,'S3','李丙','M',1,1,3,'ACTIVE'),(9,'2023009','张旧','F',9,9,9,'ACTIVE'),
              (8,'SOLD','李旧','F',1,0,0,'ACTIVE')],
        2025:[(2,'2023001','张甲新','M',2,2,None,'INACTIVE'),(1,'2023002','张乙新','F',1,1,0,'ACTIVE'),
              (3,'S3','李丙新','M',2,2,2,'ACTIVE'),(4,'2023004','王丁','M',6,0,3,'ACTIVE'),
              (5,'2023005','赵戊旧','F',1,1,5,'ACTIVE'),(105,'2023005','赵戊新','F',1,1,5,'ACTIVE')],
    }
    for year,values in students.items():
        for ident,no,name,gender,dept,cl,region,status in values:
            add(year,'student',student_id=ident,student_no=no,student_name=name,gender_code=gender,
                department_id=dept,class_id=cl,home_region_id=region,student_status_code=status,academic_level_code='UG',admit_date_key=20230101)
    courses = {
        2024:[(1,'C1','数据库概论',1,1,5,'必修'),(2,'C2','数据结构',2,2,3,'必修'),
              (3,'C3','数学',3,9,0,'选修'),(9,'C9','数论',6,9,1,'选修')],
        2025:[(2,'C1','数据库系统',2,99,6,'必修'),(1,'C2','数据结构',1,1,4,'必修'),
              (3,'C3','数学新',3,5,2,'选修'),(4,'C4','数据库实验',6,5,3,'选修'),(5,'C5','零学分',5,1,0,'必修')],
    }
    for year,values in courses.items():
        for ident,code,name,dept,teacher,credit,kind in values:
            add(year,'course',course_id=ident,course_code=code,course_name=name,department_id=dept,
                teacher_id=teacher,credit=float(credit),course_type=kind,max_score=100)
    enrollments = {
        2024:[(1,1,1,1,0),(2,2,1,1,0),(30,2,2,1,0),(40,9,3,9,0),(90,2,1,9,0),
              (5,1,1,2,0),(6,2,1,2,0),(7,2,2,2,0),(8,3,3,2,0)],
        2025:[(2,2,2,2,1),(1,1,2,2,0),(3,3,1,1,0),(4,2,2,1,0),
              (5,1,2,1,0),(15,1,2,1,0),(6,1,1,1,0),(7,3,3,1,0),(8,105,4,1,0),(9,4,5,1,1)],
    }
    totals = {2024:{1:90,2:40,30:70,40:80,90:75,5:60,6:30,7:60,8:40},
              2025:{2:85,1:60,3:60,4:90,5:20,15:80,6:50,7:90,8:70,9:100}}
    for year,values in enrollments.items():
        for ident,student,course,semester,withdraw in values:
            add(year,'enrollment',enrollment_id=ident,student_id=student,course_id=course,semester_id=semester,
                enroll_date_key=year*10000+101,take_mode='NORMAL',is_withdraw=withdraw)
            for item_index,item in enumerate(('平时','期中','期末'),1):
                add(year,'exam_score',exam_score_id=year*1000+ident*10+item_index,enrollment_id=ident,
                    exam_item=item,score=float(totals[year][ident]),score_date_key=year*10000+630)
    return rows


def merge_oracle(rows):
    """Resolve source records in Python before writing ordinary canonical tables."""
    original = {year:{entity:{row[entity+'_id']:row for row in items} for entity,items in tables.items()}
                for year,tables in rows.items()}
    identities = {year:{} for year in rows}
    winners = {entity:{} for entity in ENTITY_INFO}
    for entity in ENTITY_INFO:
        for year,tables in rows.items():
            identities[year][entity] = {}
            for row in tables[entity]:
                if entity not in ('enrollment','exam_score'):
                    key = tuple(row[field] for field in ENTITY_INFO[entity][1])
                else:
                    enrollment = row if entity=='enrollment' else original[year]['enrollment'][row['enrollment_id']]
                    student = original[year]['student'][enrollment['student_id']]
                    course = original[year]['course'][enrollment['course_id']]
                    semester = original[year]['semester'][enrollment['semester_id']]
                    key = (student['student_no'],course['course_code'],semester['academic_year'],semester['semester_code'])
                    if entity=='exam_score':
                        key += (row['exam_item'],)
                identities[year][entity][row[entity+'_id']] = key
                order = (year,row.get('score_date_key') or 0,row[entity+'_id'])
                if key not in winners[entity] or order>winners[entity][key][0]:
                    winners[entity][key] = (order,year,row)
    canonical = {entity:{} for entity in ENTITY_INFO}
    for entity in ENTITY_INFO:
        for key,(_,year,raw) in winners[entity].items():
            row = dict(raw)
            for parent,local_id,_ in PARENTS.get(entity,[]):
                parent_key = identities[year][parent].get(raw[local_id])
                if parent_key in canonical[parent]:
                    row[local_id] = canonical[parent][parent_key][parent+'_id']
            canonical[entity][key] = row
        # This fixture keeps chosen public IDs distinct; original yearly IDs are deliberately swapped.
        assert len({r[entity+'_id'] for r in canonical[entity].values()})==len(canonical[entity])
    return canonical


def student_cohort_rows(catalog):
    """Students whose number prefix differs from their latest class cohort."""
    rows = data_rows(catalog)
    template = rows[2025]['student'][1]
    for index in range(30):
        rows[2025]['student'].append({**template,'student_id':200+index,
            'student_no':f'S2023{index:03d}','student_name':f'2023级学生{index:02d}'})
    rows[2025]['student'].append({**template,'student_id':999,
        'student_no':'S2023000','student_name':'同年修订姓名'})
    rows[2025]['student'].append({**template,'student_id':300,
        'student_no':'2023999','student_name':'前缀符合但班级2022级','class_id':2})
    rows[2025]['student'].append({**template,'student_id':301,
        'student_no':'2023000','student_name':'2023级但已非在读','student_status_code':'INACTIVE'})
    old_template = rows[2024]['student'][1]
    rows[2024]['student'].append({**old_template,'student_id':302,
        'student_no':'2023003','student_name':'仅旧年存在的2023级学生'})
    old_class = rows[2024]['class'][1]
    new_class = rows[2025]['class'][1]
    rows[2024]['class'].append({**old_class,'class_id':10,'class_code':'CLCHANGE','grade_year':2023})
    rows[2025]['class'].append({**new_class,'class_id':10,'class_code':'CLCHANGE','grade_year':2022})
    rows[2024]['student'].append({**old_template,'student_id':303,
        'student_no':'2023006','student_name':'所属班级新年改为2022级','class_id':10})
    return rows


def fixture(catalog, rows=None):
    conn = sqlite3.connect(':memory:')
    conn.create_function('CONCAT',-1,lambda *args: ''.join(map(str,args)) if all(x is not None for x in args) else None)
    conn.create_function('MOD',2,lambda a,b:a%b if a is not None and b else None)
    # MySQL division by an integer NULLIF denominator is real division.
    conn.create_function('NULLIF',2,lambda a,b:None if a==b else float(a) if isinstance(a,(int,float)) else a)
    rows = data_rows(catalog) if rows is None else rows
    canonical = merge_oracle(rows)
    for database,data in [('education_2024',rows[2024]),('education_2025',rows[2025]),
                          ('oracle',{e:list(items.values()) for e,items in canonical.items()})]:
        conn.execute(f"ATTACH DATABASE ':memory:' AS {database}")
        for entity,items in data.items():
            cols = entity_columns(catalog,entity)
            definitions = ','.join(f'"{name}" '+('REAL' if name in ('score','credit') else 'INTEGER' if name.endswith('_id') or name.endswith('_key') or name in ('academic_year','grade_year','is_withdraw','max_score') else 'TEXT') for name in cols)
            conn.execute(f'CREATE TABLE {database}."{entity}" ({definitions})')
            marks = ','.join('?' for _ in cols)
            conn.executemany(f'INSERT INTO {database}."{entity}" VALUES ({marks})',[[row[c] for c in cols] for row in items])
    return conn,canonical
