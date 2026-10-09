"""Audit the historical 400-row submission without changing it or calling a model.

The semantic findings below are a reviewed inventory for the pinned submission,
not a general-purpose SQL correctness classifier. Counterexamples are synthetic;
they are not competition answers or an execution-accuracy evaluation.
"""
import collections
import csv
import hashlib
import json
import math
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from text2query.model_pipeline import validate
from text2query.pipeline import queries, read_text, references, source

PINNED_SHA256 = '210e0859613681fe36f23fcb1e5430145796f89891007c250565f1300bd8c7a6'
RUN = ROOT / 'artifacts/runs/prediction/predict-ee19c0abd953'
OUT = ROOT / 'artifacts/analysis' / ('submission-' + PINNED_SHA256[:12])
REPORT = ROOT / 'docs/reports/上一轮提交结果分析.md'
CATEGORIES = {
    'structure': '结构/语法问题（确定）',
    'semantics': '语义问题（题意、Schema或反例支持）',
    'knowledge': '知识口径不一致（本地知识/已裁定规则支持）',
    'risk': '条件性风险（需要真实值或业务口径确认）',
    'policy': '后续业务口径变化（不回推为原始提交错误）',
}


def finding(qid, category, title, evidence, cause, recommendation, relevant_kb=()):
    return dict(question_id=qid, category=category, title=title, evidence=evidence,
                cause_inference=cause, recommendation=recommendation,
                relevant_knowledge_ids=list(relevant_kb))


