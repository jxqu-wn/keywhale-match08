# 开发与版本管理

## 环境与入口

需要Python 3，代码依赖Python标准库。Git用于版本管理；curl用于离线请求的网络传输；7z仅用于重新解压原始补充数据。当前原始输入、处理数据和回归快照均纳入版本管理，本地测试无需模型服务。

统一入口是 `scripts/run.py`，从其他目录以完整路径执行也能定位项目数据：

```sh
python3 scripts/run.py --help
python3 scripts/run.py model --help
python3 scripts/run.py react --help
```

主要模块职责见[项目结构](structure.md)，原始输入和人工配置见[数据目录](../../data/README.md)。

## 数据重建

```sh
python3 scripts/run.py prepare
```

该命令重写 `data/processed/` 的Schema目录、知识、字典、清洗/划分数据和审计文件。人工修订配置来自 `data/curated/`；原始训练与知识输入保留。修改处理逻辑或输入后，应检查生成差异，并将相关配置与处理数据一起提交。

验证集先从原始标注分组划分，人工示例修订应用于训练及清洗集合。新增修订应使用原始训练输入和明确业务知识，保持验证目标稳定。当前回归快照保存验证集校验和，重建后可运行测试确认未发生非预期变化。

## 测试

在项目根目录运行：

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests -v
```

按文件运行定向检查：

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_rag_improvements.py' -v
PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_react.py' -v
```

测试覆盖数据划分、Schema解析、知识召回、人工修订、教育合并、SQLite构造查询、ES结构、离线请求缓存、ReAct工具与模型供应方请求构建。模拟模型及构造数据检查不产生真实模型调用。测试必需的6份历史快照已由 `.gitignore` 显式保留；添加依赖新快照的测试时，应同步维护跟踪规则和[产物索引](../../artifacts/README.md)。

## 模型服务与离线运行

普通 `model` 入口的主要参数如下；具体默认值以 `--help` 和源码为准。

| 参数 | 用途 |
|---|---|
| `--base-url` | 模型接口根路径，默认 `http://192.168.15.119:28081/v1` |
| `--model` | 模型列表中的ID，默认 `contest-qwen` |
| `--provider vllm\|deepseek` | 供应方请求构建方式，默认 `vllm` |
| `--models-file` | 离线模式使用的模型列表JSON，默认 `artifacts/service/models.json` |
| `--ids`、`--sample`、`--sample-file` | 指定问题或验证样本；固定样本文件仅用于 `evaluate` |
| `--workers` | 并发问题数 |
| `--output` | 运行父目录；入口在其中创建指纹子目录 |
| `--offline` | 写入请求并消费已保存的响应，不直接访问接口 |

`api()` 从环境变量 `MODEL_API_KEY` 读取可选认证信息；凭据文件及本地环境配置由Git忽略。普通模式和ReAct共用 `completion_payload()` 构造供应方参数，都支持 `--provider vllm|deepseek`；DeepSeek请求关闭思考模式。

完整原始训练集的SQLite问题可使用DeepSeek Flash ReAct批处理：

```sh
# 预先设置 MODEL_API_KEY；凭据不要写入代码或结果文件。
python scripts/run_sqlite_train_react.py --workers 24 --max-steps 16
```

范围为 `QA_train.json` 中 `db/db1/db2` 包含SQLite的全部原始记录，保留原始问题ID。检索上下文默认为 `raw-only`，包含原始训练示例，因此该运行不是独立验证集评测。当前默认结果目录为 `artifacts/runs/sqlite-train-react-v6-deepseek-flash/`，其中 `answers.json` 保存题目及生成结果，`predictions.json` 保存成功提交的查询，`failures.json` 保存未完成的问题，`metrics.json` 保存覆盖率和检查统计，`questions/` 保存逐题请求、响应及工具轨迹。每25题更新汇总；已有失败记录保持原样，不自动重试，网络失败也不自动重试。使用 `--sample-file` 可运行指定问题集合。成功指流程检查通过，不代表真实数据库执行结果。

离线调用普通RAG时，先使用已保存的服务模型列表生成请求，再通过curl传输：

```sh
python3 scripts/run.py model predict --offline --ids 442
RUN_DIR='artifacts/runs/prediction/predict-入口打印的实际指纹'
python3 scripts/run.py curl-batch "$RUN_DIR"
curl --parallel --parallel-max 4 --silent --show-error --config "$RUN_DIR/batch.curl.conf"
python3 scripts/run.py model predict --offline --ids 442
```

`RUN_DIR` 是需要替换的目录占位符。非默认服务应在模型入口和 `curl-batch --base-url` 中使用一致的URL；离线模型列表应包含实际选用的模型ID。curl认证可通过 `curl-batch --auth-header-file` 引用私有请求头文件，该配置只保存文件路径。普通流程若生成新的修复请求，需要再次生成批量配置、传输并执行同一模型命令。

