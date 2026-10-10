"""Dated research facts; observation updates never alter rule scores or forecasts."""
import hashlib
import json
import math
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from collector import CST
from scheduler import is_trading_day, previous_trading_day
from storage import read_optional_json, write_json, json_digest

VERSION='research-context-v1'
ASSETS=(('SP500','标普500','us','.INX'),('NASDAQ','纳斯达克','us','.IXIC'),
        ('HSI','恒生指数','hk','HSI'),('USDCNH','美元/离岸人民币','fx','USDCNH'),
        ('OIL','WTI原油期货','future','CL'),('GOLD','黄金期货','future','GC'),
        ('US10Y','美国10年国债收益率','yield','US10Y'))


def completed_asset_date(kind,at):
    local=at.astimezone(ZoneInfo('Asia/Hong_Kong' if kind=='hk' else 'America/New_York'))
    return (local.date() if local.hour>=17 else local.date()-timedelta(days=1)).isoformat()


def number(value):
    return value if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else None


def trading_dates(target,count):
    day=date.fromisoformat(target)
    result=[]
    for _ in range(45):
        status=is_trading_day(day)
        if status is None:return []
        if status:result.append(day.isoformat())
        if len(result)==count:return list(reversed(result))
        day-=timedelta(days=1)
    return []


def series(rows,field,target):
    values={}
    for row in rows:
        if not isinstance(row,dict):continue
        day=str(row.get('date',row.get('日期','')))[:10]
        value=number(row.get(field))
        if day and day<=target and value is not None:values.setdefault(day,set()).add(value)
    return {d:next(iter(v)) for d,v in values.items() if len(v)==1},[d for d,v in values.items() if len(v)>1]


def windows(rows,field,target):
    values,conflicts=series(rows,field,target)
    result={}
    for count in (1,3,5):
        expected=trading_dates(target,count)
        missing=[d for d in expected if d not in values]
        conflict=[d for d in expected if d in conflicts]
        status='CONFLICT' if conflict else 'READY' if len(expected)==count and not missing else 'MISSING_DATA'
        samples=[values[d] for d in expected if d in values]
        result[str(count)]={'status':status,'net_cny':sum(samples) if status=='READY' else None,
            'positive_days':sum(v>0 for v in samples) if status=='READY' else None,
            'missing_dates':missing,'conflict_dates':conflict,'dates':expected}
    return result,values


def context_digest(context,stock_codes=None):
    if stock_codes is not None:
        stocks={r['code']:r for r in context['funds']['stocks']}
        context={**context,'funds':{**context['funds'],
            'stocks':[stocks[code] for code in stock_codes if code in stocks]}}
    return material_digest(context)


def material_digest(value):
    """Refresh timestamps do not change facts; publication dates and failures do."""
    def normalize(item):
        if isinstance(item,dict):
            return {k:normalize(v) for k,v in item.items() if k not in
                    ('known_at','as_of','fetched_at','data_errors','issues','message','sz_message','source_error','scope','mapping','limits','limitations')}
        if isinstance(item,list):
            return sorted((normalize(v) for v in item),key=lambda v:json.dumps(v,ensure_ascii=False,sort_keys=True))
        if isinstance(item,str):
            return item.replace('供应商','数据来源').replace('东方财富行业分类','数据来源行业').replace('东方财富估算净额','数据来源净额')
        return item
    return json_digest(normalize(value))


def changed_modules(saved,current,stock_codes=None):
    def funds(c):
        value=c.get('funds',{})
        return {**value,'stocks':[r for r in value.get('stocks',[])
                if stock_codes is None or r['code'] in stock_codes]}
    parts={'funds':(funds(saved),funds(current)),
           'international':(saved.get('international'),current.get('international')),
           'events':(saved.get('events'),current.get('events')),
           'companies':(saved.get('company_updates'),current.get('company_updates')),
           'activity':(saved.get('activity'),current.get('activity'))}
    return [name for name,(before,after) in parts.items() if material_digest(before)!=material_digest(after)]


