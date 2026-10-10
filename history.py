"""Point-in-time history and bounded candidate research using the shared collector."""
import math
from datetime import date, timedelta

from collector import CST, REQUIRED_DAILY_COLUMNS, membership_signature
from storage import read_json, write_json


def bars(rows, target, *, minimum=1):
    normalized = []
    for raw in rows:
        day = str(raw.get('date', raw.get('日期', '')))[:10]
        date.fromisoformat(day)
        if day > target:
            continue
        row = {'date': day}
        for field in ('close', 'open', 'high', 'low', 'volume', 'amount'):
            source = {'close':'收盘', 'open':'开盘', 'high':'最高', 'low':'最低',
                      'volume':'成交量', 'amount':'成交额'}[field]
            value = raw.get(field, raw.get(source))
            if value is not None:
                value = float(value)
                if not math.isfinite(value) or value < 0 or (field == 'close' and value == 0):
                    raise ValueError('历史行情包含无效值')
                row[field] = value
        if 'close' not in row:
            raise ValueError('历史行情缺少收盘价')
        row['source'] = raw.get('source', 'AKShare，来源见适配器')
        normalized.append(row)
    normalized.sort(key=lambda r:r['date'])
    if len(normalized) < minimum or len({r['date'] for r in normalized}) != len(normalized):
        raise ValueError('历史窗口不足或日期重复')
    return normalized, normalized[-1]['date']


def comparable(data, previous, industry):
    current = membership_signature(data, industry)
    manifest = read_json(data/'snapshots'/'industry-sw'/previous.get('industry_generation', '')/'manifest.json')
    old = membership_signature(data, manifest)
    if previous.get('membership_signature') and old != previous['membership_signature']:
        return False
    return bool(current and old and current == old)


def shortlist(quotes, industries, members, maximum=6, *, opportunities=None):
    """Inspect liquid stocks across leading industries, including quiet candidates."""
    if opportunities is not None:
        from opportunities import balanced, VERSION
        boards = balanced([r for r in opportunities['sectors'] if r['type'] in ('潜在启动','反转观察','趋势跟随')], maximum=8)
        pools = []
        for board in boards:
            pool = [r for code,r in quotes.items() if members.get(code,{}).get('code')==board['code']
                    and r['成交额']>=1e8 and 'ST' not in r.get('名称','').upper()
                    and not r.get('名称','').startswith(('N','C','退'))]
            pool.sort(key=lambda r:r['成交额'],reverse=True)
            pools.append([{**r,'industry':members[r['代码']],'opportunity_type':board['type'],
                           'opportunity_code':board['code'],'selection_version':VERSION} for r in pool[:2]])
        from itertools import zip_longest
        return [r for turn in zip_longest(*pools) for r in turn if r is not None][:maximum]
    selected = []
    for board in industries[:5]:
        pool = [r for code, r in quotes.items() if members.get(code, {}).get('code') == board['code']
                and r['成交额'] >= 1e8 and 'ST' not in r.get('名称', '').upper()
                and not r.get('名称', '').startswith(('N', 'C', '退'))]
        pool.sort(key=lambda r:r['成交额'], reverse=True)
        selected.extend(pool[:2])
    selected.sort(key=lambda r:r['成交额'], reverse=True)
    return [{**r, 'industry': members[r['代码']]} for r in selected[:maximum]]


def collect(collector, target, industries, candidates, *, stage='all', initial=None):
    daily = collector.data/'daily'/target
    start = (date.fromisoformat(target)-timedelta(days=240)).strftime('%Y%m%d')
    REQUIRED_DAILY_COLUMNS.update({k:('date','close') for k in ('industry_history','candidate_history','macro_history')})
    REQUIRED_DAILY_COLUMNS.update({'margin':('date','balance_cny'), 'etf_shares':('date','shares'),
                                   'stock_info':('date',), 'candidate_notices':('date',)})
    result = initial if initial is not None else {'industries':{}, 'stocks':{}, 'funds':{}, 'international':{}, 'gaps':[]}
    jobs = [('industry_history', row['code'], {'symbol':row['code']}, 'industries') for row in industries] if stage != 'stocks' else []
    jobs += [('candidate_history', row['代码'], {'code':row['代码']}, 'stocks') for row in candidates]
    if stage != 'stocks':
        jobs += [('macro_history', '.INX', {'symbol':'.INX'}, 'international')]
    # Failed sources rotate behind unattempted jobs on continuation.
    jobs.sort(key=lambda j:collector.checkpoint['tasks'].get(f'history:{target}:{j[0]}:{j[1]}', {}).get('attempt_rounds', 0))
    for kind, code, params, group in jobs:
        path = daily/'history'/f'{kind}-{code}.json'
        final = (date.fromisoformat(target)-timedelta(days=1)).isoformat() if kind == 'macro_history' else target
        def validate(rows, final=final):
            lower = f'{start[:4]}-{start[4:6]}-{start[6:]}'
            return bars([r for r in rows if str(r.get('date',r.get('日期','')))[:10] >= lower], final, minimum=21)
        fetched = collector._fetch(f'history:{target}:{kind}:{code}', kind,
            {**params, 'start_date':start, 'end_date':final.replace('-',''),
             **({'target_date':target} if kind != 'macro_history' else {})}, path, validate)
        if fetched:
            result[group][code] = read_json(path)
        else:
            result['gaps'].append(f'{kind}:{code} 历史不足、过期或源不可用')
    for kind in (() if stage == 'stocks' else ('margin','etf_shares')):
        path = daily/'history'/f'{kind}.json'
        def validate(rows, kind=kind):
            field = 'balance_cny' if kind == 'margin' else 'shares'
            matched = [r for r in rows if str(r['date'])[:10] <= target]
            if not matched or any(not math.isfinite(float(r[field])) or float(r[field]) < 0 for r in matched):
                raise ValueError('资金数据为空或数值无效')
            for r in matched:
                r['date'] = date.fromisoformat(str(r['date'])[:10]).isoformat()
            return matched, max(r['date'] for r in matched)
        if collector._fetch(f'history:{target}:{kind}', kind,
                {'target_date':target,'start_date':start,'end_date':target.replace('-','')}, path, validate):
            result['funds'][kind] = read_json(path)
        else:
            result['gaps'].append(f'{kind} 无已核验目标日记录')
    for candidate in candidates:
        code = candidate['代码']
        detail = result['stocks'].setdefault(code, {'rows':[]})
        for kind in ('stock_info','candidate_notices'):
            path = daily/'history'/f'{kind}-{code}.json'
            def validate(rows, kind=kind):
                if not rows or not isinstance(rows[0].get('facts' if kind == 'stock_info' else 'items'), (dict, list)):
                    raise ValueError('风险信息缺失')
                return rows, str(rows[0]['date'])[:10]
            params = {'code':code, 'target_date':target}
            if kind == 'stock_info':
                # Current fundamentals must never masquerade as historical disclosures.
                params.pop('target_date')
                params['as_of'] = collector.clock().astimezone(CST).isoformat()
                saved = read_json(path,{})
                if saved.get('rows') and saved['rows'][0]['date'] == params['as_of'][:10]:
                    params = saved.get('params',params)
            if collector._fetch(f'history:{target}:{kind}:{code}', kind, params, path, validate,
                                reuse=True):
                detail[kind] = read_json(path)
            else:
                result['gaps'].append(f'{code} {kind} 未取得')
    write_json(daily/'history'/'summary.json', result)
    return result