FINDINGS = [
    finding(4976, 'structure', 'SQLite保留字表名未加引号',
            '原始DDL为CREATE TABLE "transaction"；提交使用beer_factory.transaction。EXPLAIN报near "transaction": syntax error。',
            '模型生成了正确的交易关联方向，却漏掉标识符引号；基础校验只识别表名，不调用SQLite编译。',
            '将物理表写为beer_factory."transaction"，并对SQLite结果做原始DDL上的编译检查。'),
    finding(5029, 'structure', 'UNION分支排序不合法，且容器字段属于另一张表',
            '两个UNION分支直接带ORDER BY/LIMIT，SQLite编译失败；只修正集合语法后，仍报no such column: T2.ContainerType。ContainerType在rootbeer即T1中。',
            '检索示例4970提供最低利润排序，5071提供容器关联。模型把两个Top1查询直接拼接，并在重新命名别名时绑定错列。',
            '分别包装最高/最低利润子查询，或用MAX/MIN利润筛选；输出T1.ContainerType。修复后重新编译，不能只消除第一条错误。'),
    finding(5686, 'structure', 'JOIN后的year列歧义',
            'hockey.Teams和TeamsSC都有year；SELECT year报ambiguous column name: year。',
            '相近示例5684的年份写法有T1限定；模型改成返回年份时遗漏限定，基础检查没有列绑定。',
            '使用SELECT T1.year；根据题目候选范围还可显式限定1924、1923、1918。'),
    finding(1825, 'structure', 'MySQL查询使用FULL OUTER JOIN',
            'db为MYSQL，SQL对2024/2025课程汇总做FULL OUTER JOIN。MySQL JOIN语法支持LEFT/RIGHT，不支持该写法。这里是方言核对，未连接MySQL执行。',
            '模型需要保留两个年份中单边存在的院系，选择了其他SQL方言的全外连接；校验未按目标方言解析。',
            '先UNION两年院系键，再LEFT JOIN两个汇总；跨年院系身份应按已确定的业务编号对齐。'),
    finding(2399, 'structure', 'ES嵌套filter无效，并引用不存在的排序字段',
            'query.bool.filter同一对象含geo_distance和range两个查询类型；应为两个独立子句。sort使用daily_outpatient，而原始mapping只有annual_outpatient，没有daily_outpatient。',
            '根query只有bool一个键，所以旧根结构检查放行了嵌套错误。模型把题目“日均门诊量”直译成了未定义字段，缺少mapping级验证。',
            'filter改为数组；核对annual_outpatient与日均量口径，固定天数换算时可按annual_outpatient同序排序，不能发明字段。'),
    finding(649, 'semantics', '格尔木市被扩成整个海西州',
            "SQL使用area_id LIKE '6328%'。参考区划中格尔木是632801，632802是德令哈；6328前缀会包含其他市县。",
            '历史dictionary没有召回格尔木区划行；模型由常识补了较粗行政编码，未核对区划层级。',
            '按参考区划定位格尔木632801；需要辖区范围时再按实际区划层级生成条件。'),
    finding(846, 'semantics', '把县级市名称放在地级city_name字段过滤',
            "参考数据格尔木行name/county_name为格尔木市，city_name为海西蒙古族藏族自治州；没有city_name='格尔木市'的行。原SQL按city_name过滤会漏掉该市。",
            '历史dictionary只召回能源/场站类别，没有召回行政区划值。模型按中文“市”猜测字段，没有使用真实层级。',
            '优先使用已核对的行政区划id，或结合name/county_name与省/州限定。'),
    finding(1781, 'semantics', '课程编号被替换为课程业务代码',
            '题目要课程编号，原Schema区分course_id和course_code，原查询输出course_code；相近题1800等也能提示编号与代码的区分。',
            '模型沿用分组业务代码后把它直接投影，未逐项核对返回列；跨年合并的业务键与提交所需输出字段被混淆。',
            '返回最终胜出记录的course_id、course_name，并按teacher_no去重；跨年教师/课程映射按当前教育合并规则处理。'),
    finding(3512, 'semantics', '教练GROUP BY后只返回一支任意球队',
            '按coachID分组但直接选择未聚合的球队name，加LIMIT 1；一个教练执教两支球队时只能返回其中一支，无法列出执教过的球队。',
            '将“先选最长执教教练，再列球队”压成一个分组查询。另有MAX(year)-MIN(year)把间断执教算连续年跨度的风险。',
            '先按明确的执教时长定义选教练，再关联并去重输出其球队。'),
    finding(3581, 'semantics', '获奖教练关联了非获奖年份的执教记录',
            'awards_coaches只按coachID关联coaches，遗漏获奖year与执教year相等。构造1990和1992不同球队时，两队都会返回。',
            '历史示例中混有获奖教练的职业生涯球队查询；模型只把年份加在奖项表，忽略了时点关联。',
            '增加T1.year=T2.year，并视赛季/联盟字段定义补充限定。'),
    finding(3590, 'semantics', '全明星出场次数被当成全明星球员人数',
            'player_allstar主键为(playerID,season_id)，同一人可有多个赛季；COUNT(playerID)统计关联行。构造一州1人4届、另一州2人各1届时会选错州。',
            '使用通用JOIN+COUNT模板，未识别表的赛季粒度；“球员”应该在playerID粒度计数。',
            'COUNT(DISTINCT T1.playerID)，并明确空出生州的处理。'),
    finding(3674, 'semantics', '段落数量被当成角色数量',
            'COUNT(paragraphs.character_id)数的是每段台词的角色引用。相同角色说10段会计10次；构造两部作品分别1和2个角色时，返回6而不是1.5。',
            '模型找到了作品→章节→段落链，但没按每部作品去重角色；错误来自聚合粒度。',
            '先按作品COUNT(DISTINCT character_id)，再对作品取AVG；所有作品的范围需要包含无台词作品。'),
    finding(3902, 'semantics', '游戏占比的分母变成发行关系条数',
            'game_publisher以id为主键，不保证game_id唯一；COUNT(game_id)会把多发行商/重复关联的一个游戏计多次，也未包含未关联游戏。',
            '比例模板直接作用于关系表；检索还包含4298的类别行占比模板，容易忽略实体粒度。',
            '分子去重指定发行商game_id，分母从game统计所有游戏。'),
    finding(3915, 'semantics', '发行商排名按关联行而非不同游戏数',
            'COUNT(T2.game_id)未去重且按publisher_name分组；重复关系可抬高排名，同名但不同id的发行商也可能合并。',
            '检索示例提供计数排名形式，模型没有核对关系表的唯一性约束。',
            '按publisher.id分组，COUNT(DISTINCT game_id)，最后返回名称。'),
    finding(3977, 'semantics', '年平均分母只包含有论文的年份',
            '2008至2011包括4个年份；COUNT(DISTINCT Paper.Year)只数出现记录的年份。构造2008/2011各1篇且2009/2010为0，原SQL为1，按完整区间应为0.5。',
            '将常见“总量/有数据的周期数”模板用于明确完整时间区间，遗漏零值年份。若业务定义只平均有发表的年份则需另行说明。',
            '完整区间平均除以4，或先构造完整年份序列再左关联计数。'),
    finding(4123, 'semantics', '汽车类商家占比统计了类别关联行',
            '分母COUNT(category_name)是Business_Categories行数。构造两家商家：一家有汽车/餐饮两个类别，另一家仅餐饮，返回33.33%，按商家数应为50%。',
            '历史Top1示例4298“宠物类商家占比”的SQL使用同样错误分母；本题仅替换为Automotive，是错误训练模板传播的直接证据。',
            '分母COUNT(Business.business_id)，分子COUNT(DISTINCT符合Automotive的business_id)。'),
    finding(4207, 'semantics', '12小时制字符串直接相减',
            'opening_time/closing_time为TEXT，知识159明确8AM、9PM格式。SQLite把这两个文本按数字前缀做9-8=1，实际营业13小时；相等字符串还应按知识163视为24小时。',
            '知识150/1355提供裸减法，同时159/163给出格式和全天定义；这些都在历史上下文中。模型照用了简化公式，未解决知识内部的表示层冲突。',
            '将AM/PM、分钟解析为统一分钟数，处理跨午夜、相等为24小时及缺失值，再比较时长。', (150, 1355, 159, 163)),
    finding(4424, 'semantics', '长评论数被当成商家数',
            'Reviews一商家可有不同用户多条长评论；COUNT(Business.business_id)会重复计数。构造一家Phoenix活跃商家两条Long评论，原SQL计2而不是1。',
            '模型用JOIN表达“存在长评论”，随后直接COUNT关联行，未转换到商家去重粒度。',
            'COUNT(DISTINCT business_id)或EXISTS；active真实值大小写需单独核对，不能据假设判错。'),
    finding(4682, 'semantics', '年龄题干和美国受访者筛选均错误',
            "知识188给出年龄QuestionID=1、题干What is your age?；原SQL使用How old are you?。知识189规定国家是QuestionID=3的United States回答，原SQL却按Survey.Description LIKE '%USA%'选调查。",
            '历史10条知识中缺少188/189，混入电影/自行车的平均数公式；示例4883来自奥运USA平均年龄。模型在缺少领域锚点后猜测文本和筛选位置。',
            '年龄按QuestionID=1取数，通过同一UserID和SurveyID关联QuestionID=3、AnswerText=United States的国家回答，并排除无效年龄。', (188, 189)),
    finding(5921, 'semantics', '月份LIKE漏掉末尾通配符',
            "start_date为带时间的TEXT，知识322给出月份/%/年份%的格式；原SQL使用'8/%/2013'，无法匹配'8/29/2013 12:00:00'。",
            '知识322确实被召回；模型落笔时丢掉末尾%，基础检查只验证SQL可执行，没有验证日期字符串样例。',
            "使用LIKE '8/%/2013%'，或先按已知格式解析日期后使用半开区间。", (322,)),
    finding(3576, 'knowledge', 'BMI使用了错误单位，并可能重复加权球员',
            '知识51/83说明height为英寸、weight为磅；83规定本题公式weight/(height*height)。原SQL按身高厘米除100，单球员200磅/80英寸时为312.5，本地公式为0.03125，差10000倍。',
            '历史上下文没有51、83、1248、1249，却有其他领域AVG公式；模型套用了公斤/厘米的常见BMI计算。JOIN全明星赛季还会对多次入选者重复加权。',
            '比赛口径按知识83计算并去重球员；如要真实物理BMI须另行确认单位换算口径，不能悄悄替换竞赛定义。', (51, 83, 1248, 1249)),
    finding(442, 'knowledge', '市净率未遵循知识库的每股净资产口径',
            '知识1624规定BPS=(total_assets*10000)/total_shares；原SQL改成(total_assets-total_liabilities)*10000/total_shares。知识1625本题PB=股价/BPS。',
            '1625召回而1624遗漏。Top1示例432使用资产公式，Top2示例428为ROE使用资产减负债；可能混合相邻公式，或用一般财务常识覆盖了本赛题定义。',
            '按竞赛知识1624/1625一致计算。该知识定义不同于通常财务含义，应标明竞赛口径，不能用常识把正确样本判错。', (1624, 1625)),
    finding(2570, 'knowledge', 'PG货船编码被MySQL字典污染',
            'PG查询用vessel_type=30；历史dictionary明确来自common.vessel_type_dict的MySQL货船30。PG训练题2502、2513、2703等显式规定货船70，测试题3033也明确类型70。处理前schema_adjudications只裁定了油轮，未实现货船规则。',
            '字典按词匹配召回，没有以目标数据源隔离同名业务枚举，模型把另一数据源的编码带入AIS表。',
            '以PG训练题显式编码70为优先依据，并补全货船的来源约束。真实库枚举仍需数据核对；此项不是数据库执行失败断言。'),
    finding(2613, 'knowledge', '另一条PG货船查询使用30',
            '原SQL使用vessel_type=30；与PG训练题2502、2513、2703等显式货船70不一致。真实比赛数据中30是否为空，本地无法确认。',
            '同一货船词汇的跨源编码混用；历史dictionary与PG字段之间没有来源约束。',
            '补全PG货船来源规则，按领域训练题显式编码70，不沿用MySQL的货船30。'),
    finding(2646, 'knowledge', 'PG油轮编码被MySQL字典污染',
            'PG查询用vessel_type=35；历史dictionary直接提供MySQL common.vessel_type_dict的油轮35。当前裁定PG油轮采用题目显式编码80。',
            '知识召回未区分MySQL船舶类型字典和PG AIS类型；字段同名使错误替换不被表存在性检查发现。',
            'PG油轮采用80；排除目标船时还需明确其他船IMO为NULL的处理，!=会排除NULL。'),
    finding(2923, 'knowledge', '招银港区航道北界坐标换算错误',
            '知识1757北端为24°28.0′，十进制度为24+28/60=24.4666667；原SQL北纬上界24.4833约为24°29′，扩大范围。1757已在历史知识中。',
            '模型在度分换算中多算1角分；基础校验没有地理常量校验。另把180米宽航道简化为矩形是单独的近似风险。',
            '坐标由确定的度分转十进制度函数产生；是否使用航道中心线缓冲区还需按评测几何口径确认。', (1757,)),
    finding(4324, 'risk', 'year_id可能是代理键而非实际年份',
            'Elite.year_id外键指向Years.year_id，Years另有actual_year。原SQL直接year_id=2012；构造year_id=7、actual_year=2012时漏用户。实际键是否等于年份尚无数据。',
            '知识153/158把year_id简写成年份，示例4401也直接用2006；历史检索未召回1362/1385的字段区别。Schema与简化知识存在歧义。',
            '核对Years实际数据；按外键JOIN Years并筛actual_year=2012更稳妥，不能仅因反例就宣称官方判错。', (153, 158, 1362, 1385)),
    finding(3422, 'risk', '板球平局字段的知识与Schema关系需要核对',
            '知识28规定Win_By.Win_Type=Tie；原SQL从Out_Type.Out_Name取Tie并筛Outcome_type。原DDL确实把Outcome_type外键连到Out_Type，不能单凭名称认定表不存在或绑定错误。',
            '知识28未召回；模型依Schema外键猜测Tie所在字典。原知识/字典定义需结合真实值核对，不能武断改用另一个Outcome表。',
            '核对Win_By和Out_Type实际枚举；如按知识28，使用Match.Win_Type→Win_By.Win_Id→Win_Type=Tie。', (28,)),
    finding(5028, 'risk', '题目“瓶”没有容器过滤，日均分母也有歧义',
            'SQL只筛Caffeinated=TRUE，未筛rootbeer.ContainerType=Bottle。构造同日一瓶一罐时返回2而不是瓶数1。分母是有咖啡因交易的不同TransactionDate，不含零销售日。',
            '检索到含咖啡因计数模板，模型可能把“瓶”当泛指单位，忽略容器类型以及每天的时间范围。',
            '若“瓶”是严格容器类型应筛Bottle；明确日均按交易日还是完整日历区间，再设计分母。'),
    finding(5311, 'risk', '国家地址被直接当成客户地址',
            '查询只读country/address，未关联customer_address/customer。构造有客户的San Jose与无客户的Liberia地址时，两市都返回。是否所有address都被客户引用尚未知。',
            '模型省掉存在性关联，把地址维表中的城市视为购书客户实际所在城市。',
            '通过customer_address/customer限定；若“购书客户”要求实际下单，还需EXISTS cust_order，按业务确认。'),
    finding(2838, 'risk', '一天的闭区间遗漏最后一秒的小数部分',
            "timestamp字段允许子秒精度；BETWEEN 00:00:00 AND 23:59:59会漏掉2025-02-28 23:59:59.500000。真实数据是否含子秒未确定。",
            '常见自然日模板使用最后一整秒，没有考虑字段精度。',
            "使用timestamp >= '2025-02-28' AND timestamp < '2025-03-01'。"),
    finding(517, 'risk', '非民营不一定等于国企',
            '原SQL以is_private=FALSE代表国企；知识1621只定义TRUE民营/FALSE非民营，非民营可能包含外资等其他所有制。是否存在这些企业需数据确认。',
            '模型把二元民营标志的补集直接命名国企，缺少所有制字典的进一步约束。',
            '按已确认的登记类型/所有制编码筛国企；如数据确实只有两类，则记录这一前提。', (1621,)),
    finding(2274, 'risk', '“统计值”只返回sum',
            '护士数统计值使用sum；同类床位/救护车统计题使用stats。若题意要求count/min/max/avg/sum，则原结果列不完整；题干本身不能唯一确定。',
            '模型将不明确的“统计值”收窄成总和，缺少输出集合约定。',
            '核对同类题知识/评测口径后确定stats还是sum，不能凭措辞直接判错。'),
    finding(5265, 'risk', '客户国家路由及State字段可疑',
            "Australia是国家，原SQL却查beer_factory.customers.State。历史知识242已提供books.country.country_name='Australia'，另有movie_3.country；题目没有指定数据库，缺少唯一金标路由。",
            '跨领域客户/国家示例同时召回，模型选择了字段较少的customers表并把国家值放入State；路由和词义约束不足。',
            '优先结合领域证据选数据库；核对State真实值及国家建模。若目标books，关联customer_address→address→country并去重客户。', (242,)),
]

