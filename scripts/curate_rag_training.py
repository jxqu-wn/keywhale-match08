"""Build reviewed overlays using only original training rows and knowledge."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from text2query.shared.curation import prediction_hash, hours_cte
from text2query.shared.paths import RAW, CURATED

rows = {row['question_id']: row for row in json.loads((RAW/'QA_train.json').read_text())}
knowledge = {row['knowledge_id']: row for row in json.loads((RAW/'knowledge.json').read_text())}
patches = []


def patch(qid, sql, reason):
    row = rows[qid]
    patches.append({'question_id': qid, 'question': row['question'],
                    'original_prediction_sha256': prediction_hash(row),
                    'replacement': {'db': row['db'], 'query': sql}, 'reason': reason})


patch(4298, "SELECT 100.0*COUNT(DISTINCT bc.business_id)/NULLIF((SELECT COUNT(*) FROM public_review_platform.Business),0) AS percentage FROM public_review_platform.Business_Categories bc JOIN public_review_platform.Categories c ON c.category_id=bc.category_id WHERE c.category_name='Pets'", '所有商家是Business实体；类别关系不是分母。')
patch(4148, "SELECT SUM(CASE WHEN city='Phoenix' THEN 1 ELSE 0 END) AS num,100.0*SUM(CASE WHEN city='Phoenix' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0) AS percentage FROM public_review_platform.Business", '城市商家计数与占比不需要关联类别表，避免重复及遗漏无类别商家。')
patch(4159, "SELECT 100.0*COUNT(DISTINCT b.business_id)/NULLIF((SELECT COUNT(*) FROM public_review_platform.Business WHERE city='Chandler'),0) AS percentage FROM public_review_platform.Business b WHERE b.city='Chandler' AND b.stars=5 AND EXISTS (SELECT 1 FROM public_review_platform.Business_Categories bc JOIN public_review_platform.Categories c ON c.category_id=bc.category_id WHERE bc.business_id=b.business_id AND c.category_name='Real Estate')", '题目分母是Chandler所有商家，而非房地产商家。仅用于训练示例覆盖；不改验证金标。')
patch(4309, "SELECT 100.0*COUNT(*)/NULLIF((SELECT COUNT(*) FROM public_review_platform.Business),0) AS percentage FROM public_review_platform.Business b WHERE EXISTS (SELECT 1 FROM public_review_platform.Business_Attributes ba JOIN public_review_platform.Attributes a ON a.attribute_id=ba.attribute_id WHERE ba.business_id=b.business_id AND a.attribute_name='Accepts Credit Cards' AND LOWER(ba.attribute_value)='true')", '接受信用卡需属性值为true；分母含全部商家，避免按属性行计数。')
patch(4332, "SELECT 100.0*COUNT(*)/NULLIF((SELECT COUNT(*) FROM public_review_platform.Business WHERE city='Phoenix'),0) AS percentage FROM public_review_platform.Business b WHERE b.city='Phoenix' AND EXISTS (SELECT 1 FROM public_review_platform.Reviews r WHERE r.business_id=b.business_id AND r.review_votes_funny='Low')", '具有低有趣投票评论的商家按存在性去重；每商家可有多评论。')
patch(3579, "SELECT AVG(p.height) FROM professional_basketball.players p WHERE EXISTS (SELECT 1 FROM professional_basketball.player_allstar a WHERE a.playerID=p.playerID AND a.conference='East')", '去重球员而非AVG(DISTINCT height)；同身高不同球员均应计入。')
patch(3596, "SELECT DISTINCT c.tmID FROM professional_basketball.coaches c JOIN professional_basketball.awards_coaches a ON a.coachID=c.coachID AND a.year=c.year WHERE a.year=2010 AND a.award='NBA Coach of the Year'", '获奖当年球队，补上年份关联，不包含职业生涯其他年份球队。')
patch(3518, "SELECT DISTINCT t.divID FROM professional_basketball.awards_coaches a JOIN professional_basketball.coaches c ON a.coachID=c.coachID JOIN professional_basketball.teams t ON c.tmID=t.tmID AND c.year=t.year WHERE a.year=1977 AND a.award='NBA Coach of the Year' AND c.year=1976", '题目明确获奖年1977与执教年1976不同，球队表须与实际执教年份匹配。')
patch(3772, "SELECT p.publisher_name FROM video_games.publisher p JOIN video_games.game_publisher gp ON gp.publisher_id=p.id GROUP BY p.id,p.publisher_name ORDER BY COUNT(DISTINCT gp.game_id) DESC,p.id ASC LIMIT 1", '按发行商主键统计不同游戏，不统计发行商ID或发行关系条数；并列按ID稳定取一。')
for qid, genre in ((3875, 'Action'), (3896, 'Puzzle')):
    patch(qid, f"SELECT p.publisher_name FROM video_games.publisher p JOIN video_games.game_publisher gp ON gp.publisher_id=p.id JOIN video_games.game g ON g.id=gp.game_id JOIN video_games.genre ge ON ge.id=g.genre_id WHERE ge.genre_name='{genre}' GROUP BY p.id,p.publisher_name ORDER BY COUNT(DISTINCT g.id) DESC,p.id ASC LIMIT 1", '按发行商主键区分同名主体，并按不同游戏数排名。')
patch(5293, "SELECT DISTINCT a.city FROM books.customer c JOIN books.customer_address ca ON ca.customer_id=c.customer_id JOIN books.address a ON a.address_id=ca.address_id JOIN books.country co ON co.country_id=a.country_id WHERE co.country_name='United States of America'", '限定客户实际关联地址；不把地址维表全部城市当成客户城市，不额外推断当前地址或已下单。')
patch(4401, "SELECT e.user_id FROM public_review_platform.Elite e JOIN public_review_platform.Years y ON y.year_id=e.year_id WHERE y.actual_year=2006 ORDER BY e.user_id LIMIT 5", 'year_id是Years外键；按actual_year筛选，即使代理键恰等于年份也保持兼容。')
patch(4205, "SELECT COUNT(DISTINCT u.user_id) FROM public_review_platform.Users u JOIN public_review_platform.Elite e ON e.user_id=u.user_id JOIN public_review_platform.Years y ON y.year_id=e.year_id WHERE u.user_yelping_since_year=y.actual_year", '比较加入实际年份与精英实际年份，按用户去重。')
patch(4375, "WITH first_elite AS (SELECT e.user_id,MIN(y.actual_year) AS first_year FROM public_review_platform.Elite e JOIN public_review_platform.Years y ON y.year_id=e.year_id GROUP BY e.user_id) SELECT AVG(f.first_year-u.user_yelping_since_year) FROM public_review_platform.Users u JOIN first_elite f ON f.user_id=u.user_id WHERE u.user_fans='Uber'", '首次成为精英的实际年份减加入年份，再按用户平均，不按每次精英记录重复加权。')
for qid, suffix in (
    (4134, "SELECT h.hours AS hour FROM normalized_hours h JOIN public_review_platform.Days d ON d.day_id=h.day_id WHERE d.day_of_week='Saturday' AND h.business_id=5734"),
    (4169, "SELECT h.hours AS opening_hours FROM normalized_hours h JOIN public_review_platform.Days d ON d.day_id=h.day_id WHERE d.day_of_week='Friday' AND h.business_id=53"),
    (4175, "SELECT h.hours AS hour FROM normalized_hours h JOIN public_review_platform.Business b ON b.business_id=h.business_id WHERE b.business_id=12 AND b.city='Scottsdale' AND h.day_id=3"),
    (4366, "SELECT business_id FROM normalized_hours WHERE hours IS NOT NULL ORDER BY hours,business_id,day_id LIMIT 1"),
    (4416, "SELECT AVG(h.hours) FROM normalized_hours h JOIN public_review_platform.Business b ON b.business_id=h.business_id WHERE b.city='El Mirage' AND b.state='AZ'")):
    patch(qid, hours_cte()+'\n'+suffix, '按知识159解析12小时制文本，支持分钟及跨午夜，同值按知识163视为24小时；平均仅对已提供的有效营业日记录。')

patch(2640, "WITH target AS (SELECT geom FROM vessel_ais_data WHERE mmsi=414000001 ORDER BY timestamp DESC LIMIT 1), latest AS (SELECT DISTINCT ON (mmsi) * FROM vessel_ais_data ORDER BY mmsi,timestamp DESC) SELECT v.* FROM latest v CROSS JOIN target t WHERE v.mmsi!=414000001 AND v.vessel_type=70 AND ST_DWithin(v.geom::geography,t.geom::geography,10000)", '先按每船取真正最新记录，再判断距离；不得用范围内旧位置代替最新位置。')

knowledge_patches = []
updates = {
    150: "在数据库public_review_platform的Business_Hours和Days中，opening_time、closing_time是12小时制文本（如8AM、9PM），不能直接相减。先解析为分钟数；相等按24小时，结束早于开始按跨午夜补1440分钟，最后除60得小时。NULL/空/None为未知，不当作全天。day_id关联Days。",
    1355: "public_review_platform.Business_Hours.closing_time为营业结束时间文本；不能直接closing_time-opening_time。按照知识159解析AM/PM及分钟，处理跨午夜，相等按知识163为24小时。",
    153: "public_review_platform.Elite.year_id是Years.year_id外键；实际精英年份取Years.actual_year。user_id关联Users。按实际年份过滤须JOIN Years，不假设代理键等于年份。",
    158: "public_review_platform.Elite和Users通过user_id关联。user_average_stars=5/1表示最高/最低平均评分。Elite.year_id是年份代理键，实际精英年份须关联Years.actual_year。",
    83: knowledge[83]['knowledge_text']+"这是本赛题明确给出的计算口径，不改成公斤/厘米公式；统计全明星球员平均BMI时先按playerID去重，再按球员计算平均，排除height为空或0。",
    1624: knowledge[1624]['knowledge_text']+"这是赛题知识约定，使用total_assets，不自行减去total_liabilities；PB依赖本BPS公式，应同时参考知识1625。",
    1757: knowledge[1757]['knowledge_text']+"坐标确定换算：24°25.0′N=24.4166666667，24°28.0′N=24.4666666667；118°05.0′E=118.0833333333，118°02.0′E=118.0333333333。矩形包络是近似范围，不等于宽180米的真实航道。",
}
for kid, text in updates.items():
    knowledge_patches.append({'knowledge_id': kid, 'knowledge_text': text,
                              'original_text': knowledge[kid]['knowledge_text'],
                              'reason': '依据原Schema/相关知识消除表示、单位或字段歧义；保留竞赛明确公式。'})
result = {'version': 1, 'source': '用户授权按审计建议修正；所有示例取自原QA_train，不添加测试或验证答案。',
          'example_patches': patches, 'knowledge_patches': knowledge_patches,
          'excluded_examples': [
              {'question_id': 4249, 'reason': '题目要求评分比例，原SQL只有平均评论评分；平均对象粒度不明确，暂不作为答案示例。'},
              {'question_id': 4380, 'reason': '把review_count的等级字段/用户ID当数值平均，且时长文本直接相减；缺少精确评论数，不能构造可靠示例。'}]}
result['knowledge_scopes'] = []
for start, end, databases, sources in (
    (1572,1579,['education_2024','education_2025'],['MYSQL']),
    (1580,1603,['energy'],['MYSQL']), (1604,1664,['finance_tax'],['MYSQL']),
    (1665,1712,[],['Elasticsearch']), (1713,1760,['public'],['POSTGRESQL']),
    (1761,1761,['common'],['MYSQL']), (1762,1762,['transportation'],['MYSQL']),
    (1763,1764,['common'],['MYSQL']), (1765,1765,['transportation'],['MYSQL']),
    (1766,1766,[],['MYSQL','SQLite','POSTGRESQL','Elasticsearch']),
    (1767,1767,[],['MYSQL']), (1768,1773,['transportation'],['MYSQL']),
    (1774,1774,[],['MYSQL','SQLite','POSTGRESQL']),
    (1775,1776,['transportation'],['MYSQL']),
    (1777,1777,['transportation','public'],['MYSQL','POSTGRESQL']),
    (1778,1780,['common'],['MYSQL']), (1781,1789,['transportation'],['MYSQL']),
    (1790,1790,['transportation','public'],['MYSQL','POSTGRESQL']),
    (1791,1792,['transportation'],['MYSQL']), (1793,1800,['common'],['MYSQL'])):
    result['knowledge_scopes'].append({'range':[start,end],'databases':databases,'sources':sources,
                                       'reason':'依据原知识连续段落的字段与业务内容声明来源，补充无库名前缀的知识。'})
(CURATED/'rag_curation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
print(json.dumps({'training_patches': len(patches), 'knowledge_patches': len(knowledge_patches), 'excluded': 2}))
