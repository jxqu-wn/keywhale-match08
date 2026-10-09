"""Reproducible data preparation and retrieval baseline; Python standard library only."""
import argparse
import collections
import hashlib
import json
import math
import random
import re
from pathlib import Path

from .paths import ROOT, RAW, REFERENCE, OUT, BASELINE, CURATED


def read_text(path):
    raw = Path(path).read_bytes()
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    raise ValueError(f'Unsupported encoding: {path}')


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(name, data, directory=OUT):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def normalize(text):
    return re.sub(r'[\s，。？！、；：,.?!;:‘’“”\"\u3000]', '', text.lower())


def tokens(text):
    text = text.lower()
    words = re.findall(r'[a-z_][a-z_0-9]*|\d+', text)
    for run in re.findall(r'[\u4e00-\u9fff]+', text):
        words.extend(run[i:i+2] for i in range(max(1, len(run)-1)))
    return words


def template(question):
    # Conservative grouping of parameter substitutions; not a SQL equivalence parser.
    text = normalize(question)
    text = re.sub(r"'[^']*'", '<value>', text)
    return re.sub(r'\d+(?:\.\d+)?', '<number>', text)


def source(row):
    return row.get('db') or row['db1'] + '->' + row['db2']


def queries(row):
    return [row['query']] if 'query' in row else [row['query1'], row['query2']]


def references(sql):
    ident = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|\w+)'
    parts = re.findall(r'\b(?:FROM|JOIN)\s+('+ident+r')\s*\.\s*('+ident+r')',sql,re.I)
    return sorted({'.'.join(unquote_identifier(part) for part in pair) for pair in parts})


def unquote_identifier(name):
    return name[1:-1] if len(name)>1 and name[0] in '"`[' and name[-1]=={'"':'"','`':'`','[':']'}[name[0]] else name


def ddl_columns(ddl):
    """Split top-level column definitions, preserving quoted names and inline columns."""
    body = re.sub(r'--[^\n]*', '', ddl[ddl.find('(')+1:ddl.rfind(')')])
    parts, start, depth, quote = [], 0, 0, None
    for i, char in enumerate(body):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == '[':
            quote = ']'
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == ',' and depth == 0:
            parts.append(body[start:i].strip())
            start = i+1
    parts.append(body[start:].strip())
    columns = []
    for part in parts:
        match = re.match(r'("[^"]+"|`[^`]+`|\[[^\]]+\]|\w+)\s+(\w+(?:\([^)]*\))?)', part)
        if match and match.group(1).upper() not in {'PRIMARY','FOREIGN','UNIQUE','KEY','INDEX','CONSTRAINT','CHECK','REFERENCES','ON'}:
            columns.append({'name':unquote_identifier(match.group(1)), 'type':match.group(2), 'definition':part})
    return columns


def schema_catalog():
    tables, encodings = [], {}
    for path in sorted((RAW / 'Schema').glob('*.txt')):
        text, encoding = read_text(path)
        encodings[path.name] = encoding
        ident = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|\w+)'
        events = list(re.finditer(r'数据库:\s*(\w+)|\bUSE\s+(\w+)\s*;|CREATE TABLE\s+(?:IF NOT EXISTS\s+)?('+ident+r'(?:\s*\.\s*'+ident+r')?)', text, re.I))
        database = None
        for i, event in enumerate(events):
            if event.group(1) or event.group(2):
                database = event.group(1) or event.group(2)
                continue
            name = event.group(3).replace('"', '').replace('`', '').replace('[','').replace(']','')
            db, table = name.split('.', 1) if '.' in name else (database, name)
            engine = 'SQLite' if path.name == 'bird_schemas.txt' else 'MYSQL'
            if db == 'public':
                engine = 'POSTGRESQL'
            end = events[i+1].start() if i+1 < len(events) else len(text)
            block = text[event.start():end]
            # Find balanced DDL parentheses, ignoring parentheses in strings/comments.
            start = block.find('(')
            depth, quote, stop = 0, None, len(block)
            for j in range(start, len(block)):
                c = block[j]
                if quote:
                    if c == quote and (j == 0 or block[j-1] != '\\'):
                        quote = None
                elif c in "'\"`":
                    quote = c
                elif c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                    if depth == 0:
                        stop = j+1
                        break
            ddl = block[:stop]
            columns = ddl_columns(ddl)
            tables.append({'source': engine, 'database': db, 'table': table, 'columns': columns,
                           'ddl': ddl, 'file': path.name,
                           'foreign_keys': re.findall(r'FOREIGN KEY[^\n]+', ddl, re.I)})
        if 'PUT /medical_institutions' in text:
            mapping = text.split('PUT /medical_institutions', 1)[1].split('-- ===', 1)[0]
            mapping = re.sub(r'//[^\n]*', '', mapping).strip()
            obj = json.loads(mapping)
            tables.append({'source': 'Elasticsearch', 'database': None, 'table': 'medical_institutions',
                           'mapping': obj['mappings']['properties'], 'file': path.name})
    return tables, encodings


