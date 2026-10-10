# DeepSeek Flash与Qwen：相同40题SQLite ReAct对比

日期：2026-10-10。本轮重新调用`deepseek-flash`，与上一轮`qwen-track8-text2sql`的保存结果比较，没有复用更早的DeepSeek预测。DeepSeek服务的模型列表将`deepseek-flash`标为`DeepSeek-V4.1-Flash`。

## 结果

**DeepSeek Flash完成40题，与原始训练答案不一致12题（30%）；Qwen不一致8题（20%）。** 忽略空白及末尾分号后，DeepSeek仍为30%，Qwen为15%。本次DeepSeek批量耗时53.9秒，Qwen为241.3秒。

| 指标 | Qwen Track8 | DeepSeek Flash本轮 |
|---|---:|---:|
| 相同样本 / 成功数 | 40 / 40 | 40 / 40 |
| 与原训练答案严格一致 | 32，80% | 28，70% |
| 与原训练答案严格不一致 | 8，20% | 12，30% |
| 忽略排版及末尾分号后不一致 | 6，15% | 12，30% |
| 批量耗时 | 241.3秒 | 53.9秒 |
| 模型响应总数 | 244 | 270 |
| ReAct动作生成 / 独立语义复核 | 202 / 42 | 218 / 52 |
| 候选检查失败 | 2 | 13 |
| 人工修改预测SQL / 生成失败 | 0 / 0 | 0 / 0 |
| 实际数据库执行准确率 | 未测量 | 未测量 |

两模型预测SQL严格相同25题，不同15题（37.5%）；忽略排版及末尾分号后相同26题，不同14题（35%）。相对于原始训练答案：

| 逐题组合 | 数量 | 题号 |
|---|---:|---|
| 两者均严格一致 | 24 | 见逐题对照 |
| 仅Qwen严格一致 | 8 | 4176、4759、5688、4358、3521、4442、5988、3295 |
| 仅DeepSeek严格一致 | 4 | 4575、5023、3051、4340 |
| 两者均不严格一致 | 4 | 4478、3772、4554、6015 |

Qwen与标注更一致，DeepSeek在本轮服务下完成得更快。**不一致率更高不能直接推出DeepSeek正确率更低**：它补充了部分原标注遗漏的条件，也出现了对知识的误读、等价查询误报和不完整改写。

## 比较条件与限制

直接复用Qwen运行目录的`sample.json`，逐字段核对这40题的题目和原始SQL，没有重新抽不同题。随机种子仍为`20261010`，母体为2991道SQLite训练题。

启动前校验两轮的生产代码哈希、工作流版本5、系统提示、原始资料清单及哈希、每题最多24步、8路并发、temperature=0、4096输出上限和资料去重设置一致。40题**实际首轮消息逐字相同**；只有模型及供应方参数不同。

两轮均使用当前原始资料模式：同题训练案例 → SQLite原始Schema → 相关知识 → 格式、Schema、原始DDL编译检查及独立LLM语义复核 → finish。DeepSeek负责本轮的生成和复核；Qwen负责上一轮的生成和复核。因此比较的是两个模型各自完整ReAct流程的行为，不能把差异全部归因于首次生成能力。后续资料检索随各自的工具选择变化。

两轮都采用相同去重函数，删除重复原始标注、DDL副本、字段解析表示及来源哈希等元数据，保留原始问题、SQL、DDL、知识文本和完整历史。没有使用人工修订知识、问答、业务规则，也没有人工改SQL。

服务条件有差别：Qwen位于`http://192.168.15.119:8000/v1`，上下文上限6000 tokens，逐次测量输入并降低输出预算；DeepSeek位于`https://api.deepseek.com`，服务模型列表返回更长上下文，本轮保留4096输出预算。所有正式响应均正常`stop`结束，无输出长度截断。53.9秒与241.3秒是各自服务下的一次端到端耗时，不是同硬件性能测试。

