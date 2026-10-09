"""Year-aware education views, expanded mechanically into model-generated SQL."""
import re
from .data import references, normalize

ENTITY_INFO = {
    'department':('院系',['department_code']),
    'semester':('学期',['academic_year','semester_code']),
    'teacher':('教师',['teacher_no']),
    'class':('班级',['class_code']),
    'student':('学生',['student_no']),
    'course':('课程',['course_code']),
    'enrollment':('选课',['student_no','course_code','academic_year','semester_code']),
    'exam_score':('成绩分项',['student_no','course_code','academic_year','semester_code','exam_item']),
}
DEPENDENCIES = {
    'department':[], 'semester':[], 'teacher':['department'], 'class':['department'],
    'student':['department','class'], 'course':['department','teacher'],
    'enrollment':['student','course','semester'], 'exam_score':['enrollment'],
}
PARENTS = {
    'teacher':[('department','department_id','department_code')],
    'class':[('department','department_id','department_code')],
    'student':[('department','department_id','department_code'),('class','class_id','class_code')],
    'course':[('department','department_id','department_code'),('teacher','teacher_id','teacher_no')],
    'enrollment':[('student','student_id','student_no'),('course','course_id','course_code'),('semester','semester_id','semester_code')],
    'exam_score':[('enrollment','enrollment_id',None)],
}
EVENT_KEY = ['student_no','course_code','academic_year','semester_code']
DISPLAY_IDENTIFIERS = {'课程编号':'course_id','课程名':'course_name','班级编号':'class_id','班级名称':'class_name',
                       '院系编号':'department_id','学期编号':'semester_id','学期代码':'semester_code',
                       '教师工号':'teacher_no','学号':'student_no','学生姓名':'student_name','学分':'credit',
                       '家庭地区编号':'home_region_id','学籍状态':'student_status_code'}


def education_query_mode(question):
    # Literal values inside a field predicate do not select the database snapshot.
    scope = re.sub(r'(?:学号(?:的)?前缀(?:为|是)|入学年份(?:为|是)|学期(?:的)?代码(?:包含|含有|中含有))[“”"\s]*20\d{2}', '', question)
    scope = re.sub(r'20\d{2}年(?:入学|入校)', '', scope)
    # Dates, date keys and quoted code values do not name a database snapshot.
    scope = re.sub(r'(?<!\d)20\d{2}(?:[-/]\d{1,2}[-/]\d{1,2}|\d{4})(?!\d)', '', scope)
    years = sorted(set(re.findall(r'(?<!\d)20\d{2}(?!\d)',scope)))
    if len(years)>1:
        return {'mode':'separate_years','years':years}
    if years:
        return {'mode':'explicit_year','years':years}
    if re.search(r'今年|去年|前年|明年|上一年|上年|本年|上年度|本年度|[二零〇一三四五六七八九]{4}年',scope):
        return {'mode':'explicit_time','years':[]}
    return {'mode':'merged_latest','years':['2024','2025']}


def entity_columns(catalog, entity):
    table = next(t for t in catalog if t.get('database')=='education_2025' and t['table']==entity)
    return [c['name'] for c in table['columns']]


def dependency_order(entities):
    needed = set()
    def visit(entity):
        if entity in needed:
            return
        for parent in DEPENDENCIES[entity]:
            visit(parent)
        needed.add(entity)
    for entity in entities:
        visit(entity)
    return [entity for entity in ENTITY_INFO if entity in needed]


