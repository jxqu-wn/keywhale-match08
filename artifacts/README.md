# 运行产物索引

数据审计与清洗文件见 `data/processed/`；基线见 `artifacts/baseline/`；日志见 `artifacts/logs/`。

本索引记录本地历史运行。Git仅跟踪下文列出的6份测试快照，其余 `runs/`、`baseline/` 和 `logs/` 内容被忽略；全新检出可能没有表中的完整目录。完整运行需单独备份或重新生成，忽略规则不会删除本地文件。

## 纳入版本管理的回归快照

这些文件由 `tests/test_rag_improvements.py` 读取，用于检查历史错误能够被当前流程拒绝，以及验证集未被人工修订改写。

- `runs/prediction/predict-ee19c0abd953/predictions.json`：历史400条预测。
- `runs/regression/rag-improvements-20261009/baseline.json`：验证集校验和及修订前基线信息。
- `runs/regression/test20-20261009/predict-94f84ad400f2/3674.json`、`4123.json`、`4682.json`、`5686.json`：四条历史错误的上下文、预测和检查记录。

## 本地历史运行

| 运行目录 | 用途 | 问题数 |
|---|---|---:|
| [evaluate-3e42d4434195](runs/evaluation/evaluate-3e42d4434195/) | 修复过程的中间回归 | 8 |
| [evaluate-9ba2966139c5](runs/evaluation/evaluate-9ba2966139c5/) | 最终已知错误回归 | 8 |
| [evaluate-a1a6239d66af](runs/evaluation/evaluate-a1a6239d66af/) | 200条分层验证 | 200 |
| [evaluate-b017d2ae0342](runs/evaluation/evaluate-b017d2ae0342/) | 首次跨类型模型接入验证 | 12 |
| [predict-ee19c0abd953](runs/prediction/predict-ee19c0abd953/) | 400条测试预测与ES恢复 | 400 |
| [predict-db04585845d5](runs/prediction/predict-db04585845d5/) | 修订RAG后的Qwen完整预测，现已归档 | 400 |
| [DeepSeek Flash完整预测](runs/prediction/deepseek-flash-20261009/predict-bc8c191e7fd6/) | 与Qwen首轮上下文相同的普通RAG，当前提交来源 | 400 |
| [ReAct定向实测](runs/regression/react-smoke-20261009/react-predict-ef8e9f74c6e3/) | 模型动作、工具观察及查询修复循环 | 8 |
| [teacher-rag](runs/regression/teacher-rag/) | 教师跨年首次回归，发现同年重复计数 | 2 |
| [teacher-rag-final](runs/regression/teacher-rag-final/) | 教师跨年规则修复后回归，两条构造数据执行检查通过 | 2 |
| [education-ee34fe4ec1e9](runs/regression/education-ee34fe4ec1e9/) | 全45组年度问题及4条年份边界检查，构造执行49/49通过 | 49 |
| [student-cohort-2023-trial](runs/regression/student-cohort-2023-trial/) | 2023级学生试验：本地SQL构造验证通过，模型请求待调用 | 1 |
| [conflict-corrections-20261009](runs/regression/conflict-corrections-20261009/) | 原11组冲突的明确修正SQL，13项本地构造检查通过 | 11 |
| [rag-improvements-20261009](runs/regression/rag-improvements-20261009/) | 召回前后证据、本地回归、原200题比较状态与连接探测 | 200题比较待响应 |
| [evaluate-ad9effe7095b](runs/evaluation/evaluate-ad9effe7095b/) | 使用新RAG和校验的原200条固定样本请求，接口连接失败 | 200待推理 |

每个目录保存配置、请求、响应、逐题记录和汇总。保持目录完整，便于复现。旧请求内的原路径映射见 `docs/project/path_migration.json`。

最终提交见 `outputs/submission/result.json`。

对该历史提交的审计保存在 [submission-210e08596136](analysis/submission-210e08596136/)：`analysis.json` 含逐题检查、问题与历史检索证据，`audit.csv` 为400条检查清单，`counterexamples.json` 保存16组构造数据和原SQL输出。对应说明见 [上一轮提交结果分析](../docs/reports/上一轮提交结果分析.md)。这些检查不构成真实数据库执行准确率评测。
