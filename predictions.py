"""Immutable issued research records and horizon reviews with no look-ahead."""
import hashlib
import math
import copy
from datetime import date, datetime, timedelta
from storage import read_json, read_optional_json, write_json, json_digest as digest

REVIEW_VERSION = 'review-v0.1.2'
FORWARD_SELECTION_VERSION = 'forward-selection-v1'


def trading_horizon(target, count):
    from scheduler import is_trading_day
    day = date.fromisoformat(target)
    days = []
    for _ in range(40):
        day += timedelta(days=1)
        trading = is_trading_day(day)
        if trading is None:
            return None
        if trading:
            days.append(day.isoformat())
        if len(days) == count:
            return days
    return None


def benchmark_history(root, incoming):
    sources = [read_json(root/'data/reviews/benchmark.json', {}).get('rows',[]), incoming]
    sources += [read_json(p,{}).get('rows',[]) for p in sorted((root/'data/daily').glob('*/index-sh000300.json'))]
    values, conflicts = {}, set(read_json(root/'data/reviews/benchmark.json',{}).get('conflicts',[]))
    for rows in sources:
        for row in rows:
            close = row.get('close')
            if not isinstance(close,(int,float)) or not math.isfinite(close) or close <= 0:
                continue
            stamp = str(row.get('date',''))[:10]
            if not stamp:
                continue
            try:
                date.fromisoformat(stamp)
            except ValueError:
                continue
            if stamp in values and abs(values[stamp]-close) > .011:
                conflicts.add(stamp)
            values[stamp] = close
    return [{'date':d,'close':v} for d,v in sorted(values.items()) if d not in conflicts], sorted(conflicts)


def semantic(value):
    if isinstance(value,dict):
        return {k:semantic(v) for k,v in value.items() if k not in ('generated_at','fetched_at','known_at','as_of')}
    if isinstance(value,list):
        return [semantic(v) for v in value]
    return value


def archive_ai_research(root, payload, result):
    if result.get('status') != 'READY' or result.get('business_status') != 'PASSED':
        return None
    record={'target_date':payload['target_date'],'issued_at':result['generated_at'],
        'mode':'INDEPENDENT_AI_RESEARCH','evidence_digest':result.get('evidence_digest'),
        'research_evidence':result.get('research_evidence'),
        'interpretation':result['interpretation'],'request_id':result['request_id'],
        'original_prediction_id':payload.get('prediction_id'),
        'note':'独立研究对象；不生成订单，不改写原规则预测，不计真实前瞻交易日'}
    record['id']=digest(record)[:20]
    path=root/'data/research-predictions'/f"{payload['target_date']}-{record['id']}.json"
    if not path.exists(): write_json(path,record)
    return record


def archive(root, payload, decision, evidence, now):
    original = {k:v for k,v in decision.items() if k not in ('generated_at','ai')}
    interpretation = decision.get('ai',{}).get('interpretation')
    identity = digest({'issued_at':now.isoformat(),
        'research':semantic({'input':payload,'rules':original,'interpretation':interpretation})})[:20]
    path = root/'data'/'predictions'/f'{payload["target_date"]}-{identity}.json'
    saved = read_json(path)
    if saved:
        return saved
    from version import PRODUCT_VERSION, PROMPT_VERSION
    ai=decision.get('ai',{})
    prompt=root/'prompts/decision_research.txt'
    if not prompt.is_file():
        from pathlib import Path
        prompt=Path(__file__).resolve().parent/'prompts/decision_research.txt'
    record = {'id':identity,'target_date':payload['target_date'],'issued_at':now.isoformat(),
        'schema_version':2,'review_algorithm_version':REVIEW_VERSION,
        'mode':'RESEARCH_SNAPSHOT','record_type':'RESEARCH_SNAPSHOT',
        'product_version':PRODUCT_VERSION,'market_baseline_date':payload['target_date'],
        'external_information_cutoff':payload.get('research_window',{}).get('external_information_cutoff'),
        'data_completeness':payload.get('data_completeness',{}),
        'ai_model_version':ai.get('model') or ai.get('parameters',{}).get('model'),
        'model_parameters':ai.get('parameters',{}),'prompt_version':PROMPT_VERSION,
        'prompt_sha256':hashlib.sha256(prompt.read_bytes()).hexdigest(),
        'rule_version':decision['version'],'candidate_rule_version':decision.get('candidate_version'),
        'version':decision['version'],'input_digest':digest(payload),'evidence':evidence,
        'input':payload,'decision':decision,
        'baseline':{'name':'同期相对强弱前5行业','codes':[r['code'] for r in payload['industries'][:5]]},
        'review_definition':{'horizons':[1,3,5],'benchmark':'沪深300指数收盘变化',
            'sector_hit':'行业指数绝对收益>0且跑赢沪深300；衰退：行业绝对收益<0或跑输沪深300',
            'stock_model':'仅状态可考虑参与且风险核验通过的条件单；次日以后日线核验，不假设信号日成交',
            'fee_percent':0.15,'slippage_percent':0.10,
            'cost_note':'研究假设，非实际券商费率；买卖合计费用另列，价格跳空与封板不得假定成交'}}
    record=copy.deepcopy(record)
    write_json(path,record)
    archive_ai_research(root,payload,decision.get('ai',{}))
    register_forward(root,record,now)
    return record


