"""Explain existing participation gates and recover already-saved evidence."""
from storage import read_optional_json
from version import QUALIFICATION_VERSION
from decision import ENTRY_SCORE


def evidence_digest(status):
    from research_context import material_digest
    # The exact day of a late fetch cannot grant historical qualification.
    stocks=[{**s,'business_date':'POST_TARGET' if s.get('business_late') else s.get('business_date')}
            for s in status.get('stocks',[])]
    return material_digest({**status,'stocks':stocks})


def snapshot(root, payload):
    target = payload['target_date']
    market = payload.get('decision', {}).get('market', {})
    boards = {s['code']:s for s in payload.get('opportunities', {}).get('sectors', [])}
    stocks = []
    errors = []
    for plan in payload.get('decision', {}).get('stocks', []):
        code = plan['code']
        folder = root/'data'/'daily'/target/'history'
        info = (read_optional_json(folder/f'stock_info-{code}.json', errors).get('rows') or [{}])[0]
        notices = (read_optional_json(folder/f'candidate_notices-{code}.json', errors).get('rows') or [{}])[0]
        task = read_optional_json(root/'data/checkpoint.json', errors).get('tasks', {}).get(
            f'history:{target}:candidate_notices:{code}', {})
        fetched = notices.get('date') == target and task.get('status','SUCCESS') == 'SUCCESS'
        items = notices.get('items', [])
        body_verified = bool(items) and all(r.get('body_verified') is True and r.get('body') and r.get('url') for r in items)
        notice_status = ('SOURCE_FAILED' if not fetched else 'FETCHED_EMPTY' if not items else
                         'BODY_VERIFIED_SAMPLE' if body_verified else 'TITLE_SAMPLE')
        business = info.get('facts', {}).get('主营业务')
        trend = plan.get('history')
        late = bool(info.get('date') and info['date'] > target)
        issues = []
        if not trend:
            issues.append({'item':'个股历史', 'impact':'不能判断趋势位置', 'action':'下次更新重试已有历史接口'})
        if not business:
            issues.append({'item':'主营资料', 'impact':'不能核对业务与题材关联', 'action':'下次更新重试已有公司资料接口'})
        elif late:
            issues.append({'item':'业务时点', 'impact':f"资料为{info['date']}后补，不能作为{target}当时证据",
                           'action':'可用于当前了解业务；原预测保留原时点'})
        issues.append({'item':'公告核验', 'impact':'首页标题样本不等于风险全文已审阅',
                       'action':'打开已存公告核对全文、减持与重大风险；空样本不等于无风险'})
        board = boards.get(plan.get('industry', {}).get('code'), {})
        checks = board.get('confirmation_checks', [])
        close, ma20 = (trend or {}).get('close'), (trend or {}).get('ma20')
        gates = [
            {'item':'市场达到原参与门槛', 'met':market.get('score') is not None and market['score'] >= ENTRY_SCORE and market.get('action') != '回避'},
            {'item':'原板块确认条件全部满足', 'met':bool(checks) and all(c.get('met') is True for c in checks)},
            {'item':'个股未触发原MA20重审', 'met':close is not None and ma20 is not None and close >= ma20},
            {'item':'已有连续历史与同期主营', 'met':bool(trend) and bool(business) and bool(info.get('date')) and not late},
            {'item':'已有记录未命中标题风险', 'met':plan.get('state') != '暂不参与' and not plan.get('risk_events')},
            {'item':'同期公告源成功取得（空样本仍有风险缺口）', 'met':fetched},
        ]
        stocks.append({'code':code, 'name':plan.get('name', code),
            'buy_allowed':all(g['met'] for g in gates), 'participation_checks':gates,
            'rule_state':plan.get('state'), 'business':business, 'business_date':info.get('date'),
            'business_late':late, 'history_samples':trend.get('samples') if trend else None,
            'notice_date':notices.get('date'), 'notice_scope':notices.get('scope', '公告样本未取得'),
            'notice_status':notice_status, 'full_risk_verified':False,
            'evidence_qualification':'有限研究证据' if fetched else '公告源失败，风险资格不足',
            'notices':notices.get('items', []), 'issues':issues,
            'price_context':{k:trend.get(k) for k in ('close','ma5','ma20','return5_percent','amount_ratio5')} if trend else {},
            'release_condition':plan.get('entry', {}).get('condition') or '先补齐历史与风险核验，再等已有板块确认条件',
            'source_ids':['history:stock:'+code, 'research:'+code]})
    return {'target_date':target, 'qualification_version':QUALIFICATION_VERSION,
            'market_action':market.get('action'), 'score':market.get('score'),
            'data_errors':errors,
            'change_condition':'完整总分达到原65分参与门槛，且未触发原风险回避条件；按后续同口径行情重算。',
            'missing_dimensions':[d['name'] for d in market.get('dimensions', []) if d.get('score') is None],
            'stocks':stocks,
            'limits':[
                {'item':'估值与极端情绪', 'impact':'不作低估或抄底确认；不妨碍现有趋势条件核对', 'action':'现有范围无该证据，保持未取得'},
                {'item':'国家队身份与金额', 'impact':'不作身份和金额归因；现有资金支持指标仍可计算', 'action':'只接受具名公开披露'},
                {'item':'行业资金映射', 'impact':'东方财富估算净额不作申万行业或个股流入证据', 'action':'独立展示已验收128行业资金'},
                {'item':'次日与持续时间', 'impact':'未到期不计结果；样本不足不预测持续天数', 'action':'下一份合格报告核对1/3/5交易日条件'},
            ]}


