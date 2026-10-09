# 多源查询指令生成

面向 MySQL、PostgreSQL、Elasticsearch 和 SQLite 的检索增强查询生成项目。使用 Python 标准库，不需要安装额外依赖。默认模型服务为 `http://192.168.15.119:28081/v1`，默认模型 ID 为 `contest-qwen`；普通生成与ReAct入口均支持配置其他供应方。

文档入口：[文档索引](docs/README.md)、[项目结构与调用链](docs/project/structure.md)、[知识检索与上下文注入](docs/project/knowledge_context.md)、[开发与版本管理](docs/project/development.md)。

普通 RAG 会将全部知识加载到本地检索器，针对当前问题按领域、数据源、必需公式和关键词选取通常最多10条知识，再放入首轮用户消息的 `knowledge` 数组。ReAct 初始消息不传入该数组，模型调用 `search_knowledge` 后通过 `observation` 获得筛选结果。两种流程共用知识选择逻辑，详见[完整流程及示例](docs/project/knowledge_context.md)。

## 本地开始

需要 Python 3；本地测试不需要模型服务。当前仓库包含原始输入、处理数据和测试必需快照，检出后可直接运行：

```sh
python3 scripts/run.py --help
PYTHONPATH=src python3 -B -m unittest discover -s tests -v
```

修改原始数据后，用 `python3 scripts/run.py prepare` 重建处理数据。在线生成需要可访问的模型服务；`curl` 用于批量传输，`7z` 仅用于重新解压补充数据。

## 目录

| 目录 | 内容 |
|---|---|
| `src/text2query/non_react/` | 普通RAG生成与检索复制基线 |
| `src/text2query/react/` | ReAct生成与动作工具 |
| `src/text2query/shared/` | 共通数据、上下文、模型接口、校验、评测与导出工具 |
| `scripts/run.py` | 统一命令入口 |
| `tests/` | 自动检查 |
| `data/raw/` | 原始训练/测试/知识/Schema、原始压缩包和公开赛题元数据 |
| `data/reference/` | 解压后的区划数据与补充说明 |
| `data/processed/` | 清洗数据、训练/验证划分、元数据目录和审计结果 |
| `data/curated/` | 用户补充的业务口径，独立于官方训练标注 |
| `docs/competition/` | 赛题、官方评测标准和提交要求 |
| `docs/reports/` | 验证说明、修复报告及用户保存的平台得分结果 |
| `docs/project/` | 项目结构说明与旧路径迁移清单 |
| `artifacts/baseline/` | 检索基线预测、上下文和指标 |
| `artifacts/runs/` | 按验证/预测分类的完整模型运行记录 |
| `artifacts/logs/`、`artifacts/service/` | 运行日志和模型服务元数据 |
| `outputs/submission/` | 最终提交文件与检查报告 |

