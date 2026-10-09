import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audit_sqlite_train_react_semantics import comparison_hash, judge_batch, normalize_sql


class SemanticAuditTests(unittest.TestCase):
    def test_normalization_retains_string_contents_and_cast_types(self):
        prefix = 'SELECT CAST(qty AS '
        self.assertNotEqual(normalize_sql(prefix+'REAL) FROM example.sales'),
                            normalize_sql(prefix+'INTEGER) FROM example.sales'))
        literal = "SELECT name FROM example.item WHERE name = 'a--b/*c*/'"
        self.assertIn("'a--b/*c*/'", normalize_sql(literal))
        self.assertNotEqual(normalize_sql(literal),normalize_sql(literal.replace('a--b','a--B')))
        self.assertNotEqual(normalize_sql('SELECT * FROM a LEFT JOIN b ON a.id=b.id'),
                            normalize_sql('SELECT * FROM a JOIN b ON a.id=b.id'))

    def test_judge_artifacts_and_cache_identity_use_actual_comparison(self):
        item={'question_id':1,'question':'数量是多少？','gold_query':'SELECT COUNT(*) FROM a',
              'prediction_query':'SELECT COUNT(*) FROM b'}
        changed={**item,'prediction_query':'SELECT COUNT(*) FROM c'}
        self.assertNotEqual(comparison_hash(item,'model','https://example.invalid'),
                            comparison_hash(changed,'model','https://example.invalid'))
        body={'choices':[{'message':{'content':json.dumps({'results':[{'id':1,
              'semantic_deviation':True,'category':'table','model_only':True,'explanation':'表不同'}]})}}]}
        with tempfile.TemporaryDirectory() as directory:
            with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(body).encode())):
                result=judge_batch([item],'test-secret','https://example.invalid','model',artifacts=Path(directory))
            self.assertEqual(result[1]['comparison_sha256'],comparison_hash(item,'model','https://example.invalid'))
            files=list(Path(directory).glob('*.json'))
            self.assertEqual(len(files),2)
            self.assertTrue(any(f.name.endswith('.request.json') for f in files))
            self.assertFalse(any('test-secret' in f.read_text() for f in files))


if __name__=='__main__':
    unittest.main()
