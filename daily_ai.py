"""Compact model transport; program facts and conditions retain their full saved evidence."""
import copy

MODEL_SCHEMA='decision-research-compact-v3'


def pick(value,keys):
    return {key:value[key] for key in keys if key in value}


def research_event_ids(request):
    """Only sampled events and adopted policy evidence are model-visible."""
    ids={e['id'] for group in ('original_items','supplemental_items')
         for e in request['facts'].get('research:events',{}).get(group,[])}
    ids.update(request.get('policy_assessment',{}).get('source_ids',[]))
    return ids


def compact_request(request):
    result=copy.deepcopy(request)
    for key in ('market','candidates','coverage'):result.pop(key,None)
    rules=result['decision_rules']
    rules['sectors']=[pick(r,('code','name','state')) for r in rules['sectors']]
    rules['stocks']=[pick(r,('code','name','industry','state')) for r in rules['stocks']]
    rules['market']=pick(rules['market'],('action','score','dimensions','coverage_percent','score_basis'))
    if 'research:events' in result['facts']:
        result['facts']['events']={'scope':'正文与标题在research:events；不重复发送'}
    def prune(value):
        if isinstance(value,dict):return {k:prune(v) for k,v in value.items() if k not in ('rows','history_rows','source_ids','missing_dates','conflict_dates','dates','issues','notices')}
        if isinstance(value,list):return [prune(v) for v in value]
        return value
    definitions={}
    for key,facts in list(result['facts'].items()):
        if key=='participation':
            result['facts'][key]=pick(facts,('market_action','score','change_condition','missing_dimensions','qualification_version'))
        elif key.startswith('research:') and key.split(':',1)[1].isdigit():
            result['facts'][key]=pick(facts,('code','name','buy_allowed','rule_state','business','business_date','business_late',
                'notice_date','notice_scope','notice_status','full_risk_verified','evidence_qualification','price_context','release_condition','current_notices'))
            result['facts'][key]['notice_sample_count']=len(facts.get('notices',[]))
            if isinstance(facts.get('business'),str):result['facts'][key]['business']=facts['business'][:500]
        elif key=='research:events':
            def event(e):
                item=pick(e,('id','date','time','title','source','url','excerpt','evidence_status','time_relation'))
                if 'excerpt' in item:item['excerpt']=item['excerpt'][:900]
                return item
            from research_context import research_event_sample
            result['facts'][key]={**pick(facts,('as_of','scope')),
                'original_items':[event(e) for e in research_event_sample(facts.get('original_items',[]),6)],
                'supplemental_items':[event(e) for e in research_event_sample(facts.get('supplemental_items',[]),6)]}
        elif key.startswith('history:industry:'):
            result['facts'][key]=pick(facts.get('trend',{}),('date','close','ma5','ma20','return5_percent','amount_ratio5','samples'))
        elif key.startswith('history:stock:'):
            result['facts'][key]={'facts_alias':'research:'+key.rsplit(':',1)[1]}
        elif key.startswith('opportunity:'):
            row=prune(facts);row.pop('condition_text',None)
            for field in ('confirmation_checks','invalidation_checks'):
                for check in row.get(field,[]):
                    if check['id'] not in definitions:definitions[check['id']]=check['label']
                    if definitions[check['id']]==check['label']:check.pop('label')
            result['facts'][key]=row
        else:result['facts'][key]=prune(facts)
    if 'research:funds' in result['facts']:
        result['facts']['funds']=pick(result['facts'].get('funds',{}),('positive_share_percent','board_observed_count','detail_scope'))
        result['facts']['research:funds'].pop('corrections',None)
        funds=result['facts']['research:funds']
        funds['boards']=[pick(r,('code','name','net_cny','windows')) for r in funds['boards']]
        funds['stocks']=[pick(r,('code','net_cny','windows','price_fund_relation')) for r in funds['stocks']]
        funds['etfs']=[pick(r,('code','name','date','shares','change_shares','unit')) for r in funds['etfs']]
        funds['margin'].pop('sz_message',None)
        funds['margin']['sz_status']='READY' if funds['margin'].get('sz_balance_cny') is not None else 'DATA_INSUFFICIENT'
    if definitions:result['condition_definitions']=definitions
    event_ids=research_event_ids(result)
    result['evidence']={k:pick(v,('date',)) for k,v in result['evidence'].items()
                        if not k.startswith('event:') or k in event_ids}
    return result


