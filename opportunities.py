"""Versioned opportunity diagnostics over existing, dated facts. No collection."""
from itertools import zip_longest
import math

from storage import read_json

VERSION = 'opportunity-v1.1'
TYPES = ('潜在启动', '反转观察', '趋势跟随', '衰退风险')


def check(key, label, value, predicate):
    known = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return {'id':key, 'label':label, 'value':value if known else None,
            'met':bool(predicate(value)) if known else None}


def complete(checks):
    return bool(checks) and all(c['met'] is True for c in checks)


def balanced(rows, maximum=8):
    """Cover opportunity types before filling from existing rule priorities."""
    groups = [sorted((r for r in rows if r['type'] == kind), key=lambda r:r['rank']) for kind in TYPES]
    return [r for turn in zip_longest(*groups) for r in turn if r is not None][:maximum]


def build(payload, decision, history, index_history=None):
    target = payload['target_date']
    benchmark = (index_history or {}).get('sh000300', [])
    calendar = sorted({r['date'] for r in benchmark if r['date'] <= target})[-21:]
    industries = {r['code']:r for r in payload.get('industries', [])}
    rows = []
    for rule in decision.get('sectors', []):
        board = industries[rule['code']]
        bars = sorted((r for r in history.get('industries', {}).get(rule['code'], {}).get('rows', [])
                       if r['date'] <= target), key=lambda r:r['date'])
        dates = [r['date'] for r in bars[-21:]]
        contiguous = len(dates) == 21 and len(set(dates)) == 21 and dates[-1] == target
        if calendar:
            contiguous = contiguous and len(calendar) == 21 and dates == calendar
        t = rule.get('trend') if contiguous else None
        daily = ((bars[-1]['close']/bars[-2]['close']-1)*100) if t else None
        close, ma5, ma20 = ((t.get(k) if t else None) for k in ('close','ma5','ma20'))
        above5 = close/ma5-1 if close is not None and ma5 else None
        above20 = close/ma20-1 if close is not None and ma20 else None
        five = t.get('return5_percent') if t else None
        ratio = t.get('amount_ratio5') if t else None
        early = [check('relative_positive','相对全市场走强',board.get('relative'),lambda v:v>0),
                 check('breadth50','上涨成员至少一半',board.get('breadth'),lambda v:v>=50),
                 check('above_ma5','指数收盘高于5日均线',above5,lambda v:v>0),
                 check('quiet5','5日收益−2%至3%',five,lambda v:-2<=v<=3),
                 check('near_ma20','距20日均线不超过2.5%',above20,lambda v:abs(v)<=.025),
                 check('amount12','成交额至少为前5日均额1.2倍',ratio,lambda v:v>=1.2)]
        reversal = [check('recent_weak','行业指数近5日下跌',five,lambda v:v<0),
                    check('index_up','行业指数当日上涨',daily,lambda v:v>0),early[2],early[0],
                    check('breadth55','上涨成员至少55%',board.get('breadth'),lambda v:v>=55)]
        kind = ('潜在启动' if t and rule['state']=='启动候选' else
                '反转观察' if complete(reversal) else
                '趋势跟随' if t and rule['state']=='强势延续观察' else
                '衰退风险' if t and rule['state']=='衰退预警' else '待确认')
        confirmed = [check('index_up','行业指数当日上涨',daily,lambda v:v>0), early[2], early[0], reversal[-1],
                     check('above_ma20','指数收盘高于20日均线',above20,lambda v:v>0)]
        if kind=='反转观察':
            # Recent weakness identifies the signal, not a condition that must
            # remain weak on every later confirmation day.
            confirmed = reversal[1:]
        reference = ma20 if close is not None and ma20 is not None and close>=ma20 else close
        reference_name = '信号日20日均线' if reference==ma20 else '信号日收盘'
        invalidation = [check('below_signal_reference','指数跌破'+reference_name,
                              close-reference if close is not None and reference is not None else None,lambda v:v<0),
                        check('relative_negative','相对全市场转弱',board.get('relative'),lambda v:v<0)]
        if kind=='衰退风险':
            relative,breadth = board.get('relative'),board.get('breadth')
            known = five is not None and relative is not None and breadth is not None
            confirmed = [{'id':'weak_after_rise','label':'近5日上涨后，相对为负或上涨成员不足40%',
                          'value':{'five_percent':five,'relative':relative,'breadth':breadth},
                          'met':five>0 and (relative<0 or breadth<40) if known else None}]
            invalidation = [{'id':'recovered_strength','label':'相对转正且上涨成员至少55%',
                             'value':{'relative':relative,'breadth':breadth},
                             'met':relative>0 and breadth>=55 if relative is not None and breadth is not None else None}]
            reference,reference_name = None,'衰退风险检查转弱与恢复，不使用上涨机会参考点位'
        rows.append({'code':rule['code'],'name':rule['name'],'rank':board.get('rank',999),
            'member_signature':board.get('member_signature'),'type':kind,'original_state':rule['state'],
            'checks':{'潜在启动':early,'反转观察':reversal},'confirmation_checks':confirmed,
            'invalidation_checks':invalidation,
            'signal_ma20':ma20,'signal_reference':reference,'reference_name':reference_name,'history_complete':bool(t),'index_change_percent':daily,
            'amount_ratio5':ratio,'five_percent':five,'relative':board.get('relative'),
            'breadth':board.get('breadth'),'absolute_change_percent':board.get('absolute_change_percent'),
            'resilient':(payload['market']['equal_weight_change_percent']<0 and board['relative']>0)
                        if payload.get('market',{}).get('equal_weight_change_percent') is not None and board.get('relative') is not None else None,
            'confirmation':('转弱迹象已出现，待后续复核' if kind=='衰退风险' else '当日条件满足，仍需后续核验') if complete(confirmed) else '等待确认',
            'source_ids':['industry:'+rule['code'],'history:industry:'+rule['code']]})
    trends = decision.get('market',{}).get('index_trends',{})
    trend_checks = [check('index_'+code,name+'高于20日均线',
        t['close']/t['ma20']-1 if t and t.get('ma20') else None,lambda v:v>0)
        for code,name in [('sh000001','上证指数'),('sz399001','深证成指'),('sh000300','沪深300')]
        for t in [trends.get(code)]]
    signals = [r for r in rows if r['type'] in TYPES]
    reversal_count = sum(r['type']=='反转观察' for r in rows)
    return {'version':VERSION,'target_date':target,'sectors':rows,'selected_codes':[r['code'] for r in balanced(signals)],
        'market':{'trend_checks':trend_checks,'reversal_count':reversal_count,
                  'reversal_text':f'{reversal_count}个行业出现近期走弱后的改善迹象' if reversal_count else '尚无行业满足反转观察条件',
                  'valuation':None,'extreme_sentiment':None,
                  'limits':'缺少估值与连续情绪位置证据；无法判断低估或极端恐慌。反转观察不是底部确认。'},
        'counts':{kind:sum(r['type']==kind for r in rows) for kind in TYPES},
        'scope':f'{len(rows)}申万行业按现有历史扫描；资金分类独立；机会条件不是收益概率。'}