for qid in (1636, 1648, 1737, 1781):
    FINDINGS.append(finding(qid, 'policy', '旧教育查询未应用后来确定的跨年合并口径',
        '题目未显式指定数据库年份，旧结果只使用education_2024或education_2025。用户后来要求两年按业务键合并、去重、冲突以2025胜出；旧result.json没有重新生成。',
        '历史context只有schema/knowledge/dictionary/examples，没有当前education_merge/business_rules；这是结果版本与新业务规则的差异。1737的20240101是日期值，不能直接据此限定2024数据库。',
        '下一次生成应应用当前合并逻辑。额外检查日期中的2024不被错误识别为指定数据库年份；先整行选最新记录，再筛选与排序。'))


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sqlite_schema(catalog, sql):
    """Use complete raw DDL, not the lossy parsed column metadata."""
    connection = sqlite3.connect(':memory:')
    for database in sorted({ref.split('.', 1)[0] for ref in references(sql)}):
        connection.execute(f'ATTACH DATABASE ":memory:" AS "{database}"')
        for table in catalog:
            if table['source'] != 'SQLite' or table.get('database') != database:
                continue
            pattern = r'^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?("[^"]+"|`[^`]+`|\[[^\]]+\]|\w+)'
            match = re.match(pattern, table['ddl'], re.I)
            if match is None:
                raise ValueError(f'Cannot read DDL: {database}.{table["table"]}')
            name = match.group(1).strip('"`[]')
            ddl = re.sub(pattern, lambda _: f'CREATE TABLE "{database}"."{name}"',
                         table['ddl'], count=1, flags=re.I)
            connection.execute(ddl)
    return connection


