import unittest
from pathlib import Path

import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text2query import pipeline as p


class PipelineTests(unittest.TestCase):
    def test_schema_engines_and_nested_columns(self):
        catalog, encodings = p.schema_catalog()
        self.assertEqual(encodings['core_schema.txt'], 'gb18030')
        vehicle = next(x for x in catalog if x.get('database') == 'transportation' and x['table'] == 'vehicle')
        self.assertIn('load_capacity', [c['name'] for c in vehicle['columns']])
        self.assertIn('company_id', [c['name'] for c in vehicle['columns']])
        ais = next(x for x in catalog if x['table'] == 'vessel_ais_data')
        self.assertEqual(ais['source'], 'POSTGRESQL')
        self.assertIn('geom', [c['name'] for c in ais['columns']])
        self.assertEqual(next(x for x in catalog if x['table'] == 'medical_institutions')['mapping']['location']['type'], 'geo_point')

    def test_parameter_groups_do_not_leak(self):
        rows = [{'question_id':i,'question':f'查询{i}公里内的船舶','db':'POSTGRESQL'} for i in range(1,10)]
        rows += [{'question_id':10,'question':'所有医院','db':'Elasticsearch'}]
        train, dev = p.split_groups(rows)
        self.assertFalse({p.template(x['question']) for x in train} & {p.template(x['question']) for x in dev})
        self.assertEqual(len(train)+len(dev),len(rows))

    def test_cross_source_and_retrieval(self):
        row = {'db1':'POSTGRESQL','db2':'MYSQL','query1':'SELECT mmsi FROM vessel_ais_data','query2':'SELECT * FROM transportation.vessel WHERE mmsi IN (result)'}
        self.assertEqual(p.source(row),'POSTGRESQL->MYSQL')
        self.assertEqual(p.references(row['query2']),['transportation.vessel'])
        self.assertEqual(p.Retriever(['驾驶员人数','医院床位数']).search('医院床位')[0][1],1)


if __name__ == '__main__':
    unittest.main()
