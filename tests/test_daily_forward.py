"""Synthetic clocks and isolated data only; never create production forward days."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from collector import CST
from predictions import archive, review
from scheduler import due_slots, tick
from storage import read_json, write_json


class DailyForwardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def save(self, stamp, baseline='2026-10-08', defect=None):
        at = datetime.fromisoformat(stamp).astimezone(CST)
        payload = {'target_date':baseline, 'industries':[],
            'research_window':{'market_baseline_date':baseline,
                'external_information_cutoff':(at-timedelta(minutes=2)).isoformat()},
            'data_completeness':{'required':[{'name':'fixture core', 'ok':True}]}}
        decision = {'version':'v2.1', 'market':{'action':'等待确认'}, 'sectors':[], 'stocks':[],
            'ai':{'status':'READY', 'business_status':'PASSED', 'model':'fixture-model',
                'generated_at':(at-timedelta(minutes=1)).isoformat(), 'interpretation':{},'request_id':'fixture-only'}}
        if defect == 'ai':decision['ai']['business_status']='FAILED'
        if defect == 'core':payload['data_completeness']['required'][0]['ok']=False
        if defect == 'future':payload['research_window']['external_information_cutoff']=(at+timedelta(minutes=1)).isoformat()
        if defect == 'old_model':decision['ai']['generated_at']='2026-10-08T08:00:00+08:00'
        return archive(self.root,payload,decision,{},at)

    def test_post_market_is_automatically_saved_for_next_trading_day(self):
        self.save('2026-10-08T18:10:00+08:00')
        forecasts = list((self.root/'data/forward-forecasts').glob('*.json'))
        self.assertEqual(len(forecasts),1)
        frozen = read_json(forecasts[0])
        self.assertEqual(frozen['prediction_date'],'2026-10-09')
        self.assertEqual(frozen['forecast_phase'],'POST_MARKET')
        self.assertEqual(frozen['record_type'],'OFFICIAL_FORWARD_FORECAST')
        self.assertEqual(frozen['issued_at'],frozen['frozen_at'])
        self.assertEqual(review(self.root,[],datetime(2026,10,8,20,tzinfo=CST))['forward_days'],0)
        result = review(self.root,[],datetime(2026,10,9,10,tzinfo=CST))
        self.assertEqual(result['forward_days'],1)
        self.assertEqual(result['official_forward_dates'],['2026-10-09'])

    def test_latest_premarket_wins_once_without_modifying_any_original(self):
        self.save('2026-10-08T18:10:00+08:00')
        self.save('2026-10-09T08:30:00+08:00')
        latest=self.save('2026-10-09T09:08:00+08:00')
        before={p.name:p.read_bytes() for p in (self.root/'data/forward-forecasts').glob('*.json')}
        self.save('2026-10-09T09:20:00+08:00')
        self.save('2026-10-09T18:00:00+08:00',baseline='2026-10-09')
        summary=review(self.root,[],datetime(2026,10,9,18,5,tzinfo=CST))
        self.assertEqual(summary['forward_days'],1)
        selected=summary['selected_forward_forecasts']['2026-10-09']
        self.assertEqual(selected['source_snapshot_id'],latest['id'])
        counted=[r for r in summary['records'] if r.get('counts_as_forward')]
        self.assertEqual(len(counted),1)
        self.assertEqual(counted[0]['id'],selected['id'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in (self.root/'data/forward-forecasts').glob('*.json') if p.name in before})

    def test_no_run_uses_corresponding_post_version_but_does_not_inflate_future_days(self):
        from predictions import forecast_status
        self.save('2026-10-08T18:10:00+08:00')
        friday=datetime(2026,10,9,10,tzinfo=CST)
        self.assertEqual(forecast_status(self.root,friday)['status'],'POST_MARKET_FALLBACK')
        monday=datetime(2026,10,12,9,tzinfo=CST)
        self.assertEqual(forecast_status(self.root,monday)['status'],'RECENT_RESEARCH')
        self.assertEqual(review(self.root,[],monday)['forward_days'],1)

    def test_incomplete_late_intraday_wrong_baseline_and_old_ai_are_research_only(self):
        cases=[('2026-10-09T09:08:00+08:00','2026-10-08',defect) for defect in ('ai','core','future','old_model')]
        cases += [('2026-10-09T10:00:00+08:00','2026-10-08',None),
                  ('2026-10-09T09:08:00+08:00','2026-09-30',None),
                  ('2026-10-08T09:08:00+08:00','2026-10-07',None),
                  ('2026-10-08T10:00:00+08:00','2026-10-08',None)]
        for stamp,base,defect in cases:
            with self.subTest(stamp=stamp,base=base,defect=defect),tempfile.TemporaryDirectory() as folder:
                original=self.root;self.root=Path(folder)
                self.save(stamp,base,defect)
                self.assertFalse(list((self.root/'data/forward-forecasts').glob('*.json')))
                self.assertEqual(len(list((self.root/'data/predictions').glob('*.json'))),1)
                self.root=original

    def test_prior_evening_on_holiday_keeps_actual_time_and_september_baseline(self):
        self.save('2026-10-07T22:00:00+08:00',baseline='2026-09-30')
        record=read_json(next((self.root/'data/forward-forecasts').glob('*.json')))
        self.assertEqual(record['prediction_date'],'2026-10-08')
        self.assertEqual(record['market_baseline_date'],'2026-09-30')
        self.assertEqual(record['issued_at'],'2026-10-07T22:00:00+08:00')

    def test_reading_forecast_status_does_not_register_old_snapshots_or_call_model(self):
        from predictions import forecast_status
        self.save('2026-10-09T10:00:00+08:00')
        before={str(p):p.read_bytes() for p in self.root.rglob('*.json')}
        with patch('decision.analyze',side_effect=AssertionError('read must not bill')):
            state=forecast_status(self.root,datetime(2026,10,9,11,tzinfo=CST))
        self.assertEqual(state['status'],'RECENT_RESEARCH')
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*.json')})

    def test_scheduler_prioritizes_nine_am_over_old_post_catchup_in_beijing_time(self):
        at=datetime(2026,10,9,9,0,tzinfo=CST).astimezone(timezone(timedelta(hours=-6)))
        slots=due_slots(at,{})
        self.assertEqual(slots[0]['phase'],'pre')
        self.assertEqual(slots[0]['id'],'2026-10-09:pre')
        self.assertFalse(any(r['phase']=='pre' for r in due_slots(datetime(2026,10,9,9,15,tzinfo=CST),{})))

    def test_automatic_pre_failure_never_starts_a_second_paid_attempt(self):
        write_json(self.root/'config.json',{'schedule':{'enabled':True}})
        write_json(self.root/'data/schedule.json',{'slots':{'2026-10-08:post':{'status':'DONE','attempts':1}}})
        write_json(self.root/'data/publications/2026-10-08.json',{})
        with patch('scheduler.subprocess.run',return_value=Mock(returncode=1)) as run:
            tick(self.root,datetime(2026,10,9,9,tzinfo=CST))
            tick(self.root,datetime(2026,10,9,9,5,tzinfo=CST))
        self.assertEqual(run.call_count,1)
        state=read_json(self.root/'data/schedule.json')['slots']['2026-10-09:pre']
        self.assertEqual(state['status'],'NEEDS_ATTENTION')
        self.assertIn('盘前',state['message'])

    def test_automatic_pre_succeeds_only_with_that_day_saved_forecast(self):
        write_json(self.root/'config.json',{'schedule':{'enabled':True}})
        write_json(self.root/'data/schedule.json',{'slots':{'2026-10-08:post':{'status':'DONE','attempts':1}}})
        write_json(self.root/'data/publications/2026-10-08.json',{})
        write_json(self.root/'data/last_run.json',{'status':'READY','target_date':'2026-10-08',
            'required':[{'ok':True}],'product':{'gaps':[]},'ai':{'status':'READY','business_status':'FAILED'}})
        with patch('scheduler.subprocess.run',return_value=Mock(returncode=0)):
            state=tick(self.root,datetime(2026,10,9,9,tzinfo=CST))
        self.assertEqual(state['slots']['2026-10-09:pre']['status'],'NEEDS_ATTENTION')

    def test_corrupt_forecast_is_reported_without_hiding_research(self):
        from predictions import forecast_status
        path=self.root/'data/forward-forecasts/broken.json'
        path.parent.mkdir(parents=True)
        path.write_text('{broken',encoding='utf-8')
        state=forecast_status(self.root,datetime(2026,10,9,9,tzinfo=CST))
        self.assertEqual(state['status'],'RECENT_RESEARCH')
        self.assertTrue(state['data_errors'])

    def test_scheduler_marks_only_new_successful_pre_run_done_and_runs_once(self):
        write_json(self.root/'config.json',{'schedule':{'enabled':True}})
        write_json(self.root/'data/schedule.json',{'slots':{'2026-10-08:post':{'status':'DONE','attempts':1}}})
        write_json(self.root/'data/publications/2026-10-08.json',{})
        finished=datetime(2026,10,9,9,8,tzinfo=CST)
        def successful_run(*args,**kwargs):
            snapshot=self.save(finished.isoformat())
            write_json(self.root/'data/last_run.json',{'status':'READY','target_date':'2026-10-08',
                'ai':{'status':'READY','business_status':'PASSED'},'product':{'prediction_id':snapshot['id']}})
            return Mock(returncode=0)
        with patch('scheduler.now_cst',return_value=finished),patch('scheduler.subprocess.run',side_effect=successful_run) as run:
            result=tick(self.root,finished-timedelta(minutes=8))
            tick(self.root,finished+timedelta(minutes=2))
        self.assertEqual(result['slots']['2026-10-09:pre']['status'],'DONE')
        self.assertEqual(run.call_count,1)

    def test_failed_new_pre_run_is_not_hidden_by_earlier_qualified_pre_record(self):
        self.save('2026-10-09T08:30:00+08:00')
        write_json(self.root/'config.json',{'schedule':{'enabled':True}})
        write_json(self.root/'data/schedule.json',{'slots':{'2026-10-08:post':{'status':'DONE','attempts':1}}})
        write_json(self.root/'data/publications/2026-10-08.json',{})
        write_json(self.root/'data/last_run.json',{'target_date':'2026-10-08',
            'ai':{'status':'FAILED'},'product':{'prediction_id':'failed-new-run'}})
        finished=datetime(2026,10,9,9,8,tzinfo=CST)
        with patch('scheduler.now_cst',return_value=finished),patch('scheduler.subprocess.run',return_value=Mock(returncode=0)):
            result=tick(self.root,finished-timedelta(minutes=8))
        self.assertEqual(result['slots']['2026-10-09:pre']['status'],'NEEDS_ATTENTION')


if __name__=='__main__':unittest.main()