def event_id(event):
    value=[event.get(k,'') for k in ('title','time','url')]
    return 'event:'+hashlib.sha256(json.dumps(value,ensure_ascii=False).encode()).hexdigest()[:16]


def event_category(event):
    # Browsing labels only; these never grant policy scores or verified facts.
    title=event.get('title','')
    if event.get('excerpt') and any(w in event.get('source','') for w in ('人民银行官网','财政部官网','国务院官网')):
        return '国内官方政策'
    if any(w in title for w in ('美国','美联储','欧元','欧洲','日本','日经','港股','恒生','美元','WTI','原油','霍尔木兹','以色列','伊朗','关税')):
        return '国际'
    if any(w in title for w in ('产业','商品','工业','制造','科技','芯片','人工智能','AI','航空','能源','化工','旅游','文旅','景区','地产','融资','消费')):
        return '产业与经济'
    return '其他资讯'


def research_event_sample(events, limit):
    """Reserve a place for each observed theme, then fill by recency."""
    ordered=sorted(events,key=lambda e:str(e.get('time') or e.get('date') or ''),reverse=True)
    chosen=[]
    for category in ('国内官方政策','国际','产业与经济','其他资讯'):
        match=next((e for e in ordered if e.get('category',event_category(e))==category),None)
        if match is not None and len(chosen)<limit:chosen.append(match)
    for event in ordered:
        if len(chosen)>=limit:break
        if event not in chosen:chosen.append(event)
    return chosen