def es_errors(payload, mapping):
    """Focused recursive query-clause arity and sort-field check, not all ES DSL."""
    errors = []

    def clause(value, path):
        if isinstance(value, list):
            for index, item in enumerate(value):
                clause(item, f'{path}[{index}]')
            return
        if not isinstance(value, dict) or len(value) != 1:
            errors.append(f'{path}: 查询子句必须只有一个查询类型键')
            return
        kind, body = next(iter(value.items()))
        if kind == 'bool':
            if not isinstance(body, dict):
                errors.append(f'{path}.bool: 必须是对象')
                return
            for name in ('must', 'should', 'filter', 'must_not'):
                if name in body:
                    clause(body[name], f'{path}.bool.{name}')
        elif kind in ('constant_score', 'nested', 'has_child', 'has_parent'):
            if isinstance(body, dict):
                for name in ('query', 'filter'):
                    if name in body:
                        clause(body[name], f'{path}.{kind}.{name}')

    if 'query' in payload:
        clause(payload['query'], 'query')
    if 'post_filter' in payload:
        clause(payload['post_filter'], 'post_filter')
    sorts = payload.get('sort', [])
    if not isinstance(sorts, list):
        sorts = [sorts]
    for item in sorts:
        names = list(item) if isinstance(item, dict) else [item]
        for name in names:
            if name not in ('_score', '_doc', '_geo_distance', '_script'):
                base = name.split('.', 1)[0]
                if base not in mapping:
                    errors.append(f'sort: mapping中不存在字段{name}')
    return errors


