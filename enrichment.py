"""Dated, bounded enrichment. Search results are samples, never complete news coverage."""
import json
import math
import re
import html
import sys
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit, urljoin

import requests

from collector import CST, list_rows
from storage import read_json, write_json


def fetch_concept_catalog():
    response = requests.get('https://money.finance.sina.com.cn/q/view/newFLJK.php',
                            params={'param':'class'}, timeout=12)
    response.raise_for_status()
    text = response.text.strip()
    prefix = 'var S_Finance_bankuai_class = '
    if not text.startswith(prefix):
        raise ValueError('新浪概念目录响应格式变化')
    data = json.loads(text[len(prefix):].rstrip(';'))
    rows = []
    for node, value in data.items():
        fields = value.split(',')
        if not re.fullmatch(r'gn_[A-Za-z0-9_]+',node) or len(fields) != 13 or fields[0] != node or not fields[1]:
            raise ValueError('新浪概念目录字段不符')
        # Directory company counts are capped at 100; use the member count endpoint instead.
        rows.append({'板块代码':'sina-'+node,'板块名称':fields[1]})
    if not rows:
        raise ValueError('新浪概念目录为空')
    return rows


def fetch_concept_members(node):
    if not re.fullmatch(r'gn_[A-Za-z0-9_]+',node):
        raise ValueError('新浪概念代码不符')
    base = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.'
    session = requests.Session()
    def request(method, params):
        for host in (base,base.replace('vip.stock.finance','money.finance')):
            try:
                response = session.get(host+method,params=params,timeout=12)
                response.raise_for_status()
                response.json()
                return response
            except (requests.RequestException,ValueError) as error:
                if host != base:
                    raise
                print(f'概念成员首选接口失败，尝试既有备用源；{type(error).__name__}: {str(error)[-500:]}',file=sys.stderr)
    count_urls = []
    def count():
        response = request('getHQNodeStockCount',{'node':node})
        count_urls.append(response.url)
        value = response.json()
        if not re.fullmatch(r'[1-9][0-9]*',str(value)):
            raise ValueError('新浪成员总数为空或无效')
        return int(value)
    def pages(ascending):
        rows, evidence = [], []
        ended = False
        for page in range(1,101):
            params = {'node':node,'page':page,'num':80,'sort':'symbol',
                      'asc':ascending,'symbol':'','_s_r_a':'page'}
            response = request('getHQNodeData',params)
            batch = response.json()
            if not isinstance(batch,list) or len(batch) > 80:
                raise ValueError('新浪概念分页格式或页长无效')
            evidence.append({'url':response.url,'page':page,'size':len(batch)})
            if not batch:
                if not ended and rows:
                    ended = True  # An exact multiple of the page size.
                break
            if ended:
                raise ValueError('新浪概念短页之后仍有成员，分页不能确认为完整')
            for row in batch:
                code = str(row.get('code',''))
                if not re.fullmatch(r'[0-9]{6}',code) or not row.get('name'):
                    raise ValueError('新浪概念成员代码或名称无效')
                if row.get('symbol') not in ('sh'+code,'sz'+code,'bj'+code):
                    raise ValueError('新浪概念证券代码与市场前缀不符')
                rows.append({'code':code,'name':row['name'],'symbol':row['symbol']})
            ended = len(batch) < 80
        else:
            raise ValueError('新浪概念超过分页上限，未取得终止空页')
        codes = [r['code'] for r in rows]
        symbols = [r['symbol'] for r in rows]
        if not rows or len(set(codes)) != len(codes):
            raise ValueError('新浪概念成员为空或分页重复')
        if symbols != sorted(symbols,reverse=not ascending) or evidence[-1]['size'] != 0:
            raise ValueError('新浪概念分页排序或终止页不符')
        return rows, evidence
    try:
        total = count()
        rows, forward = pages(1)
        reverse_rows, backward = pages(0)
        if {r['code']:r for r in rows} != {r['code']:r for r in reverse_rows} or count() != total:
            raise ValueError('新浪概念正反分页不一致或采集期间数据变化')
        return [{'raw_rows':rows,'collection':{'source_count':total,'raw_count':len(rows),
            'count_url':base+'getHQNodeStockCount?node='+node,
            'count_urls':count_urls,
            'forward_pages':forward,'reverse_pages':backward,
            'complete':True,'count_mismatch':total != len(rows),
            'count_difference_reason':'源端成员明细与 count 字段不一致；正反分页同集且终止空页已确认'
                if total != len(rows) else ''}}]
    finally:
        session.close()