**两轮40题各自的原始训练答案都在实际首轮请求中。** 本实验是训练答案一致性比较，不是未见题泛化评测。未取得真实SQLite业务记录，不能给出执行正确率。

## 差异来自哪里

DeepSeek的12道不一致题中，10道从原答案出发，被语义复核要求修改；4478在复核返回`uncertain`及修改建议后调整；只有3772在首次检查前主动修改。Qwen则有6道在首次检查前改写，2道由复核要求修改。

DeepSeek共做54次候选检查，其中52次进入独立语义复核：40次accept、11次revise、1次uncertain。13次检查失败包括11次语义否决和2次工具参数格式错误（3788、4322）；这两道格式错误最终修复，预测与原答案一致。4478的uncertain没有被程序当作失败，生成模型仍主动采纳建议。

下面覆盖两轮不一致题的并集16题，其中3772的最终预测相同。

| 题号 | Qwen行为 | DeepSeek本轮行为 | 语义判断 |
|---|---|---|---|
| 4575 | 删除末尾分号。 | 保留原SQL。 | 仅格式差异。 |
| 4176 | 用Checkins的`label_time_4='None'`判断周四不营业。 | 改为周四不存在Business_Hours行，并统计不同商家。 | 改变营业口径。复核与原知识152/1358约定冲突，新式未检查营业时间字段，不能直接认定更正确。 |
| 4478 | 年份改为整数比较，未筛Winner。 | 年份改为整数比较，并加`result='Winner'`。 | 排除提名更贴合“获得”奖项及知识1416/172；Qwen保留了原标注的缺漏。 |
| 3772 | 复核后按不同game_id统计游戏数。 | 首次检查前按不同game_id统计。 | 两模型最终SQL相同；有重复关系时都可能与按关系行数排序的原答案不同。 |
| 4759 | 保留characters与票房表连接。 | 以票房表为分母全集，使用标题集合判断歌曲。 | 正常完整外键数据下通常等价。复核的“重映导致比例放大”理由不成立；NULL或未匹配标题可能改变分母。 |
| 5023 | 年份边界使用整数。 | 保留数字字符串边界。 | INTEGER字段亲和性使其等价。 |
| 3051 | 给payterms、qty补T1前缀。 | 保留原字段写法。 | 字段只来自sales，等价。 |
| 4340 | 加入题目指定的`state='AZ'`。 | 保留原SQL，仅过滤Avondale城市名。 | 若其他州有同名城市，Qwen筛选更完整；实际影响取决于数据。 |
| 5688 | 保留对手连接后对同一W/G求平均。 | 直接取该年球队W/G。 | 单一球队、存在对手记录且G非零时通常等价；无对手记录等边界不同。 |
| 4554 | 只筛出生信息。 | 补Credit/Episode、第20季、Cast及去重。 | DeepSeek落实了题目中的季数和演员身份要求；结果可能缩小。 |
| 4358 | 按商家与属性连接后的行数计算比例，返回裸属性值。 | 独立统计Mesa商家比例，LEFT JOIN属性，增加business_id。 | 改善分子粒度并保留无属性商家；新增输出列仍可能影响评测匹配。 |
| 3521 | 保留DSF及rank升序。 | 将轮次改为F，rank改为降序。 | 排序修正有知识依据；轮次知识存在冲突，且两者均缺少year连接条件。 |
| 6015 | 排除其他店库存，但仍连接2号店库存。 | 使用EXISTS及NOT EXISTS。 | 两者都实现排他性；DeepSeek避免同一电影多个库存副本造成重复标题。 |
| 4442 | 保留小写active及仅按BYOB属性名筛选。 | active改为True，排除None/No/FALSE属性值。 | 有原始知识支持，但否定值集合不完整，仍可能计入False。 |
| 5988 | 返回国家。 | 将United States映射为North America，其余NULL。 | 回答方向改为洲，但映射不完整，且未获得Clarksville实际所属国家。 |
| 3295 | 按球员比赛角色记录计数，仅匹配Captain。 | 按不同球员计数，LEFT JOIN包含无比赛球员，覆盖CaptainKeeper。 | 对统计对象、分母和角色范围的修正有明确依据。 |