def fixture_cases():
    """Table, inserted row dictionaries; expected answers come from the question."""
    cases = {}

    def add(qid, description, expected, **tables):
        cases[qid] = dict(description=description, expected=expected, tables=tables)

    add(3512, '同一教练1970/1980分别执教A/B，应列出两队，排序不影响该集合比较。',
        [['c1', 'A'], ['c1', 'B']],
        **{'hockey.Coaches': [dict(coachID='c1', year=y, tmID=t, stint=1) for y, t in ((1970, 'a'), (1980, 'b'))],
           'hockey.Teams': [dict(year=y, tmID=t, name=n) for y, t, n in ((1970, 'a', 'A'), (1980, 'b', 'B'))]})
    add(3576, '单球员weight=200磅，height=80英寸；按知识83计算200/(80*80)。',
        [[200 / (80 * 80)]],
        **{'professional_basketball.players': [dict(playerID='p1', weight=200, height=80)],
           'professional_basketball.player_allstar': [dict(playerID='p1', season_id=1992)]})
    add(3581, '教练1990执教A，1992执教B，并于1992获奖；当年球队仅B。', [['B']],
        **{'professional_basketball.awards_coaches': [dict(id=1, coachID='c1', year=1992, award='NBA Coach of the Year')],
           'professional_basketball.coaches': [dict(coachID='c1', year=y, tmID=t, stint=1) for y, t in ((1990, 'a'), (1992, 'b'))],
           'professional_basketball.teams': [dict(year=y, tmID=t, name=n) for y, t, n in ((1990, 'a', 'A'), (1992, 'b', 'B'))]})
    add(3590, '州A一人入选四届，州B两人各入选一届；人数最多为B。', [['B']],
        **{'professional_basketball.players': [dict(playerID=p, birthState=s) for p, s in (('a', 'A'), ('b', 'B'), ('c', 'B'))],
           'professional_basketball.player_allstar': [dict(playerID='a', season_id=y) for y in range(1990, 1994)] + [dict(playerID=p, season_id=1992) for p in ('b', 'c')]})
    add(3674, '作品1一个角色重复10段，作品2两个角色各一段，平均角色数(1+2)/2。', [[1.5]],
        **{'shakespeare.works': [dict(id=i, Title=str(i), LongTitle=str(i), Date=1600, GenreType='Drama') for i in (1, 2)],
           'shakespeare.chapters': [dict(id=i, Act=1, Scene=1, Description='', work_id=i) for i in (1, 2)],
           'shakespeare.characters': [dict(id=i, CharName=str(i), Abbrev=str(i), Description='') for i in (1, 2, 3)],
           'shakespeare.paragraphs': [dict(id=i, ParagraphNum=i, PlainText='', character_id=1, chapter_id=1) for i in range(1, 11)] + [dict(id=i+9, ParagraphNum=i, PlainText='', character_id=i, chapter_id=2) for i in (2, 3)]})
    add(3902, '三个游戏均有发行记录，游戏1有两个发行商；Brash发行其中一个，占1/3。', [[100 / 3]],
        **{'video_games.game': [dict(id=i, game_name=str(i)) for i in (1, 2, 3)],
           'video_games.publisher': [dict(id=1, publisher_name='Brash Entertainment'), dict(id=2, publisher_name='Other')],
           'video_games.game_publisher': [dict(id=i, game_id=g, publisher_id=p) for i, g, p in ((1, 1, 1), (2, 1, 2), (3, 2, 2), (4, 3, 2))]})
    add(3915, '发行商A同一游戏关联3条，B不同游戏2个；按不同游戏数应为B。', [['B']],
        **{'video_games.game': [dict(id=i, game_name=str(i)) for i in (1, 2, 3)],
           'video_games.publisher': [dict(id=i, publisher_name=n) for i, n in ((1, 'A'), (2, 'B'))],
           'video_games.game_publisher': [dict(id=i, game_id=g, publisher_id=p) for i, g, p in ((1, 1, 1), (2, 1, 1), (3, 1, 1), (4, 2, 2), (5, 3, 2))]})
    add(3977, '2008和2011各发表一篇，中间两年0篇；完整4年平均0.5。', [[0.5]],
        **{'authors.Journal': [dict(Id=1, FullName='Molecular Brain')],
           'authors.Paper': [dict(Id=i, Year=y, JournalId=1) for i, y in ((1, 2008), (2, 2011))]})
    add(4123, '两家商家，其中一家汽车/餐饮两类，另一家餐饮一类；汽车商家比例1/2。', [[50.0]],
        **{'public_review_platform.Business': [dict(business_id=i) for i in (1, 2)],
           'public_review_platform.Categories': [dict(category_id=1, category_name='Automotive'), dict(category_id=2, category_name='Food')],
           'public_review_platform.Business_Categories': [dict(business_id=b, category_id=c) for b, c in ((1, 1), (1, 2), (2, 2))]})
    add(4207, '周日8AM到9PM营业13小时，应返回商家1。', [[1]],
        **{'public_review_platform.Business': [dict(business_id=1)],
           'public_review_platform.Days': [dict(day_id=1, day_of_week='Sunday')],
           'public_review_platform.Business_Hours': [dict(business_id=1, day_id=1, opening_time='8AM', closing_time='9PM')]})
    add(4424, '一家Phoenix活跃商家有两个不同用户的长评论，应计1家。active按原查询小写以隔离去重问题。', [[1]],
        **{'public_review_platform.Business': [dict(business_id=1, city='Phoenix', active='true')],
           'public_review_platform.Reviews': [dict(business_id=1, user_id=i, review_length='Long') for i in (1, 2)]})
    add(4682, '年龄和国家问题采用知识188/189；美国受访者20岁，加拿大受访者40岁；平均美国年龄20。', [[20.0]],
        **{'mental_health_survey.Question': [dict(questionid=1, questiontext='What is your age?'), dict(questionid=3, questiontext='What country do you live in?')],
           'mental_health_survey.Survey': [dict(SurveyID=2014, Description='mental health survey for 2014')],
           'mental_health_survey.Answer': [dict(UserID=u, SurveyID=2014, QuestionID=q, AnswerText=a) for u, q, a in ((1, 1, '20'), (1, 3, 'United States'), (2, 1, '40'), (2, 3, 'Canada'))]})
    add(5921, 'San Jose一条行程，文本日期8/29/2013 12:00:00，应计1。', [[1]],
        **{'bike_share_1.station': [dict(id=1, city='San Jose')],
           'bike_share_1.trip': [dict(id=1, start_station_id=1, start_date='8/29/2013 12:00:00')]})
    add(4324, '条件性反例：Years代理键7对应2012，用户1引用7。此数据分布并非已知真实值。', [[1]],
        **{'public_review_platform.Years': [dict(year_id=7, actual_year=2012)],
           'public_review_platform.Elite': [dict(user_id=1, year_id=7)]})
    add(5028, '条件性反例：同一交易日含咖啡因饮料一瓶一罐，按严格Bottle容器数每日1瓶。', [[1.0]],
        **{'beer_factory.rootbeerbrand': [dict(BrandID=1, Caffeinated='TRUE')],
           'beer_factory.rootbeer': [dict(RootBeerID=i, BrandID=1, ContainerType=t) for i, t in ((1, 'Bottle'), (2, 'Can'))],
           'beer_factory.transaction': [dict(TransactionID=i, RootBeerID=i, TransactionDate='2014-08-01') for i in (1, 2)]})
    add(5311, '条件性反例：Costa Rica两地址，只有San Jose关联客户；客户城市集合仅San Jose。', [['San Jose']],
        **{'books.country': [dict(country_id=1, country_name='Costa Rica')],
           'books.address': [dict(address_id=i, city=n, country_id=1) for i, n in ((1, 'San Jose'), (2, 'Liberia'))],
           'books.customer': [dict(customer_id=1)],
           'books.customer_address': [dict(customer_id=1, address_id=1)]})
    return cases


