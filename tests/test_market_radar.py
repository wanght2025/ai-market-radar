import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from market_radar import Radar
from storage import read_json


TARGET = "2026-09-24"
CST = timezone(timedelta(hours=8))


class FakeFetcher:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self.flow_date = TARGET

    def __call__(self, kind, params):
        self.calls.append((kind, dict(params)))
        if kind in self.fail:
            raise RuntimeError("simulated source outage")
        if kind == "universe":
            return [{"代码": "000001", "名称": "平安银行"}, {"代码": "600001", "名称": "全市场股票"}]
        if kind == "market_batch":
            return [{"代码": c, "名称": "测试", "日期": TARGET, "收盘": 12.5,
                     "涨跌幅": 4.0, "成交额": 200000000} for c in ("000001", "600001")]
        if kind == "market_history":
            return [{"代码": params['code'], "名称": params['name'], "日期": TARGET,
                     "收盘":12.5, "涨跌幅":4.0, "成交额":200000000}]
        if kind == "concept_list":
            return [{"板块代码":"sina-gn_test","板块名称":"测试概念"}]
        if kind == "industry_list":
            return [{"板块代码": "BK0001", "板块名称": "测试板块"}]
        if kind == "concept_members":
            return [{'raw_rows':[{'code':'000001','name':'平安银行','symbol':'sz000001'}],
                     'collection':{'complete':True,'source_count':1,'raw_count':1,
                         'forward_pages':[{'page':1,'size':1},{'page':2,'size':0}],
                         'reverse_pages':[{'page':1,'size':1},{'page':2,'size':0}]}}]
        if kind == "industry_members":
            return [{"代码": "000001", "名称": "平安银行"}]
        if kind == "index":
            return [{"date": TARGET, "close": 3000.0}]
        if kind == "stock_daily":
            return [{"日期": TARGET, "收盘": 12.5}]
        if kind == "stock_flow":
            return [{"日期": self.flow_date, "主力净流入-净额": 200.0}]
        raise AssertionError(kind)


class RadarTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fetcher = FakeFetcher()
        self.clock = lambda: datetime(2026, 9, 24, 17, 0, tzinfo=CST)
        self.watchlist = [{"code": "000001", "market": "sz", "name": "平安银行"}]

    def tearDown(self):
        self.temp.cleanup()

    def run_radar(self, **kwargs):
        return Radar(self.root, self.fetcher, self.clock).run(TARGET, self.watchlist, retries=0, interval=0, **kwargs)

    def test_failed_member_resumes_without_refetching_successes(self):
        self.fetcher.fail.add("industry_members")
        first = self.run_radar()
        self.assertEqual(first["status"], "INCOMPLETE")
        self.assertTrue((self.root / "reports" / "index.html").exists())
        self.assertFalse((self.root / "reports" / f"market-{TARGET}.html").exists())
        first_call_count = len(self.fetcher.calls)
        self.fetcher.fail.clear()
        second = self.run_radar()
        self.assertEqual(second["status"], "READY")
        self.assertEqual([kind for kind, _ in self.fetcher.calls[first_call_count:]],
                         ["industry_members"])
        self.assertTrue((self.root / "reports" / f"market-{TARGET}.html").exists())
        self.assertTrue((self.root / "data" / "snapshots" / "industry-sw" / "latest.json").exists())

    def test_stale_daily_flow_is_partial_and_retried(self):
        self.fetcher.flow_date = "2026-09-23"
        first = self.run_radar()
        self.assertEqual(first["status"], "READY")
        self.assertFalse(first['watchlist_flow_ready'])
        checkpoint = read_json(self.root / "data" / "checkpoint.json")
        self.assertEqual(checkpoint["tasks"][f"daily:{TARGET}:flow:000001"]["status"], "PARTIAL")
        self.assertEqual(checkpoint["tasks"][f"daily:{TARGET}:flow:000001"]["observed_date"],
                         "2026-09-23")
        first_call_count = len(self.fetcher.calls)
        self.fetcher.flow_date = TARGET
        second = self.run_radar()
        self.assertEqual(second["status"], "READY")
        self.assertEqual([kind for kind, _ in self.fetcher.calls[first_call_count:]], ["stock_flow"])

    def test_refresh_failure_preserves_last_complete_snapshot_with_date(self):
        self.assertEqual(self.run_radar()["status"], "READY")
        original = read_json(self.root / "data" / "snapshots" / "industry-sw" / "latest.json")
        self.fetcher.fail.update({"industry_list", "concept_list", "universe"})
        refreshed = self.run_radar(refresh=True)
        self.assertEqual(refreshed["status"], "READY")
        self.assertEqual(refreshed["snapshots"]["行业成员"], original["completed_at"])
        status_html = (self.root / "reports" / "index.html").read_text(encoding="utf-8")
        self.assertIn(original["completed_at"], status_html)

    def test_budgeted_run_continues_next_click(self):
        first = self.run_radar(max_tasks=2)
        self.assertEqual(first["status"], "INCOMPLETE")
        self.assertEqual(first["requests_this_run"], 2)
        second = self.run_radar()
        self.assertEqual(second["status"], "READY")
        self.assertEqual([kind for kind, _ in self.fetcher.calls].count("universe"), 1)
        self.assertEqual([kind for kind, _ in self.fetcher.calls].count("index"), 3)

    def test_optional_outage_does_not_block_core_or_market_scan(self):
        self.watchlist.append({'code':'000002','name':'独立观察股票'})
        self.fetcher.fail.update({'concept_list', 'stock_flow', 'stock_daily'})
        result = self.run_radar()
        self.assertEqual(result['status'], 'READY')
        self.assertTrue(result['readiness']['MARKET_READY'])
        self.assertFalse(result['readiness']['WATCHLIST_READY'])
        report = (self.root/'reports'/f'market-{TARGET}.html').read_text(encoding='utf-8')
        self.assertIn('600001', report)
        self.assertIn('缺失', report)

    def test_market_failure_preserves_report_and_generates_status(self):
        self.run_radar()
        path = self.root/'reports'/f'market-{TARGET}.html'
        before = path.read_bytes()
        next((self.root/'data'/'daily'/TARGET/'market-batches').glob('*.json')).unlink()
        self.fetcher.fail.add('market_batch')
        result = self.run_radar()
        self.assertFalse(result['readiness']['MARKET_READY'])
        self.assertEqual(before, path.read_bytes())
        self.assertTrue((self.root/'reports'/'index.html').exists())

    def test_unknown_trade_day_never_guessed(self):
        self.fetcher.fail.add('index')
        result = Radar(self.root,self.fetcher,self.clock).run(None,[],retries=0,interval=0)
        self.assertEqual(result['target_date'],'UNCONFIRMED')
        self.assertFalse(result['readiness']['MARKET_READY'])

    def test_market_requires_every_universe_code_and_correct_date(self):
        for mode in ('missing','stale'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as work:
                def source(kind,params):
                    rows = self.fetcher(kind,params)
                    if kind == 'market_batch':
                        if mode == 'missing':
                            rows = rows[:1]
                        else:
                            rows[0]['日期'] = '2026-09-23'
                    return rows
                result=Radar(Path(work),source,self.clock).run(TARGET,[],retries=0,interval=0)
                self.assertFalse(result['readiness']['MARKET_READY'])

    def test_retry_count_is_separate_from_task_budget(self):
        failed = False
        def source(kind, params):
            nonlocal failed
            if kind == 'index' and not failed:
                failed = True
                raise RuntimeError('transient')
            return self.fetcher(kind, params)
        result = Radar(self.root,source,self.clock).run(TARGET,[],retries=1,interval=0)
        self.assertEqual(result['attempts_this_run'],result['requests_this_run']+1)
        self.assertEqual(result['status'],'READY')

    def test_second_successful_run_uses_zero_network_tasks(self):
        self.run_radar()
        result = self.run_radar()
        self.assertEqual(result['requests_this_run'],0)
        self.assertEqual(result['attempts_this_run'],0)
        analytics = read_json(self.root/'data'/'analytics'/f'{TARGET}.json')
        self.assertEqual(analytics['industries'][0]['amount'],200000000)
        self.assertEqual(analytics['industries'][0]['share'],50)

    def test_comparison_requires_same_industry_generation_and_keeps_date(self):
        from storage import write_json
        self.run_radar()
        analytics = read_json(self.root/'data'/'analytics'/f'{TARGET}.json')
        previous = {**analytics, 'target_date':'2026-09-23'}
        previous['industries'][0]['amount'] = 100000000
        previous['industries'][0]['rank'] = 3
        path = self.root/'data'/'analytics'/'2026-09-23.json'
        write_json(path, previous)
        self.run_radar()
        payload = read_json(self.root/'data'/'daily'/TARGET/'ai-input.json')
        self.assertEqual(payload['comparison_date'],'2026-09-23')
        self.assertEqual(payload['industries'][0]['amount_change_percent'],100)
        self.assertEqual(payload['industries'][0]['rank_improvement'],2)
        previous['industry_generation'] = 'different-classification'
        write_json(path, previous)
        self.run_radar()
        payload = read_json(self.root/'data'/'daily'/TARGET/'ai-input.json')
        self.assertIsNone(payload['comparison_date'])
        self.assertIsNone(payload['industries'][0]['rank_improvement'])

    def test_duplicate_industry_members_block_only_industry_gate(self):
        def source(kind, params):
            if kind == 'industry_list':
                return [{'板块代码':x,'板块名称':x} for x in ('one','two')]
            return self.fetcher(kind,params)
        result=Radar(self.root,source,self.clock).run(TARGET,[],retries=0,interval=0)
        self.assertTrue(result['readiness']['MARKET_READY'])
        self.assertFalse(result['readiness']['INDUSTRY_READY'])

    def test_before_16_falls_back_to_history_when_quotes_unavailable(self):
        for hour, minute in ((8, 0), (10, 30), (15, 59)):
            with self.subTest(hour=hour), tempfile.TemporaryDirectory() as work:
                source = FakeFetcher()
                source.fail.add('market_batch')
                clock = lambda: datetime(2026, 9, 25, hour, minute, tzinfo=CST)
                result = Radar(Path(work),source,clock).run(None,[],retries=0,interval=0)
                self.assertEqual(result['mode'], 'PRE_MARKET')
                self.assertEqual(result['target_date'], TARGET)
                self.assertEqual(result['status'], 'READY')
                self.assertEqual(sum(kind == 'market_batch' for kind,_ in source.calls), 1)
                self.assertEqual(sum(kind == 'market_history' for kind,_ in source.calls), 2)

    def test_16_boundary_and_timezone(self):
        # 02:00 in Mexico City is 16:00 in Beijing.
        clock = lambda: datetime(2026,9,24,2,tzinfo=timezone(timedelta(hours=-6)))
        result = Radar(self.root,self.fetcher,clock).run(None,[],retries=0,interval=0)
        self.assertEqual(result['mode'], 'POST_MARKET')
        self.assertEqual(result['target_date'], TARGET)
        self.assertIn('market_batch', [kind for kind,_ in self.fetcher.calls])

    def test_premarket_reuses_saved_batches(self):
        self.run_radar()
        self.fetcher.calls.clear()
        clock = lambda: datetime(2026,9,28,10,tzinfo=CST)
        result = Radar(self.root,self.fetcher,clock).run(None,self.watchlist,retries=0,interval=0)
        self.assertEqual(result['status'], 'READY')
        self.assertFalse(any(kind.startswith('market_') for kind,_ in self.fetcher.calls))

    def test_history_budget_resumes_and_rejects_wrong_day(self):
        clock = lambda: datetime(2026,9,28,10,tzinfo=CST)
        self.fetcher.fail.add('market_batch')
        first = Radar(self.root,self.fetcher,clock).run(None,[],max_tasks=6,retries=0,interval=0)
        self.assertEqual(first['coverage']['valid_quotes'], 1)
        self.fetcher.calls.clear()
        second = Radar(self.root,self.fetcher,clock).run(None,[],retries=0,interval=0)
        self.assertEqual(second['status'], 'READY')
        histories = [params['code'] for kind,params in self.fetcher.calls if kind == 'market_history']
        self.assertEqual(histories, ['600001'])
        with tempfile.TemporaryDirectory() as work:
            def stale(kind,params):
                rows = self.fetcher(kind,params)
                if kind == 'market_history':
                    rows[0]['日期'] = '2026-09-23'
                return rows
            result = Radar(Path(work),stale,clock).run(None,[],retries=0,interval=0)
            self.assertFalse(result['readiness']['MARKET_READY'])
            self.assertEqual(result['coverage']['valid_quotes'], 0)

    def test_premarket_can_collect_still_dated_previous_close_in_batches(self):
        clock = lambda: datetime(2026,9,25,8,tzinfo=CST)
        result = Radar(self.root,self.fetcher,clock).run(None,[],retries=0,interval=0)
        self.assertEqual(result['mode'], 'PRE_MARKET')
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(sum(kind == 'market_batch' for kind,_ in self.fetcher.calls),1)
        self.assertFalse(any(kind == 'market_history' for kind,_ in self.fetcher.calls))

    def test_failed_history_cannot_starve_unattempted_codes(self):
        clock = lambda: datetime(2026,9,25,8,tzinfo=CST)
        self.fetcher.fail.add('market_batch')
        def source(kind,params):
            if kind == 'market_history' and params['code'] == '000001':
                self.fetcher.calls.append((kind,dict(params)))
                raise RuntimeError('persistent outage')
            return self.fetcher(kind,params)
        first = Radar(self.root,source,clock).run(None,[],max_tasks=6,retries=0,interval=0)
        self.assertEqual(first['coverage']['valid_quotes'],0)
        self.fetcher.calls.clear()
        second = Radar(self.root,source,clock).run(None,[],max_tasks=2,retries=0,interval=0)
        histories = [p['code'] for k,p in self.fetcher.calls if k == 'market_history']
        self.assertEqual(histories,['600001'])
        self.assertEqual(second['coverage']['valid_quotes'],1)

    def test_newer_live_date_is_rejected_before_history_fallback(self):
        clock = lambda: datetime(2026,9,25,10,tzinfo=CST)
        def source(kind,params):
            rows = self.fetcher(kind,params)
            if kind == 'market_batch':
                for row in rows:
                    row['日期'] = '2026-09-25'
            return rows
        result = Radar(self.root,source,clock).run(None,[],retries=0,interval=0)
        self.assertEqual(result['status'],'READY')
        self.assertEqual(sum(k == 'market_history' for k,_ in self.fetcher.calls),2)
        batches = self.root/'data'/'daily'/TARGET/'market-batches'
        self.assertEqual(list(batches.glob('*.json')),[])

    def test_incomplete_market_still_shows_verified_watchlist(self):
        self.fetcher.fail.add('market_batch')
        result = self.run_radar()
        self.assertEqual(result['status'],'INCOMPLETE')
        self.assertTrue(result['readiness']['WATCHLIST_READY'])
        status = (self.root/'reports'/'index.html').read_text(encoding='utf-8')
        self.assertIn('自选股 · 独立数据',status)
        self.assertIn('12.50',status)
        self.assertIn('已验证 0 / 2',status)
        self.assertNotIn('打开本次市场报告',status)

    def test_premarket_rejects_explicit_current_day(self):
        clock = lambda: datetime(2026,9,24,15,59,tzinfo=CST)
        with self.assertRaises(ValueError):
            Radar(self.root,self.fetcher,clock).run(TARGET,[],retries=0,interval=0)

    def test_ai_failure_does_not_block_core_and_output_is_escaped(self):
        for ai_result in ({'status':'FAILED','message':'API unavailable'},
                          {'status':'READY','model':'test','content':'<script>bad()</script>'}):
            with self.subTest(status=ai_result['status']), \
                    patch('deepseek_analysis.analyze',return_value=ai_result):
                result = self.run_radar(ai_settings={})
                self.assertEqual(result['status'],'READY')
                report = Path(result['report']).read_text(encoding='utf-8')
                self.assertIn('DeepSeek',report)
                self.assertNotIn('<script>bad()</script>',report)

    def test_incomplete_data_does_not_call_ai(self):
        self.fetcher.fail.add('market_batch')
        with patch('deepseek_analysis.analyze') as analyze:
            result = self.run_radar(ai_settings={})
            analyze.assert_not_called()
            self.assertEqual(result['ai']['status'],'WAITING_FOR_DATA')


if __name__ == "__main__":
    unittest.main()
