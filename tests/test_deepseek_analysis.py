import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from deepseek_analysis import analyze, analysis_input, load_api_key, model_budget
from storage import write_json


class DeepSeekTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = {'target_date':'2026-09-24', 'mode':'PRE_MARKET', 'coverage':{'valid_quotes':2}}

    def test_unconfigured_makes_no_request(self):
        with patch.dict('os.environ', {}, clear=True), patch('deepseek_analysis.requests.post') as post:
            self.assertEqual(analyze(self.root,self.payload,{})['status'], 'NOT_CONFIGURED')
            post.assert_not_called()

    def test_damaged_cache_never_triggers_an_automatic_paid_request(self):
        for value in ([],['invalid'],0,{}, {'status':'READY'}, {'status':'READY','content':''}):
            with self.subTest(value=value),patch('deepseek_analysis.json_digest',return_value='damaged'), \
                    patch('deepseek_analysis.load_api_key',return_value='test'), \
                    patch('deepseek_analysis.requests.post') as post:
                path=self.root/'data/ai/damaged.json';write_json(path,value);before=path.read_bytes()
                self.assertEqual(analyze(self.root,self.payload,{})['status'],'FAILED')
                post.assert_not_called()
                self.assertEqual(path.read_bytes(),before)

    def test_damaged_usage_records_block_model_calls(self):
        from datetime import datetime,timezone
        day=datetime.now(timezone.utc).date().isoformat()
        for account in ({'requests':-1,'total_tokens':0},{'requests':0,'total_tokens':-1},
                {'requests':True,'total_tokens':0},{'requests':0,'total_tokens':0,'reserved_tokens':-1},
                {'requests':0,'total_tokens':0,'unknown_usage_requests':-1},
                {'requests':0},{'total_tokens':0},{}):
            with self.subTest(account=account):
                ledger={'days':{day:account}}
                with self.assertRaises(ValueError):model_budget({},ledger)
                write_json(self.root/'data/ai/usage.json',ledger)
                with patch('deepseek_analysis.load_api_key',return_value='test'), \
                        patch('deepseek_analysis.requests.post') as post:
                    self.assertEqual(analyze(self.root,self.payload,{},force=True)['status'],'FAILED')
                    post.assert_not_called()

    def test_verified_summary_separates_relative_strength_from_rising(self):
        payload = {**self.payload, 'candidates':[{'代码':'000001'}]*50,
                   'market':{'equal_weight_change_percent':-2.24, 'turnover_cny':1716712968144},
                   'industries':[{'name':'样本行业','rank':1,'relative':2.10}]}
        summary = analysis_input(payload)['verified_summary']
        self.assertEqual(summary['candidate_count'],50)
        self.assertEqual(summary['market_turnover_display'],'17167.13亿元')
        self.assertFalse(summary['index_change_available'])
        self.assertEqual(summary['industry_absolute_changes'][0]['equal_weight_change_percent'],-0.14)
        self.assertNotIn('verified_summary',payload)

    def test_key_precedence_cache_and_changed_input(self):
        for mean, relative, expected in [(2, -1, 1), (-2, 1, -1), (0, 0, 0)]:
            with self.subTest(mean=mean):
                payload = {**self.payload, 'candidates':[],
                    'market':{'equal_weight_change_percent':mean,'turnover_cny':0},
                    'industries':[{'name':'sample','rank':1,'relative':relative}]}
                summary = analysis_input(payload)['verified_summary']
                self.assertEqual(summary['candidate_count'],0)
                self.assertEqual(summary['industry_absolute_changes'][0]['equal_weight_change_percent'],expected)
        (self.root/'.env').write_text('DEEPSEEK_API_KEY="file-test-key"\n', encoding='utf-8')
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(load_api_key(self.root), 'file-test-key')
        response = Mock(status_code=200)
        response.json.return_value = {'choices':[{'finish_reason':'stop','message':{'content':'测试解读'}}],
                                      'usage':{'total_tokens':100}}
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY':'env-test-key'}, clear=True), \
                patch('deepseek_analysis.requests.post', return_value=response) as post:
            first = analyze(self.root,self.payload,{})
            second = analyze(self.root,self.payload,{})
            self.assertEqual(first['status'],'READY')
            self.assertTrue(second['cached'])
            self.assertEqual(post.call_count,1)
            self.assertEqual(post.call_args.kwargs['headers']['Authorization'],'Bearer env-test-key')
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
            analyze(self.root,{**self.payload,'mode':'POST_MARKET'},{})
            self.assertEqual(post.call_count,2)
        for path in (self.root/'data'/'ai').glob('*.json'):
            self.assertNotIn('test-key',path.read_text(encoding='utf-8'))

    def test_failed_attempts_are_sanitized_and_suppress_automatic_rebilling(self):
        response = Mock(status_code=401)
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY':'secret-test-key'}, clear=True), \
                patch('deepseek_analysis.requests.post',return_value=response) as post:
            self.assertEqual(analyze(self.root,self.payload,{},force=True)['status'],'FAILED')
            response.status_code = 200
            response.json.return_value = {'choices':[{'finish_reason':'length','message':{'content':'partial'}}]}
            self.assertEqual(analyze(self.root,self.payload,{})['status'],'FAILED')
            post.side_effect = requests.Timeout('secret-test-key')
            result = analyze(self.root,self.payload,{},force=True)
            self.assertNotIn('secret-test-key',json.dumps(result))
            self.assertEqual(result['status'],'FAILED')
            self.assertTrue((self.root/'data'/'ai').exists())
            count=post.call_count
            self.assertEqual(analyze(self.root,self.payload,{})['status'],'FAILED')
            self.assertEqual(post.call_count,count)
            for path in (self.root/'data/ai').glob('*.json'):
                self.assertNotIn('secret-test-key',path.read_text(encoding='utf-8'))

    def test_process_interruption_blocks_automatic_rebilling_and_preserves_qualified_cache(self):
        from deepseek_analysis import finalize_validation
        response=Mock(status_code=200)
        response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}],
                                    'usage':{'total_tokens':10}}
        with patch('deepseek_analysis.load_api_key',return_value='test'), \
                patch('deepseek_analysis.requests.post',side_effect=KeyboardInterrupt) as post:
            with self.assertRaises(KeyboardInterrupt):analyze(self.root,self.payload,{},json_output=True)
            post.side_effect=None;post.return_value=response
            stopped=analyze(self.root,self.payload,{},json_output=True)
            self.assertEqual(stopped['status'],'FAILED');self.assertEqual(post.call_count,1)
            result=analyze(self.root,self.payload,{},json_output=True,force=True)
            finalize_validation(self.root,result,True)
            self.assertEqual(post.call_count,2)
            post.side_effect=KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):analyze(self.root,self.payload,{},json_output=True,force=True)
            self.assertEqual(analyze(self.root,self.payload,{},json_output=True)['business_status'],'PASSED')
            self.assertEqual(post.call_count,3)


if __name__ == '__main__':
    unittest.main()
