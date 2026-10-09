import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from text2query.score_sample import official_score
from text2query.model_pipeline import stratified_sample
from text2query.pipeline import source

class ScoreTests(unittest.TestCase):
    def test_weighted_execution(self):
        rows=[{'difficulty':'简单','type':'基础查询','source':'MYSQL','execution_match':True},
              {'difficulty':'困难','type':'高阶推理','source':'Elasticsearch','id_match':True,'order_match':False}]
        self.assertAlmostEqual(official_score(rows),400/7)
        rows[1]['id_match']=False;rows[1]['order_match']=True
        with self.assertRaises(ValueError):official_score(rows)

    def test_stratified_sample(self):
        rows=[{'question_id':i,'db':'MYSQL' if i<70 else 'SQLite'} for i in range(100)]
        a=stratified_sample(rows,20)
        self.assertEqual(a,stratified_sample(rows,20))
        self.assertEqual(len({r['question_id'] for r in a}),20)
        self.assertEqual(sum(source(x)=='MYSQL' for x in a),14)

if __name__=='__main__':unittest.main()