def forward_qualification(record, now):
    """Only actual completed evidence can predict its next, still unopened session."""
    from collector import CST
    from scheduler import is_trading_day
    try:
        baseline=date.fromisoformat(record['target_date'])
        if not is_trading_day(baseline):return None
        future=trading_horizon(baseline.isoformat(),1)
        if not future:return None
        day=date.fromisoformat(future[0])
        completed=datetime.combine(baseline,datetime.min.time(),CST).replace(hour=16)
        deadline=datetime.combine(day,datetime.min.time(),CST).replace(hour=9,minute=15)
        issued=datetime.fromisoformat(record['issued_at']).astimezone(CST)
        cutoff=datetime.fromisoformat(record['external_information_cutoff']).astimezone(CST)
        ai=record['decision']['ai']
        model_time=datetime.fromisoformat(ai['generated_at']).astimezone(CST)
        core=record['data_completeness']['required']
        if (completed<=cutoff<=model_time<=issued<=now.astimezone(CST)<deadline
                and ai.get('status')=='READY' and ai.get('business_status')=='PASSED'
                and ai.get('evidence_status')!='STALE'
                and isinstance(core,list) and bool(core) and all(isinstance(r,dict) and r.get('ok') is True for r in core)
                and record.get('ai_model_version') and record.get('prompt_version')):
            return {'prediction_date':day.isoformat(),
                'forecast_phase':'PRE_MARKET' if issued.date()==day else 'POST_MARKET',
                'selection_deadline':deadline.isoformat()}
    except (KeyError,ValueError,TypeError):
        pass
    return None


def register_forward(root, source, now):
    qualification=forward_qualification(source,now)
    if not qualification:return None
    identity=digest({'source':source['id'],'official_date':qualification['prediction_date']})[:20]
    path=root/'data/forward-forecasts'/f"{qualification['prediction_date']}-{identity}.json"
    if path.exists():return read_json(path)
    record={**copy.deepcopy(source),**qualification,'id':identity,
        'source_snapshot_id':source['id'],'record_type':'OFFICIAL_FORWARD_FORECAST','mode':'FORWARD',
        'frozen_at':now.isoformat(),'forward_selection_version':FORWARD_SELECTION_VERSION}
    write_json(path,record)
    return record


def forward_records(root, errors=None):
    errors=errors if errors is not None else []
    paths=[*(root/'data/forward-forecasts').glob('*.json'),
           *(root/'data/predictions').glob('*-official-forward.json')]
    records={}
    for path in sorted(paths):
        record=read_optional_json(path,errors)
        if not record:continue
        try:
            date.fromisoformat(record['prediction_date'])
            if datetime.fromisoformat(record['issued_at']).tzinfo is None:raise ValueError('missing timezone')
            qualification=forward_qualification(record,datetime.fromisoformat(record['frozen_at']))
            if not qualification or qualification['prediction_date']!=record['prediction_date']:raise ValueError('unqualified evidence')
            if record['record_type']=='OFFICIAL_FORWARD_FORECAST':records[record['id']]=record
        except (KeyError,ValueError,TypeError):
            errors.append(f'{path.name} 前瞻记录格式无效，保留基本研究，请恢复备份')
    return list(records.values())


