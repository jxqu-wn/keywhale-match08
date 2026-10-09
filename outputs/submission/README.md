# 提交结果

上传本目录的 `result.json`。该 UTF-8 JSON 包含完整 400 条测试问题，ID 顺序与原测试集一致，无重复和额外字段。

当前版本来源见 [submission_check.json](submission_check.json)：模型 `deepseek-flash`，供应方 `deepseek`，接口 `https://api.deepseek.com`，关闭思考模式，使用普通RAG流程。

本轮完整400题与上一轮Qwen的首轮消息逐条核对一致，共410次模型请求，包含校验修复重试。查询经过当前基础检查和自动转换，4条教育查询展开合并CTE；没有手工编辑模型查询。与上一轮的SQLite差异及构造反例见 [DeepSeek Flash与Qwen对比](../../docs/reports/DeepSeek_Flash_SQLite对比.md)。

完整运行记录位于 `artifacts/runs/prediction/deepseek-flash-20261009/predict-bc8c191e7fd6/`，由Git忽略并保留在本地。上一轮Qwen结果归档为 [result-20261009-qwen-cfcc4791c075.json](archive/result-20261009-qwen-cfcc4791c075.json)，更早提交保存在同一归档目录。再次运行入口时，代码和数据变化会产生新的指纹目录。

当前结果SHA256为 `0b900e961d074013fda5112064576b67be1616cbc588af4794bd31f7f451050c`。全部400条通过当前基础检查，56项自动测试通过；没有本轮比赛后台执行准确率或官方分数记录。文件及本地校验不能代替平台执行评测。
