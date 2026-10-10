import copy
import json
import subprocess
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch

from decision import build,evidence,interpret
from participation import snapshot
from refresh_ai_review import publication_digest
from dashboard import research_analysis
from storage import write_json
from test_market_forecast import forecast_fixture


def model_response(request, policy_score=100):
    if 'events' in request:
        key,event=next(iter(request['events'].items()))
        return {'status':'READY','content':json.dumps({'score':policy_score,'reason':'官方政策有限样本评价',
            'source_ids':[key],'evidence_urls':[event['url']]})}
    market=request['decision_rules']['market']
    output={'schema_version':'decision-research-compact-v3','target_date':request['target_date'],
        'market':{'stance':market['action'] if market['score'] is not None else '证据不足',
            'headline':'按最终评分继续检查','judgment':'当前评分依据齐备程度决定研究范围，预测仍需验证。',
            'opportunity_bias':'先核对候选股','risk':'有限公告样本仍有风险','fund_view':'同口径资金证据有限',
            'international_view':'海外仅参考已结束日线','event_view':'官方政策只提供有限样本',
            'changes':'保持数据日期','source_ids':['market'],'forecast':forecast_fixture(request['target_date'])},
        'sectors':[],'stocks':[],'focus':[],'event_research':[],
        'fact_claims':[{'path':['target_date'],'value':request['target_date']},
            {'path':['decision_rules','market','score'],'value':market['score']},
            {'path':['decision_rules','market','action'],'value':market['action']}]}
    for row in request['decision_rules']['stocks']:
        code=row['code'];allowed=request['facts']['research:'+code]['buy_allowed']
        output['stocks'].append({'code':code,'price_facts':{k:v for k,v in request['price_facts']['stocks'][code].items()
            if k in ('date','above_ma5','above_ma20')},'decision':'值得买入' if allowed else '先不买',
            'judgment':'按已有研究条件核对','risk':'有限样本未完成全面风险核验','source_ids':['research:'+code]})
    if request.get('fact_claims'):output['fact_claims']=copy.deepcopy(request['fact_claims'])
    return {'status':'READY','content':json.dumps(output,ensure_ascii=False),'generated_at':'2026-09-30T18:00:00+08:00'}


def chain_fixture(root,above=3,fund_positive=100,breadth=90,equal=1,policy=True):
    target='2026-09-30'
    dates=[(datetime(2026,8,20)+timedelta(days=i)).date().isoformat() for i in range(42)]
    rising=[{'date':d,'close':100+i,'amount':100} for i,d in enumerate(dates)]
    falling=[{**r,'close':200-i} for i,r in enumerate(rising)]
    indices={code:rising if i<above else falling for i,code in enumerate(('sh000001','sz399001','sh000300'))}
    event={'id':'event:policy','date':target,'title':'官方政策通知','url':'https://www.pbc.gov.cn/policy.html','excerpt':'政策正文'}
    payload={'target_date':target,'market':{'breadth_percent':breadth,'equal_weight_change_percent':equal},'industries':[],
        'enrichment':{'flows':[{'code':str(i),'name':'合成分类'+str(i),'date':target,'net_cny':1 if i<fund_positive else -1} for i in range(100)],'flow_complete':True,
                      'events':[event] if policy else []}}
    history={'international':{'.INX':{'rows':rising}},'stocks':{'000001':{'rows':rising}}}
    candidates=[{'代码':'000001','名称':'合成样本','industry':{'code':'SYNTHETIC'}}]
    decision=build(payload,history,candidates,indices,datetime(2026,9,30,18))
    payload['decision']=decision
    payload['opportunities']={'selected_codes':[],'sectors':[{'code':'SYNTHETIC','confirmation_checks':[{'met':True,'label':'合成条件满足'}]}]}
    base=root/'data/daily'/target/'history'
    write_json(base/'stock_info-000001.json',{'rows':[{'date':target,'facts':{'主营业务':'合成业务'}}]})
    write_json(base/'candidate_notices-000001.json',{'rows':[{'date':target,'items':[]}]})
    refs=evidence(payload,decision,history)
    return payload,decision,refs


class FinalScoreChainTest(unittest.TestCase):
    def test_full_pipeline_reaches_all_five_bands_and_preserves_original_report(self):
        cases=[('avoid',0,0,10,0,0,1),('low',0,0,50,0,0,2),('middle',2,0,50,0,0,3),
            ('below65',2,50,50,0,99.9,3),('exact65',2,50,50,0,100,4),
            ('high',3,80,70,0,80,5),('93.7',3,100,90,1,100,5),
            ('missing',3,100,90,1,None,None)]
        for name,above,funds,breadth,equal,policy_score,level in cases:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);payload,decision,refs=chain_fixture(root,above,funds,breadth,equal,policy_score is not None)
                before=copy.deepcopy(decision['market']);publication={'analysis':copy.deepcopy(payload)}
                path=root/'data/publications/2026-09-30.json';write_json(path,publication);frozen=path.read_bytes()
                requests=[]
                def fake_model(_root,request,*args,**kwargs):
                    requests.append(copy.deepcopy(request));return model_response(request,policy_score)
                with patch('decision.analyze',side_effect=fake_model):
                    result=interpret(root,payload,decision,refs,{},save=False)
                self.assertEqual(result['status'],'READY',result.get('validation_error'))
                self.assertEqual(decision['original_market'],before)
                score=decision['market']['score']
                self.assertEqual(requests[-1]['decision_rules']['market']['score'],score)
                self.assertEqual(result['research_evidence']['score'],score)
                self.assertEqual(result['final_market']['score'],score)
                if name=='93.7':self.assertAlmostEqual(score,93.7)
                record={**result,'publication_digest':publication_digest(publication)}
                write_json(root/'data/daily/2026-09-30/ai-review.json',record)
                # Use the actual reader; context digests omitted only for synthetic evidence.
                with patch('research_context.context_digest',return_value=None):
                    report=research_analysis(root,'2026-09-30')
                self.assertEqual(report['decision']['market']['score'],score)
                self.assertEqual(path.read_bytes(),frozen)
                write_json(root/'ui-report.json',report)
                js="""const fs=require('fs'),vm=require('vm'),a=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));
const api=vm.runInNewContext(fs.readFileSync('dashboard-research.js','utf8')+fs.readFileSync('dashboard-visuals.js','utf8')+';({entrySignal,entryGaugeLevel,scoreDimensions})',
{analysis:a,num:String});process.stdout.write(JSON.stringify({level:api.entryGaugeLevel(api.entrySignal()),scores:api.scoreDimensions().map(d=>d.score)}));"""
                ui=subprocess.run(['node','-e',js,str(root/'ui-report.json')],cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,text=True,encoding='utf-8',check=True)
                view=json.loads(ui.stdout)
                self.assertEqual(view['level'],level)
                self.assertEqual(view['scores'],[d['score'] for d in decision['market']['dimensions']])

    def test_rounding_cannot_upgrade_a_score_below65(self):
        from decision import apply_policy
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);payload,decision,_=chain_fixture(root,2,50,50,0)
            apply_policy(decision,{'policy_assessment':{'score':99.999,'reason':'测试','source_ids':['event:policy']}})
            self.assertLess(decision['market']['score'],65)
            self.assertFalse(snapshot(root,payload)['stocks'][0]['buy_allowed'])
