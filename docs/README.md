# 文档索引

项目使用Python标准库完成数据处理、检索、模型请求和本地校验。统一入口与常用命令见[项目README](../README.md)。

## 理解与开发

| 文档 | 内容 |
|---|---|
| [项目结构与调用链](project/structure.md) | 模块职责、命令入口、普通RAG和ReAct流程、历史路径迁移 |
| [知识检索与上下文注入](project/knowledge_context.md) | `knowledge.json` 的处理、领域筛选、必需知识、消息注入与排查示例 |
| [开发与版本管理](project/development.md) | 环境、测试、数据重建、模型参数、离线续跑及Git跟踪范围 |
| [数据目录](../data/README.md) | 原始与处理数据、人工修订配置和当前数据规模 |
| [运行产物索引](../artifacts/README.md) | 测试快照及本地历史模型运行 |
| [提交结果说明](../outputs/submission/README.md) | 已交付结果、生成来源和验证范围 |

## 赛题与评测

- [赛题说明](competition/topic.md)
- [官方评测标准](competition/evaluation_standard.md)
- [提交要求](competition/submission_requirements.md)

## 业务口径与验证记录

- [DeepSeek Flash与Qwen相同40题ReAct对比](reports/DeepSeekFlash与Qwen_SQLite40题ReAct对比.md)：相同首轮输入的新一轮DeepSeek作答、三方答案对照、修改来源及构造执行。
- [Qwen Track8 SQLite训练集40题ReAct评测](reports/QwenTrack8_SQLite40题ReAct评测.md)：指定本地模型的不一致率、上下文适配、逐题原因及原始资料来源审计。
- [SQLite训练集40题原始资料模式对比](reports/SQLite40题原始资料模式对比.md)：停用人工修订后的相同40题重测、不一致率、逐题原因及实际请求来源审计。
- [RAG与校验改进](reports/RAG与校验改进报告.md)：训练示例、知识与领域约束的修订依据。
- [DeepSeek Flash ReAct与初轮Qwen对比](reports/DeepSeek_Flash_ReAct与初轮Qwen对比.md)：当前400条提交、构造执行对比、改善与退化及评测范围。
- [DeepSeek Flash与Qwen的SQLite对比](reports/DeepSeek_Flash_SQLite对比.md)：普通RAG轮次在相同首轮上下文下的生成差异、构造反例及该轮提交来源。
- [教育年度合并](reports/education_year_merge.md)、[教师跨年处理](reports/teacher_rag.md)：跨年度业务身份与逻辑视图。
- [数据一致性](reports/data_consistency.md)：类型编码、行政区划字段及数据源差异。
- [已修正冲突](reports/conflicts/已修正冲突报告.md)：11组明确修正的结论与查询。
- [上一轮提交分析](reports/上一轮提交结果分析.md)、[错误修复](reports/fixes_report.md)：历史结果与构造反例。
- [平台得分记录](reports/得分结果.md)：用户保存的平台反馈。

报告描述的是各自生成时的版本。完整请求和响应属于本地运行产物，全新检出时需要额外取得对应运行目录；当前调用关系以 `src/text2query/` 代码及项目说明为准。本地格式检查、SQL编译和构造数据验证与比赛后台执行准确率分别记录。
