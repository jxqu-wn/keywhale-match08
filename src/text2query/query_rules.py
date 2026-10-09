"""Conservative question-derived checks; no validation-answer lookup."""
import re
from .pipeline import references, normalize
from .education import education_query_mode, merge_errors


def mask_sql(sql):
    return re.sub(r"'(?:''|[^'])*'|--[^\n]*|/\*.*?\*/", ' ', sql, flags=re.S)


def applicable_business_rules(question, rules, table_refs=None):
    result = []
    if education_query_mode(question)['mode']!='merged_latest':
        return result
    exact_overrides = [rule for rule in rules if rule.get('question_exact') and normalize(rule['question_exact'])==normalize(question)]
    # Distinctive education terms can route a question even when BM25 retrieves
    # unrelated examples sharing generic phrases such as "出现次数最多".
    education_hint = re.search(r'education_20\d{2}\.|教务|院系|学期|学籍|选课|退课|成绩分项|教师工号', question, re.I)
    if table_refs is not None and not exact_overrides and not education_hint and not any(ref.startswith('education_') for ref in table_refs):
        return result
    for rule in rules:
        if rule.get('question_exact'):
            if rule in exact_overrides:
                result.append(rule)
            continue
        pattern = rule.get('trigger_pattern')
        if pattern and re.search(pattern,question):
            result.append(rule)
        elif rule['rule_id']=='education_teacher_latest_union' and '教师' in question:
            result.append(rule)
    return result


def requirements(question):
    rules = []
    if re.search(r'销量最高|销售(?:数量|量)最多',question) and re.search(r'最少|最低',question):
        rules.append({'rule':'nested_sales_aggregation', 'instruction':'先按商店/主体汇总总销量选出第一名，再在选中的主体内按书籍/商品汇总数量选出最少者；两层排名都需要聚合，不能以单笔qty代替总销量。返回问题要求的主体标识/名称与商品标识/名称。'})
    if re.search(r'今天|最近|近\d+|本日|当天',question) and re.search(r'航向|航速|速度',question) and re.search(r'最大(?:速度|航速)',question):
        rules.append({'rule':'observed_speed', 'instruction':'区别时段内观测最大速度MAX(sog)和基础表额定max_speed；未要求额定/设计/基础属性时，在AIS记录内按mmsi聚合，不因“最大速度”一词添加MySQL。'})
    if re.search(r'图像|图片',question) and '？' in question:
        rules.append({'rule':'single_source_multi_part', 'instruction':'同一图像库的多问句仍是单源查询；用子查询、条件聚合、CTE或关联组成一条SELECT，不使用db1/query1/db2/query2。图像编号按当前问题，不照搬示例。'})
    if re.search(r'自持力|所属公司|所属.*公司|所属.*企业|荷载量|载重',question) and re.search(r'范围|周围|距离|公里|经纬度',question):
        rules.append({'rule':'federated_grain', 'instruction':'第一阶段输出关联键mmsi；第二阶段必须IN(result)。基础属性补充需要携带mmsi和问题要求的属性；返回AIS数据/记录或未指定字段的船舶查询时保留记录粒度，不擅自改成DISTINCT mmsi。'})
    if re.search(r'商家|球员|游戏|角色',question) and re.search(r'多少|数量|人数|最多|百分比|平均|占',question):
        rules.append({'rule':'entity_grain','instruction':'明确实体主键：商家business_id、球员playerID、游戏game_id、角色(character_id,work_id)。一对多关联不得重复计实体，属性值相同的不同实体不能被AVG(DISTINCT 属性)合并。'})
    if re.search(r'平均每年|每年.*平均',question) and re.search(r'\d{4}.*\d{4}',question):
        rules.append({'rule':'complete_period','instruction':'完整年份区间的平均包含零记录年份；按包含首尾的年数计算，不以出现数据的年份数代替。'})
    return rules