def fetch_news_feed(as_of):
    # Bounded pages broaden the sample; they do not guarantee complete news coverage.
    records=[]; seen=set(); page_error=''
    for page in range(1,4):
        try:
            response = requests.get('https://zhibo.sina.com.cn/api/zhibo/feed',
                params={'page':page, 'page_size':20, 'zhibo_id':152, 'tag_id':0,
                        'dire':'f', 'dpc':1, 'pagesize':20, 'type':1}, timeout=8)
            response.raise_for_status()
            batch=response.json()['result']['data']['feed']['list']
        except (requests.RequestException,ValueError,KeyError,TypeError) as exc:
            if not records:raise
            page_error=f'第{page}页未取得：{type(exc).__name__}'
            break
        fresh=[r for r in batch if (r['create_time'],r['rich_text']) not in seen]
        for r in fresh:seen.add((r['create_time'],r['rich_text']))
        records.extend(fresh)
        if len(batch)<20 or not fresh:break
    rows = [{'date': r['create_time'][:10], 'time': r['create_time'],
             'title': html.unescape(re.sub('<[^>]+>', '', r['rich_text'])).strip(),
             'source': '新浪财经7×24', 'code':'市场资讯', 'source_id':str(r.get('id','')),
             'url':r['url'] if isinstance(r.get('url'),str) and urlsplit(r['url']).scheme in ('http','https')
                 else 'https://finance.sina.com.cn/7x24'}
            for r in records]
    return [{'date': as_of[:10], 'as_of':as_of,
             'scope':'新浪财经最近最多3页资讯，独立于历史行情；不代表完整事件覆盖',
             'page_error':page_error, 'items':rows}]


def fetch_fund_catalog():
    try:
        response = requests.get('https://push2.eastmoney.com/api/qt/clist/get',
            params={'pn':1,'pz':1000,'po':1,'np':1,'fltt':2,'invt':2,
                    'fs':'m:90 s:4','fields':'f12,f14','fid':'f12'},
            headers={'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/'},timeout=10)
        response.raise_for_status()
        data = response.json()['data']
        rows = list(data['diff'].values()) if isinstance(data['diff'],dict) else data['diff']
        if not rows or len(rows) != data['total']:
            raise ValueError('供应商行业列表不完整')
        return [{'板块代码':r['f12'],'板块名称':r['f14']} for r in rows]
    except (requests.RequestException,ValueError,KeyError,TypeError) as error:
        print(f'行业目录首选接口失败，尝试既有备用源；{type(error).__name__}: {str(error)[-500:]}',file=sys.stderr)
    from bs4 import BeautifulSoup
    response = requests.get('https://data.eastmoney.com/bkzj/hy.html',timeout=12)
    response.raise_for_status()
    response.encoding = 'utf-8'
    soup = BeautifulSoup(response.text,'html.parser')
    selector = soup.select_one('.bkselectorpop')
    tabs = selector.select('.pop_slider li') if selector else []
    boxes = selector.select('.pop-cont') if selector else []
    if [t.get_text(strip=True) for t in tabs] != ['行业','概念','地域'] or len(boxes) != 3:
        raise ValueError('官网板块目录分类结构变化，拒绝混入概念或地域')
    rows = []
    for link in boxes[0].select('a[href]'):
        code = re.fullmatch(r'/bkzj/(BK[0-9]{4})\.html',link['href'])
        name = link.get_text(strip=True)
        if not code or not name:
            raise ValueError('官网行业链接不完整')
        rows.append({'板块代码':code[1],'板块名称':name})
    list_rows(rows,'板块代码','板块名称')
    return rows


