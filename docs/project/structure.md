# 项目组织说明

代码、输入数据、生成过程和最终输出分别存放。路径由 `src/text2query/paths.py` 统一定义，入口为 `scripts/run.py`，从其他目录执行该入口也能找到项目数据。

## 模块职责

| 模块 | 职责 |
|---|---|
| `pipeline.py` | 读取数据、解析Schema、审计/清洗/划分与关键词检索基线 |
| `model_pipeline.py` | 检索上下文、模型请求、响应处理、校验及重试 |
| `retrieval_context.py` | 问题领域路由、知识适用范围、必需知识补齐、字典匹配与坐标换算 |
| `curation.py` | 应用训练与知识人工修订、补充知识范围及营业时间解析CTE |
| `react_pipeline.py` | 模型动作与工具观察循环、逐步请求记录、续跑和结果汇总 |
| `react_tools.py` | Schema、知识、示例检索，查询检查与最终提交工具 |
| `dsl_normalization.py` | ES根级段规范化与冲突检查 |
| `query_rules.py` | 从当前问题提取要求及保守语义检查 |
| `education.py` | 教育年度范围、业务身份合并视图、SQL展开和关联/返回字段检查 |
| `education_fixture.py` | 构造教育数据、独立Python合并结果和SQLite执行环境 |
| `curl_batch.py` | 为待完成请求生成curl批量配置 |
| `recover_es.py` | 用模型原生对象恢复ES查询 |
| `score_sample.py` | 本地诊断指标与显式执行结果的官方加权汇总 |
| `verify_fixes.py` | 已知问题的定向回归和SQLite构造数据执行检查 |
| `export_submission.py` | 完整性检查、提交文件和校验报告导出 |
| `paths.py`、`cli.py` | 统一路径与命令分发 |

## 入口与调用链

`scripts/run.py` 将 `src/` 加入导入路径，再由 `cli.py` 分发命令。各入口的主要调用关系如下：

| 命令 | 主要调用链 | 输出 |
|---|---|---|
| `prepare` | `pipeline.prepare` → Schema解析、数据划分、人工修订 | `data/processed/` |
| `baseline` | `pipeline.baseline` → `Retriever.search` → 复制最近训练示例查询 | `artifacts/baseline/` |
| `model` | `Context.build` → `select_knowledge` → 组装消息 → 模型调用 → 展开与校验 | `artifacts/runs/evaluation/` 或 `prediction/` |
| `react` | `Context.build` → `initial_prompt` → 模型动作 → `QueryTools.dispatch` → observation → `finish` | 独立的 `react-*` 运行目录 |
| `export` | 覆盖率、格式和查询检查 → 提交文件 | `outputs/submission/` 或指定目录 |

普通生成最多4次请求，后续修复在同一对话中追加错误反馈；ReAct 每轮执行一个工具动作，步数由 `--max-steps` 控制。教育逻辑视图展开和ES格式处理属于程序转换，运行记录保留转换信息。

验证模式的示例来自 `data/processed/train.json`，预测模式来自 `clean.json`；知识和Schema目录由两种模式共用。完整题目命中人工修正时，普通 `model` 入口在通过校验后直接使用修正查询，并标注 `prediction_source`。

知识如何进入消息及相关性筛选见[知识检索与上下文注入](knowledge_context.md)；本地命令、测试和Git跟踪范围见[开发与版本管理](development.md)。

`scripts/verify_teacher_rag.py` 用于跨年度教师规则的真实模型定向验证。用户补充的规则在 `data/curated/`；冲突候选在 `data/processed/ambiguous_examples.json`，单独进入检索上下文，不恢复为唯一答案标注。

`scripts/build_conflict_report.py` 从原始标注、审计、Schema和专家知识生成 `docs/reports/conflicts/` 下的报告、离线审核页面和CSV模板；页面模板位于 `scripts/templates/`。人工填写后通过导出文件保存，不自动修改生成管线。

`scripts/resolve_education_conflicts.py` 验证全部年度问题，并生成年度处理状态及剩余审核报告。处理状态存于`data/curated/education_conflict_resolutions.json`；`prepare`将其附加到RAG候选元数据，原始标注不改写。

## 迁移对照

| 原路径 | 当前路径 |
|---|---|
| `scripts/*.py` | `src/text2query/*.py`；用 `scripts/run.py` 调用 |
| `subject/` | `data/raw/subject/` |
| 根目录原始压缩包 | `data/raw/archives/` |
| `topic.md` | `docs/competition/topic.md` |
| `artifacts/reference/` | `data/reference/` |
| `artifacts/prepared/` 数据文件 | `data/processed/` |
| `artifacts/prepared/` 基线预测与指标 | `artifacts/baseline/` |
| `artifacts/model/evaluate-*` | `artifacts/runs/evaluation/evaluate-*` |
| `artifacts/model/predict-*` | `artifacts/runs/prediction/predict-*` |
| `artifacts/model/*.log` | `artifacts/logs/` |
| `artifacts/model/models.json` | `artifacts/service/models.json` |
| `submission/` | `outputs/submission/` |
| `results/得分结果.md` | `docs/reports/得分结果.md` |

`path_migration.json` 保存逐文件对照及迁移前校验和。原始数据、请求和响应内容未改写；历史配置中的实现校验和仍指向当时的代码版本。旧请求上下文内的路径是当时的记录，通过迁移对照查找当前文件。可执行curl配置、当前处理数据的文件引用与提交检查报告已更新路径。

运行指纹随新代码组织变化，新运行会创建新的目录；历史运行不会被当作新运行缓存。路径迁移时未改写当时的提交文件；后续模型生成的版本由提交检查报告和归档记录区分。
