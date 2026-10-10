import unittest
import contextlib
import io
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from akshare_worker import fetch


class HistoryAdapterTest(unittest.TestCase):
    def test_successful_worker_keeps_fallback_diagnostic_in_parent_log(self):
        from collector import SubprocessFetcher
        from storage import write_json
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir()
            def worker(command,**kwargs):
                write_json(Path(command[-1]),[{'date':'2026-10-09','close':10.0}])
                return subprocess.CompletedProcess(command,0,'','primary source timeout; backup used')
            log=io.StringIO()
            with patch('collector.subprocess.run',side_effect=worker),contextlib.redirect_stderr(log):
                rows=SubprocessFetcher(root)('index',{'symbol':'sh000001'})
            self.assertEqual(rows[0]['close'],10.0)
            self.assertIn('primary source timeout; backup used',log.getvalue())
            self.assertEqual(list((root/'data').iterdir()),[])

    def test_primary_failure_is_logged_when_backup_history_succeeds(self):
        frame=pd.DataFrame([{'date':'2026-10-09','close':10.0}])
        log=io.StringIO()
        with patch('akshare.stock_zh_a_hist',side_effect=RuntimeError('primary unavailable')), \
                patch('akshare.stock_zh_a_hist_tx',return_value=frame),contextlib.redirect_stderr(log):
            rows=fetch('candidate_history',{'code':'600000','start_date':'20261001','end_date':'20261009'})
        self.assertEqual(rows[0]['close'],10.0)
        self.assertIn('腾讯',rows[0]['source'])
        self.assertIn('RuntimeError: primary unavailable',log.getvalue())

    def test_dated_unadjusted_history_preserves_amount_and_change(self):
        frame = pd.DataFrame([{'日期':'2026-09-24', '股票代码':'920001',
                               '收盘':10.0,'涨跌幅':2.3,'成交额':123456789.0}])
        with patch('akshare.stock_zh_a_hist',return_value=frame) as source:
            rows = fetch('market_history', {'code':'920001','name':'样本','target_date':'2026-09-24'})
            self.assertEqual(source.call_args.kwargs['adjust'],'')
            self.assertEqual(source.call_args.kwargs['start_date'],'20260924')
            self.assertEqual(rows[0]['代码'],'920001')
            self.assertEqual(rows[0]['成交额'],123456789.0)
            self.assertEqual(rows[0]['涨跌幅'],2.3)
            self.assertIn('Eastmoney',rows[0]['source'])

    def test_empty_history_is_not_synthesized(self):
        with patch('akshare.stock_zh_a_hist',return_value=pd.DataFrame()):
            with self.assertRaises(ValueError):
                fetch('market_history',{'code':'600671','name':'样本','target_date':'2026-09-24'})
