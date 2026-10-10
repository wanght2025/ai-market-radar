"""User-selected current research or bounded historical supplementation."""
import argparse
import json
from datetime import date
from pathlib import Path

from collector import Collector, SubprocessFetcher, now_cst
from storage import read_json, workspace_lock


def update(root, mode, target='', refresh=False):
    from market_radar import Radar
    from scheduler import is_trading_day, previous_trading_day
    from research_context import collect_observations
    from refresh_ai_review import refresh_review
    print('RESEARCH_STAGE:正在检查行情与已有研究',flush=True)
    at=now_cst()
    if mode=='current':
        day=at.date()
        if is_trading_day(day) is True and at.hour>=16:
            target=day.isoformat()
        elif is_trading_day(day) is not None:
            target=previous_trading_day(day).isoformat()
        else:
            target=''
    path=root/'data/publications'/f'{target}.json'
    if not path.is_file() or refresh:
        settings=read_json(root/'config.json',{})
        result=Radar(root,SubprocessFetcher(root,30)).run(target or None,settings.get('watchlist',[]),
            refresh=refresh,retries=0,interval=.3,ai_settings=None if mode!='current' else settings.get('deepseek'),
            enrichment=True,product=True)
        target=result['target_date']
        if result['status']!='READY':
            raise ValueError('行情尚未齐备；保留上次研究，请检查数据缺口')
        if mode=='current':
            ai=result.get('ai',{})
            print('RESEARCH_RESULT:'+json.dumps({'target_date':target,'data':'新行情已保存',
                'evidence':'已更新可取得的资料','ai':ai.get('status'),
                'cached':ai.get('cached',False),'message':ai.get('validation_error') or ai.get('message','')},
                ensure_ascii=False),flush=True)
            if settings.get('deepseek') and ai.get('status') not in ('READY','BUDGET_EXCEEDED','NOT_CONFIGURED','DISABLED'):
                raise ValueError(ai.get('validation_error') or ai.get('message','新研究尚未完成，保留上一份合格解释'))
            return result
    print('RESEARCH_STAGE:正在更新海外行情与事件',flush=True)
    if mode!='replay':
        collector=Collector(root,SubprocessFetcher(root,30))
        collector.limit=36;collector.retries=0;collector.interval=.3
        collect_observations(collector,target,historical=mode!='current')
    else:
        print('复用已保存的同期证据；缺口保留，不把后来的消息倒填',flush=True)
    print('RESEARCH_STAGE:正在生成并核验研究结论',flush=True)
    result=refresh_review(root,target,historical=mode!='current')
    print('RESEARCH_STAGE:正在保存研究结果',flush=True)
    print('RESEARCH_RESULT:'+json.dumps({'target_date':target,'data':'已检查保存行情',
        'evidence':'复用同期证据' if mode=='replay' else '已更新可取得的资料',
        'ai':result.get('status'),'cached':result.get('cached',False),
        'message':result.get('validation_error') or result.get('message','')},ensure_ascii=False),flush=True)
    if result.get('status') in ('BUDGET_EXCEEDED','NOT_CONFIGURED','DISABLED'):
        print('资料处理结束，未生成新AI；保留原研究，资格以页面核对为准',flush=True)
        return result
    if result.get('status')!='READY':
        raise ValueError(result.get('validation_error') or result.get('message','研究尚未完成，保留上次结果'))
    print('已有研究可复用' if result.get('cached') else '研究已保存',flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=('current','supplement','replay'),required=True)
    parser.add_argument('--date',default='')
    parser.add_argument('--refresh-snapshots',action='store_true')
    args=parser.parse_args()
    if args.mode!='current':date.fromisoformat(args.date)
    with workspace_lock(Path(__file__).resolve().parent):
        update(Path(__file__).resolve().parent,args.mode,args.date,args.refresh_snapshots)
