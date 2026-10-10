"""Isolated clock fixtures; these never create a real forward sample."""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from collector import CST, Collector
from predictions import archive, review
from research_context import build_context, collect_observations
from storage import read_json, write_json


BASE = '2026-09-30'


class ForwardForecastTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 8, 9, 10, tzinfo=CST)
        self.payload = {'target_date':BASE, 'mode':'PRE_MARKET', 'industries':[],
            'research_window':{'market_baseline_date':BASE,
                'external_information_cutoff':'2026-10-08T09:05:00+08:00'},
            'data_completeness':{'required':[{'name':'fixture core', 'ok':True}],
                'optional':{'international':'5/7', 'margin_sz':'FETCHED_EMPTY'}}}
        self.decision = {'version':'v2.1', 'candidate_version':'opportunities-v1',
            'market':{'action':'暂停判断'}, 'sectors':[], 'stocks':[],
            'ai':{'status':'READY', 'business_status':'PASSED', 'model':'fixture-model',
                'generated_at':'2026-10-08T09:09:00+08:00', 'request_id':'fixture-only',
                'interpretation':{}, 'parameters':{'model':'fixture-model','max_tokens':16384}}}

    def save(self, at=None):
        return archive(self.root, copy.deepcopy(self.payload), copy.deepcopy(self.decision), {}, at or self.now)

    def freeze(self, at=None, snapshot_id=None, day='2026-10-08'):
        from predictions import freeze_forward
        return freeze_forward(self.root, day, snapshot_id=snapshot_id, now=at or self.now)

    def test_every_run_is_separate_and_qualified_day_counts_only_once(self):
        first=self.save(self.now-timedelta(minutes=1)); second=self.save()
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['record_type'], 'RESEARCH_SNAPSHOT')
        self.assertEqual(second['record_type'], 'RESEARCH_SNAPSHOT')
        self.assertEqual(review(self.root, [], self.now)['forward_days'], 1)

    def test_latest_qualified_same_day_snapshot_freezes_once_with_versions(self):
        first=self.save(self.now-timedelta(minutes=1)); second=self.save()
        frozen=self.freeze(self.now+timedelta(seconds=5))
        self.assertEqual(frozen['record_type'], 'OFFICIAL_FORWARD_FORECAST')
        self.assertEqual(frozen['source_snapshot_id'], second['id'])
        self.assertEqual(frozen['prediction_date'], '2026-10-08')
        self.assertEqual(frozen['market_baseline_date'], BASE)
        self.assertEqual(frozen['external_information_cutoff'], self.payload['research_window']['external_information_cutoff'])
        self.assertEqual(frozen['issued_at'], self.now.isoformat())
        self.assertEqual(frozen['frozen_at'], self.now.isoformat())
        self.assertEqual(frozen['ai_model_version'], 'fixture-model')
        self.assertTrue(frozen['prompt_version']); self.assertTrue(frozen['prompt_sha256'])
        self.assertEqual(frozen['rule_version'], 'v2.1')
        path=self.root/'data/forward-forecasts'/f"2026-10-08-{frozen['id']}.json"
        original=path.read_bytes()
        self.payload['market']={'later':'changed'}; self.save(self.now+timedelta(minutes=10))
        self.assertEqual(self.freeze(self.now+timedelta(minutes=12))['id'], frozen['id'])
        self.freeze(self.now+timedelta(minutes=12),first['id'])
        self.assertEqual(path.read_bytes(), original)
        summary=review(self.root, [], self.now+timedelta(hours=1))
        self.assertEqual(summary['forward_days'], 1)
        self.assertEqual(summary['official_forward_dates'], ['2026-10-08'])
        outcomes=next(r for r in summary['records'] if r['id']==frozen['id'])['outcomes']
        self.assertEqual([r['date'] for r in outcomes], ['2026-10-08','2026-10-12','2026-10-14'])
        self.assertEqual([r['status'] for r in outcomes], ['PENDING']*3)

    def test_evening_snapshot_with_future_model_and_information_cannot_qualify(self):
        evening=datetime(2026,10,7,22,tzinfo=CST)
        self.save(evening)
        with self.assertRaises(ValueError):self.freeze(evening)
        with self.assertRaises(ValueError):self.freeze()
        self.assertEqual(review(self.root, [], evening)['forward_days'], 0)

    def test_freezing_requires_real_completion_before_auction_no_backdating(self):
        for at in (datetime(2026,10,8,9,15,tzinfo=CST),datetime(2026,10,8,9,30,tzinfo=CST),
                   datetime(2026,10,6,9,tzinfo=CST)):
            self.save(at)
            with self.subTest(at=at), self.assertRaises(ValueError):self.freeze(at)
        self.assertFalse(list((self.root/'data/forward-forecasts').glob('*.json')))

    def test_failed_ai_future_information_and_wrong_baseline_cannot_freeze(self):
        for defect in ('ai','cutoff','baseline','core','model_time'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                original_root=self.root; self.root=Path(folder)
                original_payload=copy.deepcopy(self.payload); original_decision=copy.deepcopy(self.decision)
                if defect=='ai':self.decision['ai']['business_status']='FAILED'
                if defect=='cutoff':self.payload['research_window']['external_information_cutoff']='2026-10-08T09:20:00+08:00'
                if defect=='baseline':self.payload['target_date']='2026-09-29'
                if defect=='core':self.payload['data_completeness']['required'][0]['ok']=False
                if defect=='model_time':self.decision['ai']['generated_at']='2026-10-08T09:20:00+08:00'
                self.save()
                with self.assertRaises(ValueError):self.freeze()
                self.root=original_root; self.payload=original_payload; self.decision=original_decision

    def test_manual_same_day_observations_refresh_news_and_use_completed_us_session(self):
        calls=[]; at=[datetime(2026,10,8,8,tzinfo=CST)]
        def fetch(kind,params):
            calls.append((kind,copy.deepcopy(params)))
            if kind=='news_feed':return [{'as_of':params['as_of'],'items':[{
                'time':at[0].isoformat(),'title':'fixture news','source':'fixture', 'url':'https://example.test/news'}]}]
            if kind=='policy':return [{'date':params['target_date'],'items':[{
                'date':'2026-10-07','time':'2026-10-07','title':'fixture policy',
                'source':'中国人民银行官网','url':'https://www.pbc.gov.cn/fixture','excerpt':'fixture body'}]}]
            if kind=='margin_sz':return [{'date':params['target_date'],'balance_cny':100}]
            return [{'date':params.get('cutoff',BASE),'close':100,'source':'fixture'}]
        c=Collector(self.root,fetch,clock=lambda:at[0]); c.limit=40; c.retries=0; c.interval=0
        collect_observations(c,BASE)
        at[0]=datetime(2026,10,8,9,5,tzinfo=CST)
        collect_observations(c,BASE)
        news=[p for kind,p in calls if kind=='news_feed']
        self.assertEqual(len(news),2)
        self.assertEqual(news[-1]['as_of'],at[0].isoformat())
        assets={p['kind']:p['cutoff'] for kind,p in calls if kind=='international_observation'}
        self.assertEqual(assets['us'],'2026-10-07')
        self.assertEqual(assets['hk'],'2026-10-07')
        self.assertTrue(any(kind=='policy' for kind,p in calls))

    def test_context_excludes_post_cutoff_news_and_future_cached_sources(self):
        folder=self.root/'data/daily'/BASE/'observations'
        write_json(folder/'summary.json',{'as_of':'2026-10-08T09:05:00+08:00','cutoff':'2026-10-07'})
        write_json(folder/'news.json',{'fetched_at':'2026-10-08T09:04:00+08:00','rows':[{
            'as_of':'2026-10-08T10:00:00+08:00','items':[
                {'time':'2026-10-08T09:00:00+08:00','title':'known fixture','source':'fixture','url':'https://example.test/known'},
                {'time':'2026-10-08T09:40:00+08:00','title':'future fixture','source':'fixture','url':'https://example.test/future'}]}]})
        write_json(folder/'NASDAQ.json',{'fetched_at':'2026-10-08T10:00:00+08:00',
            'rows':[{'date':'2026-10-07','close':999,'source':'future fixture'}]})
        context=build_context(self.root,self.payload,include_updates=True)
        self.assertEqual([r['title'] for r in context['events']['supplemental_items']],['known fixture'])
        row=next(r for r in context['international'] if r['code']=='NASDAQ')
        self.assertIsNone(row['value'])

    def test_existing_notice_source_queries_the_public_window_without_risk_qualification_upgrade(self):
        from unittest.mock import Mock
        from enrichment import fetch_events, event_validator
        from daily_ai import compact_request
        response=Mock()
        response.json.return_value={'data':{'list':[{'notice_date':'2026-10-07',
            'title':'fixture company notice','codes':[{'stock_code':'600000'}],'art_code':'fixture'}]}}
        params={'code':'600000','start_date':BASE,'target_date':'2026-10-08','as_of':self.now.isoformat()}
        with patch('enrichment.requests.get',return_value=response) as get:
            rows=fetch_events('candidate_notices',params)
            self.assertEqual(get.call_args.kwargs['params']['begin_time'],BASE)
            self.assertEqual(get.call_args.kwargs['params']['end_time'],'2026-10-08')
        event_validator('2026-10-08',start_date=BASE,as_of=self.now.isoformat())(rows)
        rows[0]['items'][0]['time']='2026-10-08T09:40:00+08:00'
        with self.assertRaises(ValueError):event_validator('2026-10-08',start_date=BASE,as_of=self.now.isoformat())(rows)
        request={'target_date':BASE,'decision_rules':{'sectors':[],'stocks':[],'market':{}},
            'research_window':self.payload['research_window'],'evidence':{},'facts':{'research:600000':{
                'buy_allowed':False,'notice_status':'SOURCE_FAILED','current_notices':{'status':'TITLE_SAMPLE','items':rows[0]['items']}}}}
        compact=compact_request(request)
        self.assertFalse(compact['facts']['research:600000']['buy_allowed'])
        self.assertEqual(compact['facts']['research:600000']['notice_status'],'SOURCE_FAILED')
        self.assertIn('current_notices',compact['facts']['research:600000'])

    def test_freeze_never_calls_model_and_legacy_auto_forward_does_not_count(self):
        write_json(self.root/'data/predictions/legacy.json',{'id':'legacy','target_date':BASE,
            'issued_at':self.now.isoformat(),'mode':'FORWARD','version':'v2.1',
            'decision':{'market':{'action':'暂停判断'},'sectors':[],'stocks':[]},'baseline':{'codes':[]},
            'review_definition':{'fee_percent':.15,'slippage_percent':.1}})
        self.assertEqual(review(self.root,[],self.now)['forward_days'],0)
        with patch('decision.analyze',side_effect=AssertionError('freezing must not bill')):
            self.save()
            frozen=self.freeze()
        path=self.root/'data/forward-forecasts'/f"2026-10-08-{frozen['id']}.json"
        before=path.read_bytes()
        review(self.root,[],datetime(2026,10,8,18,tzinfo=CST))
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(read_json(self.root/'data/reviews/summary.json')['forward_days'],1)

    def test_main_ai_receives_the_current_research_cutoff(self):
        from test_product import ProductTest
        from decision import evidence, interpret
        fixture=ProductTest(); fixture.setUp()
        try:
            payload,history,decision=fixture.decision()
            payload['research_window']=self.payload['research_window']
            refs=evidence(payload,decision,history)
            with patch('decision.analyze',return_value={'status':'DISABLED','message':'fixture'}) as model:
                interpret(fixture.root,payload,decision,refs,None)
            self.assertEqual(model.call_args.args[1]['research_window'],self.payload['research_window'])
        finally:
            fixture.temp.cleanup()

    def test_evening_snapshot_keeps_today_completed_hong_kong_rows(self):
        folder=self.root/'data/daily'/BASE/'observations'
        write_json(folder/'summary.json',{'as_of':'2026-10-07T22:00:00+08:00','cutoff':'2026-10-06'})
        write_json(folder/'HSI.json',{'params':{'cutoff':'2026-10-07'},
            'rows':[{'date':'2026-10-07','close':100,'source':'fixture'}]})
        payload=copy.deepcopy(self.payload)
        payload['research_window']['external_information_cutoff']='2026-10-07T22:00:00+08:00'
        context=build_context(self.root,payload,include_updates=True)
        row=next(r for r in context['international'] if r['code']=='HSI')
        self.assertEqual(row['date'],'2026-10-07')

    def test_legacy_news_receipt_does_not_change_when_only_collection_status_is_newer(self):
        folder=self.root/'data/daily'/BASE/'observations'
        write_json(folder/'summary.json',{'as_of':'2026-10-08T09:12:00+08:00','cutoff':'2026-10-07'})
        write_json(folder/'news.json',{'rows':[{'as_of':'2026-10-08T09:00:00+08:00','items':[
            {'time':'2026-10-08T08:59:00+08:00','title':'known fixture','source':'fixture','url':'https://example.test/known'}]}]})
        payload=copy.deepcopy(self.payload);payload.pop('research_window')
        context=build_context(self.root,payload,include_updates=True)
        self.assertEqual(context['events']['as_of'],'2026-10-08T09:00:00+08:00')
        self.assertEqual(context['events']['supplemental_items'][0]['known_at'],'2026-10-08T09:00:00+08:00')


if __name__=='__main__':unittest.main()
