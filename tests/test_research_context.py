import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from storage import write_json
from research_context import build_context, collect_observations, context_digest, event_category, coverage_report


class ResearchContextTest(unittest.TestCase):
    def test_event_browser_categories_are_not_qualification_or_scores(self):
        self.assertEqual(event_category({'title':'霍尔木兹海峡原油出口'}),'国际')
        self.assertEqual(event_category({'title':'9月大宗商品价格指数上涨'}),'产业与经济')
        self.assertEqual(event_category({'title':'婚姻登记全国通办'}),'其他资讯')
        self.assertEqual(event_category({'title':'财政政策通知','source':'中国人民银行官网','excerpt':'正文'}),'国内官方政策')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.target='2026-09-30'
        self.payload={'target_date':self.target,'market':{'turnover_cny':300},
            'decision':{'funds':{'board_flows':[{'code':'BK1','name':'供应商行业','date':self.target,
                'net_cny':10,'source':'东方财富'}],'board_expected':1,'board_coverage_complete':True},
                'market':{},'stocks':[]},'enrichment':{'events':[]},'watchlist_quotes':[]}

    def flow(self,day,value):
        write_json(self.root/'data/daily'/day/'enrichment/flow-BK1.json',{
            'rows':[{'日期':day,'主力净流入-净额':value,'source':'东方财富'}]})

    def test_funds_require_exact_trading_days_and_preserve_zero(self):
        self.flow('2026-09-29',0)
        self.flow('2026-09-28',-5)
        row=build_context(self.root,self.payload)['funds']['boards'][0]
        self.assertEqual(row['net_cny'],10)
        self.assertEqual(row['windows']['3']['net_cny'],5)
        self.assertIsNone(row['windows']['5']['net_cny'])
        self.assertEqual(row['windows']['3']['positive_days'],1)
        self.assertEqual(row['windows']['5']['status'],'MISSING_DATA')

    def test_missing_middle_day_is_not_replaced_by_older_record(self):
        self.flow('2026-09-28',10);self.flow('2026-09-24',10)
        row=build_context(self.root,self.payload)['funds']['boards'][0]
        self.assertIsNone(row['windows']['3']['net_cny'])
        self.assertIn('2026-09-29',row['windows']['3']['missing_dates'])

    def test_conflicting_amounts_and_future_rows_are_not_used(self):
        self.flow(self.target,99);self.flow('2026-10-08',1000)
        row=build_context(self.root,self.payload)['funds']['boards'][0]
        self.assertIsNone(row['net_cny'])
        self.assertEqual(row['windows']['1']['status'],'CONFLICT')
        self.assertNotIn('2026-10-08',row['dates'])

    def test_stale_latest_news_is_separate_from_original_evidence(self):
        write_json(self.root/'data/daily'/self.target/'enrichment/latest-news.json',{'rows':[{
            'as_of':'2026-10-04T14:30:00+08:00','items':[
                {'time':'2026-10-04 14:00:00','date':'2026-10-04','title':'原油供应消息',
                 'source':'新浪财经','url':'https://finance.sina.com.cn/7x24'},
                {'time':'2026-10-04 14:00:00','date':'2026-10-04','title':'原油供应消息',
                 'source':'新浪财经','url':'https://finance.sina.com.cn/7x24'},
                {'time':'2026-10-04 15:00:00','date':'2026-10-04','title':'未来消息',
                 'source':'新浪财经','url':'https://finance.sina.com.cn/7x24'}]}]})
        before=copy.deepcopy(self.payload)
        historical=build_context(self.root,self.payload)
        self.assertFalse(historical['events']['supplemental_items'])
        current=build_context(self.root,self.payload,include_updates=True)
        self.assertEqual(len(current['events']['supplemental_items']),1)
        self.assertEqual(current['events']['supplemental_items'][0]['evidence_status'],'UNVERIFIED_REPORT')
        self.assertEqual(current['events']['as_of'],'2026-10-04T14:30:00+08:00')
        self.assertEqual(before,self.payload)
        self.assertNotEqual(context_digest(historical),context_digest(current))

    def test_corrupt_optional_context_does_not_hide_base_facts(self):
        file=self.root/'data/daily'/self.target/'enrichment/latest-news.json'
        file.parent.mkdir(parents=True);file.write_text('{broken',encoding='utf-8')
        context=build_context(self.root,self.payload,include_updates=True)
        self.assertTrue(context['data_errors'])
        self.assertEqual(context['funds']['boards'][0]['net_cny'],10)

    def test_structurally_damaged_optional_json_keeps_original_report_readable(self):
        for name,data in [('enrichment/flow-BK1.json',{'rows':None,'params':[]}),
                ('observations/SP500.json',{'rows':'damaged'}),
                ('observations/summary.json',{'statuses':[]})]:
            write_json(self.root/'data/daily'/self.target/name,data)
        context=build_context(self.root,self.payload,include_updates=True)
        self.assertGreaterEqual(len(context['data_errors']),3)
        self.assertEqual(context['funds']['boards'][0]['net_cny'],10)

    def test_turnover_requires_previous_trading_day_not_previous_report(self):
        write_json(self.root/'data/publications/2026-09-28.json',{
            'analysis':{'market':{'turnover_cny':100}}})
        self.assertIsNone(build_context(self.root,self.payload)['activity']['change_percent'])
        write_json(self.root/'data/publications/2026-09-29.json',{
            'analysis':{'market':{'turnover_cny':200}}})
        self.assertEqual(build_context(self.root,self.payload)['activity']['change_percent'],50)

    def test_stock_fund_conflict_and_price_divergence(self):
        self.payload['watchlist_quotes']=[{'code':'000001','name':'平安银行','涨跌幅':2}]
        write_json(self.root/'data/daily'/self.target/'flow-000001.json',{
            'rows':[{'日期':self.target,'主力净流入-净额':-10}]})
        row=build_context(self.root,self.payload)['funds']['stocks'][0]
        self.assertEqual(row['price_fund_relation'],'价涨净额为负')

    def test_current_coverage_separates_original_rows_and_amount_conflicts(self):
        self.flow(self.target,20)
        coverage=build_context(self.root,self.payload)['funds']['coverage']
        self.assertEqual(coverage,{'observed':0,'expected':1,'original_observed':1,'conflicted':1})

    def test_failed_sources_are_visible_for_margin_and_stock_funds(self):
        self.payload['watchlist_quotes']=[{'code':'000001','name':'平安银行'}]
        write_json(self.root/'data/daily'/self.target/'observations/summary.json',{'statuses':{
            'margin_sz':{'status':'FAILED','error':'深市接口连接失败'}}})
        write_json(self.root/'data/checkpoint.json',{'tasks':{
            'daily:2026-09-30:flow:000001':{'status':'FAILED','error':'个股接口超时'}}})
        funds=build_context(self.root,self.payload)['funds']
        self.assertEqual(funds['margin']['sz_message'],'深市接口连接失败')
        self.assertEqual(funds['stocks'][0]['message'],'个股接口超时')

    def test_margin_scope_and_etf_delta_do_not_become_cash_flow(self):
        self.payload['decision']['funds']['margin']={'balance_cny':20,'date':self.target,'change_cny':5}
        self.payload['decision']['funds']['etf_shares']=[{'code':'510300','name':'沪深300ETF','date':self.target,'shares':10}]
        write_json(self.root/'data/daily/2026-09-29/history/etf_shares.json',{
            'rows':[{'code':'510300','name':'沪深300ETF','date':'2026-09-29','shares':8}]})
        context=build_context(self.root,self.payload)
        self.assertIsNone(context['funds']['margin']['combined_balance_cny'])
        self.assertEqual(context['funds']['etfs'][0]['change_shares'],2)
        self.assertNotIn('cash_flow_cny',context['funds']['etfs'][0])

    def test_coverage_distinguishes_policy_materials_mapping_and_comparison(self):
        self.payload['decision']['market']['dimensions']=[{'id':'policy','score':None}]
        self.payload['enrichment']['events']=[{'date':self.target,'title':'官方政策','excerpt':'正文',
            'source':'中国人民银行官网','url':'https://www.pbc.gov.cn/policy.html'}]
        context=build_context(self.root,self.payload)
        report=coverage_report(self.payload,context)
        gaps={g['id']:g for g in report['gaps']}
        self.assertEqual(gaps['policy']['status'],'材料已取得')
        self.assertIn('本轮没有可采用',gaps['policy']['cause'])
        self.assertEqual(gaps['mapping']['status'],'分类不同')
        self.assertIn('股指5日走势和均线仍可判断',gaps['comparison']['impact'])
        self.assertNotIn('不能计算连续变化',context['activity']['message'])

    def test_repaired_etf_gap_disappears_and_diagnostics_do_not_invalidate_facts(self):
        self.payload['decision']['funds']['etf_shares']=[{'code':'510300','date':self.target,'shares':10}]
        before=build_context(self.root,self.payload)
        self.assertIn('etf',[g['id'] for g in coverage_report(self.payload,before)['gaps']])
        write_json(self.root/'data/daily/2026-09-29/history/etf_shares.json',{'rows':[
            {'code':'510300','date':'2026-09-29','shares':10}]})
        after=build_context(self.root,self.payload)
        self.assertEqual(coverage_report(self.payload,after)['etf_comparable'],1)
        self.assertNotIn('etf',[g['id'] for g in coverage_report(self.payload,after)['gaps']])
        self.assertNotEqual(context_digest(before),context_digest(after))
        after['funds']['margin']['sz_message']='改成新的网络失败原因'
        after['activity']['message']='改成精确范围的说明'
        self.assertEqual(context_digest(after),context_digest(build_context(self.root,self.payload)))

    def test_latest_margin_failure_is_shown_without_changing_missing_balance(self):
        write_json(self.root/'data/daily'/self.target/'observations/summary.json',{
            'as_of':'2026-10-07T07:00:00+08:00','statuses':{'margin_sz':{'error':'旧空记录'}}})
        write_json(self.root/'data/checkpoint.json',{'tasks':{
            f'observation:{self.target}:margin_sz:{self.target}':{
                'updated_at':'2026-10-07T14:00:00+08:00','error':'新连接中断'}}})
        margin=build_context(self.root,self.payload)['funds']['margin']
        self.assertEqual(margin['sz_message'],'新连接中断')
        self.assertIsNone(margin['sz_balance_cny'])

    def test_observation_collection_is_bounded_and_never_calls_model(self):
        from collector import Collector
        from datetime import datetime
        calls=[]
        def fetch(kind,params):
            calls.append((kind,params))
            if kind=='news_feed':return [{'as_of':params['as_of'],'items':[{
                'time':'2026-10-04 09:00:00','title':'国际事件','source':'新浪','url':'https://finance.sina.com.cn/7x24'}]}]
            return [{'date':'2026-09-30','close':100,'source':'原始来源'}]
        c=Collector(self.root,fetch,clock=lambda:datetime.fromisoformat('2026-10-04T14:00:00+08:00'))
        c.limit=3;c.retries=0;c.interval=0
        with patch('decision.analyze',side_effect=AssertionError('collector must not bill')):
            collect_observations(c,self.target)
        self.assertEqual(len(calls),3)
        self.assertTrue((self.root/'data/daily'/self.target/'observations/summary.json').exists())

    def test_completed_foreign_daily_rows_exclude_same_local_date(self):
        from akshare_worker import observation_rows
        rows=observation_rows([{'date':'2026-10-04','close':999},
            {'date':'2026-10-03','close':100}],{'cutoff':'2026-10-03','kind':'us'},'source')
        self.assertEqual(rows,[{'date':'2026-10-03','close':100.0,'source':'source'}])

    def test_yield_change_uses_basis_points_not_price_return(self):
        write_json(self.root/'data/daily'/self.target/'observations/summary.json',{'cutoff':self.target})
        write_json(self.root/'data/daily'/self.target/'observations/US10Y.json',{'rows':[
            {'date':'2026-09-29','close':4.1},{'date':self.target,'close':4.2}]})
        row=next(r for r in build_context(self.root,self.payload,include_updates=True)['international'] if r['code']=='US10Y')
        self.assertAlmostEqual(row['change_bp'],10)
        self.assertIsNone(row['change_percent'])



if __name__=='__main__':unittest.main()
