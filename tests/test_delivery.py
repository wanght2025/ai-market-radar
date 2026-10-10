import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from collector import Collector
from enrichment import event_validator, fetch_news_feed
from research_context import research_event_sample


class DeliveryTest(unittest.TestCase):
    def test_treasury_timeout_uses_dated_fred_series_and_excludes_future(self):
        import requests
        from akshare_worker import fetch
        response=Mock();response.text='observation_date,DGS10\n2026-10-01,5.24\n2026-10-02,5.28\n2026-10-05,\n2026-10-06,5.30\n'
        with patch('requests.get',side_effect=[requests.Timeout(),response]):
            rows=fetch('international_observation',{'kind':'yield','symbol':'US10Y','cutoff':'2026-10-05'})
        self.assertEqual([r['date'] for r in rows],['2026-10-01','2026-10-02'])
        self.assertEqual(rows[-1]['close'],5.28)
        self.assertIn('DGS10',rows[-1]['source'])

    def test_fred_wrong_series_is_rejected(self):
        import requests
        from akshare_worker import fetch
        response=Mock();response.text='observation_date,DGS2\n2026-10-02,5.28\n'
        with patch('requests.get',side_effect=[requests.Timeout(),response]):
            with self.assertRaisesRegex(ValueError,'FRED'):
                fetch('international_observation',{'kind':'yield','symbol':'US10Y','cutoff':'2026-10-05'})

    def test_supplemental_review_does_not_reuse_original_time_cutoff(self):
        from refresh_ai_review import refresh_review
        from storage import write_json
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30'
            payload={'target_date':target,'decision':{'market':{},'stocks':[],'sectors':[]},
                'research_window':{'external_information_cutoff':'2026-09-30T18:00:00+08:00'},
                'enrichment':{'events':[]},'market':{},'decision_evidence':{'market':{'facts':{}}}}
            write_json(root/'data/publications'/f'{target}.json',{'analysis':payload})
            write_json(root/'data/daily'/target/'observations/news.json',{
                'fetched_at':'2026-10-04T14:00:00+08:00','rows':[{
                    'as_of':'2026-10-04T14:00:00+08:00','items':[{'date':'2026-10-04',
                        'time':'2026-10-04 13:00:00','title':'美国市场事件','source':'测试来源',
                        'url':'https://example.com/event'}]}]})
            with patch('refresh_ai_review.interpret',return_value={'status':'NOT_CONFIGURED'}) as model:
                refresh_review(root,target)
            events=model.call_args.args[1]['research_context']['events']['supplemental_items']
            self.assertEqual(events[0]['title'],'美国市场事件')
            self.assertEqual(payload['research_window']['external_information_cutoff'],'2026-09-30T18:00:00+08:00')

    def test_batch_date_uses_validated_date_without_registered_column(self):
        target='2026-09-30'
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            collector=Collector(root,lambda *_:[{'date':target,'items':[]}])
            collector.limit=1;collector.interval=0
            with patch.dict('collector.REQUIRED_DAILY_COLUMNS',{},clear=True):
                self.assertTrue(collector._fetch('notice','candidate_notices',{'target_date':target},
                    root/'data/notice.json',event_validator(target)))
            self.assertEqual(collector.checkpoint['tasks']['notice']['status'],'SUCCESS')

    def test_theme_sampling_keeps_international_and_policy_despite_recent_noise(self):
        events=[{'id':str(i),'time':f'2026-10-05T12:{i:02}:00','title':'其他报道'} for i in range(20)]
        events += [{'id':'policy','time':'2026-10-05T09:00:00','title':'财政政策通知',
                    'source':'中国人民银行官网','excerpt':'正文'},
                   {'id':'world','time':'2026-10-05T10:00:00','title':'美联储会议'}]
        sample=research_event_sample(events,12)
        self.assertEqual(len(sample),12)
        self.assertTrue({'policy','world'}.issubset({e['id'] for e in sample}))
        self.assertEqual(len(events),22)

    def test_news_partial_page_failure_keeps_facts_and_reports_gap(self):
        records=[{'create_time':f'2026-10-05 12:{i:02}:00','rich_text':f'新闻{i}'} for i in range(20)]
        response=Mock();response.json.return_value={'result':{'data':{'feed':{'list':records}}}}
        import requests
        with patch('enrichment.requests.get',side_effect=[response,requests.Timeout()]) as get:
            result=fetch_news_feed('2026-10-05T13:00:00+08:00')[0]
        self.assertEqual(len(result['items']),20)
        self.assertIn('第2页未取得',result['page_error'])
        self.assertEqual(get.call_count,2)

    def test_repeated_news_page_does_not_inflate_coverage(self):
        records=[{'create_time':f'2026-10-05 12:{i:02}:00','rich_text':f'新闻{i}'} for i in range(20)]
        response=Mock();response.json.return_value={'result':{'data':{'feed':{'list':records}}}}
        with patch('enrichment.requests.get',return_value=response) as get:
            result=fetch_news_feed('2026-10-05T13:00:00+08:00')[0]
        self.assertEqual(len(result['items']),20)
        self.assertEqual(get.call_count,2)
