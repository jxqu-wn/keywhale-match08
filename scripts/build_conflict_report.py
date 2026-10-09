"""Build a read-only conflict report and blank business-review artifacts."""
import collections
import csv
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text2query.shared.data import load, normalize, queries, references, source
from text2query.shared.paths import ROOT, OUT, RAW, CURATED
from text2query.shared.query_rules import applicable_business_rules

DEST = ROOT/'docs/reports/conflicts'
CATEGORIES = {
    'annual_database':('仅年度库不同','P1'),
    'question_mismatch':('年度库不同且标注与问题不一致','P0'),
    'business_definition':('业务定义或过滤范围不同','P0'),
    'likely_equivalent':('疑似SQL等价写法','P2'),
}
REVIEW_FIELDS = {
    'status':'审核状态', 'handling':'处理方式', 'identity_key':'业务身份键',
    'latest_policy':'新旧记录优先规则', 'history_policy':'历史记录保留规则',
    'filter_order':'合并与筛选顺序', 'join_policy':'跨表关联方法',
    'parameter_corrections':'问题参数与标注修正', 'result_policy':'结果粒度/并列/空值规则',
    'notes':'补充说明',
}
PROFILES = {
    'teacher':('教师', 'teacher_no（业务身份候选；未声明UNIQUE）',
        '用户已提供两年合并、2025整条记录优先的口径。确认编号跨年稳定，以及2025不存在时是否确为旧年补充而非已离职教师；电话空值不回退旧值。'),
    'student':('学生', 'student_no（候选；未声明UNIQUE）',
        '确认同一学号是否跨年对应同一人，以及毕业/离校学生是否应纳入默认查询。先选择身份记录再筛选学籍、性别、地区，还是查询历史状态？'),
    'course':('课程', 'course_code（候选；未声明UNIQUE）',
        '确认课程是稳定定义还是每年开课实例。学分、任课教师、max_score可能改变；历史成绩折算应使用对应年度课程属性。'),
    'class':('班级', 'class_code（候选；未声明UNIQUE）',
        '确认班级代码是否跨年复用。grade_year是入学年份条件，与年度库选择分别判断。'),
    'semester':('学期', 'academic_year + semester_code（候选）',
        '确认学期代码唯一范围、两年库是否重复保存同一学期，以及semester_id=1在两库是否指同一学期。不得仅按本地自增编号跨年去重。'),
    'enrollment':('选课记录', '业务学生身份 + 课程身份 + 学期身份；可能还需重修/选课批次',
        '选课是历史事件，不等于学生快照。跨学期选课不能按学生编号合并为一条；确认重修、重复选课、退课与补录的保留和覆盖规则。'),
    'exam_score':('成绩分项', '业务选课身份 + exam_item；是否包含score_date_key需确认',
        '确认补考、重考、补录是新增事件还是修订原分项。先映射年度内enrollment关联，再判断更新优先级，不用exam_score_id直接跨年连接。'),
    'department':('院系（关联维度）', 'department_code（候选；未声明UNIQUE）',
        '问题中的院系编号可能是年度本地ID。合并跨年数据前须确认ID一致性或按业务代码映射。'),
}
SPECIAL = {
    1674:('question_mismatch',
        '问题要求学号前缀2023、升序；2024候选用S0001%且升序，2025候选用S0009%且降序。两条均未按原文前缀筛选，2025候选还改变排序。',
        ['确认2023确为学号文字前缀，还是入学年份概念；按原文两条LIKE均不符。',
         '选择年度口径后仍须独立保留问题要求的升序和前25名。']),
    1684:('question_mismatch',
        '问题要求入学年份2022、班级编号升序；候选分别使用grade_year=2024/2025，2025候选还改为降序。',
        ['确认过滤字段为grade_year并将问题参数2022与年度库选择区分。',
         '候选均有参数偏移，不能仅挑选较新的候选作为最终逻辑。']),
    1703:('question_mismatch',
        '问题要求平均学分大于等于3；2024候选HAVING ROUND(AVG(credit),2)<=4，2025候选使用>=2。两条均未使用要求的>=3。',
        ['确认应比较原始平均值还是四舍五入后的平均值。',
         '先确认年度与去重口径，再修正阈值和比较方向。']),
    1708:('question_mismatch',
        '问题要求semester_code包含2024；2024候选LIKE %2024%，2025候选改为%2025%。2025候选改变了问题字符串参数。',
        ['查询参数2024不自动等于只允许访问education_2024库。',
         '确认两年是否保存同一学期；即使访问2025库，也应保留原问题的代码筛选参数。']),
    1891:('likely_equivalent',
        '两候选均分别返回两年credit=0课程数，只将结果列别名credit_zero_course_cnt改为credit_0_course_cnt；3条标注使用前者，1条使用后者。',
        ['问题明确要求分别返回两年，不应合并成一个去重总量。',
         '确认结果比较是否关注列名和行顺序；未执行真实数据库，当前仅判断为疑似等价。']),
    1921:('likely_equivalent',
        '差值表达式b.cnt_2025-a.cnt_2024有无外层括号不同，运算及结果列相同。',
        ['保持2025减2024及两年数量，不采用统一最新快照替代对比。',
         '确认真实执行结果后可作为等价写法处理。']),
    3803:('likely_equivalent',
        '一条使用派生表包裹聚合，另一条直接聚合；均按发行商名称分组、COUNT(DISTINCT game_id)降序取1条。',
        ['两个查询看起来统计同一对象，尚未执行真实库证明等价。',
         '确认发行商同名是否合并，以及第一名并列时是否返回全部或按稳定键选一个。']),
    3851:('likely_equivalent',
        '仅COUNT(T.id)与COUNT(id)的限定符不同，均统计2001年game_platform记录。',
        ['两条查询彼此看似等价，但游戏平台记录数未必等于去重游戏款数。',
         '知识123明确id是游戏平台记录标识；若要游戏款数，应确认是否沿game_publisher关联按game_id去重。']),
    4009:('business_definition',
        '候选A只计Year=0；候选B计Year=0 OR (ConferenceId=0 AND JournalId=0)，后者可额外包含Year非0但没有会议/期刊标识的论文。',
        ['确认“预印本或未发表”按年份、发表载体缺失、OR还是AND判断。',
         '知识85/129/134分别提供不同描述，需确认其为等价信号还是须同时满足的条件。',
         '确认Year为NULL、载体ID为-1以及Year>0但载体均0的数据如何处理；当前无实际数据分布。']),
    5196:('likely_equivalent',
        '同一INNER JOIN内按出版社名称分组；计数从COUNT(book.book_id)换为COUNT(publisher.publisher_id)。结合主键与内连接，计数看似相同。',
        ['真实库中验证约束与结果后，再认定等价。',
         '确认出版社同名是否合并，以及出版数量最高并列时返回谁。']),
}


