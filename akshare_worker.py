"""One bounded AKShare call per subprocess; the parent owns checkpoint writes."""

import json
import sys
import re
from datetime import datetime
from pathlib import Path


def log_fallback(kind, error):
    print(f'{kind}：首选数据源失败，尝试既有备用源；{type(error).__name__}: {str(error)[-500:]}',file=sys.stderr)


def observation_rows(rows,params,source):
    import math
    result=[]
    for row in rows:
        day=str(row.get('date',row.get('日期','')))[:10]
        if not day or day>params['cutoff']:continue
        value=float(row.get('close',row.get('最新价')))
        if not math.isfinite(value) or value<=0:raise ValueError('国际日线价格无效')
        result.append({'date':day,'close':value,'source':source})
    return sorted(result,key=lambda r:r['date'])[-45:]


def fetch(kind: str, params: dict):
    if kind=='international_observation':
        import akshare as ak
        symbol=params['symbol'];category=params['kind']
        if category=='us':frame=ak.index_us_stock_sina(symbol=symbol);source='新浪美股指数 '+symbol
        elif category=='hk':frame=ak.stock_hk_index_daily_sina(symbol=symbol);source='新浪港股指数 '+symbol
        elif category=='fx':
            import requests
            from akshare.forex.forex_em import symbol_market_map
            source='https://push2his.eastmoney.com/api/qt/stock/kline/get'
            market=symbol_market_map[symbol]
            response=requests.get(source,params={'secid':f'{market}.{symbol}','klt':101,'fqt':1,
                'lmt':90,'end':params['cutoff'].replace('-',''),'fields1':'f1,f2,f3,f4,f5,f6',
                'fields2':'f51,f52,f53,f54,f55','iscca':1},
                headers={'User-Agent':'Mozilla/5.0','Referer':'https://quote.eastmoney.com/'},timeout=(5,15))
            response.raise_for_status();data=response.json()['data']
            if data.get('code')!=symbol or data.get('market')!=market:raise ValueError('外汇币对或市场不符')
            rows=[]
            for raw in data.get('klines',[]):
                fields=raw.split(',')
                if len(fields)!=5:raise ValueError('外汇日线字段不符')
                rows.append({'date':fields[0],'close':float(fields[2])})
            return observation_rows(rows,params,source+'；美元/离岸人民币，日线收盘')
        elif category=='future':frame=ak.futures_foreign_hist(symbol=symbol);source='新浪外盘期货连续日线 '+symbol+'；非现货价格'
        elif category=='yield':
            import requests
            import csv
            import io
            import xml.etree.ElementTree as ET
            source='https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml'
            try:
                response=requests.get(source,params={'data':'daily_treasury_yield_curve',
                    'field_tdr_date_value_month':params['cutoff'][:7].replace('-','')},timeout=(4,8))
                response.raise_for_status()
            except requests.RequestException as error:
                log_fallback(kind,error)
                # FRED distributes the Federal Reserve's dated 10-year
                # constant-maturity series, in percent. Never substitute a quote.
                source='https://fred.stlouisfed.org/graph/fredgraph.csv'
                response=requests.get(source,params={'id':'DGS10','cosd':params['cutoff'][:7]+'-01',
                    'coed':params['cutoff']},timeout=(4,8))
                response.raise_for_status()
                reader=csv.DictReader(io.StringIO(response.text))
                if reader.fieldnames!=['observation_date','DGS10']:
                    raise ValueError('FRED序列或日期列不符')
                rows=[{'date':r['observation_date'],'close':float(r['DGS10'])}
                      for r in reader if r['DGS10'] not in ('','.','NA')]
                result=observation_rows(rows,params,source+'?id=DGS10；美联储H.15，10年恒定期限收益率，单位%；财政部源不可用时备用')
                if not result:raise ValueError('FRED没有截止日前的有效日线')
                return result
            response.raise_for_status();rows=[]
            for node in ET.fromstring(response.content).iter():
                if node.tag.endswith('properties'):
                    fields={n.tag.rsplit('}',1)[-1]:n.text for n in node}
                    if fields.get('NEW_DATE') and fields.get('BC_10YEAR'):
                        rows.append({'date':fields['NEW_DATE'][:10],'close':float(fields['BC_10YEAR'])})
            return observation_rows(rows,params,source+'；10年期限收益率，单位%')
        else:raise ValueError('未知国际资产类型')
        return observation_rows(json.loads(frame.to_json(orient='records',date_format='iso')),params,source)
    if kind=='margin_sz':
        import requests
        source='https://www.szse.cn/api/report/ShowReport/data'
        response=requests.get(source,params={'SHOWTYPE':'JSON','CATALOGID':'1837_xxpl',
            'txtDate':params['target_date'],'tab1PAGENO':'1'},timeout=(5,15),
            headers={'Referer':'https://www.szse.cn/disclosure/margin/object/index.html','User-Agent':'Mozilla/5.0'})
        response.raise_for_status();report=response.json()[0]
        meta=report['metadata']
        dates=[r['defaultValue'] for r in meta['conditions'] if r['name']=='txtDate']
        if report.get('error') or meta.get('catalogid')!='1837_xxpl' or dates!=[params['target_date']]:
            raise ValueError('深交所融资汇总来源或日期不符')
        rows=report['data']
        if not rows:raise ValueError('SZSE_FETCHED_EMPTY: 深交所官网成功响应，但所选日期无记录；不能推断停牌、休市或余额为0')
        if len(rows)!=1:raise ValueError('深交所融资汇总记录数量异常')
        return [{'date':dates[0],'balance_cny':float(str(rows[0]['jrrzye']).replace(',','')),
            'scope':'深交所融资余额；元','source':source}]
    if kind == 'board_flows':
        from enrichment import fetch_board_flows
        return fetch_board_flows(params)
    if kind == 'board_flow':
        from enrichment import fetch_board_flow
        return fetch_board_flow(params)
    if kind == 'fund_board_list':
        from enrichment import fetch_fund_catalog
        try:
            return fetch_fund_catalog()
        except Exception as error:
            log_fallback(kind,error)
    if kind == 'concept_members' and params['symbol'].startswith('sina-'):
        from enrichment import fetch_concept_members
        return fetch_concept_members(params['symbol'][5:])
    if kind == 'concept_list':
        from enrichment import fetch_concept_catalog
        try:
            return fetch_concept_catalog()
        except Exception as error:
            log_fallback(kind,error)
    import akshare as ak

    if kind == "universe":
        frame = ak.stock_info_a_code_name().rename(columns={"code": "代码", "name": "名称"})
    elif kind == "market_batch":
        import requests
        symbols = [("sh" if c.startswith('6') else "sz" if c.startswith(('0','3')) else "bj") + c
                   for c in params['codes']]
        response = requests.get('https://qt.gtimg.cn/q=' + ','.join(symbols), timeout=20)
        response.raise_for_status()
        records = []
        for symbol, payload in re.findall(r'v_(\w+)="([^"]*)"', response.text):
            fields = payload.split('~')
            if len(fields) < 38:
                continue
            stamp = datetime.strptime(fields[30], '%Y%m%d%H%M%S')
            if stamp.hour < 15:
                raise ValueError(f'{symbol}: 源时间 {fields[30]} 为盘前/盘中，不能冒充目标日 {params["target_date"]} 收盘行情')
            # Field 35 contains price/volume/turnover in CNY (no rounded 万元 conversion).
            turnover = fields[35].split('/')
            if len(turnover) != 3:
                raise ValueError('腾讯成交额字段格式变化，拒绝猜测单位')
            records.append({'代码': fields[2], '名称': fields[1], '日期': stamp.date().isoformat(),
                '源时间': fields[30], 'source': 'Tencent qt.gtimg.cn batch',
                '收盘': float(fields[3]), '涨跌幅': float(fields[32]), '成交额': float(turnover[2])})
        if {r['代码'] for r in records} != set(params['codes']):
            raise ValueError('批量行情返回股票数量/代码不完整')
        return records
    elif kind == "market_history":
        target = params['target_date'].replace('-', '')
        frame = ak.stock_zh_a_hist(symbol=params['code'], period='daily',
                                 start_date=target, end_date=target, adjust='', timeout=30)
        if frame.empty:
            raise ValueError('目标日无历史行情；不能推测为停牌或填零')
        frame = frame.rename(columns={'股票代码': '代码'})
        frame['名称'] = params['name']
        frame['source'] = 'AKShare stock_zh_a_hist / Eastmoney unadjusted, amount CNY'
    elif kind == "industry_list":
        frame = ak.index_realtime_sw(symbol="一级行业").rename(
            columns={"指数代码": "板块代码", "指数名称": "板块名称"})
    elif kind == "concept_list":
        frame = ak.stock_board_concept_name_em()
    elif kind == "industry_members":
        frame = ak.index_component_sw(symbol=params["symbol"]).rename(
            columns={"证券代码": "代码", "证券名称": "名称"})
    elif kind == "concept_members":
        frame = ak.stock_board_concept_cons_em(symbol=params["symbol"])
    elif kind == "index":
        frame = ak.stock_zh_index_daily_tx(symbol=params.get("symbol", "sh000001"), start_date=params["start_date"],
                                            end_date=params["end_date"])
        frame['source'] = 'AKShare stock_zh_index_daily_tx'
    elif kind == "stock_daily":
        frame = ak.stock_zh_a_hist(symbol=params["code"], period="daily",
                                   start_date=params["start_date"], end_date=params["end_date"],
                                   adjust="", timeout=30)
    elif kind == "stock_flow":
        frame = ak.stock_individual_fund_flow(stock=params["code"], market=params["market"])
    elif kind == 'fund_board_list':
        frame = ak.stock_board_industry_name_em()
    elif kind in ('news', 'notices'):
        from enrichment import fetch_events
        return fetch_events(kind, params)
    elif kind == 'news_feed':
        from enrichment import fetch_news_feed
        return fetch_news_feed(params['as_of'])
    elif kind == 'policy':
        from enrichment import fetch_policy
        return fetch_policy(params)
    elif kind == 'industry_history':
        frame = ak.index_hist_sw(symbol=params['symbol'], period='day')
        frame['source'] = '申万宏源指数历史 / AKShare；成交量额仅同源比率'
    elif kind == 'candidate_history':
        try:
            frame = ak.stock_zh_a_hist(symbol=params['code'], period='daily',
                start_date=params['start_date'], end_date=params['end_date'], adjust='', timeout=10)
            if frame.empty:
                raise ValueError('目标窗口为空')
            frame['source'] = '东方财富不复权日线 / AKShare；成交额元'
        except Exception as error:
            log_fallback(kind,error)
            from collector import stock_market
            frame = ak.stock_zh_a_hist_tx(symbol=stock_market(params['code'])+params['code'],
                start_date=params['start_date'], end_date=params['end_date'], adjust='', timeout=10)
            frame['source'] = '腾讯不复权日线 / AKShare；无成交额时不计算金额比率'
    elif kind == 'macro_history':
        frame = ak.index_us_stock_sina(symbol=params['symbol'])
        frame['source'] = '新浪美股标普500历史；仅使用目标日前已结束日期'
    elif kind == 'margin':
        frame = ak.stock_margin_sse(start_date=params['start_date'], end_date=params['end_date'])
        records = []
        for row in frame.to_dict('records'):
            day = str(row['信用交易日期']).replace('-', '')[:8]
            records.append({'date':datetime.strptime(day, '%Y%m%d').date().isoformat(),
                'balance_cny':float(row['融资余额']), 'scope':'仅上交所融资余额',
                'source':'https://www.sse.com.cn/market/othersdata/margin/sum/'})
        return records
    elif kind == 'etf_shares':
        frame = ak.fund_etf_scale_sse(date=params['end_date'])
        return [{'date':str(row['统计日期'])[:10], 'code':str(row['基金代码']),
            'name':row['基金简称'], 'shares':float(row['基金份额']),
            'scope':'上交所ETF份额；含不同资产ETF，不能汇总为A股现金',
            'source':'https://www.sse.com.cn/assortment/fund/etf/list/scale/'} for row in frame.to_dict('records')]
    elif kind == 'stock_info':
        try:
            frame = ak.stock_individual_info_em(symbol=params['code'], timeout=10)
            facts = dict(zip(frame['item'],frame['value']))
            source = '东方财富个股资料 / AKShare；采集时点资料'
        except Exception as error:
            log_fallback(kind,error)
            frame = ak.stock_profile_cninfo(symbol=params['code'])
            facts = frame.to_dict('records')[0]
            source = '巨潮资讯公司概况 / AKShare；采集时点资料'
        return [{'date':params['as_of'][:10], 'as_of':params['as_of'],
            'facts':facts, 'source':source}]
    elif kind == 'candidate_notices':
        from enrichment import fetch_events
        return fetch_events('candidate_notices', params)
    else:
        raise ValueError(f"未知数据模块：{kind}")
    if "代码" in frame.columns:
        frame["代码"] = frame["代码"].astype(str).str.zfill(6)
    return json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))


if __name__ == "__main__":
    sys.stderr.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        rows = fetch(sys.argv[1], json.loads(sys.argv[2]))
        Path(sys.argv[3]).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
