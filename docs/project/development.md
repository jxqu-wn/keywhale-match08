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

`api()` 从环境变量 `MODEL_API_KEY` 读取可选认证信息；凭据文件及本地环境配置由Git忽略。普通模式通过 `completion_payload()` 构造供应方参数，ReAct当前单独构造请求，没有 `--provider` 参数。

离线调用普通RAG时，先使用已保存的服务模型列表生成请求，再通过curl传输：

```sh
python3 scripts/run.py model predict --offline --ids 442
RUN_DIR='artifacts/runs/prediction/predict-入口打印的实际指纹'
python3 scripts/run.py curl-batch "$RUN_DIR"
curl --parallel --parallel-max 4 --silent --show-error --config "$RUN_DIR/batch.curl.conf"
python3 scripts/run.py model predict --offline --ids 442
```

`RUN_DIR` 是需要替换的目录占位符。非默认服务应在模型入口和 `curl-batch --base-url` 中使用一致的URL；离线模型列表应包含实际选用的模型ID。curl认证可通过 `curl-batch --auth-header-file` 引用私有请求头文件，该配置只保存文件路径。普通流程若生成新的修复请求，需要再次生成批量配置、传输并执行同一模型命令。

ReAct也支持离线生成。每轮传输后重复相同的 `react` 命令消费响应，直到 `pending_count=0`，再检查 `unresolved_count`。ReAct默认最多10个动作，可通过 `--max-steps` 调整，当前上限24。

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
