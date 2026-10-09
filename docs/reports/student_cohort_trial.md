# 2023级在读学生修正

原题：查询学号前缀为2023的在读学生，返回学号与姓名，并按学号升序取前25名。

用户于2026-10-09要求尝试结合班级`grade_year=2023`，并明确不保留`LIKE`过滤。因此本题的试验含义为：查询所属班级入学年份为2023的在读学生，不限定学号的文字前缀。

该口径已写入`data/curated/business_rules.json`的`education_student_2023_cohort_trial`，仅匹配当前完整题目的规范化文本。用户随后授权自主修正全部剩余冲突，该题的明确查询现保存在`question_corrections.json`：精确匹配时直接采用修正SQL，其他题目可检索它作为示例。其他学号前缀问题继续按原文筛选，原始训练标注保持原文。

模型使用的逻辑查询：

```sql
SELECT s.student_no, s.student_name
FROM edu_student s
JOIN edu_class cl ON s._class_code = cl.class_code
WHERE s.student_status_code = 'ACTIVE'
  AND cl.grade_year = 2023
ORDER BY s.student_no ASC
LIMIT 25;
```

`grade_year`属于班级表，不是学生表字段。学生与班级均先按既定年度合并规则选2025记录，缺失时补2024；再通过班级业务代码关联和筛选。不能仅比较两年自增`class_id`来认定相同班级。

上面的`edu_*`是生成器提供的逻辑视图。程序注入两年物理表及去重CTE后，保存为[完整SQL](../../artifacts/runs/regression/conflict-corrections-20261009/1674.sql)，可执行查询不依赖数据库预建视图。

## 验证

- 本地构造数据有33名符合口径的学生，SQL按学号升序返回前25条，与独立Python合并结果一致。这些数量仅是构造数据结果。
- 验证覆盖非2023前缀但属于2023级、前缀2023但属于其他届别、最新非在读、最新班级届别变更、旧年独有学生、同年姓名修订及超过25名时的排序截断。
- 当前26项单元检查通过；11组明确修正SQL的构造检查13/13通过。原有49条已生成年度查询的构造回归是此前年度处理记录。
- 当前环境的`curl`无法连接模型接口，沙箱外调用被审批策略拒绝。尚未获得本次试验的模型响应，也没有真实业务记录验证；这里的完整SQL是按用户口径编写的本地实现。

记录目录：`artifacts/runs/regression/student-cohort-2023-trial/`。`local_verification.json`记录构造结果，`1674.request.0.json`保留待调用请求；尚无模型生成结果。

复现本地验证：

```sh
python scripts/verify_student_cohort.py prepare
python scripts/verify_student_cohort.py check-local
```

网络可用时可继续调用已有模型请求：

```sh
python scripts/run.py curl-batch artifacts/runs/regression/student-cohort-2023-trial
curl --silent --show-error --config artifacts/runs/regression/student-cohort-2023-trial/batch.curl.conf
python scripts/verify_student_cohort.py check
```

以上模型请求是额外验证入口；当前生成器对本题已直接使用修正查询。完整修正依据见[已修正冲突报告](conflicts/已修正冲突报告.md)。不会把构造执行结果当作官方答案或成绩，400条历史提交文件未重新生成。