def split_groups(rows, ratio=0.2, seed=42):
    groups = collections.defaultdict(list)
    for row in rows:
        groups[template(row['question'])].append(row)
    buckets = collections.defaultdict(list)
    for group in groups.values():
        buckets['|'.join(sorted({source(x) for x in group}))].append(group)
    train, dev = [], []
    rng = random.Random(seed)
    for key in sorted(buckets):
        bucket = buckets[key]
        rng.shuffle(bucket)
        target = round(sum(map(len, bucket))*ratio)
        count = 0
        for group in bucket:
            dest = dev if count < target else train
            dest.extend(group)
            if dest is dev:
                count += len(group)
    return sorted(train, key=lambda x: x['question_id']), sorted(dev, key=lambda x: x['question_id'])


def ambiguous_examples(groups):
    """Keep conflicting annotations as alternatives, never as a single gold answer."""
    result = []
    resolution_path = CURATED/'education_conflict_resolutions.json'
    resolutions = load(resolution_path).get('groups',{}) if resolution_path.exists() else {}
    for group in groups:
        tables = sorted({ref for row in group for query in queries(row) for ref in references(query)})
        item = {'group_id':'ambiguous-'+str(group[0]['question_id']),
                       'question':group[0]['question'], 'status':'unresolved_candidates',
                       'tables':tables,
                       'instruction':'候选标注代表可能涉及的库表，不是唯一正确答案；按当前问题和显式业务规则确定如何路由或合并。',
                       'candidate_annotations':group}
        resolution = resolutions.get(item['group_id'])
        if resolution:
            item['resolution'] = resolution
            if resolution['status']=='resolved_by_year_policy':
                item['status'] = 'business_rule_resolved'
                item['instruction'] = '年度口径已由用户确认；原始候选仅保留来源线索，查询使用教育年度合并视图。'
            elif resolution['status']=='resolved_by_query_correction':
                item['status'] = 'query_correction_resolved'
                item['instruction'] = '已按问题、Schema和知识修正；原始候选保留证据，优先使用当前修正示例。'
        result.append(item)
    return result


