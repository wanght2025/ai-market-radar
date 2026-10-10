import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from decision import WEIGHTS, interpret
from participation import snapshot, validate_brief
from refresh_entry_brief import refresh
from refresh_ai_review import publication_digest
from storage import read_json, write_json


class ParticipationTest(unittest.TestCase):
    def setUp(self):
        self.payload = {'target_date':'2026-09-30', 'decision':{
            'market':{'action':'等待确认', 'score':33, 'dimensions':[]},
            'sectors':[], 'stocks':[{'code':'600000', 'state':'观察线索',
                'history':{'samples':161}, 'entry':{'condition':'等待原板块确认'}}]}}
        self.opportunities = {'selected_codes':['801780'], 'sectors':[{
            'code':'801780', 'confirmation_checks':[{'label':'站上20日均线'}]}]}

    def brief(self, status):
        return {'decision':'暂不入场', 'reason':'总分低于65分', 'risk':'政策支持构成反证',
            'change_condition':status['change_condition'],
            'focus':[{'code':'801780', 'verdict':'优先跟踪', 'why':'相对较强',
                      'trigger':'站上20日均线', 'risk':'跌回均线下'}],
            'stocks':[{'code':'600000', 'decision':'先不买', 'reason':'原参与前提未满足',
                       'release_condition':'等待原板块确认'}]}

    def test_saved_business_is_recovered_without_rewriting_its_date(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); path=root/'data/daily/2026-09-30/history'
            write_json(path/'stock_info-600000.json', {'rows':[{
                'date':'2026-10-04', 'facts':{'主营业务':'银行业务'}}]})
            write_json(path/'candidate_notices-600000.json', {'rows':[]})
            before=copy.deepcopy(self.payload)
            status=snapshot(root,self.payload)
            stock=status['stocks'][0]
            self.assertEqual(stock['business'],'银行业务')
            self.assertTrue(stock['business_late'])
            self.assertEqual(stock['history_samples'],161)
            self.assertFalse(stock['buy_allowed'])
            self.assertEqual(stock['notices'],[])
            self.assertEqual(self.payload,before)

    def test_positive_and_missing_market_decisions_have_distinct_gates(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); status=snapshot(root,self.payload); brief=self.brief(status)
            validate_brief(brief,status,self.opportunities)
            brief['decision']='值得入场'
            with self.assertRaises(ValueError): validate_brief(brief,status,self.opportunities)
            self.payload['decision']['market'].update(action='可考虑参与',score=75)
            status=snapshot(root,self.payload)
            validate_brief(brief,status,self.opportunities)
            self.payload['decision']['market'].update(action='等待确认',score=None)
            status=snapshot(root,self.payload); brief['decision']='暂停判断'
            validate_brief(brief,status,self.opportunities)
            status['market_action']='可考虑参与'
            validate_brief(brief,status,self.opportunities)
            status['market_action']='回避'; brief['decision']='回避'
            validate_brief(brief,status,self.opportunities)

    def test_stock_buy_requires_existing_eligibility_and_no_risk_event(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); status=snapshot(root,self.payload); brief=self.brief(status)
            brief['stocks'][0]['decision']='值得买入'
            with self.assertRaises(ValueError): validate_brief(brief,status,self.opportunities)
            self.payload['decision']['market'].update(score=75,action='可考虑参与')
            self.payload['opportunities']={'sectors':[{'code':'801780',
                'confirmation_checks':[{'label':'站上20日均线','met':True}]}]}
            self.payload['decision']['stocks'][0].update(industry={'code':'801780'},
                history={'samples':161,'close':11,'ma20':10})
            write_json(root/'data/daily/2026-09-30/history/stock_info-600000.json',{
                'rows':[{'date':'2026-09-30','facts':{'主营业务':'银行业务'}}]})
            write_json(root/'data/daily/2026-09-30/history/candidate_notices-600000.json',{
                'rows':[{'date':'2026-09-30','items':[]}]})
            brief['decision']='值得入场'
            status=snapshot(root,self.payload)
            self.assertEqual(status['stocks'][0]['rule_state'],'观察线索')
            self.assertTrue(status['stocks'][0]['buy_allowed'])
            validate_brief(brief,status,self.opportunities)
            self.payload['decision']['stocks'][0]['risk_events']=['重大风险']
            status=snapshot(root,self.payload)
            with self.assertRaises(ValueError): validate_brief(brief,status,self.opportunities)
            self.payload['decision']['stocks'][0]['risk_events']=[]
            self.payload['decision']['stocks'][0]['history']['close']=9
            self.assertFalse(snapshot(root,self.payload)['stocks'][0]['buy_allowed'])
            self.payload['decision']['stocks'][0]['history']['close']=11
            self.payload['opportunities']['sectors'][0]['confirmation_checks'][0]['met']=False
            self.assertFalse(snapshot(root,self.payload)['stocks'][0]['buy_allowed'])

    def test_invented_thresholds_missing_stocks_and_unsupported_attribution_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            status=snapshot(Path(folder),self.payload)
            for mutate in (
                lambda b:b.update(change_condition='达到70分才允许买入'),
                lambda b:b['focus'][0].update(trigger='连续3日广度70%'),
                lambda b:b['focus'][0].update(why='国家队吸筹'),
                lambda b:b['stocks'][0].update(release_condition='必须有一条公告'),
                lambda b:b['stocks'][0].update(reason='买入权限未开'),
                lambda b:b.update(focus=['错误字符串行']),
                lambda b:b.update(stocks={'code':'600000'}),
                lambda b:b.update(stocks=[])):
                brief=self.brief(status); mutate(brief)
                with self.assertRaises(ValueError): validate_brief(brief,status,self.opportunities)

    def test_failed_edit_preserves_valid_ai_and_repeat_uses_no_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); target=self.payload['target_date']
            publication={'analysis':self.payload}
            write_json(root/'data/publications'/f'{target}.json',publication)
            saved={'status':'READY','publication_digest':publication_digest(publication),
                'opportunities':self.opportunities,'interpretation':{'sectors':[]},'content':'original model response'}
            path=root/'data/daily'/target/'ai-review.json'; write_json(path,saved)
            write_json(root/'config.json',{'deepseek':{}})
            prompt=root/'prompts/entry_brief.txt'; prompt.parent.mkdir(); prompt.write_text('test')
            brief=self.brief(snapshot(root,self.payload))
            result={'status':'READY','content':json.dumps(brief),'model':'test',
                    'generated_at':'test-time','usage':{'total_tokens':10}}
            before=(root/'data/publications'/f'{target}.json').read_bytes()
            with patch('refresh_entry_brief.analyze',return_value=result) as model:
                self.assertEqual(refresh(root,target)['status'],'READY')
                self.assertTrue(refresh(root,target)['cached'])
                self.assertEqual(model.call_count,1)
            self.assertEqual(read_json(path)['content'],'original model response')
            original=read_json(path)['interpretation']
            prompt.write_text('changed')
            with patch('refresh_entry_brief.analyze',return_value={**result,'content':'{}'}):
                self.assertEqual(refresh(root,target)['status'],'FAILED')
            self.assertEqual(read_json(path)['interpretation'],original)
            self.assertEqual((root/'data/publications'/f'{target}.json').read_bytes(),before)

    def test_policy_opinion_never_overwrites_rule_qualification(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); decision={'target_date':'2026-09-30','stocks':[], 'sectors':[],
                'market':{'action':'等待确认','score':None,'dimensions':[
                    {'id':key,'name':key,'weight':weight,'score':None if key=='policy' else 80}
                    for key,weight in WEIGHTS.items()]}}
            payload={'target_date':'2026-09-30','opportunities':{'selected_codes':[], 'sectors':[]}}
            status=snapshot(root,{'target_date':payload['target_date'],'decision':decision})
            parsed={'schema_version':'decision-ui-v2','policy_assessment':{
                'score':80,'reason':'有政策事实','source_ids':['events']},
                'entry_brief':{'decision':'暂停判断','reason':'原规则仍缺政策分',
                    'change_condition':status['change_condition'],'risk':'政策不及预期', 'focus':[], 'stocks':[]}}
            result={'status':'READY','content':json.dumps(parsed)}
            with patch('decision.analyze',return_value=result), patch('decision.validate_ai',return_value=parsed), \
                 patch('decision.validate_visual_ai'), patch('opportunities.validate_ai'):
                valid=interpret(root,payload,decision,{}, {},save=False)
                self.assertEqual(valid['status'],'FAILED')
                self.assertIsNone(decision['market']['score'])
                self.assertIsNone(valid['research_evidence']['score'])
                self.assertNotIn('final_market',valid)
                decision['market'].update(score=None,action='等待确认')
                decision['market']['dimensions'][2]['score']=None
                parsed['entry_brief']['decision']='值得入场'
                invalid=interpret(root,payload,decision,{}, {},save=False)
                self.assertEqual(invalid['status'],'FAILED')
                self.assertIsNone(decision['market']['score'])
                self.assertIsNone(invalid['research_evidence']['score'])
