import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from decision import interpret, validate_ai, validate_visual_ai
from refresh_ai_review import refresh_review, publication_digest
from storage import read_json, write_json


class VisualAiTest(unittest.TestCase):
    def test_model_event_samples_are_bounded_without_duplicating_news(self):
        from research_context import build_context
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30'
            payload={'target_date':target,'decision':{'market':{'dimensions':[]},'stocks':[],'sectors':[]},
                'enrichment':{'events':[{'title':f'产业报道{i}','date':target,'time':target,
                    'url':'https://example.com/news','excerpt':'正文'*2000} for i in range(60)]}}
            payload['research_context']=build_context(root,payload,include_updates=True)
            before=copy.deepcopy(payload)
            refs={'market':{'title':'市场','date':target,'facts':{}}}
            with patch('decision.analyze',return_value={'status':'NOT_CONFIGURED'}) as model:
                interpret(root,payload,copy.deepcopy(payload['decision']),refs,None,save=False)
            request=model.call_args.args[1];facts=request['facts']
            self.assertLessEqual(len(facts['research:events']['original_items']),8)
            self.assertFalse(any(k.startswith('event:') for k in facts))
            self.assertEqual(facts['events']['scope'],'正文与标题在research:events；不重复发送')
            self.assertLess(len(json.dumps(request,ensure_ascii=False)),25000)
            self.assertEqual(payload,before)

    def test_supplemental_input_contains_news_fund_amounts_and_time_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30'
            payload={'target_date':target,'decision':{'market':{},'stocks':[],'sectors':[]},
                'enrichment':{'events':[]},'market':{},'decision_evidence':{'market':{'facts':{}}}}
            write_json(root/'data/publications'/f'{target}.json',{'analysis':payload})
            write_json(root/'data/daily'/target/'enrichment/latest-news.json',{'rows':[{
                'as_of':'2026-10-04T14:00:00+08:00','items':[{'date':'2026-10-04',
                    'time':'2026-10-04 13:00:00','title':'国际产业事件','source':'新浪财经',
                    'url':'https://finance.sina.com.cn/7x24'}]}]})
            with patch('refresh_ai_review.interpret',return_value={'status':'NOT_CONFIGURED'}) as model:
                refresh_review(root,target)
            input=model.call_args.args[1]
            self.assertEqual(input['research_context']['mode'],'SUPPLEMENTAL_RESEARCH')
            self.assertEqual(input['research_context']['events']['supplemental_items'][0]['title'],'国际产业事件')
            self.assertIn('research:funds',model.call_args.args[3])

    def valid(self):
        return {'schema_version':'decision-ui-v1','target_date':'2026-09-30',
                'summary':'等待','counterevidence':'证据有限',
                'market':{'stance':'等待确认','headline':'等待条件', 'judgment':'等待',
                          'confirmation':'观察确认','risk':'缺证据','source_ids':['market']},
                'sectors':[],'stocks':[]}

    def test_new_research_cannot_reuse_legacy_output_as_qualified_cache(self):
        from research_context import build_context
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30'
            decision={'target_date':target,'market':{'dimensions':[]},'stocks':[],'sectors':[]}
            payload={'target_date':target,'decision':decision}
            payload['research_context']=build_context(root,payload,include_updates=True)
            response={'status':'READY','transport_status':'SUCCEEDED','cache_key':'legacy-research',
                'content':json.dumps(self.valid(),ensure_ascii=False)}
            refs={'market':{'title':'市场','date':target,'facts':{}}}
            with patch('decision.analyze',return_value=response):
                result=interpret(root,payload,copy.deepcopy(decision),refs,{},save=False)
            self.assertEqual(result['status'],'FAILED')
            self.assertEqual(result['transport_status'],'SUCCEEDED')
            self.assertEqual(result['business_status'],'FAILED')
            self.assertEqual(read_json(root/'data/ai/legacy-research.json')['status'],'FAILED')
            # The legacy reader still accepts saved historical explanations.
            validate_visual_ai(self.valid())

    def test_original_research_records_the_context_it_actually_used(self):
        from research_context import build_context, context_digest
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30'
            decision={'target_date':target,'market':{'dimensions':[]},'stocks':[],'sectors':[]}
            payload={'target_date':target,'decision':decision}
            payload['research_context']=build_context(root,payload,include_updates=True)
            data=self.valid();data['schema_version']='decision-ui-v3';data['event_research']=[]
            data['market'].update(fund_view='资金不足',international_view='国际样本不足',
                event_view='事件有限',changes='缺少前一日，不能比较')
            refs={'market':{'title':'市场','date':target,'facts':{}}}
            with patch('decision.analyze',return_value={'status':'READY','content':json.dumps(data)}):
                result=interpret(root,payload,decision,refs,{},save=True)
            self.assertEqual(result['business_status'],'PASSED')
            self.assertEqual(result.get('context_digest'),context_digest(payload['research_context']))
            self.assertEqual(read_json(root/'data/daily'/target/'decision.json')['ai']['context_digest'],
                context_digest(payload['research_context']))

    def test_v3_requires_research_beyond_short_market_headline(self):
        data=self.valid();data['schema_version']='decision-ui-v3'
        with self.assertRaises(ValueError):validate_visual_ai(data)
        data['market'].update(fund_view='资金方向及缺口',international_view='国际变量影响路径',
            event_view='事件影响',changes='缺上一交易日，无法比较')
        data['event_research']=[]
        validate_visual_ai(data)
        data['market']['fund_view']=''
        with self.assertRaises(ValueError):validate_visual_ai(data)

    def test_unmatched_optional_event_is_isolated_without_weakening_market_sources(self):
        from research_context import build_context
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30'
            decision={'target_date':target,'market':{'dimensions':[]},'stocks':[],'sectors':[]}
            payload={'target_date':target,'decision':decision}
            payload['research_context']=build_context(root,payload,include_updates=True)
            data=self.valid();data['schema_version']='decision-ui-v3'
            data['market'].update(fund_view='资金不足',international_view='国际样本不足',event_view='事件有限',changes='缺比较日')
            data['event_research']=[{'event_id':'event:unknown','impact_path':'不可匹配','counterevidence':'缺口','next_check':'核对'}]
            content=json.dumps(data,ensure_ascii=False)
            refs={'market':{'title':'市场','date':target,'facts':{}}}
            with patch('decision.analyze',return_value={'status':'READY','content':content}):
                result=interpret(root,payload,copy.deepcopy(decision),copy.deepcopy(refs),{},save=False)
            self.assertEqual(result['status'],'FAILED')
            self.assertIn('引用对象不存在',result['validation_error'])
            self.assertEqual(result['content'],content)
            data['market']['source_ids']=['event:unknown']
            with patch('decision.analyze',return_value={'status':'READY','content':json.dumps(data)}):
                invalid=interpret(root,payload,copy.deepcopy(decision),copy.deepcopy(refs),{},save=False)
            self.assertEqual(invalid['status'],'FAILED')

    def test_saved_response_requires_identical_input_and_never_calls_model(self):
        from research_context import build_context
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30'
            decision={'target_date':target,'market':{'dimensions':[]},'stocks':[],'sectors':[]}
            payload={'target_date':target,'decision':decision}
            payload['research_context']=build_context(root,payload,include_updates=True)
            refs={'market':{'title':'市场','date':target,'facts':{}}}
            with patch('decision.analyze',return_value={'status':'NOT_CONFIGURED'}):
                prepared=interpret(root,payload,copy.deepcopy(decision),copy.deepcopy(refs),{},save=False)
            data=self.valid();data['schema_version']='decision-ui-v3';data['event_research']=[]
            data['market'].update(fund_view='资金不足',international_view='国际样本不足',event_view='事件有限',changes='缺比较日')
            saved={'request_id':prepared['request_id'],'content':json.dumps(data)}
            with patch('decision.analyze',side_effect=AssertionError('must not call model')) as model:
                result=interpret(root,payload,copy.deepcopy(decision),copy.deepcopy(refs),{},save=False,saved_response=saved)
                self.assertEqual(result['status'],'READY')
                self.assertTrue(result['revalidated_without_request'])
                saved['request_id']='different'
                with self.assertRaisesRegex(ValueError,'输入不一致'):
                    interpret(root,payload,copy.deepcopy(decision),copy.deepcopy(refs),{},save=False,saved_response=saved)
                model.assert_not_called()

    def test_secondhand_housing_is_not_confused_with_existing_loan_replacement(self):
        from research_context import build_context
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30'
            decision={'target_date':target,'market':{'dimensions':[]},'stocks':[],'sectors':[]}
            payload={'target_date':target,'decision':decision}
            payload['research_context']=build_context(root,payload,include_updates=True)
            payload['research_context']['events']['original_items']=[{'id':'event:policy',
                'title':'首套住房贷款贴息','date':target,
                'excerpt':'首套住房既包括新房也包括二手房，不包括存量贷款置换。'}]
            data=self.valid();data['schema_version']='decision-ui-v3'
            data['market'].update(fund_view='资金不足',international_view='国际样本不足',event_view='事件有限',changes='缺比较日')
            row={'event_id':'event:policy','impact_path':'购房成本降低',
                 'counterevidence':'存量房不覆盖','next_check':'核对执行情况'}
            data['event_research']=[row]
            refs={'market':{'title':'市场','date':target,'facts':{}}}
            content=json.dumps(data,ensure_ascii=False)
            with patch('decision.analyze',return_value={'status':'READY','content':content}):
                result=interpret(root,payload,copy.deepcopy(decision),copy.deepcopy(refs),{},save=False)
            self.assertEqual(result['status'],'FAILED')
            self.assertIn('包括二手房',result['validation_error'])
            self.assertEqual(result['content'],content)
            row['counterevidence']='存量贷款置换不覆盖'
            with patch('decision.analyze',return_value={'status':'READY','content':json.dumps(data,ensure_ascii=False)}):
                result=interpret(root,payload,copy.deepcopy(decision),copy.deepcopy(refs),{},save=False)
            self.assertEqual(len(result['interpretation']['event_research']),1)
            self.assertEqual(result['validation_warnings'],[])

    def test_structured_judgment_requires_conditions_and_bounded_text(self):
        data=self.valid()
        validate_visual_ai(data)
        for field in ('headline','confirmation','risk'):
            bad=copy.deepcopy(data)
            bad['market'].pop(field)
            with self.assertRaises(ValueError): validate_visual_ai(bad)
        data['market']['stance']='确定上涨'
        with self.assertRaises(ValueError): validate_visual_ai(data)

    def test_unknown_evidence_rejected_even_with_structured_output(self):
        data=self.valid();data['market']['source_ids']=['invented']
        decision={'target_date':'2026-09-30','sectors':[],'stocks':[]}
        with self.assertRaises(ValueError): validate_ai(json.dumps(data),decision,{'market':{}})

    def test_unmapped_fund_attribution_rejected(self):
        data=self.valid()
        data['stocks']=[{'code':'600000','judgment':'板块净流入说明可以买',
            'confirmation':'等待','risk':'缺失','invalidation':'失效','source_ids':['market']}]
        with self.assertRaises(ValueError): validate_visual_ai(data)

    def test_missing_amount_disclaimer_is_not_a_positive_fund_attribution(self):
        data=self.valid()
        row={'code':'600036','judgment':'公告不足，等待核对',
            'confirmation':'等待','risk':'缺少净额与完整公告，追高风险',
            'invalidation':'出现新的风险证据','source_ids':['market']}
        data['stocks']=[row]
        validate_visual_ai(data)
        for text in ('净额证实资金买入','缺少净额，仍凭板块净流入买入'):
            row['risk']=text
            with self.assertRaises(ValueError):validate_visual_ai(data)

    def test_review_does_not_republish_and_failure_preserves_valid_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target='2026-09-30'
            publication={'analysis':{'target_date':target,'decision':{'market':{},'stocks':[],'sectors':[]},'decision_evidence':{'market':{}}}}
            path=root/'data/publications'/f'{target}.json';write_json(path,publication)
            before=path.read_bytes()
            with patch('refresh_ai_review.interpret',return_value={'status':'READY','business_status':'PASSED',
                'generated_at':'2026-10-06T12:00:00+08:00','request_id':'test','interpretation':self.valid()}) as model:
                result=refresh_review(root,target)
                self.assertFalse(model.call_args.kwargs['save'])
            self.assertEqual(result['publication_digest'],publication_digest(publication))
            self.assertEqual(path.read_bytes(),before)
            with patch('refresh_ai_review.interpret') as model:
                self.assertTrue(refresh_review(root,target)['cached'])
                model.assert_not_called()
            with patch('refresh_ai_review.interpret',return_value={'status':'FAILED','message':'失败原因'}), patch('refresh_ai_review.review_signature',return_value='changed-input'):
                result=refresh_review(root,target)
            self.assertEqual(result['message'],'失败原因')
            self.assertEqual(read_json(root/'data/daily'/target/'ai-review.json')['status'],'READY')
            self.assertFalse((root/'data/daily'/target/'decision.json').exists())