def selected_forwards(root, now, errors=None):
    """Selection uses only immutable pre-deadline issue times, never outcomes."""
    selected={}
    for record in forward_records(root,errors):
        issued=datetime.fromisoformat(record['issued_at'])
        if issued>now:continue
        day=record['prediction_date']
        previous=selected.get(day)
        if previous is None or (issued,record['id'])>(datetime.fromisoformat(previous['issued_at']),previous['id']):
            selected[day]=record
    return selected


def forecast_status(root, now=None):
    """Read-only display; never promote a historical snapshot during browsing."""
    from collector import CST, now_cst
    from scheduler import is_trading_day, previous_trading_day
    now=(now or now_cst()).astimezone(CST)
    day=now.date()
    if is_trading_day(day) and now.hour<16:
        target=day.isoformat()
    else:
        baseline=day if is_trading_day(day) and now.hour>=16 else previous_trading_day(day)
        future=trading_horizon(baseline.isoformat(),1) if baseline else None
        target=future[0] if future else None
    errors=[]
    record=selected_forwards(root,now,errors).get(target)
    if record:
        phase=record.get('forecast_phase') or ('PRE_MARKET' if record['issued_at'][:10]==target else 'POST_MARKET')
        state=('PRE_MARKET' if phase=='PRE_MARKET' else 'POST_MARKET_FALLBACK') if target==day.isoformat() else 'NEXT_SESSION'
        return {k:record.get(k) for k in ('id','source_snapshot_id','prediction_date','market_baseline_date','issued_at','external_information_cutoff')}|{'status':state,'data_errors':errors}
    return {'status':'RECENT_RESEARCH','prediction_date':target,'data_errors':errors}


def freeze_forward(root, prediction_date, *, snapshot_id=None, now=None):
    """Compatibility entry point; new successful runs already freeze automatically."""
    from collector import CST, now_cst
    now=(now or now_cst()).astimezone(CST)
    candidates=[r for r in forward_records(root) if r['prediction_date']==prediction_date
        and datetime.fromisoformat(r['issued_at'])<=now
        and (not snapshot_id or r['source_snapshot_id']==snapshot_id)]
    if not candidates:
        raise ValueError('没有对应交易日已真实保存的合格盘后或盘前预测；不能提前伪造或事后补写')
    return max(candidates,key=lambda r:(datetime.fromisoformat(r['issued_at']),r['id']))


def historical_bars(root, kind, code, target):
    """Merge dated publications without silently preferring conflicting sources."""
    by_date = {}
    conflicts = set()
    for daily in sorted((root/'data'/'daily').glob('????-??-??')):
        saved = read_json(daily/'history'/f'{kind}-{code}.json',{})
        for row in saved.get('rows',[]):
            if row['date'] <= target:
                old = by_date.get(row['date'])
                fields = ('close','open','high','low','volume') if kind == 'candidate_history' else ('close',)
                if old and any(old.get(k) != row.get(k) for k in fields):
                    conflicts.add(row['date'])
                by_date[row['date']] = row
    return [v for k,v in sorted(by_date.items()) if k not in conflicts], sorted(conflicts)


