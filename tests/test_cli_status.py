import contextlib
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import run
from collector import CST
from scheduler import tick
from storage import write_json


class CliStatusTest(unittest.TestCase):
    def test_collection_exit_code_distinguishes_failure_and_allowed_degradation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);write_json(root/'config.json',{})
            for data,ai,expected in (('INCOMPLETE','WAITING_FOR_DATA',1),('READY','FAILED',1),
                    ('READY','READY',0),('READY','DISABLED',0),('READY','NOT_CONFIGURED',0),
                    ('READY','BUDGET_EXCEEDED',0)):
                with self.subTest(data=data,ai=ai),patch.object(run,'ROOT',root), \
                        patch('sys.argv',['run.py']),patch('run.Radar') as radar, \
                        contextlib.redirect_stdout(io.StringIO()):
                    radar.return_value.run.return_value={'target_date':'2026-09-30','mode':'POST_MARKET',
                        'status':data,'requests_this_run':1,'attempts_this_run':1,'readiness':{},
                        'ai':{'status':ai}}
                    self.assertEqual(run.main(),expected)

    def test_post_market_model_failure_is_not_automatically_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);now=datetime(2026,9,30,18,tzinfo=CST)
            write_json(root/'config.json',{'schedule':{'enabled':True}})
            write_json(root/'data/last_run.json',{'target_date':'2026-09-30','status':'READY','ai':{'status':'FAILED'}})
            with patch('scheduler.subprocess.run',return_value=Mock(returncode=1)) as process:
                state=tick(root,now)
                self.assertEqual(state['slots']['2026-09-30:post']['status'],'NEEDS_ATTENTION')
                tick(root,datetime(2026,9,30,19,tzinfo=CST))
                self.assertEqual(process.call_count,1)

    def test_ai_cli_reports_business_failure(self):
        import refresh_ai_review
        import refresh_entry_brief
        for module,function in ((refresh_ai_review,'refresh_review'),(refresh_entry_brief,'refresh')):
            for status,expected in (('FAILED',1),('READY',0),('DISABLED',0)):
                with self.subTest(module=module.__name__,status=status), \
                        patch('sys.argv',[module.__name__+'.py','--date','2026-09-30']), \
                        patch.object(module,'workspace_lock'),patch.object(module,function,return_value={'status':status}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main(),expected)
