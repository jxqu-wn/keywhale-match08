"""Reviewed training/knowledge overlays; never add held-out answers to retrieval."""
import hashlib
import json
from .paths import CURATED


def prediction_hash(row):
    keys = ('db', 'query', 'db1', 'query1', 'db2', 'query2')
    return hashlib.sha256(json.dumps({k: row[k] for k in keys if k in row}, sort_keys=True).encode()).hexdigest()


def load_overlays():
    path = CURATED / 'rag_curation.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'example_patches': [], 'knowledge_patches': [], 'excluded_examples': []}


def apply_examples(rows, overlays):
    patches = {p['question_id']: p for p in overlays['example_patches']}
    excluded = {p['question_id'] for p in overlays.get('excluded_examples', [])}
    result = []
    for row in rows:
        if row['question_id'] in excluded:
            continue
        patch = patches.get(row['question_id'])
        if patch:
            if row['question'] != patch['question']:
                raise ValueError('训练修正题目不匹配: ' + str(row['question_id']))
            replacement = {**row, **patch['replacement']}
            if prediction_hash(row) not in (patch['original_prediction_sha256'], prediction_hash(replacement)):
                raise ValueError('训练修正原SQL已经变化: ' + str(row['question_id']))
            row = {**replacement, 'annotation_origin': 'reviewed_training_overlay',
                   'curation_reason': patch['reason']}
        result.append(row)
    return result


def apply_knowledge(rows, overlays):
    patches = {p['knowledge_id']: p for p in overlays['knowledge_patches']}
    result = []
    for row in rows:
        patch = patches.get(row['knowledge_id'])
        if patch:
            row = {**row, 'knowledge_text': patch['knowledge_text'],
                   'annotation_origin': 'reviewed_knowledge_overlay'}
        for declared in overlays.get('knowledge_scopes',[]):
            if declared['range'][0] <= row['knowledge_id'] <= declared['range'][1]:
                row = {**row,'declared_scope':{key:declared[key] for key in ('databases','sources')}}
                break
        result.append(row)
    return result


def hours_cte():
    def minutes(field):
        clean = f'LOWER(TRIM({field}))'
        hour = f'CAST({field} AS INTEGER)'
        return (f"CASE WHEN {field} IS NULL OR {clean} IN ('','none','null') THEN NULL ELSE "
                f"(CASE WHEN {clean} LIKE '%am' OR {clean} LIKE '%pm' THEN {hour}%12 "
                f"+ CASE WHEN {clean} LIKE '%pm' THEN 12 ELSE 0 END ELSE {hour} END)*60 "
                f"+ CASE WHEN INSTR({field},':')>0 THEN CAST(SUBSTR({field},INSTR({field},':')+1,2) AS INTEGER) ELSE 0 END END")
    return ('WITH parsed_hours AS (SELECT business_id,day_id,' + minutes('opening_time') + ' AS opening_minutes,'
            + minutes('closing_time') + ' AS closing_minutes FROM public_review_platform.Business_Hours),\n'
            'normalized_hours AS (SELECT business_id,day_id,CASE WHEN opening_minutes IS NULL OR closing_minutes IS NULL THEN NULL '
            'WHEN opening_minutes=closing_minutes THEN 24.0 '
            'WHEN closing_minutes>opening_minutes THEN (closing_minutes-opening_minutes)/60.0 '
            'ELSE (closing_minutes-opening_minutes+1440)/60.0 END AS hours FROM parsed_hours)')
