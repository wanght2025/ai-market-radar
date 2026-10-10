"""Explicit public observation collection; no models or publication writes."""
import argparse
import json
from pathlib import Path

from collector import Collector, SubprocessFetcher
from research_context import collect_observations
from storage import workspace_lock


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--date',required=True)
    parser.add_argument('--max-tasks',type=int,default=24)
    args=parser.parse_args()
    root=Path(__file__).resolve().parent
    if not (root/'data/publications'/f'{args.date}.json').is_file():raise ValueError('需要正式报告的行情日期')
    with workspace_lock(root):
        print('RESEARCH_STAGE:正在更新海外行情与事件',flush=True)
        collector=Collector(root,SubprocessFetcher(root,30))
        collector.limit=args.max_tasks;collector.retries=0;collector.interval=.3
        result=collect_observations(collector,args.date)
        print('RESEARCH_STAGE:正在保存研究结果',flush=True)
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':main()
