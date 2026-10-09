# 数据一致性处理

以下两项来自原数据审计，独立于55组同题冲突。已按用户授权选定处理口径，写入`data/curated/schema_adjudications.json`并提供给相关RAG上下文；原始数据保留。

| 事项 | 采用口径 | 本地依据 |
|---|---|---|
| 油轮35/80 | 按数据源和实际字段解释。MySQL基础船舶表的字典外键使用油轮35；PostgreSQL AIS记录按题目中的油轮80。禁止把两源枚举直接等值关联。问题明确给出代码时按当前问题。 | `common.vessel_type_dict`的油轮记录为35，`transportation.vessel.vessel_type`有该字典外键；AIS题2492等明确指定80，AIS表没有该MySQL外键。 |
| 行政区划division_code/id | 使用实际表`common.administrative_division.id CHAR(6)`关联；业务表province_code等真实列名不变。按字符串比较、保留前导零。 | 通用表CREATE TABLE不存在division_code；电商引用它的位置是字段注释。 |

类型代码处理是基于本地源定义的选择，不证明真实数据已消除编码矛盾。当前未在真实MySQL/PostgreSQL数据库执行核验。区划实际列定义明确，因此不依据过时注释臆造字段。
