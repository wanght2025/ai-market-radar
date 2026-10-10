"""Reproducible V2 research rules; unknown evidence stays unknown."""
import json
import copy
import statistics
import math
import hashlib
import re
from pathlib import Path
from datetime import datetime, timezone

from deepseek_analysis import analyze, analysis_input, finalize_validation
from urllib.parse import urlsplit
from storage import write_json

VERSION = 'v2.1'
WEIGHTS = {'technical':30, 'funds':25, 'policy':15, 'international':10, 'sentiment':20}
ENTRY_SCORE = 65


def weighted_score(dimensions):
    known = [r for r in dimensions if r['score'] is not None]
    coverage = sum(r['weight'] for r in known)
    partial = sum(r['score']*r['weight'] for r in known)/coverage if coverage else None
    return coverage, partial


def trend(rows, target):
    rows = sorted((r for r in rows if r['date'] <= target), key=lambda r:r['date'])
    if len(rows) < 21 or rows[-1]['date'] != target or len({r['date'] for r in rows[-21:]}) != 21:
        return None
    close = [r['close'] for r in rows]
    five = (close[-1]/close[-6]-1)*100
    amount = [r.get('amount') for r in rows[-6:]]
    volume_ratio = (amount[-1]/statistics.mean(amount[:-1])
        if all(isinstance(a,(int,float)) and a > 0 for a in amount) else None)
    return {'date':target, 'close':close[-1], 'ma5':statistics.mean(close[-5:]),
            'ma20':statistics.mean(close[-20:]), 'return5_percent':five,
            'amount_ratio5':volume_ratio, 'samples':len(rows), 'source':rows[-1].get('source','')}


