# SQLite 训练集 ReAct 结果与标准答案的语义对比

对 `artifacts/runs/sqlite-train-react-deepseek-flash` 的 DeepSeek Flash ReAct 输出与 `data/raw/subject/QA_train.json` 中同题标准查询逐题比对，筛选语义存在偏差的样本。

## 范围与方法

- 范围：`QA_train.json` 中 `db/db1/db2` 含 SQLite 的 2,991 题，与运行目录的 `answers.json` 按 `question_id` 对齐。
- 确定性层：去注释、统一大小写与空白、解析并归一化表别名、统一等价写法；保留字符串字面量内容，字面值不同不算等价。同时记录涉及库表、返回列数（原始 DDL 内存编译）及检索上下文是否含本题标准查询示例。
- 语义层：对不能自动判定等价的 2,397 对，用 `deepseek-flash` 逐题判断是否语义等价、偏差类别及说明。判定缓存于运行目录 `semantic_audit/judge_cache.json`，可续跑。

复现：

```sh
python scripts/audit_sqlite_train_react_semantics.py            # 仅确定性统计
MODEL_API_KEY=... python scripts/audit_sqlite_train_react_semantics.py --judge
```

产物在 `artifacts/runs/sqlite-train-react-deepseek-flash/semantic_audit/`：

- `semantic_audit.json`：全部 2,991 题的特征、判定与偏差清单。
- `semantic_audit.md`：汇总与全部偏差样本表。

## 结果

| 项目 | 数量 |
|---|---|
| 对齐题目 | 2,991 |
| 有预测 / 无预测 | 2,990 / 1（3404，`step_budget_exhausted`） |
| 完全一致（含空白、大小写） | 529 |
| 别名与写法规范化后一致 | 593 |
| 送模型判定 | 2,397 |
| 语义存在偏差 | **1,107** |
| 判定等价 | 1,883 |
| 检索上下文含本题标准查询示例 | 2,960 |
| 其中仍出现语义偏差 | 1,087 |

偏差按类别：

| 类别 | 数量 | 含义 |
|---|---|---|
| filter | 312 | 筛选条件多、少或边界/字段不同 |
| aggregation | 293 | 聚合函数或统计口径不同 |
| projection | 245 | 返回列的数量或含义不同 |
| join | 114 | 关联表、关联键或去重不同 |
| table | 52 | 使用不同库表或数据源 |
| value | 51 | 字面值、枚举或百分比分母不同 |
| grouping | 16 | 分组粒度不同 |
| ordering | 11 | 排序字段或方向不同 |
| limit | 10 | LIMIT 有无或数量不同 |
| other | 3 | 其他口径差异 |

## 典型样例

| 题号 | 类别 | 观察 |
|---|---|---|
| 3040 | table | 标准用 `book_publishing_company.sales` 按 `SUM(qty)`，预测改用 `books.cust_order` 的 `COUNT(*)`。 |
| 3041 | aggregation | 标准按单条 `qty` 排序，预测按 `SUM(qty)` 分组排序。 |
| 3044 | filter | 版税范围标准为 `>10000`、`<50000`，预测为 `>=`、`<=`，边界不同。 |
| 3090 | join | 预测未关联 `jobs` 表，直接用 `employee.job_lvl`。 |
| 3099 | value | 百分比分母标准用 Fremont，预测用 Portland。 |
| 3335 | ordering | 标准按出生日期降序，预测升序。 |
| 3877 | value | 游戏名字面值少了开头的 `2 `，筛选值不同。 |
| 6204 | limit | 题目要求“至少3个”，标准无 LIMIT，预测加 `LIMIT 3`。 |

## 结论与边界

- 本次为训练集生成，检索上下文在 2,960 题中包含本题的标准查询示例；仍有 1,087 题出现语义偏差，说明模型会脱离上下文中的标准示例而改写逻辑。
- 偏差集中在筛选条件、聚合口径和返回列三类，与结构检查通过与否无关：`check_query` 只验证语法、库表与字段绑定，不能发现业务语义错误。
- 判定以训练集标准查询为基准，标准查询本身未在真实数据库执行复核；差异不等于平台判错，也不等于比赛准确率。
