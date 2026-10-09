# 提交结果

上传本目录的 `result.json`。该 UTF-8 JSON 包含完整 400 条测试问题，ID 顺序与原测试集一致，无重复和额外字段。

当前版本来源见 [submission_check.json](submission_check.json)：模型 `deepseek-flash`，供应方 `deepseek`，接口 `https://api.deepseek.com`，关闭思考模式，使用ReAct流程。

本轮400题共消费1870次模型响应。398题在通常的ReAct循环内完成；2151、2265两题达到24步上限后，脚本各自动补一个外层JSON末尾 `}`，经过工具检查，再各调用模型一次完成提交。该恢复没有改变查询字段或条件，本轮没有手工修改生成查询。查询经过当前基础检查和自动转换，完整对比见 [DeepSeek Flash ReAct与初轮Qwen对比](../../docs/reports/DeepSeek_Flash_ReAct与初轮Qwen对比.md)。

完整运行记录位于 `artifacts/runs/prediction/deepseek-flash-react-20261009/react-predict-bb3435ff0550/`，由Git忽略并保留在本地。主要比较基线为 [最初Qwen结果](archive/result-20261009-before-rerun-210e08596136.json)，补充比较使用 [修正后Qwen](archive/result-20261009-qwen-cfcc4791c075.json) 和 [普通DeepSeek Flash](archive/result-20261009-deepseek-flash-0b900e961d07.json)。[本轮归档](archive/result-20261009-deepseek-flash-react-fcbe1975ef27.json)保留当前提交的精确副本。再次运行入口时，代码和数据变化会产生新的指纹目录。

当前结果SHA256为 `fcbe1975ef27ec0242fa856f68951c1f93931f4c4d4952490487e2718fda92bf`。全部400条通过当前基础检查；本轮实现的65项自动测试通过。199条输出为SQLite的查询均通过原始DDL编译，但5254题改变了数据源，不能将这一结果视为200题SQLite全部正确。没有本轮比赛后台执行准确率或官方分数记录。文件及本地校验不能代替平台执行评测。
