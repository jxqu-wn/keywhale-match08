"""Question-derived domain routing and scoped, dependency-complete knowledge."""
import re
from .pipeline import queries, references, source

PROFILES = [
    (r'Yelp|精英用户|商家.*营业|商家.*评论|有趣投票|营业时间', ['public_review_platform']),
    (r'受访者|心理健康|精神病|精神健康', ['mental_health_survey']),
    (r'全明星|NBA|篮球', ['professional_basketball']),
    (r'斯坦利杯|冰球|加拿大人队', ['hockey']),
    (r'板球', ['soccer_2016']),
    (r'莎士比亚', ['shakespeare']),
    (r'发行商|游戏平台|发行.*游戏|游戏.*发行|款游戏', ['video_games']),
    (r'购书|订购书籍|书籍.*出版社', ['books']),
    (r'自行车|出行.*San Jose|San Jose.*出行', ['bike_share_1']),
    (r'根汁啤酒', ['beer_factory']),
    (r'装机|光伏|发电场站|火电|风电', ['energy','common']),
    (r'市净率|每股净资产|股票|所得税|民营企业|国企|税额', ['finance_tax','common']),
    (r'院系|学期|选课|教师|学籍|在读学生|课程|班级|学号', ['education_2024','education_2025','common']),
]


def infer_scope(question, examples, catalog):
    known = {t.get('database'):t['source'] for t in catalog if t.get('database')}
    explicit = [db for db in known if re.search(r'(?<![A-Za-z0-9_])'+re.escape(db)+r'(?![A-Za-z0-9_])',question,re.I)]
    databases, confidence, reason = explicit, 'explicit' if explicit else 'weak', '题目中的显式库名' if explicit else '首条训练示例，仅为候选'
    sources = []
    if ('shakespeare' in explicit and re.search(r'出版.*书|书.*出版',question)
            and not re.search(r'(?:数据库|库名)\s*["`]?shakespeare|shakespeare\s*\.',question,re.I)):
        databases, confidence, reason = ['books'], 'strong', '书籍出版查询中的Shakespeare是作者姓名，不是数据库声明'
        sources = [known['books']]
    if not explicit and re.search(r'医院|门诊|救护车|护士|三甲',question):
        sources, confidence, reason = ['Elasticsearch'], 'strong', '医院检索领域'
    if not explicit and not sources:
        for pattern, domains in PROFILES:
            if re.search(pattern,question,re.I):
                databases, confidence, reason = domains, 'strong', '领域词：'+pattern
                break
    if not explicit and re.search(r'船舶|货船|油轮|MMSI|AIS',question,re.I) and re.search(r'AIS|记录|吃水|对地|轨迹|公里|范围|周围|经纬|最近|今天|昨天|过去',question):
        databases, confidence, reason = ['public'], 'strong', 'AIS时空/观测记录'
        if re.search(r'船长|自持力|所属.*(?:公司|企业)|最大载重|荷载量|最大荷载',question):
            databases += ['transportation','common']
            reason += '及基础属性，需要保留跨源候选'
    if not databases and not sources and examples:
        databases = sorted({ref.split('.',1)[0] for q in queries(examples[0]) for ref in references(q)})
        sources = source(examples[0]).split('->')
    if not sources:
        sources = sorted({known[db] for db in databases if db in known})
    return {'databases':databases, 'sources':sources, 'confidence':confidence, 'reason':reason,
            'instruction':'数据源是候选范围，最终按当前题目及字段归属选择；同名字段/业务枚举必须按源库解释，不强行把跨源查询变单源。'}


def row_domains(row):
    return {ref.split('.',1)[0] for sql in queries(row) for ref in references(sql)}


def compatible_example(row, scope):
    if scope['confidence'] == 'weak':
        return True
    if not set(source(row).split('->')) <= set(scope['sources']):
        return False
    domains = row_domains(row)
    return not domains or bool(domains.intersection(scope['databases']))


def knowledge_domains(item, catalog):
    if item.get('declared_scope'):
        return item['declared_scope']
    text = item['knowledge_text']
    names = {t.get('database') for t in catalog if t.get('database')}
    databases = {name for name in names if re.search(r'(?<![A-Za-z0-9_])'+re.escape(name)+r'(?![A-Za-z0-9_])',text,re.I)}
    if not databases and re.search(r'BPS|P/B|PB|市净率|每股净资产|companies\.|company_financials|stocks\.|税率|所得税',text,re.I):
        databases = {'finance_tax'}
    if not databases and re.search(r'医院|门诊|救护车|护士|三甲|人民广场',text):
        return {'databases':[], 'sources':['Elasticsearch']}
    source_by_db = {t.get('database'):t['source'] for t in catalog if t.get('database')}
    return {'databases':sorted(databases), 'sources':sorted({source_by_db[db] for db in databases})}


