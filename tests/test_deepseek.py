import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from text2query.curl_batch import build
from text2query.model_pipeline import completion_payload


class DeepSeekIntegrationTests(unittest.TestCase):
    def test_provider_payload_preserves_messages_and_disables_thinking(self):
        messages = [{'role':'user','content':'Output JSON'}]
        actual = completion_payload('deepseek-flash',messages,'deepseek')
        self.assertIs(actual['messages'],messages)
        self.assertEqual(actual['thinking'],{'type':'disabled'})
        self.assertNotIn('chat_template_kwargs',actual)
        self.assertEqual(actual['response_format'],{'type':'json_object'})
        self.assertIn('chat_template_kwargs',completion_payload('contest-qwen',messages))

    def test_each_curl_transfer_references_auth_without_copying_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root/'private.header'
            private.write_text('Authorization: Bearer test-secret')
            for qid in (1,2):
                (root/f'{qid}.request.0.json').write_text(json.dumps({'model':'deepseek-flash'}))
            with contextlib.redirect_stdout(io.StringIO()):
                config = build(root,'https://api.deepseek.com',private)
            body = config.read_text()
            self.assertEqual(body.count('header = "@'+str(private)+'"'),2)
            self.assertNotIn('test-secret',body)
            self.assertEqual(body.count('next'),1)


if __name__ == '__main__':
    unittest.main()