def prepare():
    from .curation import load_overlays, apply_examples, apply_knowledge
    rows = load(RAW / 'QA_train.json')
    test = load(RAW / 'QA_test.json')
    knowledge = load(RAW / 'knowledge.json')
    catalog, encodings = schema_catalog()
    grouped = collections.defaultdict(list)
    for row in rows:
        grouped[normalize(row['question'])].append(row)
    duplicate_groups = [xs for xs in grouped.values() if len(xs) > 1]
    conflicts = [xs for xs in duplicate_groups if len({json.dumps({k:v for k,v in x.items() if k not in ('question','question_id')}, sort_keys=True) for x in xs}) > 1]
    excluded = {x['question_id'] for xs in conflicts for x in xs}
    clean = [xs[0] for xs in grouped.values() if xs[0]['question_id'] not in excluded]
    train, dev = split_groups(clean)
    assert not ({template(x['question']) for x in train} & {template(x['question']) for x in dev})
    overlays = load_overlays()
    # Split from immutable annotations first. Never rewrite held-out targets.
    clean = apply_examples(clean, overlays)
    train = apply_examples(train, overlays)
    knowledge = apply_knowledge(knowledge, overlays)
    dictionary_docs = []
    paths = [RAW / 'Schema/commom_schema.txt'] + sorted((REFERENCE).rglob('*.sql'))
    for path in paths:
        text, _ = read_text(path)
        for match in re.finditer(r'INSERT\s+INTO\s+.*?;', text, re.I | re.S):
            table_match = re.match(r'INSERT\s+INTO\s+([\w.`"]+)',match.group(0),re.I)
            full = table_match.group(1).replace('`','').replace('"','') if table_match else ''
            table_name = full.split('.')[-1]
            candidates = [t for t in catalog if t['table'].lower()==table_name.lower()]
            db = full.split('.')[0] if '.' in full else ('common' if path.name=='administrative_division.sql' else None)
            if not db and len(candidates)==1:
                db = candidates[0]['database']
            dictionary_docs.append({'id': f'{path.name}:{match.start()}', 'file': str(path.relative_to(ROOT)),
                                    'source':'MYSQL','database':db,'table':table_name,'text':match.group(0)})
    known = {f"{x['database']}.{x['table']}" for x in catalog if x.get('database')}
    known_folded = {k.lower() for k in known}
    unknown = collections.Counter(r for row in rows for q in queries(row) for r in references(q) if r.lower() not in known_folded)
    report = {'counts': {'raw_train':len(rows),'test':len(test),'knowledge':len(knowledge),'clean':len(clean),'train':len(train),'dev':len(dev),'schema_entries':len(catalog)},
              'sources':dict(collections.Counter(source(x) for x in rows)), 'encodings':encodings,
              'duplicate_groups': [[x['question_id'] for x in xs] for xs in duplicate_groups],
              'conflicting_groups':conflicts, 'unknown_qualified_references':dict(unknown),
              'train_test_exact_overlap':[x['question_id'] for x in test if x['question'] in {r['question'] for r in rows}],
              'manual_issues':['船舶类型字典油轮=35，与部分标注油轮=80冲突', '电商引用administrative_division.division_code，但通用表定义id'],
              'split':{'seed':42,'method':'normalized question parameter-template grouping, source-stratified; semantic paraphrase leakage remains possible'},
              'sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((RAW).rglob('*')) if p.is_file()}}
    for name, data in [('catalog.json',catalog),('knowledge.json',knowledge),('dictionary_documents.json',dictionary_docs),('train.json',train),('dev.json',dev),('clean.json',clean),('audit.json',report)]:
        save(name,data)
    save('ambiguous_examples.json',ambiguous_examples(conflicts))
    save('curation_audit.json', {'training_patch_ids':[p['question_id'] for p in overlays['example_patches']],
                               'knowledge_patch_ids':[p['knowledge_id'] for p in overlays['knowledge_patches']],
                               'excluded_example_ids':[p['question_id'] for p in overlays.get('excluded_examples',[])],
                               'validation_targets_unchanged':True, 'overlay_file':'data/curated/rag_curation.json'})
    print(json.dumps(report['counts'], ensure_ascii=False))


class Retriever:
    def __init__(self, texts):
        self.docs = [collections.Counter(tokens(x)) for x in texts]
        self.df = collections.Counter(t for doc in self.docs for t in doc)
        self.avg = sum(sum(d.values()) for d in self.docs)/max(1,len(self.docs))

    def search(self, query, top_k=5):
        scores = []
        query_tokens = set(tokens(query))
        for i, doc in enumerate(self.docs):
            size = sum(doc.values())
            score = 0
            for token in query_tokens:
                freq = doc[token]
                if freq:
                    idf = math.log(1+(len(self.docs)-self.df[token]+0.5)/(self.df[token]+0.5))
                    score += idf*freq*2.2/(freq+1.2*(0.25+0.75*size/max(1,self.avg)))
            scores.append((score,i))
        return sorted(scores, key=lambda x:(-x[0],x[1]))[:top_k]


def baseline(evaluate=False):
    train = load(OUT/'train.json' if evaluate else OUT/'clean.json')
    inputs = load(OUT/'dev.json' if evaluate else RAW/'QA_test.json')
    retriever = Retriever([x['question'] for x in train])
    knowledge = load(OUT/'knowledge.json')
    kr = Retriever([x['knowledge_text'] for x in knowledge])
    predictions, contexts = [], []
    route_hits = query_hits = 0
    for row in inputs:
        hits = retriever.search(row['question'])
        best = train[hits[0][1]]
        prediction = {'question_id':row['question_id'], **{k:v for k,v in best.items() if k not in ('question','question_id')}}
        predictions.append(prediction)
        context = {'question_id':row['question_id'],'question':row['question'],
                   'mode':'retrieval_copy_baseline', 'examples':[{'score':round(s,4),**train[i]} for s,i in hits],
                   'knowledge':[knowledge[i] for s,i in kr.search(row['question']) if s>0],
                   'candidate_tables':sorted(set(r for _,i in hits for q in queries(train[i]) for r in references(q)))}
        contexts.append(context)
        if evaluate:
            route_hits += source(best)==source(row)
            query_hits += queries(best)==queries(row) and source(best)==source(row)
    prefix = 'dev' if evaluate else 'test'
    save(prefix+'_baseline_predictions.json', predictions, BASELINE)
    save(prefix+'_contexts.json', contexts, BASELINE)
    if evaluate:
        metrics = {'count':len(inputs),'source_accuracy':route_hits/max(1,len(inputs)),
                   'query_exact_match':query_hits/max(1,len(inputs)),
                   'limitations':'Copies nearest training query without parameter adaptation; no execution accuracy measured.'}
        save('baseline_metrics.json',metrics, BASELINE)
        print(json.dumps(metrics,ensure_ascii=False))
    else:
        print(f'Wrote {len(predictions)} provisional predictions and retrieval contexts')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare','evaluate','predict'])
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare()
    else:
        baseline(args.command == 'evaluate')