def stock_outcome(plan, rows, horizon_day, fees, slippage, *, expected_entry_day=None, conflicts=()):
    if plan['state'] != '可考虑参与' or plan.get('risk_verified') is not True:
        return {'status':'ABSTAIN','reason':'关键风险未核验，原判断仅观察或回避；不计为交易胜率'}
    entry = plan['entry']
    low, high = entry.get('low'),entry.get('high')
    if low is None or high is None or low > high:
        return {'status':'NO_VALID_ORDER'}
    if not rows:
        return {'status':'MISSING_DATA'}
    if expected_entry_day in conflicts:
        return {'status':'UNRESOLVED','reason':'真正次日行情存在冲突，不顺延成交'}
    if expected_entry_day is None:
        return {'status':'UNRESOLVED','reason':'缺少可核验的预期次交易日，不顺延成交'}
    first = next((r for r in rows if r['date'] == expected_entry_day),None)
    if first is None:
        return {'status':'MISSING_DATA','reason':'真正次日行情缺失，不顺延成交'}
    if any(not isinstance(first.get(key),(int,float)) or not math.isfinite(first[key]) for key in ('open','high','low','volume')):
        return {'status':'UNRESOLVED','reason':'日线缺少成交核验字段'}
    if first['open'] <= 0 or first['volume'] <= 0 or first['high'] == first['low']:
        return {'status':'UNEXECUTABLE','reason':'无成交或一字价格；不假定封板可成交'}
    if not low <= first['open'] <= high:
        return {'status':'NOT_TRIGGERED','reason':'次日开盘未在区间；日内触发顺序未知不强行成交'}
    if any(r['date'] == horizon_day for r in rows):
        end = next(r for r in rows if r['date'] == horizon_day)
        # Corporate actions must be known before raw prices are used for returns.
        if not plan.get('corporate_actions_verified'):
            return {'status':'UNRESOLVED','reason':'除权除息尚未核验，不以不复权价差冒充净收益'}
        gross = (end['close']/first['open']-1)*100
        return {'status':'SIMULATED','entry_date':first['date'],'gross_percent':gross,
                'cost_percent':fees+slippage,'net_percent':gross-fees-slippage}
    return {'status':'MISSING_DATA'}


def opinion_at_issue(record, issued):
    if record['mode']=='INDEPENDENT_AI_RESEARCH':
        return True
    try:
        opinion_time=datetime.fromisoformat(issued)
        original_time=datetime.fromisoformat(record['issued_at'])
        return opinion_time <= original_time
    except (ValueError,TypeError):
        return False


