import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from decision import validate_ai, interpret
from deepseek_analysis import analyze, finalize_validation
from participation import snapshot
from refresh_ai_review import refresh_review
from storage import write_json
from datetime import datetime
from collector import CST
from predictions import evaluate, review, archive_ai_research


class EvidenceReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = '2026-09-01'
        self.folder = self.root/'data/daily'/self.target/'history'
        self.payload = {'target_date':self.target, 'decision':{
            'market':{'score':75, 'action':'可考虑参与'},
            'stocks':[{'code':'600000','state':'观察线索','industry':{'code':'801780'},
                'history':{'samples':161,'close':11,'ma20':10},'entry':{'condition':'原条件'}}]},
            'opportunities':{'selected_codes':[], 'sectors':[{'code':'801780',
                'confirmation_checks':[{'met':True}]}]}}
        write_json(self.folder/'stock_info-600000.json', {'rows':[{
            'date':self.target,'facts':{'主营业务':'银行'}}]})

    def test_announcement_failure_empty_titles_and_body_are_distinct(self):
        stock = snapshot(self.root,self.payload)['stocks'][0]
        self.assertFalse(stock['buy_allowed'])
        self.assertEqual(stock['notice_status'],'SOURCE_FAILED')
        for items, expected in [([], 'FETCHED_EMPTY'), ([{'title':'公告'}], 'TITLE_SAMPLE')]:
            write_json(self.folder/'candidate_notices-600000.json', {'rows':[{
                'date':self.target,'items':items}]})
            stock = snapshot(self.root,self.payload)['stocks'][0]
            self.assertEqual(stock['notice_status'],expected)
            self.assertFalse(stock['full_risk_verified'])
        write_json(self.folder/'candidate_notices-600000.json', {'rows':[{
            'date':self.target,'items':[{'title':'公告','body_verified':True,
                'url':'https://www.sse.com.cn/test','body':'正文'}]}]})
        stock = snapshot(self.root,self.payload)['stocks'][0]
        self.assertEqual(stock['notice_status'],'BODY_VERIFIED_SAMPLE')
        self.assertFalse(stock['full_risk_verified'])

    def test_evidence_change_invalidates_supplement_cache(self):
        write_json(self.root/'data/publications'/f'{self.target}.json',{'analysis':self.payload})
        write_json(self.root/'config.json',{'deepseek':{}})
        with patch('refresh_ai_review.interpret',return_value={'status':'READY','business_status':'PASSED',
            'generated_at':'2026-10-06T12:00:00+08:00','request_id':'test','interpretation':{}}) as model:
            refresh_review(self.root,self.target)
            self.assertTrue(refresh_review(self.root,self.target)['cached'])
            write_json(self.folder/'stock_info-600000.json',{'rows':[]})
            refresh_review(self.root,self.target)
            self.assertEqual(model.call_count,2)

    def test_raw_json_success_is_not_ready_business_cache(self):
        response=Mock(status_code=200)
        response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
        with patch('deepseek_analysis.load_api_key',return_value='test'), \
                patch('deepseek_analysis.requests.post',return_value=response) as post:
            first=analyze(self.root,{'target_date':self.target},{},json_output=True)
            self.assertEqual(first['transport_status'],'SUCCEEDED')
            second=analyze(self.root,{'target_date':self.target},{},json_output=True)
            self.assertEqual(second['status'],'FAILED')
            self.assertEqual(post.call_count,1)
            analyze(self.root,{'target_date':self.target},{},json_output=True,force=True)
            self.assertEqual(post.call_count,2)

    def test_commercial_policy_title_cannot_complete_policy_score(self):
        data={'target_date':self.target,'summary':'摘要','counterevidence':'反证',
            'policy_assessment':{'score':100,'reason':'政策','source_ids':['events']},
            'market':{'judgment':'判断','source_ids':['events']},'sectors':[],'stocks':[]}
        refs={'events':{'facts':{'events':[{'title':'政策主题商业研究',
            'source':'商业媒体','date':self.target}]}}}
        decision={'target_date':self.target,'sectors':[],'stocks':[]}
        with self.assertRaises(ValueError): validate_ai(json.dumps(data),decision,refs)
        with self.assertRaises(ValueError): validate_ai('[]',decision,refs)

    def test_policy_score_accepts_bound_v3_event_references_without_mutation(self):
        url='https://www.pbc.gov.cn/policy.html'
        event={'title':'货币政策通知','date':self.target,'url':url,'excerpt':'官方政策正文节选'}
        decision={'target_date':self.target,'sectors':[],'stocks':[]}
        data={'target_date':self.target,'summary':'摘要','counterevidence':'反证',
            'market':{'judgment':'等待','source_ids':['market']},'sectors':[],'stocks':[],
            'policy_assessment':{'score':55,'reason':'有限官方正文','source_ids':[],
                'evidence_urls':[url]}}
        refs={'market':{'facts':{}},'event:official':{'facts':event},
            'research:events':{'facts':{'original_items':[event],'supplemental_items':[]}}}
        before=copy.deepcopy((decision,refs))
        for key in ('event:official','research:events'):
            with self.subTest(reference=key):
                data['policy_assessment']['source_ids']=[key]
                self.assertEqual(validate_ai(json.dumps(data),decision,refs)['policy_assessment']['score'],55)
        self.assertEqual((decision,refs),before)

    def test_v3_policy_references_still_reject_titles_media_late_and_unbound_urls(self):
        url='https://www.pbc.gov.cn/policy.html'
        event={'title':'货币政策通知','date':self.target,'url':url,'excerpt':'官方政策正文节选'}
        decision={'target_date':self.target,'sectors':[],'stocks':[]}
        data={'target_date':self.target,'summary':'摘要','counterevidence':'反证',
            'market':{'judgment':'等待','source_ids':['market']},'sectors':[],'stocks':[],
            'policy_assessment':{'score':55,'reason':'政策','source_ids':['event:sample'],
                'evidence_urls':[url]}}
        for changed in ({'excerpt':''},{'url':'https://finance.sina.com.cn/news'},
                {'date':'2026-10-05'},{'url':'https://www.pbc.gov.cn/other.html'}):
            with self.subTest(evidence=changed):
                refs={'market':{'facts':{}},'event:sample':{'facts':{**event,**changed}}}
                with self.assertRaises(ValueError):validate_ai(json.dumps(data),decision,refs)
        data['policy_assessment']['source_ids']=['event:official','event:media']
        refs={'market':{'facts':{}},'event:official':{'facts':event},
            'event:media':{'facts':{**event,'url':'https://finance.sina.com.cn/news'}}}
        with self.assertRaises(ValueError):validate_ai(json.dumps(data),decision,refs)

    def test_business_rejection_is_persisted_and_retry_can_recover(self):
        response=Mock(status_code=200)
        response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}],
                                    'usage':{'total_tokens':10}}
        with patch('deepseek_analysis.load_api_key',return_value='test'), \
                patch('deepseek_analysis.requests.post',return_value=response) as post:
            first=analyze(self.root,{'target_date':self.target},{},json_output=True)
            finalize_validation(self.root,first,False,'业务校验失败')
            self.assertEqual(analyze(self.root,{'target_date':self.target},{},json_output=True)['status'],'FAILED')
            second=analyze(self.root,{'target_date':self.target},{},json_output=True,force=True)
            finalize_validation(self.root,second,True)
            cached=analyze(self.root,{'target_date':self.target},{},json_output=True)
            self.assertEqual(cached['business_status'],'PASSED')
            self.assertEqual(post.call_count,2)

    def test_budget_prevents_another_paid_call(self):
        response=Mock(status_code=200)
        response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'ok'}}]}
        settings={'max_requests_per_day':1,'max_total_tokens_per_day':65536}
        with patch('deepseek_analysis.load_api_key',return_value='test'), \
                patch('deepseek_analysis.requests.post',return_value=response) as post:
            self.assertEqual(analyze(self.root,{'target_date':self.target},settings)['status'],'READY')
            self.assertEqual(analyze(self.root,{'target_date':self.target,'new':1},settings)['status'],'BUDGET_EXCEEDED')
            self.assertEqual(post.call_count,1)

    def test_slow_model_response_can_complete_without_automatic_retry(self):
        import requests
        response=Mock(status_code=200)
        response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'ok'}}],
                                    'usage':{'total_tokens':10}}
        def slow_response(*args,**kwargs):
            # The complete paid launch response took about 281 seconds.
            if kwargs['timeout'][1] < 281:raise requests.ReadTimeout()
            return response
        with patch('deepseek_analysis.load_api_key',return_value='test'), \
                patch('deepseek_analysis.requests.post',side_effect=slow_response) as post:
            first=analyze(self.root,{'target_date':self.target},{})
            self.assertEqual(first['status'],'READY')
            cached=analyze(self.root,{'target_date':self.target},{})
            self.assertTrue(cached['cached'])
            self.assertEqual(post.call_count,1)

    def test_corrupt_evidence_fails_closed_without_hiding_snapshot(self):
        (self.folder/'candidate_notices-600000.json').write_text('{bad',encoding='utf-8')
        stock=snapshot(self.root,self.payload)['stocks'][0]
        self.assertFalse(stock['buy_allowed'])
        self.assertEqual(stock['notice_status'],'SOURCE_FAILED')

    def test_real_interpretation_validator_rejects_raw_cache_and_no_auto_rebill(self):
        decision={'target_date':self.target,'market':{'action':'等待确认','score':None,'dimensions':[]},
                  'sectors':[],'stocks':[]}
        payload={'target_date':self.target}
        response=Mock(status_code=200)
        response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
        with patch('deepseek_analysis.load_api_key',return_value='test'), \
                patch('deepseek_analysis.requests.post',return_value=response) as post:
            first=interpret(self.root,payload,copy.deepcopy(decision),{},{},save=False)
            self.assertEqual(first['business_status'],'FAILED')
            second=interpret(self.root,payload,copy.deepcopy(decision),{},{},save=False)
            self.assertEqual(second['status'],'FAILED')
            self.assertEqual(post.call_count,1)
            interpret(self.root,payload,copy.deepcopy(decision),{},{},save=False,force=True)
            self.assertEqual(post.call_count,2)


