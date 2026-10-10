"""Raw ReAct evidence; reviewed overlays never replace the retained originals."""
import hashlib
import re
from collections import defaultdict
from .curation import apply_examples, apply_knowledge, prediction_hash
from .paths import OUT, RAW, REFERENCE, ROOT
from .data import ambiguous_examples, load, normalize, read_text, schema_catalog, template


def provenance(path, **identifiers):
    return {'file': str(path.relative_to(ROOT)),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), **identifiers}


def raw_dictionaries(catalog, raw_only=False):
    documents = []
    paths = sorted((RAW / 'Schema').glob('*.txt'))
    if not raw_only:
        paths += sorted(REFERENCE.rglob('*.sql'))
    for path in paths:
        text, encoding = read_text(path)
        for match in re.finditer(r"INSERT\s+INTO\s+(?:[^;'\"]|'(?:''|[^'])*'|\"[^\"]*\")*;", text, re.I):
            target = re.match(r'INSERT\s+INTO\s+([\w.`"]+)', match.group(), re.I)
            if not target:
                continue
            name = target.group(1).replace('`', '').replace('"', '')
            table = name.split('.')[-1]
            preceding = text[:match.start()]
            uses = re.findall(r'\bUSE\s+(\w+)\s*;', preceding, re.I)
            database = name.split('.')[0] if '.' in name else (uses[-1] if uses else None)
            candidates = [t for t in catalog if t['table'].lower() == table.lower()
                          and (not database or t.get('database') == database)]
            if not database and len(candidates) == 1:
                database = candidates[0].get('database')
            source = candidates[0]['source'] if len(candidates) == 1 else 'MYSQL'
            documents.append({'id': f'{path.relative_to(ROOT)}:{match.start()}',
                'file': str(path.relative_to(ROOT)), 'source': source, 'database': database,
                'table': table, 'text': match.group(),
                'provenance': provenance(path, encoding=encoding, start_line=preceding.count('\n')+1)})
    return documents


def load_raw_evidence(evaluate, overlays, raw_only=False):
    rows = load(RAW / 'QA_train.json')
    held_out_templates = {template(r['question']) for r in load(OUT / 'dev.json')} if evaluate else set()
    groups = defaultdict(list)
    for row in rows:
        groups[normalize(row['question'])].append(row)
    conflicts = [g for g in groups.values() if len({prediction_hash(r) for r in g}) > 1]
    conflicting_ids = {r['question_id'] for g in conflicts for r in g}
    examples = []
    example_source = provenance(RAW / 'QA_train.json')
    for row in rows:
        if template(row['question']) in held_out_templates:
            continue
        reviewed = [row] if raw_only else apply_examples([row], overlays)
        effective = reviewed[0] if reviewed else row
        status = ('excluded_by_review' if not reviewed else
                  'reviewed' if effective.get('annotation_origin') else
                  'raw_conflicting' if row['question_id'] in conflicting_ids else 'raw_unreviewed')
        item = {**effective, 'raw_annotation': row, 'annotation_status': status,
                'provenance': {**example_source, 'question_id':row['question_id']}}
        if effective != row:
            item['reviewed_annotation'] = effective
        if not reviewed:
            item['review_note'] = next(p for p in overlays['excluded_examples'] if p['question_id'] == row['question_id'])
        examples.append(item)
    knowledge = []
    originals = load(RAW / 'knowledge.json')
    knowledge_source = provenance(RAW / 'knowledge.json')
    effective_knowledge = originals if raw_only else apply_knowledge(originals, overlays)
    for original, effective in zip(originals, effective_knowledge):
        item = {**effective, 'raw_annotation': original,
                'provenance': {**knowledge_source, 'knowledge_id':original['knowledge_id']}}
        if effective.get('knowledge_text') != original['knowledge_text']:
            item['reviewed_knowledge_text'] = effective['knowledge_text']
        knowledge.append(item)
    catalog, _ = schema_catalog(preserve_raw=True)
    available_ids = {r['question_id'] for r in examples}
    allowed_conflicts = [[r for r in g if r['question_id'] in available_ids] for g in conflicts]
    reference_documents = [] if raw_only else [
        {'text': read_text(path)[0], 'provenance': provenance(path)}
        for path in sorted(REFERENCE.rglob('*.txt'))]
    evidence_files = sorted(RAW.rglob('*'))
    if not raw_only:
        evidence_files += sorted(REFERENCE.rglob('*'))
    return {'catalog': catalog, 'examples': examples, 'knowledge': knowledge,
            'dictionaries': raw_dictionaries(catalog, raw_only=raw_only),
            'ambiguous': ambiguous_examples([g for g in allowed_conflicts if g], include_resolutions=not raw_only),
            'reference_documents': reference_documents,
            'evidence_manifest': {'mode': 'raw_only' if raw_only else 'raw_with_reviewed_overlays',
                'raw_example_count': len(rows),
                'retrievable_example_count': len(examples), 'raw_knowledge_count': len(originals),
                'held_out_template_count': len(held_out_templates),
                'files': [provenance(path) for path in evidence_files if path.is_file()]}}
