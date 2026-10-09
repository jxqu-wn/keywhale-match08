"""Move misplaced ES DSL sections without changing filters or aggregation values."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from text2query.shared.generation import parse_output, validate
from text2query.shared.paths import ROOT
from text2query.shared.dsl_normalization import normalize_es as normalize

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    catalog = json.loads((ROOT / 'data/processed/catalog.json').read_text())
    changed = []
    for item in json.loads((directory / 'sample.json').read_text()):
        qid = item['question_id']
        responses = sorted(directory.glob(f'{qid}.response.*.json'),
                           key=lambda p: int(p.name.split('.')[2]), reverse=True)
        if not responses:
            continue
        row = json.loads((directory / f'{qid}.json').read_text())
        if row.get('prediction') and not row.get('errors'):
            continue
        try:
            response = json.loads(responses[0].read_text())
            if response['choices'][0]['finish_reason'] != 'stop':
                continue
            raw = parse_output(response['choices'][0]['message']['content'])
            if raw.get('db') != 'Elasticsearch' or not validate(raw, catalog):
                continue
            prediction = normalize(raw)
            if validate(prediction, catalog, item['question'], row['context']['business_rules']):
                continue
            attempts = []
            for path in reversed(responses):
                completed = json.loads(path.read_text())
                choice = completed['choices'][0]
                attempts.append({'content': choice['message']['content'],
                                 'finish_reason': choice['finish_reason'], 'usage': completed.get('usage')})
        except (ValueError, KeyError, TypeError):
            continue
        row.update(prediction=prediction, model_prediction=raw, errors=[], attempts=attempts,
                   prediction_source='model_with_mechanical_dsl_normalization',
                   automatic_transforms=['es_dsl_level_normalization'],
                   postprocessor_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        (directory / f'{qid}.json').write_text(json.dumps(row, ensure_ascii=False, indent=2) + '\n')
        changed.append(qid)
    print(json.dumps({'normalized_ids': changed, 'condition_and_aggregation_values_preserved': True}))


if __name__ == '__main__':
    main()
