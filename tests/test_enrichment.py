import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from enrichment import fetch_events, event_validator, news_feed_validator, collect
from collector import Collector

TARGET = '2026-09-28'


class EnrichmentTest(unittest.TestCase):
    def test_connection_outage_does_not_spend_128_identical_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            def fetch(kind,params):
                if kind=='fund_board_list':return [{'code':f'BK{i}','name':f'行业{i}'} for i in range(128)]
                if kind=='news_feed':return [{'as_of':params['as_of'],'items':[{'time':'2026-09-28 09:00:00','title':'资讯'}]}]
                if kind=='notices':return [{'date':TARGET,'items':[]}]
                calls.append(kind)
                raise RuntimeError("ConnectionError: RemoteDisconnected('Remote end closed connection without response')")
            c=Collector(Path(directory),fetch);c.limit=180;c.retries=0;c.interval=0
            result=collect(c,TARGET,[])
            self.assertFalse(result['flow_complete'])
            self.assertLessEqual(calls.count('board_flow'),3)
            self.assertEqual(len(result['gaps']),128)

    def test_news_excludes_future_and_keeps_empty_search_distinct(self):
        response = Mock()
        response.text = 'radar('+json.dumps({'result':{'cmsArticleWebOld':[
            {'date':'2026-09-29 09:00','title':'future','mediaName':'source','code':'123'}]}})+');'
        with patch('enrichment.requests.get',return_value=response):
            rows = fetch_events('news',{'target_date':TARGET,'code':'000001'})
        self.assertEqual(rows[0]['items'],[])
        self.assertEqual(event_validator(TARGET)(rows)[1],TARGET)

    def test_reject_wrong_day_and_unsafe_link(self):
        event = {'date':TARGET,'time':TARGET,'title':'title','source':'source','url':'javascript:alert(1)'}
        with self.assertRaises(ValueError):
            event_validator(TARGET)([{'date':TARGET,'items':[event]}])
        with self.assertRaises(ValueError):
            event_validator(TARGET)([{'date':'2026-09-29','items':[]}])

    def test_latest_news_rejects_future_publication(self):
        as_of = '2026-10-01T09:00:00+08:00'
        batch = [{'as_of':as_of, 'items':[{'time':'2026-10-01 09:01:00','title':'future'}]}]
        with self.assertRaises(ValueError):
            news_feed_validator(as_of)(batch)

    def test_failed_flow_does_not_block_events_and_resume_reuses(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            fail=[True]
            def fetch(kind, params):
                calls.append(kind)
                if kind in ('news','notices'):
                    return [{'date':TARGET,'items':[]}]
                if kind == 'news_feed':
                    return [{'date':TARGET,'as_of':params['as_of'],'items':[{'time':'2026-09-28 09:00:00','title':'sample'}]}]
                if kind == 'fund_board_list':
                    return [{'板块代码':'BK1','板块名称':'供应商行业'}]
                if kind == 'board_flows':
                    raise RuntimeError('batch source unavailable; use dated fallback')
                if fail[0]:
                    raise RuntimeError('source unavailable')
                return [{'日期':TARGET,'主力净流入-净额':123.0}]
            collector=Collector(Path(directory),fetch)
            collector.limit=20
            collector.retries=0
            collector.interval=0
            first=collect(collector,TARGET,[{'code':'000001'}])
            self.assertFalse(first['flow_complete'])
            fail[0]=False
            calls.clear()
            second=collect(collector,TARGET,[{'code':'000001'}])
            self.assertEqual(calls,['news_feed','board_flows','board_flow'])
            self.assertTrue(second['flow_complete'])
            self.assertEqual(second['flows'][0]['net_cny'],123)

    def test_failed_news_cannot_starve_unattempted_fund_task(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            def fetch(kind, params):
                calls.append(kind)
                if kind == 'fund_board_list':
                    return [{'板块代码':'BK1','板块名称':'行业'}]
                if kind == 'board_flows':
                    raise RuntimeError('batch source unavailable; use dated fallback')
                if kind == 'news_feed':
                    return [{'as_of':params['as_of'],'items':[{'time':'2026-09-28 09:00:00','title':'sample'}]}]
                if kind == 'notices':
                    return [{'date':TARGET,'items':[]}]
                if kind == 'news':
                    raise RuntimeError('outage')
                return [{'日期':TARGET,'主力净流入-净额':123}]
            collector=Collector(Path(directory),fetch)
            collector.limit,collector.retries,collector.interval=5,0,0
            self.assertFalse(collect(collector,TARGET,[{'code':'000001'}])['flow_complete'])
            collector.calls=0
            collector.limit=4
            calls.clear()
            self.assertTrue(collect(collector,TARGET,[{'code':'000001'}])['flow_complete'])
            self.assertLess(calls.index('board_flow'),calls.index('news'))