def build_context(root,payload,*,include_updates=False,observation_directory='observations'):
    target=payload['target_date'];daily=root/'data/daily'/target
    errors=[]
    observations=daily/observation_directory
    information_cutoff=payload.get('research_window',{}).get('external_information_cutoff')
    def load(path):
        saved=read_optional_json(path,errors)
        if include_updates and information_cutoff and saved.get('fetched_at') and observation_directory=='observations':
            try:
                if datetime.fromisoformat(saved['fetched_at'])>datetime.fromisoformat(information_cutoff):
                    errors.append(f'{path.name} 获取晚于研究截止时间，未用于本次判断')
                    return {}
            except (ValueError,TypeError):
                errors.append(f'{path.name} 获取时间无效，未用于本次判断')
                return {}
        for key,kind in (('rows',list),('history_rows',list),('params',dict),
                ('statuses',dict),('tasks',dict),('international',dict)):
            if key in saved and (not isinstance(saved[key],kind)
                    or key in ('rows','history_rows') and any(not isinstance(r,dict) for r in saved[key])
                    or key in ('statuses','tasks','international') and any(not isinstance(r,dict) for r in saved[key].values())):
                errors.append(f'{path.name} 的 {key} 结构损坏，跳过可选补充；原报告保留')
                return {}
        return saved
    dates=trading_dates(target,5)
    observed=load(observations/'summary.json')
    tasks=load(root/'data/checkpoint.json').get('tasks',{})
    funds=payload.get('decision',{}).get('funds',{})
    boards=[];corrections=[]
    for item in funds.get('board_flows',[]):
        code=item['code'];name=item['name']
        rows=[{'日期':item.get('date'),'主力净流入-净额':item.get('net_cny')}]
        for day in dates:
            saved=load(root/'data/daily'/day/'enrichment'/f'flow-{code}.json')
            params=saved.get('params',{})
            if params.get('symbol',name)!=name:continue
            original=saved.get('rows',[])
            matched=[r for r in original if str(r.get('日期',''))[:10]==target]
            if day==target and saved.get('kind')=='board_flow' and params=={'symbol':name,'code':code,'target_date':target} \
                    and len(matched)==1 and len(original)>1 and str(original[0].get('日期',''))[:10]!=target \
                    and number(item.get('net_cny')) is not None \
                    and number(original[0].get('主力净流入-净额'))==number(item.get('net_cny')) \
                    and number(matched[0].get('主力净流入-净额')) is not None:
                corrections.append({'code':code,'name':name,'reason':'LEGACY_FIRST_ROW_MISDATED',
                    'old_actual_date':str(original[0]['日期'])[:10],'old_net_cny':item['net_cny'],
                    'corrected_date':target,'corrected_net_cny':matched[0]['主力净流入-净额'],
                    'source_file':str((root/'data/daily'/day/'enrichment'/f'flow-{code}.json').relative_to(root)),
                    'source_sha256':json_digest(saved)})
                rows.pop(0)  # Remove only the proven misdated derived value; keep all source rows and conflicts.
            rows.extend(saved.get('rows',[]))
            rows.extend(saved.get('history_rows',[]))
        rows.extend(load(observations/f'fund-{code}.json').get('rows',[]))
        metrics,values=windows(rows,'主力净流入-净额',target)
        boards.append({'code':code,'name':name,'net_cny':metrics['1']['net_cny'],
            'windows':metrics,'dates':sorted(values),'source':item.get('source','东方财富行业资金'),
            'scope':'东方财富行业分类，未映射申万；单位元；订单分类统计'})
    stock_quotes={q.get('code',q.get('代码')):q for q in payload.get('watchlist_quotes',[])}
    for q in payload.get('candidates',[]):stock_quotes.setdefault(q.get('代码'),q)
    stocks=[]
    for code,q in stock_quotes.items():
        rows=[]
        for day in dates:rows.extend(load(root/'data/daily'/day/f'flow-{code}.json').get('rows',[]))
        metrics,_=windows(rows,'主力净流入-净额',target)
        net=metrics['1']['net_cny'];change=number(q.get('涨跌幅'))
        relation=('价涨净额为负' if change is not None and net is not None and change>0 and net<0 else
                  '价跌净额为正' if change is not None and net is not None and change<0 and net>0 else
                  '同向' if change is not None and net is not None and change*net>0 else '无法确认')
        stocks.append({'code':code,'name':q.get('name',q.get('名称',code)),
            'net_cny':net,'windows':metrics,'price_fund_relation':relation,'price_change_percent':change,
            'message':tasks.get(f'daily:{target}:flow:{code}',{}).get('error','未取得完整资金序列') if net is None else ''})
    prior=previous_trading_day(date.fromisoformat(target))
    prior_day=prior.isoformat() if prior else None
    before=load(root/'data/publications'/f'{prior_day}.json').get('analysis',{}) if prior else {}
    amount=number(payload.get('market',{}).get('turnover_cny'))
    old=number(before.get('market',{}).get('turnover_cny'))
    etfs=[]
    old_etfs=load(root/'data/daily'/str(prior_day)/'history/etf_shares.json').get('rows',[]) if prior else []
    for row in funds.get('etf_shares',[]):
        values,conflicts=series([r for r in old_etfs if str(r.get('code'))==str(row.get('code'))],'shares',target)
        shares=number(row.get('shares'));old_shares=values.get(prior_day)
        etfs.append({**row,'change_shares':shares-old_shares if shares is not None and old_shares is not None
            and row.get('date')==target and prior_day not in conflicts else None,
            'unit':'来源原始份额单位','note':'份额变化，不能换称现金净流入'})
    margin=funds.get('margin',{})
    sz=load(observations/'margin-sz.json').get('rows',[])
    sz_values,sz_conflicts=series(sz,'balance_cny',target)
    sz_balance=sz_values.get(target) if target not in sz_conflicts else None
    sz_task=tasks.get(f'observation:{target}:margin_sz:{target}',{})
    sz_status=observed.get('statuses',{}).get('margin_sz',{})
    sz_error=(sz_task if sz_task and (not sz_status or sz_task.get('updated_at','')>observed.get('as_of','')) else sz_status).get('error')
    sh_balance=number(margin.get('balance_cny')) if margin.get('date')==target else None
    international=[]
    cutoff=observed.get('cutoff') if include_updates else target
    try:
        stamp=(information_cutoff or observed.get('as_of')) if include_updates else None
        information_time=datetime.fromisoformat(stamp) if stamp else None
    except (ValueError,TypeError):
        errors.append('国际观察截止时间损坏，仅保留原行情日范围')
        information_time=None
    history=load(daily/'history/summary.json')
    for code,name,kind,symbol in ASSETS:
        saved=load(observations/f'{code}.json') if include_updates else {}
        if not saved and code=='SP500':saved=history.get('international',{}).get('.INX',{})
        rows=saved.get('rows',[])
        # Observations are completed daily rows; their local calendar differs from A shares.
        asset_limit=completed_asset_date(kind,information_time) if information_time else cutoff or target
        values,conflicts=series(rows,'close',asset_limit)
        ordered=sorted(values)
        last=ordered[-1] if ordered else None
        value=values.get(last);previous=values.get(ordered[-2]) if len(ordered)>1 else None
        international.append({'code':code,'name':name,'kind':kind,'date':last,'value':value,
            'change_percent':(value/previous-1)*100 if kind!='yield' and previous and value is not None else None,
            'change_bp':(value-previous)*100 if kind=='yield' and value is not None and previous is not None else None,
            'source':rows[-1].get('source','') if rows else '',
            'status':'READY' if last else 'CONFLICT' if conflicts else 'MISSING_DATA',
            'message':observed.get('statuses',{}).get(code,{}).get('error','未取得该资产可核验日线') if not last else ''})
    original=[]
    for e in payload.get('enrichment',{}).get('events',[]):
        if e.get('date','')<=target and e.get('title'):
            original.append({**e,'id':event_id(e),'category':event_category(e),
                'evidence_status':'OFFICIAL_EXCERPT' if e.get('excerpt') else 'TITLE_SAMPLE'})
    original.sort(key=lambda e:not bool(e.get('excerpt')))
    feed=load(observations/'news.json').get('rows',[])
    if not feed:feed=load(daily/'enrichment/latest-news.json').get('rows',[])
    batch=feed[0] if feed and isinstance(feed[0],dict) else {}
    supplemental=[];seen=set();as_of=information_cutoff or batch.get('as_of') or observed.get('as_of')
    company_updates={}
    if include_updates:
        policy=load(observations/'policy.json')
        for row in policy.get('rows',[]):
            batch.setdefault('items',[]).extend(row.get('items',[]))
        for candidate in payload.get('candidates',[]):
            code=candidate['代码'];saved=load(observations/f'notices-{code}.json')
            rows=saved.get('rows',[])
            task=observed.get('statuses',{}).get('NOTICE:'+code,{})
            if not task and not rows:continue
            fetched=task.get('status')=='SUCCESS' and bool(rows)
            items=rows[0].get('items',[]) if fetched else []
            company_updates[code]={'status':'TITLE_SAMPLE' if items else 'FETCHED_EMPTY' if fetched else 'SOURCE_FAILED',
                'known_at':saved.get('fetched_at'),'items':items,
                'scope':'截止研究时点已取得的公告首页样本；不证明完整风险已核验',
                'message':task.get('error','')}
            batch.setdefault('items',[]).extend(items)
    if include_updates and as_of:
        try:
            at=datetime.fromisoformat(as_of)
            for e in batch.get('items',[]):
                published=datetime.fromisoformat(e['time'])
                if published.tzinfo is None:published=published.replace(tzinfo=CST)
                key=(e['title'].strip(),published.isoformat())
                if published>at or key in seen or urlsplit(e.get('url','')).scheme not in ('https','http'):continue
                seen.add(key)
                supplemental.append({**e,'id':event_id(e),'category':event_category(e),'known_at':as_of,
                    'evidence_status':'OFFICIAL_EXCERPT' if e.get('excerpt') else 'UNVERIFIED_REPORT',
                    'time_relation':'LATER_UPDATE' if published.date().isoformat()>target else 'DATED_SAMPLE'})
        except (KeyError,ValueError,TypeError):errors.append('资讯样本结构或时间损坏，未用于研究')
    counts={'observed':sum(r['net_cny'] is not None for r in boards),'expected':funds.get('board_expected',0),
        'original_observed':len(boards),'conflicted':sum(r['windows']['1']['status']=='CONFLICT' for r in boards)}
    return {'version':VERSION,'target_date':target,'mode':'SUPPLEMENTAL_RESEARCH' if include_updates else 'ORIGINAL_DATE',
        'activity':{'turnover_cny':amount,'previous_date':prior_day,
            'change_percent':(amount/old-1)*100 if amount is not None and old and old>0 else None,
            'message':'前一交易日全市场快照缺失，不能比较全市场成交额；指数均线与5日走势另有历史数据' if old is None else ''},
        'funds':{'boards':boards,'stocks':stocks,'coverage':counts,'corrections':corrections,'mapping':'未核验数据来源与申万成员映射，禁止跨分类归因',
            'margin':{'sh':margin,'sz_balance_cny':sz_balance,'date':target,
                'sz_message':sz_error or '未取得深市同日融资数据' if sz_balance is None else '',
                'combined_balance_cny':sh_balance+sz_balance if sh_balance is not None and sz_balance is not None else None},
            'etfs':etfs,'national_team':funds.get('national_team',{'status':'UNKNOWN','message':'未取得具名持仓/交易披露'}),
            'cross_border':{'status':'UNAVAILABLE','message':'尚未接入当前披露口径的跨境成交/持仓，不提供实时净流入'}},
        'international':international,**({'company_updates':company_updates} if company_updates else {}),
        'events':{'original_items':original,'supplemental_items':supplemental,
            'as_of':as_of if include_updates else None,'scope':batch.get('scope') or '有限资讯样本；报道未独立核验，不代表完整覆盖',
            'source_status':observed.get('statuses',{}).get('NEWS',{}).get('status','UNKNOWN'),
            'source_error':observed.get('statuses',{}).get('NEWS',{}).get('error','') or batch.get('page_error',''),
            'sample_start':min((e.get('time') or e.get('date') for e in supplemental),default=None)},
        'comparison':{'date':prior_day,'available':bool(before),'market':before.get('market'),
            'sectors':before.get('industries',[])},'data_errors':errors}