def evaluate(root, record, benchmark, now):
    target = record['target_date']
    benchmark, benchmark_conflicts = benchmark_history(root,benchmark)
    prices = {r['date']:r['close'] for r in benchmark if r['date'] <= now.date().isoformat()}
    future = trading_horizon(target,5)
    if future is None:
        return {'id':record['id'],'target_date':target,'mode':record['mode'],'version':record['version'],
            'issued_at':record['issued_at'],'updated_at':now.isoformat(),'review_version':REVIEW_VERSION,
            'outcomes':[{'horizon':h,'status':'CALENDAR_UNKNOWN','message':'交易日历未核验'} for h in (1,3,5)]}
    outcomes = []
    for horizon in (1,3,5):
        day = future[horizon-1]
        if day > now.date().isoformat() or (day == now.date().isoformat() and now.hour < 16):
            outcomes.append({'horizon':horizon,'date':day,'status':'PENDING','message':'等待到期交易日收盘'})
            continue
        if any(d not in prices for d in [target,*future[:horizon]]):
            outcomes.append({'horizon':horizon,'date':day,'status':'MISSING_DATA',
                'message':'已到期，基准行情数据不足','conflicts':benchmark_conflicts})
            continue
        benchmark_return = (prices[day]/prices[target]-1)*100
        sectors = []
        for sector in record['decision']['sectors']:
            rows, conflicts = historical_bars(root,'industry_history',sector['code'],day)
            close = {r['date']:r['close'] for r in rows}
            if target not in close or day not in close:
                sectors.append({'code':sector['code'],'state':sector['state'],'status':'MISSING_DATA','conflicts':conflicts})
                continue
            change = (close[day]/close[target]-1)*100
            signal = sector['state'] in ('启动候选','强势延续观察','衰退预警')
            hit = (change < 0 or change < benchmark_return) if sector['state'] == '衰退预警' else (change > 0 and change > benchmark_return)
            sectors.append({'code':sector['code'],'state':sector['state'],
                'status':'EVALUATED' if signal else 'ABSTAIN','hit':hit if signal else None,
                'absolute_percent':change,'excess_percent':change-benchmark_return,
                'failure_type':None if hit or not signal else '方向或相对表现未成立；时机与归因需另核验'})
        stocks = []
        rules = record['review_definition']
        for plan in record['decision']['stocks']:
            rows,conflicts = historical_bars(root,'candidate_history',plan['code'],day)
            rows = [r for r in rows if target < r['date'] <= day]
            stocks.append({'code':plan['code'],**stock_outcome(plan,rows,day,rules['fee_percent'],rules['slippage_percent'],
                expected_entry_day=future[0],conflicts=conflicts)})
        ai = record['decision'].get('ai',{})
        ai_research = []
        for opinion in ai.get('interpretation',{}).get('sectors',[]):
            item={'code':opinion['code'],'object_type':'SECTOR','opinion':opinion.get('scenario_bias','无法区分'),
                'issued_at':ai.get('generated_at'),
                'evidence_digest':digest(ai.get('research_evidence',{})),'status':'RESEARCH_ONLY'}
            if not opinion_at_issue(record,item['issued_at']):
                item.update(status='NOT_AT_SIGNAL',reason='后生成AI研究不冒充原预测')
            elif item['opinion'] in ('启动','转弱'):
                rows,conflicts=historical_bars(root,'industry_history',opinion['code'],day)
                close={r['date']:r['close'] for r in rows}
                if target in close and day in close:
                    change=(close[day]/close[target]-1)*100
                    item.update(status='EVALUATED',absolute_percent=change,
                        hit=change>0 if item['opinion']=='启动' else change<0,
                        definition='AI独立情景方向核验；不计交易收益')
                else:item.update(status='MISSING_DATA',conflicts=conflicts)
            ai_research.append(item)
        for opinion in ai.get('interpretation',{}).get('entry_brief',{}).get('stocks',[]):
            item = {'code':opinion['code'],'opinion':opinion['decision'],
                'issued_at':ai.get('generated_at'),
                'evidence_digest':digest(ai.get('research_evidence',{})), 'status':'ABSTAIN'}
            if not opinion_at_issue(record,item['issued_at']):
                item.update(status='NOT_AT_SIGNAL',reason='AI意见后生成，不作为信号日原预测')
            elif opinion['decision'] == '值得买入':
                rows, conflicts = historical_bars(root,'candidate_history',opinion['code'],day)
                close = {r['date']:r['close'] for r in rows}
                if target in close and day in close:
                    change=(close[day]/close[target]-1)*100
                    item.update(status='EVALUATED',absolute_percent=change,hit=change>0,
                        definition='独立AI方向研究的收盘变化；不是模拟成交或收益')
                else:
                    item.update(status='MISSING_DATA',conflicts=conflicts)
            ai_research.append(item)
        sequence = [prices[target],*[prices[d] for d in future[:horizon]]]
        peak, drawdown = sequence[0], 0
        for close in sequence[1:]:
            peak=max(peak,close)
            drawdown=min(drawdown,(close/peak-1)*100)
        base = [s for s in sectors if s['code'] in record['baseline']['codes'] and 'absolute_percent' in s]
        outcomes.append({'horizon':horizon,'date':day,'status':'EVALUATED',
            'benchmark_percent':benchmark_return,'sectors':sectors,'stocks':stocks,
            'rule_plans':{'sectors':sectors,'stocks':stocks},'ai_research':ai_research,
            'simulated_trades':[s for s in stocks if s['status']=='SIMULATED'],
            'denominators':{'rule_signals':sum(s['status']=='EVALUATED' for s in sectors),
                'ai_research':sum(s['status']=='EVALUATED' for s in ai_research),
                'simulated_trades':sum(s['status']=='SIMULATED' for s in stocks)},
            'baseline':{'covered':len(base),'expected':len(record['baseline']['codes']),
                'mean_excess_percent':sum(s['excess_percent'] for s in base)/len(base) if base else None},
            'market_action':record['decision']['market']['action'],
            'market_result':{'benchmark_percent':benchmark_return,
                'max_drawdown_percent':drawdown,
                'max_signal_decline_percent':min(0,*[(prices[d]/prices[target]-1)*100 for d in future[:horizon]])},
            'duration_review':'持续时间区间需逐日状态序列；未齐备时不据单个期限宣称命中'})
        if record.get('input',{}).get('opportunities'):
            from opportunities import review_conditions
            outcomes[-1]['opportunity_conditions'] = review_conditions(root,record,future[:horizon])
    return {'id':record['id'],'target_date':target,'mode':record['mode'],'version':record['version'],
            'record_type':record.get('record_type'),'prediction_date':record.get('prediction_date'),
            'issued_at':record['issued_at'],'updated_at':now.isoformat(),'review_version':REVIEW_VERSION,'outcomes':outcomes}


