# DeepSeek Flash 与上一轮 Qwen 的 SQLite 对比

本轮使用 deepseek-flash（接口模型列表名称 DeepSeek-V4.1-Flash），关闭思考模式；完整400题。与上一轮相同的RAG数据、SYSTEM和首轮messages逐条核对，400/400一致。未用ReAct，未手工改模型SQL。

上一轮Qwen归档：`outputs/submission/archive/result-20261009-qwen-cfcc4791c075.json`；SHA256 `cfcc4791c0758c85d2ff3ce908f9617967c3673c62c3e48ae5c1f97179a89965`。

新提交：`outputs/submission/result.json`；SHA256 `0b900e961d074013fda5112064576b67be1616cbc588af4794bd31f7f451050c`。

## 对比范围与结果

| 指标 | 结果 |
|---|---|
| 上一轮SQLite题目 | 200 |
| 回答文本完全相同 | 91 |
| 回答文本不同 | 109（包含别名、空白、写法差异；不等于正确性变化） |
| SQLite原始DDL编译 | Qwen 200/200；Flash 199/199 |
| 数据库类型变化 | [5254] |
| 返回列数变化 | [3279, 3318, 3348, 3582, 3590, 4180, 4592, 5029, 5254, 5545, 5973] |
| API调用数 | 410（含校验修复重试） |

## 结论

在相同上下文、关闭思考的设置下，本轮没有证据支持Flash能提升SQLite得分。六项前次明确问题（3069、3531、4279、5265、4689、5775）仍未完整解决；可复现的新增语义退步包括年龄/国家关联、修订ID、订单均价、畅销筛选、颜色属性、地区字段及瓶装筛选。
Flash也有局部改进：5686补全题目候选年份；5855和5893改用站点ID；3512按执教年份数量排名，较首末年份差合理。上述改进不能抵消或量化为官方总分变化。
两轮仍共享RAG领域选错、必需知识遗漏或互相矛盾的问题；而4682、5411、6103说明Flash即便收到正确Schema/知识也可能绑定错字段。现有编译与少量语义规则无法发现这些错误，校验通过不能称为正确。

## 可复现反例

以下在原始DDL的内存SQLite中执行两轮原SQL；输入是人为构造的数据。仅证明注明口径下的行为，不是隐藏测试集的金标。

| 题号 | 检查要求 | Qwen符合构造预期 | Flash符合构造预期 |
|---|---|---|---|
| 3069 | 年初销售的旧书被错误日期筛选排除 | 否 | 否 |
| 3531 | 姓名要求不能由ID代替（仅检查输出含义，身份映射为构造前提） | 否 | 否 |
| 4279 | 点赞最高用户与评论最多商家内任意用户不同 | 否 | 否 |
| 4689 | -1、空字符串和NULL均不应作有效回答 | 否 | 否 |
| 5775 | ISO8601时区日期无法直接匹配空格时间文本 | 否 | 否 |
| 3902 | 游戏总体包括没有发行关系的游戏（构造前提） | 否 | 否 |
| 3674 | 所有作品包含零角色作品（构造前提） | 否 | 否 |
| 4682 | 按同一受访者关联年龄和国家答案 | 是 | 否 |
| 5099 | 区分页面ID和修订页面ID | 是 | 否 |
| 5206 | 一个订单多本书时，每订单均价与每本均价不同 | 是 | 否 |
| 5215 | 返回全部书籍不等于畅销书 | 是 | 否 |
| 5411 | white是对象属性，不是car等对象类别 | 是 | 否 |
| 6103 | Gansu是地址district而不是country | 是 | 否 |
| 5011 | 瓶数不包括罐装（两种日期均设在范围内，以隔离容器遗漏） | 是 | 否 |
| 5028 | 同一天瓶装和罐装同时出售，隔离瓶装条件遗漏 | 是 | 否 |
| 4661 | 未列入演职员表的高投票记录不能取代credited=true记录 | 是 | 否 |
| 5576 | 名人堂建设者不能作为球员返回 | 是 | 否 |
| 5686 | 题目明确候选年份，排除不在候选中的相同罚时年份 | 否 | 是 |
| 5855 | 不同站点重名时，站点ID准确定位归还地点（构造前提） | 否 | 是 |
| 5893 | 同名不同站不能计作起终站相同（构造前提） | 否 | 是 |

这20个定向反例中，Qwen符合而Flash不符合的题号为[4682, 5099, 5206, 5215, 5411, 6103, 5011, 5028, 4661, 5576]；反方向为[5686, 5855, 5893]。该样本有目的选取，不能外推准确率。

## 重点变化与保留问题