def add_evidence(refs,context):
    for key,title,facts in [('research:funds','资金金额、真实序列及口径',context['funds']),
            ('research:activity','成交关注与前一交易日比较',context['activity']),
            ('research:international','国际资产已结束日线',context['international']),
            ('research:events','原日事件与独立时点新增报道',context['events']),
            ('research:comparison','前一交易日事实',context['comparison'])]:
        refs[key]={'title':title,'date':context['target_date'],'facts':facts}
    for event in context['events']['original_items'][:20]+context['events']['supplemental_items'][:20]:
        refs[event['id']]={'title':event['title'],'date':event.get('date'),'facts':event}


def collect_observations(collector,target,*,historical=False):
    from enrichment import news_feed_validator, policy_validator, event_validator
    daily=collector.data/'daily'/target/('historical-observations' if historical else 'observations')
    at=collector.clock().astimezone(CST)
    if historical:
        at=datetime.fromisoformat(target+'T23:59:59+08:00')
    as_of=at.isoformat();cutoff=(at.date()-timedelta(days=1)).isoformat()
    statuses={}
    # Completed asset rows can be reused; each run refreshes time-bound public information.
    jobs=[('NEWS','news_feed',{'as_of':as_of},daily/'news.json',news_feed_validator(as_of))]
    def validate(rows,limit=cutoff):
        values,conflicts=series(rows,'close',limit)
        if conflicts or not values or any(v<=0 for v in values.values()):raise ValueError('国际日线为空、冲突或非有效值')
        kept=[r for r in rows if str(r.get('date',r.get('日期','')))[:10] in values]
        return kept,max(values)
    for code,name,kind,symbol in ASSETS:
        asset_cutoff=completed_asset_date(kind,at)
        jobs.append((code,'international_observation',{'kind':kind,'symbol':symbol,'cutoff':asset_cutoff},daily/f'{code}.json',
                     lambda rows,limit=asset_cutoff:validate(rows,limit)))
    publication=read_optional_json(collector.data/'publications'/f'{target}.json',[]).get('analysis',{})
    jobs.append(('POLICY','policy',{'target_date':at.date().isoformat(),'as_of':as_of},
        daily/'policy.json',policy_validator(at.date().isoformat())))
    for q in publication.get('candidates',[])[:6]:
        code=q['代码']
        jobs.append(('NOTICE:'+code,'candidate_notices',{'code':code,'start_date':target,
            'target_date':at.date().isoformat(),'as_of':as_of},daily/f'notices-{code}.json',
            event_validator(at.date().isoformat(),start_date=target,as_of=as_of)))
    for code,kind,params,path,validator in jobs:
        key=f'observation:{target}:{at.date()}:{code}'
        success=collector._fetch(key,kind,params,path,validator)
        task=collector.checkpoint['tasks'].get(key,{})
        statuses[code]={'status':'SUCCESS' if success else task.get('status','PENDING'),'error':task.get('error','等待续采') if not success else ''}
    prior=previous_trading_day(date.fromisoformat(target))
    if prior:
        day=prior.isoformat()
        def etf_validator(rows):
            if not rows or any(r.get('date')!=day or not r.get('code') or number(r.get('shares')) is None
                    or r['shares']<0 for r in rows) or len({r['code'] for r in rows})!=len(rows):
                raise ValueError('ETF份额缺少同代码目标日有效记录')
            return rows,day
        collector._fetch(f'history:{day}:etf_shares','etf_shares',{'target_date':day,'end_date':day.replace('-','')},
            collector.data/'daily'/day/'history/etf_shares.json',etf_validator)
    def margin_validator(rows):
        if len(rows)!=1 or number(rows[0].get('balance_cny')) is None:raise ValueError('融资汇总无有效金额')
        return rows,rows[0]['date']
    for day in ([prior.isoformat(),target] if prior else [target]):
        key=f'observation:{target}:margin_sz:{day}'
        success=collector._fetch(key,'margin_sz',{'target_date':day},
            collector.data/'daily'/day/'observations/margin-sz.json',margin_validator)
        if day==target:
            task=collector.checkpoint['tasks'].get(key,{})
            statuses['margin_sz']={'status':'SUCCESS' if success else task.get('status','PENDING'),
                'error':task.get('error','等待续采') if not success else ''}
    for q in publication.get('candidates',[])[:6]:
        code=q['代码']
        collector._fetch(f'daily:{target}:flow:{code}','stock_flow',{'code':code,
            'market':'sh' if code.startswith('6') else 'sz','target_date':target},
            collector.data/'daily'/target/f'flow-{code}.json',collector._daily_validator('stock_flow'))
    boards=publication.get('decision',{}).get('funds',{}).get('board_flows',[])
    ranked=sorted((b for b in boards if number(b.get('net_cny')) is not None),key=lambda b:b['net_cny'])
    chosen={b['code']:b for b in ranked[:3]+ranked[-3:]}
    for code,b in chosen.items():
        collector._fetch(f'observation:{target}:fund:{code}','board_flow',{'code':code,'symbol':b['name'],'target_date':target},
            daily/f'fund-{code}.json',collector._daily_validator('board_flow'))
    result={'as_of':as_of,'cutoff':cutoff,'statuses':statuses}
    write_json(daily/'summary.json',result)
    return result