def concept_facts(data, generation, quotes, target):
    """Snapshot membership + saved quotes; no implied historical membership."""
    manifest = read_json(data/'snapshots/concept'/generation/'manifest.json',{})
    if not manifest or manifest.get('unknown_member_conflicts') != 0:
        return {'rows':[],'message':'概念成员未通过完整性核验，暂停分析'}
    average = sum(r['涨跌幅'] for r in quotes.values())/len(quotes) if quotes else None
    rows = []
    for board in manifest['boards']:
        saved = read_json(data/'snapshots/concept'/generation/'members'/f"{board['code']}.json",{})
        codes = {r['code'] for r in saved.get('rows',[])}
        matched = [quotes[c] for c in codes if c in quotes]
        full = bool(codes) and len(matched)==len(codes)
        change = sum(r['涨跌幅'] for r in matched)/len(matched) if full else None
        rows.append({**board,'count':len(codes),'covered':len(matched),
            'absolute_change_percent':change,'relative':change-average if full and average is not None else None,
            'breadth':sum(r['涨跌幅']>0 for r in matched)/len(matched)*100 if full else None,
            'amount':sum(r['成交额'] for r in matched) if full else None,
            'leaders':[{'code':r['代码'],'name':r.get('名称',''),'change':r['涨跌幅']} for r in sorted(matched,key=lambda r:r['成交额'],reverse=True)[:3]],
            'history_status':'无同口径连续概念历史，仅当日线索'})
    return {'rows':rows,'target_date':target,'membership_at':manifest.get('completed_at'),
            'message':'按已保存成员口径；重叠成员不计独立证据，历史成员未重建，不预测启动时长。'}


