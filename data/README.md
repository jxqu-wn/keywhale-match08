# 数据目录

- `raw/subject/`：赛题原始Schema、5,830条训练标注、400条测试问题及1,800条专家知识。
- `raw/archives/`：原始ZIP/RAR和附带空说明文件，保留原文件名。
- `raw/competition_public.json`：获取的官方公开页面内容。
- `reference/`：补充区划表与数据库说明。
- `processed/`：元数据、知识目录、审计与清洗结果；当前清洗示例5,605条，训练4,483条、验证1,122条，知识1,800条，Schema目录265项。训练及清洗集合已排除人工修订配置声明的2条示例。`ambiguous_examples.json` 单独保留55组冲突标注，供RAG参考候选库表。
- `curated/`：用户补充的业务规则，与官方知识和标注分开保存。

`curated/education_conflict_resolutions.json`当前记录44组 `resolved_by_year_policy` 和11组 `resolved_by_query_correction`。`processed/ambiguous_examples.json`保留全部原始候选，并同步处理状态；11组修正结论见[已修正冲突报告](../docs/reports/conflicts/已修正冲突报告.md)。[剩余冲突报告](../docs/reports/conflicts/剩余冲突报告.md)是修正前的历史记录。

## 知识与人工修订

原始知识位于 `raw/subject/knowledge.json`。`prepare` 应用 `curated/rag_curation.json` 的 `knowledge_patches` 与 `knowledge_scopes` 后，生成 `processed/knowledge.json`。模型初始化时再次应用当前修订配置，建立关键词索引；每题向模型提供筛选后的知识子集。

| 人工配置 | 用途 |
|---|---|
| `rag_curation.json` | 训练示例与知识修订、排除示例、知识适用范围 |
| `business_rules.json` | 用户明确的业务口径 |
| `education_conflict_resolutions.json` | 冲突候选的处理状态与来源 |
| `question_corrections.json` | 完整题目精确匹配的修正查询及可召回修正示例 |
| `schema_adjudications.json` | 数据源编码、实际字段与过时注释的冲突处理 |

修改原始知识后需要重新 `prepare`；若同一ID存在 `knowledge_patches`，运行时采用修订文本。详细调用链见[知识检索与上下文注入](../docs/project/knowledge_context.md)。

使用 `python scripts/run.py prepare` 重建处理数据。原始输入保留。

在全新目录恢复补充数据可执行：

```sh
7z x 'data/raw/archives/数据库说明及行政区划表数据.rar' -odata/reference -y
```

基线和模型预测存于 `artifacts/`，最终提交存于 `outputs/submission/`。
