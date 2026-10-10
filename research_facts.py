"""Small, deterministic checks for the price facts used in research prose."""
import math
import re


def claim_template(request):
    claims=[]
    def add(path):
        value=request
        try:
            for key in path:value=value[key]
        except (KeyError,TypeError,IndexError):return
        if not isinstance(value,(dict,list)):claims.append({'path':path,'value':value})
    for path in (['target_date'],['decision_rules','market','score'],['decision_rules','market','action']):add(path)
    for i in range(len(request['decision_rules']['market'].get('dimensions',[]))):
        add(['decision_rules','market','dimensions',i,'score'])
    facts=request.get('facts',{})
    for code in facts.get('history:indices',{}):
        for field in ('date','close','ma5','ma20'):add(['facts','history:indices',code,field])
    for field in ('breadth_percent','equal_weight_change_percent','turnover_cny'):add(['facts','market',field])
    for field in ('positive_share_percent','board_observed_count'):add(['facts','funds',field])
    for field in facts.get('research:funds',{}).get('margin',{}):add(['facts','research:funds','margin',field])
    international=facts.get('research:international',{})
    rows=international if isinstance(international,list) else international.get('assets',[])
    prefix=['facts','research:international']+([] if isinstance(international,list) else ['assets'])
    for i,row in enumerate(rows):
        for field in ('symbol','date','close','change_percent','change_bp'):add([*prefix,i,field])
    return claims


def validate_research_claims(data, request):
    """Reject known contradictions; never repair the model's response text."""
    if not isinstance(data,dict):raise ValueError('模型返回不是对象')
    if data.get('target_date')!=request['target_date']:raise ValueError('研究日期错误')
    market=request['decision_rules']['market']
    if market.get('score_basis')=='final-effective-v1':
        expected=market['action'] if market.get('score') is not None else '证据不足'
        if data.get('market',{}).get('stance')!=expected:
            raise ValueError('AI入场状态与最终有效评分冲突')
    def strings(value):
        if isinstance(value,str):yield value
        elif isinstance(value,dict):
            for key,item in value.items():
                if key not in ('source_ids','evidence_urls','fact_claims'):yield from strings(item)
        elif isinstance(value,list):
            for item in value:yield from strings(item)
    text='。'.join(strings(data.get('market',{})))
    policy=request.get('policy_assessment')
    if policy and policy.get('score') is not None and re.search(r'政策(?:评|计)分.{0,8}(?:缺失|未计入|未取得|不足)|缺乏完整政策计分',text):
        raise ValueError('已取得政策评分却声称缺失')
    funds=request.get('facts',{}).get('research:funds',{})
    if funds.get('margin',{}).get('sz_balance_cny') is None:
        for clause in re.split(r'[。；]',text):
            if re.search(r'沪深融资.{0,16}(?:余额|增加|减少)',clause) and not re.search(r'不能|无法|未取得|不完整|不合计',clause):
                raise ValueError('只有沪市融资数据却声称沪深合计')
    for clause in re.split(r'[。；]',text):
        if 'ETF' in clause and '份额' in clause:
            for relation in re.finditer(r'(?:只能|等于|代表|反映).{0,6}(?:申购|赎回|现金|资金流入)',clause):
                if not re.search(r'不|不能|并非',clause[max(0,relation.start()-3):relation.start()]):
                    raise ValueError('ETF份额被等同申赎或现金')
        if re.search(r'不覆盖改善型|排除改善|不支持改善型',clause):
            raise ValueError('政策样本未支持对全部改善需求的排除')
    forecast=data.get('market',{}).get('forecast',{})
    for scenario in forecast.get('scenarios',{}).values():
        if re.search(r'政策(?:评|计)分',str(scenario.get('trigger',''))):
            raise ValueError('预测触发条件不能把主观评分当市场事实')
    event_ids={e['id'] for group in ('original_items','supplemental_items')
        for e in request.get('facts',{}).get('research:events',{}).get(group,[])}
    event_ids.update(key for key in request.get('evidence',{}) if key.startswith('event:'))
    for row in data.get('event_research',[]):
        if row.get('event_id') not in event_ids:raise ValueError('事件引用对象不存在')
    if data.get('schema_version')=='decision-research-compact-v3':
        claims=data.get('fact_claims')
        if not isinstance(claims,list) or not claims:raise ValueError('缺少可机械核对的事实清单')
        if request.get('fact_claims') and claims!=request['fact_claims']:
            raise ValueError('事实清单未按输入完整引用')
        for claim in claims:
            if not isinstance(claim,dict) or set(claim)!={'path','value'} or not isinstance(claim['path'],list):
                raise ValueError('事实清单格式错误')
            value=request
            try:
                for key in claim['path']:value=value[key]
            except (KeyError,IndexError,TypeError):raise ValueError('事实引用路径不存在') from None
            stated=claim['value']
            numeric=type(value) in (int,float) and type(stated) in (int,float)
            if isinstance(value,(dict,list)) or (not math.isclose(value,stated,rel_tol=0,abs_tol=1e-8) if numeric else
                    type(value) is not type(stated) or value!=stated):
                raise ValueError('事实日期、数值或方向与输入不一致')
        # Check common declarative numeric statements as well as the claim list.
        facts=request.get('facts',{})
        comparison=facts.get('research:comparison',{})
        previous=comparison.get('market',{}) if comparison.get('available') else {}
        number=r'([+-]?\d+(?:\.\d+)?)%'
        for label,key in ((r'上涨(?:股票占比|广度|家数占比)','breadth_percent'),(r'等权(?:涨跌幅?|收益)','equal_weight_change_percent')):
            current=facts.get('market',{}).get(key)
            for clause in re.split(r'[，,。；;]',text):
                for found in re.finditer(label+r'(?:为|约|是|达到|仅|从|由|：|:|\s)*'+number,clause):
                    prefix=clause[:found.start()]
                    if re.search(r'若|如果',prefix):continue
                    transition=re.match(r'\s*(?:升至|降至|增至|减至|转为|转|变为|变成)\s*'+number,clause[found.end():])
                    dates=re.findall(r'前一交易日|前一日|昨日|当日|当前|今日',prefix)
                    value=previous.get(key) if transition or dates and dates[-1] in ('前一交易日','前一日','昨日') else current
                    if value is None or abs(float(found[1])-value)>.1:
                        raise ValueError('正文市场比例或方向错误')
                    if transition and (current is None or abs(float(transition[1])-current)>.1):
                        raise ValueError('正文市场比例或方向错误')


