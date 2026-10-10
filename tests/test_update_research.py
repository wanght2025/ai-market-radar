import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from collector import CST
from dashboard import research_progress, same_review_baseline
from decision import apply_policy, build, trend
from participation import evidence_digest
from refresh_ai_review import refresh_review
from research_context import changed_modules, context_digest
from storage import write_json, read_json


class UpdateResearchTest(unittest.TestCase):
    def test_holiday_current_mode_uses_last_session_and_replay_does_not_collect(self):
        from update_research import update
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30'
            write_json(root/'data/publications'/f'{target}.json',{'analysis':{}})
            with patch('update_research.now_cst',return_value=datetime(2026,10,6,18,tzinfo=CST)), \
                    patch('update_research.Collector'),patch('research_context.collect_observations') as collect, \
                    patch('refresh_ai_review.refresh_review',return_value={'status':'READY'}) as model:
                update(root,'current')
                collect.assert_called_once_with(unittest.mock.ANY,target,historical=False)
                model.assert_called_once_with(root,target,historical=False)
                collect.reset_mock();model.reset_mock()
                update(root,'replay',target)
                collect.assert_not_called()
                model.assert_called_once_with(root,target,historical=True)

    def test_unvalidated_outer_review_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30';daily=root/'data/daily'/target
            payload={'target_date':target,'decision':{'market':{},'stocks':[],'sectors':[]},'market':{}}
            write_json(root/'data/publications'/f'{target}.json',{'analysis':payload})
            with patch('refresh_ai_review.interpret',return_value={'status':'READY'}) as model:
                refresh_review(root,target)
                refresh_review(root,target)
            self.assertEqual(model.call_count,2)

    def test_failed_republication_can_retain_review_only_when_baseline_matches(self):
        target='2026-09-30';indices={code:{'date':target,'close':100,'ma20':101,'samples':21}
            for code in ('sh000001','sz399001','sh000300')}
        market={'dimensions':[{'id':'technical','score':0,'weight':30}], 'index_trends':indices}
        review={'target_date':target,'ai_policy_assessment':copy.deepcopy(market),'research_evidence':{'stocks':[]}}
        payload={'target_date':target,'decision':{'market':copy.deepcopy(market),'stocks':[]}}
        payload['decision']['market']['index_trends']['sh000001']['samples']=32
        self.assertTrue(same_review_baseline(review,payload))
        payload['decision']['market']['index_trends']['sh000001']['close']=99
        self.assertFalse(same_review_baseline(review,payload))

    def test_policy_cannot_override_risk_veto(self):
        weights={'technical':30,'funds':25,'policy':15,'international':10,'sentiment':20}
        scores={'technical':100/3,'funds':100,'policy':None,'international':75,'sentiment':39}
        decision={'market':{'action':'回避','dimensions':[
            {'id':k,'score':scores[k],'weight':v} for k,v in weights.items()]}}
        apply_policy(decision,{'policy_assessment':{'score':100,'reason':'测试','source_ids':['event:p']}})
        self.assertAlmostEqual(decision['market']['score'],65.3)
        self.assertEqual(decision['market']['action'],'回避')

    def test_metadata_does_not_change_digest_but_event_body_and_failure_do(self):
        context={'funds':{'stocks':[]},'events':{'as_of':'old','original_items':[],
            'supplemental_items':[{'id':'e','time':'2026-09-30T12:00:00','title':'事实','known_at':'old','excerpt':'正文'}],
            'source_status':'SUCCESS'}}
        changed=copy.deepcopy(context);changed['events']['as_of']='new'
        changed['events']['supplemental_items'][0]['known_at']='new'
        self.assertEqual(context_digest(context),context_digest(changed))
        self.assertEqual(changed_modules(context,changed),[])
        changed['events']['supplemental_items'][0]['excerpt']='实质修正'
        self.assertEqual(changed_modules(context,changed),['events'])
        changed=copy.deepcopy(context);changed['events']['source_status']='FAILED'
        self.assertNotEqual(context_digest(context),context_digest(changed))

    def test_late_business_fetch_date_never_changes_historical_qualification(self):
        a={'stocks':[{'code':'000001','business_late':True,'business_date':'2026-10-06','business':'事实'}]}
        b=copy.deepcopy(a);b['stocks'][0]['business_date']='2026-10-07'
        self.assertEqual(evidence_digest(a),evidence_digest(b))
        b['stocks'][0]['business']='主营发生变化'
        self.assertNotEqual(evidence_digest(a),evidence_digest(b))

    def test_future_overseas_rows_cannot_change_original_score(self):
        target='2026-09-30';final=datetime.fromisoformat(target)
        rows=[{'date':(final-timedelta(days=30-i)).date().isoformat(),'close':100+i} for i in range(31)]
        payload={'target_date':target,'market':{'breadth_percent':50,'equal_weight_change_percent':0},'industries':[]}
        history={'international':{'.INX':{'rows':rows}}}
        result=build(payload,history,[],{},datetime.now(CST))
        history['international']['.INX']['rows'].append({'date':'2026-10-01','close':1})
        after=build(payload,history,[],{},datetime.now(CST))
        self.assertEqual(result['market']['dimensions'],after['market']['dimensions'])
        self.assertIsNone(trend(rows+[rows[-2]],target))

    def test_progress_tracks_completed_steps_and_never_finishes_failed_run(self):
        lines=['RESEARCH_STAGE:正在检查行情与已有研究','RESEARCH_STAGE:正在更新海外行情与事件']
        self.assertEqual(research_progress(lines,True,None)['completed'],1)
        self.assertEqual(research_progress(lines,False,1)['completed'],1)
        self.assertEqual(research_progress(lines,False,0)['completed'],4)

    def test_historical_evidence_is_separate_and_filters_later_publications(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30';daily=root/'data/daily'/target
            payload={'target_date':target,'decision':{'market':{},'stocks':[],'sectors':[]},'market':{},'enrichment':{}}
            write_json(root/'data/publications'/f'{target}.json',{'analysis':payload})
            events=[{'date':day,'time':day+'T12:00:00+08:00','title':day,'url':'https://example.test/'+day}
                    for day in ('2026-09-30','2026-10-04')]
            write_json(daily/'historical-observations/news.json',{'fetched_at':'2026-10-06T12:00:00+08:00',
                'rows':[{'as_of':target+'T23:59:59+08:00','items':events}]})
            write_json(daily/'ai-review.json',{'status':'READY','marker':'current'})
            with patch('refresh_ai_review.interpret',return_value={'status':'READY','business_status':'PASSED',
                'generated_at':'2026-10-06T12:00:00+08:00','interpretation':{},'request_id':'test'}) as model:
                refresh_review(root,target,historical=True)
            context=model.call_args.args[1]['research_context']
            self.assertEqual([e['date'] for e in context['events']['supplemental_items']],[target])
            self.assertEqual(read_json(daily/'ai-review.json')['marker'],'current')
            self.assertEqual(read_json(daily/'ai-review-historical.json')['mode'],'HISTORICAL_REVIEW')

    def test_review_cache_reuses_same_facts_after_timestamp_refresh(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30';daily=root/'data/daily'/target
            payload={'target_date':target,'decision':{'market':{},'stocks':[],'sectors':[]},'market':{},'enrichment':{}}
            write_json(root/'data/publications'/f'{target}.json',{'analysis':payload})
            record={'rows':[{'as_of':'2026-10-04T14:00:00+08:00','items':[
                {'date':target,'time':target+'T12:00:00+08:00','title':'事实','url':'https://example.test/event'}]}]}
            write_json(daily/'observations/news.json',record)
            with patch('refresh_ai_review.interpret',return_value={'status':'READY','business_status':'PASSED',
                'generated_at':'2026-10-06T12:00:00+08:00','interpretation':{},'request_id':'test'}) as model:
                refresh_review(root,target)
                record['rows'][0]['as_of']='2026-10-05T14:00:00+08:00'
                write_json(daily/'observations/news.json',record)
                self.assertTrue(refresh_review(root,target)['cached'])
            self.assertEqual(model.call_count,1)


if __name__=='__main__':unittest.main()