def fetch_board_flow(params):
    response = requests.get('https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get',
        params={'secid':'90.'+params['code'],'lmt':180,'klt':101,
                'fields1':'f1,f2,f3,f7','fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65'},
        headers={'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/'},timeout=10)
    response.raise_for_status()
    data = response.json()['data']
    if data['code'] != params['code'] or data['market'] != 90 or data['name'] != params['symbol']:
        raise ValueError('资金响应行业代码、名称或市场不符')
    rows = data['klines']
    if not rows:
        raise ValueError('无带日期的板块资金')
    result = []
    for row in rows:
        fields = row.split(',')
        if len(fields) != 15:
            raise ValueError('资金字段数量变化，拒绝猜测口径')
        day = date.fromisoformat(fields[0]).isoformat()
        net = float(fields[1])
        if not math.isfinite(net):
            raise ValueError('资金净额无效')
        result.append({'日期':day,'主力净流入-净额':net,
                       'source':'东方财富带日期板块资金；订单分类估算，单位元'})
    if len({r['日期'] for r in result}) != len(result):
        raise ValueError('资金日期重复')
    return result


def fetch_board_flows(params):
    """The official chart proxy accepts a board filter, not a BK security code."""
    url = 'https://data.eastmoney.com/dataapi/bkzj/getbkzj'
    expected = {r['code']:r['name'] for r in params['boards']}
    def snapshot(key):
        response = requests.get(url,params={'code':'m:90+s:4','key':key},timeout=15)
        response.raise_for_status()
        payload = response.json()
        data = payload.get('data') or {}
        rows = data.get('diff')
        if payload.get('rc') != 0 or not isinstance(rows,list) or len(rows) != data.get('total'):
            raise ValueError('官网资金批量响应为空或范围不完整')
        result = {}
        for row in rows:
            code = row.get('f12')
            if code in result or row.get('f13') != 90 or expected.get(code) != row.get('f14') or key not in row:
                raise ValueError('官网资金批量响应代码、名称、市场或字段不符')
            result[code] = row[key]
        if set(result) != set(expected):
            raise ValueError('官网资金批量范围与目标行业目录不同')
        return result
    before = snapshot('f124')
    amounts = snapshot('f62')
    after = snapshot('f124')
    if before != after:
        raise ValueError('采集期间资金源时间变化，拒绝拼接不同批次')
    rows = []
    for code,stamp in after.items():
        if not isinstance(stamp,(int,float)) or not math.isfinite(stamp):
            raise ValueError('行业资金源时间无效')
        observed = datetime.fromtimestamp(stamp,CST)
        if observed.date().isoformat() != params['target_date'] or observed.hour < 15:
            raise ValueError(f'{code}资金源时间{observed.isoformat()}不是目标日收盘')
        amount = amounts[code]
        if not isinstance(amount,(int,float)) or not math.isfinite(amount):
            raise ValueError(f'{code}资金净额缺失或无效')
        rows.append({'code':code,'name':expected[code],'日期':params['target_date'],
                     '主力净流入-净额':amount,'源时间':observed.isoformat(),
                     'source':url+'；东方财富行业过滤m:90+s:4，f62原始元；f124前后时点一致'})
    return rows