def _year_rows(entity, year, columns):
    alias = 'r'
    select = [f'{alias}.{c}' for c in columns]+[f'{year} AS _source_year']
    joins = []
    if entity in ('teacher','class','student','course'):
        for parent,foreign_key,business_key in PARENTS.get(entity,[]):
            pa = 'p_'+parent
            select.append(f'{pa}.{business_key} AS _{business_key}')
            joins.append(f'LEFT JOIN education_{year}.{parent} {pa} ON {pa}.{foreign_key}=r.{foreign_key}')
    elif entity in ('enrollment','exam_score'):
        enrollment_alias = 'r' if entity=='enrollment' else 'e'
        if entity=='exam_score':
            joins.append(f'LEFT JOIN education_{year}.enrollment e ON e.enrollment_id=r.enrollment_id')
        for parent,local_id,key in [('student','student_id','student_no'),('course','course_id','course_code'),('semester','semester_id','semester_code')]:
            pa = 'p_'+parent
            joins.append(f'LEFT JOIN education_{year}.{parent} {pa} ON {pa}.{local_id}={enrollment_alias}.{local_id}')
            select.append(f'{pa}.{key} AS {key}')
        select.append('p_semester.academic_year AS academic_year')
    return 'SELECT '+', '.join(select)+f' FROM education_{year}.{entity} r '+' '.join(joins)


def _key_expression(entity, key):
    # Missing/empty identity values stay distinct per source row, never collapse into one NULL group.
    ident = entity+'_id'
    return f"COALESCE(NULLIF(r.{key}, ''), CONCAT('__unmapped:',r._source_year,':',r.{ident}))"


def view_definitions(catalog, entities):
    definitions = []
    for entity in dependency_order(entities):
        columns = entity_columns(catalog,entity)
        primary = entity+'_id'
        keys = ENTITY_INFO[entity][1]
        definitions.append(f'_edu_{entity}_rows AS (\n'+_year_rows(entity,'2025',columns)+'\nUNION ALL\n'+_year_rows(entity,'2024',columns)+'\n)')
        order = ['r._source_year DESC']
        if entity=='exam_score':
            order.append('r.score_date_key DESC')
        order.append(f'r.{primary} DESC')
        partitions = ', '.join(_key_expression(entity,key) for key in keys)
        definitions.append(f'_edu_{entity}_ranked AS (SELECT r.*, ROW_NUMBER() OVER (PARTITION BY {partitions} ORDER BY '+', '.join(order)+f') AS _merge_rank FROM _edu_{entity}_rows r)')
        joins, replacements = [], {}
        if entity in ('teacher','class','student','course'):
            for parent,foreign_key,business_key in PARENTS.get(entity,[]):
                pa = 'm_'+parent
                joins.append(f'LEFT JOIN edu_{parent} {pa} ON {pa}.{business_key}=r._{business_key}')
                replacements[foreign_key] = f'COALESCE({pa}.{foreign_key},r.{foreign_key}) AS {foreign_key}'
        elif entity=='enrollment':
            for parent,foreign_key,business_key in PARENTS[entity]:
                pa = 'm_'+parent
                predicate = f'{pa}.{business_key}=r.{business_key}'
                if parent=='semester':
                    predicate += f' AND {pa}.academic_year=r.academic_year'
                joins.append(f'LEFT JOIN edu_{parent} {pa} ON {predicate}')
                replacements[foreign_key] = f'COALESCE({pa}.{foreign_key},r.{foreign_key}) AS {foreign_key}'
        elif entity=='exam_score':
            predicate = ' AND '.join(f'm_enrollment.{key}=r.{key}' for key in EVENT_KEY)
            joins.append('LEFT JOIN edu_enrollment m_enrollment ON '+predicate)
            replacements['enrollment_id'] = 'COALESCE(m_enrollment.enrollment_id,r.enrollment_id) AS enrollment_id'
        select = [replacements.get(c,'r.'+c) for c in columns]+['r._source_year']
        if entity in ('teacher','class','student','course'):
            select += ['r._'+key for _,_,key in PARENTS.get(entity,[])]
        if entity in ('enrollment','exam_score'):
            select += ['r.'+key for key in EVENT_KEY]
        definitions.append(f'edu_{entity} AS (SELECT '+', '.join(select)+f' FROM _edu_{entity}_ranked r '+' '.join(joins)+' WHERE r._merge_rank=1)')
    return definitions