def duration(rows, target):
    """Completed historical above-MA5, positive-return episodes, without future rows."""
    close = [r['close'] for r in sorted(rows,key=lambda r:r['date']) if r['date'] <= target]
    completed, length = [], 0
    for i in range(5, len(close)):
        active = close[i] > statistics.mean(close[i-4:i+1]) and close[i] > close[i-1]
        if active:
            length += 1
        elif length:
            completed.append(length)
            length = 0
    result = {'sample_count':len(completed), 'definition':'同源行业指数收盘高于MA5且较前日上涨的已结束连续区间；历史研究样本',
              'range_days':None, 'message':'已结束样本不足10段，暂无法可靠估计'}
    if len(completed) >= 10:
        values = sorted(completed)
        result.update(range_days=[values[len(values)//4],values[(len(values)*3)//4]],
                      message='历史区间中间50%；仅供观察，不是上涨承诺')
    return result


def fund_view(payload, history):
    extra = payload.get('enrichment', {})
    margins = sorted(history.get('funds',{}).get('margin',{}).get('rows',[]),key=lambda r:r['date'])
    margin = {'scope':'仅上交所融资余额', 'change_cny':None, 'date':None}
    if len(margins) >= 2:
        margin.update(date=margins[-1]['date'], balance_cny=margins[-1]['balance_cny'],
            change_cny=margins[-1]['balance_cny']-margins[-2]['balance_cny'])
    return {'board_flows':extra.get('flows',[]), 'board_coverage_complete':extra.get('flow_complete',False),
            'board_expected':extra.get('flow_expected',0), 'margin':margin,
            'etf_shares':[r for r in history.get('funds',{}).get('etf_shares',{}).get('rows',[])
                          if any(k in r.get('name','') for k in ('沪深300','上证50','中证500','科创50','创业板'))][:20],
            'national_team':{'status':'UNKNOWN', 'amount_cny':None,
                'message':'仅使用具名公开披露；订单净额和ETF份额不能识别国家队'},
            'mapping':'东方财富行业分类与申万行业独立展示；未核验成员映射，不拼接',
            'limitations':['融资仅覆盖上交所','ETF份额不是现金流入','东方财富估算净额不代表机构身份']}


def build(payload, history, candidates, indices, now):
    target = payload['target_date']
    market = payload['market']
    index_trends = {code:trend(rows,target) for code,rows in indices.items()}
    valid = [r for r in index_trends.values() if r]
    technical = (sum(100 if r['close'] > r['ma20'] else 0 for r in valid)/len(valid)) if len(valid) == 3 else None
    emotion = max(0,min(100,market['breadth_percent']*.7+(50+market['equal_weight_change_percent']*10)*.3))
    funds = fund_view(payload,history)
    flows = funds['board_flows']
    fund_score = (sum(r['net_cny'] > 0 for r in flows)/len(flows)*100
                  if flows and funds['board_coverage_complete'] else None)
    macro = sorted((r for r in history.get('international',{}).get('.INX',{}).get('rows',[])
                    if r['date'] <= target),key=lambda r:r['date'])
    international = None
    if len(macro) >= 21 and len({r['date'] for r in macro[-21:]}) == 21:
        international = 75 if macro[-1]['close'] > statistics.mean(r['close'] for r in macro[-20:]) else 25
    dimensions = [
        {'id':'technical','name':'技术','score':technical,'reason':'三个核心指数收盘与MA20的关系；需至少21条目标日历史','source_ids':['history:indices']},
        {'id':'funds','name':'资金','score':fund_score,'reason':'已覆盖东方财富行业分类净额为正的比例；融资和ETF独立核对','source_ids':['funds']},
        {'id':'policy','name':'政策','score':None,'reason':'需有来源与发布时间的政策证据，由AI审阅；空样本不计中性分','source_ids':['events']},
        {'id':'international','name':'国际环境','score':international,'reason':'标普500上一已结束日期相对MA20；只是国际环境的一项代理','source_ids':['history:international']},
        {'id':'sentiment','name':'情绪','score':round(emotion,2),'reason':'上涨广度70%＋等权涨跌映射30%；未经效果校准','source_ids':['market']},
    ]
    for row in dimensions:
        row['weight'] = WEIGHTS[row['id']]
    coverage, partial = weighted_score(dimensions)
    overall = partial if coverage == 100 else None
    action = ('回避' if technical is not None and technical < 34 and emotion < 40 else
              '可考虑参与' if overall is not None and overall >= ENTRY_SCORE else '等待确认')
    market_judgment = {'action':action, 'score':overall, 'observed_score':round(partial,2) if partial is not None else None,
        'coverage_percent':coverage, 'dimensions':dimensions, 'index_trends':index_trends,
        'counterevidence':['规则尚未通过前瞻验证','缺失维度不补中性分；局部分不能当作完整环境分'],
        'reason':'先依据技术和情绪判断风险，再核对资金与事件；评分权重为初始研究规则'}
    predictions = []
    for board in payload['industries']:
        rows = history.get('industries',{}).get(board['code'],{}).get('rows',[])
        t = trend(rows,target)
        state, reason = '证据不足', '缺少连续行业历史；当日排名不能证明即将启动'
        if t:
            strong = board['absolute_change_percent'] > 0 and board['relative'] > 0 and board['breadth'] >= 55
            early = (board['relative'] > 0 and board['breadth'] >= 50 and t['close'] > t['ma5']
                and -2 <= t['return5_percent'] <= 3 and abs(t['close']/t['ma20']-1) <= .025
                and t['amount_ratio5'] is not None and t['amount_ratio5'] >= 1.2)
            if early:
                state, reason = '启动候选', '近5日尚未大幅走强，成交关注放大、站上MA5且靠近MA20；资金与后续突破仍待确认'
            elif strong and t['close'] > t['ma20'] and t['return5_percent'] > 0:
                state = '启动候选' if (board.get('rank_improvement') or 0) >= 3 and t['return5_percent'] < 5 else '强势延续观察'
                reason = '绝对与相对表现、上涨广度和行业指数趋势一致；资金归因仍需独立证据'
            elif t['return5_percent'] > 0 and (board['relative'] < 0 or board['breadth'] < 40):
                state, reason = '衰退预警', '近期上涨后广度或相对强弱转弱，等待后续日期核验'
            else:
                state, reason = '等待确认', '趋势与广度未形成一致信号'
        predictions.append({'code':board['code'], 'name':board['name'], 'state':state, 'reason':reason,
            'next_day':'观察绝对上涨、跑赢全市场等权表现且广度≥55%是否同时成立',
            'near_term':'在后续第3、5交易日检查是否绝对走强并跑赢全市场等权表现',
            'confirmation':'行业指数上涨并跑赢全市场等权表现；广度和成交关注不转弱',
            'invalidation':'行业指数跌破预测日MA20或连续相对走弱', 'trend':t,
            'duration':duration(rows,target), 'source_ids':['industry:'+board['code'],'history:industry:'+board['code']]})
    plans = []
    for row in candidates:
        code = row['代码']
        detail = history.get('stocks',{}).get(code,{})
        t = trend(detail.get('rows',[]),target)
        info = detail.get('stock_info',{}).get('rows',[{}])[0]
        notices = detail.get('candidate_notices',{}).get('rows',[{}])[0]
        risks = ['公告为有限样本，尚未完成完整风险核验','行业归属不等于主营业务受益已确认']
        if not t:
            risks.append('缺少足够连续个股历史')
        if info.get('date','') > target:
            risks.append('个股资料晚于行情日期，不能作为当时已知信息')
        risky = [e for e in notices.get('items',[]) if any(w in e['title'] for w in ('立案','退市','风险警示','减持','诉讼'))]
        state = '暂不参与' if risky else '观察线索'
        plans.append({'code':code,'name':row.get('名称',code),'industry':row['industry'], 'state':state,
            'reason':'流动性与所属板块表现进入研究范围，关键业务及公告风险仍需核验',
            'entry':{'low':None, 'high':None, 'ma5_reference':round(t['ma5'],2) if t else None,
                     'close_reference':round(t['close'],2) if t else None,
                     'type':'MA5与收盘参考，尚未形成可执行买入区间',
                     'condition':'先核验业务关联和完整公告，再等板块确认；不得跳空追价或假定涨停成交'},
            'exit':{'reference':round(t['ma20'],2) if t else None, 'condition':'板块逻辑失效、风险公告或跌破MA20时重新评估'},
            'risks':risks,'risk_events':risky,'history':t,'review_days':[1,3,5],
            'source_ids':['stock:'+code,'history:stock:'+code]})
    return {'version':VERSION,'target_date':target,'generated_at':now.isoformat(),
        'market':market_judgment,'sectors':predictions,'stocks':plans,'funds':funds,
        'sentiment':{'breadth_percent':market['breadth_percent'], 'equal_weight_change_percent':market['equal_weight_change_percent'],
            'large_rise_count':market.get('large_rise_count'), 'large_fall_count':market.get('large_fall_count'),
            'limit_up_count':None,'limit_down_count':None,'message':'未取得逐股适用涨跌停价格与上市交易日状态；大幅涨跌不冒充涨跌停'},
        'gaps':history.get('gaps',[]),'ai':{'status':'DISABLED','message':'未启用模型'},
        'study_warning':'初始研究规则，概率和收益未校准；未来3–5日是验证期限，非承诺持续上涨'}


def evidence(payload, decision, history):
    from research_brief import evidence_registry
    refs = evidence_registry(payload)
    def add(key,title,facts):
        refs[key] = {'date':payload['target_date'],'title':title,'facts':facts}
    add('funds','资金渠道与覆盖',decision['funds'])
    add('history:indices','指数连续趋势',decision['market']['index_trends'])
    add('history:international','国际行情已结束日期',
        {k:{**v,'rows':v.get('rows',[])[-21:]} for k,v in history.get('international',{}).items()})
    for row in decision['sectors']:
        add('history:industry:'+row['code'],row['name']+' · 趋势与持续时间样本',
            {'trend':row['trend'],'duration':row['duration']})
    for row in decision['stocks']:
        add('history:stock:'+row['code'],row['name']+' · 研究条件和风险',row)
    return refs


def validate_market_forecast(forecast, target, refs):
    if not isinstance(forecast,dict) or forecast.get('basis_date')!=target:
        raise ValueError('市场预测基准日期不匹配')
    if type(forecast.get('horizon_trading_days')) is not int or forecast['horizon_trading_days']!=5:
        raise ValueError('市场预测期限须为5个交易日')
    fields=('next_day','near_term','confidence_reason')
    if any(not isinstance(forecast.get(k),str) or not 0<len(forecast[k].strip())<=240 for k in fields):
        raise ValueError('市场预测说明缺失或过长')
    if forecast.get('confidence') not in ('低','中','高'):
        raise ValueError('市场预测把握程度无效')
    scenarios=forecast.get('scenarios')
    if not isinstance(scenarios,dict) or set(scenarios)!={'基准','偏强','偏弱'}:
        raise ValueError('市场预测必须包含三种情景')
    for scenario in scenarios.values():
        if not isinstance(scenario,dict) or set(scenario)!={'outlook','trigger','invalidation'} or any(not isinstance(scenario.get(k),str) or
                not 0<len(scenario[k].strip())<=240 for k in ('outlook','trigger','invalidation')):
            raise ValueError('市场预测情景缺少走势、触发或失效条件')
    ids=forecast.get('source_ids')
    if not isinstance(ids,list) or not ids or any(not isinstance(k,str) or k not in refs for k in ids):
        raise ValueError('市场预测证据引用不匹配')
    if set(forecast)-{'basis_date','horizon_trading_days','next_day','near_term','scenarios','confidence','confidence_reason','source_ids'}:
        raise ValueError('市场预测包含未经支持的字段')


def validate_ai(content, decision, refs):
    data = json.loads(content)
    if not isinstance(data,dict) or data.get('target_date') != decision['target_date']:
        raise ValueError('AI日期不匹配')
    counter = data.get('counterevidence')
    if isinstance(counter,list) and counter and all(isinstance(s,str) and s.strip() for s in counter):
        data['counterevidence'] = '；'.join(counter)
    if not all(isinstance(data.get(k),str) and data[k].strip() for k in ('summary','counterevidence')):
        raise ValueError('AI摘要或反证缺失')
    validate_policy(data.get('policy_assessment'),decision['target_date'],refs)
    for key in ('market','sectors','stocks'):
        rows = [data.get(key)] if key == 'market' else data.get(key)
        if not isinstance(rows,list):
            raise ValueError('AI判断结构缺失')
        allowed = {r['code'] for r in decision[key]} if key != 'market' else set()
        seen = set()
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('judgment'),str) or not row['judgment'].strip():
                raise ValueError('AI判断缺失')
            ids = row.get('source_ids')
            if not isinstance(ids,list) or not ids or any(not isinstance(k,str) or k not in refs for k in ids):
                raise ValueError('AI证据引用不匹配')
            if key != 'market':
                if row.get('code') not in allowed or row['code'] in seen:
                    raise ValueError('AI候选代码未知或重复')
                seen.add(row['code'])
        if key != 'market' and seen != allowed:
            raise ValueError('AI候选不完整')
    from research_facts import validate_price_facts
    validate_price_facts(data, decision)
    if 'forecast' in data['market']:
        validate_market_forecast(data['market']['forecast'],decision['target_date'],refs)
    return data


def validate_policy(policy, target, refs):
    if policy is not None:
        if not isinstance(policy,dict) or not isinstance(policy.get('reason'),str):
            raise ValueError('政策评分结构不符')
        score = policy.get('score')
        if score is not None:
            if isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score) or not 0 <= score <= 100:
                raise ValueError('政策分值无效')
            ids = policy.get('source_ids')
            if not isinstance(ids,list) or not ids or any(not isinstance(k,str) or k not in refs or
                    k not in ('events','event_update','research:events') and not k.startswith('event:') for k in ids):
                raise ValueError('政策评分无可核验事件来源')
            groups = []
            for key in ids:
                facts = refs[key].get('facts',{})
                if key=='events':events=facts.get('events',[])
                elif key=='event_update':events=facts.get('items',[])
                elif key=='research:events':events=facts.get('original_items',[])+facts.get('supplemental_items',[])
                else:events=[facts]
                groups.append(events)
            policy_ids = policy.get('evidence_urls')
            verified = [[e for e in events if e.get('date') and e['date'] <= target
                and urlsplit(e.get('url','')).hostname in ('www.pbc.gov.cn','www.gov.cn','www.mof.gov.cn')
                and e.get('excerpt') and any(w in e.get('title','') for w in ('政策','通知','货币','财政'))] for events in groups]
            if not isinstance(policy_ids,list) or not policy_ids or any(
                    url not in {e['url'] for group in verified for e in group} for url in policy_ids) or any(
                    not any(e['url'] in policy_ids for e in group) for group in verified):
                raise ValueError('缺少政策事实，不能生成政策分')


