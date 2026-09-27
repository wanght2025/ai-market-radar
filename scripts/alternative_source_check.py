#!/usr/bin/env python3
"""对 AKShare 所列不同上游做低量备选源核查。"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import platform
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

BEIJING = ZoneInfo("Asia/Shanghai")


def specs() -> list[dict]:
    end = datetime.now(BEIJING).date()
    start = end - timedelta(days=35)
    ymd = lambda value: value.strftime("%Y%m%d")
    return [
        {"id": "ths_industry_list", "source": "同花顺公开网页", "fn": "stock_board_industry_name_ths", "kwargs": {}},
        {"id": "ths_industry_members", "source": "同花顺公开网页", "fn": "stock_board_industry_info_ths", "kwargs": {"symbol": "半导体"}},
        {"id": "ths_industry_history", "source": "同花顺公开网页", "fn": "stock_board_industry_index_ths", "kwargs": {"symbol": "半导体", "start_date": ymd(start), "end_date": ymd(end)}},
        {"id": "ths_concept_list", "source": "同花顺公开网页", "fn": "stock_board_concept_name_ths", "kwargs": {}},
        {"id": "ths_concept_members", "source": "同花顺公开网页", "fn": "stock_board_concept_info_ths", "kwargs": {"symbol": "人工智能"}},
        {"id": "ths_concept_history", "source": "同花顺公开网页", "fn": "stock_board_concept_index_ths", "kwargs": {"symbol": "人工智能", "start_date": ymd(start), "end_date": ymd(end)}},
        {"id": "tencent_stock_daily", "source": "腾讯证券", "fn": "stock_zh_a_hist_tx", "kwargs": {"symbol": "sz000001", "start_date": ymd(start), "end_date": ymd(end), "adjust": "", "timeout": 10}},
        {"id": "sina_trade_calendar", "source": "新浪财经", "fn": "tool_trade_date_hist_sina", "kwargs": {}},
        {"id": "sina_index_daily", "source": "新浪财经", "fn": "stock_zh_index_daily", "kwargs": {"symbol": "sh000001"}},
        {"id": "cninfo_notice", "source": "巨潮资讯", "fn": "stock_zh_a_disclosure_report_cninfo", "kwargs": {"symbol": "000001", "market": "沪深京", "start_date": ymd(start), "end_date": ymd(end)}},
        {"id": "eastmoney_individual_notice", "source": "东方财富", "fn": "stock_individual_notice_report", "kwargs": {"security": "000001", "symbol": "全部", "begin_date": ymd(start), "end_date": ymd(end)}},
        {"id": "sina_us_daily", "source": "新浪财经", "fn": "stock_us_daily", "kwargs": {"symbol": ".DJI", "adjust": ""}},
        {"id": "cctv_events", "source": "央视网", "fn": "news_cctv", "kwargs": {"date": ymd(end - timedelta(days=1))}},
    ]


def summarize(frame: pd.DataFrame) -> dict:
    columns = [str(c) for c in frame.columns]
    date_col = next((c for c in columns if c.lower() in {"日期", "交易日", "date", "trade_date", "公告时间", "公告日期"}), None)
    answer = {"rows": len(frame), "columns": columns, "missing_by_column": {str(c): int(frame[c].isna().sum()) for c in frame.columns}}
    if date_col and not frame.empty:
        dates = frame[date_col].dropna().astype(str)
        answer.update({"date_column": date_col, "date_min": dates.min(), "date_max": dates.max()})
    identity = [c for c in frame.columns if str(c) in {"代码", "名称", "股票代码", "股票简称", "板块", "日期", "交易日", "date", "公告标题", "公告时间"}]
    answer["sample_identity"] = frame.loc[:, identity].head(2).astype(str).to_dict("records") if identity else []
    return answer


def worker(spec: dict, queue: mp.Queue) -> None:
    started = time.monotonic()
    try:
        frame = getattr(ak, spec["fn"])(**spec["kwargs"])
        queue.put({"status": "success", "elapsed_seconds": round(time.monotonic() - started, 3), **summarize(frame)})
    except Exception as exc:
        queue.put({"status": "failed", "elapsed_seconds": round(time.monotonic() - started, 3), "error_type": type(exc).__name__, "error": str(exc)[:800]})


def run(spec: dict, timeout: int) -> dict:
    queue: mp.Queue = mp.Queue(1)
    process = mp.Process(target=worker, args=(spec, queue))
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate(); process.join(2)
        result = {"status": "timeout", "elapsed_seconds": timeout, "error": f"exceeded {timeout}s"}
    elif queue.empty():
        result = {"status": "failed", "error": f"worker exit {process.exitcode} without result"}
    else:
        result = queue.get()
    return {**spec, **result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--output", type=Path, default=Path("artifacts/alternative_source_result.json"))
    args = parser.parse_args()
    started = datetime.now(BEIJING)
    results = []
    for spec in specs():
        print(spec["id"], flush=True)
        results.append(run(spec, args.timeout))
    output = {"purpose": "small-sample alternative-source verification; not an investment conclusion", "started_at_beijing": started.isoformat(), "finished_at_beijing": datetime.now(BEIJING).isoformat(), "environment": {"python": platform.python_version(), "akshare": ak.__version__}, "per_call_timeout_seconds": args.timeout, "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {args.output}")


if __name__ == "__main__":
    main()