def logical_views(catalog):
    views = []
    for entity,(title,keys) in ENTITY_INFO.items():
        columns = [dict(c) for t in catalog if t.get('database')=='education_2025' and t['table']==entity for c in t['columns']]
        for column in columns:
            labels = [label for label,name in DISPLAY_IDENTIFIERS.items() if name==column['name']]
            if labels:
                column['definition'] += '；原问题“'+'、'.join(labels)+'”对应本字段，返回时不得换为业务代码'
        additional = ['_source_year']
        if entity in ('teacher','class','student','course'):
            additional += ['_'+key for _,_,key in PARENTS.get(entity,[])]
        if entity in ('enrollment','exam_score'):
            additional += EVENT_KEY
        columns += [{'name':name,'type':'INT' if name in ('_source_year','academic_year') else 'VARCHAR','definition':'合并视图附加的来源或业务关联字段'} for name in additional]
        views.append({'name':'edu_'+entity,'entity':entity,'title':title,'columns':columns,'identity_keys':keys,
                      'depends_on':['edu_'+x for x in DEPENDENCIES[entity]],
                      'physical_tables':[f'education_{year}.{entity}' for year in ('2024','2025')]})
    return views


def query_contract(question, rules=()):
    returned = question.split('返回',1)[1] if '返回' in question else ''
    returned = re.split(r'并按|，按|按.+(?:升序|降序)',returned)[0]
    display = [name for label,name in DISPLAY_IDENTIFIERS.items() if label in returned]
    numeric = []
    for label,name in [('院系','department_id'),('学期','semester_id'),('班级','class_id'),('课程','course_id')]:
        for number in re.findall(label+r'编号(?:为|是)?\s*(\d+)',question):
            numeric.append({'field':name,'value':int(number),'instruction':label+'编号是'+name+'，不是相应业务code'})
    if '学期一' in question:
        numeric.append({'field':'semester_id','value':1,'instruction':'学期一按semester_id=1筛选，不能用semester_code=1'})
    exclude_withdrawn = bool(re.search(r'成绩|总评|折算|得分|分项|通过率',question)) and not bool(re.search(r'包含退课|含退课|退课(?:的)?(?:学生|记录|选课)',question))
    overrides = [rule for rule in rules if rule.get('kind')=='education_question_override'
                 and normalize(rule.get('question_exact',''))==normalize(question)]
    for rule in overrides:
        display = list(dict.fromkeys(display+rule.get('display_fields',[])))
    return {'display_fields':display,'numeric_filters':numeric,'exclude_withdrawn_scores':exclude_withdrawn,
            'question_overrides':overrides,
            'instruction':'业务代码用于跨年身份、关联和分组；返回列与排序须按问题要求保留数字编号，不额外返回业务代码或辅助列。未要求班级筛选时，保留class_id为NULL或0的学生，不用INNER JOIN丢掉这些学生。question_overrides是用户针对当前题目提供的修正口径，优先于原问句和原始候选中相冲突的过滤条件。'}


def referenced_views(sql):
    return sorted(set(re.findall(r'\b(?:FROM|JOIN)\s+[`"]?(edu_(?:department|semester|teacher|class|student|course|enrollment|exam_score))\b',sql,re.I)))


def expand_prediction(prediction, context, catalog):
    info = context.get('education_merge',{})
    if info.get('mode')!='merged_latest' or prediction.get('db')!='MYSQL' or not isinstance(prediction.get('query'),str):
        return prediction
    query = prediction['query'].strip()
    names = referenced_views(query)
    if not names:
        return prediction
    if re.search(r'\b(?:_edu_\w+|edu_\w+)\s+AS\s*\(',query,re.I):
        raise ValueError('edu_及_edu_前缀为保留视图名，不可在模型SQL中重新定义')
    definitions = view_definitions(catalog,[name.lower()[4:] for name in names])
    if re.match(r'^WITH\s+RECURSIVE\b',query,re.I):
        body = re.sub(r'^WITH\s+RECURSIVE\s+','',query,flags=re.I)
        expanded = 'WITH RECURSIVE '+',\n'.join(definitions)+',\n'+body
    elif re.match(r'^WITH\b',query,re.I):
        body = re.sub(r'^WITH\s+','',query,flags=re.I)
        expanded = 'WITH '+',\n'.join(definitions)+',\n'+body
    else:
        expanded = 'WITH '+',\n'.join(definitions)+'\n'+query
    return {**prediction,'query':expanded}


