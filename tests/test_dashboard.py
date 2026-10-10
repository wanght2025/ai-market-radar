import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch

from dashboard import Dashboard, watchlist_rows, saved_analysis
from research_brief import input_digest
from storage import write_json, read_json, workspace_lock


class DashboardTest(unittest.TestCase):
    def test_first_use_ai_setup_saves_enables_and_tests_without_exposing_key(self):
        import os
        from unittest.mock import Mock
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':''}):
            write_json(self.root/'config.json',{'watchlist':[],'deepseek':None})
            with self.request('/api/ai-config',{'key':'synthetic-key','enabled':True},**{'X-Radar-Token':self.server.token}) as response:
                result=json.load(response)
            self.assertTrue(result['ok'])
            self.assertNotIn('synthetic-key',json.dumps(result))
            self.assertEqual(read_json(self.root/'config.json')['deepseek']['max_tokens'],32768)
            with self.request('/api/status') as response:state=json.load(response)
            self.assertTrue(state['ai_settings']['enabled'])
            self.assertTrue(state['ai_settings']['key_configured'])
            self.assertNotIn('synthetic-key',json.dumps(state))
            upstream=Mock(status_code=200);upstream.json.return_value={'data':[{'id':'deepseek-v4-pro'}]}
            with patch('deepseek_analysis.requests.get',return_value=upstream),patch('deepseek_analysis.requests.post',side_effect=AssertionError('No model generation')):
                with self.request('/api/ai-test',{},**{'X-Radar-Token':self.server.token}) as response:
                    self.assertTrue(json.load(response)['ok'])

    def test_reading_snapshot_stays_complete_while_update_changes_files(self):
        daily=self.publish_analysis()
        self.server.pin_reading()
        old=self.server.reading_snapshots['2026-09-30']['research_context']
        self.server.process=object()
        try:
            write_json(daily/'observations/news.json',{'rows':[{'as_of':'2026-10-04T14:00:00+08:00','items':[
                {'date':'2026-10-04','time':'2026-10-04T13:00:00+08:00','title':'更新中的消息','url':'https://example.test/news'}]}]})
            with self.request('/api/analysis?date=2026-09-30') as response: pinned=json.load(response)
            self.assertEqual(pinned['research_context'],old)
        finally:self.server.process=None
        with self.request('/api/analysis?date=2026-09-30') as response: completed=json.load(response)
        self.assertNotEqual(completed['research_context'],old)

    def test_update_mode_is_validated_and_passed_to_launcher(self):
        with patch.object(self.server,'start_run') as start:
            with self.request('/api/run',{'mode':'supplement','date':'2026-09-30'},**{'X-Radar-Token':self.server.token}):pass
            start.assert_called_once_with('2026-09-30',False,mode='supplement')
        for body in ({'mode':'unknown'},{'mode':'replay','date':''}):
            with self.assertRaises(HTTPError) as error:self.request('/api/run',body,**{'X-Radar-Token':self.server.token})
            self.assertEqual(error.exception.code,400)

    def test_enable_automatic_running_installs_task_before_saving_preference(self):
        with patch.object(self.server,'install_schedule',create=True) as install:
            with self.request('/api/schedule',{'enabled':True},**{'X-Radar-Token':self.server.token}):pass
        install.assert_called_once_with()
        self.assertTrue(read_json(self.root/'config.json')['schedule']['enabled'])
        with patch.object(self.server,'install_schedule',side_effect=ValueError('fixture installation failure'),create=True):
            write_json(self.root/'config.json',{'watchlist':[],'schedule':{'enabled':False}})
            with self.assertRaises(HTTPError):
                self.request('/api/schedule',{'enabled':True},**{'X-Radar-Token':self.server.token})
        self.assertFalse(read_json(self.root/'config.json')['schedule']['enabled'])

    def test_forecast_status_get_is_read_only_and_does_not_bill(self):
        self.publish_analysis()
        with patch('decision.analyze',side_effect=AssertionError('GET must never bill')):
            with self.request('/api/status') as response:data=json.load(response)
        self.assertEqual(data['forecast']['status'],'RECENT_RESEARCH')
        self.assertFalse(list((self.root/'data/forward-forecasts').glob('*.json')))

    def test_failed_ai_attempt_is_visible_without_replacing_last_valid_review(self):
        target='2026-09-30'
        write_json(self.root/'data/publications'/f'{target}.json',{'analysis':{'target_date':target,
            'decision':{'market':{},'stocks':[],'sectors':[]},'decision_evidence':{}}})
        write_json(self.root/'data/daily'/target/'ai-review-attempt.json',{'status':'FAILED',
            'message':'未返回完整研究','finish_reason':'length','usage':{'total_tokens':20},
            'content':'模型原始私密输出','research_context':{'private':'不应返回'}})
        with self.request('/api/analysis?date='+target) as response:data=json.load(response)
        self.assertEqual(data['ai_review_attempt']['finish_reason'],'length')
        self.assertNotIn('content',data['ai_review_attempt'])
        self.assertNotIn('research_context',data['ai_review_attempt'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_json(self.root/'config.json', {'watchlist': [], 'deepseek': {'model': 'test'}})
        (self.root/'dashboard.html').write_text('dashboard', encoding='utf-8')
        (self.root/'dashboard.css').write_text('body{}', encoding='utf-8')
        (self.root/'dashboard.js').write_text('const radar=1;', encoding='utf-8')
        (self.root/'dashboard-visuals.js').write_text('const visual=1;', encoding='utf-8')
        self.server = Dashboard(('127.0.0.1', 0), self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, data=None, **headers):
        request = Request(self.url+path, data=json.dumps(data).encode() if data is not None else None,
                          headers=headers)
        return urlopen(request, timeout=3)

    def test_write_requires_token_and_rejects_foreign_origin(self):
        for headers in ({}, {'X-Radar-Token':self.server.token,'Origin':'https://example.com'})*10:
            with self.assertRaises(HTTPError) as error:
                self.request('/api/watchlist', {'text':'000001'}, **headers)
            self.assertEqual(error.exception.code,403)
        self.assertEqual(read_json(self.root/'config.json')['watchlist'],[])

    def test_damaged_archive_html_returns_explicit_error_without_changing_file(self):
        path=self.root/'data/publications/2026-09-30.json'
        for value in ([], ['broken'], None, {'html':None}, {'html':17}, {'html':''}, {'analysis':{}}):
            with self.subTest(value=value):
                write_json(path,value)
                before=path.read_bytes()
                with self.assertRaises(HTTPError) as error:
                    self.request('/reports/market-2026-09-30.html')
                self.assertEqual(error.exception.code,500)
                self.assertIn('损坏',json.load(error.exception)['error'])
                self.assertEqual(path.read_bytes(),before)

    def test_corrupt_optional_files_and_config_leave_core_report_readable(self):
        daily=self.publish_analysis()
        for name in ('research-brief.json','ai-review.json','opportunity-snapshot.json'):
            (daily/name).write_text('{broken',encoding='utf-8')
        (self.root/'config.json').write_text('{broken',encoding='utf-8')
        with self.request('/api/analysis?date=2026-09-30') as response:
            data=json.load(response)
        self.assertEqual(data['target_date'],'2026-09-30')
        self.assertTrue(data['data_errors'])
        with self.request('/api/status') as response:
            data=json.load(response)
        self.assertTrue(data['config_error'])

    def test_supplement_version_changes_without_new_collection(self):
        daily=self.publish_analysis()
        with self.request('/api/status') as response: before=json.load(response)
        write_json(daily/'ai-review.json',{'status':'FAILED'})
        with self.request('/api/status') as response: after=json.load(response)
        self.assertNotEqual(before['analysis_versions'],after['analysis_versions'])

    def test_explicit_ai_retry_is_post_only(self):
        self.publish_analysis()
        with patch.object(self.server,'start_ai_retry') as start:
            with self.request('/api/ai-retry',{'date':'2026-09-30'},**{'X-Radar-Token':self.server.token}): pass
            start.assert_called_once_with('2026-09-30')
        with patch('decision.analyze',side_effect=AssertionError('GET must never bill')):
            with self.request('/api/analysis?date=2026-09-30'): pass

    def test_observation_update_is_post_only_and_get_reads_without_billing(self):
        self.publish_analysis()
        with patch.object(self.server,'start_observations',create=True) as start:
            with self.request('/api/observations',{'date':'2026-09-30'},**{'X-Radar-Token':self.server.token}):pass
            start.assert_called_once_with('2026-09-30')
        with patch('decision.analyze',side_effect=AssertionError('no GET billing')):
            with self.request('/api/analysis?date=2026-09-30') as response:data=json.load(response)
        self.assertIn('research_context',data)


    def test_second_dashboard_cannot_share_a_listening_port(self):
        try:
            duplicate=Dashboard(('127.0.0.1',self.server.server_port),self.root)
        except OSError:
            return
        duplicate.server_close()
        self.fail('第二个工作台不能共享同一监听端口')

    def test_bad_optional_structure_is_reported_without_hiding_publication(self):
        daily=self.publish_analysis()
        from refresh_ai_review import publication_digest
        published=read_json(self.root/'data/publications/2026-09-30.json')
        write_json(daily/'opportunity-snapshot.json',{'publication_digest':publication_digest(published),
                                                   'opportunities':'broken'})
        with self.request('/api/analysis?date=2026-09-30') as response:
            data=json.load(response)
        self.assertNotIn('opportunities',data)
        self.assertTrue(data['data_errors'])

    def test_original_ai_explanation_is_marked_stale_when_evidence_changes(self):
        self.publish_analysis()
        path=self.root/'data/publications/2026-09-30.json'
        published=read_json(path)
        published['analysis']['decision']={'market':{},'stocks':[],
            'ai':{'status':'READY','business_status':'PASSED','evidence_digest':'old',
                  'interpretation':{'market':{}}}}
        write_json(path,published)
        before=path.read_bytes()
        with self.request('/api/analysis?date=2026-09-30') as response: data=json.load(response)
        self.assertEqual(data['decision']['ai']['evidence_status'],'STALE')
        self.assertEqual(path.read_bytes(),before)

    def test_watchlist_outside_model_scope_does_not_stale_fresh_research_but_scoped_fund_changes_do(self):
        from research_context import build_context,context_digest
        from participation import snapshot,evidence_digest
        target='2026-09-30';path=self.root/'data/publications'/f'{target}.json'
        payload={'target_date':target,'candidates':[{'代码':'000001','名称':'研究对象','涨跌幅':1}],
            'decision':{'market':{},'stocks':[],'sectors':[]},'decision_evidence':{}}
        flow=self.root/f'data/daily/{target}/flow-000001.json'
        write_json(flow,{'rows':[{'日期':target,'主力净流入-净额':10}]})
        context=build_context(self.root,payload,include_updates=True)
        payload['decision']['ai']={'status':'READY','business_status':'PASSED','interpretation':{'market':{}},
            'evidence_digest':evidence_digest(snapshot(self.root,payload)),
            'context_digest':context_digest(context),'context_stock_codes':['000001']}
        payload['watchlist_quotes']=[{'code':'600000','name':'独立自选','涨跌幅':2}]
        write_json(path,{'analysis':payload});before=path.read_bytes()
        with patch('decision.analyze',side_effect=AssertionError('GET cannot call model')):
            with self.request('/api/analysis?date='+target) as response:initial=json.load(response)
            self.assertNotEqual(initial['decision']['ai'].get('evidence_status'),'STALE')
            payload['decision']['ai'].pop('context_stock_codes')
            payload['research_context']=context
            write_json(path,{'analysis':payload});before=path.read_bytes()
            with self.request('/api/analysis?date='+target) as response:bound=json.load(response)
            self.assertNotEqual(bound['decision']['ai'].get('evidence_status'),'STALE')
            write_json(flow,{'rows':[{'日期':target,'主力净流入-净额':20}]})
            with self.request('/api/analysis?date='+target) as response:changed=json.load(response)
        self.assertEqual(changed['decision']['ai'].get('evidence_status'),'STALE')
        self.assertEqual(path.read_bytes(),before)

    def test_original_ai_becomes_stale_when_only_news_context_changes(self):
        from participation import snapshot, evidence_digest
        from research_context import build_context, context_digest
        daily=self.publish_analysis()
        path=self.root/'data/publications/2026-09-30.json'
        published=read_json(path);payload=published['analysis']
        payload['decision']={'market':{},'stocks':[],'sectors':[]}
        payload['decision']['ai']={'status':'READY','business_status':'PASSED',
            'interpretation':{'market':{}},'evidence_digest':evidence_digest(snapshot(self.root,payload)),
            'context_digest':context_digest(build_context(self.root,payload,include_updates=True))}
        write_json(path,published);before=path.read_bytes()
        with patch('decision.analyze',side_effect=AssertionError('GET must never call a model')):
            with self.request('/api/analysis?date=2026-09-30') as response: initial=json.load(response)
            self.assertNotEqual(initial['decision']['ai'].get('evidence_status'),'STALE')
            write_json(daily/'enrichment/latest-news.json',{'rows':[{
                'as_of':'2026-10-04T14:00:00+08:00','items':[{'date':'2026-10-04',
                    'time':'2026-10-04 13:00:00','title':'国际事件更新','source':'新浪财经',
                    'url':'https://finance.sina.com.cn/7x24'}]}]})
            with self.request('/api/analysis?date=2026-09-30') as response: changed=json.load(response)
        self.assertEqual(initial['research_evidence'],changed['research_evidence'])
        self.assertEqual(changed['decision']['ai'].get('evidence_status'),'STALE')
        self.assertEqual(path.read_bytes(),before)

    def test_save_validates_and_preserves_model(self):
        with self.request('/api/watchlist', {'text':'920001 北交样本'}, **{'X-Radar-Token':self.server.token}) as response:
            self.assertEqual(response.status,200)
        config = read_json(self.root/'config.json')
        self.assertEqual(config['watchlist'][0]['market'],'bj')
        self.assertEqual(config['deepseek'],{'model':'test'})
        for value in ('000001 A\n000001 B','../.env','6000000', 'abc'):
            with self.assertRaises(ValueError):
                watchlist_rows(value)

    def test_sensitive_files_and_host_rejected(self):
        for path in ('/.env','/config.json','/reports/../.env','/api/../../config.json'):
            with self.assertRaises(HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code,404)
        with self.assertRaises(HTTPError) as error:
            self.request('/api/status', **{'Host':'attacker.example'})
        self.assertEqual(error.exception.code,403)

    def test_frontend_assets_use_explicit_allowlist(self):
        for path, kind in (('/dashboard.css','text/css'),('/dashboard.js','text/javascript')):
            with self.request(path) as response:
                self.assertTrue(response.headers['Content-Type'].startswith(kind))
        for path in ('/storage.py','/dashboard.py','/../dashboard.js'):
            with self.assertRaises(HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code,404)

    def test_publication_metadata_comes_from_frozen_report(self):
        self.publish_analysis()
        path=self.root/'data/publications/2026-09-30.json'
        saved=read_json(path)
        saved['generated_at']='2026-10-04T14:30:00+08:00'
        saved['required']=[{'name':'完整资金','ok':True}]
        write_json(path,saved)
        original=path.read_bytes()
        write_json(self.root/'data/last_run.json',{'generated_at':'later-failure','status':'INCOMPLETE'})
        with self.request('/api/analysis?date=2026-09-30') as response:
            data=json.load(response)
        self.assertEqual(data['publication']['generated_at'],saved['generated_at'])
        self.assertEqual(data['publication']['status'],'PUBLISHED')
        self.assertNotIn('publication',saved_analysis(self.root,'2026-09-30'))
        self.assertEqual(path.read_bytes(),original)

    def test_cli_lock_prevents_config_changes(self):
        with workspace_lock(self.root), self.assertRaises(HTTPError) as error:
            self.request('/api/watchlist', {'text':'000001'}, **{'X-Radar-Token':self.server.token})
        self.assertEqual(error.exception.code,409)
        self.assertEqual(read_json(self.root/'config.json')['watchlist'],[])

    def test_run_and_double_start(self):
        with patch.object(self.server, 'start_run') as start:
            with self.request('/api/run', {'date':'2026-09-28'}, **{'X-Radar-Token':self.server.token}):
                pass
            start.assert_called_once_with('2026-09-28',False)
        with patch.object(self.server, 'process') as process:
            process.poll.return_value = None
            with self.assertRaises(HTTPError) as error:
                self.request('/api/run', {}, **{'X-Radar-Token':self.server.token})
            self.assertEqual(error.exception.code,409)

    def publish_analysis(self):
        daily = self.root/'data'/'daily'/'2026-09-30'
        write_json(daily/'ai-input.json', {'target_date':'2026-09-30',
            'watchlist':[['000001','存档名称',11.57,125000000]],
            'industries':[{'code':'801780','name':'银行'}],
            'candidates':[{'代码':'000001','industry':{'code':'801780','name':'银行'}}],
            'watchlist_quotes':[{'code':'000001','name':'存档名称','收盘':11.57,'涨跌幅':1.94,'net_flow_cny':125000000}]})
        write_json(daily/'market.json', {'rows':[{'代码':'000001','涨跌幅':1.94,'收盘':11.57}]})
        write_json(self.root/'data'/'analytics'/'2026-09-30.json', {'industry_generation':'saved-version'})
        write_json(self.root/'data'/'snapshots'/'industry-sw'/'saved-version'/'members'/'801780.json',
                   {'rows':[{'code':'000001','name':'银行'}]})
        (self.root/'reports').mkdir(exist_ok=True)
        (self.root/'reports'/'market-2026-09-30.html').write_text('published')
        write_json(self.root/'data/publications/2026-09-30.json',
                   {'analysis':read_json(daily/'ai-input.json'),'html':'published'})
        return daily

    def test_analysis_uses_saved_watchlist_and_classification(self):
        self.publish_analysis()
        write_json(self.root/'config.json', {'watchlist':[{'code':'600000','name':'新名单'}]})
        write_json(self.root/'data'/'snapshots'/'industry-sw'/'latest.json', {'generation':'different-version'})
        write_json(self.root/'data/daily/2026-09-30/market.json',{'rows':[{'代码':'000001','涨跌幅':999}]})
        with self.request('/api/analysis?date=2026-09-30') as response:
            data = json.load(response)
        self.assertEqual(data['watchlist_quotes'][0]['code'],'000001')
        self.assertEqual(data['watchlist_quotes'][0]['涨跌幅'],1.94)
        self.assertEqual(data['candidates'][0]['industry'],{'code':'801780','name':'银行'})
        with self.request('/api/status') as response:
            self.assertEqual(json.load(response)['analysis_dates'],['2026-09-30'])

    def test_unpublished_or_mismatched_analysis_rejected(self):
        daily = self.publish_analysis()
        (self.root/'data/publications/2026-09-30.json').unlink()
        with self.assertRaises(HTTPError) as error:
            self.request('/api/analysis?date=2026-09-30')
        self.assertEqual(error.exception.code,404)

    def test_brief_must_match_current_snapshot_and_never_calls_model(self):
        daily = self.publish_analysis()
        original = saved_analysis(self.root, '2026-09-30')
        brief = {'status': 'READY', 'target_date': '2026-09-30', 'input_digest': input_digest(original), 'brief': {}}
        write_json(daily/'research-brief.json', brief)
        with patch('research_brief.analyze') as call:
            self.assertIn('research_brief', saved_analysis(self.root, '2026-09-30'))
            call.assert_not_called()
        raw = read_json(daily/'ai-input.json')
        write_json(daily/'ai-input.json', {**raw, 'mode': 'CHANGED'})
        self.assertIn('research_brief', saved_analysis(self.root, '2026-09-30'))
        publication=self.root/'data/publications/2026-09-30.json'
        saved=read_json(publication)
        saved['analysis']['mode']='CHANGED'
        write_json(publication,saved)
        self.assertNotIn('research_brief', saved_analysis(self.root, '2026-09-30'))
 
    def test_mismatched_snapshot_date_rejected(self):
        daily = self.publish_analysis()
        (self.root/'reports'/'market-2026-09-30.html').write_text('published')
        write_json(self.root/'data/publications/2026-09-30.json', {'analysis':{'target_date':'2026-09-28'}})
        with self.assertRaises(HTTPError) as error:
            self.request('/api/analysis?date=2026-09-30')
        self.assertEqual(error.exception.code,404)

    def test_malformed_publication_shape_returns_explicit_error(self):
        path=self.root/'data/publications/2026-09-30.json'
        for value in ([],{'analysis':[]},{'analysis':None}):
            with self.subTest(value=value):
                write_json(path,value)
                with self.assertRaises(HTTPError) as error:self.request('/api/analysis?date=2026-09-30')
                self.assertEqual(error.exception.code,500)
                self.assertIn('损坏',json.loads(error.exception.read())['error'])

    def test_published_html_survives_missing_or_overwritten_working_report(self):
        self.publish_analysis()
        report=self.root/'reports/market-2026-09-30.html'
        for content in ('unfinished draft',None):
            if content is None:report.unlink()
            else:report.write_text(content)
            with self.request('/reports/market-2026-09-30.html') as response:
                self.assertEqual(response.read(),b'published')

    def test_legacy_ai_test_is_hidden_and_redirects_without_deleting_archive(self):
        self.publish_analysis()
        report=self.root/'reports/deepseek-test-2026-09-30.html'
        report.write_text('old API acceptance sample',encoding='utf-8')
        before=report.read_bytes()
        with patch('decision.analyze',side_effect=AssertionError('reading must not call model')):
            with self.request('/api/status') as response:
                reports=json.load(response)['reports']
            self.assertNotIn(report.name,reports)
            self.assertIn('market-2026-09-30.html',reports)
            with self.request('/reports/'+report.name) as response:
                self.assertEqual(response.geturl(),self.url+'/?view=overview')
                self.assertEqual(response.read(),b'dashboard')
        self.assertEqual(report.read_bytes(),before)

    def test_analysis_date_cannot_read_arbitrary_files(self):
        for query in ('', '?date=', '?date=../../config', '?date=2026-02-30',
                      '?date=2026-09-30&date=2026-09-28'):
            with self.assertRaises(HTTPError) as error:
                self.request('/api/analysis'+query)
            self.assertEqual(error.exception.code,400)

    def test_ai_review_must_match_frozen_publication(self):
        from refresh_ai_review import publication_digest
        self.publish_analysis()
        publication=read_json(self.root/'data/publications/2026-09-30.json')
        record={'status':'READY','publication_digest':publication_digest(publication),'interpretation':{'summary':'test'}}
        path=self.root/'data/daily/2026-09-30/ai-review.json'
        write_json(path,record)
        with self.request('/api/analysis?date=2026-09-30') as response:
            self.assertIn('ai_review',json.load(response))
        record['publication_digest']='different'
        write_json(path,record)
        with self.request('/api/analysis?date=2026-09-30') as response:
            self.assertNotIn('ai_review',json.load(response))
        self.assertNotIn('ai_review',saved_analysis(self.root,'2026-09-30'))

    def test_opportunity_supplement_requires_matching_publication(self):
        from refresh_ai_review import publication_digest
        daily=self.publish_analysis()
        publication=read_json(self.root/'data/publications/2026-09-30.json')
        record={'publication_digest':publication_digest(publication),'opportunities':{'version':'opportunity-v1'},
                'evidence':{'opportunities':{'date':'2026-09-30'}}}
        write_json(daily/'opportunity-snapshot.json',record)
        with self.request('/api/analysis?date=2026-09-30') as response:
            data=json.load(response)
        self.assertEqual(data['opportunities']['version'],'opportunity-v1')
        self.assertIn('opportunities',data['evidence'])
        record['publication_digest']='different'
        write_json(daily/'opportunity-snapshot.json',record)
        with self.request('/api/analysis?date=2026-09-30') as response:
            data=json.load(response)
        self.assertNotIn('opportunities',data)
        self.assertNotIn('opportunities',data['evidence'])