def semantic_errors(question, prediction, business_rules=()):
    errors = []
    rules = {x['rule'] for x in requirements(question)}
    if 'nested_sales_aggregation' in rules:
        sqls = [prediction['query']] if 'query' in prediction else [prediction.get('query1',''),prediction.get('query2','')]
        for sql in sqls:
            if not isinstance(sql,str):
                continue
            # Narrow known defect: ranking a raw qty field where the question asks total quantity.
            if re.search(r'ORDER\s+BY\s+(?:[\w]+\.)?(?:qty|quantity)\b',mask_sql(sql),re.I):
                errors.append('总销量排名不能按单笔qty/quantity排序；应按实体GROUP BY后SUM数量')
    applied = applicable_business_rules(question,business_rules)
    errors.extend(merge_errors(question,prediction,applied))
    if any(rule.get('entity')=='teacher' for rule in applied):
        query = prediction.get('query','')
        if prediction.get('db')!='MYSQL' or not isinstance(query,str):
            errors.append('未指定年份的教师查询应为MySQL单源跨年度查询')
        else:
            text = mask_sql(query).lower().replace('`','').replace('"','')
            tables = {table.lower() for table in references(mask_sql(query))}
            if not {'education_2024.teacher','education_2025.teacher'} <= tables:
                errors.append('教师未指定年份：需要同时访问2024和2025教师表')
            if not re.search(r'\bteacher_no\b',text):
                errors.append('跨年度教师去重应使用teacher_no，而不是年度自增teacher_id')
            if (re.search(r'统计.*教师.*(?:数量|人数)|教师.*(?:多少|几名|几位)',question) and
                    re.search(r'\bCOUNT\s*\(\s*\*\s*\)',text,re.I) and
                    not re.search(r'\bDISTINCT\b|\bGROUP\s+BY\b|\bROW_NUMBER\s*\(',text,re.I)):
                errors.append('教师人数使用COUNT(*)但没有编号去重；合并后需要按teacher_no去重，避免同年重复教师被多计')
    pairs = [(prediction.get('db'),prediction.get('query',''))] if 'query' in prediction else [(prediction.get('db1'),prediction.get('query1','')),(prediction.get('db2'),prediction.get('query2',''))]
    for db, query in pairs:
        if not isinstance(query,str):
            continue
        text = mask_sql(query)
        refs = {ref.lower() for ref in references(text)}
        if 'public_review_platform.business_hours' in refs and re.search(r'\b(?:\w+\.)?closing_time\s*-\s*(?:\w+\.)?opening_time\b',text,re.I):
            errors.append('Business_Hours为AM/PM文本，不能直接相减；使用营业时间解析CTE')
        if ('public_review_platform.reviews' in refs and 'public_review_platform.business' in refs
                and re.search(r'多少.*商家|商家.*数量',question)
                and re.search(r'COUNT\s*\(\s*(?:\w+\.)?business_id\s*\)',text,re.I)
                and re.search(r'\bJOIN\s+public_review_platform\.Reviews\b',text.replace('"','').replace('`',''),re.I)
                and not re.search(r'\bEXISTS\b',text,re.I)
                and not re.search(r'\bGROUP\s+BY\b',text,re.I)):
            errors.append('统计商家不能COUNT关联评论行；使用COUNT(DISTINCT business_id)或EXISTS')
        if 'public_review_platform.business_categories' in refs and re.search(r'占所有.*商家|占.*所有商家',question) and re.search(r'COUNT\s*\(\s*(?:\w+\.)?category_name\s*\)',text,re.I):
            errors.append('商家占比分母不能是类别关联行；从Business统计总体实体')
        if 'professional_basketball.player_allstar' in refs and re.search(r'州.*全明星.*最多|哪个州.*全明星|全明星.*哪个州',question) and re.search(r'COUNT\s*\(\s*(?:\w+\.)?playerID\s*\)',text,re.I) and not re.search(r'\bEXISTS\b',text,re.I):
            errors.append('全明星球员人数按playerID去重，不能统计入选赛季条数')
        if 'shakespeare.paragraphs' in refs and re.search(r'平均.*角色|平均角色',question) and re.search(r'COUNT\s*\(\s*(?:\w+\.)?character_id\s*\)',text,re.I):
            errors.append('每部作品角色数须去重角色，不能统计角色台词段落数')
        if ('shakespeare.paragraphs' in refs and re.search(r'平均.*角色|平均角色',question)
                and re.search(r'COUNT\s*\(\s*DISTINCT\s+(?:\w+\.)?character_id\s*\)',text,re.I)
                and not re.search(r'\bGROUP\s+BY\b',text,re.I)):
            errors.append('平均作品角色数应先按作品GROUP BY去重角色，再AVG；不能全局COUNT(DISTINCT character_id)除以作品数')
        if ('public_review_platform.categories' in refs and '汽车' in question and 'Cars' not in question
                and re.search(r"\bcategory_name\s*(?:=|LIKE)\s*'Cars'",query,re.I)):
            errors.append('知识149明确汽车类别为Automotive，不能自行翻译为Cars')
        if ('mental_health_survey.answer' in refs and '年龄' in question
                and re.search(r'\bAnswerText\s+IN\s*\(\s*SELECT\s+AnswerText\b',text,re.I)):
            errors.append('年龄与国家不能比较AnswerText；按UserID和SurveyID关联年龄QuestionID=1与国家QuestionID=3的答案行')
        if '斯坦利杯决赛' in question and 'hockey.teams' in refs and 'hockey.teamssc' not in refs:
            errors.append('斯坦利杯决赛指标来自hockey.TeamsSC；按year和tmID关联Teams，不能使用Teams的常规赛同名字段')
        if 'bike_share_1.trip' in refs and re.search(r"\bstart_date\s+LIKE\s+'\d{1,2}/%/\d{4}'",query,re.I):
            errors.append('start_date含时间；月份LIKE模式需要末尾%')
        if db=='POSTGRESQL' and re.search(r'货船|油轮',question) and not re.search(r'(?:类型|vessel_type|货船|油轮)\s*[=为（(：:]?\s*\d+',question,re.I):
            wrong = '30' if '货船' in question else '35'
            if re.search(r'\bvessel_type\s*=\s*'+wrong+r'\b',text,re.I):
                errors.append('PG AIS编码按源解释：货船70、油轮80，不沿用MySQL字典30/35')
        if db=='MYSQL' and '市净率' in question and re.search(r'total_assets\s*-\s*(?:\w+\.)?total_liabilities',text,re.I):
            errors.append('赛题PB依赖知识1624的BPS(total_assets*10000)/total_shares，不能自行减负债')
    return errors
