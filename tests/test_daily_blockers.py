import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from collector import Collector,CST
from enrichment import collect
from research_context import build_context
from storage import write_json
from test_market_radar import FakeFetcher,TARGET


class DailyFundTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.rows=[{'日期':'2026-04-09','主力净流入-净额':10,'source':'东方财富'},
                   {'日期':TARGET,'主力净流入-净额':-99,'source':'东方财富'}]
        self.params={'symbol':'测试行业','code':'BK1','target_date':TARGET}
        self.path=self.root/f'data/daily/{TARGET}/enrichment/flow-BK1.json'
        write_json(self.path,{'kind':'board_flow','params':self.params,'rows':self.rows,
            'observed_date':TARGET,'fetched_at':'2026-10-04T12:00:00+08:00'})

    def test_cached_history_selects_exact_target_not_first_row(self):
        fake=FakeFetcher()
        def fetch(kind,params):
            if kind=='fund_board_list':return [{'板块代码':'BK1','板块名称':'测试行业'}]
            if kind=='board_flow':raise AssertionError('valid dated cache should be reused')
            return fake(kind,params)
        c=Collector(self.root,fetch,clock=lambda:datetime(2026,10,5,18,tzinfo=CST))
        c.limit=10;c.retries=0;c.interval=0
        c.checkpoint['tasks'][f'daily:{TARGET}:board_flow:BK1']={'status':'SUCCESS'}
        result=collect(c,TARGET,[])
        self.assertEqual(result['flows'][0]['net_cny'],-99)
        self.assertTrue(result['flow_complete'])

    def test_proven_legacy_first_row_error_is_resolved_without_erasing_evidence(self):
        payload={'target_date':TARGET,'decision':{'funds':{'board_flows':[
            {'code':'BK1','name':'测试行业','date':TARGET,'net_cny':10,'source':'东方财富'}],
            'board_expected':1},'stocks':[]},'market':{},'enrichment':{}}
        before=copy.deepcopy(payload);raw=self.path.read_bytes()
        context=build_context(self.root,payload)
        self.assertEqual(context['funds']['boards'][0]['net_cny'],-99)
        correction=context['funds']['corrections'][0]
        self.assertEqual(correction['old_actual_date'],'2026-04-09')
        self.assertEqual(correction['old_net_cny'],10)
        self.assertEqual(correction['corrected_net_cny'],-99)
        self.assertEqual(context['funds']['coverage']['conflicted'],0)
        self.assertEqual(payload,before);self.assertEqual(self.path.read_bytes(),raw)

    def test_legacy_correction_cannot_hide_a_genuine_same_day_conflict(self):
        self.rows.append({'日期':TARGET,'主力净流入-净额':77,'source':'东方财富'})
        write_json(self.path,{'kind':'board_flow','params':self.params,'rows':self.rows})
        payload={'target_date':TARGET,'decision':{'funds':{'board_flows':[
            {'code':'BK1','name':'测试行业','date':TARGET,'net_cny':10}], 'board_expected':1}},'market':{}}
        self.assertIsNone(build_context(self.root,payload)['funds']['boards'][0]['net_cny'])