def add_evidence(refs, status):
    refs['participation'] = {'date':status['target_date'], 'title':'参与判断与证据待办', 'facts':status}
    for stock in status['stocks']:
        refs['research:'+stock['code']] = {'date':status['target_date'],
            'title':stock['name']+' · 已有资料与具体核验事项', 'facts':stock}


def validate_brief(brief, status, opportunities):
    if not isinstance(brief, dict):
        raise ValueError('入场简报缺失')
    expected = ('回避' if status['market_action'] == '回避' else
                '暂停判断' if status['score'] is None else
                '值得入场' if status['market_action'] == '可考虑参与' else '暂不入场')
    if brief.get('decision') != expected:
        raise ValueError('入场结论与原规则不一致')
    for key in ('reason', 'change_condition', 'risk'):
        if not isinstance(brief.get(key), str) or not 0 < len(brief[key]) <= (600 if key=='reason' else 150):
            raise ValueError('入场说明缺失或过长')
    if brief['change_condition'] != status['change_condition']:
        raise ValueError('不能自创市场参与阈值')
    focus = brief.get('focus')
    allowed = set(opportunities.get('selected_codes', []))
    if not isinstance(focus, list) or len(focus) > 3 or not all(isinstance(r, dict) for r in focus):
        raise ValueError('重点方向最多3个')
    seen = set()
    boards = {s['code']:s for s in opportunities.get('sectors', [])}
    for row in focus:
        if row.get('code') not in allowed or row['code'] in seen:
            raise ValueError('重点板块未知或重复')
        seen.add(row['code'])
        if row.get('trigger') != '；'.join(c['label'] for c in boards[row['code']]['confirmation_checks']):
            raise ValueError('不能自创板块确认条件')
        for key in ('verdict', 'why', 'trigger', 'risk'):
            if not isinstance(row.get(key), str) or not 0 < len(row[key]) <= 120:
                raise ValueError('重点方向说明缺失或过长')
        if any(word in ' '.join(row[key] for key in ('verdict','why','trigger','risk'))
               for word in ('净流入','吸筹','国家队','必涨','暗盘')):
            raise ValueError('重点方向使用未核验归因')
    rows = brief.get('stocks', [])
    expected_stocks = {s['code']:s for s in status['stocks']}
    if (not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows)
            or len(rows) != len(expected_stocks) or {s.get('code') for s in rows} != set(expected_stocks)):
        raise ValueError('个股结论范围不符')
    for row in rows:
        if row.get('decision') not in ('值得买入','先不买','不买'):
            raise ValueError('个股参与结论无效')
        if row['decision'] == '值得买入' and not expected_stocks[row['code']]['buy_allowed']:
            raise ValueError('不能把观察线索升级为买入')
        if any(word in row.get('reason', '') for word in ('权限','解锁','未开')):
            raise ValueError('研究状态不能写成软件权限')
        if row.get('release_condition') != expected_stocks[row['code']]['release_condition']:
            raise ValueError('不能自创个股参与条件')
        for key in ('reason','release_condition'):
            if not isinstance(row.get(key), str) or not 0 < len(row[key]) <= 150:
                raise ValueError('个股参与理由缺失或过长')
