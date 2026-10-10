"""Loopback-only control panel; the existing CLI owns collection and its OS lock."""
import json
import os
import copy
import time
import hashlib
import re
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from collector import stock_market
from storage import read_json, read_optional_json, write_json, workspace_lock
from research_brief import evidence_registry, input_digest
from refresh_ai_review import publication_digest

ROOT = Path(__file__).resolve().parent


def analysis_dates(root):
    return sorted([p.stem for p in (root/'data'/'publications').glob('*.json')
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',p.stem)],reverse=True)


def analysis_versions(root):
    result = {}
    for target in analysis_dates(root):
        files = [root/'data/publications'/f'{target}.json', root/'data/reviews/summary.json',
                 root/'data/checkpoint.json', *(root/'data/daily'/target).rglob('*.json')]
        stamps = [(str(p.relative_to(root)),p.stat().st_mtime_ns,p.stat().st_size) for p in files if p.is_file()]
        result[target] = hashlib.sha256(repr(stamps).encode()).hexdigest()
    return result


def saved_analysis(root, target):
    """Read a published snapshot; never recalculate or collect during browsing."""
    if target not in analysis_dates(root):
        return None
    daily = root/'data'/'daily'/target
    publication=read_json(root/'data'/'publications'/f'{target}.json',{})
    if not isinstance(publication,dict) or not isinstance(publication.get('analysis',{}),dict):
        raise ValueError('正式报告JSON结构损坏')
    summary = publication.get('analysis',{})
    if summary.get('target_date') != target:
        return None
    errors = []
    brief = read_optional_json(daily/'research-brief.json', errors)
    if (brief.get('status') == 'READY' and brief.get('target_date') == target
            and brief.get('input_digest') == input_digest(summary)):
        summary['research_brief'] = brief
    summary['evidence'] = summary.get('decision_evidence') or evidence_registry(summary)
    summary['reviews'] = read_optional_json(root/'data'/'reviews'/'summary.json',errors) or summary.get('reviews',{})
    summary['data_errors'] = errors
    return summary


def watchlist_rows(value):
    if not isinstance(value, str) or len(value) > 3000:
        raise ValueError('请输入自选股，每行六位代码及可选名称，最多50只')
    rows = []
    for line in value.splitlines():
        fields = line.strip().split(maxsplit=1)
        if not fields:
            continue
        if not re.fullmatch(r'[036489]\d{5}', fields[0]):
            raise ValueError('股票代码必须是六位沪深京代码')
        if any(row['code'] == fields[0] for row in rows):
            raise ValueError(f'股票代码 {fields[0]} 重复，请只保留一行')
        rows.append({'code': fields[0], 'name': fields[1] if len(fields) > 1 else fields[0],
                     'market': stock_market(fields[0])})
    if len(rows) > 50:
        raise ValueError('自选股最多50只')
    return rows


def research_progress(lines,running,exit_code):
    stages=['正在检查行情与已有研究','正在更新海外行情与事件','正在生成并核验研究结论','正在保存研究结果']
    recorded=[stages.index(line.removeprefix('RESEARCH_STAGE:')) for line in lines
        if line.startswith('RESEARCH_STAGE:') and line.removeprefix('RESEARCH_STAGE:') in stages]
    completed=max(recorded,default=0) if running or recorded else None
    if recorded and not running and exit_code==0:completed=4
    return {'completed':completed,'known':bool(recorded),'total':4,
        'steps':['检查行情','补充证据','分析与核验','保存结果']}


def same_review_baseline(review,payload):
    from research_context import material_digest
    assessed=review.get('ai_policy_assessment',{})
    current=payload.get('decision',{}).get('market',{})
    def dimensions(m):
        return [{k:d.get(k) for k in ('id','score','weight')} for d in m.get('dimensions',[]) if d['id']!='policy']
    def trends(m):
        return {code:{k:t.get(k) for k in ('date','close','ma5','ma20','return5_percent','amount_ratio5')}
                for code,t in m.get('index_trends',{}).items() if t and t.get('samples',0)>=21}
    old_stocks=review.get('research_evidence',{}).get('stocks',[])
    stocks=payload.get('decision',{}).get('stocks',[])
    def sectors(opportunities):
        return [{k:s.get(k) for k in ('code','type','member_signature','history_complete','index_change_percent',
            'five_percent','relative','breadth','amount_ratio5')} for s in opportunities.get('sectors',[])]
    return review.get('target_date')==payload.get('target_date') and len(trends(assessed))==3 and material_digest(trends(assessed))==material_digest(trends(current)) and dimensions(assessed)==dimensions(current) and sorted(s['code'] for s in old_stocks)==sorted(s['code'] for s in stocks) and material_digest(sectors(review.get('opportunities',{})))==material_digest(sectors(payload.get('opportunities',{})))


def research_analysis(root,target):
    summary=saved_analysis(root,target)
    if summary:
        publication = read_json(root/'data/publications'/f'{target}.json',{})
        summary['publication'] = {'generated_at':publication.get('generated_at'),
            'required':publication.get('required',[]), 'status':'PUBLISHED'}
        errors=summary['data_errors']
        diagnostic = read_optional_json(root/'data/daily'/target/'opportunity-snapshot.json',errors)
        if diagnostic.get('publication_digest') == publication_digest(publication) and not isinstance(diagnostic.get('opportunities'),dict):
            errors.append('opportunity-snapshot.json 结构损坏，跳过可选补充；原正式报告保留')
            diagnostic={}
        if diagnostic.get('publication_digest') == publication_digest(publication):
            summary['opportunities'] = diagnostic['opportunities']
            summary['evidence'].update(diagnostic.get('evidence',{}))
        review = read_optional_json(root/'data/daily'/target/'ai-review.json',errors)
        attempt=read_optional_json(root/'data/daily'/target/'ai-review-attempt.json',errors)
        if attempt:
            summary['ai_review_attempt']={k:attempt.get(k) for k in
                ('status','message','finish_reason','usage','model','generated_at','validation_error')}
        if review.get('status') == 'READY' and (not isinstance(review.get('interpretation'),dict)
                or review.get('opportunities') is not None and not isinstance(review['opportunities'],dict)):
            errors.append('ai-review.json 结构损坏，跳过可选AI补充；原正式报告保留')
            review={}
        exact_review=review.get('publication_digest') == publication_digest(publication)
        if review.get('status') == 'READY' and (exact_review or same_review_baseline(review,summary)):
            summary['ai_review'] = review
            if not exact_review:summary['ai_review']['review_note']='上一份合格研究：行情与候选基准一致；补充证据逐项核对'
            if exact_review and review.get('opportunities'):
                summary['opportunities'] = review['opportunities']
                summary['opportunity_review'] = review.get('opportunity_review')
                summary['evidence'].update(review.get('opportunity_evidence',{}))
        score_record=summary.get('ai_review')
        raw_market=copy.deepcopy(summary.get('decision',{}).get('market',{}))
        score_applied=False
        if summary.get('decision') and score_record and score_record.get('business_status')=='PASSED' and score_record.get('final_market',{}).get('score_basis')=='final-effective-v1':
            summary['decision']['original_market']=raw_market
            summary['decision']['market']=copy.deepcopy(score_record['final_market'])
            score_applied=True
        status = summary.get('ai_review', {}).get('research_evidence') or summary.get('decision', {}).get('ai', {}).get('research_evidence')
        if status:
            from participation import add_evidence
            summary['research_evidence'] = status
            add_evidence(summary['evidence'], status)
        summary['historical_evidence']=copy.deepcopy(summary['evidence'])
        if summary.get('decision'):
            from participation import snapshot, evidence_digest
            current=snapshot(root,summary)
            summary['research_evidence']=current
            from participation import add_evidence
            add_evidence(summary['evidence'],current)
            errors.extend(current.get('data_errors',[]))
            original_ai=summary['decision'].get('ai',{})
            def qualification_changed(record):
                stored=record.get('research_evidence')
                return evidence_digest(stored)!=evidence_digest(current) if stored else record.get('evidence_digest')!=evidence_digest(current)
            if original_ai.get('interpretation') and qualification_changed(original_ai):
                original_ai['evidence_status']='STALE'
                errors.append('原报告AI解释属于原证据快照，不能覆盖当前程序资格')
            if summary.get('ai_review') and qualification_changed(review):
                summary['ai_review']['evidence_status']='STALE'
                errors.append('AI补充研究基于旧证据版本，保留历史解释；当前资格以程序证据为准')
            if summary.get('ai_review') and review.get('business_status') != 'PASSED':
                summary['ai_review']['evidence_status']='LEGACY_UNVALIDATED'
                errors.append('旧AI研究尚未按新版业务规则校验，不能作为当前合格买入结论')
            summary['evidence'].update({'participation':{'title':'当前程序证据资格','date':target,'facts':current}})
        summary['analysis_version']=analysis_versions(root).get(target)
        from research_context import build_context, add_evidence as context_evidence, context_digest, coverage_report
        saved_context=summary.get('research_context')
        # The reading surface shows current supplemental evidence, while
        # the publication's research window remains immutable.
        context=build_context(root,{**summary,'research_window':{}},include_updates=True)
        summary['research_context']=context
        context_evidence(summary['evidence'],context)
        errors.extend(context['data_errors'])
        original_ai=summary.get('decision',{}).get('ai',{})
        stock_codes=original_ai.get('context_stock_codes')
        if stock_codes is None and saved_context and original_ai.get('context_digest')==context_digest(saved_context):
            stock_codes=[r['code'] for r in saved_context['funds']['stocks']]
        if original_ai.get('interpretation') and (context_digest(saved_context,stock_codes)!=context_digest(context,stock_codes)
                if saved_context else original_ai.get('context_digest')!=context_digest(context,stock_codes)):
            original_ai['evidence_status']='STALE'
            errors.append('原报告AI的资金或事件证据已变化，保留历史解释，不能作为当前合格研究')
        from research_context import changed_modules
        saved_review_context=review.get('research_context')
        if saved_review_context:
            context_evidence(summary['historical_evidence'],saved_review_context)
        changes=changed_modules(saved_review_context,context,review.get('context_stock_codes')) if saved_review_context else []
        if summary.get('ai_review') and (bool(changes) if saved_review_context else review.get('context_digest')!=context_digest(context,review.get('context_stock_codes'))):
            summary['ai_review']['evidence_status']='STALE'
            names={'funds':'资金','international':'海外行情','events':'政策与资讯','companies':'公司公告','activity':'成交活跃度'}
            summary['ai_review']['changed_modules']=changes
            errors.append('、'.join(names[c] for c in changes)+'证据已变化，相关AI解释待更新；已确认行情与上次研究保留')
        if summary.get('ai_review') and saved_review_context:
            policy=review.get('interpretation',{}).get('policy_assessment',{})
            urls=policy.get('evidence_urls',[])
            def policy_items(c):
                return {e['url']:e for group in ('original_items','supplemental_items')
                        for e in c.get('events',{}).get(group,[])
                        if e.get('evidence_status')=='OFFICIAL_EXCERPT'}
            before,after=policy_items(saved_review_context),policy_items(context)
            from research_context import material_digest
            summary['ai_review']['policy_evidence_status']='CURRENT' if urls and all(
                i in before and i in after and material_digest(before[i])==material_digest(after[i]) for i in urls
                ) and material_digest(list(before.values()))==material_digest(list(after.values())) else 'NEEDS_REVIEW'
        if review.get('status')=='READY' and not summary.get('ai_review'):
            review['evidence_status']='DIFFERENT_BASELINE'
            summary['previous_ai_review']=review
        historical=read_optional_json(root/'data/daily'/target/'ai-review-historical.json',errors)
        if historical.get('status')=='READY' and historical.get('publication_digest')==publication_digest(publication):
            summary['historical_review']=historical
        from research_facts import price_facts, validate_price_facts
        summary['price_facts']={kind:{r['code']:price_facts(r,target) for r in summary.get('decision',{}).get(kind,[])}
            for kind in ('sectors','stocks')}
        for record in (summary.get('ai_review'),summary.get('decision',{}).get('ai')):
            if not record or not record.get('interpretation'):
                continue
            try:
                validate_price_facts(record['interpretation'],summary.get('decision',{}))
            except (ValueError,TypeError,KeyError) as error:
                record.update(status='FAILED',business_status='FAILED',evidence_status='FACT_MISMATCH',
                    message='关键事实校验不通过，已暂停采用；原文保留供历史核对',
                    fact_validation={'status':'FAILED','reason':str(error)})
                errors.append('AI关键事实不一致，当前意见暂停采用：'+str(error))
        summary['data_coverage']=coverage_report(summary,context)
        if score_applied and (score_record.get('evidence_status') or score_record.get('business_status')!='PASSED'):
            summary['decision']['market']=raw_market
            from participation import snapshot,add_evidence
            summary['research_evidence']=snapshot(root,summary)
            add_evidence(summary['evidence'],summary['research_evidence'])
        return summary

class Dashboard(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()

    def __init__(self, address, root=ROOT):
        self.root, self.token = root, secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.process = None
        self.output = []
        self.stages = []
        self.exit_code = None
        self.reading_snapshots = {}
        self.started_at = None
        super().__init__(address, Handler)

    def start_run(self, target, refresh=False, mode=None):
        if target:
            try:
                valid = date.fromisoformat(target).isoformat() == target
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('日期格式为YYYY-MM-DD')
        command = [sys.executable, '-u', str(self.root/'run.py'), '--timeout', '30', '--retries', '0']
        if target:
            command += ['--date', target]
        if refresh:
            command += ['--refresh-snapshots']
        if mode:
            command=[sys.executable,'-u',str(self.root/'update_research.py'),'--mode',mode]
            if target:command+=['--date',target]
            if refresh:command+=['--refresh-snapshots']
        self.pin_reading()
        self.process = subprocess.Popen(command, cwd=self.root, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                                        env={**os.environ,'PYTHONIOENCODING':'utf-8'})
        self.output, self.exit_code = [], None
        threading.Thread(target=self.collect_output, args=(self.process,), daemon=True).start()

    def pin_reading(self):
        self.reading_snapshots={target:research_analysis(self.root,target) for target in analysis_dates(self.root)}
        self.started_at=time.monotonic()
        self.stages=[]

    def collect_output(self, process):
        for line in process.stdout:
            with self.lock:
                self.output = (self.output + [line.rstrip()])[-80:]
                if line.startswith('RESEARCH_STAGE:'):
                    self.stages=(self.stages+[line.rstrip()])[-8:]
        process.stdout.close()
        code = process.wait()
        with self.lock:
            self.exit_code = code

    def start_ai_retry(self, target):
        if target not in analysis_dates(self.root):
            raise ValueError('只能重试已正式发布的日期')
        command = [sys.executable,'-u',str(self.root/'refresh_ai_review.py'),'--date',target,'--retry']
        self.pin_reading()
        self.process = subprocess.Popen(command,cwd=self.root,stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',env={**os.environ,'PYTHONIOENCODING':'utf-8'})
        self.output,self.exit_code=[],None
        threading.Thread(target=self.collect_output,args=(self.process,),daemon=True).start()


    def start_observations(self,target):
        command=[sys.executable,'-u',str(self.root/'refresh_observations.py'),'--date',target]
        self.pin_reading()
        self.process=subprocess.Popen(command,cwd=self.root,stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',env={**os.environ,'PYTHONIOENCODING':'utf-8'})
        self.output,self.exit_code=[],None
        threading.Thread(target=self.collect_output,args=(self.process,),daemon=True).start()

    def install_schedule(self):
        command=['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(self.root/'install-schedule.ps1')]
        try:
            result=subprocess.run(command,cwd=self.root,capture_output=True,timeout=30,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except subprocess.TimeoutExpired:
            raise ValueError('自动运行任务安装超时，尚未启用；请检查Windows任务计划服务')
        if result.returncode!=0:
            raise ValueError('自动运行任务安装失败，尚未启用；请检查Windows任务计划服务和当前用户权限')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, kind='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def valid_host(self):
        return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {'error': '仅允许本机访问'})
        root = self.server.root
        request = urlsplit(self.path)
        path = request.path
        if path == '/':
            return self.send(200, (root/'dashboard.html').read_bytes(), 'text/html; charset=utf-8')
        if path == '/ai-prompt':
            return self.send(200, (ROOT/'prompts/decision_research.txt').read_bytes(), 'text/plain; charset=utf-8')
        if path == '/opportunity-alignment-prompt':
            return self.send(200, (ROOT/'prompts/opportunity_condition_alignment.txt').read_bytes(), 'text/plain; charset=utf-8')
        if path == '/opportunity-fact-prompt':
            return self.send(200, (ROOT/'prompts/opportunity_fact_correction.txt').read_bytes(), 'text/plain; charset=utf-8')
        if path == '/entry-brief-prompt':
            return self.send(200, (ROOT/'prompts/entry_brief.txt').read_bytes(), 'text/plain; charset=utf-8')
        if path == '/ai-correction-prompt':
            return self.send(200, (ROOT/'prompts/decision_fact_correction.txt').read_bytes(), 'text/plain; charset=utf-8')
        if path in ('/dashboard.css', '/dashboard.js', '/dashboard-visuals.js','/dashboard-research.js'):
            kind = 'text/css' if path.endswith('.css') else 'text/javascript'
            return self.send(200, (root/path[1:]).read_bytes(), kind+'; charset=utf-8')
        if path == '/api/status':
            with self.server.lock:
                errors=[]
                config=read_optional_json(root/'config.json',errors)
                if (not isinstance(config.get('watchlist',[]),list)
                        or not isinstance(config.get('schedule',{}),dict)
                        or config.get('deepseek') is not None and not isinstance(config['deepseek'],dict)):
                    errors.append('config.json 字段类型无效；请恢复配置备份')
                    config={}
                running = self.server.process is not None and self.server.exit_code is None
                last = read_optional_json(root/'data'/'last_run.json',errors)
                from deepseek_analysis import load_api_key, model_budget
                settings=config.get('deepseek')
                usage=read_optional_json(root/'data/ai/usage.json',errors)
                try:
                    budget=model_budget(settings or {},usage)
                except (ValueError,TypeError,KeyError) as error:
                    budget={'new_request_possible':False,'error':str(error)}
                from version import PRODUCT_VERSION
                from predictions import forecast_status
                forecast=forecast_status(root)
                from scheduler import schedule_summary
                schedule_state=read_optional_json(root/'data'/'schedule.json',errors)
                schedule_enabled=config.get('schedule',{}).get('enabled',False)
                schedule_details=schedule_summary(schedule_state,schedule_enabled)
                qualified=[]
                for review_path in (root/'data/daily').glob('*/ai-review.json'):
                    item=read_optional_json(review_path,errors)
                    if item.get('status')=='READY' and item.get('business_status')=='PASSED' and item.get('fact_validation',{}).get('status')=='PASSED':
                        qualified.append(item.get('generated_at',''))
                original=last.get('ai',{})
                if original.get('status')=='READY' and original.get('business_status')=='PASSED' and original.get('fact_validation',{}).get('status')=='PASSED':
                    qualified.append(original.get('generated_at',''))
                schedule_details['latest_qualified_research']=max(qualified,default=None)
                errors.extend(forecast.get('data_errors',[]))
                result_line=next((line.removeprefix('RESEARCH_RESULT:') for line in reversed(self.server.output)
                    if line.startswith('RESEARCH_RESULT:')),None)
                return self.send(200, {'running': running, 'exit_code': self.server.exit_code,
                    'update_result':json.loads(result_line) if result_line else None,
                    'log': self.server.output, 'token': self.server.token,
                    'progress':research_progress(self.server.stages or self.server.output,running,self.server.exit_code),
                    'elapsed_seconds':round(time.monotonic()-self.server.started_at) if self.server.started_at else 0,
                    'stage':next((line.removeprefix('RESEARCH_STAGE:') for line in reversed(self.server.stages or self.server.output)
                                  if line.startswith('RESEARCH_STAGE:')), '正在检查行情与已有研究'),
                    'last_run': {k:last[k] for k in ('target_date','generated_at','status','coverage','ai','snapshots','required','data_gaps','optional') if k in last},
                    'config_error':next((e for e in errors if e.startswith('config.json')),''),
                    'product_version':PRODUCT_VERSION,
                    'forecast':forecast,
                    'data_errors':errors,
                    'ai_settings':{'enabled':settings is not None,'key_configured':bool(load_api_key(root)),
                        **{k:(settings or {}).get(k,v) for k,v in (('model','deepseek-v4-pro'),('reasoning_effort','max'),
                            ('max_tokens',32768),('max_requests_per_day',None),('max_total_tokens_per_day',262144))}},
                    'ai_usage':usage,
                    'ai_budget':budget,
                    'watchlist': config.get('watchlist', []),
                    'analysis_dates': analysis_dates(root),
                    'analysis_versions':analysis_versions(root),
                    'schedule':{'enabled':config.get('schedule',{}).get('enabled',False),
                        'installation':read_optional_json(root/'data'/'scheduler-install.json',errors),
                        'state':schedule_state,'summary':schedule_details},
                    'reports': sorted([p.name for p in (root/'reports').glob('*.html')
                        if re.fullmatch(r'(market-\d{4}-\d{2}-\d{2}|index)\.html', p.name)], reverse=True)})
        if path == '/api/analysis':
            values = parse_qs(request.query, keep_blank_values=True).get('date', [])
            if len(values) != 1:
                return self.send(400, {'error': '请选择一个已发布的交易日'})
            try:
                target = date.fromisoformat(values[0]).isoformat()
                if target != values[0]:
                    raise ValueError('日期格式无效')
            except ValueError:
                return self.send(400, {'error': '日期格式为YYYY-MM-DD'})
            try:
                summary = self.server.reading_snapshots.get(target) if self.server.process is not None and self.server.exit_code is None else None
                summary = copy.deepcopy(summary) if summary else research_analysis(root,target)
            except (ValueError,UnicodeError,OSError):
                return self.send(500,{'error':'正式报告JSON损坏或无法读取，请恢复备份或选择其他日期'})
            return self.send(200, summary) if summary else self.send(404, {'error': '该日期尚无已发布的分析'})
        if path == '/reports/index.html':
            from reporting import page_html, table
            last=read_optional_json(root/'data/last_run.json',[])
            content='<section class="card"><h2>这页用于排查采集问题</h2><p>日常判断、图表与事件请在研究首页阅读。行情采集通过不等于完整研究已经就绪。</p></section>'
            content+='<section class="card"><h2>行情与行业</h2>'+table(['项目','采集结果','说明'],
                [(r['name'],'通过' if r['ok'] else '未通过',r.get('reason','')) for r in last.get('required',[])])+'</section>'
            content+='<section class="card"><h2>补充数据</h2>'+table(['项目','状态'],last.get('optional',{}).items())+'</section>'
            content+='<details class="card"><summary>采集缺口</summary>'+table(['类别','名称','原因'],
                [(r.get('kind'),r.get('name'),r.get('reason')) for r in last.get('data_gaps',[])])+'</details>'
            return self.send(200,page_html('市场雷达 · 采集诊断','采集诊断',last.get('target_date','未运行'),
                last.get('status','UNKNOWN'),last.get('generated_at','未记录'),content),'text/html; charset=utf-8')
        if re.fullmatch(r'/reports/deepseek-test-\d{4}-\d{2}-\d{2}\.html', path):
            self.send_response(302)
            self.send_header('Location', '/?view=overview')
            self.send_header('Content-Length', '0')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        if re.fullmatch(r'/reports/(market-\d{4}-\d{2}-\d{2}|index)\.html', path):
            report = root/'reports'/path.rsplit('/', 1)[1]
            if report.name.startswith('market-'):
                try:
                    published=read_json(root/'data'/'publications'/f'{report.stem[7:]}.json',{})
                    if not isinstance(published,dict) or published and (
                            not isinstance(published.get('html'),str) or not published['html']):
                        raise ValueError('正式报告HTML结构损坏')
                except (ValueError,UnicodeError,OSError):
                    return self.send(500,{'error':'正式报告JSON损坏或无法读取，请恢复备份或选择其他日期'})
                if published:
                    document=published['html']
                    # Restyle the reading surface without changing frozen facts.
                    reader_style='<style>:root{--bg:#f6f5f2;--card:#fff;--text:#27292d;--muted:#697078;--line:#deddd8;--good:#6554a5;--warn:#976c29}main{max-width:1100px}h1{font-size:26px}.card{border-radius:6px;padding:18px}a{color:#6554a5}.action{background:#f0ecf7;border-color:#b7abd0}.archive-context{padding:12px 0 20px;border-bottom:1px solid #deddd8;margin-bottom:20px}</style>'
                    document=document.replace('</head>',reader_style+'</head>',1)
                    document=document.replace('<main>',f'<main><div class="archive-context"><a href="/?date={report.stem[7:]}">返回研究首页</a><p>原始存档 · 下方结论与状态反映生成当时的证据。最新补充研究请在首页阅读。</p></div>',1)
                    return self.send(200,document,'text/html; charset=utf-8')
            if report.is_file():
                if report.name.startswith('market-'):
                    legacy=report.read_text(encoding='utf-8').replace('<main>','<main><p class="card banner">历史研究草稿：未按当前必需数据门槛验收，不是正式报告。</p>',1)
                    return self.send(200,legacy,'text/html; charset=utf-8')
                return self.send(200, report.read_bytes(), 'text/html; charset=utf-8')
        return self.send(404, {'error': '页面不存在'})

    def do_POST(self):
        expected_origin = f'http://127.0.0.1:{self.server.server_port}'
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                raise ValueError('请求大小无效')
            # Consume the bounded body before replying; unread POST data can
            # cause Windows to reset the connection instead of delivering 403.
            self.connection.settimeout(5)
            body=self.rfile.read(length)
            if (not self.valid_host() or self.headers.get('Origin') not in (None, expected_origin)
                    or self.headers.get('X-Radar-Token') != self.server.token):
                return self.send(403, {'error': '请求来源校验失败，请刷新本地页面'})
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError('请求格式无效')
            with self.server.lock:
                if self.server.process is not None and self.server.exit_code is None:
                    return self.send(409, {'error': '采集中，请等待完成后再操作'})
                if self.path == '/api/run':
                    target = data.get('date', '')
                    if not isinstance(target, str):
                        raise ValueError('日期格式无效')
                    refresh = data.get('refresh', False)
                    if not isinstance(refresh, bool):
                        raise ValueError('刷新选项格式无效')
                    mode=data.get('mode')
                    if mode is not None and mode not in ('current','supplement','replay'):
                        raise ValueError('更新方式无效')
                    if mode in ('supplement','replay') and not target:
                        raise ValueError('请选择行情日期')
                    if mode is None:self.server.start_run(target, refresh)
                    else:self.server.start_run(target, refresh, mode=mode)
                elif self.path == '/api/ai-config':
                    from deepseek_analysis import configure_ai
                    with workspace_lock(self.server.root):
                        configure_ai(self.server.root,data.get('key',''),data.get('enabled'))
                    return self.send(200,{'ok':True,'message':'配置已保存；下一步测试连接。Key不会回显。'})
                elif self.path == '/api/ai-test':
                    from deepseek_analysis import test_connection
                    return self.send(200,test_connection(self.server.root))
                elif self.path == '/api/ai-retry':
                    target=data.get('date')
                    if not isinstance(target,str) or target not in analysis_dates(self.server.root):
                        raise ValueError('请选择已正式发布的日期')
                    self.server.start_ai_retry(target)
                elif self.path == '/api/watchlist':
                    rows = watchlist_rows(data.get('text'))
                    with workspace_lock(self.server.root):
                        config = read_json(self.server.root/'config.json')
                        config['watchlist'] = rows
                        write_json(self.server.root/'config.json', config)
                elif self.path == '/api/observations':
                    target=data.get('date')
                    if not isinstance(target,str) or target not in analysis_dates(self.server.root):
                        raise ValueError('请选择已正式发布的日期')
                    self.server.start_observations(target)
                elif self.path == '/api/schedule':
                    enabled = data.get('enabled')
                    if not isinstance(enabled,bool):
                        raise ValueError('定时选项格式无效')
                    with workspace_lock(self.server.root):
                        config = read_json(self.server.root/'config.json')
                        if enabled:self.server.install_schedule()
                        config.setdefault('schedule',{})['enabled'] = enabled
                        if enabled:config['schedule']['pre_market']='08:30'
                        write_json(self.server.root/'config.json',config)
                else:
                    return self.send(404, {'error': '操作不存在'})
            return self.send(200, {'ok': True})
        except json.JSONDecodeError:
            return self.send(400, {'error': '请求格式无效，请刷新页面后重试'})
        except ValueError as error:
            return self.send(400, {'error': str(error)})
        except TypeError:
            return self.send(400, {'error': '输入格式无效'})
        except BlockingIOError:
            return self.send(409, {'error': '命令行采集正在进行，结束后才能保存自选股'})
        except OSError:
            return self.send(500, {'error': '无法启动进程或保存文件，请检查目录权限'})


if __name__ == '__main__':
    address = ('127.0.0.1', 8768)
    try:
        server = Dashboard(address)
    except OSError:
        raise SystemExit('8768端口被占用。若工作台已经启动，请打开 http://127.0.0.1:8768')
    print('本地工作台：http://127.0.0.1:8768；关闭此窗口将停止服务。', flush=True)
    if '--no-browser' not in sys.argv:
        webbrowser.open('http://127.0.0.1:8768')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