class CompactDailyTest(unittest.TestCase):
    def test_input_tokens_are_budgeted_before_calling_a_long_request(self):
        from deepseek_analysis import analyze
        with tempfile.TemporaryDirectory() as folder,patch('deepseek_analysis.load_api_key',return_value='test'), \
                patch('deepseek_analysis.requests.post') as post:
            result=analyze(Path(folder),{'target_date':TARGET,'text':'超长输入'*10000},
                {'max_tokens':8192,'max_total_tokens_per_day':20000})
            self.assertEqual(result['status'],'BUDGET_EXCEEDED')
            post.assert_not_called()

    def test_compaction_keeps_qualification_and_evidence_but_removes_repeated_raw_facts(self):
        from daily_ai import compact_request
        event={'id':'event:official','date':TARGET,'title':'货币政策通知','source':'央行',
               'url':'https://www.pbc.gov.cn/policy','excerpt':'正文'*900}
        request={'target_date':TARGET,'market':{'x':1},'candidates':[{'x':2}]*50,'candidate_rule':'按原机会类型选择研究对象',
            'decision_rules':{'market':{'score':None,'action':'证据不足'},'sectors':[], 'stocks':[]},
            'evidence':{'research:events':{'title':'样本','date':TARGET}},
            'facts':{'participation':{'stocks':[{'code':'000001','buy_allowed':False,'notices':[event]*10}]},
                'research:000001':{'code':'000001','buy_allowed':False,'notice_status':'SOURCE_FAILED',
                    'business':'主营业务','notices':[event]*10,'release_condition':'不得买入'},
                'research:events':{'original_items':[event],'supplemental_items':[]},
                'history:international':{'rows':[{'date':TARGET,'close':100}]*45}}}
        before=copy.deepcopy(request)
        result=compact_request(request)
        self.assertNotIn('market',result);self.assertNotIn('candidates',result)
        self.assertEqual(result['candidate_rule'],request['candidate_rule'])
        self.assertFalse(result['facts']['research:000001']['buy_allowed'])
        self.assertEqual(result['facts']['research:000001']['notice_status'],'SOURCE_FAILED')
        self.assertEqual(result['facts']['research:events']['original_items'][0]['url'],event['url'])
        self.assertLess(len(result['facts']['research:events']['original_items'][0]['excerpt']),len(event['excerpt']))
        self.assertNotIn('rows',result['facts']['history:international'])
        self.assertEqual(request,before)

    def test_program_conditions_are_expanded_without_a_model_copy(self):
        from daily_ai import expand_output,MODEL_SCHEMA
        original={'target_date':TARGET,'sectors':[{'code':'S'}],
            'stocks':[{'code':'000001','exit':{'condition':'原规则失效条件'}}]}
        participation={'score':33,'market_action':'等待确认','change_condition':'原65分条件',
            'stocks':[{'code':'000001','buy_allowed':False,'release_condition':'原股票条件'}]}
        opportunities={'sectors':[{'code':'S','confirmation_checks':[{'id':'c','label':'原确认'}],
            'invalidation_checks':[{'id':'i','label':'原失效'}]}]}
        data={'schema_version':MODEL_SCHEMA,'target_date':TARGET,
            'market':{'headline':'等待','judgment':'目前等待','risk':'反证','forecast':{}},
            'sectors':[{'code':'S','judgment':'观察方向','scenario_bias':'盘整','scenarios':{'盘整':'保持观察'},
                'priority_reason':'理由'}], 'stocks':[{'code':'000001','decision':'先不买','judgment':'证据不足'}],
            'focus':['S'],'event_research':[]}
        before=copy.deepcopy(data)
        result=expand_output(data,original,participation,opportunities)
        self.assertEqual(result['entry_brief']['decision'],'暂不入场')
        self.assertEqual(result['entry_brief']['change_condition'],'原65分条件')
        self.assertEqual(result['sectors'][0]['confirmation_checks'],['c'])
        self.assertEqual(result['stocks'][0]['confirmation'],'原股票条件')
        self.assertEqual(result['stocks'][0]['invalidation'],'原规则失效条件')
        self.assertEqual(result['entry_brief']['stocks'][0]['decision'],'先不买')
        self.assertEqual(data,before)
        malformed=copy.deepcopy(before);malformed['sectors'][0]['scenarios']=[]
        with self.assertRaises(ValueError):expand_output(malformed,original,participation,opportunities)
        data['sectors'][0]['confirmation']='AI伪造条件'
        with self.assertRaises(ValueError):expand_output(data,original,participation,opportunities)

    def test_supplemental_event_sample_retains_international_and_industry_categories(self):
        from daily_ai import compact_request
        events=[{'id':'event:'+str(i),'category':'国际' if i<8 else '产业与经济',
            'title':'样本','date':TARGET} for i in range(10)]
        request={'decision_rules':{'market':{},'sectors':[],'stocks':[]},
            'facts':{'research:events':{'original_items':[],'supplemental_items':events}},
            'evidence':{e['id']:{'date':TARGET} for e in events}}
        selected=compact_request(request)['facts']['research:events']['supplemental_items']
        self.assertTrue(any(e['id']=='event:8' for e in selected))
        self.assertLessEqual(len(selected),6)

    def test_compact_business_failure_stops_auto_billing_and_explicit_retry_recovers(self):
        import json
        from unittest.mock import Mock
        from daily_ai import MODEL_SCHEMA
        from decision import interpret
        from storage import read_json
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            decision={'target_date':TARGET,'market':{'score':75,'action':'可考虑参与','dimensions':[]},
                'sectors':[],'stocks':[{'code':'000001','name':'测试','state':'观察线索',
                    'industry':{'code':'801780'},'entry':{'condition':'原规则条件'}}]}
            payload={'target_date':TARGET,'decision':decision,
                'opportunities':{'selected_codes':[],'sectors':[]}}
            payload['research_context']=build_context(root,payload,include_updates=True)
            output={'schema_version':MODEL_SCHEMA,'target_date':TARGET,'market':{
                'stance':'等待确认','headline':'等待','judgment':'有限证据','risk':'样本有限','opportunity_bias':'等待条件',
                'fund_view':'资金不足','international_view':'国际不足','event_view':'事件不足',
                'changes':'无法比较','source_ids':['market']},'sectors':[],'stocks':[{
                'code':'000001','decision':'先不买','judgment':'公告源失败','risk':'未核验',
                'source_ids':['research:000001']}],'focus':[],'event_research':[]}
            from test_market_forecast import forecast_fixture
            output['market']['forecast']=forecast_fixture(TARGET)
            response=Mock(status_code=200)
            def answer(*args,**kwargs):
                submitted=json.loads(kwargs['json']['messages'][1]['content'])
                output['fact_claims']=submitted['fact_claims']
                response.json.return_value={'choices':[{'finish_reason':'stop','message':{
                    'content':json.dumps(output)}}],'usage':{'total_tokens':100}}
                return response
            def run(force=False):
                return interpret(root,payload,copy.deepcopy(decision),
                    {'market':{'title':'市场','date':TARGET,'facts':{}}},{},save=False,force=force)
            with patch('deepseek_analysis.load_api_key',return_value='test'), \
                    patch('deepseek_analysis.requests.post',side_effect=answer) as post:
                first=run();self.assertEqual(first['business_status'],'PASSED',first.get('validation_error'))
                self.assertTrue(run()['cached']);self.assertEqual(post.call_count,1)
                output['stocks'][0]['decision']='值得买入'
                rejected=run(True)
                self.assertEqual(rejected['business_status'],'FAILED')
                self.assertEqual(read_json(root/'data/ai'/f'{rejected["cache_key"]}.json')['status'],'FAILED')
                self.assertEqual(run()['status'],'FAILED');self.assertEqual(post.call_count,2)
                output['stocks'][0]['decision']='先不买'
                self.assertEqual(run(True)['business_status'],'PASSED')
                self.assertEqual(post.call_count,3)


