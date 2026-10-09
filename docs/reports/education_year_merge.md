# 教育年度库统一处理

已将用户授权的“2024/2025合并、业务身份去重、2025整条记录优先”落实到查询生成代码。原45组纯年度差异已处理；另4组参数错误问题的年度部分也使用同一口径。2026-10-09用户授权自主修正剩余问题，11组已逐项修正，包括1700/1750隐藏在读条件及排序。原55组目前均已有明确实现口径。

最新[已修正冲突报告](conflicts/已修正冲突报告.md)包含原11组、24条标注的修正结论和可执行SQL；[剩余冲突报告](conflicts/剩余冲突报告.md)记录当前待决组数为0。原55组完整报告保留为处理前快照。

## 合并身份和关联

| 逻辑视图 | 业务身份键 | 处理粒度 |
|---|---|---|
| `edu_department` | department_code | 院系实体 |
| `edu_teacher` | teacher_no | 教师实体 |
| `edu_student` | student_no | 学生实体 |
| `edu_course` | course_code | 课程实体 |
| `edu_class` | class_code | 班级实体 |
| `edu_semester` | academic_year + semester_code | 学期实体 |
| `edu_enrollment` | student_no + course_code + academic_year + semester_code | 一次业务选课身份，不同学期保留不同事件 |
| `edu_exam_score` | 业务选课四字段 + exam_item | 成绩分项；同分项修订采用新记录 |

各年度内先通过本地外键解析业务身份，再跨年合并。关联字段中的学生、课程、教师、班级、院系、学期ID映射到合并维度；逻辑视图额外提供业务关联字段。跨年JOIN、GROUP BY、PARTITION BY、DISTINCT使用业务键，不能据相同的自增ID认定相同实体。返回“课程编号”等数字编号时仍返回题目要求的ID，不能替换成业务代码；旧年补充实体与新年实体可能保留相同的本地ID，业务身份用于区分它们。

同一业务身份优先采用2025整条记录，2025不存在时才补2024。2025字段为NULL或空串时不回退旧值。完成合并后再筛选、聚合、排序与LIMIT，平均值从选定明细重算。按专家知识1575，默认成绩统计/排名排除退课；明确要求查看退课时按题意保留。

业务键来自Schema中的编号/代码字段，是本次实施约定，Schema没有完整声明其UNIQUE约束。未拿到真实业务记录验证跨年稳定性。相同年度重复身份暂按本地自增ID最大记录确定性选择；成绩分项先按同年score_date_key最新，再按ID。选课同学期内的重修批次没有独立标识，因此当前同一学生、课程、学期按一次业务选课身份处理。

## 年份与问题参数

未明确查询年度时应用合并视图；明确2024或2025时查询指定年度；明确比较两年时分别计算，不折叠成一个总值。

“学号前缀2023”“入学年份2022”“学期代码包含2024”是字段筛选参数，不自动选择年度库。原4组候选的参数/排序错误已修正；1674/1724有用户明确例外，改为班级grade_year=2023并取消LIKE。“学期编号1”对应semester_id，不能换成semester_code='1'。

## 查询生成与RAG状态

模型在RAG提供的`edu_*`逻辑视图上生成查询条件、聚合和排序。`src/text2query/education.py`机械注入这些视图及依赖的只读CTE定义，生成引用`education_2024.*`和`education_2025.*`的完整SQL。最终结果中不依赖服务端预建视图，没有创建或修改实际数据库。

查询逻辑来自提供的`/models/qwen3-8-27b-fp8`模型；服务原始响应、`model_prediction`和自动展开后的`prediction`分开保存。视图展开不修改模型写出的具体筛选、投影或排名逻辑。实际执行环境需要支持CTE和ROW_NUMBER的MySQL版本；现有训练标注已包含同类写法。

`data/curated/business_rules.json`记录8类实体口径及用户指定题目的修正规则；`education_conflict_resolutions.json`记录55组处理状态。RAG继续保留全部原始候选与Schema，将已解决组标记为业务口径已处理。清洗数据、训练/验证划分、官方知识和原始SQL内容保持不变，新规则单独保存。运行指纹已加入新实现与状态，旧预测不作为新口径的缓存。

“学期、院系、学籍、选课”等明确教育词汇会先补充相关年度表，再检索冲突候选，避免“出现次数最多”等通用措辞把简化问句路由到无关的词频库。仅有“学生”等宽泛词汇且示例来自其他库时，不据此套用教育年度规则。

2026-10-09用户指定1674/1724按班级`grade_year=2023`替代学号LIKE条件，详见[2023级学生修正说明](student_cohort_trial.md)。该例外仅匹配当前完整题目；原有“按问题保留前缀”的一般规则继续适用于其他题目。

`question_corrections.json`保存11组明确修正。完整题目精确匹配时直接采用经检查的SQL，运行记录注明人工规则来源；其他问题可检索这些修正示例，由模型按当前参数生成。`schema_adjudications.json`补充跨数据源类型编码和实际区划字段说明。

## 验证与局限

- 全45组年度问题均通过真实模型生成，并在构造数据上执行检查。
- 另外2个指定年份查询和2个分别比较两年的查询通过，历史模型回归合计49/49。此次11组修正SQL另通过13/13项构造检查；当前26项单元检查通过。
- 数据故意包含跨年本地ID交换、同年业务编号重复、最新空值、学籍变化、退课更新，以及同一学生跨不同学期的选课。视图结果逐字段与独立Python合并结果比较。
- 构造执行使用SQLite附加内存库，补充CONCAT/MOD及MySQL整数空串比较、NULLIF除法语义。不证明真实MySQL数据库的执行兼容性或官方分数。
- 1700/1750按最新修正不额外限制在读，筛选未退课选课并按院系、人数降序；最新检查采用独立Python预期。旧49条模型回归属于此前年度处理记录，不证明最新修正已由模型重新生成。验证未将构造结果值发送给模型。

运行记录：`artifacts/runs/regression/education-ee34fe4ec1e9/`；`verification.json`保存检查结果，逐题文件保留SQL、构造预期与实际结果。原400条提交文件尚未重新生成，本次不报告新的官方准确率。

最新修正的复现入口：

```sh
python scripts/correct_conflicts.py
python -m unittest discover -s tests -v
```

下面的入口用于此前年度模型生成回归；旧请求、响应及参考属于当时的口径，最新11组以修正记录和独立构造预期为准：

```sh
python scripts/resolve_education_conflicts.py prepare
python scripts/run.py curl-batch 实际打印的运行目录
curl --parallel --parallel-max 2 --silent --show-error --config 实际打印的运行目录/batch.curl.conf
python scripts/resolve_education_conflicts.py process --directory 实际打印的运行目录
```

有修复请求时重复调用与解析，最多4轮。全部检查通过后执行`publish --directory 实际打印的运行目录`更新处理状态与剩余报告，再执行`python scripts/run.py prepare`同步RAG候选状态。