def md(text):
    return str(text).replace('|','\\|').replace('\n','<br>').replace('\r','')


def schema_closure(table_names, catalog):
    by_name = {f"{t.get('database')}.{t['table']}":t for t in catalog}
    found = set(table_names)
    pending = list(found)
    while pending:
        name = pending.pop()
        table = by_name.get(name)
        if not table:
            continue
        for ref in re.findall(r'\bREFERENCES\s+["`]?([\w.]+)',table.get('ddl',''),re.I):
            target = ref if '.' in ref else str(table['database'])+'.'+ref
            if target in by_name and target not in found:
                found.add(target)
                pending.append(target)
    return sorted(found)


def group_evidence(rows, catalog, knowledge, rules):
    first_id = rows[0]['question_id']
    original = [dict(row) for row in rows]
    variants = {}
    for row in rows:
        annotation = {k:v for k,v in row.items() if k not in ('question','question_id')}
        key = json.dumps(annotation,sort_keys=True,ensure_ascii=False)
        if key not in variants:
            variants[key] = {'label':chr(65+len(variants)),'question_ids':[], 'annotation':annotation,
                             'tables':sorted({r for q in queries(row) for r in references(q)})}
        variants[key]['question_ids'].append(row['question_id'])
    variants = list(variants.values())
    tables = sorted({t for variant in variants for t in variant['tables']})
    if first_id in SPECIAL:
        category, difference, checks = SPECIAL[first_id]
    else:
        signatures = {re.sub(r'education_20\d{2}\.', 'education_<YEAR>.',q) for row in rows for q in queries(row)}
        if len(signatures)!=1:
            raise ValueError(f'Unclassified conflict {first_id}; review differences before publishing')
        category = 'annual_database'
        difference = '除education_2024与education_2025数据库限定符外，候选SQL文本完全相同；问题未明确查询年度库。'
        checks = ['确认年度范围：仅最新年度、指定年度、两年合并去重、还是分别统计。',
                  '如合并，确认业务身份键、年度本地ID映射、重复/更新记录及缺失记录的含义。']
    entities = sorted({t.split('.')[-1] for t in tables if t.startswith('education_')})
    if len(entities)>1:
        checks.append('多表查询先保持年度内关联，再映射到跨年业务键；不要把两库的本地自增ID直接连接。')
    sql = ' '.join(q for row in rows for q in queries(row))
    if 'semester_id' in sql:
        checks.append('核实两库semester_id=1是否表示同一学期，必要时使用academic_year/semester_code映射。')
    if re.search(r'ORDER\s+BY|LIMIT',sql,re.I):
        checks.append('若合并年度，确认在完整合并和去重后排序/取前N，以及并列时的稳定顺序。')
    if re.search(r'AVG\s*\(',sql,re.I):
        checks.append('平均值应按选定口径的明细重算，不能直接平均两个年度的平均值；确认NULL和四舍五入。')
    if re.search(r'COUNT\s*\(',sql,re.I):
        checks.append('确认数量指实体去重人数/门数、明细记录数还是历史事件数。')
    knowledge_ids = []
    if re.search('总评|折算|分项',rows[0]['question']):
        knowledge_ids.append(1572)
    if '通过' in rows[0]['question']:
        knowledge_ids.append(1573)
    if 'is_withdraw' in sql:
        knowledge_ids.append(1575)
    if '总分' in rows[0]['question']:
        knowledge_ids.append(1579)
    knowledge_ids.extend({3803:[121],3851:[120,123,1539],4009:[85,129,134,405,407]}.get(first_id,[]))
    existing_rules = applicable_business_rules(rows[0]['question'],rules)
    return {'group_id':'ambiguous-'+str(first_id),'question':rows[0]['question'],
            'question_variants':list(dict.fromkeys(row['question'] for row in rows)),
            'question_ids':[row['question_id'] for row in rows], 'source':source(rows[0]),
            'category':category,'category_label':CATEGORIES[category][0],'priority':CATEGORIES[category][1],
            'difference':difference,'review_questions':checks,'entities':entities,'tables':tables,
            'schema_refs':schema_closure(tables,catalog),'variants':variants,'original_records':original,
            'knowledge':[x for x in knowledge if x['knowledge_id'] in knowledge_ids],
            'existing_user_rules':existing_rules,
            'review':{key:('待审核' if key=='status' else '') for key in REVIEW_FIELDS}}