class DailySourceTest(unittest.TestCase):
    def test_sz_margin_distinguishes_empty_response_and_checks_returned_date(self):
        from akshare_worker import fetch
        from unittest.mock import Mock
        response=Mock()
        report={'metadata':{'catalogid':'1837_xxpl','conditions':[
            {'name':'txtDate','defaultValue':TARGET}]},'data':[],'error':None}
        response.json.return_value=[report]
        with patch('requests.get',return_value=response):
            with self.assertRaisesRegex(ValueError,'SZSE_FETCHED_EMPTY'):
                fetch('margin_sz',{'target_date':TARGET})
            report['data']=[{'jrrzye':'1,200'}]
            self.assertEqual(fetch('margin_sz',{'target_date':TARGET})[0]['balance_cny'],1200)
            report['metadata']['conditions'][0]['defaultValue']='2026-09-01'
            with self.assertRaises(ValueError):fetch('margin_sz',{'target_date':TARGET})

    def test_us_assets_exclude_current_new_york_day_at_beijing_midnight(self):
        from research_context import collect_observations
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);calls=[]
            def fetch(kind,params):
                calls.append((kind,params))
                if kind=='news_feed':return [{'as_of':params['as_of'],'items':[{
                    'time':'2026-10-05 10:00:00','title':'有限消息','source':'新浪','url':'https://finance.sina.com.cn/7x24'}]}]
                return [{'date':'2026-10-02','close':100}]
            collector=Collector(root,fetch,clock=lambda:datetime(2026,10,6,0,30,tzinfo=CST))
            collector.limit=8;collector.retries=0;collector.interval=0
            collect_observations(collector,TARGET)
            assets={p['kind']:p['cutoff'] for kind,p in calls if kind=='international_observation'}
            self.assertEqual(assets['us'],'2026-10-04')
            self.assertEqual(assets['hk'],'2026-10-05')

    def test_treasury_uses_bounded_month_feed_and_exact_ten_year_field(self):
        from akshare_worker import fetch
        from unittest.mock import Mock
        xml=b'<feed xmlns:d="d"><properties><d:NEW_DATE>2026-10-02</d:NEW_DATE><d:BC_10YEAR>4.2</d:BC_10YEAR><d:BC_2YEAR>3.1</d:BC_2YEAR></properties></feed>'
        response=Mock(content=xml)
        with patch('requests.get',return_value=response) as get:
            rows=fetch('international_observation',{'symbol':'US10Y','kind':'yield','cutoff':'2026-10-02'})
            self.assertEqual(rows[0]['close'],4.2)
            self.assertIn('field_tdr_date_value_month',get.call_args.kwargs['params'])

    def test_fx_history_is_bounded_and_rejects_onshore_pair(self):
        from akshare_worker import fetch
        from unittest.mock import Mock
        response=Mock()
        response.json.return_value={'data':{'code':'USDCNH','market':133,'klines':['2026-10-02,7.1,7.2,7.3,7.0']}}
        params={'symbol':'USDCNH','kind':'fx','cutoff':'2026-10-02'}
        with patch('requests.get',return_value=response) as get:
            rows=fetch('international_observation',params)
            self.assertEqual(rows[0]['close'],7.2)
            self.assertLessEqual(int(get.call_args.kwargs['params']['lmt']),180)
            response.json.return_value['data']['code']='USDCNY'
            with self.assertRaises(ValueError):fetch('international_observation',params)


if __name__=='__main__':unittest.main()
