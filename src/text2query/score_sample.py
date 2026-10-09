"""Local diagnostic report; official score requires execution outcomes and weights."""
import argparse
import collections
import json
from pathlib import Path
from .pipeline import load, source, queries


def official_score(outcomes):
    numerator = denominator = 0.0
    for row in outcomes:
        difficulty = row['difficulty']
        kind = row['type']
        weight = {'简单':1.0,'中等':1.5,'困难':2.0}[difficulty] * {'基础查询':1.0,'知识增强':2.0,'高阶推理':3.0}[kind]
        if row['source'] == 'Elasticsearch':
            ids, order = row['id_match'], row['order_match']
            if type(ids) is not bool or type(order) is not bool:
                raise ValueError('Match outcomes must be booleans')
            if order and not ids:
                raise ValueError('Order match cannot be true when ID sets differ')
            accuracy = 0.5*ids + 0.5*order
        else:
            if type(row['execution_match']) is not bool:
                raise ValueError('execution_match must be boolean')
            accuracy = float(row['execution_match'])
        numerator += weight*accuracy
        denominator += weight
    if not denominator:
        raise ValueError('No execution outcomes')
    return 100*numerator/denominator


def summarize(directory):
    rows = load(directory/'sample.json')
    by = collections.defaultdict(collections.Counter)
    mismatches = []
    for gold in rows:
        record = load(directory/f"{gold['question_id']}.json")
        pred = record.get('prediction')
        valid = bool(pred) and not record.get('errors')
        try:
            route = bool(pred) and source(pred)==source(gold)
            exact = route and queries(pred)==queries(gold)
        except (KeyError, TypeError):
            route = exact = False
        c = by[source(gold)]
        c['n'] += 1;c['valid'] += valid;c['route'] += route;c['exact'] += exact
        if not exact:
            mismatches.append({'question_id':gold['question_id'],'question':gold['question'],'prediction':pred,'target':gold,'errors':record.get('errors')})
    total = sum(by.values(),collections.Counter())
    metrics = {'count':total['n'],'format_and_lexical_check':total['valid'],'source_correct':total['route'],'query_exact_match':total['exact'],
               'official_score':None,'official_score_status':'缺少真实执行结果和逐题难度/类型标签，不能计算',
               'by_source':dict(by)}
    (directory/'sample_report.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2)+'\n')
    (directory/'mismatches.json').write_text(json.dumps(mismatches,ensure_ascii=False,indent=2)+'\n')
    lines = ['# 200条验证样本评测报告','',
             '本次使用验证集分层随机抽样，固定种子42。样本及模型响应完整保存在本目录。检索示例仅来自训练分区。', '',
             '## 官方指标', '',
             '官方采用查询语句加权执行准确率。当前缺少真实数据库执行结果和逐题难度/类型标签，因此官方总分为不可计算，不能用字符串匹配率替代。标准见 `docs/competition/evaluation_standard.md`。','',
             '## 本地诊断指标','',
             f"- 样本数：{total['n']}。",
             f"- 严格查询匹配率：{total['exact']}/{total['n']} = {100*total['exact']/total['n']:.2f}%。",
             f"- 数据源/跨源顺序正确率：{total['route']}/{total['n']} = {100*total['route']/total['n']:.2f}%。",
             f"- 基础格式与词法检查通过率：{total['valid']}/{total['n']} = {100*total['valid']/total['n']:.2f}%。",'',
             '| 数据源 | 数量 | 严格匹配 | 严格匹配率 | 路由正确 | 基础检查通过 |',
             '|---|---:|---:|---:|---:|---:|']
    for key,c in sorted(by.items()):
        lines.append(f"| {key} | {c['n']} | {c['exact']} | {100*c['exact']/c['n']:.2f}% | {c['route']} | {c['valid']} |")
    lines += ['', '字符串完全一致不等于执行等价；差异记录不是已证实的错误列表。单源及跨源SQL都可能有不同的等价表达式。基础检查也不证明查询语义正确。', '',
              '官方SQL判等在标准查询无ORDER BY时忽略结果顺序；ES必须用执行返回的ID集合和顺序判定，不能通过DSL文本推断其分数。', '',
              '取得官方标签与执行结果后，可用 `official_score()` 计算权重汇总；跨源执行及复杂结果判等仍需遵循官方评测器。']
    (directory/'sample_report.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps(metrics,ensure_ascii=False))


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    summarize(parser.parse_args().directory)