Git 跟踪代码、文档、输入及处理数据、已交付结果和6份回归快照；其余模型运行、基线上下文和日志由 `.gitignore` 排除，继续保留在本地。历史报告中的完整运行目录需要另行保存或重新生成，详见[版本管理边界](docs/project/development.md#版本管理边界)。

已生成的 [400 条提交结果](outputs/submission/result.json)、[平台得分记录](docs/reports/得分结果.md)、[错误修复报告](docs/reports/fixes_report.md) 和 [运行记录索引](artifacts/README.md) 可直接查看。当前提交来自DeepSeek Flash ReAct流程，生成来源与验证范围见[提交说明](outputs/submission/README.md)和[与初轮Qwen的对比报告](docs/reports/DeepSeek_Flash_ReAct与初轮Qwen对比.md)；此前Qwen及普通DeepSeek结果保存在 `outputs/submission/archive/`。

[上一轮提交结果分析](docs/reports/上一轮提交结果分析.md)记录5条结构问题、语义与知识口径问题及条件性风险，并保存400条检查清单和16组构造反例。运行 `python scripts/analyze_last_submission.py` 可复现该历史提交的审计；编译通过率和构造反例均不等于比赛执行准确率。

[RAG与校验改进报告](docs/reports/RAG与校验改进报告.md)记录此前的训练示例、知识、领域与来源约束、提示词和SQLite/ES检查。修订配置包含21条训练示例修正和7条知识修正，Qwen轮次已重新生成完整400条测试结果。报告所述原200题配对评测状态属于该轮记录。`python scripts/run_model_comparison.py` 可自动用curl批量调用并处理修复轮次。

RAG 示例通过 `schema_refs` 关联库表，完整字段与约束放在上下文的 `schema` 中。冲突标注保留在 `data/processed/ambiguous_examples.json`，作为带歧义提示的候选召回，不进入唯一答案示例集。[跨年度教师处理说明](docs/reports/teacher_rag.md) 记录了用户补充的合并口径及验证方式。

[冲突数据人工审核报告](docs/reports/conflicts/冲突数据报告.md) 覆盖55组、113条标注；[离线审核页面](docs/reports/conflicts/冲突人工审核.html) 支持筛选、填写和导出，也可使用同目录的CSV模板。生成命令为 `python scripts/build_conflict_report.py`，不会自动应用填写的业务决定。

教育年度差异已按业务身份合并、2025记录优先处理，见[统一处理说明](docs/reports/education_year_merge.md)。原11组剩余冲突已按用户授权修正，见[修正结论与SQL](docs/reports/conflicts/已修正冲突报告.md)；原55组报告保留为处理前快照。模型使用合并逻辑视图，程序自动展开为完整MySQL只读CTE查询。

针对1674/1724的[2023级学生修正](docs/reports/student_cohort_trial.md)已按用户要求取消学号LIKE，改为关联所属班级筛选grade_year=2023。11组完整题目精确匹配时直接使用经构造检查的修正SQL，运行记录明确标注人工规则来源；其他题目通过RAG召回修正示例。类型编码和行政区划字段处理见[数据一致性说明](docs/reports/data_consistency.md)。

## 常用命令

在项目根目录运行：

```sh
python scripts/run.py prepare
python scripts/run.py baseline evaluate
python scripts/run.py model evaluate --sample 200 --seed 42 --workers 2
python scripts/run.py model predict --workers 2
python -m unittest discover -s tests -v
```

模型生成入口会打印实际运行目录。数据、代码和问题集合变化会产生新的运行指纹；后续操作使用该目录，不要照搬历史运行的指纹。

普通 `model` 和 `react` 入口支持 `--base-url`、`--model`、`--provider vllm|deepseek`；普通入口另有离线模式的 `--models-file`。两种流程共用供应方请求构建，DeepSeek关闭思考模式。在线认证通过环境变量 `MODEL_API_KEY` 读取；curl批量调用可指定私有认证头文件。参数及离线流程见[开发说明](docs/project/development.md#模型服务与离线运行)。

## 使用 curl 批量调用

当 Python 网络受限时，先保存服务模型列表，再准备请求：

```sh
curl -sS http://192.168.15.119:28081/v1/models -o artifacts/service/models.json
python scripts/run.py model predict --offline
```

将以下 `RUN_DIR` 设置为入口实际打印的目录，再调用并解析响应：

```sh
RUN_DIR='artifacts/runs/prediction/predict-实际指纹'
python scripts/run.py curl-batch "$RUN_DIR"
curl --parallel --parallel-max 4 --silent --show-error --config "$RUN_DIR/batch.curl.conf"
python scripts/run.py model predict --offline
```

若仍有修复请求，重新生成配置并重复调用、解析，最多四轮。ES遗留字符串格式的恢复入口为 `python scripts/run.py recover-es "$RUN_DIR"`。

## ReAct 查询生成

`react` 是独立生成入口。模型每轮选择工具并读取 observation，可动态检索 Schema、知识和修正示例，检查候选后自行修改，再通过 `finish` 提交。工具只读；目前无比赛后台数据库连接，SQLite 使用原始 DDL 编译，PG 做目录表名及显式物理表别名字段检查，ES 做结构和 mapping 检查。

```sh
python scripts/run.py react predict --workers 4 --max-steps 10
python scripts/run.py react evaluate --sample 200 --seed 42 --workers 4
```

网络受限时先准备请求，使用打印的 ReAct 目录，通过现有 curl 批量入口调用，再重复**同一个** ReAct 命令消费响应并生成下一步请求：

```sh
python scripts/run.py react predict --offline --ids 2399,2646,2990,4682,5029,5236,5686 --workers 4
RUN_DIR='artifacts/runs/prediction/react-predict-实际指纹'
python scripts/run.py curl-batch "$RUN_DIR"
curl --noproxy '*' --parallel --parallel-max 4 --silent --show-error --config "$RUN_DIR/batch.curl.conf"
python scripts/run.py react predict --offline --ids 2399,2646,2990,4682,5029,5236,5686 --workers 4
```

循环直到 `pending_count=0`；检查 `unresolved_count`。每题最多10个模型动作，可用 `--max-steps` 调整到24以内。程序保留逐步动作、工具观察、请求和响应，支持中断续跑；成功结果单独保存，未完成或检查失败的查询不会进入 predictions.json。完整400题全部完成后才可用 `export` 导出提交。

`check_query` 自动规范化 ES 错放的根级段，冲突值直接拒绝；教育逻辑视图自动展开为完整 SQL。模型必须先查看所用物理表及知识，并通过检查后才能结束。最新已交付的400条 `result.json` 来自DeepSeek Flash ReAct，共消费1870次模型响应；两题经自动闭合外层JSON后由模型完成提交，没有手工修改查询。与最初Qwen相比有结构和统计口径改善，也有领域与字段含义退化，详见[完整对比](docs/reports/DeepSeek_Flash_ReAct与初轮Qwen对比.md)；官方分数尚未取得。

此前定向实测覆盖8题（教育合并、ES医院、PG最新位置、船长跨源、心理调查、根汁啤酒、作者出版及冰球），43次模型调用全部完成，无手工查询编辑；当时54项自动测试通过。8题的本地检查通过率为100%，不是官方执行准确率。逐步记录位于 `artifacts/runs/regression/react-smoke-20261009/react-predict-ef8e9f74c6e3/`。

## 评测和提交

```sh
python scripts/run.py score artifacts/runs/evaluation/evaluate-a1a6239d66af
python scripts/run.py verify-fixes artifacts/runs/evaluation/evaluate-9ba2966139c5
python scripts/run.py export artifacts/runs/prediction/predict-db04585845d5 --output /tmp/match-result-check/result.json
```

默认导出位置为 `outputs/submission/result.json`。导出要求完整覆盖测试问题、无重复 ID，并通过当前基础检查。

[官方指标](docs/competition/evaluation_standard.md) 是加权执行准确率，本地字符串匹配率不等于官方分数。当前检索使用关键词评分、领域约束和训练示例，没有 embedding；校验包含SQLite原始DDL编译和ES定向结构/字段检查，仍不能保证所有SQL方言及业务语义正确。定向回归包含构造的SQLite执行检查。

原始数据、历史请求及响应保留。历史记录内出现的旧路径可通过 [迁移清单](docs/project/path_migration.json) 对照；当前代码和可执行 curl 配置已使用新路径。详细结构见 [项目组织说明](docs/project/structure.md)。