def equivalent(actual, expected):
    def row_key(row):
        return json.dumps(row, ensure_ascii=False)
    actual, expected = sorted(actual, key=row_key), sorted(expected, key=row_key)
    return len(actual) == len(expected) and all(
        len(a) == len(b) and all(
            math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-9) if isinstance(x, (int, float)) and isinstance(y, (int, float)) else x == y
            for x, y in zip(a, b)) for a, b in zip(actual, expected))


def run_counterexamples(rows, catalog):
    results = []
    for qid, case in fixture_cases().items():
        sql = rows[qid]['query']
        connection = sqlite_schema(catalog, sql)
        try:
            for table, records in case['tables'].items():
                database, name = table.split('.', 1)
                for row in records:
                    columns = ','.join('"' + name + '"' for name in row)
                    placeholders = ','.join('?' for _ in row)
                    connection.execute(f'INSERT INTO "{database}"."{name}" ({columns}) VALUES ({placeholders})', list(row.values()))
            actual = [list(row) for row in connection.execute(sql).fetchall()]
        finally:
            connection.close()
        if equivalent(actual, case['expected']):
            raise AssertionError(f'Counterexample did not demonstrate a difference: {qid}')
        results.append(dict(question_id=qid, query=sql, **case, actual=actual,
                            demonstrates_difference=True, synthetic_data_only=True))
    return results


def administrative_evidence():
    path = next((ROOT / 'data/reference').rglob('administrative_division.sql'))
    text, encoding = read_text(path)
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE administrative_division (id TEXT,name TEXT,full_name TEXT,province_name TEXT,city_name TEXT,county_name TEXT,level INTEGER,longitude REAL,latitude REAL)')
    connection.executescript(text)
    columns = ['id', 'name', 'full_name', 'province_name', 'city_name', 'county_name', 'level', 'longitude', 'latitude']
    selected = [dict(zip(columns, row)) for row in connection.execute("SELECT * FROM administrative_division WHERE id LIKE '6328%'")]
    city_matches = connection.execute("SELECT COUNT(*) FROM administrative_division WHERE city_name='格尔木市'").fetchone()[0]
    connection.close()
    return dict(file=str(path.relative_to(ROOT)), encoding=encoding,
                haixi_rows=selected, city_name_geermu_matches=city_matches)