def validate_visual_ai(data):
    if data.get('schema_version') not in ('decision-ui-v1','decision-ui-v2','decision-ui-v3'):
        raise ValueError('AI输出版本不匹配')
    if data['market'].get('stance') not in ('可考虑参与','等待确认','回避','证据不足'):
        raise ValueError('AI参与态度无效')
    if data['schema_version']=='decision-ui-v3':
        for row,fields in [(data['market'],('fund_view','international_view','event_view','changes')),
                *[(r,('priority_reason','catalyst','changes','fund_support')) for r in data['sectors']]]:
            if any(not isinstance(row.get(k),str) or not row[k].strip() or len(row[k])>360 for k in fields):
                raise ValueError('AI 1.0研究字段缺失或过长')
        events=data.get('event_research')
        if not isinstance(events,list) or len(events)>6:raise ValueError('AI事件研究结构不符')
        for e in events:
            if not isinstance(e,dict) or any(not isinstance(e.get(k),str) or not e[k].strip()
                    or len(e[k])>360 for k in ('event_id','impact_path','counterevidence','next_check')):
                raise ValueError('AI事件研究内容不符')
    for kind, rows, fields in (
        ('market',[data['market']],('headline','confirmation','risk')),
        ('sectors',data['sectors'],('next_day','near_term','confirmation','invalidation')),
        ('stocks',data['stocks'],('confirmation','risk','invalidation'))):
        for row in rows:
            if kind != 'market' and any(word in ' '.join(str(row.get(k,'')) for k in ('judgment',*fields,'hypothesis','alternative','scenarios')).replace('缺少净额','缺少资金金额')
                    for word in ('净流入','净流出','净额','权重股')):
                raise ValueError('AI使用了未映射资金或未提供的权重信息')
            for field in fields:
                value = row.get(field)
                if not isinstance(value,str) or not value.strip() or len(value)>180:
                    raise ValueError('AI条件字段缺失或过长')
            if len(row['judgment'])>(600 if kind=='market' else 260):
                raise ValueError('AI判断过长')