def fetch_policy(params):
    from bs4 import BeautifulSoup
    url = 'https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html'
    response = requests.get(url,timeout=15)
    response.raise_for_status()
    response.encoding = response.apparent_encoding
    soup = BeautifulSoup(response.text,'html.parser')
    target = params['target_date']
    lower = (date.fromisoformat(target)-timedelta(days=14)).isoformat()
    items = []
    for link in soup.select('font.newslist_style a[href]'):
        row = link.find_parent('tr')
        stamp = row.select_one('span.hui12') if row else None
        if not stamp:
            continue
        published = date.fromisoformat(stamp.get_text(strip=True)).isoformat()
        href = urljoin(url,link['href'])
        if lower <= published <= target and urlsplit(href).hostname == 'www.pbc.gov.cn':
            items.append({'date':published,'time':published,'title':link.get('title') or link.get_text(strip=True),
                'source':'中国人民银行官网','code':'央行','url':href,
                'known_at':params['as_of'],'time_precision':'官网发布日期；未提供首次公开时分'})
    if not items:
        raise ValueError('官网首页无目标窗口政策样本，不能推断没有政策')
    for item in [r for r in items if any(w in r['title'] for w in ('政策','通知'))][:2]:
        try:
            article = requests.get(item['url'],timeout=8)
            article.raise_for_status()
            article.encoding = article.apparent_encoding
            body = BeautifulSoup(article.text,'html.parser').select_one('#zoom')
            if body:
                item['excerpt'] = body.get_text(' ',strip=True)[:1000]
                item['excerpt_scope'] = '官方正文前1000字；完整文件见来源'
        except requests.RequestException:
            item['excerpt_scope'] = '正文暂未取得，仅标题与官网发布日期'
    return [{'date':target,'items':items,'scope':'央行新闻首页目标日前14日样本，非完整政策库',
             'as_of':params['as_of']}]


def policy_validator(target):
    def validate(rows):
        if len(rows) != 1 or rows[0]['date'] != target or not rows[0]['items']:
            raise ValueError('政策样本日期不符或为空')
        for row in rows[0]['items']:
            if row['date'] > target or urlsplit(row['url']).hostname != 'www.pbc.gov.cn' or not row['title']:
                raise ValueError('政策来源或时点不符')
        return rows,target
    return validate


def news_feed_validator(as_of):
    def validate(rows):
        if len(rows) != 1 or rows[0]['as_of'] != as_of or not rows[0]['items']:
            raise ValueError('市场资讯为空或采集时点不符')
        for row in rows[0]['items']:
            published = datetime.fromisoformat(row['time']).replace(tzinfo=CST)
            if published > datetime.fromisoformat(as_of) or not row['title']:
                raise ValueError('市场资讯含未来时间或空标题')
        return rows, as_of[:10]
    return validate


def fetch_events(kind, params):
    target = params['target_date']
    start = params.get('start_date',target)
    if kind == 'news':
        query = {'uid': '', 'keyword': params['code'], 'type': ['cmsArticleWebOld'],
                 'client': 'web', 'clientType': 'web', 'clientVersion': 'curr',
                 'param': {'cmsArticleWebOld': {'searchScope': 'default', 'sort': 'default',
                           'pageIndex': 1, 'pageSize': 10, 'preTag': '', 'postTag': ''}}}
        response = requests.get('https://search-api-web.eastmoney.com/search/jsonp',
                                params={'cb': 'radar', 'param': json.dumps(query)}, timeout=20)
        response.raise_for_status()
        text = response.text.strip()
        if not text.startswith('radar('):
            raise ValueError('新闻响应为空或格式不符')
        data = json.loads(text[len('radar('):].rstrip(';').rstrip(')'))
        items = data['result']['cmsArticleWebOld']
        rows = [{'date': str(r['date'])[:10], 'time': r['date'], 'title': r['title'],
                 'source': r['mediaName'], 'code': params['code'],
                 'url': 'https://finance.eastmoney.com/a/'+str(r['code'])+'.html'} for r in items]
    else:
        candidate = kind == 'candidate_notices'
        response = requests.get('https://np-anotice-stock.eastmoney.com/api/security/ann',
            params={'sr': '-1', 'page_size': 100, 'page_index': 1, 'ann_type': 'A',
                    'client_source': 'web', 'f_node': '0', 's_node': '0',
                    'begin_time': start, 'end_time': target,
                    **({'stock_list':params['code']} if candidate else {})}, timeout=20)
        response.raise_for_status()
        items = response.json()['data']['list']
        rows = [{'date': str(r['notice_date'])[:10], 'time': r['notice_date'],
                 'title': r['title'], 'source': '东方财富公告索引',
                 'code': params['code'] if candidate else r['codes'][0]['stock_code'],
                 'url': 'https://data.eastmoney.com/notices/detail/'+r['codes'][0]['stock_code']+'/'+r['art_code']+'.html'} for r in items]
    return [{'date': target, 'scope': '近期搜索前10条中的目标日样本' if kind == 'news' else '目标日公告首页最多100条样本',
             'items': [r for r in rows if start <= r['date'] <= target],
             'coverage':'首页样本，不能证明公告完整或风险已排除'}]


