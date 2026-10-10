"""Local Beijing-time scheduling, missed-run detection and bounded continuation."""
import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from collector import CST, now_cst
from storage import read_json, write_json, workspace_lock

ROOT = Path(__file__).resolve().parent
CALENDAR_SOURCE = 'https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml'
HOLIDAYS_2026 = [('01-01','01-03'),('02-15','02-23'),('04-04','04-06'),
                 ('05-01','05-05'),('06-19','06-21'),('09-25','09-27'),('10-01','10-07')]


def is_trading_day(day):
    if day.year != 2026:
        return None
    return day.weekday() < 5 and not any(start <= day.strftime('%m-%d') <= end for start,end in HOLIDAYS_2026)


def previous_trading_day(day):
    for offset in range(1,15):
        candidate = day-timedelta(days=offset)
        if is_trading_day(candidate):
            return candidate
    return None


def due_slots(now, state):
    """Catch up latest finished session, rather than invent old daily forecasts."""
    now = now.astimezone(CST)
    day = now.date()
    if is_trading_day(day) is None:
        return []
    slots = []
    if is_trading_day(day) and (8,30)<=(now.hour,now.minute)<(9,15):
        slots.append({'id':day.isoformat()+':pre','date':'','phase':'pre'})
    post = day if is_trading_day(day) and now.hour >= 18 else previous_trading_day(day)
    if post:
        slots.append({'id':post.isoformat()+':post','date':post.isoformat(),'phase':'post'})
    due = []
    for slot in slots:
        item = state.get('slots',{}).get(slot['id'],{})
        limit=1 if slot['phase']=='pre' else 3
        if item.get('status') in ('DONE','NEEDS_ATTENTION') or item.get('attempts',0) >= limit:
            continue
        last = item.get('attempted_at')
        if last and (now-datetime.fromisoformat(last)).total_seconds() < 2700:
            continue
        due.append(slot)
    return due


def schedule_summary(state, enabled, *, now=None):
    now=(now or now_cst()).astimezone(CST)
    slots=list(state.get('slots',{}).values())
    latest=max((s for s in slots if s.get('attempted_at')),key=lambda s:s['attempted_at'],default=None)
    successful=max((s for s in slots if s.get('status')=='DONE'),key=lambda s:s.get('finished_at',''),default=None)
    next_check=(now+timedelta(minutes=5-now.minute%5)).replace(second=0,microsecond=0) if enabled else None
    next_research=None
    if enabled:
        for offset in range(15):
            day=now.date()+timedelta(days=offset)
            if is_trading_day(day):
                for hour,minute in ((8,30),(18,0)):
                    candidate=datetime(day.year,day.month,day.day,hour,minute,tzinfo=CST)
                    if candidate>now:
                        next_research=candidate.isoformat()
                        break
            if next_research:break
    return {'latest_attempt':latest,'latest_success':successful,
        'next_check_estimate':next_check.isoformat() if next_check else None,'next_research_window':next_research,
        'calendar_status':'READY' if now.year==2026 else 'NEEDS_UPDATE',
        'today_trading':is_trading_day(now.date()),'checked_at':state.get('checked_at')}


def tick(root=ROOT, now=None):
    now = now or now_cst()
    if not read_json(root/'config.json',{}).get('schedule',{}).get('enabled',False):
        return {'status':'DISABLED'}
    try:
        with workspace_lock(root):
            path = root/'data'/'schedule.json'
            state = read_json(path,{'slots':{}})
            for slot_id, item in state['slots'].items():
                published_date=item.get('target_date',slot_id[:10])
                if item.get('status') == 'DONE' and not (root/'data/publications'/f'{published_date}.json').is_file():
                    item.update(status='RETRY_PENDING',message='旧完成记录没有符合当前门槛的正式发布，等待续采')
            slots = due_slots(now,state)
            state.update(calendar_source=CALENDAR_SOURCE, calendar_year=2026,
                         checked_at=now.isoformat(), calendar_status='READY' if now.year == 2026 else 'NEEDS_UPDATE')
            if not slots:
                write_json(path,state)
                return state
            slot = slots[0]
            item = state['slots'].setdefault(slot['id'],{'attempts':0})
            item.update(status='RUNNING',attempts=item['attempts']+1,attempted_at=now.isoformat())
            write_json(path,state)
    except BlockingIOError:
        return {'status':'BUSY'}
    command = [sys.executable,str(root/'run.py'),'--timeout','30','--retries','0']
    if slot['date']:
        command += ['--date',slot['date']]
    # run.py owns the same OS lock throughout all writes and model calls.
    log = root/'artifacts'/'scheduled-run.log'
    log.parent.mkdir(exist_ok=True)
    with log.open('a',encoding='utf-8') as output:
        result = subprocess.run(command,cwd=root,stdout=output,stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    with workspace_lock(root):
        state = read_json(path,{'slots':{}})
        item = state['slots'][slot['id']]
        last = read_json(root/'data'/'last_run.json',{})
        if slot['phase']=='pre':
            from predictions import selected_forwards
            forecast=selected_forwards(root,now_cst()).get(slot['id'][:10])
            successful=(result.returncode==0 and forecast and forecast.get('forecast_phase')=='PRE_MARKET'
                and forecast.get('source_snapshot_id')==last.get('product',{}).get('prediction_id')
                and last.get('ai',{}).get('status')=='READY' and last['ai'].get('business_status')=='PASSED'
                and datetime.fromisoformat(forecast['issued_at'])>=datetime.fromisoformat(item['attempted_at']))
            item['status']='DONE' if successful else 'NEEDS_ATTENTION'
        elif last.get('ai',{}).get('status') == 'FAILED':
            item['status'] = 'NEEDS_ATTENTION'
        elif result.returncode != 0:
            item['status'] = 'RETRY_PENDING'
        elif (last.get('status') == 'READY' and last.get('required') and all(row['ok'] for row in last['required'])
                and (root/'data/publications'/f"{last.get('target_date')}.json").is_file()
                and (not slot['date'] or last.get('target_date') == slot['date'])
                and not last.get('product',{}).get('gaps')):
            item['status'] = 'DONE'
        else:
            item['status'] = 'RETRY_PENDING'
        item.update(finished_at=now_cst().isoformat(),exit_code=result.returncode,target_date=last.get('target_date'),
                    message=('盘前预测已保存；原始版本保留' if item['status']=='DONE' else
                        '盘前自动更新未成功，保留最近研究；本日不自动重复调用模型') if slot['phase']=='pre' else
                        '本日最多3次续采；模型失败需人工核对，避免自动重复付费')
        write_json(path,state)
    return state


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='本地定时任务，每5分钟由Windows任务计划检查')
    parser.add_argument('--once',action='store_true')
    parser.parse_args()
    tick()