def price_facts(row, target):
    trend = row.get('trend') or row.get('history') or {}
    def number(key):
        value = trend.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None
    close, ma5, ma20 = (number(key) for key in ('close', 'ma5', 'ma20'))
    return {'date':trend.get('date') or target, 'close':close, 'ma5':ma5, 'ma20':ma20,
        'above_ma5':close > ma5 if close is not None and ma5 is not None else None,
        'above_ma20':close > ma20 if close is not None and ma20 is not None else None}


def validate_price_facts(data, decision, *, require=False):
    if not isinstance(data,dict):
        raise ValueError('模型事实陈述不是对象')
    for kind in ('sectors', 'stocks'):
        originals = {row['code']:row for row in decision.get(kind, [])}
        for row in data.get(kind, []):
            if not isinstance(row,dict):
                raise ValueError('模型事实条目不是对象')
            if row.get('code') not in originals:
                continue  # Candidate validation reports unknown codes separately.
            facts = price_facts(originals[row['code']], decision.get('target_date'))
            claim = row.get('price_facts')
            if claim is not None or require and facts['close'] is not None:
                expected = {key:facts[key] for key in ('date', 'above_ma5', 'above_ma20')}
                if not isinstance(claim, dict) or set(claim) != set(expected) or any(
                        type(claim[key]) is not type(value) or claim[key] != value for key, value in expected.items()):
                    raise ValueError(row['code']+' 均线事实陈述与保存行情不一致')
            # Only declarative current-price statements, not future conditions.
            for field in ('judgment', 'hypothesis', 'changes', 'risk'):
                for clause in re.split(r'[，。；;]', str(row.get(field, ''))):
                    if re.search(r'若|如果|后续|未来|需|才能|风险|可能', clause):
                        continue
                    for period in (5, 20):
                        average = facts['ma'+str(period)]
                        line = rf'(?:{period}(?:日|天)(?:均)?线|MA{period})'
                        subject = r'(?:收盘|股价|价格)' if kind=='stocks' else r'(?:收盘|指数|价格)'
                        below = re.search(rf'{subject}.{{0,8}}(?:未站上|未突破|低于|跌破){line}', clause)
                        above = re.search(rf'{subject}.{{0,8}}(?:高于|站上|突破){line}', clause)
                        invalid_below=(facts['close'] is None or average is None or
                            facts['close'] > average or facts['close']==average and not re.search(r'未站上|未突破',clause))
                        if below and invalid_below:
                            raise ValueError(row['code']+f' 文本声称低于MA{period}，与保存行情不一致')
                        if above and not below and (facts['close'] is None or average is None or facts['close'] <= average):
                            raise ValueError(row['code']+f' 文本声称高于MA{period}，与保存行情不一致')
