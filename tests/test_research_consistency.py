import copy
import unittest

from research_facts import validate_research_claims, validate_price_facts
from test_final_score_chain import model_response


class ResearchConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.request={'target_date':'2026-09-30','decision_rules':{'market':{'score':35.48,'action':'等待确认',
            'score_basis':'final-effective-v1'},'stocks':[]},'price_facts':{'stocks':{}},
            'policy_assessment':{'score':55},'facts':{'market':{'breadth_percent':46.06,'equal_weight_change_percent':-.11},
                'research:funds':{'margin':{'sz_balance_cny':None}},'research:events':{'original_items':[],'supplemental_items':[]}}}
        import json
        self.good=json.loads(model_response(self.request)['content'])

    def test_all_eight_previously_manual_corrections_are_automatically_rejected(self):
        cases=[('judgment','政策评分按样本口径尚未计入，不能补总分'),
            ('judgment','缺乏完整政策计分和连续量价验证'),
            ('opportunity_bias','但政策计分缺失和节后补数据限制追价'),
            ('fund_view','沪深融资余额仅上交所单日减少229.6亿元'),
            ('fund_view','ETF份额变动只能反映申购赎回，不代表现金方向'),
            ('event_view','但不覆盖改善型和存量贷款'),
            ('confidence_reason','政策计分缺失、前一交易日快照缺失，国际资产与资金均无法同口径确认'),
            ('trigger','单日上涨广度突破60%，政策评分恢复为正。')]
        for field,text in cases:
            with self.subTest(field=field,text=text):
                bad=copy.deepcopy(self.good)
                if field=='trigger':bad['market']['forecast']['scenarios']['偏强']['trigger']=text
                elif field=='confidence_reason':bad['market']['forecast'][field]=text
                else:bad['market'][field]=text
                with self.assertRaises(ValueError):validate_research_claims(bad,self.request)

    def test_wrong_date_number_sign_source_and_conclusion_are_rejected_without_rewriting(self):
        for error in ('date','number','sign','path','event','stance','prose_number'):
            with self.subTest(error=error):
                bad=copy.deepcopy(self.good)
                if error=='date':bad['target_date']='2026-10-07'
                if error=='number':bad['fact_claims'][1]['value']=93.7
                if error=='sign':bad['fact_claims'].append({'path':['facts','market','equal_weight_change_percent'],'value':.11})
                if error=='path':bad['fact_claims'].append({'path':['facts','missing'],'value':100})
                if error=='event':bad['event_research']=[{'event_id':'event:invented'}]
                if error=='stance':bad['market']['stance']='可考虑参与'
                if error=='prose_number':bad['market']['judgment']='上涨广度66.1%'
                before=copy.deepcopy(bad)
                with self.assertRaises(ValueError):validate_research_claims(bad,self.request)
                self.assertEqual(bad,before)

    def test_correct_scope_and_conditional_forecast_remain_accepted(self):
        self.good['market'].update(judgment='上涨广度46.1%，等权涨跌幅-0.11%。政策已完成评分。',
            fund_view='上交所融资余额减少。ETF份额变化不等于现金净流入。',event_view='不覆盖全部改善需求。')
        before=copy.deepcopy(self.good)
        validate_research_claims(self.good,self.request)
        self.assertEqual(self.good,before)

    def test_preliminary_policy_reference_is_valid_even_outside_bounded_news_list(self):
        self.request['evidence']={'event:policy':{'date':'2026-09-29'}}
        self.good['event_research']=[{'event_id':'event:policy','impact_path':'有条件支持'}]
        validate_research_claims(self.good,self.request)

    def test_previous_day_comparisons_check_both_dates(self):
        self.request['facts']['market'].update(breadth_percent=59.1488597593823,
            equal_weight_change_percent=.6083731370084396)
        self.request['facts']['research:comparison']={'available':True,'date':'2026-09-29',
            'market':{'breadth_percent':30.49021368288741,'equal_weight_change_percent':-1.0573478182797629}}
        for text in ('上涨广度30.5%升至59.1%，等权涨跌-1.06%转+0.61%。',
                '上涨广度从30.5%升至59.1%，等权涨跌由-1.06%转为+0.61%。',
                '前一交易日上涨广度30.5%，当日上涨广度59.1%。'):
            with self.subTest(text=text):
                self.good['market']['changes']=text
                before=copy.deepcopy(self.good)
                validate_research_claims(self.good,self.request)
                self.assertEqual(self.good,before)
        for text in ('上涨广度30.5%升至66.1%。','上涨广度35.5%升至59.1%。',
                '等权涨跌+1.06%转+0.61%。','等权涨跌-1.06%转-0.61%。',
                '前一交易日上涨广度35.5%，当日上涨广度59.1%。',
                '当日上涨广度30.5%。'):
            with self.subTest(text=text):
                self.good['market']['changes']=text
                with self.assertRaises(ValueError):validate_research_claims(self.good,self.request)
        self.good['market']['changes']='上涨广度30.5%升至59.1%。'
        del self.request['facts']['research:comparison']
        with self.assertRaises(ValueError):validate_research_claims(self.good,self.request)

    def test_conditional_percentages_do_not_hide_wrong_current_facts(self):
        self.good['market']['forecast']['scenarios']['偏强']['trigger']='如果上涨广度达到60%，则反弹延续。'
        validate_research_claims(self.good,self.request)
        self.good['market']['judgment']='若上涨广度达到60%，反弹可能延续；当前上涨广度66.1%。'
        with self.assertRaises(ValueError):validate_research_claims(self.good,self.request)

    def test_industry_index_statement_is_not_compared_with_stock_price(self):
        decision={'target_date':'2026-09-30','stocks':[{'code':'600019',
            'trend':{'close':5.93,'ma5':5.834,'ma20':5.821}}]}
        response={'stocks':[{'code':'600019','price_facts':{'date':'2026-09-30',
            'above_ma5':True,'above_ma20':True},
            'judgment':'收盘站上双均线，但行业指数仍低于20日线。'}]}
        validate_price_facts(response,decision,require=True)
        response['stocks'][0]['judgment']='股价低于20日线。'
        with self.assertRaises(ValueError):validate_price_facts(response,decision,require=True)
