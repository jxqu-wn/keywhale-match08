# 7题ReAct提示词与外部知识审阅

依据：此前保存的真实请求、响应与工具轨迹；本次未调用模型、未改写生成结果。原始训练SQL仅用于比较，不视为无条件正确的标准。

## 3040：哪一天的订单数量最多？该天的总订单数量是多少？

结论：不理想：跨库Schema与知识不一致

首轮routing指向book_publishing_company，候选却同时包含books与ecommerce等33张表。模型只获取books的4张表Schema，最终使用books.cust_order按DATE(order_date)统计COUNT(*)。知识工具却仍返回出版公司sales的qty/ord_date/ord_num等信息，没有提供所选books.cust_order的对应业务知识。

原始训练标注使用book_publishing_company.sales，按ord_date计算SUM(qty)。题干独立看没有库名且“订单数量”存在口径歧义，不能仅凭标注认定自然语言唯一解；但本次输出明显偏离训练记录的目标库和统计口径。知识ID1、555、554、556支持目标出版库，526、527（折扣上下限）、1766（占比公式）与题意无关。

理想上下文应明确订单笔数与订购件数的区别、所属库、日期粒度。关键问题是Schema允许跨库检索，而search_knowledge固定使用初始scope；本次两类证据失配后check_query仍通过。

[各轮提示词、响应与原始标注](3040.md)；[结构化外部证据](3040.evidence.json)

## 3041：1992年订购数量最多的书名是什么？

结论：基本可用：核心知识命中，但噪声较多

ID556和1解释sales.qty是销售数量，555解释ord_date，568解释titles.title，558/567解释title_id；Schema提供sales到titles的关联。因此年筛选、返回书名和按书聚合均有依据。

版税表知识3、付款方式557、作者书籍关系564不是必需信息。首轮44张候选表还来自books、movie_3和ecommerce，范围偏宽。

最终SQL按书SUM(qty)再排序，原始标注按单条qty排序。根据“1992年订购数量最多的书名”的一般解释，聚合同一本书所有订单更合理；不能把偏离原始标注直接记为错误。理想知识应明确统计对象是书籍，全年订购量为SUM(qty)。

[各轮提示词、响应与原始标注](3041.md)；[结构化外部证据](3041.evidence.json)

## 3044：列出书名ID为 'BU2075' 且在10000到50000范围内的书名和版税百分比。

结论：部分理想：知识引导发现正确表，边界口径未说明

模型最初只查titles。知识ID3明确roysched.royalty表示版税百分比、lorange/hirange表示区间上下限，促使模型再次查roysched Schema；这是工具检索实际发挥作用的正例。

titles.royalty的知识573/2与roysched.royalty同名，可能混淆；1766是类别数量占比，与版税百分比无关。未返回单独的lorange/hirange字段条目，但ID3已覆盖含义。

最终条件为lorange >= 10000 AND hirange <= 50000；原始标注为>和<。题干“10000到50000范围内”未说明端点是否包含、区间完全包含还是相交，现有知识未消除歧义。应提供明确业务定义，而非凭旧示例替代定义。

[各轮提示词、响应与原始标注](3044.md)；[结构化外部证据](3044.evidence.json)

## 3090：出版社中哪个员工的职位级别最高？给出他的全名。

结论：知识基本足够，最终输出未完整利用

知识ID6给出fname、lname、minit（中间名）和job_lvl；534明确数值越高职位级别越高。employee Schema包含这四个字段，足以支持按员工job_lvl取最高者。

jobs.max_lvl相关知识540/7和min_lvl知识539属于职位等级范围，可能干扰“员工实际级别”判断；出版社国家与名称、pub_info、titles.pub_id等信息没有必要。

最终只拼接fname与lname，遗漏已有知识明确说明的minit；原始标注返回fname/minit/lname。因此主要问题是“全名”返回列检查未发挥作用，而不是缺少中间名知识。可明确全名包含非空minit，并处理空值。

[各轮提示词、响应与原始标注](3090.md)；[结构化外部证据](3090.evidence.json)

## 3099：1993年，Fremont 的书店比 Portland 的书店在图书销售数量上多出百分之多少？

结论：不理想：错误同题示例与不匹配公式混入

知识556/1/555/553提供销售数量、日期和商店关联键；stores.city在Schema中可见。但ID1766是COUNT(*)/总体COUNT(*)的数量占比公式，本题需要相对增幅(Fremont数量-Portland数量)/Portland数量，统计对象和分母均不同。版税知识3、付款方式557、ytd_sales574也是噪声。

