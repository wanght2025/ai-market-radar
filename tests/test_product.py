import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from collector import membership_signature
from decision import build, duration, evidence, interpret, validate_ai
from enrichment import fetch_board_flow, fetch_concept_catalog, fetch_concept_members, fetch_fund_catalog
from history import bars, comparable, shortlist
from market_radar import Radar
from predictions import archive, evaluate, stock_outcome
from scheduler import CST, due_slots, is_trading_day, tick
from storage import read_json, write_json
from test_market_radar import FakeFetcher, TARGET


def series(end=TARGET, count=45):
    final = datetime.fromisoformat(end).date()
    return [{'date':(final-timedelta(days=count-i-1)).isoformat(),'close':100+i*.1,
             'open':100+i*.1,'high':101+i*.1,'low':99+i*.1,'volume':1000,'amount':1e8,
             'source':'isolated test fixture'} for i in range(count)]


def payload():
    return {'target_date':TARGET, 'market':{'breadth_percent':65,'equal_weight_change_percent':1.2},
        'industries':[{'code':'BK0001','name':'测试行业','rank':1,'relative':1,'absolute_change_percent':2,
                       'breadth':70,'rank_improvement':4}], 'candidates':[], 'enrichment':{}, 'snapshots':{}}


class ProductTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.now=datetime(2026,9,24,18,tzinfo=CST)

    def decision(self):
        p=payload()
        h={'industries':{'BK0001':{'rows':series()}},'stocks':{},'gaps':[]}
        return p,h,build(p,h,[],{k:series() for k in ('sh000001','sz399001','sh000300')},self.now)

    def test_concept_catalog_uses_distinct_source_codes(self):
        from unittest.mock import Mock
        response=Mock(text='var S_Finance_bankuai_class = {"gn_test":"gn_test,测试,100,1,2,3,4,5,6,7,8,9,10"}')
        with patch('enrichment.requests.get',return_value=response):
            self.assertEqual(fetch_concept_catalog(),[{'板块代码':'sina-gn_test','板块名称':'测试'}])
            response.text='var other = {}'
            with self.assertRaises(ValueError):fetch_concept_catalog()

    def test_concept_members_requires_complete_stable_pages(self):
        from unittest.mock import Mock
        members=[{'code':f'{i:06d}','name':'测试','symbol':f'sz{i:06d}'} for i in range(81)]
        reverse=list(reversed(members))
        valid=('81',members[:80],members[80:],[],reverse[:80],reverse[80:],[],'81')
        def session(values):
            responses=[Mock(url='https://example.test/members',json=Mock(return_value=value)) for value in values]
            return Mock(get=Mock(side_effect=responses))
        client=session(valid)
        with patch('enrichment.requests.Session',return_value=client):
            result=fetch_concept_members('gn_test')[0]
            self.assertEqual(len(result['raw_rows']),81)
            self.assertTrue(result['collection']['complete'])
            self.assertEqual(result['collection']['forward_pages'][-1]['size'],0)
        client.close.assert_called_once_with()
        for values in (('invalid',), ('81',members[:79],members[79:]),
                       ('81',members[:80],members[:1],[]),
                       (*valid[:-1],'82')):
            client=session(values)
            with patch('enrichment.requests.Session',return_value=client):
                with self.assertRaises(ValueError):fetch_concept_members('gn_test')
            client.close.assert_called_once_with()

    def test_fund_catalog_fallback_excludes_other_classifications(self):
        from unittest.mock import Mock
        import requests
        html='<div class="bkselectorpop"><ul class="pop_slider"><li>行业</li><li>概念</li><li>地域</li></ul>'
        html+=''.join(f'<div class="pop-cont"><a href="/bkzj/BK{i:04d}.html">名称{i}</a></div>' for i in range(3))+'</div>'
        response=Mock(text=html)
        with patch('enrichment.requests.get',side_effect=[requests.ConnectionError('unavailable'),response]):
            self.assertEqual(fetch_fund_catalog(),[{'板块代码':'BK0000','板块名称':'名称0'}])
        response.text=html.replace('行业','概念')
        with patch('enrichment.requests.get',side_effect=[requests.ConnectionError('unavailable'),response]):
            with self.assertRaises(ValueError):fetch_fund_catalog()

    def test_board_flow_rejects_wrong_identity_duplicate_dates_and_nonfinite(self):
        from unittest.mock import Mock
        data={'code':'BK0001','name':'行业','market':90,'klines':[TARGET+',123'+',0'*13]}
        response=Mock(json=Mock(return_value={'data':data}))
        params={'code':'BK0001','symbol':'行业'}
        with patch('enrichment.requests.get',return_value=response):
            self.assertEqual(fetch_board_flow(params)[0]['主力净流入-净额'],123)
            for change in ({'code':'BK0002'},{'klines':data['klines']*2},
                           {'klines':[TARGET+',nan'+',0'*13]}, {'klines':[TARGET+',123']}):
                response.json.return_value={'data':{**data,**change}}
                with self.assertRaises(ValueError):fetch_board_flow(params)

    def test_history_rejects_duplicates_and_excludes_future(self):
        rows=series()+[{'date':'2026-09-25','close':999}]
        self.assertEqual(bars(rows,TARGET)[0][-1]['close'],104.4)
        with self.assertRaises(ValueError):bars(series()+[series()[0]],TARGET)
        with self.assertRaises(ValueError):bars([{'date':TARGET,'close':float('nan')}],TARGET)

    def test_classification_refresh_compares_content_not_generation(self):
        boards=[{'code':'B','name':'行业'}]
        for generation in ('one','two'):
            path=self.root/'snapshots/industry-sw'/generation
            write_json(path/'members/B.json',{'rows':[{'code':'000001'}]})
            write_json(path/'manifest.json',{'generation':generation,'boards':boards})
        current={'generation':'two','boards':boards}
        old={'industry_generation':'one'}
        self.assertTrue(comparable(self.root,old,current))
        write_json(self.root/'snapshots/industry-sw/two/members/B.json',{'rows':[{'code':'600000'}]})
        self.assertFalse(comparable(self.root,old,current))

    def test_quiet_liquid_stock_can_enter_research(self):
        quotes={'000001':{'代码':'000001','名称':'银行','涨跌幅':.1,'成交额':2e8}}
        result=shortlist(quotes,[{'code':'B'}],{'000001':{'code':'B','name':'银行'}})
        self.assertEqual(result[0]['代码'],'000001')

    def test_missing_dimensions_never_become_full_score(self):
        p,h,d=self.decision()
        self.assertIsNone(d['market']['score'])
        self.assertEqual(d['market']['coverage_percent'],50)
        self.assertEqual(d['market']['action'],'等待确认')

    def test_duration_does_not_count_unfinished_episode(self):
        self.assertEqual(duration(series(),TARGET)['sample_count'],0)
        self.assertIsNone(duration(series(),TARGET)['range_days'])

    def test_ai_rejects_unknown_refs_codes_dates_or_missing_candidates(self):
        p,h,d=self.decision(); refs=evidence(p,d,h)
        valid={'target_date':TARGET,'summary':'有条件观察','counterevidence':['风险未核验'],
               'market':{'judgment':'等待','source_ids':['market']},
               'sectors':[{'code':'BK0001','judgment':'观察','source_ids':['industry:BK0001']}],'stocks':[]}
        self.assertEqual(validate_ai(json.dumps(valid),d,refs)['counterevidence'],'风险未核验')
        for mutation in ('date','refs','code','missing'):
            item=copy.deepcopy(valid)
            if mutation=='date':item['target_date']='2026-09-25'
            if mutation=='refs':item['market']['source_ids']=['fabricated']
            if mutation=='code':item['sectors'][0]['code']='unknown'
            if mutation=='missing':item['sectors']=[]
            with self.assertRaises(ValueError):validate_ai(json.dumps(item),d,refs)

    def test_model_input_stable_when_only_generation_time_changes(self):
        p,h,d=self.decision(); refs=evidence(p,d,h)
        with patch('decision.analyze',return_value={'status':'DISABLED','message':'test'}) as model:
            interpret(self.root,p,d,refs,None)
            first=model.call_args.args[1]
            d['generated_at']='changed'
            interpret(self.root,p,d,refs,None)
            self.assertEqual(first,model.call_args.args[1])

    def test_prediction_original_survives_repeated_run_and_review(self):
        p,h,d=self.decision(); refs=evidence(p,d,h)
        original=archive(self.root,p,d,refs,self.now)
        path=self.root/'data/predictions'/f"{TARGET}-{original['id']}.json"
        before=path.read_bytes()
        again=archive(self.root,p,d,refs,self.now+timedelta(hours=1))
        self.assertNotEqual(original['id'],again['id'])
        self.assertEqual(len(list((self.root/'data/predictions').glob('*.json'))),2)
        reviewed=evaluate(self.root,original,series('2026-10-01'),self.now+timedelta(days=7))
        self.assertEqual(reviewed['outcomes'][2]['horizon'],5)
        self.assertEqual(before,path.read_bytes())

    def test_late_generation_is_not_forward_evidence(self):
        p,h,d=self.decision()
        record=archive(self.root,p,d,evidence(p,d,h),self.now+timedelta(days=1))
        self.assertEqual(record['record_type'],'RESEARCH_SNAPSHOT')
        self.assertNotEqual(record['mode'],'FORWARD')

    def test_stock_execution_handles_abstain_limits_trigger_and_costs(self):
        plan={'state':'观察线索','entry':{'low':10,'high':11}}
        self.assertEqual(stock_outcome(plan,[],'2026-09-25',.15,.10)['status'],'ABSTAIN')
        plan['state']='可考虑参与'
        plan['risk_verified']=True
        row={'date':'2026-09-25','open':10,'high':10,'low':10,'close':10,'volume':100}
        self.assertEqual(stock_outcome(plan,[row],row['date'],.15,.10,expected_entry_day=row['date'])['status'],'UNEXECUTABLE')
        row.update(high=12,low=9,close=11)
        self.assertEqual(stock_outcome(plan,[row],row['date'],.15,.10,expected_entry_day=row['date'])['status'],'UNRESOLVED')
        plan['corporate_actions_verified']=True
        outcome=stock_outcome(plan,[row],row['date'],.15,.10,expected_entry_day=row['date'])
        self.assertAlmostEqual(outcome['net_percent'],9.75)

    def test_complete_five_day_replay_keeps_all_horizons_and_baseline(self):
        p,h,d=self.decision()
        record=archive(self.root,p,d,evidence(p,d,h),self.now)
        write_json(self.root/'data/daily/2026-10-09/history/industry_history-BK0001.json',{'rows':series('2026-10-09')})
        reviewed=evaluate(self.root,record,series('2026-10-09'),datetime(2026,10,9,18,tzinfo=CST))
        for outcome in reviewed['outcomes']:
            self.assertEqual(outcome['status'],'EVALUATED')
            self.assertEqual(outcome['sectors'][0]['status'],'EVALUATED')
            self.assertEqual(outcome['baseline']['covered'],1)

    def test_policy_score_requires_actual_policy_sample_and_does_not_mutate_evidence(self):
        p,h,d=self.decision();refs=evidence(p,d,h)
        data={'target_date':TARGET,'summary':'观察','counterevidence':'缺口',
            'market':{'judgment':'等待','source_ids':['market']},
            'sectors':[{'code':'BK0001','judgment':'观察','source_ids':['industry:BK0001']}],
            'stocks':[], 'policy_assessment':{'score':60,'reason':'有政策样本','source_ids':['events']}}
        with self.assertRaises(ValueError):validate_ai(json.dumps(data),d,refs)
        policy_url='https://www.pbc.gov.cn/test.html'
        refs['events']['facts']={'events':[{'title':'央行货币政策公告','date':TARGET,
            'url':policy_url,'excerpt':'官方政策正文节选'}]}
        data['policy_assessment']['evidence_urls']=[policy_url]
        before=copy.deepcopy(refs)
        self.assertEqual(validate_ai(json.dumps(data),d,refs)['policy_assessment']['score'],60)
        self.assertEqual(before,refs)

    def test_calendar_skips_holidays_and_does_not_guess_future_year(self):
        self.assertFalse(is_trading_day(datetime(2026,10,1).date()))
        self.assertTrue(is_trading_day(datetime(2026,10,8).date()))
        self.assertIsNone(is_trading_day(datetime(2027,1,4).date()))

    def test_scheduler_no_duplicate_and_missed_latest_session_catchup(self):
        now=datetime(2026,10,3,20,tzinfo=CST)
        due=due_slots(now,{})
        self.assertEqual(due[0]['date'],'2026-09-30')
        self.assertFalse(due_slots(now,{'slots':{due[0]['id']:{'status':'DONE'}}}))
        self.assertFalse(due_slots(now,{'slots':{due[0]['id']:{'attempts':3}}}))

    def test_scheduler_old_done_without_formal_publication_is_not_complete(self):
        from unittest.mock import Mock
        now=datetime(2026,10,3,20,tzinfo=CST)
        write_json(self.root/'config.json',{'schedule':{'enabled':True}})
        write_json(self.root/'data/schedule.json',{'slots':{'2026-09-30:post':{'status':'DONE','attempts':1}}})
        write_json(self.root/'data/last_run.json',{'target_date':'2026-09-30','status':'READY'})
        with patch('scheduler.subprocess.run',return_value=Mock(returncode=0)) as run:
            state=tick(self.root,now)
        run.assert_called_once()
        self.assertEqual(state['slots']['2026-09-30:post']['status'],'RETRY_PENDING')
        self.assertEqual(state['slots']['2026-09-30:post']['attempts'],2)

    def test_full_product_run_and_read_are_offline_after_publication(self):
        base=FakeFetcher()
        def fetch(kind,params):
            if kind=='fund_board_list':return [{'板块代码':'BK0001','板块名称':'测试板块'}]
            if kind=='board_flow':return [{'日期':TARGET,'主力净流入-净额':100}]
            if kind=='index':return series()
            if kind=='market_batch':
                rows=base(kind,params)
                rows[1]['涨跌幅']=-1.0
                return rows
            if kind in ('industry_history','candidate_history'):return series()
            if kind=='macro_history':return series('2026-09-23')
            if kind=='margin':return [{'date':TARGET,'balance_cny':1000}]
            if kind=='etf_shares':return [{'date':TARGET,'shares':100,'name':'沪深300ETF'}]
            if kind=='stock_info':return [{'date':TARGET,'facts':{'主营':'银行'},'as_of':self.now.isoformat()}]
            if kind=='candidate_notices':return [{'date':TARGET,'items':[]}]
            return base(kind,params)
        result=Radar(self.root,fetch,lambda:self.now).run(TARGET,[],product=True,enrichment=True,retries=0,interval=0)
        self.assertEqual(result['status'],'READY')
        saved=read_json(self.root/'data/daily'/TARGET/'ai-input.json')
        self.assertIn('decision',saved)
        self.assertIsNone(saved['decision']['stocks'][0]['entry']['low'])
        self.assertIn('ma5_reference',saved['decision']['stocks'][0]['entry'])
        self.assertTrue(saved['prediction_id'])
        from dashboard import saved_analysis
        with patch('decision.analyze',side_effect=AssertionError('Browsing must not bill')):
            self.assertEqual(saved_analysis(self.root,TARGET)['decision']['version'],'v2.1')
        publication=self.root/'data/publications'/f'{TARGET}.json'
        before=publication.read_bytes()
        base.fail.add('concept_list')
        result=Radar(self.root,fetch,lambda:self.now).run(TARGET,[],product=True,enrichment=True,
                                                       refresh=True,retries=0,interval=0)
        self.assertEqual(result['status'],'INCOMPLETE')
        self.assertEqual(publication.read_bytes(),before)

    def test_partial_members_resume_without_refetching_successful_normalized_rows(self):
        base=FakeFetcher()
        calls=[]
        def fetch(kind,params):
            if kind=='concept_list':return [{'板块代码':code,'板块名称':code} for code in ('A','B')]
            if kind=='concept_members':
                calls.append(params['symbol'])
                if params['symbol']=='B':raise ValueError('unresolved member conflict')
            return base(kind,params)
        for _ in range(2):
            Radar(self.root,fetch,lambda:self.now).run(TARGET,[],product=True,retries=0,interval=0)
        self.assertEqual(calls,['A','B','B'])

    def test_required_data_failure_preserves_complete_publication(self):
        publication=self.root/'data/publications'/f'{TARGET}.json'
        write_json(publication,{'analysis':{'target_date':TARGET},'html':'previous complete report'})
        before=publication.read_bytes()
        for failure in ('concept_members','board_flow'):
            with self.subTest(failure=failure):
                if failure=='board_flow':
                    (self.root/'data/daily'/TARGET/'enrichment/flow-BK0001.json').unlink()
                base=FakeFetcher()
                base.fail.add(failure)
                def fetch(kind,params):
                    if kind=='fund_board_list':return [{'板块代码':'BK0001','板块名称':'测试板块'}]
                    if kind=='board_flow' and failure!='board_flow':return [{'日期':TARGET,'主力净流入-净额':100}]
                    return base(kind,params)
                with patch.object(Radar,'write_market_report',side_effect=AssertionError('Must not publish')):
                    result=Radar(self.root,fetch,lambda:self.now).run(TARGET,[],product=True,enrichment=True,retries=0,interval=0)
                self.assertEqual(result['status'],'INCOMPLETE')
                self.assertEqual(result['ai']['status'],'WAITING_FOR_DATA')
                self.assertEqual(publication.read_bytes(),before)
                self.assertTrue(result['data_gaps'])


if __name__=='__main__':unittest.main()