def report(data):
    summary = data['summary']
    lines = ['# 上一轮400条提交结果分析', '',
        '本报告保留历史提交的处理前诊断；后续修正及当前验证状态见[RAG与校验改进报告](RAG与校验改进报告.md)。', '',
        f"分析时间：{data['generated_at']}。对象：[result.json](../../outputs/submission/result.json)，历史运行：`predict-ee19c0abd953`。", '',
        '**结论：存在明显问题。400条格式合格不代表查询能执行，更不代表执行结果符合题意。**', '',
        f"全量基础检查400/400通过；使用原始DDL编译全部200条SQLite查询，197条通过、3条失败。另确认1条MySQL方言错误、1条ES结构/字段错误，共5条结构问题。对16个语义/知识/条件性风险构造最小数据，原查询都与按题意及注明口径得到的预期不同。", '',
        '没有连接比赛实际数据库，也没有逐题金标或平台失败ID。下面的反例证明查询存在错误路径，不能据此宣称这些ID一定是平台判错题，不能将197/200当作执行准确率，也不能从本报告重新计算官方分数。', '',
        '平台文件记录317/400、79.25%，总分849.5/1087、78.15%；其中SQLite136/200，占83条未全对题中的64条，是当前最值得投入的部分。知识题162/226也提示要优先补领域知识和精确值召回。平台记录中PG/联邦按非空判定，因此这两类得分较高也不足以证明业务语义正确；其描述与官网标准摘录并不完全一致，需以实际评测实现为准。', '',
        '## 方法与证据范围', '',
        '- 对照400条测试题，检查ID覆盖、重复、源类型、基础校验及历史预测一致性。',
        '- 对全部200条SQLite在内存数据库加载原始CREATE TABLE DDL，执行EXPLAIN；检查语法与字段绑定，不读取比赛数据。',
        '- 对50条ES检查嵌套查询子句形状与mapping排序字段；对MySQL核对FULL OUTER JOIN。ES检查不是完整DSL解析器。',
        '- 阅读SQL/DSL、Schema、knowledge.json、真实参考区划数据；回看生成时保存的检索示例、知识及字典。原因均为基于这些证据的推断，不声称能直接观察模型内部思维。',
        '- 构造16组最小数据执行原SQL，记录输入、原输出与人工按题意计算的预期。条件性风险的反例只证明在所述前提下会错。',
        '- 原result.json保持原样；输出SHA256为`' + data['submission_sha256'] + '`。本次未调用模型、未修正生产SQL、未重新生成提交。', '',
        '| 检查 | 结果 |', '|---|---|',
        f"| 测试题覆盖 | {summary['coverage_count']}/400，无重复/缺失/多余ID |",
        f"| 历史预测与提交一致 | {summary['historical_prediction_match_count']}/400 |",
        '| 基础格式/词法检查 | 400/400通过 |',
        '| SQLite原始DDL编译 | 197/200通过；4976、5029、5686失败 |',
        '| MySQL方言确定问题 | 1825 |',
        '| ES嵌套/排序字段确定问题 | 2399 |',
        '| 构造反例 | 16/16复现输出差异；非真实集准确率 |', '',
        '## 问题清单', '',
        '同一ID可以同时有输出字段问题和后续口径变化；以下按问题记录分类，不把重复ID加总为错误题数。未列出的题也不保证正确。', '',
        '| 类别 | 记录数 |', '|---|---|']
    for category, label in CATEGORIES.items():
        lines.append(f"| {label} | {summary['finding_category_counts'].get(category, 0)} |")
    lines.extend(['', '| 题号 | 类别 | 问题 |', '|---|---|---|'])
    for item in data['findings']:
        lines.append(f"| {item['question_id']} | {CATEGORIES[item['category']]} | {item['title']} |")
    lines.extend(['', '## 逐项分析', ''])
    by_id = {item['question_id']: item for item in data['counterexamples']}
    for item in data['findings']:
        qid = item['question_id']
        lines.extend([f"### {qid}：{item['title']}", '',
            f"题目：{item['question']}", '',
            f"判定：{CATEGORIES[item['category']]}。", '',
            '```' + ('json' if item['source'] == 'Elasticsearch' else 'sql'),
            '\n'.join(item['queries']), '```', '',
            f"证据：{item['evidence']}", '',
            f"原因推断：{item['cause_inference']}", '',
            f"改进方向：{item['recommendation']}", ''])
        trace = item['retrieval_trace']
        lines.append('历史检索：示例题号 `' + ', '.join(map(str, trace['example_ids'])) + '`；知识ID `' + ', '.join(map(str, trace['knowledge_ids'])) + '`。')
        if item['relevant_knowledge_ids']:
            lines.append('相关知识召回核对：已召回 `' + ', '.join(map(str, trace['relevant_present'])) + '`；未召回 `' + ', '.join(map(str, trace['relevant_missing'])) + '`。')
        lines.append('')
        if qid in by_id:
            case = by_id[qid]
            lines.extend([f"构造反例：{case['description']}", '',
                f"原SQL输出：`{json.dumps(case['actual'], ensure_ascii=False)}`；按注明口径预期：`{json.dumps(case['expected'], ensure_ascii=False)}`。", ''])
    lines.extend(['## 原因归纳及改进顺序', '',
        '1. **先补执行前检查。** SQLite原始DDL编译可直接挡住3条硬错误；MySQL必须做目标方言约束，ES要递归检查子句并绑定mapping字段。旧检查只保证JSON/根结构/表名/只读词法，所以这些输出不会触发修复轮次。',
        '2. **纠正实体粒度，再做统计。** 商家/球员/游戏/角色问题反复出现JOIN后的COUNT错误。按实体主键去重、按业务粒度预聚合、用EXISTS表达存在性，不能把DISTINCT机械地加到所有查询。',
        '3. **清洗错误训练SQL和矛盾知识。** 4123沿用4298的错误分母是强证据；4207的知识同时包含裸减法与AM/PM字符串定义。当前清洗主要解决结构、重复与跨年冲突，没有保证所有非冲突样本的业务正确性。仅增加召回量或embedding不能修复错误知识。',
        '4. **先确定数据源，再补必须的领域事实。** 对4682确保年龄/国家问题ID及值召回，对3576确保单位及公式，对649/846确保区划行及层级。字典、知识和示例都要标记来源并按目标库约束；字段相同不代表枚举相同。需要从计算公式递归召回依赖，例如PB还依赖BPS定义。',
        '5. **把时间和地理常量交给确定规则。** 检查日期字符串样例、使用半开区间、转换AM/PM、用程序转度分坐标。题目提供知识仍有生成落笔错误，不能只靠检索。',
        '6. **以新口径生成新版本后再评测。** 旧教育输出没有跨年合并规则；处理前调用年份识别器，1737日期20240101被识别为explicit_year/2024；当时船舶来源裁定只覆盖油轮，未补货船。这两项后续已经处理，状态见RAG与校验改进报告。修正规则对完整题目精确匹配或RAG示例生效，不代表旧400条已经修复。', '',
        '## 审计中避免的误判', '',
        '- 不把“整数除法”一律当错误：3202的Stars > Forks*1/3，3348的整数计数 > 均值，在当前整数严格大于比较中截断可能不改变真值，不能仅凭运算符判错。',
        '- 不以通用财务定义覆盖本地知识：418的每股净资产按知识1624、537的波动率按知识1640等，应首先尊重赛题显式口径。',
        '- 不把解析后catalog列缺失当真实DDL缺列：language_corpus首行列wid等在原始DDL中存在；Disney带空格/连字符标识符也有元数据提取缺陷。编译使用完整DDL，所以未将这些解析问题误算成提交错误。元数据提取本身仍可影响召回质量。',
        '- 题目“统计值”、代理键与实际年份、自然语言“瓶”、国家路由没有唯一确证时列为条件性风险。',
        '- 不把后来增加的跨年合并、优先2025规则回推成旧提交当时的确定错误，也不猜83条平台失败题的具体ID。', '',
        '## 文件及复现', '',
        '- [结构化分析及完整检索证据](../../artifacts/analysis/' + OUT.name + '/analysis.json)',
        '- [400条检查清单](../../artifacts/analysis/' + OUT.name + '/audit.csv)',
        '- [16组反例数据和输出](../../artifacts/analysis/' + OUT.name + '/counterexamples.json)',
        '- [审计脚本](../../scripts/analyze_last_submission.py)：在项目根目录运行 `python scripts/analyze_last_submission.py`。脚本绑定该提交SHA256，提交变化后需要重新审核语义清单。',
        '- [平台得分记录](得分结果.md)、[官网标准摘录](../competition/evaluation_standard.md)、[当前教育合并规则](education_year_merge.md)、[当前字段/枚举裁定](data_consistency.md)。', '',
        '方言/DSL外部依据：MySQL JOIN支持的语法不包含FULL OUTER JOIN，见[MySQL官方JOIN说明](https://dev.mysql.com/doc/refman/8.4/en/join.html)；ES多个过滤条件应使用独立查询子句，见[Elastic官方filter示例](https://www.elastic.co/docs/reference/query-languages/query-dsl/query-filter-context)，默认对未映射字段排序会失败，见[Elastic官方排序说明](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/sort-search-results)。这些文档支持结构核对，不提供比赛逐题答案。', ''])
    REPORT.write_text('\n'.join(lines), encoding='utf-8')