def snapshot(root, payload):
    """Supplement a frozen report without selecting new historical stocks."""
    history = read_json(root/'data/daily'/payload['target_date']/'history/summary.json',{})
    indices = {code:read_json(root/'data/daily'/payload['target_date']/f'index-{code}.json',{}).get('rows',[])
               for code in ('sh000001','sz399001','sh000300')}
    from history import bars
    normalized = {}
    for code, values in indices.items():
        if values:
            normalized[code] = bars(values,payload['target_date'])[0]
    result = build(payload,payload['decision'],history,normalized)
    concept_time = payload.get('snapshots',{}).get('概念成员')
    manifest = next((value for path in (root/'data/snapshots/concept').glob('*/manifest.json')
                     if (value:=read_json(path,{})).get('completed_at')==concept_time),{}) if concept_time else {}
    quotes = {r['代码']:r for r in read_json(root/'data/daily'/payload['target_date']/'market.json',{}).get('rows',[])}
    result['concepts'] = concept_facts(root/'data',manifest.get('generation',''),quotes,payload['target_date'])
    analytics = read_json(root/'data/analytics'/f"{payload['target_date']}.json",{})
    generation = analytics.get('industry_generation','')
    import hashlib
    result['watch_industries'] = {}
    watched = {r.get('code') for r in payload.get('watchlist_quotes',[])}
    for board in payload.get('industries',[]):
        members = read_json(root/'data/snapshots/industry-sw'/generation/'members'/f"{board['code']}.json",{}).get('rows',[])
        signature = hashlib.sha256(','.join(sorted(r['code'] for r in members)).encode()).hexdigest()
        if signature==board.get('member_signature'):
            for member in members:
                if member['code'] in watched:
                    result['watch_industries'][member['code']] = {'code':board['code'],'name':board['name']}
    result['mode'] = 'SNAPSHOT_REVIEW'
    result['candidate_note'] = '历史报告候选保持原名单；新的机会选股仅用于后续更新。'
    return result


def add_evidence(refs, opportunities):
    refs['opportunities'] = {'date':opportunities['target_date'],'title':'机会类型、条件与数据边界','facts':opportunities}
    for row in opportunities['sectors']:
        refs['opportunity:'+row['code']] = {'date':opportunities['target_date'],'title':row['name']+' · 机会条件','facts':row}