def coverage_report(payload,context):
    """Explain actual gaps without turning optional channels into missing core facts."""
    target=payload['target_date'];funds=context['funds'];gaps=[]
    def add(key,title,status,cause,impact,action):
        gaps.append({'id':key,'title':title,'status':status,'cause':cause,'impact':impact,'action':action})
    policy=next((d for d in payload.get('decision',{}).get('market',{}).get('dimensions',[])
                 if d['id']=='policy'),{})
    if number(policy.get('score')) is None:
        events=context['events'];official=any(e.get('excerpt') and e.get('evidence_status')=='OFFICIAL_EXCERPT'
            and e.get('date','9999')<=target for group in ('original_items','supplemental_items') for e in events[group])
        review=payload.get('ai_review') or payload.get('decision',{}).get('ai',{})
        assessed=review.get('ai_policy_assessment') if review.get('business_status')=='PASSED' \
            and review.get('fact_validation',{}).get('status')!='FAILED' \
            and (not review.get('evidence_status') or review.get('policy_evidence_status')=='CURRENT') else None
        add('policy','原政策评分','补评已取得' if assessed else '材料已取得' if official else '缺正文样本',
            '同期官方正文已取得；原报告没有政策分，历史评分保留。'+('补评单独保存。' if assessed else '本轮没有可采用的独立政策补评。') if official else '未取得可用于评分的同期官方正文，标题不能代替。',
            '原总分保留缺项；现有补评单列，不自动改变个股条件。' if assessed else '原总分不能合计；政策意见与股指走势分开判断。',
            '已单独补评，点击政策分查看正文依据；原报告保留。' if assessed else '更新研究时根据同期正文单独补评；后补信息不覆盖原分。' if official else '补采同期官方正文后再评，不填中性分。')
    if number(funds['margin'].get('sz_balance_cny')) is None:
        error=funds['margin'].get('sz_message','')
        cause='所选日期返回空记录。' if 'FETCHED_EMPTY' in error else '官网连接被远端中断，未取得所选日期记录。' \
            if 'ConnectionResetError' in error else '接口等待超时，未取得所选日期记录。' \
            if 'timeout' in error.lower() or '超过' in error else '最近获取失败，未取得所选日期记录。'
        add('margin','沪深融资','深市未取得',cause+'沪市已有同日余额。',
            '不能判断沪深合计融资变化；不影响指数趋势和当日上涨占比。',
            '保留沪市结果；待深交所同日记录可用后补采。')
    etfs=funds.get('etfs',[]);missing=sum(number(e.get('change_shares')) is None for e in etfs)
    if missing:
        add('etf','ETF份额变化',f'{missing}/{len(etfs)}项待比较','缺少同代码前一交易日份额，或日期/记录不一致。',
            '不能确认份额增减；份额变化也不等于现金净流入。','按前一交易日补采同代码份额，保留日期与单位。')
    if not context['comparison']['available']:
        add('comparison','全市场日间比较','前日快照未保存',f"未保存{context['comparison']['date']}正式全市场快照；股指历史另有数据。",
            '全市场成交额与上涨占比无法环比；股指5日走势和均线仍可判断。','从下一份正式快照连续比较；补采历史时保持同一股票范围。')
    if funds.get('boards'):
        add('mapping','板块资金归因','分类不同','资金用东方财富行业分类，行情用申万分类；成员对应关系尚未验证。',
            '可以看资金分类分布，不能把它解释为某申万行业的资金流入。','取得并核对两套分类成员后再对应；当前按各自口径展示。')
    if funds.get('cross_border',{}).get('status')=='UNAVAILABLE':
        add('cross_border','跨境资金','尚未接入','当前披露口径的成交或持仓数据尚未接入。',
            '不支持实时跨境净流入判断；不能用旧口径补值。','接入可核验的成交或持仓披露，明确区间和口径。')
    codes={s['code'] for s in payload.get('decision',{}).get('stocks',[])}
    stocks=[s for s in payload.get('research_evidence',{}).get('stocks',[]) if s.get('code') in codes]
    unverified=sum(s.get('full_risk_verified') is not True for s in stocks)
    if unverified:
        add('risk','候选股公告风险',f'{unverified}/{len(stocks)}只未完整查齐','当前取得主营与有限公告样本，未完成全部风险核查。',
            '不确认个股可买；不否定已取得的市场行情。','逐股检查公告正文、风险事项与报告时点，结果单独列出。')
    return {'gaps':gaps,'etf_comparable':len(etfs)-missing,'etf_total':len(etfs),
            'funds_observed':funds['coverage']['observed'],'funds_expected':funds['coverage']['expected']}