def main():
    submission = ROOT / 'outputs/submission/result.json'
    original = submission.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest != PINNED_SHA256:
        raise SystemExit('提交已变化；请重新审核语义清单，不能对新版本沿用本脚本结论。')
    rows_list = json.loads(original)
    rows = {row['question_id']: row for row in rows_list}
    questions = {row['question_id']: row['question'] for row in load(ROOT / 'data/raw/subject/QA_test.json')}
    catalog = load(ROOT / 'data/processed/catalog.json')
    assert len(rows) == len(rows_list) == 400 and rows.keys() == questions.keys()
    mapping = next(t['mapping'] for t in catalog if t['source'] == 'Elasticsearch')
    checks = []
    historical_matches = 0
    for row in rows_list:
        qid = row['question_id']
        record = load(RUN / f'{qid}.json')
        prediction = {key: value for key, value in row.items() if key != 'question_id'}
        same = prediction == record['prediction']
        historical_matches += same
        check = dict(question_id=qid, question=questions[qid], source=source(row),
                     historical_prediction_matches=same, basic_errors=validate(prediction, catalog, structural=False),
                     sqlite_prepare=None, sqlite_error=None, es_errors=[])
        if row.get('db') == 'SQLite':
            connection = sqlite_schema(catalog, row['query'])
            try:
                connection.execute('EXPLAIN ' + row['query'])
                check['sqlite_prepare'] = 'passed'
            except sqlite3.Error as error:
                check['sqlite_prepare'] = 'failed'
                check['sqlite_error'] = str(error)
            finally:
                connection.close()
        elif row.get('db') == 'Elasticsearch':
            check['es_errors'] = es_errors(json.loads(row['query']), mapping)
        checks.append(check)
    assert historical_matches == 400
    assert not any(item['basic_errors'] for item in checks)
    assert {item['question_id'] for item in checks if item['sqlite_prepare'] == 'failed'} == {4976, 5029, 5686}
    assert {item['question_id'] for item in checks if item['es_errors']} == {2399}
    full_join_ids = [row['question_id'] for row in rows_list if row.get('db') == 'MYSQL' and re.search(r'\bFULL\s+(?:OUTER\s+)?JOIN\b', row['query'], re.I)]
    assert full_join_ids == [1825]
    connection = sqlite_schema(catalog, rows[5029]['query'])
    grammar_repaired = rows[5029]['query'].split('WHERE T2.BrandID IN')[0] + 'WHERE T2.BrandID IN (SELECT BrandID FROM beer_factory.rootbeerbrand)'
    try:
        connection.execute('EXPLAIN ' + grammar_repaired)
        raise AssertionError('Expected ContainerType binding failure')
    except sqlite3.Error as error:
        secondary = dict(question_id=5029, grammar_only_rewrite=grammar_repaired, error=str(error))
        assert 'T2.ContainerType' in str(error)
    finally:
        connection.close()
    findings = []
    for item in FINDINGS:
        qid = item['question_id']
        context = load(RUN / f'{qid}.json')['context']
        known_ids = [k['knowledge_id'] for k in context['knowledge']]
        trace = dict(context_file=str((RUN / f'{qid}.json').relative_to(ROOT)),
                     context_keys=list(context), example_ids=[e['question_id'] for e in context['examples']],
                     knowledge_ids=known_ids, relevant_present=[k for k in item['relevant_knowledge_ids'] if k in known_ids],
                     relevant_missing=[k for k in item['relevant_knowledge_ids'] if k not in known_ids],
                     examples=context['examples'], knowledge=context['knowledge'], dictionary=context['dictionary'])
        findings.append(dict(**item, question=questions[qid], source=source(rows[qid]),
                             queries=queries(rows[qid]), retrieval_trace=trace))
    counterexamples = run_counterexamples(rows, catalog)
    admin = administrative_evidence()
    assert admin['city_name_geermu_matches'] == 0
    assert any(row['id'] == '632801' and row['county_name'] == '格尔木市' for row in admin['haixi_rows'])
    summary = dict(coverage_count=len(rows), historical_prediction_match_count=historical_matches,
                   source_counts=dict(collections.Counter(source(row) for row in rows_list)),
                   sqlite_compile_count=200, sqlite_compile_passed=197, sqlite_compile_failed=3,
                   es_structural_flag_ids=[2399], mysql_full_outer_join_ids=full_join_ids,
                   finding_category_counts=dict(collections.Counter(f['category'] for f in findings)),
                   finding_record_count=len(findings), finding_unique_question_count=len({f['question_id'] for f in findings}),
                   synthetic_counterexample_count=len(counterexamples), official_execution_accuracy=None)
    data = dict(generated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                submission_file=str(submission.relative_to(ROOT)), submission_sha256=digest,
                summary=summary, checks=checks, findings=findings, counterexamples=counterexamples,
                secondary_compile_failure=secondary, administrative_evidence=admin,
                limitations=['No real competition database or per-question gold/output.',
                             'Synthetic counterexamples are not an accuracy estimate.',
                             'Semantic inventory is pinned to this historical submission.',
                             'MySQL dialect and ES checks were local/offline, not native-server execution.'])
    OUT.mkdir(parents=True, exist_ok=True)
    for name, value in (('analysis.json', data), ('counterexamples.json', counterexamples)):
        (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with (OUT / 'audit.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['question_id', 'source', 'question', 'basic_errors', 'sqlite_prepare', 'sqlite_error', 'es_errors', 'finding_categories', 'findings'])
        writer.writeheader()
        for check in checks:
            related = [f for f in findings if f['question_id'] == check['question_id']]
            writer.writerow({key: check.get(key) for key in writer.fieldnames if key not in ('finding_categories', 'findings')} |
                            dict(finding_categories=';'.join(f['category'] for f in related), findings=';'.join(f['title'] for f in related)))
    report(data)
    assert submission.read_bytes() == original
    print(json.dumps(dict(**summary, report=str(REPORT.relative_to(ROOT)), artifacts=str(OUT.relative_to(ROOT)), submission_unchanged=True), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