def merge_context(question, catalog, rules):
    mode = education_query_mode(question)
    if mode['mode']!='merged_latest' or not rules:
        return {}
    return {**mode,'policy_version':'education-latest-v1','views':logical_views(catalog),
            'query_contract':query_contract(question,rules),
            'score_scope_rule':{'knowledge_id':1575,'instruction':'退课记录不计入默认成绩统计与排名；用选课视图is_withdraw筛选。明确要求查看退课成绩时按题意保留。'},
            'instruction':'使用提供的edu_*合并视图写MySQL查询，不自行重建或只查一个年度。程序自动展开为完整的只读CTE SQL。所有实体先选2025整条记录，同身份无2025才补2024；之后执行问题筛选、聚合、排序和LIMIT。实体视图已去重，可以COUNT(*)；不要对两年聚合结果简单相加或平均。有query_contract.question_overrides时优先执行用户针对当前题目给出的修正口径；否则保留问题中的学号前缀、入学年份、代码字符串、数值阈值和排序方向。',
            'joins':['学生与选课：edu_student.student_no=edu_enrollment.student_no',
                     '课程与选课：edu_course.course_code=edu_enrollment.course_code',
                     '学期与选课：academic_year和semester_code同时相等',
                     '选课与成绩：student_no、course_code、academic_year、semester_code四字段同时相等',
                     '跨年统计按业务键分组/去重；输出题目要求的编号可取合并记录保留的本地ID，不把本地ID当跨年唯一身份。不同年度业务实体可能保留相同本地ID。',
                     '成绩先按业务选课的四字段汇总分项，再按课程/学生统计；不同学期保留为不同历史事件。',
                     '同年相同身份选本地自增ID最大记录；成绩先选同年score_date_key最新，日期并列按ID。该顺序作为确定性实现约定。']}


def merge_errors(question, prediction, rules):
    if not rules or education_query_mode(question)['mode']!='merged_latest':
        return []
    if prediction.get('db')!='MYSQL' or not isinstance(prediction.get('query'),str):
        return ['教育年度合并应使用MySQL单源查询']
    query = prediction['query']
    errors = []
    if not referenced_views(query):
        # Teacher-only legacy handcrafted union remains supported for older artifacts.
        if all(rule.get('entity')=='teacher' for rule in rules):
            return errors
        errors.append('教育年度合并需要查询edu_*视图，由程序展开业务身份去重和年度优先逻辑')
    qualified = set(references(query))
    for name in referenced_views(query):
        entity = name.lower()[4:]
        if not {f'education_2024.{entity}',f'education_2025.{entity}'} <= qualified:
            errors.append(f'{name}尚未展开为两年物理表；请通过年度视图展开步骤生成最终SQL')
    return errors