15道模型间文本差异中，5道是格式、等价或通常等价改写，另10道有潜在结果差异。这是人工语义归类，不能把10/40（25%）理解为实际数据库测量值或错误率；“通常等价”也有下述边界条件。

## 复核中值得关注的问题

**4176不是没有检索到规则，而是复核误读了规则。** 原知识152明确把`label_time_4='None'`用于周四不营业，1358也把当天None记录解释为不营业。DeepSeek复核却引用1358声称“仅无签到，不能等同不营业”，随后用“营业时间行不存在”代替。新SQL没有区分“有营业时间行但时间为空”和“数据缺失”，其正确性没有得到现有证据证明。

**4759的重复计数理由不成立。** characters的movie_title是主键，一条票房行最多关联一个characters行；重映票房本来就是票房表中的独立行，原式和新式都将其各汇总一次。若非空标题遵守`movies_total_gross → characters`外键，票房行也不会因为反向覆盖问题被INNER JOIN丢失。NULL标题或不遵守外键的记录才是需要单独判断的边界，不能将通常等价查询当作明确错误。

**3521修正后仍可能跨年份关联。** DeepSeek改正排序方向并选择总决赛F，但连接仍只有`tmIDWinner=tmID`，未加`series_post.year=teams.year`。同一球队有多个赛季时，返回年份可能与所选季后赛记录无关；Qwen同样存在这个缺漏。

**5988的映射只覆盖美国。** 原查询没有回答“洲”，DeepSeek发现了这个问题；但生成的CASE只支持United States，对其他国家返回NULL。此次没有读取真实数据库来证明Clarksville只对应美国，补查知识后的最终复核输入也没有保留1095的洲映射文本。复核接受不能代替数据及映射依据。

**4358、4442也有残留问题。** 4358增加business_id使输出从两列变三列；业务上更便于对应属性，仍可能不满足目标结果列要求。4442过滤了`FALSE`，未过滤原知识144同时出现的`False`，不能把否定形式列表当作完整枚举。

## 原始DDL构造执行

对上述16题建立25组小数据场景，执行原答案、Qwen和DeepSeek的最终SQL，没有向模型追加请求，也没有修改预测。构造库沿用SQLite默认的`foreign_keys=OFF`；分析同时考虑原DDL中的键和外键声明，NULL及缺失数据场景单独记录。它们用于展示机制，不代表真实比赛数据。

| 场景 | Qwen输出 | DeepSeek输出 | 说明 |
|---|---|---|---|
| 4478：同年同奖项有获奖及提名记录 | 8.5、7.0 | 8.5 | Winner条件排除提名。 |
| 4554：出生信息匹配，但只有第19季参演 | 姓名 | 空结果 | DeepSeek落实第20季范围。 |
| 3295：3名出生年份匹配球员，一人两次Captain、一人Keeper、一人无比赛 | 66.67% | 33.33% | 角色行比例变为球员比例，分母包含无比赛球员。 |
| 6015：独有电影在2号店有两个库存副本 | 独有、独有 | 独有 | EXISTS避免同一film重复。 |
| 4358：Mesa一家商户有两条属性，Phoenix一家 | 100%、一个属性 | 两行50%、商家ID及属性 | 原式按属性连接行计数；DeepSeek改变比例、行数及列数。 |
| 4176：周四营业时间行存在，但一家的时间为NULL | 1 | 0 | DeepSeek仅按行不存在判断，忽略时间内容。 |
| 4176：两家均无营业时间行，但签到口径只有一家None | 1 | 2 | 缺失记录改变闭店判断。 |
| 4759：歌曲及无歌曲电影，含重映票房 | 50% | 50% | 正常关联下等价，未发生比例放大。 |
| 4759：另有NULL标题的票房记录 | 50% | 33.33% | 新分母包括原连接排除的记录。 |
| 5688：正常赛季且有对手记录 | 0.5 | 0.5 | 求同一值的平均与直接读取一致。 |
| 5688：赛季存在但无对手记录 | NULL | 0.5 | 边界结果不同。 |
| 3521：球队另有不相关赛季、rank更大 | 1968 | 2000 | DeepSeek仍可能返回与总决赛无关的年份。 |
| 4442：一个Yes商家、另一个False商家，均active=True | 0 | 2 | 大小写影响匹配，新否定值过滤仍漏False。 |
| 5988：国家为Australia | Australia | NULL | 单一国家映射无法覆盖其他取值。 |