def review_conditions(root, record, days):
    """Check frozen conditions on dated bars and matching published membership."""
    from predictions import historical_bars
    original = record.get('input',{}).get('opportunities',{})
    results = []
    for signal in original.get('sectors',[]):
        if signal['type'] not in TYPES:
            continue
        rows, conflicts = historical_bars(root,'industry_history',signal['code'],days[-1])
        prices = {r['date']:r['close'] for r in rows}
        confirmed_at, invalidated_at, observations = None, None, []
        for day in days:
            publication = read_json(root/'data/publications'/f'{day}.json',{}).get('analysis',{})
            board = next((r for r in publication.get('industries',[]) if r['code']==signal['code']),{})
            comparable = bool(signal.get('member_signature')) and board.get('member_signature')==signal['member_signature']
            window = [r for r in rows if r['date']<=day]
            close = prices.get(day)
            index_up = close>window[-2]['close'] if close is not None and len(window)>1 else None
            above5 = close>sum(r['close'] for r in window[-5:])/5 if close is not None and len(window)>=5 else None
            above20 = close>sum(r['close'] for r in window[-20:])/20 if close is not None and len(window)>=20 else None
            relative = board.get('relative') if comparable else None
            breadth = board.get('breadth') if comparable else None
            confirm = {'index_up':index_up,'above_ma5':above5,
                       'relative_positive':relative>0 if relative is not None else None,
                       'breadth55':breadth>=55 if breadth is not None else None,'above_ma20':above20}
            invalid = {'below_signal_reference':close<signal['signal_reference'] if close is not None and signal.get('signal_reference') else None,
                       'relative_negative':relative<0 if relative is not None else None}
            if signal['type']=='衰退风险':
                five = (close/window[-6]['close']-1)*100 if close is not None and len(window)>=6 else None
                confirm = {'weak_after_rise':five>0 and (relative<0 or breadth<40)
                           if five is not None and relative is not None and breadth is not None else None}
                invalid = {'recovered_strength':relative>0 and breadth>=55
                           if relative is not None and breadth is not None else None}
            # Missing trading dates must not masquerade as a continuous moving window.
            required = [record['target_date'],*days[:days.index(day)+1]]
            if any(d not in prices for d in required) or day in conflicts:
                if signal['type']=='衰退风险':confirm['weak_after_rise']=None
                else:confirm['index_up'] = confirm['above_ma5'] = confirm['above_ma20'] = None
            if any(v is True for v in invalid.values()):
                invalidated_at = invalidated_at or day
            if all(v is True for v in confirm.values()) and not invalidated_at:
                confirmed_at = confirmed_at or day
            state = ('INVALIDATED' if invalidated_at else
                     'MISSING_DATA' if any(v is None for v in (*confirm.values(),*invalid.values())) else
                     'CONFIRMED' if confirmed_at else 'NOT_CONFIRMED')
            observations.append({'date':day,'status':state,'confirmation':confirm,'invalidation':invalid})
        results.append({'code':signal['code'],'name':signal['name'],'original_type':signal['type'],
            'status':observations[-1]['status'],'confirmed_at':confirmed_at,'invalidated_at':invalidated_at,
            'observations':observations,'note':'原条件复核，不计交易胜率；缺少同口径广度或相对表现时不可完整核验。'})
    return results


def validate_ai(data, opportunities):
    if data.get('schema_version') not in ('decision-ui-v2','decision-ui-v3') or not isinstance(data['market'].get('opportunity_bias'),str) or not 0<len(data['market']['opportunity_bias'])<=180:
        raise ValueError('机会AI结构不符')
    by_code = {r['code']:r for r in opportunities['sectors']}
    for row in data['sectors']:
        for field in ('hypothesis','alternative'):
            if not isinstance(row.get(field),str) or not row[field].strip() or len(row[field])>180:
                raise ValueError('机会解释或替代解释缺失')
        scenarios = row.get('scenarios',{})
        if not isinstance(scenarios,dict) or not all(isinstance(scenarios.get(k),str) and 0<len(scenarios[k])<=180 for k in ('启动','盘整','转弱')):
            raise ValueError('机会情景不完整')
        if row.get('scenario_bias') not in ('启动','盘整','转弱','无法区分'):
            raise ValueError('情景偏向无效')
        for field in ('confirmation_checks','invalidation_checks'):
            allowed = {c['id'] for c in by_code[row['code']][field]}
            values = row.get(field)
            if not isinstance(values,list) or set(values)!=allowed or len(set(values))!=len(values):
                raise ValueError('AI引用了未知或遗漏可执行条件')
