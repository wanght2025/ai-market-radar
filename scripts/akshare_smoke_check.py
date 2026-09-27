#!/usr/bin/env python3
"""AKShare 免费接口小样本核查；不用于生成投资结论。

每个接口在独立子进程中执行，父进程在超时后终止它。默认仅输出字段、
行数、首尾交易日期和极少量样本标识，不保存完整行情。
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import platform
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd


BEIJING = ZoneInfo("Asia/Shanghai")
DEFAULT_TIMEOUT = 20
SAMPLE_ROWS = 3


def date_window(days: int = 120) -> tuple[str, str]:
    today = datetime.now(BEIJING).date()
    return (today - timedelta(days=days)).strftime("%Y%m%d"), today.strftime("%Y%m%d")


def task_specs() -> list[dict]:
    start, end = date_window()
    return [
        {"id": "industry_list", "source": "东方财富", "fn": "stock_board_industry_name_em", "kwargs": {}},
        {"id": "concept_list", "source": "东方财富", "fn": "stock_board_concept_name_em", "kwargs": {}},
        {"id": "industry_bank_members", "source": "东方财富", "fn": "stock_board_industry_cons_em", "kwargs": {"symbol": "银行"}},
        {"id": "industry_auto_members", "source": "东方财富", "fn": "stock_board_industry_cons_em", "kwargs": {"symbol": "汽车整车"}},
        {"id": "concept_ai_members", "source": "东方财富", "fn": "stock_board_concept_cons_em", "kwargs": {"symbol": "人工智能"}},
        {"id": "concept_robot_members", "source": "东方财富", "fn": "stock_board_concept_cons_em", "kwargs": {"symbol": "机器人概念"}},
        {"id": "industry_bank_history", "source": "东方财富", "fn": "stock_board_industry_hist_em", "kwargs": {"symbol": "银行", "start_date": start, "end_date": end, "period": "日k", "adjust": ""}},
        {"id": "industry_auto_history", "source": "东方财富", "fn": "stock_board_industry_hist_em", "kwargs": {"symbol": "汽车整车", "start_date": start, "end_date": end, "period": "日k", "adjust": ""}},
        {"id": "concept_ai_history", "source": "东方财富", "fn": "stock_board_concept_hist_em", "kwargs": {"symbol": "人工智能", "start_date": start, "end_date": end, "period": "daily", "adjust": ""}},
        {"id": "concept_robot_history", "source": "东方财富", "fn": "stock_board_concept_hist_em", "kwargs": {"symbol": "机器人概念", "start_date": start, "end_date": end, "period": "daily", "adjust": ""}},
        {"id": "stock_000001_history", "source": "东方财富", "fn": "stock_zh_a_hist", "kwargs": {"symbol": "000001", "start_date": start, "end_date": end, "adjust": "", "timeout": 10}},
        {"id": "stock_600000_history", "source": "东方财富", "fn": "stock_zh_a_hist", "kwargs": {"symbol": "600000", "start_date": start, "end_date": end, "adjust": "", "timeout": 10}},
        {"id": "risk_sample_000004_info", "source": "东方财富", "fn": "stock_individual_info_em", "kwargs": {"symbol": "000004", "timeout": 10}},
        {"id": "industry_fund_rank_today", "source": "东方财富", "fn": "stock_sector_fund_flow_rank", "kwargs": {"indicator": "今日", "sector_type": "行业资金流"}},
        {"id": "industry_bank_fund_history", "source": "东方财富", "fn": "stock_sector_fund_flow_hist", "kwargs": {"symbol": "银行"}},
        {"id": "stock_000001_fund", "source": "东方财富", "fn": "stock_individual_fund_flow", "kwargs": {"stock": "000001", "market": "sz"}},
        {"id": "index_sh000001", "source": "东方财富", "fn": "stock_zh_index_daily_em", "kwargs": {"symbol": "sh000001", "start_date": start, "end_date": end}},
        {"id": "notice_all_on_end_date", "source": "东方财富", "fn": "stock_notice_report", "kwargs": {"symbol": "全部", "date": end}},
        {"id": "news_000001", "source": "东方财富", "fn": "stock_news_em", "kwargs": {"symbol": "000001"}},
        {"id": "overseas_dow_jones", "source": "东方财富", "fn": "stock_us_hist", "kwargs": {"symbol": "100.DJIA", "start_date": start, "end_date": end, "adjust": ""}},
    ]


def summarize(frame: pd.DataFrame) -> dict:
    columns = [str(value) for value in frame.columns]
    date_column = next((c for c in columns if c in {"日期", "交易日", "发布时间", "datetime", "date"}), None)
    result = {"rows": len(frame), "columns": columns, "missing_by_column": {str(c): int(frame[c].isna().sum()) for c in frame.columns}}
    if date_column and not frame.empty:
        values = frame[date_column].dropna().astype(str)
        result["date_column"] = date_column
        result["date_min"] = values.min() if not values.empty else None
        result["date_max"] = values.max() if not values.empty else None
    safe_columns = [c for c in frame.columns if str(c) in {"代码", "名称", "股票代码", "股票简称", "日期", "交易日", "标题", "新闻标题", "发布时间", "item", "value"}]
    result["sample"] = frame.loc[:, safe_columns].head(SAMPLE_ROWS).astype(str).to_dict("records") if safe_columns else []
    return result


def worker(spec: dict, queue: mp.Queue) -> None:
    started = time.monotonic()
    try:
        value = getattr(ak, spec["fn"])(**spec["kwargs"])
        if not isinstance(value, pd.DataFrame):
            raise TypeError(f"expected DataFrame, got {type(value).__name__}")
        queue.put({"status": "success", "elapsed_seconds": round(time.monotonic() - started, 3), **summarize(value)})
    except Exception as exc:  # 接口异常本身就是本轮需要记录的结果
        queue.put({"status": "failed", "elapsed_seconds": round(time.monotonic() - started, 3), "error_type": type(exc).__name__, "error": str(exc)[:1000]})


def run_task(spec: dict, timeout: int) -> dict:
    queue: mp.Queue = mp.Queue(maxsize=1)
    process = mp.Process(target=worker, args=(spec, queue))
    started = time.monotonic()
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(2)
        outcome = {"status": "timeout", "elapsed_seconds": round(time.monotonic() - started, 3), "error": f"exceeded {timeout}s process timeout"}
    elif queue.empty():
        outcome = {"status": "failed", "elapsed_seconds": round(time.monotonic() - started, 3), "error": f"worker exited with code {process.exitcode} without a result"}
    else:
        outcome = queue.get()
    return {**spec, **outcome}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="每个接口的进程级超时秒数")
    parser.add_argument("--output", type=Path, default=Path("artifacts/akshare_smoke_result.json"))
    parser.add_argument("--only", action="append", default=[], help="只执行指定任务 id；可重复传入")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")

    started_at = datetime.now(BEIJING)
    specs = task_specs()
    if args.only:
        specs = [spec for spec in specs if spec["id"] in set(args.only)]
        missing = sorted(set(args.only) - {spec["id"] for spec in specs})
        if missing:
            parser.error(f"unknown task id: {', '.join(missing)}")
    results = []
    for spec in specs:
        print(f"[{len(results) + 1:02d}] {spec['id']}", flush=True)
        result = run_task(spec, args.timeout)
        results.append(result)
        print(f"     {result['status']} ({result['elapsed_seconds']}s)", flush=True)
    document = {
        "purpose": "free API small-sample verification; not an investment conclusion",
        "started_at_beijing": started_at.isoformat(),
        "finished_at_beijing": datetime.now(BEIJING).isoformat(),
        "environment": {"python": platform.python_version(), "akshare": ak.__version__, "platform": platform.platform()},
        "per_call_timeout_seconds": args.timeout,
        "sample_rows_saved_per_call": SAMPLE_ROWS,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    succeeded = sum(item["status"] == "success" for item in results)
    print(f"saved {args.output}; success={succeeded}/{len(results)}")
    return 0 if succeeded else 2


if __name__ == "__main__":
    sys.exit(main())
