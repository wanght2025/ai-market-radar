import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from daily_ai import compact_request
from dashboard import Dashboard, research_analysis, research_progress
from deepseek_analysis import model_budget
from research_facts import price_facts, validate_price_facts
from scheduler import CST, schedule_summary
from storage import read_json, write_json


class ExperienceImprovementsTest(unittest.TestCase):
    def decision(self, close=7989.13, ma20=7753.4605):
        return {'target_date':'2026-09-30','stocks':[],'sectors':[{'code':'801150','state':'等待确认',
            'trend':{'date':'2026-09-30','close':close,'ma5':7872.788,'ma20':ma20}}]}

    def test_actual_medical_response_cannot_pass_price_consistency(self):
        response={'sectors':[{'code':'801150','judgment':
            '医药指数反弹强劲且广度84%，但收盘未站上20日线，仍属等待确认；后续需连续站稳20日线才能提高判断。'}]}
        with self.assertRaisesRegex(ValueError,'801150.*MA20'):
            validate_price_facts(response,self.decision())
        response['sectors'][0]['judgment']='收盘高于20日线，但需后续连续站稳；原规则仍为等待确认。'
        validate_price_facts(response,self.decision())

    def test_structured_facts_above_below_equal_missing_and_bool_type(self):
        for close,ma20,above in ((101,100,True),(99,100,False),(100,100,False),(100,None,None)):
            decision=self.decision(close,ma20)
            expected=price_facts(decision['sectors'][0],decision['target_date'])
            self.assertIs(expected['above_ma20'],above)
            claim={k:expected[k] for k in ('date','above_ma5','above_ma20')}
            response={'sectors':[{'code':'801150','judgment':'条件性观察','price_facts':claim}]}
            validate_price_facts(response,decision,require=True)
            for wrong in (not above,1,'true'):
                bad=copy.deepcopy(response);bad['sectors'][0]['price_facts']['above_ma20']=wrong
                with self.assertRaises(ValueError):validate_price_facts(bad,decision,require=True)
        with self.assertRaises(ValueError):
            validate_price_facts({'sectors':[{'code':'801150'}]},self.decision(),require=True)
        # Equality is not "above", but also is not "below".
        validate_price_facts({'sectors':[{'code':'801150','judgment':'收盘未站上20日线'}]},self.decision(100,100))
        with self.assertRaises(ValueError):
            validate_price_facts({'sectors':[{'code':'801150','judgment':'收盘低于20日线'}]},self.decision(100,100))

    def test_compact_history_keeps_direct_average_facts_without_mutating_input(self):
        request={'decision_rules':{'market':{},'sectors':[],'stocks':[]},'evidence':{},
            'facts':{'history:industry:801150':{'trend':self.decision()['sectors'][0]['trend'],
                'history_rows':[{'private':'not repeated'}]}}}
        before=copy.deepcopy(request)
        result=compact_request(request)
        self.assertEqual(result['facts']['history:industry:801150']['ma20'],7753.4605)
        self.assertEqual(result['facts']['history:industry:801150']['close'],7989.13)
        self.assertNotIn('history_rows',result['facts']['history:industry:801150'])
        self.assertEqual(request,before)

    def test_reader_uses_current_business_evidence_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30'
            old={'target_date':target,'stocks':[{'code':'603259','name':'药明康德','business':'旧主营',
                'business_date':'2026-10-06','notices':[],'issues':[],'source_ids':['research:603259']}]}
            decision={'target_date':target,'market':{'dimensions':[]},'sectors':[],
                'stocks':[{'code':'603259','name':'药明康德'}],
                'ai':{'status':'READY','research_evidence':old}}
            path=root/'data/publications'/f'{target}.json'
            write_json(path,{'analysis':{'target_date':target,'decision':decision,'decision_evidence':{}}})
            before=path.read_bytes()
            current=copy.deepcopy(old);current['stocks'][0].update(business='当前主营',business_date='2026-10-07')
            with patch('participation.snapshot',return_value=current):result=research_analysis(root,target)
            self.assertEqual(result['evidence']['research:603259']['facts'],result['research_evidence']['stocks'][0])
            self.assertEqual(result['evidence']['research:603259']['facts']['business_date'],'2026-10-07')
            self.assertEqual(result['historical_evidence']['research:603259']['facts']['business_date'],'2026-10-06')
            self.assertEqual(path.read_bytes(),before)

    def test_saved_false_ai_is_disqualified_on_read_without_writing_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30'
            decision=self.decision();decision['market']={'dimensions':[]}
            decision['ai']={'status':'READY','business_status':'PASSED','interpretation':{
                'sectors':[{'code':'801150','judgment':'收盘未站上20日线'}]}}
            path=root/'data/publications'/f'{target}.json'
            write_json(path,{'analysis':{'target_date':target,'decision':decision}})
            before=path.read_bytes()
            result=research_analysis(root,target)
            self.assertEqual(result['decision']['ai']['evidence_status'],'FACT_MISMATCH')
            self.assertEqual(result['decision']['ai']['fact_validation']['status'],'FAILED')
            self.assertEqual(result['decision']['ai']['status'],'FAILED')
            self.assertEqual(path.read_bytes(),before)

    def test_budget_uses_utc_day_and_exact_input_reservation(self):
        now=datetime(2026,10,6,23,59,tzinfo=timezone.utc)
        settings={'max_tokens':100,'max_requests_per_day':4,'max_total_tokens_per_day':10000}
        ledger={'days':{'2026-10-06':{'requests':4,'total_tokens':200}}}
        result=model_budget(settings,ledger,now=now)
        self.assertFalse(result['new_request_possible'])
        self.assertEqual(result['reset_at'],'2026-10-07T00:00:00+00:00')
        ledger['days']['2026-10-06']['requests']=3
        self.assertTrue(model_budget(settings,ledger,now=now,reservation=9800)['new_request_possible'])
        self.assertFalse(model_budget(settings,ledger,now=now,reservation=9801)['new_request_possible'])

    def test_requests_are_unlimited_when_no_count_cap_is_configured(self):
        settings={'max_tokens':100,'max_total_tokens_per_day':10000}
        now=datetime(2026,10,6,12,tzinfo=timezone.utc)
        ledger={'days':{'2026-10-06':{'requests':500,'total_tokens':200}}}
        result=model_budget(settings,ledger,now=now)
        self.assertIsNone(result['remaining_requests'])
        self.assertTrue(result['new_request_possible'])
        ledger['days']['2026-10-06']['total_tokens']=9900
        self.assertFalse(model_budget(settings,ledger,now=now)['new_request_possible'])

    def test_finished_task_without_stage_records_has_unknown_progress(self):
        self.assertIsNone(research_progress([],False,0)['completed'])
        self.assertFalse(research_progress([],False,0)['known'])

    def test_schedule_reports_actual_attempt_and_holiday_next_window(self):
        state={'slots':{'old':{'status':'DONE','attempted_at':'2026-09-30T18:00:00+08:00',
            'finished_at':'2026-09-30T19:00:00+08:00'},'new':{'status':'NEEDS_ATTENTION',
            'attempted_at':'2026-10-06T18:00:00+08:00'}}}
        result=schedule_summary(state,True,now=datetime(2026,10,7,1,2,tzinfo=CST))
        self.assertEqual(result['latest_attempt']['status'],'NEEDS_ATTENTION')
        self.assertEqual(result['latest_success']['status'],'DONE')
        self.assertFalse(result['today_trading'])
        self.assertEqual(result['next_research_window'],'2026-10-08T08:30:00+08:00')
        self.assertEqual(result['next_check_estimate'],'2026-10-07T01:05:00+08:00')

    def test_real_child_process_stage_encoding_with_gbk_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'update_research.py').write_text("print('RESEARCH_STAGE:正在更新海外行情与事件',flush=True)\n",encoding='utf-8')
            server=Dashboard(('127.0.0.1',0),root)
            try:
                with patch.dict(os.environ,{'PYTHONIOENCODING':'gbk'}):server.start_run('',mode='current')
                server.process.wait(timeout=10)
                # The production collector thread owns stdout, not a mocked Popen.
                for thread in threading.enumerate():
                    if thread.name.startswith('Thread-') and 'collect_output' in thread.name:thread.join(timeout=5)
                self.assertIn('RESEARCH_STAGE:正在更新海外行情与事件',server.output)
                self.assertEqual(research_progress(server.stages,True,None)['completed'],1)
                self.assertFalse(any('�' in line for line in server.output))
            finally:server.server_close()

    def test_invalid_child_log_byte_does_not_leave_task_running(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'update_research.py').write_text("import sys\nsys.stdout.buffer.write(b'\\xff\\n')\nsys.stdout.flush()\nraise SystemExit(1)\n")
            server=Dashboard(('127.0.0.1',0),root)
            try:
                server.start_run('',mode='current')
                server.process.wait(timeout=10)
                for thread in threading.enumerate():
                    if 'collect_output' in thread.name:thread.join(timeout=5)
                self.assertEqual(server.exit_code,1)
                self.assertTrue(server.process.stdout.closed)
                self.assertIn('�',server.output)
            finally:
                server.process.stdout.close();server.server_close()

    def test_budget_exhausted_update_can_finish_evidence_without_new_ai(self):
        from update_research import update
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30'
            write_json(root/'data/publications'/f'{target}.json',{'analysis':{}})
            with patch('update_research.now_cst',return_value=datetime(2026,10,6,18,tzinfo=CST)), \
                    patch('update_research.Collector'),patch('research_context.collect_observations') as collect, \
                    patch('refresh_ai_review.refresh_review',return_value={'status':'BUDGET_EXCEEDED','message':'次数已用尽'}):
                result=update(root,'current')
            self.assertEqual(result['status'],'BUDGET_EXCEEDED')
            collect.assert_called_once()

    def test_new_market_publication_survives_budget_exhaustion(self):
        from update_research import update
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            write_json(root/'config.json',{'deepseek':{'enabled':True}})
            published={'target_date':'2026-10-08','status':'READY',
                'ai':{'status':'BUDGET_EXCEEDED','message':'次数已用尽'}}
            with patch('update_research.now_cst',return_value=datetime(2026,10,8,18,tzinfo=CST)), \
                    patch('market_radar.Radar') as radar:
                radar.return_value.run.return_value=published
                result=update(root,'current')
            self.assertEqual(result,published)


if __name__=='__main__':unittest.main()
