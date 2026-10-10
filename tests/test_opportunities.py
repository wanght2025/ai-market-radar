import copy
from datetime import date, timedelta
import tempfile
import unittest
from pathlib import Path

from decision import trend
from history import shortlist
from opportunities import build, concept_facts, review_conditions, snapshot, validate_ai
from storage import write_json


class OpportunityTest(unittest.TestCase):
    def facts(self):
        target='2026-09-30'
        days=[];day=date.fromisoformat(target)
        while len(days)<21:
            if day.weekday()<5: days.append(day.isoformat())
            day-=timedelta(days=1)
        days.reverse()
        values=[100]*15+[102,101,100,98,99,100]
        bars=[{'date':d,'close':v,'amount':100} for d,v in zip(days,values)]
        board={'code':'quiet','name':'安静行业','rank':20,'relative':1,'breadth':70,
               'absolute_change_percent':.3,'member_signature':'same'}
        payload={'target_date':target,'industries':[board],'market':{'equal_weight_change_percent':-.7}}
        rule={'code':'quiet','name':'安静行业','state':'等待确认','trend':trend(bars,target)}
        decision={'sectors':[rule],'market':{'index_trends':{}}}
        history={'industries':{'quiet':{'rows':bars}}}
        return payload,decision,history,{'sh000300':bars}

    def test_weak_market_can_have_reversal_without_changing_original(self):
        p,d,h,indices=self.facts();before=copy.deepcopy(d)
        result=build(p,d,h,indices)
        self.assertEqual(result['sectors'][0]['type'],'反转观察')
        self.assertEqual(result['market']['reversal_count'],1)
        self.assertIsNone(result['market']['valuation'])
        self.assertIsNone(result['market']['extreme_sentiment'])
        self.assertEqual(d,before)

    def test_missing_trading_day_is_unknown_not_zero(self):
        p,d,h,indices=self.facts();h['industries']['quiet']['rows'].pop(10)
        row=build(p,d,h,indices)['sectors'][0]
        self.assertEqual(row['type'],'待确认')
        self.assertIsNone(row['checks']['反转观察'][0]['met'])
        self.assertFalse(row['history_complete'])

    def test_future_rows_do_not_change_diagnostic(self):
        p,d,h,indices=self.facts();before=build(p,d,h,indices)
        h['industries']['quiet']['rows'].append({'date':'2026-10-01','close':9999})
        self.assertEqual(build(p,d,h,indices),before)

    def test_candidate_can_come_from_outside_leading_five_and_empty_is_empty(self):
        p,d,h,indices=self.facts();op=build(p,d,h,indices)
        quotes={'600001':{'代码':'600001','名称':'普通股票','成交额':2e8,'涨跌幅':0}}
        members={'600001':{'code':'quiet','name':'安静行业'}}
        rows=shortlist(quotes,[{'code':str(i)} for i in range(5)],members,opportunities=op)
        self.assertEqual(rows[0]['opportunity_type'],'反转观察')
        self.assertEqual(rows[0]['代码'],'600001')
        op['sectors'][0]['type']='待确认'
        self.assertEqual(shortlist(quotes,p['industries'],members,opportunities=op),[])

    def test_concept_partial_members_do_not_generate_metrics(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base=root/'snapshots/concept/g'
            write_json(base/'manifest.json',{'unknown_member_conflicts':0,'boards':[{'code':'c','name':'概念'}]})
            write_json(base/'members/c.json',{'rows':[{'code':'1'},{'code':'2'}]})
            result=concept_facts(root,'g',{'1':{'代码':'1','涨跌幅':1,'成交额':100}},'2026-09-30')
            self.assertIsNone(result['rows'][0]['breadth'])
            self.assertIsNone(result['rows'][0]['relative'])

    def test_ai_cannot_invent_executable_conditions(self):
        p,d,h,indices=self.facts();op=build(p,d,h,indices)
        row={'code':'quiet','hypothesis':'改善','alternative':'反弹',
             'scenarios':{'启动':'确认','盘整':'等待','转弱':'失效'},'scenario_bias':'无法区分',
             'confirmation_checks':[r['id'] for r in op['sectors'][0]['confirmation_checks']],
             'invalidation_checks':[r['id'] for r in op['sectors'][0]['invalidation_checks']]}
        data={'schema_version':'decision-ui-v2','market':{'opportunity_bias':'观察'},'sectors':[row]}
        validate_ai(data,op)
        data['schema_version']='decision-ui-v3'
        validate_ai(data,op)
        row['confirmation_checks'].pop()
        with self.assertRaises(ValueError): validate_ai(data,op)
        row['confirmation_checks']=['buy_now']
        with self.assertRaises(ValueError): validate_ai(data,op)

    def test_reversal_below_ma20_uses_signal_close_for_invalidation(self):
        p,d,h,indices=self.facts();h['industries']['quiet']['rows'][1]['close']=110
        d['sectors'][0]['trend']=trend(h['industries']['quiet']['rows'],p['target_date'])
        row=build(p,d,h,indices)['sectors'][0]
        self.assertEqual(row['type'],'反转观察')
        self.assertNotIn('above_ma20',[c['id'] for c in row['confirmation_checks']])
        self.assertTrue(all(c['met'] for c in row['confirmation_checks']))
        self.assertEqual(row['signal_reference'],d['sectors'][0]['trend']['close'])
        self.assertFalse(next(c for c in row['invalidation_checks'] if c['id']=='below_signal_reference')['met'])

    def test_historical_supplement_uses_saved_concept_generation(self):
        p,d,h,indices=self.facts();p['decision']=d;p['snapshots']={'概念成员':'original-time'}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            write_json(root/'data/daily'/p['target_date']/'history/summary.json',h)
            for generation,stamp,name in (('old','original-time','原概念'),('new','later-time','新概念')):
                base=root/'data/snapshots/concept'/generation
                write_json(base/'manifest.json',{'generation':generation,'completed_at':stamp,
                    'unknown_member_conflicts':0,'boards':[{'code':generation,'name':name}]})
                write_json(base/'members'/f'{generation}.json',{'rows':[{'code':'600001'}]})
            write_json(root/'data/snapshots/concept/latest.json',{'generation':'new','completed_at':'later-time'})
            self.assertEqual(snapshot(root,p)['concepts']['rows'][0]['code'],'old')

    def test_review_does_not_treat_missing_breadth_as_confirmation(self):
        p,d,h,indices=self.facts();op=build(p,d,h,indices)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);bars=h['industries']['quiet']['rows']+[{'date':'2026-10-09','close':101}]
            write_json(root/'data/daily/2026-10-09/history/industry_history-quiet.json',{'rows':bars})
            record={'target_date':p['target_date'],'input':{'opportunities':op}}
            result=review_conditions(root,record,['2026-10-09'])[0]
            self.assertEqual(result['status'],'MISSING_DATA')
            write_json(root/'data/publications/2026-10-09.json',{'analysis':{'industries':[{**p['industries'][0],'relative':1,'breadth':70}]}})
            self.assertEqual(review_conditions(root,record,['2026-10-09'])[0]['status'],'CONFIRMED')
            write_json(root/'data/publications/2026-10-09.json',{'analysis':{'industries':[{**p['industries'][0],'relative':-1,'breadth':70}]}})
            self.assertEqual(review_conditions(root,record,['2026-10-09'])[0]['status'],'INVALIDATED')

    def test_later_missing_data_keeps_past_confirmation_but_marks_unknown(self):
        p,d,h,indices=self.facts();op=build(p,d,h,indices)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            rows=h['industries']['quiet']['rows']+[{'date':'2026-10-09','close':101},{'date':'2026-10-12','close':102}]
            write_json(root/'data/daily/2026-10-12/history/industry_history-quiet.json',{'rows':rows})
            write_json(root/'data/publications/2026-10-09.json',{'analysis':{'industries':[p['industries'][0]]}})
            row=review_conditions(root,{'target_date':p['target_date'],'input':{'opportunities':op}},['2026-10-09','2026-10-12'])[0]
            self.assertEqual(row['confirmed_at'],'2026-10-09')
            self.assertEqual(row['status'],'MISSING_DATA')

    def test_decline_risk_confirms_weakness_not_a_bullish_rebound(self):
        p,d,h,indices=self.facts()
        for i,row in enumerate(h['industries']['quiet']['rows']):row['close']=100+i
        p['industries'][0].update(relative=-1,breadth=35)
        d['sectors'][0].update(state='衰退预警',trend=trend(h['industries']['quiet']['rows'],p['target_date']))
        op=build(p,d,h,indices);signal=op['sectors'][0]
        self.assertEqual(signal['type'],'衰退风险')
        self.assertTrue(signal['confirmation_checks'][0]['met'])
        self.assertFalse(signal['invalidation_checks'][0]['met'])
        self.assertEqual(shortlist({},p['industries'],{},opportunities=op),[])
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            write_json(root/'data/daily/2026-10-09/history/industry_history-quiet.json',{'rows':h['industries']['quiet']['rows']+[{'date':'2026-10-09','close':121}]})
            write_json(root/'data/publications/2026-10-09.json',{'analysis':{'industries':[p['industries'][0]]}})
            row=review_conditions(root,{'target_date':p['target_date'],'input':{'opportunities':op}},['2026-10-09'])[0]
            self.assertEqual(row['status'],'CONFIRMED')