def build_markdown(report):
    groups = report['groups']
    lines = ['# 冲突数据人工审核报告','',
        f"本报告覆盖 **{len(groups)}组、{report['summary']['record_count']}条原始训练标注**。生成时间：{report['generated_at']}。分组按问题去除标点和空白后的文本一致，标签按原始字段精确比较；SQL文本不同不自动意味着业务冲突。",'',
        '全部候选均来自原始训练数据，未改写为人工生成答案；分类和待确认事项是本地分析。未执行真实业务数据库，不据此判定最终正确答案。审核内容仅保存到报告及审核文件，不自动改写当前业务规则、RAG或提交结果。','',
        '可用同目录的 `冲突人工审核.html` 逐组筛选、填写并导出审核结果；也可填写 `人工审核模板.csv`。`冲突明细.json` 保留原始记录、完整候选与表结构。请把填写后的文件另存，重建报告会重建空白模板。','',
        '## 冲突概况','', '| 分析类别 | 组数 | 原始标注数 | 优先级 |','|---|---:|---:|---|']
    for key,(label,priority) in CATEGORIES.items():
        subset = [g for g in groups if g['category']==key]
        lines.append(f"| {label} | {len(subset)} | {sum(len(g['question_ids']) for g in subset)} | {priority} |")
    lines += ['',f"数据源：MySQL {report['summary']['source_counts'].get('MYSQL',0)}条，SQLite {report['summary']['source_counts'].get('SQLite',0)}条。每组均有2种不同候选；其中重复标注已在同一候选下合并展示，113个原始题号全部保留。",'',
        '优先审核P0的5组：四组问题参数/排序错误（1674、1684、1703、1708）和论文定义差异（4009）。P1可按实体先统一口径，再逐组检查条件和结果粒度。P2先判断是否等价，不必因SQL写法不同就重定义业务。','',
        '## 已有用户口径与待确认范围','',
        '教师规则来源于用户补充，已保存在 `data/curated/business_rules.json`：默认查询合并2024/2025，2025整条记录优先，旧年只补新年不存在的教师，先合并再筛选，计数按教师身份去重。其触发范围覆盖本报告5个教师组。`teacher_no`是当前实现采用的身份键，Schema未声明UNIQUE，跨年稳定性仍待确认。','',
        '学生、课程、班级、学期、选课、成绩没有被自动赋予同样的业务口径。年度新库未出现旧实体，可能表示缺失、删除、离校或停开，需要由业务定义判断是否应从旧年补回。','',
        '## 按实体确认年度逻辑','',
        '| 实体 | 相关冲突组数 | 跨年身份候选（未验证） | 需确认的业务逻辑 |','|---|---:|---|---|']
    for entity,(title,key,note) in PROFILES.items():
        count = sum(entity in g['entities'] or (entity=='department' and any('department_id' in q for v in g['variants'] for q in queries(v['annotation']))) for g in groups)
        lines.append(f'| {title} | {count} | {md(key)} | {md(note)} |')
    lines += ['', '实体组数会重叠，不能相加。业务键仅为Schema支持的候选，不是唯一性证明。年度自增主键和外键需先在各年度内解析，再转换成跨年业务身份。','',
        '通用审核顺序：年度范围 → 实体/事件粒度 → 去重键与更新优先级 → 跨表身份映射 → 条件筛选顺序 → 聚合 → 排序/并列/取前N。已有“分别比较两年”的问题保持年度维度，不合并成一个总值。','',
        '## 全部冲突索引','',
        '| 组 | 优先级 | 类别 | 原始题号 | 问题 |','|---|---|---|---|---|']
    for number,g in enumerate(groups,1):
        lines.append(f"| [G{number:02d}](#{g['group_id']}) | {g['priority']} | {g['category_label']} | {','.join(map(str,g['question_ids']))} | {md(g['question'])} |")
    lines += ['', '## 逐组详情','']
    for number,g in enumerate(groups,1):
        lines += [f'<a id="{g["group_id"]}"></a>','',f'### G{number:02d} · {g["group_id"]} · {g["priority"]}', '',
                  f"**问题**：{g['question']}",'', f"**原始题号**：{', '.join(map(str,g['question_ids']))}；**数据源**：{g['source']}；**分类**：{g['category_label']}。",'',
                  f"**差异分析**：{g['difference']}",'']
        if len(g['question_variants'])>1:
            lines += ['**原始问题写法**：','']+[f'- {q}' for q in g['question_variants']]+['']
        for v in g['variants']:
            lines += [f"**候选{v['label']}**（题号：{', '.join(map(str,v['question_ids']))}）",'',
                      '涉及库表：'+', '.join('`'+t+'`' for t in v['tables'])+'。','',
                      '```sql', *queries(v['annotation']), '```','']
        lines += ['**Schema依据**：','', '| 库表 | 字段（名称:类型） |','|---|---|']
        schemas = {f"{t.get('database')}.{t['table']}":t for t in report['schemas']}
        for name in g['tables']:
            table = schemas[name]
            columns = ', '.join(c['name']+':'+c['type'] for c in table.get('columns',[]))
            lines.append(f'| `{name}` | {md(columns)} |')
        lines += ['', '完整DDL及外键见文末Schema附录，关联维度也已收录。','']
        if g['knowledge']:
            lines += ['**相关专家知识（原文）**：','']
            lines += [f"- 知识{x['knowledge_id']}：{x['knowledge_text']}" for x in g['knowledge']]+['']
        if g['existing_user_rules']:
            lines += ['**已有用户口径（不是官方唯一答案标注）**：','']
            lines += [f"- {r['instruction']}" for r in g['existing_user_rules']]+['']
        lines += ['**人工需确认**：','']+[f'- {q}' for q in g['review_questions']]+['',
                  '**人工决定（待填写）**：','',
                  '- 年度范围/处理方式：____','- 业务身份键、历史事件粒度：____',
                  '- 新旧优先、缺失/空值处理：____','- 跨表映射、筛选顺序：____',
                  '- 参数修正、排序/并列规则：____','- 最终口径与说明：____','']
    lines += ['## Schema附录','', '包括20张查询直接引用表及其外键依赖，DDL来自现有Schema目录，未推断新增约束。','']
    for table in report['schemas']:
        lines += [f"### {table['database']}.{table['table']}",'',
                  f"数据源：{table['source']}；Schema文件：`data/raw/subject/Schema/{table['file']}`。",'',
                  '```sql',table['ddl'].replace('\r\n','\n'),'```','']
    lines += ['## 输入与完整性','', f"- 原始训练数据SHA-256：`{report['input_sha256']['data/raw/subject/QA_train.json']}`", 
              '- `生成校验.json` 记录覆盖、分类统计、原始标注一致性与输出文件校验和。',
              '- 重建命令：`python scripts/build_conflict_report.py`。','']
    return '\n'.join(lines)