def evaluate_ai_research(root, research, benchmark, now):
    from collector import CST
    from scheduler import is_trading_day, previous_trading_day
    issued=datetime.fromisoformat(research['issued_at']).astimezone(CST)
    reference=issued.date() if is_trading_day(issued.date()) and issued.hour >= 16 else previous_trading_day(issued.date())
    if not reference:
        return {'id':research['id'],'issued_at':research['issued_at'],'evidence_date':research['target_date'],
            'mode':'INDEPENDENT_AI_RESEARCH','reference_date':None,'outcomes':[
                {'horizon':h,'status':'CALENDAR_UNKNOWN','message':'研究发表时点的交易日历未核验'} for h in (1,3,5)]}
    record={'id':research['id'],'target_date':reference.isoformat(),'mode':'INDEPENDENT_AI_RESEARCH',
        'version':REVIEW_VERSION,'issued_at':research['issued_at'],'baseline':{'codes':[]},
        'review_definition':{'fee_percent':.15,'slippage_percent':.10},
        'decision':{'market':{'action':'独立AI研究'},'sectors':[],'stocks':[],
            'ai':{'generated_at':research['issued_at'],'interpretation':research['interpretation'],
                  'research_evidence':research.get('research_evidence')}}}
    result=evaluate(root,record,benchmark,now)
    result.update(reference_date=reference.isoformat(),evidence_date=research['target_date'],
        note='按真实发表时点之后的交易日核验，以此前最近完成收盘为参考；不是原预测、模拟成交或交易收益，不计真实前瞻样本')
    return result


def review(root, benchmark, now):
    benchmark, conflicts = benchmark_history(root,benchmark)
    write_json(root/'data/reviews/benchmark.json',{'rows':benchmark,'conflicts':conflicts})
    records = [read_json(p) for p in sorted((root/'data'/'predictions').glob('*.json'))]
    records = list({r['id']:r for r in [*records,*forward_records(root)]}.values())
    selected=selected_forwards(root,now)
    counted={r['id'] for day,r in selected.items() if day<=now.date().isoformat()}
    results = [evaluate(root,r,benchmark,now) for r in records]
    for row in results:
        row['counts_as_forward']=row['id'] in counted
        old = read_json(root/'data/reviews'/f"{row['id']}.json",{})
        if old.get('review_version') == REVIEW_VERSION:
            completed = {o['horizon']:o for o in old.get('outcomes',[]) if o['status']=='EVALUATED'}
            row['outcomes'] = [completed.get(o['horizon'],o) if o['status'] in ('PENDING','MISSING_DATA') else o for o in row['outcomes']]
        write_json(root/'data'/'reviews'/f"{row['id']}.json",row)
    forward = {day for day in selected if day<=now.date().isoformat()}
    research_records=[read_json(p) for p in sorted((root/'data/research-predictions').glob('*.json'))]
    research_reviews=[evaluate_ai_research(root,r,benchmark,now) for r in research_records]
    for row in research_reviews:write_json(root/'data/reviews'/f"ai-{row['id']}.json",row)
    summary = {'records':results,'forward_days':len(forward),'preliminary_minimum_days':20,
               'official_forward_dates':sorted(forward),
               'selected_forward_forecasts':{day:{k:r.get(k) for k in ('id','source_snapshot_id','issued_at','forecast_phase')} for day,r in selected.items()},
               'forward_selection_version':FORWARD_SELECTION_VERSION,
               'ai_research_records':research_records,'ai_research_reviews':research_reviews,
               'effect_status':'尚待验证' if len(forward) < 20 else '达到初步样本门槛，仍需多环境评估',
               'improvement_policy':'保留原判断，记录误报、缺口和版本；不每日自动修改规则',
               'limitations':['回放不计入前瞻样本','观察和无法成交不计交易胜率','资金与AI增量尚无充分样本']}
    write_json(root/'data'/'reviews'/'summary.json',summary)
    return summary
