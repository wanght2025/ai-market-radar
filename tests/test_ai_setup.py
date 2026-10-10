import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from deepseek_analysis import configure_ai,load_api_key,test_connection
from storage import read_json


class AiSetupTest(unittest.TestCase):
    def test_key_save_enable_and_read_only_connection_on_fresh_install(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'DEEPSEEK_API_KEY':''}):
            root=Path(folder)
            configure_ai(root,'synthetic-key',True)
            self.assertEqual(load_api_key(root),'synthetic-key')
            self.assertIsNone(read_json(root/'config.json')['deepseek']['max_requests_per_day'])
            response=Mock(status_code=200)
            response.json.return_value={'data':[{'id':'deepseek-v4-pro'}]}
            with patch('deepseek_analysis.requests.get',return_value=response) as connection,patch('deepseek_analysis.requests.post',side_effect=AssertionError('No paid generation')):
                self.assertTrue(test_connection(root)['ok'])
                connection.assert_called_once()
            configure_ai(root,'',False)
            self.assertEqual(load_api_key(root),'synthetic-key')
            with self.assertRaisesRegex(ValueError,'AI未启用'):test_connection(root)

    def test_missing_invalid_key_and_http_failures_are_explained(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'DEEPSEEK_API_KEY':''}):
            root=Path(folder)
            for key in ('','key\nINJECTED=1'):
                with self.assertRaises(ValueError):configure_ai(root,key,True)
            configure_ai(root,'synthetic-key',True)
            for code in (401,403,429,500):
                with self.subTest(code=code),patch('deepseek_analysis.requests.get',return_value=Mock(status_code=code)):
                    with self.assertRaises(ValueError) as error:test_connection(root)
                    self.assertNotIn('synthetic-key',str(error.exception))