完整25组输入与执行结果见[语义与构造执行对照](../../artifacts/runs/evaluation/sqlite-train40-deepseek-flash-react-raw-only-compact-compare-qwen-20261010/semantic_comparison.json)。

## 来源审计及复现

审计本轮270份实际请求：全部符合相同的资料去重规则。860次问答出现、395次Schema出现及1458次知识出现均与原始资料一致，2713次来源记录全部来自`QA_train.json`、`Schema/bird_schemas.txt`及`knowledge.json`。这些计数包含重复历史，不代表独立记录数。没有人工修订或非空业务规则注入，认证信息没有写入实验请求JSON、配置或结果文件。

本轮只增加实验脚本能力、报告和本地运行产物，未修改生产ReAct流程或比赛提交文件。40题完整性、两轮配置一致性、首轮消息一致性、全部实际请求来源、原始DDL构造执行及报告链接校验通过。

- [本轮DeepSeek指标](../../artifacts/runs/evaluation/sqlite-train40-deepseek-flash-react-raw-only-compact-compare-qwen-20261010/metrics.json)
- [本轮40题与原训练答案对照](../../artifacts/runs/evaluation/sqlite-train40-deepseek-flash-react-raw-only-compact-compare-qwen-20261010/comparison.json)
- [Qwen、DeepSeek与原答案三方逐题对照](../../artifacts/runs/evaluation/sqlite-train40-deepseek-flash-react-raw-only-compact-compare-qwen-20261010/paired_comparison.json)
- [本轮实际请求审计](../../artifacts/runs/evaluation/sqlite-train40-deepseek-flash-react-raw-only-compact-compare-qwen-20261010/comparison_evidence_audit.json)
- [模型列表](../../artifacts/runs/evaluation/sqlite-train40-deepseek-flash-react-raw-only-compact-compare-qwen-20261010/models.json)与[运行配置](../../artifacts/runs/evaluation/sqlite-train40-deepseek-flash-react-raw-only-compact-compare-qwen-20261010/config.json)
- [Qwen原报告](QwenTrack8_SQLite40题ReAct评测.md)

完整请求、响应及轨迹仍在本地运行目录的`wire/`和`questions/`。核查实际发送的消息使用`wire/`，`questions/`保存的是去重前的原始流程请求。运行产物继续遵守仓库现有Git忽略规则。

```sh
python3 -B scripts/sample_sqlite_train_flash.py \
  --output artifacts/runs/evaluation/sqlite-train40-deepseek-qwen-new \
  --auth-header-file /path/to/private.header \
  --reference-run artifacts/runs/evaluation/sqlite-train40-qwen-track8-react-raw-only-compact-20261010 \
  --seed 20261010 --count 40 --workers 8 --max-steps 24 \
  --evidence-mode raw-only --compact-context --transport-retry-count 0

python3 -B scripts/compare_sqlite_react_samples.py \
  --reference-run artifacts/runs/evaluation/sqlite-train40-qwen-track8-react-raw-only-compact-20261010 \
  --candidate-run artifacts/runs/evaluation/sqlite-train40-deepseek-qwen-new
```

认证文件使用`Authorization: Bearer ...`格式，或事先设置`MODEL_API_KEY`并省略认证文件参数。复现使用新目录；同样配置不保证模型返回逐字相同。
