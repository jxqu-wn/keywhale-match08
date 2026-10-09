# 跨年度教师查询与冲突候选保留

本次口径来自用户补充，不是官方数据集对所有年度库的统一规则：教师问题没有明确年份时，合并 `education_2024.teacher` 和 `education_2025.teacher`，按教师编号去重，同编号以 2025 年整条记录为准。明确年份时按问题年份查询。

## 原有上下文

检查“统计教师中电话不为空的数量，并返回数量。”的实际召回发现，原有 RAG 已包含两个年度的教师表。顶层 Schema 提供数据库、表名、字段、类型、DDL、主键、外键和来源文件；教师表的 `teacher_no` 与 `phone` 都可见。但原有提示没有表达跨年度合并及最新记录优先的规则，两个年度均可见不代表模型会正确合并。

原训练标注 1669、1682 查询 2024 年，1732 查询 2025 年，而问题相同。这三条曾随整组被排除出唯一答案示例集。原始训练数据和审计记录始终保留，Schema 与 `knowledge.json` 未因排除示例而删除。

## 当前处理

- 55 组、113 条冲突标注另存为 `data/processed/ambiguous_examples.json`，召回时最多提供 3 组相关候选，标明“不是唯一正确答案”。清洗后的训练和验证划分内容不变。
- 每个普通示例和冲突候选均增加 `schema_refs`，包含指令阶段、数据源、数据库和表名；完整定义保留在顶层 `schema`。这些引用及业务规则涉及的库表会加入 Schema 选择。
- `data/curated/business_rules.json` 独立保存用户口径，模型提示明确其优先于检索示例。暂仅用于未明确年份的教师查询，不推广到其他实体。
- 跨年关联暂用业务编号 `teacher_no`。两个年度的 `teacher_id` 都是自增主键，不能据此认定同一教师。Schema 没有声明 `teacher_no` 唯一，因此该选择仍需业务数据确认。
- 先决定采用哪年的记录，再执行问题条件。2025 年电话为 NULL 或空字符串时，不能回退使用 2024 年电话。“电话不为空”的过滤只适用于问题提出该要求时。
- 静态检查要求默认教师查询引用两年的教师表及 `teacher_no`，并拦截人数统计直接 `COUNT(*)` 且无去重结构的情况；这只检查必要条件，完整优先级和去重行为通过构造数据验证。

## 定向验证

`scripts/verify_teacher_rag.py` 准备两个真实模型请求，响应查询由提供的 `/models/qwen3-8-27b-fp8` 生成；通过 curl 调用服务。预期值和构造数据没有发送给模型。

构造数据包含：2024 年 T1/T2/T3 电话有值；2025 年 T1 电话改为 NULL、T3 改为空字符串、新增 T4 且 T4 在同一年重复。默认口径应只计 T2、T4，共 2 人；明确 2024 年则计 3 人。两年度自增编号故意不同。

```sh
python scripts/verify_teacher_rag.py prepare
python scripts/run.py curl-batch artifacts/runs/regression/teacher-rag-final
curl --parallel --parallel-max 2 --silent --show-error --config artifacts/runs/regression/teacher-rag-final/batch.curl.conf
python scripts/verify_teacher_rag.py check
```

初次回归保存在 `artifacts/runs/regression/teacher-rag/`：模型已正确处理跨年优先级，但直接 `COUNT(*)` 将同年重复 T4 计了两次，默认查询返回 3 而非 2。因此补强了编号去重提示和静态检查。

修复后的原始请求、上下文、模型响应和执行结果保存在 `artifacts/runs/regression/teacher-rag-final/`。验证使用 SQLite 附加内存库执行模型的兼容 SQL，仅验证构造数据上的逻辑，不代表官方数据库执行准确率。400 条旧提交结果未重新生成。

修复后两条真实模型查询均通过构造数据执行检查（2/2）：默认查询使用 `COUNT(DISTINCT t.teacher_no)`，返回 2；明确 2024 年的查询返回 3。14 项单元检查通过。旧 400 条结果重新导出至临时目录后 SHA-256 与原提交完全一致。