def model_query_errors(prediction, context):
    """Check business-key joins before generic CTE expansion obscures the model SQL."""
    if context.get('education_merge',{}).get('mode')!='merged_latest' or prediction.get('db')!='MYSQL':
        return []
    sql = prediction.get('query','')
    if not isinstance(sql,str):
        return []
    aliases = {}
    for entity,alias in re.findall(r'\b(?:FROM|JOIN)\s+edu_(\w+)(?:\s+AS)?\s+(\w+)',sql,re.I):
        if entity in ENTITY_INFO and alias.upper() not in {'WHERE','JOIN','LEFT','RIGHT','INNER','ON','GROUP','ORDER','HAVING','LIMIT','UNION'}:
            aliases[alias] = entity
    errors = []
    contract = query_contract(context['question'],context.get('business_rules',[]))
    for field in contract['display_fields']:
        if not re.search(r'\b'+field+r'\b',sql,re.I):
            errors.append(f'问题要求返回{field}，当前模型SQL没有该字段；业务代码用于关联，不替代要求的返回编号')
    for override in contract['question_overrides']:
        for condition in override.get('required_filters',[]):
            field,value = condition['field'],condition['value']
            literal = str(value) if isinstance(value,int) else "'"+str(value).replace("'","''")+"'"
            if not re.search(r'\b'+re.escape(field)+r'\s*=\s*'+re.escape(literal)+r'(?![\w])',sql,re.I):
                errors.append('用户修正口径要求筛选'+field+'='+literal)
        for field in override.get('forbid_like_fields',[]):
            if re.search(r'\b'+re.escape(field)+r'\s+(?:NOT\s+)?LIKE\b',sql,re.I):
                errors.append('用户已取消'+field+'的LIKE前缀过滤，应使用班级grade_year筛选')
        if override.get('class_cohort_filter') and 'edu_class' not in referenced_views(sql):
            errors.append('grade_year属于班级视图，需关联edu_student与edu_class，不能从学生字段推断届别')
    if contract['exclude_withdrawn_scores'] and 'edu_exam_score' in sql and not re.search(r'\bis_withdraw\b',sql,re.I):
        errors.append('按知识1575，默认成绩统计与排名不计退课，需关联选课业务身份并筛选is_withdraw=0')
    for condition in contract['numeric_filters']:
        field,value = condition['field'],condition['value']
        code = {'semester_id':'semester_code','course_id':'course_code','class_id':'class_code','department_id':'department_code'}[field]
        if re.search(r'\b'+code+r"\s*=\s*'?"+str(value)+r"'?(?!\d)",sql,re.I) and not re.search(r'\b'+field+r'\s*=\s*'+str(value)+r'(?!\d)',sql,re.I):
            errors.append(condition['instruction'])
    for first,field,second in re.findall(r'\b(\w+)\.(\w+_id)\s*=\s*(\w+)\.\2\b',sql,re.I):
        if aliases.get(first) and aliases.get(second) and aliases[first]!=aliases[second]:
            errors.append(f'合并视图不能以本地{field}跨实体关联：{first}与{second}应使用业务身份字段')
    for expression in re.findall(r'COUNT\s*\(\s*DISTINCT\s+([^)]*)\)',sql,re.I):
        for entity in ENTITY_INFO:
            field = entity+'_id'
            if re.search(r'\b'+field+r'\b',expression,re.I):
                errors.append(f'跨年COUNT(DISTINCT {field})可能合并不同业务身份；应使用业务键，已去重实体视图也可COUNT(*)')
    for clause in re.findall(r'\b(?:GROUP|PARTITION)\s+BY\s+(.*?)(?=\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|\)|$)',sql,re.I|re.S):
        for field,key in [('course_id','course_code'),('student_id','student_no'),('class_id','_class_code'),('department_id','_department_code'),('semester_id','semester_code')]:
            allowed = r'(?:'+key+'|'+key.lstrip('_')+')'
            if re.search(r'\b'+field+r'\b',clause,re.I) and not re.search(r'\b'+allowed+r'\b',clause,re.I):
                errors.append(f'跨年按{field}分组时也需包含业务身份{key}，防止不同实体的本地ID碰撞')
        if re.search(r'\bsemester_code\b',clause,re.I) and not re.search(r'\bacademic_year\b',clause,re.I):
            errors.append('跨年学期身份为academic_year和semester_code，分组需同时包含两字段')
        if re.search(r'\benrollment_id\b',clause,re.I) or ('edu_exam_score' in sql and re.search(r'\bcourse_code\b',clause,re.I) and re.search(r'\bstudent_no\b',clause,re.I)):
            if not all(re.search(r'\b'+key+r'\b',clause,re.I) for key in EVENT_KEY):
                errors.append('按业务选课汇总成绩时GROUP BY需含student_no、course_code、academic_year、semester_code，避免把不同学期或同编号的事件合并')
    return list(dict.fromkeys(errors))