def write_csv(report):
    columns = ['组编号','优先级','类别','原始题号','问题','差异分析','涉及库表','候选A题号','候选A查询','候选B题号','候选B查询','需人工确认','已有用户规则']+list(REVIEW_FIELDS.values())
    with (DEST/'人工审核模板.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer = csv.DictWriter(f,fieldnames=columns)
        writer.writeheader()
        for g in report['groups']:
            a,b = g['variants']
            item = {'组编号':g['group_id'],'优先级':g['priority'],'类别':g['category_label'],
                    '原始题号':','.join(map(str,g['question_ids'])),'问题':g['question'],
                    '差异分析':g['difference'],'涉及库表':'\n'.join(g['tables']),
                    '候选A题号':','.join(map(str,a['question_ids'])),'候选A查询':'\n'.join(queries(a['annotation'])),
                    '候选B题号':','.join(map(str,b['question_ids'])),'候选B查询':'\n'.join(queries(b['annotation'])),
                    '需人工确认':'\n'.join(g['review_questions']),
                    '已有用户规则':'\n'.join(r['instruction'] for r in g['existing_user_rules'])}
            item.update({label:g['review'][key] for key,label in REVIEW_FIELDS.items()})
            writer.writerow(item)


def main():
    raw = load(RAW/'QA_train.json')
    audit = load(OUT/'audit.json')
    catalog, knowledge = load(OUT/'catalog.json'),load(OUT/'knowledge.json')
    rules = load(CURATED/'business_rules.json')
    groups = [group_evidence(rows,catalog,knowledge,rules) for rows in audit['conflicting_groups']]
    table_names = sorted({name for g in groups for name in g['schema_refs']})
    inputs = [RAW/'QA_train.json',OUT/'audit.json',OUT/'catalog.json',OUT/'knowledge.json',CURATED/'business_rules.json']
    summary = {'group_count':len(groups),'record_count':sum(len(g['question_ids']) for g in groups),
               'category_counts':dict(collections.Counter(g['category'] for g in groups)),
               'source_counts':dict(collections.Counter(source(row) for g in groups for row in g['original_records'])),
               'direct_table_count':len({t for g in groups for t in g['tables']}),
               'schema_with_dependencies_count':len(table_names),
               'existing_teacher_rule_group_count':sum(bool(g['existing_user_rules']) for g in groups)}
    report = {'generated_at':datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
              'input_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
              'summary':summary,'review_fields':REVIEW_FIELDS,'groups':groups,
              'schemas':[t for t in catalog if f"{t.get('database')}.{t['table']}" in table_names],
              'entity_profiles':[{'entity':entity,'title':v[0],'candidate_key':v[1],'notes':v[2]} for entity,v in PROFILES.items()]}
    raw_index = {row['question_id']:row for row in raw}
    report_records = [row for group in groups for row in group['original_records']]
    assert len({row['question_id'] for row in report_records})==len(report_records)
    assert all(row==raw_index[row['question_id']] for row in report_records)
    assert {row['question_id'] for row in report_records}=={row['question_id'] for group in audit['conflicting_groups'] for row in group}
    assert all(len(g['variants'])==2 for g in groups)
    assert all(len({normalize(row['question']) for row in g['original_records']})==1 for g in groups)
    assert all(set(g['tables']) <= set(g['schema_refs']) for g in groups)
    assert len(report['schemas'])==len(table_names)
    DEST.mkdir(parents=True,exist_ok=True)
    (DEST/'冲突明细.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (DEST/'冲突数据报告.md').write_text(build_markdown(report),encoding='utf-8')
    write_csv(report)
    template = (Path(__file__).parent/'templates/conflict_review.html').read_text(encoding='utf-8')
    payload = json.dumps(report,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    assert template.count('__REPORT_JSON__')==1
    (DEST/'冲突人工审核.html').write_text(template.replace('__REPORT_JSON__',payload),encoding='utf-8')
    verification = {'summary':summary,'original_records_match':True,'unique_ids':len(report_records),
                    'candidate_count':sum(len(g['variants']) for g in groups),'real_database_execution':False,
                    'automatic_business_rule_changes':False,
                    'files':{p.name:{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(DEST.iterdir()) if p.name!='生成校验.json' and p.is_file()}}
    (DEST/'生成校验.json').write_text(json.dumps(verification,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(verification,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