def expand_output(data,decision,participation,opportunities):
    if not isinstance(data,dict):raise ValueError('模型输出不是对象')
    if data.get('schema_version') not in ('decision-research-compact-v1','decision-research-compact-v2',MODEL_SCHEMA):return data
    if data['schema_version']==MODEL_SCHEMA and not isinstance(data.get('market',{}).get('forecast'),dict):
        raise ValueError('新版市场研究必须包含预测')
    if data.get('target_date')!=decision['target_date']:raise ValueError('模型日期不匹配')
    if set(data)-{'schema_version','target_date','market','sectors','stocks','event_research','focus','policy_assessment','fact_claims'}:
        raise ValueError('模型不能生成程序控制字段')
    result=copy.deepcopy(data);result['schema_version']='decision-ui-v3'
    result.pop('fact_claims',None)
    market=result['market']
    if 'confirmation' in market:raise ValueError('模型不能改写程序条件')
    market['confirmation']=participation['change_condition']
    opportunities={s['code']:s for s in (opportunities or {}).get('sectors',[])}
    sector_map={s['code']:s for s in result['sectors']}
    for row in result['sectors']:
        row.pop('price_facts',None)
        if any(k in row for k in ('confirmation_checks','invalidation_checks','confirmation','invalidation','next_day')):
            raise ValueError('模型不能改写板块程序条件')
        opportunity=opportunities.get(row['code'],{})
        for field in ('confirmation','invalidation'):
            checks=opportunity.get(field+'_checks',[])
            row[field+'_checks']=[c['id'] for c in checks]
            row[field]='；'.join(c['label'] for c in checks)
        if not isinstance(row['scenarios'],dict):raise ValueError('板块情景不是对象')
        row['next_day']=row['scenarios'].get(row['scenario_bias']) or '；'.join(row['scenarios'].values())
    qualifications={s['code']:s for s in participation['stocks']}
    originals={s['code']:s for s in decision['stocks']}
    stock_brief=[]
    for row in result['stocks']:
        row.pop('price_facts',None)
        if any(k in row for k in ('confirmation','invalidation','buy_allowed','release_condition')):
            raise ValueError('模型不能改写个股程序资格')
        qualification=qualifications[row['code']]
        stock_brief.append({'code':row['code'],'decision':row.pop('decision'),'reason':row['judgment'],
            'release_condition':qualification['release_condition']})
        row['confirmation']=qualification['release_condition']
        row['invalidation']=originals[row['code']].get('exit',{}).get('condition') or '出现新的风险证据时重新核对'
    focus=result.pop('focus')
    if not isinstance(focus,list) or len(focus)>3 or len(set(focus))!=len(focus):raise ValueError('重点代码不符')
    result.update(summary=market['headline'],counterevidence=market['risk'],entry_brief={
        'decision':'暂停判断' if participation['score'] is None else {'可考虑参与':'值得入场','回避':'回避'}.get(participation['market_action'],'暂不入场'),
        'reason':market['judgment'],'change_condition':participation['change_condition'],'risk':market['risk'],
        'focus':[{'code':code,'verdict':sector_map[code]['judgment'],'why':sector_map[code]['priority_reason'],
            'trigger':'；'.join(c['label'] for c in opportunities[code]['confirmation_checks']),
            'risk':sector_map[code]['invalidation']} for code in focus],'stocks':stock_brief})
    return result
