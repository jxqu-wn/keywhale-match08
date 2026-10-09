"""Paired SQLite audit: raw SQL differences and reproducible synthetic examples.

This script never changes model answers and never estimates competition accuracy.
Fixtures test particular semantic requirements; they are not the hidden database.
"""
import argparse
import collections
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from text2query.pipeline import references
from text2query.validation import sqlite_connection


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def tokens(sql):
    """Ignore whitespace/case outside literals, without claiming SQL equivalence."""
    return [token if token.startswith("'") else token.lower()
            for token in re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`[^`]*`|\[[^\]]*\]|\w+|[^\s]",sql.rstrip().rstrip(';'))]


def databases(sql):
    return sorted({ref.split('.',1)[0] for ref in references(sql)})


def check_sql(prediction,catalog):
    if prediction.get('db')!='SQLite':
        return {'db':prediction.get('db'),'compiled':None,'column_count':None}
    try:
        connection = sqlite_connection(catalog,prediction['query'])
        try:
            connection.set_progress_handler(lambda: 1,100000)
            connection.execute('EXPLAIN '+prediction['query'])
            cursor = connection.execute(prediction['query'])
            return {'db':'SQLite','compiled':True,'column_count':len(cursor.description)}
        finally:
            connection.close()
    except Exception as exc:
        return {'db':'SQLite','compiled':False,'column_count':None,'error':str(exc)}


REVIEWS = {
    3048: ('ambiguous','按每笔qty>20替代按图书汇总SUM(qty)>20，需金标确定销量粒度。'),
    3069: ('shared_error','Qwen仅查当月；Flash改查全年却添加未要求的出版年份筛选，并包含年内未来订单。两者都没有使用titles.ytd_sales。'),
    3279: ('projection_risk','题目问队长姓名，Flash额外返回captain_count。'),
    3318: ('projection_risk','题目只要求出生日期，Flash额外返回Player_Name。'),
    3340: ('shared_conditional_risk','平均每个国家的分母只包括有右手击球球员的国家，未从国家总体表统计。'),
    3348: ('projection_risk','题目要求姓名和国籍，Flash额外返回catches。'),
    3422: ('value_risk',"平局字面值从'Tie'改为'tie'；SQLite文本比较区分大小写，缺少真实值确认。"),
    3464: ('conditional_change','赛季年份改为比赛日期年份，赛季跨年时可能不等价。'),
    3512: ('mixed_change','Flash按执教年份数而非首末年份差排序有所改善，但球队姓名改成逗号拼接球队ID；源域仍待金标确认。'),
    3531: ('shared_error','两者均返回coachID，题目明确要求教练姓名。'),
    3582: ('projection_risk','全名从一列拼接字符串改成firstName、lastName两列。'),
    3590: ('projection_risk','Flash额外返回allstar_count，并额外排除空州名。'),
    3674: ('shared_conditional_risk','Flash经自动重试恢复每作品计数再平均，但两者INNER JOIN均漏掉零角色作品；Flash另排除舞台指示。'),
    3902: ('shared_conditional_risk','两者分母均来自发行关系表，未发行/无发行关系的游戏被排除。'),
    4063: ('ambiguous','Flash去掉ABS；题目“总数差”没有明确是否取绝对值。'),
    4180: ('ambiguous','按商家评论级别汇总改为城市商家数量，并增加计数列；“更吸引用户”口径需确认。'),
    4279: ('shared_error','两者按business_id分组后任选用户粉丝数；Flash把投票级别TEXT转整数，仍不是按用户点赞排名。'),
    4326: ('new_error','Flash添加未要求的opening_time=10AM，并将低签到值改为None；None在知识中表示无签到/不营业，低值编码待确认。'),
    4491: ('conditional_change','Flash添加LIMIT 1；存在多位配音演员时丢结果。'),
    4592: ('projection_risk','Flash增加title列；题目“哪些剧集”的预期列需评测确认。'),
    4661: ('new_error','Flash删除credited=true，可能返回未列入演职员表的记录。'),
    4682: ('new_error','Flash将年龄答案与United States直接比较，遗漏按UserID/SurveyID关联国家答案；知识188/189已提供。'),
    4689: ('shared_error','两者未排除知识192定义的-1空回答，Flash还取消空字符串过滤。'),
    4917: ('ambiguous','Flash把百分比分母从所有1988年参赛者改为仅女性参赛者。'),
    5011: ('mixed_change','Flash正确使用交易日期，但删除Bottle条件，将其他容器计入瓶数。'),
    5028: ('new_error','Flash删除Bottle条件，并将连续自然日数改为有交易的不同日期数。'),
    5029: ('projection_and_tie_risk','Flash新增品牌、利润列；MAX/MIN全部并列品牌改为两个LIMIT 1，可能漏并列极值。'),
    5099: ('new_error','Flash用page过滤修订ID，原始DDL明确revision才是修订页面ID。'),
    5206: ('new_error','Flash以AVG(order_line.price)替代SUM(price)/COUNT(DISTINCT order_id)，将每订单均价变成每本书均价。'),
    5215: ('new_error','Flash返回全部非空书名且按字母排序，没有销量或畅销条件。'),
    5254: ('new_error','Flash把客户联系邮箱路由到MySQL能源公司，并返回contact_phone；Qwen返回beer_factory.customers.Email。'),
    5400: ('endpoint_risk','Flash只考虑OBJ1_SAMPLE_ID，漏掉对象作为第二端点的关系；若“所有关系”包含双端点则错误。'),
    5411: ('new_error','Flash将white当作OBJ_CLASSES对象类别，原Schema/知识251说明颜色应从ATT_CLASSES属性查询。'),
    5430: ('ambiguous','Flash把“最高图像”理解成最常见关系；Qwen使用最大IMG_ID，两者均未按H高度，需澄清原题。'),
    5432: ('endpoint_risk','Flash删除OBJ2_SAMPLE_ID对应坐标分支；应返回哪一端点/全部对象坐标需确认。'),
    5545: ('projection_risk','Flash额外返回playerID并增加排序。'),
    5576: ('new_error','Flash删除HOF.category=Player，可能混入建设者或裁判。知识284已区分类别。'),
    5683: ('conditional_change','Flash删除反向对阵分支，若TeamVsTeam缺少对称记录会遗漏对手。'),
    5686: ('better_constraint','Flash补上题目明确列出的1924、1923、1918年候选范围。'),
    5716: ('ambiguous','曾经是球员的排除总体从players_teams改为players，身份映射/领域仍待金标。'),
    5775: ('shared_error','两者均将ISO8601带时区日期直接与空格形式字符串比较，知识295已提供存储格式。'),
    5855: ('better_key','Flash用end_station_id关联站点，Qwen使用站名，ID更符合主外键。'),
    5893: ('better_key','Flash比较起终站ID，Qwen比较名称，ID避免同名不同站混淆。'),
    5973: ('projection_risk','Flash将姓名从一列拆成两列，并额外返回租借次数。'),
    6022: ('knowledge_conflict','Flash按原租借费率最高选择，再算每日价格；Qwen按每日价格最大值。知识344和1113存在不同口径，不据此声称官方判错。'),
    6103: ('new_error','Flash将Gansu地区错误当成country；Qwen正确过滤address.district。'),
}


def fixture_cases():
    year = datetime.now(timezone.utc).year
    month = datetime.now(timezone.utc).month
    return [
        dict(id=3069,kind='shared_error',title='年初销售的旧书被错误日期筛选排除',expected=[['Early Book','Publisher']],rows={
            'books.publisher':[dict(publisher_id=1,publisher_name='Publisher')],
            'books.book':[dict(book_id=1,title='Early Book',publisher_id=1,publication_date=f'{year-1}-01-01'),dict(book_id=2,title='Current Book',publisher_id=1,publication_date=f'{year-1}-01-01')],
            'books.cust_order':[dict(order_id=1,order_date=f'{year}-01-01'),dict(order_id=2,order_date=f'{year}-{month:02d}-01')],
            'books.order_line':[dict(line_id=1,order_id=1,book_id=1,price=120),dict(line_id=2,order_id=2,book_id=2,price=100)]}),
        dict(id=3531,kind='shared_error',title='姓名要求不能由ID代替（仅检查输出含义，身份映射为构造前提）',expected=[['Ada Coach']],rows={
            'professional_basketball.players':[dict(playerID='coach-x',firstName='Ada',lastName='Coach')],
            'professional_basketball.coaches':[dict(coachID='coach-x',year=2000,tmID=t,lgID='NBA',stint=1) for t in ['A','B','C']]}),
        dict(id=4279,kind='shared_error',title='点赞最高用户与评论最多商家内任意用户不同',expected=[['High']],rows={
            'public_review_platform.Business':[dict(business_id=1),dict(business_id=2)],
            'public_review_platform.Users':[dict(user_id=1,user_fans='Low'),dict(user_id=2,user_fans='High'),dict(user_id=3,user_fans='Low'),dict(user_id=4,user_fans='Low')],
            'public_review_platform.Reviews':[dict(business_id=1,user_id=u,review_votes_useful='Low') for u in [1,3,4]]+[dict(business_id=2,user_id=2,review_votes_useful='High')],
            'public_review_platform.Tips':[dict(business_id=1,user_id=1,likes=1),dict(business_id=2,user_id=2,likes=100)]}),
        dict(id=4689,kind='shared_error',title='-1、空字符串和NULL均不应作有效回答',expected=[['A note']],rows={
            'mental_health_survey.Question':[dict(questionid=99,questiontext='Any additional notes or comments')],
            'mental_health_survey.Answer':[dict(UserID=i,QuestionID=99,SurveyID=2014,AnswerText=x) for i,x in enumerate(['-1','','A note',None],1)]}),
        dict(id=5775,kind='shared_error',title='ISO8601时区日期无法直接匹配空格时间文本',expected=[['Content']],rows={
            'music_platform_2.reviews':[dict(podcast_id='p',title='really interesting!',content='Content',rating=5,author_id='a',created_at='2018-04-24T12:05:16-07:00')]}),
        dict(id=3902,kind='conditional_risk',title='游戏总体包括没有发行关系的游戏（构造前提）',expected=[[100/3]],rows={
            'video_games.game':[dict(id=i,game_name=f'Game{i}') for i in [1,2,3]],
            'video_games.publisher':[dict(id=1,publisher_name='Brash Entertainment'),dict(id=2,publisher_name='Other')],
            'video_games.game_publisher':[dict(id=1,game_id=1,publisher_id=1),dict(id=2,game_id=2,publisher_id=2)]}),
        dict(id=3674,kind='conditional_risk',title='所有作品包含零角色作品（构造前提）',expected=[[0.5]],rows={
            'shakespeare.works':[dict(id=i,Title=f'Work{i}',LongTitle=f'Work{i}',Date=1600,GenreType='Comedy') for i in [1,2]],
            'shakespeare.chapters':[dict(id=1,work_id=1,Act=1,Scene=1,Description='Scene')],
            'shakespeare.characters':[dict(id=1,CharName='Character',Abbrev='C',Description='Character')],
            'shakespeare.paragraphs':[dict(id=1,chapter_id=1,character_id=1,ParagraphNum=1,PlainText='Line')]}),
        dict(id=4682,kind='new_error',title='按同一受访者关联年龄和国家答案',expected=[[20]],rows={
            'mental_health_survey.Question':[dict(questionid=1,questiontext='What is your age?'),dict(questionid=3,questiontext='What country do you live in?')],
            'mental_health_survey.Answer':[dict(UserID=1,QuestionID=1,SurveyID=2016,AnswerText='20'),dict(UserID=1,QuestionID=3,SurveyID=2016,AnswerText='United States'),dict(UserID=2,QuestionID=1,SurveyID=2016,AnswerText='40'),dict(UserID=2,QuestionID=3,SurveyID=2016,AnswerText='Canada')]}),
        dict(id=5099,kind='new_error',title='区分页面ID和修订页面ID',expected=[['Right']],rows={
            'language_corpus.pages':[dict(pid=1,page=7,revision=16203226,title='Right'),dict(pid=2,page=16203226,revision=9,title='Wrong')]}),
        dict(id=5206,kind='new_error',title='一个订单多本书时，每订单均价与每本均价不同',expected=[[30]],rows={
            'books.customer':[dict(customer_id=1,first_name='Lucas',last_name='Wyldbore')],
            'books.cust_order':[dict(order_id=1,customer_id=1),dict(order_id=2,customer_id=1)],
            'books.order_line':[dict(line_id=1,order_id=1,price=10),dict(line_id=2,order_id=1,price=20),dict(line_id=3,order_id=2,price=30)]}),
        dict(id=5215,kind='new_error',title='返回全部书籍不等于畅销书',expected=[['Z Bestseller']],rows={
            'books.book':[dict(book_id=1,title='A Other'),dict(book_id=2,title='Z Bestseller')],
            'books.order_line':[dict(line_id=i,book_id=2,price=10) for i in [1,2,3]]+[dict(line_id=4,book_id=1,price=10)]}),
        dict(id=5411,kind='new_error',title='white是对象属性，不是car等对象类别',expected=[[1]],rows={
            'image_and_language.OBJ_CLASSES':[dict(OBJ_CLASS_ID=1,OBJ_CLASS='car')],
            'image_and_language.IMG_OBJ':[dict(IMG_ID=2347915,OBJ_SAMPLE_ID=1,OBJ_CLASS_ID=1)],
            'image_and_language.ATT_CLASSES':[dict(ATT_CLASS_ID=1,ATT_CLASS='white')],
            'image_and_language.IMG_OBJ_ATT':[dict(IMG_ID=2347915,OBJ_SAMPLE_ID=1,ATT_CLASS_ID=1)]}),
        dict(id=6103,kind='new_error',title='Gansu是地址district而不是country',expected=[[1]],rows={
            'movie_3.country':[dict(country_id=1,country='China')],
            'movie_3.city':[dict(city_id=1,city='Lanzhou',country_id=1)],
            'movie_3.address':[dict(address_id=1,address='Address',district='Gansu',city_id=1,phone='123')]}),
        dict(id=5011,kind='new_error',title='瓶数不包括罐装（两种日期均设在范围内，以隔离容器遗漏）',expected=[[1]],rows={
            'beer_factory.customers':[dict(CustomerID=1,First='Tom',Last='Hanks')],
            'beer_factory.rootbeer':[dict(RootBeerID=1,ContainerType='Bottle',PurchaseDate='2016-01-01'),dict(RootBeerID=2,ContainerType='Can',PurchaseDate='2016-01-01')],
            'beer_factory.transaction':[dict(TransactionID=1,CustomerID=1,RootBeerID=1,TransactionDate='2016-01-01'),dict(TransactionID=2,CustomerID=1,RootBeerID=2,TransactionDate='2016-01-01')]}),
        dict(id=5028,kind='new_error',title='同一天瓶装和罐装同时出售，隔离瓶装条件遗漏',expected=[[2]],rows={
            'beer_factory.rootbeerbrand':[dict(BrandID=1,Caffeinated='TRUE')],
            'beer_factory.rootbeer':[dict(RootBeerID=i,BrandID=1,ContainerType='Bottle' if i<3 else 'Can') for i in [1,2,3]],
            'beer_factory.transaction':[dict(TransactionID=i,RootBeerID=i,TransactionDate='2016-01-01') for i in [1,2,3]]}),
        dict(id=4661,kind='new_error',title='未列入演职员表的高投票记录不能取代credited=true记录',expected=[['Actor']],rows={
            'simpson_episodes.Episode':[dict(episode_id='e1',votes=100),dict(episode_id='e2',votes=10)],
            'simpson_episodes.Credit':[dict(episode_id='e1',person='Carlton Batten',category='Other',credited='false'),dict(episode_id='e2',person='Carlton Batten',category='Actor',credited='true')]}),
        dict(id=5576,kind='new_error',title='名人堂建设者不能作为球员返回',expected=[['Player']],rows={
            'hockey.HOF':[dict(hofID='h1',category='Player'),dict(hofID='h2',category='Builder')],
            'hockey.Master':[dict(playerID='p1',hofID='h1',lastName='P',nameGiven='Player',shootCatch=None),dict(playerID=None,coachID='c1',hofID='h2',lastName='B',nameGiven='Builder',shootCatch=None)]}),
        dict(id=5686,kind='better_constraint',title='题目明确候选年份，排除不在候选中的相同罚时年份',expected=[[1924]],rows={
            'hockey.Teams':[dict(year=y,tmID='MTL',name='Montreal Canadiens') for y in [1924,1940]],
            'hockey.TeamsSC':[dict(year=y,tmID='MTL',PIM='49') for y in [1924,1940]]}),
        dict(id=5855,kind='better_key',title='不同站点重名时，站点ID准确定位归还地点（构造前提）',expected=[[1,'2013-01-01']],rows={
            'bike_share_1.station':[dict(id=1,name='Same Name',lat=37.331415,long=-121.8932),dict(id=2,name='Same Name',lat=38,long=-120)],
            'bike_share_1.trip':[dict(id=i,start_date='2013-01-01',end_station_name='Same Name',end_station_id=i) for i in [1,2]]}),
        dict(id=5893,kind='better_key',title='同名不同站不能计作起终站相同（构造前提）',expected=[[1]],rows={
            'bike_share_1.trip':[dict(id=1,start_station_id=1,end_station_id=2,start_station_name='Same Name',end_station_name='Same Name'),dict(id=2,start_station_id=1,end_station_id=1,start_station_name='Same Name',end_station_name='Same Name')]}),
    ]


def insert_rows(connection,table,rows):
    db,name = table.split('.',1)
    for row in rows:
        columns=','.join('"'+column+'"' for column in row)
        placeholders=','.join('?' for _ in row)
        connection.execute(f'INSERT INTO "{db}"."{name}" ({columns}) VALUES ({placeholders})',list(row.values()))


def equivalent(actual,expected):
    if len(actual)!=len(expected):
        return False
    # Tiny fixtures have explicit row order or a single row.
    for arow,erow in zip(actual,expected):
        if len(arow)!=len(erow):
            return False
        for a,e in zip(arow,erow):
            if isinstance(a,(int,float)) and isinstance(e,(int,float)):
                if not math.isclose(a,e,rel_tol=1e-9,abs_tol=1e-9):
                    return False
            elif a!=e:
                return False
    return True


def execute_case(case,old,new,catalog):
    sql = old['query']+' '+new['query']+' SELECT * FROM '+', '.join(case['rows'])
    connection=sqlite_connection(catalog,sql)
    try:
        for table,rows in case['rows'].items():
            insert_rows(connection,table,rows)
        outputs={}
        for label,pred in [('qwen',old),('deepseek',new)]:
            try:
                actual=[list(row) for row in connection.execute(pred['query']).fetchall()]
                outputs[label]={'actual':actual,'matches_fixture_expected':equivalent(actual,case['expected'])}
            except Exception as exc:
                outputs[label]={'error':str(exc),'matches_fixture_expected':False}
        return {**case,'question_id':case['id'],'outputs':outputs,'synthetic':True}
    finally:
        connection.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--previous',type=Path,required=True)
    parser.add_argument('--current',type=Path,required=True)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--previous-run',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    previous={r['question_id']:r for r in load(args.previous)}
    current={r['question_id']:r for r in load(args.current)}
    questions={r['question_id']:r['question'] for r in load(ROOT/'data/raw/subject/QA_test.json')}
    catalog=load(ROOT/'data/processed/catalog.json')
    ids=[qid for qid,r in previous.items() if r.get('db')=='SQLite']
    pairs=[]
    for qid in ids:
        old,new=previous[qid],current[qid]
        record=load(args.run/f'{qid}.json')
        before=load(args.previous_run/f'{qid}.request.0.json')
        after=load(args.run/f'{qid}.request.0.json')
        category,note=REVIEWS.get(qid,('not_flagged','未列入重点问题清单；不代表真实数据判定正确。'))
        pairs.append(dict(question_id=qid,question=questions[qid],previous=old,current=new,
            exactly_equal=old==new,token_sequence_equal=old.get('db')==new.get('db') and tokens(old['query'])==tokens(new['query']),
            previous_databases=databases(old['query']),current_databases=databases(new['query']),
            previous_check=check_sql(old,catalog),current_check=check_sql(new,catalog),
            same_first_request_messages=before['messages']==after['messages'],
            review_category=category,review_note=note,routing=record['context']['routing'],
            knowledge_ids=[r['knowledge_id'] for r in record['context']['knowledge']]))
    cases=[execute_case(case,previous[case['id']],current[case['id']],catalog) for case in fixture_cases()]
    if any('error' in value for case in cases for value in case['outputs'].values()):
        raise ValueError('Synthetic fixture could not execute: '+str([(c['id'],c['outputs']) for c in cases if any('error' in v for v in c['outputs'].values())]))
    all_context_matches=sum(load(args.run/f'{qid}.request.0.json')['messages']==load(args.previous_run/f'{qid}.request.0.json')['messages'] for qid in current)
    usage=collections.Counter()
    responses=list(args.run.glob('*.response.*.json'))
    for path in responses:
        for key,value in load(path).get('usage',{}).items():
            if isinstance(value,int):
                usage[key]+=value
    summary=dict(total_questions=len(current),sqlite_comparison_count=len(ids),
        exactly_equal=sum(p['exactly_equal'] for p in pairs),text_different=sum(not p['exactly_equal'] for p in pairs),
        same_ignoring_whitespace_and_case=sum(p['token_sequence_equal'] for p in pairs),
        changed_database_type_ids=[p['question_id'] for p in pairs if p['previous']['db']!=p['current'].get('db')],
        changed_database_domain_ids=[p['question_id'] for p in pairs if p['previous_databases']!=p['current_databases']],
        changed_column_count_ids=[p['question_id'] for p in pairs if p['previous_check']['column_count']!=p['current_check']['column_count']],
        previous_sqlite_compiled=sum(p['previous_check']['compiled'] is True for p in pairs),
        current_sqlite_compiled=sum(p['current_check']['compiled'] is True for p in pairs),
        current_sqlite_count=sum(p['current'].get('db')=='SQLite' for p in pairs),
        same_first_request_messages_count=all_context_matches,api_response_count=len(responses),usage=dict(usage),
        synthetic_case_count=len(cases),
        paired_fixture_regression_ids=[c['id'] for c in cases if c['outputs']['qwen']['matches_fixture_expected'] and not c['outputs']['deepseek']['matches_fixture_expected']],
        paired_fixture_improvement_ids=[c['id'] for c in cases if not c['outputs']['qwen']['matches_fixture_expected'] and c['outputs']['deepseek']['matches_fixture_expected']],
        synthetic_scope='有目的构造的反例，不能据此计算完整测试集准确率或平台分数。',
        official_previous_sqlite=dict(correct=137,total=200,score=381.5,max_score=569.5),official_current_accuracy=None)
    args.output.mkdir(parents=True,exist_ok=True)
    data=dict(previous_file=str(args.previous),current_file=str(args.current),previous_sha256=hashlib.sha256(args.previous.read_bytes()).hexdigest(),
        current_sha256=hashlib.sha256(args.current.read_bytes()).hexdigest(),run=str(args.run),summary=summary,pairs=pairs,synthetic_cases=cases)
    (args.output/'comparison.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    (args.output/'synthetic_cases.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2)+'\n')
    lines=['# DeepSeek Flash 与上一轮 Qwen 的 SQLite 对比','',
        '本轮使用 deepseek-flash（接口模型列表名称 DeepSeek-V4.1-Flash），关闭思考模式；完整400题。与上一轮相同的RAG数据、SYSTEM和首轮messages逐条核对，'+str(all_context_matches)+'/400一致。未用ReAct，未手工改模型SQL。',
        '',f"上一轮Qwen归档：`{args.previous}`；SHA256 `{data['previous_sha256']}`。",'',
        f"新提交：`{args.current}`；SHA256 `{data['current_sha256']}`。",'',
        '## 对比范围与结果','',
        '| 指标 | 结果 |','|---|---|',f'| 上一轮SQLite题目 | {len(ids)} |',
        f"| 回答文本完全相同 | {summary['exactly_equal']} |",f"| 回答文本不同 | {summary['text_different']}（包含别名、空白、写法差异；不等于正确性变化） |",
        f"| SQLite原始DDL编译 | Qwen {summary['previous_sqlite_compiled']}/200；Flash {summary['current_sqlite_compiled']}/{summary['current_sqlite_count']} |",
        f"| 数据库类型变化 | {summary['changed_database_type_ids']} |",f"| 返回列数变化 | {summary['changed_column_count_ids']} |",
        f"| API调用数 | {len(responses)}（含校验修复重试） |",'',
        '## 结论','',
        '在相同上下文、关闭思考的设置下，本轮没有证据支持Flash能提升SQLite得分。六项前次明确问题（3069、3531、4279、5265、4689、5775）仍未完整解决；可复现的新增语义退步包括年龄/国家关联、修订ID、订单均价、畅销筛选、颜色属性、地区字段及瓶装筛选。',
        'Flash也有局部改进：5686补全题目候选年份；5855和5893改用站点ID；3512按执教年份数量排名，较首末年份差合理。上述改进不能抵消或量化为官方总分变化。',
        '两轮仍共享RAG领域选错、必需知识遗漏或互相矛盾的问题；而4682、5411、6103说明Flash即便收到正确Schema/知识也可能绑定错字段。现有编译与少量语义规则无法发现这些错误，校验通过不能称为正确。',
        '', '## 可复现反例','',
        '以下在原始DDL的内存SQLite中执行两轮原SQL；输入是人为构造的数据。仅证明注明口径下的行为，不是隐藏测试集的金标。', '',
        '| 题号 | 检查要求 | Qwen符合构造预期 | Flash符合构造预期 |','|---|---|---|---|']
    for case in cases:
        lines.append(f"| {case['id']} | {case['title']} | {'是' if case['outputs']['qwen']['matches_fixture_expected'] else '否'} | {'是' if case['outputs']['deepseek']['matches_fixture_expected'] else '否'} |")
    lines += ['',f"这{len(cases)}个定向反例中，Qwen符合而Flash不符合的题号为{summary['paired_fixture_regression_ids']}；反方向为{summary['paired_fixture_improvement_ids']}。该样本有目的选取，不能外推准确率。",'',
        '## 重点变化与保留问题','', '| 题号 | 审核类别 | 观察 |','|---|---|---|']
    for qid,(category,note) in REVIEWS.items():
        lines.append(f'| {qid} | {category} | {note} |')
    lines += ['', '## 评测边界','',
        '上一轮平台SQLite是137/200（68.5%），得分381.5/569.5（66.99%）。本轮尚未在平台提交；本地没有真实数据库或逐题金标，不能给出官方准确率、提升题数或最终分数。199条SQLite编译通过，只表示语法和字段绑定可执行；5254被误路由到MySQL，仍在200题对比总体中。',
        '', '完整200题的两轮SQL、返回列数、源域、上下文核对、知识ID和全部反例输入输出见同目录comparison.json。', '',
        '接口依据：[模型说明](https://api-docs.deepseek.com/quick_start/pricing/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)、[JSON输出](https://api-docs.deepseek.com/guides/json_mode/)。', '']
    (args.output/'comparison.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
