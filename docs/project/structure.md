# 项目组织说明

代码、输入数据、生成过程和最终输出分别存放。路径由 `src/text2query/shared/paths.py` 统一定义，入口为 `scripts/run.py`，从其他目录执行该入口也能找到项目数据。

## 模块职责

```text
src/text2query/
├── non_react/
│   ├── pipeline.py       普通RAG生成、校验反馈和最多四轮重试
│   └── baseline.py       最近训练示例复制基线
├── react/
│   ├── pipeline.py       ReAct模型动作与工具观察循环
│   └── tools.py          Schema、知识、示例、查询检查与完成工具
├── shared/
│   ├── context.py        两种现有模式共用的RAG上下文
│   ├── prompts.py        现有指令与修复反馈
│   ├── generation.py     模型接口、供应方参数、解析、校验与抽样
│   ├── data.py           数据读取、Schema解析、清洗、划分与关键词检索
│   ├── retrieval_context.py  领域、知识和字典选择
│   ├── paths.py          项目数据路径
│   ├── fingerprints.py   所有子目录代码的缓存指纹
│   └── ...               教育合并、SQL/DSL检查、curl、评测与导出
├── cli.py                统一命令分发
└── __main__.py           python -m text2query入口
```

两种模式均直接依赖shared；ReAct不再通过普通生成模块获取工具。原始文件注入的新版本按用户要求暂停，目前没有该版本的入口或代码目录。

| 目录/模块 | 职责 |
|---|---|
| `non_react/pipeline.py` | 检索后直接生成查询，错误反馈后重试 |
| `non_react/baseline.py` | 复制最近训练示例作为检索基线 |
| `react/pipeline.py`、`react/tools.py` | 模型选择工具、读取观察、检查及结束 |
| `shared/context.py`、`shared/prompts.py` | 共用上下文与当前提示词 |
| `shared/generation.py` | 请求、解析、共同校验及分层抽样 |
| `shared/data.py` | 读取、Schema解析、清洗、划分及关键词检索 |
| `shared/retrieval_context.py`、`shared/curation.py` | 领域知识、字典与人工修订 |
| `shared/query_rules.py`、`shared/validation.py` | 题意规则、原始DDL编译及ES检查 |
| `shared/education.py`、`shared/education_fixture.py` | 跨年度视图、展开和构造验证 |
| `shared/dsl_normalization.py` | ES根级段规范化 |
| `shared/curl_batch.py`、`shared/recover_es.py` | 离线批量请求与格式恢复 |
| `shared/score_sample.py`、`shared/verify_fixes.py` | 本地评测与定向回归 |
| `shared/export_submission.py` | 完整覆盖检查与提交导出 |

## 入口与调用链

`scripts/run.py` 将 `src/` 加入导入路径，再由 `cli.py` 分发命令。各入口的主要调用关系如下：

| 命令 | 主要调用链 | 输出 |
|---|---|---|
| `prepare` | `shared.data.prepare` → Schema解析、数据划分、人工修订 | `data/processed/` |
| `baseline` | `non_react.baseline.baseline` → `Retriever.search` → 复制最近训练示例查询 | `artifacts/baseline/` |
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
| 扁平的 `src/text2query/*.py` | `non_react/`、`react/`、`shared/`；仍用 `scripts/run.py` 调用 |
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

本次代码目录调整不改写输入数据、提示词、查询行为或历史产物。56项自动检查通过；7道跨领域问题的上下文及SYSTEM在迁移前后哈希一致。命令名称不变，Python内部导入改为上述新位置。