def apply_policy(decision, parsed):
    policy = parsed.get('policy_assessment') or {}
    if policy.get('score') is None:
        return
    row = next(r for r in decision['market']['dimensions'] if r['id'] == 'policy')
    row.update(score=policy['score'],reason='AI政策样本评分（主观判断）：'+policy['reason'],source_ids=policy['source_ids'])
    coverage, partial = weighted_score(decision['market']['dimensions'])
    market = decision['market']
    market.update(coverage_percent=coverage,observed_score=round(partial,2),score=partial if coverage == 100 else None)
    if market.get('action') != '回避':
        market['action'] = '可考虑参与' if coverage == 100 and partial >= ENTRY_SCORE else '等待确认'


def prepare_final_score(root, payload, decision, refs, settings, *, force=False, saved_response=None):
    """Freeze the raw score, then complete the same rule before full research."""
    decision.setdefault('target_date',payload['target_date'])
    decision.setdefault('original_market',copy.deepcopy(decision['market']))
    decision['market']=copy.deepcopy(decision['original_market'])
    context=payload.get('research_context',{})
    events={e['id']:e for group in ('original_items','supplemental_items')
            for e in context.get('events',{}).get(group,[])}
    for key,ref in refs.items():
        if key.startswith('event:'):events[key]=ref.get('facts',{})
        elif key=='events':
            for i,event in enumerate(ref.get('facts',{}).get('events',[])):
                events[event.get('id') or 'event:policy-'+str(i)]=event
    eligible={key:{k:e.get(k) for k in ('date','title','url','excerpt')} for key,e in events.items()
        if e.get('date') and e['date']<=decision['target_date'] and e.get('excerpt')
        and urlsplit(e.get('url','')).hostname in ('www.pbc.gov.cn','www.gov.cn','www.mof.gov.cn')
        and any(w in e.get('title','') for w in ('政策','通知','货币','财政'))}
    phase=None
    if eligible and settings is not None:
        policy_refs={key:{'title':e['title'],'date':e['date'],'facts':e} for key,e in eligible.items()}
        prompt=(Path(__file__).resolve().parent/'prompts/policy_score.txt').read_text(encoding='utf-8')
        request={'target_date':decision['target_date'],'events':eligible}
        phase=(copy.deepcopy(saved_response.get('policy_phase')) if saved_response is not None else
            analyze(root,request,{**settings,'max_tokens':min(settings.get('max_tokens',16384),8192)},
                    system_prompt=prompt,json_output=True,force=force))
        if not phase or phase.get('status')!='READY':
            return phase or {'status':'FAILED','message':'保存响应没有前置政策评估，未调用模型'}
        try:
            policy=json.loads(phase['content'])
            if set(policy)!={'score','reason','source_ids','evidence_urls'}:
                raise ValueError('政策评分字段不匹配')
            validate_policy(policy,decision['target_date'],policy_refs)
            apply_policy(decision,{'policy_assessment':policy})
            decision['policy_assessment']=policy
            finalize_validation(root,phase,True)
            refs.update(policy_refs)
        except (ValueError,TypeError,KeyError) as error:
            phase.update(status='FAILED',message='前置政策评分校验失败',validation_error=str(error))
            finalize_validation(root,phase,False,str(error))
            return phase
    if {d['id'] for d in decision['market'].get('dimensions',[])}==set(WEIGHTS):
        decision['market']['score_basis']='final-effective-v1'
    return phase


