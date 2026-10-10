"""Core market gates and transparent, deterministic V0.1 analytics."""
import html
import hashlib
from datetime import date, timedelta
from pathlib import Path

from collector import CST, Collector, clean_code, membership_signature
from reporting import page_html, table
from storage import read_json, write_json, write_text


class Radar(Collector):
    def run(self, target_date, watchlist, *, refresh=False, max_tasks=50,
            retries=1, interval=1, initial_messages=None, ai_settings=None, enrichment=False,
            product=False):
        if max_tasks < 1 or retries < 0 or interval < 0:
            raise ValueError("任务预算、重试次数或间隔无效")
        self.limit, self.retries, self.interval = max_tasks, retries, interval
        self.calls = self.attempts = 0
        self.messages = list(initial_messages or [])
        self.ai_settings = ai_settings
        self.product = product
        print('RESEARCH_STAGE:正在检查行情与已有研究', flush=True)
        self.data.mkdir(parents=True, exist_ok=True)
        self.reports.mkdir(parents=True, exist_ok=True)
        now = self.clock().astimezone(CST)
        mode = 'PRE_MARKET' if now.hour < 16 else 'POST_MARKET'
        # Before 16:00 exclude today's unfinished session. Weekends/holidays are
        # determined by dated index rows, never by weekday arithmetic.
        cutoff = now.date() if now.hour >= 16 else now.date() - timedelta(days=1)
        if target_date:
            requested = date.fromisoformat(target_date)
            if requested > cutoff:
                raise ValueError("请求日期尚未收盘或位于未来")
            cutoff = requested
        probe = self.data / "trading-days" / f"{cutoff}.json"
        params = {"symbol": "sh000001", "start_date": (cutoff-timedelta(days=45)).strftime("%Y%m%d"),
                  "end_date": cutoff.strftime("%Y%m%d")}
        ok = self._fetch(f"calendar:index:{cutoff}", "index", params, probe, self._daily_validator("index"))
        dates = [str(r['date'])[:10] for r in read_json(probe, {}).get('rows', [])
                 if str(r['date'])[:10] <= cutoff.isoformat()] if ok else []
        confirmed = bool(dates) and (not target_date or target_date in dates)
        target = target_date or (max(dates) if dates else "UNCONFIRMED")
        historical = confirmed and target < now.date().isoformat()
        universe = self._universe(refresh)
        daily = self.data / "daily" / target
        index_ok = []
        market_ok = False
        market_rows = []
        if confirmed:
            for symbol in ("sh000001", "sz399001", "sh000300"):
                if symbol == 'sh000001':
                    saved = read_json(probe)
                    saved['params'] = {**params, 'target_date':target}
                    write_json(daily/f'index-{symbol}.json',saved)
                    self._save_task(f'daily:{target}:index:{symbol}','SUCCESS',daily/f'index-{symbol}.json',observed_date=target)
                    index_ok.append(True)
                    continue
                index_ok.append(self._fetch(f"daily:{target}:index:{symbol}", "index",
                    {"symbol": symbol, "start_date": (date.fromisoformat(target)-timedelta(days=45)).strftime("%Y%m%d"),
                     "end_date": target.replace('-', ''), "target_date": target},
                    daily / f"index-{symbol}.json", self._daily_validator("index")))
            market_ok, market_rows = self._collect_market(target, universe, daily, now, historical)
        expected = {r['code'] for r in universe['rows']} if universe else set()
        actual = {r['代码'] for r in market_rows}
        missing = sorted(expected - actual)
        coverage_ok = bool(expected) and not missing
        if missing:
            self.messages.append(f"全市场行情缺少范围内 {len(missing)} 只股票；前20只：{', '.join(missing[:20])}")
            self.messages.append('16:00前按盘前模式处理；优先使用目标日快照或仍保留该日收盘的批量报价，核验失败后补历史日线。未尝试的缺口优先，失败任务轮流重试，停牌或源失败不填零。')
        industry = self._board_snapshot('industry', refresh)
        industry_ok = bool(industry)
        if industry:
            seen = set()
            base = self.data/'snapshots'/'industry-sw'/industry['generation']/'members'
            for board in industry['boards']:
                codes = {r['code'] for r in read_json(base/f"{board['code']}.json")['rows']}
                if seen & codes:
                    industry_ok = False
                    self.messages.append('行业成员存在重复归属；行业统计暂停，需刷新快照')
                seen.update(codes)
        flow_ok, watch_ok = [], []
        available_quotes = {r['代码']: r for r in market_rows}
        for item in watchlist:
            code = clean_code(item['code'])
            if not confirmed:
                watch_ok.append(False)
                flow_ok.append(False)
                continue
            if code in available_quotes:
                path = daily/f'stock-{code}.json'
                write_json(path, {'rows':[available_quotes[code]], 'source':'validated market batch',
                                  'observed_date':target,'fetched_at':now.isoformat()})
                self._save_task(f'daily:{target}:stock:{code}','SUCCESS',path,observed_date=target)
                watch_ok.append(True)
            else:
                watch_ok.append(self._fetch(f"daily:{target}:stock:{code}", 'stock_daily',
                    {'code': code, 'start_date': target.replace('-', ''), 'end_date': target.replace('-', ''), 'target_date': target},
                    daily / f'stock-{code}.json', self._daily_validator('stock_daily')))
            flow_ok.append(self._fetch(f"daily:{target}:flow:{code}", 'stock_flow',
                {'code': code, 'market': item.get('market', 'sz'), 'target_date': target},
                daily / f'flow-{code}.json', self._daily_validator('stock_flow')))
        concept = self._board_snapshot('concept', refresh)
        concept_status = 'PARTIAL：概念或成员缺失'
        if concept:
            concept_status = f"READY：{concept['board_count']}个概念；{concept.get('source','已保存分类')}；采集时点成员"
        else:
            active = read_json(self.data/'snapshots'/'concept'/'active.json',{})
            boards = active.get('boards',[])
            if boards:
                generation = active['generation']
                completed = sum(self._already_succeeded(f"snapshot:concept:member:{generation}:{b['code']}",
                    self.data/'snapshots'/'concept'/generation/'members'/f"{b['code']}.json") for b in boards)
                source = '新浪' if all(b['code'].startswith('sina-') for b in boards) else '供应商'
                concept_status = f'PARTIAL：{source}概念成员核验{completed}/{len(boards)}；仅采集时点分类，未发布完整快照'
        required = [
            ('指数记录确认的有效交易日', confirmed, '无法确认真实交易日'),
            ('沪深京有效股票范围', bool(universe), '交易所股票范围快照缺失'),
            ('全 A 股目标日行情（范围快照内）', market_ok and coverage_ok,
             f'行情失败/日期不符，或缺少 {len(missing)} 只范围内股票' if expected else '股票范围未知，无法核验行情覆盖'),
            ('核心指数：上证、深证、沪深300', bool(index_ok) and all(index_ok), '指数接口或日期校验失败'),
            ('申万一级分类及完整成员快照', industry_ok, '行业列表/成员未全部成功，或存在重复归属')]
        flags = {'MARKET_READY': all(x[1] for x in required[:4]), 'INDUSTRY_READY': industry_ok,
                 'THEME_READY': bool(concept), 'FUND_FLOW_READY': False,
                 'WATCHLIST_READY': bool(watchlist) and all(watch_ok)}
        # Board flows are deliberately not represented by watchlist flows.
        ready = flags['MARKET_READY'] and flags['INDUSTRY_READY']
        result = {'target_date': target, 'generated_at': now.isoformat(), 'mode': mode,
                  'status': 'READY' if ready else 'INCOMPLETE',
                  'requests_this_run': self.calls, 'attempts_this_run': self.attempts, 'task_budget': self.limit,
                  'readiness': flags, 'watchlist_flow_ready': bool(flow_ok) and all(flow_ok),
                  'required': [{'name': n, 'ok': bool(o), 'reason': '' if o else e} for n,o,e in required],
                  'snapshots': {'股票范围': universe.get('fetched_at', '') if universe else '',
                                '行业成员': industry.get('completed_at', '') if industry else '',
                                '概念成员': concept.get('completed_at', '') if concept else ''},
                  'missing_market_codes': missing, 'messages': self.messages, 'watchlist': watchlist,
                  'coverage': {'universe':len(expected),'valid_quotes':len(actual & expected),
                               'missing':len(missing)},
                  'optional': {'第三方板块资金': 'DEGRADED：尚未接入可核验日期的板块资金接口',
                               '概念': concept_status,
                               '个股资金': 'READY' if flow_ok and all(flow_ok) else 'PARTIAL：缺失，不填零',
                               '新闻': 'DEGRADED：未接入', '公告': 'DEGRADED：未接入'}}
        if enrichment and confirmed:
            from enrichment import collect
            result['enrichment'] = collect(self, target, watchlist,market_events=self.product)
            result['optional'].update(result['enrichment']['status'])
            result['readiness']['FUND_FLOW_READY'] = result['enrichment']['flow_complete']
            result['requests_this_run'], result['attempts_this_run'] = self.calls, self.attempts
        if product:
            result['required'] += [
                {'name':'完整供应商行业目标日资金','ok':flags['FUND_FLOW_READY'],
                 'reason':'' if flags['FUND_FLOW_READY'] else result['optional']['第三方板块资金']},
                {'name':'无冲突的完整概念成员快照','ok':flags['THEME_READY'],
                 'reason':'' if flags['THEME_READY'] else concept_status}]
            ready = all(row['ok'] for row in result['required'])
            result['status'] = 'READY' if ready else 'INCOMPLETE'
        result['data_gaps'] = list(result.get('enrichment',{}).get('gaps',[]))
        if not concept:
            active = read_json(self.data/'snapshots'/'concept'/'active.json',{})
            for board in active.get('boards',[]):
                task = self.checkpoint['tasks'].get(f"snapshot:concept:member:{active['generation']}:{board['code']}",{})
                if task.get('status') != 'SUCCESS':
                    result['data_gaps'].append({'kind':'概念成员',**board,'reason':task.get('error') or '等待续采'})
        if ready:
            self.write_market_report(result, daily, industry, market_rows, expected)
        else:
            result['ai'] = {'status':'WAITING_FOR_DATA', 'message':'必需数据未齐备，不生成新的正式报告；保留上一次完整报告'}
            result['generated_at'] = self.clock().astimezone(CST).isoformat()
        write_json(self.checkpoint_path, self.checkpoint)
        write_json(self.data / 'last_run.json', result)
        self._write_status_page(result)
        return result

    def write_market_report(self, result, daily, industry, rows, expected):
        if self.product and not all(result['readiness'].get(k) for k in
                ('MARKET_READY','INDUSTRY_READY','THEME_READY','FUND_FLOW_READY')):
            raise ValueError('必需数据缺失，禁止正式发布')
        quotes = {r['代码']: r for r in rows if r['代码'] in expected}
        total = sum(r['成交额'] for r in quotes.values())
        mean = sum(r['涨跌幅'] for r in quotes.values()) / len(quotes)
        breadth = sum(r['涨跌幅'] > 0 for r in quotes.values()) / len(quotes) * 100
        industries, mapped = [], set()
        base = self.data / 'snapshots' / 'industry-sw' / industry['generation'] / 'members'
        for board in industry['boards']:
            codes = {r['code'] for r in read_json(base / f"{board['code']}.json")['rows']}
            matched = codes & quotes.keys()
            if mapped & matched:
                raise ValueError('申万一级行业成员重复，不能重复计入行业统计')
            mapped.update(matched)
            members = [quotes[c] for c in matched]
            if not members:
                continue
            amount = sum(r['成交额'] for r in members)
            change = sum(r['涨跌幅'] for r in members) / len(members)
            industries.append({'code': board['code'], 'name': board['name'], 'count': len(members),
                'member_signature':hashlib.sha256(','.join(sorted(codes)).encode()).hexdigest(),
                'amount': amount, 'share': amount/total*100 if total else None,
                'breadth': sum(r['涨跌幅'] > 0 for r in members)/len(members)*100,
                'relative': change-mean, 'strong': sum(r['涨跌幅'] >= 5 for r in members)})
        industries.sort(key=lambda r: r['relative'], reverse=True)
        previous = None
        for path in sorted((self.data / 'analytics').glob('*.json'), reverse=True):
            if path.stem < result['target_date']:
                previous = read_json(path)
                break
        from history import comparable
        same_classification = previous and comparable(self.data,previous,industry)
        prior = {}
        if previous:
            old_base = self.data/'snapshots'/'industry-sw'/previous['industry_generation']/'members'
            for old in previous['industries']:
                old_members = read_json(old_base/f"{old['code']}.json")
                if old_members:
                    signature = hashlib.sha256(','.join(sorted(r['code'] for r in old_members['rows'])).encode()).hexdigest()
                    current = next((r for r in industries if r['code'] == old['code']),None)
                    if current and signature == current['member_signature']:
                        prior[old['code']] = old
        industry_rows = []
        for rank, row in enumerate(industries, 1):
            old = prior.get(row['code'])
            activity = (row['amount']/old['amount']-1)*100 if old and old['amount'] else None
            rotation = old['rank']-rank if old and same_classification else None
            row['rank'] = rank
            row['amount_change_percent'] = activity
            row['rank_improvement'] = rotation
            row['absolute_change_percent'] = row['relative'] + mean
            row['comparison_date'] = previous['target_date'] if old else None
            row['comparison_status'] = ('成员可比，整体分类变化时暂停排名比较' if old and not same_classification else
                                        '成员可比' if old else '无可比成员存档或成员发生变化')
            industry_rows.append([rank, row['name'], row['count'], row['amount']/1e8, row['share'],
                                  row['breadth'], row['relative'], row['strong'], activity, rotation])
        candidates = sorted((r for r in quotes.values() if r['涨跌幅'] >= 3 and r['成交额'] >= 1e8
                             and 'ST' not in r.get('名称', '').upper()), key=lambda r:r['成交额'], reverse=True)[:50]
        if self.product:
            from history import shortlist
            membership = {}
            for board in industry['boards']:
                membership.update({r['code']:{'code':board['code'],'name':board['name']}
                    for r in read_json(base/f"{board['code']}.json")['rows']})
            from history import bars, collect
            from decision import build
            from opportunities import build as diagnose, concept_facts
            index_history = {symbol:bars(read_json(daily/f'index-{symbol}.json')['rows'],result['target_date'])[0]
                             for symbol in ('sh000001','sz399001','sh000300')}
            history = collect(self,result['target_date'],industries,[],stage='market')
            diagnostic_input = {'target_date':result['target_date'],'industries':industries,
                'market':{'breadth_percent':breadth,'equal_weight_change_percent':mean},'enrichment':result.get('enrichment',{})}
            preliminary = build(diagnostic_input,history,[],index_history,self.clock().astimezone(CST))
            opportunities = diagnose(diagnostic_input,preliminary,history,index_history)
            concept_manifest = read_json(self.data/'snapshots/concept/latest.json',{})
            opportunities['concepts'] = concept_facts(self.data,concept_manifest.get('generation',''),quotes,result['target_date'])
            candidates = shortlist(quotes,industries,membership,opportunities=opportunities)
        regime = '偏强' if breadth >= 60 and mean > 0 else ('偏弱' if breadth <= 40 and mean < 0 else '分化')
        body = f"<section class='card'><h2>Market Regime / 大盘环境：{regime}</h2><p>覆盖 {len(quotes)} 只；上涨广度 {breadth:.2f}%；等权涨跌 {mean:.2f}%；成交额 {total/1e8:.2f} 亿元。</p><p>规则：上涨广度≥60%且等权涨幅为正为偏强；≤40%且为负为偏弱；其余分化。这是规则统计，不是 AI 预测。</p></section>"
        body += '<p>覆盖以股票范围快照为准，采集时间：' + html.escape(result['snapshots']['股票范围']) + '。工作台可更新范围和分类快照；历史样本未按当日上市退市状态重建。</p>'
        indices = []
        for symbol,name in [('sh000001','上证指数'),('sz399001','深证成指'),('sh000300','沪深300')]:
            values = read_json(daily/f'index-{symbol}.json')['rows']
            row = next(r for r in values if str(r['date'])[:10] == result['target_date'])
            indices.append([name,row['date'],row['close']])
        body += '<section class="card"><h2>核心指数</h2>' + table(['指数','交易日','收盘'],indices) + '</section>'
        body += '<section class="card"><h2>行业强弱、轮动与成交关注</h2>' + table(
            ['排名','行业','覆盖股票','成交额/亿','市场占比%','上涨广度%','相对强弱/百分点','涨幅≥5%','成交额变化%','排名提升'], industry_rows)
        body += f"<p>行业快照：{html.escape(industry['completed_at'])}。未分类 {len(expected-mapped)} 只仍参与市场统计；未分类不假装归属行业。对比日：{previous['target_date'] if prior else '缺失（需要另一日同版本快照）'}。成交额反映活跃度，不等于净流入。</p></section>"
        candidate_rule = ('机会类型轮流覆盖所属行业，每行业按成交额取最多2只，去重后最多6只；成交额≥1亿元、非ST及新股名称；不按涨幅榜补齐' if self.product else
                          '涨幅≥3%、成交额≥1亿元、名称不含ST，按成交额排序取前50')
        body += '<section class="card"><h2>全市场候选池</h2><p>'+candidate_rule+'；仅供进一步研究。</p>' + table(
            ['代码','名称','收盘','涨跌幅%','成交额/亿'], [[r['代码'],r.get('名称',''),r['收盘'],r['涨跌幅'],r['成交额']/1e8] for r in candidates]) + '</section>'
        watch = self._watchlist_rows(result, daily)
        body += '<section class="card"><h2>Watchlist（独立模块）</h2>' + table(['代码','名称','收盘','主力净流入/元'],watch) + '</section>'
        body += '<section class="card"><h2>增强模块状态</h2>' + table(['模块','状态'],result['optional'].items()) + '</section>'
        extra = result.get('enrichment', {})
        if extra:
            body += '<section class="card"><h2>目标日事件样本</h2><p>仅目标日新闻搜索和公告首页样本；未检索到不代表没有事件。公告日期不证明当时已公开，不用于盘中回测。</p>'
            for event in extra['events']:
                body += '<p>' + html.escape(event['time']+' · '+event['source']+' · '+event['code']) + ' <a target="_blank" rel="noopener noreferrer" href="' + html.escape(event['url'], quote=True) + '">' + html.escape(event['title']) + '</a></p>'
            body += '</section><section class="card"><h2>供应商行业资金</h2><p>东方财富分类，不与申万行业直接合并；净额为供应商估算。</p>' + table(['行业','日期','主力净流入/元'], [[r['name'],r['date'],r['net_cny']] for r in extra['flows']]) + '</section>'
            feed = extra.get('latest_news')
            if feed:
                body += '<section class="card"><h2>最新市场资讯 · 独立时点</h2><p>采集时点（北京时间）：' + html.escape(feed['as_of']) + '。最新20条有限样本，可能晚于上方行情日期；不参与历史行情归因或回测。</p>' + table(['发布时间','来源','资讯'], [[r['time'],r['source'],r['title']] for r in feed['items']]) + '<p><a href="https://finance.sina.com.cn/7x24" target="_blank" rel="noopener noreferrer">查看新浪财经来源</a></p></section>'
        body += '<section class="card"><h2>就绪状态及快照日期</h2>' + table(['模块','READY'],result['readiness'].items()) + table(['快照','采集完成时间'],result['snapshots'].items()) + '</section>'
        write_json(self.data/'analytics'/f"{result['target_date']}.json", {'target_date':result['target_date'],
                   'industry_generation':industry['generation'], 'membership_signature':membership_signature(self.data,industry),
                   'industries':industries})
        ai_input = {'target_date':result['target_date'], 'mode':result['mode'],
            'coverage':result['coverage'], 'snapshots':result['snapshots'], 'optional':result['optional'],
            'comparison_date': previous['target_date'] if prior else None,
            'market':{'regime':regime, 'breadth_percent':breadth, 'equal_weight_change_percent':mean,
                      'turnover_cny':total,'large_rise_count':sum(r['涨跌幅']>=5 for r in quotes.values()),
                      'large_fall_count':sum(r['涨跌幅']<=-5 for r in quotes.values())}, 'indices':indices, 'industries':industries,
            'candidates':candidates, 'watchlist':watch,
            'candidate_rule':candidate_rule,
            'enrichment': {k:v for k,v in extra.items() if k != 'latest_news'},
            'limitations':['成交额不是资金净流入', '候选规则未经收益验证',
                           '事件仅为目标日有限样本，不能代表完整新闻或实时盘前事件；公告日期不能用于判定盘中已知信息']}
        if self.product and result['mode'] == 'PRE_MARKET':
            from scheduler import is_trading_day, previous_trading_day
            now = self.clock().astimezone(CST)
            prior_day = previous_trading_day(now.date())
            if is_trading_day(now.date()) and now.hour < 9 and prior_day and prior_day.isoformat() == result['target_date']:
                ai_input['event_update'] = extra.get('latest_news')
        write_json(daily/'ai-input.json', ai_input)
        if self.product:
            from history import bars, collect
            from decision import build, evidence, interpret
            from predictions import archive, review
            history = collect(self,result['target_date'],[],candidates,stage='stocks',initial=history)
            index_history = {symbol:bars(read_json(daily/f'index-{symbol}.json')['rows'],result['target_date'])[0]
                             for symbol in ('sh000001','sz399001','sh000300')}
            decision = build(ai_input,history,candidates,index_history,self.clock().astimezone(CST))
            from opportunities import add_evidence, VERSION as opportunity_version
            decision['candidate_version'] = opportunity_version
            for plan in decision['stocks']:
                candidate = next(r for r in candidates if r['代码']==plan['code'])
                plan['opportunity_type'] = candidate['opportunity_type']
            ai_input['opportunities'] = opportunities
            refs = evidence(ai_input,decision,history)
            add_evidence(refs,opportunities)
            from research_context import build_context, collect_observations, add_evidence as context_evidence
            print('RESEARCH_STAGE:正在更新海外行情与事件', flush=True)
            if self.calls<self.limit:collect_observations(self,result['target_date'])
            ai_input['decision']=decision
            ai_input['research_window']={'market_baseline_date':result['target_date'],
                'run_started_at':result['generated_at'],
                'external_information_cutoff':self.clock().astimezone(CST).isoformat()}
            ai_input['research_context']=build_context(self.root,ai_input,include_updates=True)
            context_evidence(refs,ai_input['research_context'])
            print('RESEARCH_STAGE:正在生成并核验研究结论', flush=True)
            result['ai'] = interpret(self.root,ai_input,decision,refs,self.ai_settings)
            print('RESEARCH_STAGE:正在保存研究结果', flush=True)
            ai_input['data_completeness']={'required':result['required'],'readiness':result['readiness'],
                'optional':result['optional'],'external_statuses':read_json(daily/'observations/summary.json',{}).get('statuses',{}),
                'context_errors':ai_input['research_context'].get('data_errors',[]),'ai_status':result['ai']['status']}
            record = archive(self.root,ai_input,decision,refs,self.clock().astimezone(CST))
            reviewed = review(self.root,index_history['sh000300'],self.clock().astimezone(CST))
            ai_input['decision'] = decision
            ai_input['decision_evidence'] = refs
            ai_input['prediction_id'] = record['id']
            ai_input['reviews'] = reviewed
            write_json(daily/'ai-input.json',ai_input)
            result['requests_this_run'],result['attempts_this_run'] = self.calls,self.attempts
            result['product'] = {'version':decision['version'],'market_action':decision['market']['action'],
                                 'prediction_id':record['id'],'gaps':decision['gaps']}
            body += '<section class="card"><h2>决策研究与原预测</h2><p>'+html.escape(decision['market']['action'])+' · '+html.escape(record['mode'])+'</p>'
            body += table(['板块','判断','确认与失效'],[[s['name'],s['state'],s['confirmation']+'；'+s['invalidation']] for s in decision['sectors']])
            body += table(['个股','状态','核验条件'],[[s['name'],s['state'],s['entry']['condition']] for s in decision['stocks']])+'</section>'
        else:
            from deepseek_analysis import analyze
            result['ai'] = analyze(self.root, ai_input, self.ai_settings)
        ai = result['ai']
        body += '<section class="card"><h2>DeepSeek 辅助解读</h2>'
        if ai['status'] == 'READY':
            body += '<p>模型：' + html.escape(ai['model']) + '；模型推断需与上方事实核对。</p>'
            parsed = ai.get('interpretation')
            if parsed:
                body += '<p>'+html.escape(parsed['summary'])+'</p><p>'+html.escape(parsed['market']['judgment'])+'</p>'
                for label,key in (('重点板块','sectors'),('研究股票','stocks')):
                    body += '<h3>'+label+'</h3>'+table(['代码','模型判断'],[[r['code'],r['judgment']] for r in parsed[key]])
                body += '<p>反证：'+html.escape(parsed['counterevidence'])+'</p>'
            else:
                body += '<div style="white-space:pre-wrap;overflow-wrap:anywhere">' + html.escape(ai['content']) + '</div>'
        else:
            body += '<p>' + html.escape(ai['message']) + '</p>'
        body += '</section>'
        path = self.reports / f"market-{result['target_date']}.html"
        result['generated_at'] = self.clock().astimezone(CST).isoformat()
        write_text(path, page_html('AI 市场雷达','核心市场雷达',result['target_date'],'READY',result['generated_at'],body))
        result['report'] = str(path)
        if self.product:
            ai_input['watchlist_quotes'] = [dict(quotes.get(row[0],{}),code=row[0],name=row[1],net_flow_cny=row[3],industry=membership.get(row[0])) for row in watch]
            for candidate in ai_input['candidates']:
                candidate['industry'] = membership.get(candidate['代码'])
            # One atomic record freezes the displayed report and evidence together.
            from version import PRODUCT_VERSION, PUBLICATION_SCHEMA_VERSION
            write_json(self.data/'publications'/f"{result['target_date']}.json",{
                'schema_version':PUBLICATION_SCHEMA_VERSION,'product_version':PRODUCT_VERSION,
                'target_date':result['target_date'],'generated_at':result['generated_at'],
                'required':result['required'],'analysis':ai_input,'html':path.read_text(encoding='utf-8')})

    def _watchlist_rows(self, result, daily):
        watch = []
        for item in result['watchlist']:
            code = clean_code(item['code'])
            values = []
            for kind, field, filename in [('stock','收盘',f'stock-{code}.json'),('flow','主力净流入-净额',f'flow-{code}.json')]:
                state = self.checkpoint['tasks'].get(f"daily:{result['target_date']}:{kind}:{code}", {})
                data = read_json(daily/filename, {}).get('rows', []) if state.get('status') == 'SUCCESS' else []
                values.append(next((r[field] for r in data if str(r['日期'])[:10] == result['target_date']), None))
            watch.append([code,item.get('name',''),*values])
        return watch

    def _write_status_page(self, result):
        coverage = result['coverage']
        ratio = coverage['valid_quotes']/coverage['universe']*100 if coverage['universe'] else 0
        body = '<section class="card"><h2>行情采集覆盖</h2>'
        body += f"<p id='coverage-label'>已验证 {coverage['valid_quotes']} / {coverage['universe']} 只（{ratio:.1f}%），缺口 {coverage['missing']} 只。</p>"
        body += f"<progress aria-labelledby='coverage-label' value='{coverage['valid_quotes']}' max='{max(1,coverage['universe'])}'>{ratio:.1f}%</progress>"
        body += '<p>这是本次运行结束时的数据覆盖，不代表正在实时采集。</p></section>'
        if result.get('report'):
            body += '<p><a class="action" href="' + html.escape(Path(result['report']).name) + '">打开本次市场报告</a></p>'
        else:
            complete = sorted((self.data/'publications').glob('*.json'),reverse=True)
            if complete:
                day = complete[0].stem
                body += f'<p>本次未发布新报告，继续展示上次完整正式报告：<a href="market-{day}.html">{day}</a>。</p>'
            else:
                body += '<p>尚无符合当前必需数据门槛的完整正式报告。旧文件保留为历史研究草稿，不能冒充上次完整报告。</p>'
        labels = {'MARKET_READY':'市场行情与指数', 'INDUSTRY_READY':'申万行业',
                  'THEME_READY':'概念', 'FUND_FLOW_READY':'板块资金', 'WATCHLIST_READY':'自选股行情'}
        body += '<section class="card"><h2>模块就绪状态</h2>' + table(['模块','状态'],
            [(labels[k],'就绪' if v else '未齐备 / 未接入') for k,v in result['readiness'].items()]) + '</section>'
        body += '<p>' + ('盘前模式：使用最近已完成交易日。' if result['mode'] == 'PRE_MARKET'
                        else '盘后模式：使用指数确认的最近已完成交易日。') + '</p>'
        body += '<p>AI 解读：' + html.escape(result['ai'].get('message', result['ai']['status'])) + '</p>'
        if result['watchlist']:
            body += '<section class="card"><h2>自选股 · 独立数据</h2><p>仅显示目标交易日已核验字段；缺失不填零。个股数据不代表全市场已齐备。</p>'
            body += table(['代码','名称','收盘','主力净流入/元'],
                          self._watchlist_rows(result, self.data/'daily'/result['target_date'])) + '</section>'
        body += '<section class="card"><h2>核心发布门槛</h2>' + table(['模块','通过','原因'],
            [(r['name'],r['ok'],r['reason']) for r in result['required']]) + '</section>'
        body += '<section class="card"><h2>本次逐项缺口</h2>' + table(['类别','代码','名称','原因'],
            [[r['kind'],r['code'],r['name'],r['reason']] for r in result.get('data_gaps',[])]) + '</section>'
        body += '<section class="card"><h2>增强模块</h2>' + table(['模块','状态'],result['optional'].items()) + '</section>'
        body += '<section class="card"><h2>本地快照日期</h2>' + table(['快照','日期'],
            [(k,v or '缺失') for k,v in result['snapshots'].items()]) + '</section>'
        body += f"<section class='card'><h2>运行统计</h2><p>本次任务 {self.calls} / {self.limit}；含重试调用 {self.attempts} 次。一个 AKShare 任务内部可能包含多次分页 HTTP 请求。</p>"
        body += f"<p>股票范围 {result['coverage']['universe']}；目标日有效行情 {result['coverage']['valid_quotes']}；缺口 {result['coverage']['missing']}。</p>"
        for path in sorted(self.reports.glob('market-*.html'), reverse=True)[:5]:
            body += f"<p><a href='{path.name}'>{path.stem}</a>（请核对日期）</p>"
        body += '</section>'
        errors = [(k,v) for k,v in self.checkpoint['tasks'].items()
                  if v['status'] in ('FAILED','PARTIAL') and
                  (k.startswith(f"daily:{result['target_date']}:") or k.startswith('snapshot:'))]
        errors.sort(key=lambda item:item[1].get('updated_at',''), reverse=True)
        body += '<details class="card"><summary>最近异常（最多20条）</summary><div class="tasks">' + table(['任务','状态','原因'],
            [(k,v['status'],v.get('error','')) for k,v in errors[:20]]) + '</div></details>'
        body += '<section class="card"><h2>提示</h2>' + table(['提示'],[[m] for m in self.messages[:20]]) + '</section>'
        write_text(self.reports/'index.html', page_html('AI 市场雷达状态','运行状态',result['target_date'],result['status'],result['generated_at'],body))
