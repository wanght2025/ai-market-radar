"""Local Windows entry point. GitHub synchronization is a separate manual step."""
import argparse
import json
import sys
from pathlib import Path
from market_radar import Radar
from collector import SubprocessFetcher
from storage import read_json, workspace_lock
from version import PRODUCT_VERSION

ROOT = Path(__file__).resolve().parent

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=f'AI 市场雷达 {PRODUCT_VERSION} 本地运行')
    parser.add_argument('--date', help='指定 YYYY-MM-DD，仍须通过指数交易日校验')
    parser.add_argument('--refresh-snapshots', action='store_true')
    parser.add_argument('--max-tasks', type=int, default=180)
    parser.add_argument('--timeout', type=int, default=150)
    parser.add_argument('--retries', type=int, default=1)
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--freeze-forward', metavar='YYYY-MM-DD', help='查看已经自动保存的合格前瞻；不采集、不调用模型')
    parser.add_argument('--snapshot-id', help='查看指定研究对应的前瞻；省略时查看最新合格版本')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('timeout 必须大于零')
    try:
        with workspace_lock(ROOT):
            if args.snapshot_id and not args.freeze_forward:
                raise ValueError('snapshot-id 仅用于显式冻结')
            if args.freeze_forward:
                from predictions import freeze_forward
                record=freeze_forward(ROOT,args.freeze_forward,snapshot_id=args.snapshot_id)
                print(json.dumps({k:record[k] for k in ('id','record_type','prediction_date','source_snapshot_id',
                    'market_baseline_date','issued_at','external_information_cutoff','frozen_at')},ensure_ascii=False,indent=2))
                return 0
            config = read_json(ROOT/'config.json')
            result = Radar(ROOT, SubprocessFetcher(ROOT,args.timeout)).run(
                args.date, config.get('watchlist',[]), refresh=args.refresh_snapshots,
                max_tasks=args.max_tasks, retries=args.retries, interval=args.interval,
                ai_settings=config.get('deepseek'), enrichment=True, product=True)
    except (BlockingIOError, ValueError) as exc:
        print(str(exc))
        return 1
    print(json.dumps({k:result[k] for k in ('target_date','mode','status','requests_this_run',
                     'attempts_this_run','readiness')},ensure_ascii=False,indent=2))
    print(f"状态页：{ROOT/'reports'/'index.html'}")
    print(f"核心报告：{result.get('report','本次未生成，请查看缺失项；以前报告保留')}")
    print(f"AI 解读：{result['ai'].get('message', result['ai']['status'])}")
    if result.get('product',{}).get('prediction_id'):
        print(f"研究已保存：{result['product']['prediction_id']}；合格盘后与盘前结果自动留作前瞻，其他结果仅供研究")
    return 0 if result['status']=='READY' and result['ai'].get('status') in (
        'READY','DISABLED','NOT_CONFIGURED','BUDGET_EXCEEDED') else 1

if __name__ == '__main__':
    raise SystemExit(main())