def _research_input(payload, decision, refs, participation):
    """Bound model facts without modifying the complete evidence saved locally."""
    selected = decision['sectors']
    opportunities = payload.get('opportunities')
    # Keep the complete evidence on disk; send each fact once and bound event samples.
    model_refs = {k:{'title':v['title'],'date':v['date']} for k,v in refs.items()}
    selected_ids = {'industry:'+s['code'] for s in selected}
    facts = {k:v['facts'] for k,v in refs.items() if k in ('market','funds','history:indices','history:international')
             or k in selected_ids}
    # Unmapped supplier rows cannot support sector attribution; send only the
    # existing market-level fund score and independently identified channels.
    if 'funds' in facts:
        funds = facts['funds']
        facts['funds'] = {k:v for k,v in funds.items() if k != 'board_flows'}
        facts['funds'].update(board_observed_count=len(funds.get('board_flows',[])),
            positive_share_percent=next((d['score'] for d in decision['market']['dimensions'] if d['id']=='funds'),None),
            detail_scope='东方财富行业分类未映射；本次不提供分类净额明细，不用于板块或个股归因')
    events = sorted(payload.get('enrichment',{}).get('events',[]),key=lambda e:e.get('code') not in ('央行','国务院','中央汇金'))
    facts['events'] = {'items':[{k:v for k,v in e.items() if k != 'known_at'} for e in events[:20]],'available_sample_count':len(events),
        'scope':'给模型前20条目标日有限样本，不代表完整覆盖'}
    facts['participation'] = participation
    context=payload.get('research_context')
    event_ids=set((decision.get('policy_assessment') or {}).get('source_ids',[]))
    if context:
        from research_context import add_evidence as context_evidence, research_event_sample
        context_evidence(refs,context)
        for key in ('research:funds','research:activity','research:international','research:events','research:comparison'):
            facts[key]=refs[key]['facts']
        context_funds=context['funds']
        boards=sorted((r for r in context_funds['boards'] if r['net_cny'] is not None),key=lambda r:r['net_cny'])
        chosen={r['code']:r for r in boards[:5]+boards[-5:]}
        facts['research:funds']={**context_funds,'boards':list(chosen.values()),
            'detail_scope':'仅给净额两端各5个东方财富行业分类；完整列表在产品中，不能映射申万'}
        # Each event is sent once; full saved text remains available in the UI.
        def sample_event(event):
            return {k:(v[:1200] if k=='excerpt' and isinstance(v,str) else v)
                    for k,v in event.items() if k!='known_at'}
        facts['research:events']={**context['events'],
            'original_items':[sample_event(e) for e in research_event_sample(context['events']['original_items'],8)],
            'supplemental_items':[sample_event(e) for e in research_event_sample(context['events']['supplemental_items'],12)],
            'scope':'有限样本：原日最多8条，新增最多12条；正文节选最多1200字，完整证据保留在本地'}
        facts['events']={'scope':'正文与标题在research:events；不重复发送'}
        model_refs.update({k:{'title':v['title'],'date':v['date']} for k,v in refs.items() if k.startswith('research:')})
        for e in facts['research:events']['original_items']+facts['research:events']['supplemental_items']:
            event_ids.add(e['id'])
            model_refs[e['id']]={'title':e['title'],'date':e.get('date')}
    for item in participation['stocks']:
        facts['research:'+item['code']] = dict(item)
        if context and item['code'] in context.get('company_updates',{}):
            current=context['company_updates'][item['code']]
            facts['research:'+item['code']]['current_notices']={**current,'items':current['items'][:5]}
    if payload.get('event_update'):
        facts['event_update'] = payload['event_update']
    # Include the original facts behind every model-visible reference.
    for item in selected:
        key = 'history:industry:'+item['code']
        if key in refs:
            facts[key] = refs[key]['facts']
    for item in decision['stocks']:
        for key in ('stock:'+item['code'], 'history:stock:'+item['code']):
            if key in refs:
                facts[key] = refs[key]['facts']
    for key in ('coverage', 'rules', 'limitations'):
        if key in refs:
            facts[key] = refs[key]['facts']
    if opportunities:
        facts['opportunities'] = {k:v for k,v in opportunities.items() if k in ('version','market','counts','scope','candidate_note')}
        for item in selected:
            key = 'opportunity:'+item['code']
            facts[key] = {k:v for k,v in refs[key]['facts'].items() if k in ('code','type','original_state','history_complete','index_change_percent','five_percent','amount_ratio5','relative','breadth','confirmation_checks','invalidation_checks','signal_reference','reference_name')}
            facts[key]['condition_text'] = '；'.join(c['label'] for c in facts[key]['confirmation_checks'])
    model_refs = {key:value for key,value in model_refs.items() if key in facts or key in event_ids}
    request = {'target_date':payload['target_date'],'mode':payload.get('mode'),
        'coverage':payload.get('coverage',{}),'market':payload.get('market',{}),
        'candidates':payload.get('candidates',[]),'candidate_rule':payload.get('candidate_rule',''),
        'decision_rules':{k:v for k,v in decision.items() if k in ('version','market','sectors','stocks','gaps','study_warning')},
        'evidence':model_refs,'facts':facts}
    if decision.get('policy_assessment'):
        request['policy_assessment']=decision['policy_assessment']
    if payload.get('research_window'):
        request['research_window']=payload['research_window']
    from daily_ai import compact_request, research_event_ids
    request=compact_request(request)
    from research_facts import price_facts,claim_template
    request['price_facts']={kind:{r['code']:price_facts(r,payload['target_date']) for r in decision[kind]}
        for kind in ('sectors','stocks')}
    request['fact_claims']=claim_template(request)
    if context:
        event_ids=research_event_ids(request)
    return request, event_ids