def event_validator(target, *, start_date=None, as_of=None):
    def validate(rows):
        if len(rows) != 1 or rows[0]['date'] != target or not isinstance(rows[0]['items'], list):
            raise ValueError('事件批次日期或格式不符')
        for item in rows[0]['items']:
            date.fromisoformat(item['date'])
            if not (start_date or target) <= item['date'] <= target or not item['title'] or not item['source']:
                raise ValueError('事件日期或必需字段不符')
            if as_of:
                published=datetime.fromisoformat(item['time'])
                if published.tzinfo is None:published=published.replace(tzinfo=CST)
                if published>datetime.fromisoformat(as_of):raise ValueError('事件发布时间晚于研究截止时点')
            if urlsplit(item['url']).scheme not in ('http', 'https'):
                raise ValueError('事件链接格式不符')
        return rows, target
    return validate


def collect(collector, target, watchlist, *, market_events=False):
    daily = collector.data/'daily'/target/'enrichment'
    catalog = collector.data/'snapshots'/'fund-boards.json'
    def validate_catalog(rows):
        normalized = bool(rows) and 'code' in rows[0]
        return list_rows(rows, 'code' if normalized else '板块代码',
                         'name' if normalized else '板块名称'), ''
    listed = collector._fetch('snapshot:fund-boards', 'fund_board_list', {}, catalog, validate_catalog)
    boards = read_json(catalog)['rows'] if listed else []
    as_of = collector.clock().astimezone(CST).isoformat()
    feed_path = daily/'latest-news.json'
    feed_ok = collector._fetch(f'daily:{target}:news_feed', 'news_feed', {'as_of':as_of},
                              feed_path, news_feed_validator(as_of))
    def validate_flow(rows):
        matched = [r for r in rows if str(r['日期'])[:10] == target]
        if len(matched) != 1 or not math.isfinite(float(matched[0]['主力净流入-净额'])):
            raise ValueError('资金目标日缺失或数值无效')
        historical=[r for r in rows if str(r['日期'])[:10]<=target
                    and math.isfinite(float(r['主力净流入-净额']))]
        return matched, target, {'history_rows':historical}
    batch = {}
    needs_flow = any(not collector._already_succeeded(f'daily:{target}:board_flow:{b["code"]}',
        daily/f'flow-{b["code"]}.json') for b in boards)
    if boards and needs_flow:
        def validate_batch(rows):
            expected = {b['code']:b['name'] for b in boards}
            if len(rows) != len(expected) or {r.get('code') for r in rows} != set(expected):
                raise ValueError('行业资金批量缺项或重复')
            for row in rows:
                validate_flow([row])
                if row.get('name') != expected[row['code']]:
                    raise ValueError('行业资金批量名称不符')
                saved = read_json(daily/f'flow-{row["code"]}.json',{})
                old = [r for r in saved.get('rows',[]) if r.get('日期') == target]
                if old and float(old[0]['主力净流入-净额']) != float(row['主力净流入-净额']):
                    raise ValueError(f'{row["code"]}批量资金与已有带日期资金不一致')
            return rows,target
        batch_path = daily/'board-flows.json'
        if collector._fetch(f'daily:{target}:board_flows','board_flows',
                {'target_date':target,'boards':boards},batch_path,validate_batch):
            batch = {r['code']:r for r in read_json(batch_path)['rows']}
    events, flows, statuses, gaps = [], [], {}, []
    if not listed:
        task = collector.checkpoint['tasks'].get('snapshot:fund-boards',{})
        gaps.append({'kind':'行业资金目录','code':'','name':'覆盖范围未知',
                     'reason':task.get('error') or '目录未取得'})
    jobs = [('notices', '', '公告', {'code':''}),
            *[('news', r['code'], '新闻', {'code':r['code']}) for r in watchlist],
            *[('board_flow', r['code'], '资金', {'symbol':r['name'],'code':r['code']}) for r in boards]]
    if market_events:
        jobs += [('news',keyword,'市场事件',{'code':keyword}) for keyword in ('央行','国务院','中央汇金')]
        jobs += [('policy','pbc','官方政策',{'as_of':as_of})]
    jobs.sort(key=lambda job: collector.checkpoint['tasks'].get(
        f'daily:{target}:{job[0]}:{job[1]}', {}).get('attempt_rounds', 0))
    connection_failures=0
    for kind, code, label, params in jobs:
        path = daily/(f'flow-{code}.json' if kind == 'board_flow' else f'{kind}{code}.json')
        validator = validate_flow if kind == 'board_flow' else policy_validator(target) if kind == 'policy' else event_validator(target)
        key=f'daily:{target}:{kind}:{code}'
        if kind=='board_flow' and connection_failures>=3 and code not in batch:
            collector._save_task(key,'PENDING',error='行业资金源连续3次连接失败，暂停同源请求；下轮从未完成项续采')
            success=False
        else:
            success = collector._fetch(key, kind,
                {**params, 'target_date':target}, path, validator,
                prefetched_rows=[batch[code]] if kind == 'board_flow' and code in batch else None)
            if kind=='board_flow':
                error=collector.checkpoint['tasks'].get(key,{}).get('error','')
                if success:connection_failures=0
                elif any(w in error for w in ('ConnectionError','RemoteDisconnected','ConnectionResetError')):
                    connection_failures+=1
        statuses.setdefault(label, []).append(success)
        if kind == 'board_flow' and not success:
            task = collector.checkpoint['tasks'].get(f'daily:{target}:{kind}:{code}',{})
            gaps.append({'kind':'行业资金','code':code,'name':params['symbol'],
                         'reason':task.get('error') or '本轮预算已用完，等待续采'})
        if success:
            saved_rows = read_json(path)['rows']
            row = next(r for r in saved_rows if str(r['日期'])[:10] == target) if kind == 'board_flow' else saved_rows[0]
            if kind == 'board_flow':
                flows.append({'code':code,'name':params['symbol'], 'date':target,
                              'net_cny':float(row['主力净流入-净额']), 'source':row.get('source','东方财富行业资金')})
            else:
                events.extend(row['items'])
    flow_ok = statuses.pop('资金', [])
    result = {'events': events, 'flows': flows, 'flow_complete': bool(flow_ok) and all(flow_ok), 'gaps':gaps,
              'flow_expected': len(boards), 'status': {
                  label: ('SAMPLE：已完成目标日样本检索，不代表完整覆盖或一定有结果' if checks and all(checks)
                          else 'PARTIAL：部分源失败或任务待续采') for label, checks in statuses.items()}}
    result['status'].setdefault('新闻', '未设置自选股，未搜索新闻')
    result['status']['第三方板块资金'] = (f"{'READY' if result['flow_complete'] else 'PARTIAL'}：{len(flows)}/{len(boards)}供应商行业；单位元，不等同申万分类"
        if listed else 'DEGRADED：板块列表未取得，覆盖范围未知')
    result['latest_news'] = read_json(feed_path)['rows'][0] if feed_ok else None
    result['status']['最新市场资讯'] = 'SAMPLE：最新20条，独立采集时点，不用于历史行情归因' if feed_ok else 'PARTIAL：接口失败或待续采'
    write_json(daily/'summary.json', result)
    return result