def mandatory_knowledge(question, scope):
    domains = set(scope['databases'])
    result = []
    if 'professional_basketball' in domains:
        if re.search(r'BMI|体质指数',question,re.I):
            result += [51,83,1248,1249]
        elif re.search(r'身高|体重|出生.*州|哪个州',question):
            result += [51,1248,1249]
    if 'mental_health_survey' in domains:
        result += [186,188]
        if re.search(r'年龄|美国|加拿大|国家',question):
            result += [189]
    if 'finance_tax' in domains:
        if re.search(r'市净率|P/?B',question,re.I):
            result += [1624,1625]
        elif '每股净资产' in question:
            result += [1624]
    if 'public_review_platform' in domains:
        if re.search(r'汽车|宠物|房地产|健身|服装|购物|美容|脱毛',question):
            result += [149]
        if re.search(r'营业|小时|周日|周六',question):
            result += [150,159,163,1355]
        if '精英' in question:
            result += [153,1362,1385,1386]
    if 'bike_share_1' in domains:
        result += [319,322]
    if 'video_games' in domains:
        result += [120,121,123,1539]
    if 'public' in domains and '招银' in question:
        result += [1757]
    return list(dict.fromkeys(result))


def select_knowledge(question, knowledge, retriever, scope, catalog):
    mandatory = mandatory_knowledge(question, scope)
    by_id = {item['knowledge_id']:item for item in knowledge}
    chosen = []
    domains, sources = set(scope['databases']), set(scope['sources'])
    for kid in mandatory:
        if kid in by_id:
            chosen.append({**by_id[kid], 'scope':knowledge_domains(by_id[kid],catalog),
                           'retrieval_reason':'字段/公式依赖，必须同时提供'})
    for score, index in retriever.search(question, len(knowledge)):
        if score <= 0 or len(chosen) >= max(10,len(mandatory)):
            break
        item = knowledge[index]
        if item['knowledge_id'] in {x['knowledge_id'] for x in chosen}:
            continue
        declared = knowledge_domains(item,catalog)
        if declared['sources'] and sources and not sources.intersection(declared['sources']):
            continue
        primary_domains = set(declared['databases'])-{'common'}
        if primary_domains and domains and not domains.intersection(primary_domains):
            continue
        # With a strong route, unscoped narrative can crowd out actual field facts.
        if scope['confidence'] != 'weak' and not declared['databases'] and not declared['sources']:
            continue
        chosen.append({**item,'scope':declared,'retrieval_reason':'域内关键词召回'})
    for item in chosen:
        coordinates = []
        for degrees, minutes, seconds, direction in re.findall(r'(\d+)°\s*(\d+(?:\.\d+)?)\s*[′\x27’]\s*(?:(\d+(?:\.\d+)?)\s*[″"]\s*)?([NSEW])',item['knowledge_text'],re.I):
            value = float(degrees)+float(minutes)/60+float(seconds or 0)/3600
            if direction.upper() in ('S','W'):
                value = -value
            coordinates.append({'degrees':int(degrees),'minutes':float(minutes),'seconds':float(seconds or 0),
                                'direction':direction.upper(),'decimal_degrees':value})
        if coordinates:
            item['derived_constants'] = {'dms_to_decimal':coordinates,'method':'degrees+minutes/60+seconds/3600，S/W取负'}
    return chosen


def dictionary_matches(question, documents, scope):
    result = []
    for doc in documents:
        declared_source = doc.get('source','MYSQL')
        if scope['sources'] and declared_source not in scope['sources']:
            continue
        if doc.get('database') and scope['databases'] and doc['database'] not in scope['databases'] and doc['database'] != 'common':
            continue
        # Reference SQL has single-line INSERT VALUES; common dictionaries have multiline rows.
        values = re.split(r'\bVALUES\b',doc['text'],maxsplit=1,flags=re.I)
        if len(values) != 2:
            continue
        rows = re.findall(r"\((?:[^'()]|'(?:''|[^'])*')*\)",values[1])
        matches = []
        for row in rows:
            literals = re.findall(r"'((?:''|[^'])*)'",row)
            if any(value and ((len(value)>1 and value in question) or re.search(r'(?<!\w)'+re.escape(value)+r'(?!\w)',question)) for value in literals):
                matches.append(row)
        if matches:
            result.append({'id':doc['id'],'header':doc['text'].split('VALUES',1)[0]+'VALUES',
                           'source':declared_source,'database':doc.get('database'),
                           'rows':matches[:20], 'instruction':'仅对声明的数据源/字典引用表解释；不能把MySQL枚举套到PG AIS。'})
    return result