ReAct v2固定依赖顺序为 `search_examples → get_schema → search_knowledge → check_query → finish`。首轮 `exact_examples` 通过归一化题目索引提供全部同题记录，精确匹配优先于模糊排序、不受Top-K限制；无同题时由示例工具检索相关题目。重复、冲突和被人工排除的原始标注仍保留，附带状态和来源；不能把原始标注当作已审定答案。任何证据检索都会使成功检查失效；新增物理表或重新查示例后须重新查知识。知识适用库由已核对Schema决定，跨源查询保留所有相关源。

ReAct直接读取 `QA_train.json`、`knowledge.json` 和原始Schema/字典文件。已有人工修订作为附加证据，保留 `raw_annotation`、来源文件、行号或记录ID及校验和。`get_schema` 的 `raw_schema` 保留中文表注释、表前说明、表选项、列注释及后续SQL；用于召回，也直接返回给模型。`include_source_documents=true` 可查看完整Schema文件和参考说明，`search_knowledge` 的 `include_dictionary_text=true` 可查看匹配字典完整INSERT。验证模式仍按既有dev划分排除对应参数模板，避免把验证答案重新加入检索。

离线审计与覆盖率检查：

```sh
python scripts/audit_react_evidence.py
```

默认写入 `artifacts/analysis/react-evidence-v2/`，含原始/处理后抽样与全量字段比较、中文表注释召回排名、训练及测试问题的实际首轮案例注入覆盖率。覆盖率分开报告“同题案例是否存在”和“有案例时是否完整注入”，不把相似问题计为同题。当前训练5830题全部注入；测试400题中只有10题有归一化同题案例，这10题全部注入。

ReAct也支持离线生成。每轮传输后重复相同的 `react` 命令消费响应，直到 `pending_count=0`，再检查 `unresolved_count`。ReAct默认最多10个动作，可通过 `--max-steps` 调整，当前上限24、下限5。

当前ReAct v6按同题案例的实际库表引用确定候选领域，有同题时默认不混入其他问题，可用 `include_related=true` 请求相关案例；知识检索过滤明确属于未查询表的内容。ReAct不再继承普通RAG的全部领域先验。独立LLM语义复核已暂时默认关闭，`check_query` 保留本地基础检查，检查通过后可提交。`react`、`sample_sqlite_train_flash.py`、`sample_sqlite_train_react.py` 和 `run_sqlite_train_react.py` 均支持 `--semantic-review` 恢复复核；开启时按题意、Schema、知识及字典复核，记录 `accept/revise/uncertain` 与完整请求响应。配置保存复核开关，逐题及汇总记录复核API调用数；变更复核设置应使用新的输出目录。相同动作和错误连续出现两次、没有进展时，保存 `repeated_error_without_progress` 并结束该题。诊断证据见[44题逐项报告](../../artifacts/analysis/react-v2-deviations-44/README.md)。

运行目录由问题集合、模型、数据和实现等配置的指纹确定。使用入口打印的路径；缓存响应与原请求不一致时会归档为过期响应，不能将历史目录名称直接当作新运行路径。

## 版本管理边界

仓库初始分支为 `main`。查看和提交修改：

```sh
git status --short
git diff
git add README.md docs/ data/curated/
git commit -m "docs: explain knowledge retrieval and project workflow"
```

按实际修改范围选择暂存文件。Git跟踪范围如下：

| 内容 | 管理方式 |
|---|---|
| `src/`、`scripts/`、`tests/`、项目文档 | 跟踪 |
| `data/raw/`、`reference/`、`curated/`、`processed/` | 跟踪，保存输入、业务决定与当前可用处理数据 |
| `outputs/submission/` | 跟踪已交付结果、检查报告和小规模历史备份 |
| `artifacts/analysis/`、`artifacts/service/` | 跟踪已保存的分析结果和模型发现元数据 |
| `artifacts/runs/` | 仅跟踪6份现有测试必需快照；其余留在本地 |
| `artifacts/baseline/`、`artifacts/logs/` | 忽略生成结果和日志 |
| Python缓存、虚拟环境、临时文件、凭据文件 | 忽略 |

忽略规则不会删除现有运行数据。完整模型请求与响应应另外保存，保持同一指纹目录完整；全新检出只保证代码、数据和本地测试可用，重现历史模型运行还需要对应的本地产物或重新调用服务。

远程仓库地址由使用者提供后再配置。Git初始化和本地提交不需要远程仓库。

## 评测与导出

`score` 汇总本地诊断；显式提供真实执行结果时才能计算相应执行指标。`export` 要求测试问题完整覆盖、无重复ID并通过基础查询检查，默认写入 `outputs/submission/result.json`，也可用 `--output` 指定路径。

格式通过、SQLite编译、构造数据验证、模型成功生成和官方后台执行分数是不同的验证结果。已交付结果及验证范围见[提交说明](../../outputs/submission/README.md)，比赛指标见[官方评测标准](../competition/evaluation_standard.md)。