这是7题中唯一实际调用search_examples的题目。返回的第一条就是3099本题，示例却以Fremont的销量作分母，按题意应以Portland作分母。第三条3146来自codebase_comments，库不同，但其相对增幅公式以被比较者ID19为分母；可作计算结构参考，不能作为当前库字段知识。

最终模型使用Portland分母，纠正了同题示例。这不能证明检索理想：提示词把search_examples描述为“已修正训练示例”，但本次仍返回存在口径问题的原始标注。理想上下文应给出本题相对增幅公式，并对同题错误示例进行修正或隔离。

[各轮提示词、响应与原始标注](3099.md)；[结构化外部证据](3099.evidence.json)

## 3877：游戏2 Games in 1: Sonic Advance & ChuChu Rocket!的类型是什么？

结论：关键知识齐全，但强制知识过多；最终参数丢失

ID122明确game_name是游戏名、game.genre_id关联genre.id；126/1543和107/1544解释类型标识与名称。Schema只需game和genre，关键关联与输出字段均已覆盖。

ID120、121、123、1539被标为“字段/公式依赖，必须同时提供”，却描述发行年份、游戏平台记录及多表销售关联，本题并不需要。强制条目占10条结果中的4条，强制依赖按整个video_games域触发，粒度过粗。1544/1534还含较长的一般常识叙述。

最终WHERE game_name = 'Games in 1: Sonic Advance & ChuChu Rocket!'，遗漏题干及原始标注中的开头2；应该保留'2 Games in 1: Sonic Advance & ChuChu Rocket!'。这属于题目字面参数保真失败，现有字段知识并不缺失。无真实数据执行，不能断言实际返回行数。

[各轮提示词、响应与原始标注](3877.md)；[结构化外部证据](3877.evidence.json)

## 6204：列出菲律宾国家的至少3个城市。

结论：核心知识命中，但噪声和跨源裁决明显

ID354明确Philippines对应菲律宾；1092/355提供city.country_id含义与城市国家关系，1094/1091提供国家ID和城市名称。city/country Schema齐全，足够支持查询。

369（租借数量差）、390/391（意大利租借花费）、365（印度客户占比）均不相关。首轮候选表达85张，来自11个库。首轮及知识结果还携带common.administrative_division的id/division_code裁决，这是另一套库的行政区划定义，因“城市”关键词触发，不能为movie_3查询提供依据。

模型先生成SQL，被检查要求补做知识检索；检索后SQL不变。因此知识至少确认了国家名称，但未促使新逻辑调整。最终LIMIT 3，原始标注不限制条数。题干“至少3个”允许存在多种输出解释；LIMIT 3只在匹配记录充足时返回3个，未检查数量。这里缺少的是输出数量口径，不是城市关联知识。

[各轮提示词、响应与原始标注](6204.md)；[结构化外部证据](6204.evidence.json)

## 共性问题与代码依据

1. 初始候选按相关示例扩展整库Schema，弱路由允许保留跨库候选；本组候选数量为15到85张。不是所有候选表都有完整定义进入首轮，但表名本身会影响模型选库。
2. QueryTools.__init__固定self.scope=初始routing；get_schema可以跨候选域选表，但search_knowledge不随已选Schema重新确定域。3040展示了由此产生的证据失配（src/text2query/react_tools.py:69、108）。
3. mandatory_knowledge对video_games无条件追加120/121/123/1539，与实际查询涉及的字段没有对应（src/text2query/retrieval_context.py:79）。
4. compatible_example在weak路由直接返回True，允许检索其他库的示例；3099返回codebase_comments（src/text2query/retrieval_context.py:56）。
5. schema_adjudications按引用表命中或正则命中选择，正则触发未同时约束当前源库；6204因“城市”得到另一套行政区划裁决（src/text2query/model_pipeline.py:279）。
6. search_examples工具说明称“已修正训练示例”，但并非每个返回项都已审定；3099的同题错误分母仍在结果里。
7. 本组所有dictionary、query_helpers为空，business_rules也为空；缺少日期、数量、区间端点和相对百分比等题目所需口径的明确说明。系统提示词还包含许多与本组无关的医院、教育、AIS等固定规则。
8. 本组最终均通过check_query，但3040的库/口径、3090全名列和3877字面参数问题未被阻止。流程通过不能据此认定语义正确。

## 优先改进建议

先解决Schema与知识检索的域一致性，再按本题统计对象及已获取字段筛选知识，替换按整个数据库强制注入的规则。公式区分数量占比、相对增幅和字段已有百分比。仅把已审定示例标为已修正。跨源Schema裁决必须满足源库适用条件。对完整姓名和题目字面参数增加有针对性的检查。改进应通过定向对比验证，本次只完成审阅和提取，未修改检索逻辑或重新运行模型。