| 题号 | 审核类别 | 观察 |
|---|---|---|
| 3048 | ambiguous | 按每笔qty>20替代按图书汇总SUM(qty)>20，需金标确定销量粒度。 |
| 3069 | shared_error | Qwen仅查当月；Flash改查全年却添加未要求的出版年份筛选，并包含年内未来订单。两者都没有使用titles.ytd_sales。 |
| 3279 | projection_risk | 题目问队长姓名，Flash额外返回captain_count。 |
| 3318 | projection_risk | 题目只要求出生日期，Flash额外返回Player_Name。 |
| 3340 | shared_conditional_risk | 平均每个国家的分母只包括有右手击球球员的国家，未从国家总体表统计。 |
| 3348 | projection_risk | 题目要求姓名和国籍，Flash额外返回catches。 |
| 3422 | value_risk | 平局字面值从'Tie'改为'tie'；SQLite文本比较区分大小写，缺少真实值确认。 |
| 3464 | conditional_change | 赛季年份改为比赛日期年份，赛季跨年时可能不等价。 |
| 3512 | mixed_change | Flash按执教年份数而非首末年份差排序有所改善，但球队姓名改成逗号拼接球队ID；源域仍待金标确认。 |
| 3531 | shared_error | 两者均返回coachID，题目明确要求教练姓名。 |
| 3582 | projection_risk | 全名从一列拼接字符串改成firstName、lastName两列。 |
| 3590 | projection_risk | Flash额外返回allstar_count，并额外排除空州名。 |
| 3674 | shared_conditional_risk | Flash经自动重试恢复每作品计数再平均，但两者INNER JOIN均漏掉零角色作品；Flash另排除舞台指示。 |
| 3902 | shared_conditional_risk | 两者分母均来自发行关系表，未发行/无发行关系的游戏被排除。 |
| 4063 | ambiguous | Flash去掉ABS；题目“总数差”没有明确是否取绝对值。 |
| 4180 | ambiguous | 按商家评论级别汇总改为城市商家数量，并增加计数列；“更吸引用户”口径需确认。 |
| 4279 | shared_error | 两者按business_id分组后任选用户粉丝数；Flash把投票级别TEXT转整数，仍不是按用户点赞排名。 |
| 4326 | new_error | Flash添加未要求的opening_time=10AM，并将低签到值改为None；None在知识中表示无签到/不营业，低值编码待确认。 |
| 4491 | conditional_change | Flash添加LIMIT 1；存在多位配音演员时丢结果。 |
| 4592 | projection_risk | Flash增加title列；题目“哪些剧集”的预期列需评测确认。 |
| 4661 | new_error | Flash删除credited=true，可能返回未列入演职员表的记录。 |
| 4682 | new_error | Flash将年龄答案与United States直接比较，遗漏按UserID/SurveyID关联国家答案；知识188/189已提供。 |
| 4689 | shared_error | 两者未排除知识192定义的-1空回答，Flash还取消空字符串过滤。 |
| 4917 | ambiguous | Flash把百分比分母从所有1988年参赛者改为仅女性参赛者。 |
| 5011 | mixed_change | Flash正确使用交易日期，但删除Bottle条件，将其他容器计入瓶数。 |
| 5028 | new_error | Flash删除Bottle条件，并将连续自然日数改为有交易的不同日期数。 |
| 5029 | projection_and_tie_risk | Flash新增品牌、利润列；MAX/MIN全部并列品牌改为两个LIMIT 1，可能漏并列极值。 |
| 5099 | new_error | Flash用page过滤修订ID，原始DDL明确revision才是修订页面ID。 |
| 5206 | new_error | Flash以AVG(order_line.price)替代SUM(price)/COUNT(DISTINCT order_id)，将每订单均价变成每本书均价。 |
| 5215 | new_error | Flash返回全部非空书名且按字母排序，没有销量或畅销条件。 |
| 5254 | new_error | Flash把客户联系邮箱路由到MySQL能源公司，并返回contact_phone；Qwen返回beer_factory.customers.Email。 |
| 5400 | endpoint_risk | Flash只考虑OBJ1_SAMPLE_ID，漏掉对象作为第二端点的关系；若“所有关系”包含双端点则错误。 |
| 5411 | new_error | Flash将white当作OBJ_CLASSES对象类别，原Schema/知识251说明颜色应从ATT_CLASSES属性查询。 |
| 5430 | ambiguous | Flash把“最高图像”理解成最常见关系；Qwen使用最大IMG_ID，两者均未按H高度，需澄清原题。 |
| 5432 | endpoint_risk | Flash删除OBJ2_SAMPLE_ID对应坐标分支；应返回哪一端点/全部对象坐标需确认。 |
| 5545 | projection_risk | Flash额外返回playerID并增加排序。 |
| 5576 | new_error | Flash删除HOF.category=Player，可能混入建设者或裁判。知识284已区分类别。 |
| 5683 | conditional_change | Flash删除反向对阵分支，若TeamVsTeam缺少对称记录会遗漏对手。 |
| 5686 | better_constraint | Flash补上题目明确列出的1924、1923、1918年候选范围。 |
| 5716 | ambiguous | 曾经是球员的排除总体从players_teams改为players，身份映射/领域仍待金标。 |
| 5775 | shared_error | 两者均将ISO8601带时区日期直接与空格形式字符串比较，知识295已提供存储格式。 |
| 5855 | better_key | Flash用end_station_id关联站点，Qwen使用站名，ID更符合主外键。 |
| 5893 | better_key | Flash比较起终站ID，Qwen比较名称，ID避免同名不同站混淆。 |
| 5973 | projection_risk | Flash将姓名从一列拆成两列，并额外返回租借次数。 |
| 6022 | knowledge_conflict | Flash按原租借费率最高选择，再算每日价格；Qwen按每日价格最大值。知识344和1113存在不同口径，不据此声称官方判错。 |
| 6103 | new_error | Flash将Gansu地区错误当成country；Qwen正确过滤address.district。 |

## 评测边界

上一轮平台SQLite是137/200（68.5%），得分381.5/569.5（66.99%）。本轮尚未在平台提交；本地没有真实数据库或逐题金标，不能给出官方准确率、提升题数或最终分数。199条SQLite编译通过，只表示语法和字段绑定可执行；5254被误路由到MySQL，仍在200题对比总体中。

完整200题的两轮SQL、返回列数、源域、上下文核对、知识ID和全部反例输入输出见同目录comparison.json。

接口依据：[模型说明](https://api-docs.deepseek.com/quick_start/pricing/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)、[JSON输出](https://api-docs.deepseek.com/guides/json_mode/)。