def interpret(root, payload, decision, refs, settings, *, save=True, force=False, saved_response=None):
    phase=prepare_final_score(root,payload,decision,refs,settings,force=force,saved_response=saved_response)
    if phase and phase.get('status')!='READY':
        decision['ai']=phase
        return phase
    prompt = (Path(__file__).resolve().parent/'prompts/decision_research.txt').read_text(encoding='utf-8')
    selected = []
    ordered = [s for s in decision['sectors'] if s['state'] in ('启动候选','强势延续观察','衰退预警')]
    ordered += decision['sectors'][:5]
    opportunities = payload.get('opportunities')
    if opportunities:
        priorities = opportunities['selected_codes']
        ordered = [s for code in priorities for s in decision['sectors'] if s['code']==code]+ordered
    for row in ordered:
        if row['code'] not in {s['code'] for s in selected}:
            selected.append(row)
        if len(selected) == 8:
            break
    scoped = {**decision,'sectors':selected}
    from participation import snapshot as participation_snapshot, add_evidence as participation_evidence
    participation = participation_snapshot(root, {**payload, 'decision':scoped})
    participation_evidence(refs, participation)
    prompt += '\n必须逐一覆盖板块代码：'+','.join(s['code'] for s in selected)+'。不得省略，其他板块不在本次AI审阅范围。'
    request, event_ids = _research_input(payload,scoped,refs,participation)
    context = payload.get('research_context')
    model_refs = request['evidence']
    from daily_ai import expand_output
    from research_facts import validate_price_facts
    request_id = hashlib.sha256((prompt+json.dumps(request,ensure_ascii=False,sort_keys=True)).encode()).hexdigest()
    write_json(root/'data/ai/inputs'/f'{request_id}.json',{'system_prompt':prompt,'user_payload':analysis_input(request)})
    if saved_response is not None:
        if saved_response.get('request_id')!=request_id or not saved_response.get('content'):
            raise ValueError('保存响应与本次输入不一致，不能离线重校验')
        result={k:copy.deepcopy(v) for k,v in saved_response.items() if k not in
            ('validation_error','business_status','fact_validation','interpretation','message')}
        result.update(status='READY',revalidated_without_request=True)
    else:
        result = analyze(root, request, settings, system_prompt=prompt, json_output=True, force=force)
    result['policy_phase']=phase
    if result['status'] == 'READY':
        try:
            # decision_rules is the exact top-level evidence object sent above.
            visible_refs = {k:{**refs[k],'facts':request['facts'].get(k,refs[k].get('facts',{}))} for k in model_refs}
            if context:
                events=request['facts']['research:events']
                visible_refs['events']={'facts':{'events':events['original_items']}}
                visible_refs.update({e['id']:{'facts':e} for group in ('original_items','supplemental_items') for e in events[group]})
            elif 'events' in request['facts']:
                visible_refs['events']={'facts':{'events':request['facts']['events'].get('items',[])}}
            visible_refs['decision_rules'] = {'facts':request['decision_rules']}
            model_output=json.loads(result['content'])
            if 'policy_assessment' in model_output and model_output['policy_assessment']!=decision.get('policy_assessment'):
                raise ValueError('完整研判不能另行改写已经完成的政策分')
            if decision.get('policy_assessment'):
                model_output['policy_assessment']=decision['policy_assessment']
            from research_facts import validate_research_claims
            validate_research_claims(model_output,request)
            validate_price_facts(model_output,scoped,require=True)
            event_rows=model_output.get('event_research',[])
            if not isinstance(event_rows,list) or any(not isinstance(row,dict) for row in event_rows):
                raise ValueError('AI事件研究格式无效')
            full_events={e['id']:e for group in ('original_items','supplemental_items')
                         for e in context['events'][group]} if context else {}
            for row in event_rows:
                code=row.get('event_id');message=None
                if code not in event_ids:
                    message='事件编号不在本次输入，未采用这条事件推论；原始响应保留'
                elif re.search(r'存量房(?:不覆盖|不包括|不支持|不适用)',json.dumps(row,ensure_ascii=False)) and '包括二手房' in full_events.get(code,{}).get('excerpt',''):
                    message='政策正文明确包括二手房，与“存量房不覆盖”推论冲突；该条未采用，原始响应保留'
                if message:raise ValueError(message)
            model_output={**model_output,'event_research':event_rows}
            result['validation_warnings']=[]
            raw=expand_output(model_output,scoped,participation,opportunities)
            parsed = validate_ai(json.dumps(raw,ensure_ascii=False),scoped,visible_refs)
            if context and parsed.get('schema_version') != 'decision-ui-v3':
                raise ValueError('当前研究必须返回decision-ui-v3，旧版解释仅供历史阅读')
            validate_visual_ai(parsed)
            if opportunities:
                from opportunities import validate_ai as validate_opportunity_ai
                validate_opportunity_ai(parsed,opportunities)
                if parsed.get('schema_version') in ('decision-ui-v2','decision-ui-v3'):
                    from participation import validate_brief
                    validate_brief(parsed['entry_brief'], participation, opportunities)
            result['final_market'] = copy.deepcopy(decision['market'])
            result['ai_policy_assessment'] = result['final_market']
            result['interpretation'] = parsed
            result['coverage'] = {'sectors':len(selected),'stocks':len(decision['stocks']),
                                  'rule_sectors':len(decision['sectors'])}
            result['fact_validation']={'status':'PASSED','scope':'均线关键事实；不代表全部事实或预测已验证'}
            finalize_validation(root,result,True)
        except (ValueError,TypeError,KeyError) as error:
            result = {**result,'status':'FAILED','message':'模型结构、引用或关键事实校验失败；保留规则判断','validation_error':str(error)}
            finalize_validation(root,result,False,str(error))
    result['request_id'] = request_id
    result['research_evidence'] = participation
    from participation import evidence_digest
    result['evidence_digest'] = evidence_digest(participation)
    if context:
        from research_context import context_digest
        result['context_digest'] = context_digest(context)
        result['research_context'] = context
        result['context_stock_codes'] = [r['code'] for r in context['funds']['stocks']]
    result.setdefault('generated_at',datetime.now(timezone.utc).isoformat())
    decision['ai'] = result
    if save:
        write_json(root/'data'/'daily'/payload['target_date']/'decision.json',decision)
    return result