if __name__ == '__main__':
    unittest.main()


class ReviewReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.record={'id':'isolated','target_date':'2026-09-01','mode':'REPLAY_OR_LATE',
            'version':'test','issued_at':'2026-09-01T18:00:00+08:00',
            'decision':{'sectors':[],'stocks':[],'market':{'action':'等待确认'}},
            'baseline':{'codes':[]},'review_definition':{'fee_percent':.15,'slippage_percent':.10}}
        self.days=['2026-09-01','2026-09-02','2026-09-03','2026-09-04','2026-09-07','2026-09-08']
        self.bench=[{'date':d,'close':c} for d,c in zip(self.days,[100,110,105,90,99,120])]
        self.now=datetime(2026,9,8,18,tzinfo=CST)

    def test_max_drawdown_uses_peak_then_later_trough(self):
        outcomes=evaluate(self.root,self.record,self.bench,self.now)['outcomes']
        self.assertEqual(outcomes[0]['market_result']['max_drawdown_percent'],0)
        for row in outcomes[1:]:
            self.assertAlmostEqual(row['market_result']['max_drawdown_percent'],(90/110-1)*100)

    def test_mature_missing_and_pending_are_distinct(self):
        empty=evaluate(self.root,self.record,[],self.now)['outcomes']
        self.assertEqual([r['status'] for r in empty],['MISSING_DATA']*3)
        early=evaluate(self.root,self.record,[],datetime(2026,9,1,18,tzinfo=CST))['outcomes']
        self.assertEqual([r['status'] for r in early],['PENDING']*3)

    def test_completed_review_survives_rolling_window(self):
        write_json(self.root/'data/predictions/test.json',self.record)
        first=review(self.root,self.bench,self.now)['records'][0]
        later=review(self.root,[{'date':'2026-10-01','close':125}],datetime(2026,10,1,18,tzinfo=CST))['records'][0]
        self.assertEqual(first['outcomes'],later['outcomes'])

    def test_missing_actual_next_day_never_fills_at_later_open(self):
        self.record['decision']['stocks']=[{'code':'600000','state':'可考虑参与',
            'entry':{'low':9,'high':11},'corporate_actions_verified':True,'risk_verified':True}]
        write_json(self.root/'data/daily/2026-09-04/history/candidate_history-600000.json',{'rows':[
            {'date':'2026-09-03','open':10,'high':11,'low':9,'close':10,'volume':1000},
            {'date':'2026-09-04','open':10,'high':11,'low':9,'close':10.5,'volume':1000}]})
        row=evaluate(self.root,self.record,self.bench,self.now)['outcomes'][1]['stocks'][0]
        self.assertEqual(row['status'],'MISSING_DATA')
        self.assertNotIn('net_percent',row)

    def test_ai_buy_is_research_object_without_simulated_trade(self):
        self.record['decision']['stocks']=[{'code':'600000','state':'观察线索','entry':{}}]
        self.record['decision']['ai']={'generated_at':self.record['issued_at'],'interpretation':{'entry_brief':{
            'stocks':[{'code':'600000','decision':'值得买入'}]}}}
        write_json(self.root/'data/daily/2026-09-04/history/candidate_history-600000.json',{'rows':[
            {'date':d,'close':c} for d,c in zip(self.days,[10,11,10,10.5,11,12])]})
        result=evaluate(self.root,self.record,self.bench,self.now)['outcomes'][1]
        self.assertEqual(result['stocks'][0]['status'],'ABSTAIN')
        self.assertEqual(result['ai_research'][0]['status'],'EVALUATED')
        self.assertEqual(result['denominators']['simulated_trades'],0)
        self.assertEqual(result['denominators']['ai_research'],1)

    def test_conflicting_next_open_suspension_and_bad_fields_never_trade(self):
        self.record['decision']['stocks']=[{'code':'600000','state':'可考虑参与',
            'entry':{'low':9,'high':11},'corporate_actions_verified':True,'risk_verified':True}]
        path=self.root/'data/daily/2026-09-02/history/candidate_history-600000.json'
        first={'date':'2026-09-02','open':10,'high':11,'low':9,'close':10,'volume':1000}
        end={**first,'date':'2026-09-04','close':10.5}
        for mutate, expected in [({'volume':0},'UNEXECUTABLE'),({'open':None},'UNRESOLVED'),
                                  ({'high':10,'low':10},'UNEXECUTABLE')]:
            write_json(path,{'rows':[{**first,**mutate},end]})
            row=evaluate(self.root,self.record,self.bench,self.now)['outcomes'][1]['stocks'][0]
            self.assertEqual(row['status'],expected)
            self.assertNotIn('net_percent',row)
        write_json(path,{'rows':[first,end]})
        write_json(self.root/'data/daily/2026-09-03/history/candidate_history-600000.json',
            {'rows':[{**first,'open':10.1},end]})
        row=evaluate(self.root,self.record,self.bench,self.now)['outcomes'][1]['stocks'][0]
        self.assertEqual(row['status'],'UNRESOLVED')

    def test_later_ai_opinion_never_becomes_original_forecast(self):
        self.record['decision']['ai']={'generated_at':'2026-09-01T20:00:00+08:00',
            'interpretation':{'entry_brief':{'stocks':[{'code':'600000','decision':'值得买入'}]}}}
        result=evaluate(self.root,self.record,self.bench,self.now)['outcomes'][1]
        self.assertEqual(result['ai_research'][0]['status'],'NOT_AT_SIGNAL')
        self.assertEqual(result['denominators']['ai_research'],0)

    def test_daily_index_timestamps_are_normalized_when_recovering_old_review(self):
        write_json(self.root/'data/daily/2026-09-08/index-sh000300.json',
            {'rows':[{**r,'date':r['date']+'T00:00:00.000'} for r in self.bench]})
        result=evaluate(self.root,self.record,[],self.now)
        self.assertEqual([o['status'] for o in result['outcomes']],['EVALUATED']*3)

    def test_rule_order_without_verified_risk_cannot_generate_a_trade(self):
        self.record['decision']['stocks']=[{'code':'600000','state':'可考虑参与',
            'entry':{'low':9,'high':11},'corporate_actions_verified':True}]
        write_json(self.root/'data/daily/2026-09-02/history/candidate_history-600000.json',{'rows':[
            {'date':'2026-09-02','open':10,'high':11,'low':9,'close':10,'volume':1000},
            {'date':'2026-09-04','open':10,'high':11,'low':9,'close':10.5,'volume':1000}]})
        row=evaluate(self.root,self.record,self.bench,self.now)['outcomes'][1]['stocks'][0]
        self.assertEqual(row['status'],'ABSTAIN')

    def test_supplemental_research_uses_actual_issue_time_and_never_creates_trade(self):
        result={'status':'READY','business_status':'PASSED','generated_at':'2026-09-03T18:00:00+08:00',
            'request_id':'isolated','interpretation':{'entry_brief':{'stocks':[
                {'code':'600000','decision':'值得买入'}]}}}
        archive_ai_research(self.root,{'target_date':'2026-09-01'},result)
        write_json(self.root/'data/daily/2026-09-08/history/candidate_history-600000.json',{'rows':[
            {'date':d,'close':c} for d,c in zip(self.days,[10,11,10,10.5,11,12])]})
        summary=review(self.root,self.bench,self.now)
        self.assertEqual(summary['forward_days'],0)
        research=summary['ai_research_reviews'][0]
        self.assertEqual(research['reference_date'],'2026-09-03')
        self.assertEqual(research['evidence_date'],'2026-09-01')
        self.assertEqual(research['outcomes'][0]['date'],'2026-09-04')
        self.assertEqual(research['outcomes'][0]['ai_research'][0]['status'],'EVALUATED')
        self.assertEqual(research['outcomes'][0]['denominators']['simulated_trades'],0)
