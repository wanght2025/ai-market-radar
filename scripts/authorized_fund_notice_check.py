#!/usr/bin/env python3
"""核查已获准的同花顺资金排行，并定向复核最近交易日公告。"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import platform
import re
import time
from datetime import datetime
from io import StringIO
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import requests

BEIJING = ZoneInfo("Asia/Shanghai")
PUBLIC_HEADERS = {"content-type", "content-length", "date", "server", "via", "x-cache", "retry-after"}
FUND_HOST = "data.10jqka.com.cn"
NOTICE_HOST = "np-anotice-stock.eastmoney.com"

TASKS = [
    {"id": "ths_industry_fund", "fn": "stock_fund_flow_industry", "kwargs": {"symbol": "即时"}, "allowed_host": FUND_HOST, "request_budget": 2},
    {"id": "ths_concept_fund", "fn": "stock_fund_flow_concept", "kwargs": {"symbol": "即时"}, "allowed_host": FUND_HOST, "request_budget": 2},
    {"id": "ths_individual_fund", "fn": "stock_fund_flow_individual", "kwargs": {"symbol": "即时"}, "allowed_host": FUND_HOST, "request_budget": 2},
    {"id": "eastmoney_notice_risk_20260924", "fn": "stock_notice_report", "kwargs": {"symbol": "风险提示", "date": "20260924"}, "allowed_host": NOTICE_HOST, "request_budget": 3},
]


class RequestBudgetExceeded(RuntimeError):
    pass


def money_suffix_examples(frame: pd.DataFrame) -> dict:
    result = {}
    for column in frame.columns:
        if any(word in str(column) for word in ("资金", "净额", "成交额")):
            values = frame[column].dropna().astype(str).head(3).tolist()
            suffixes = sorted({match.group(1) for value in values if (match := re.search(r"(万|亿|元|%)$", value.strip()))})
            result[str(column)] = {"examples": values, "embedded_suffixes": suffixes}
    return result


def summarize(frame: pd.DataFrame) -> dict:
    columns = [str(column) for column in frame.columns]
    identity = [column for column in frame.columns if str(column) in {"行业", "概念", "股票代码", "股票简称", "代码", "名称", "公告标题", "公告日期"}]
    date_column = next((column for column in frame.columns if str(column) in {"日期", "公告日期"}), None)
    result = {
        "rows": len(frame), "columns": columns,
        "missing_by_column": {str(column): int(frame[column].isna().sum()) for column in frame.columns},
        "sample_identity": frame.loc[:, identity].head(3).astype(str).to_dict("records") if identity else [],
        "units_from_column_labels": {str(column): match.group(1) for column in frame.columns if (match := re.search(r"\(([^)]+)\)$", str(column)))},
        "unit_evidence_from_values": money_suffix_examples(frame),
    }
    if date_column is not None and not frame.empty:
        dates = frame[date_column].dropna().astype(str)
        result.update({"date_column": str(date_column), "date_min": dates.min(), "date_max": dates.max()})
    return result


def raw_table_summary(response_text: str) -> dict:
    try:
        tables = pd.read_html(StringIO(response_text))
    except Exception as exc:
        return {"parse_status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if not tables:
        return {"parse_status": "no_table"}
    return {"parse_status": "success", **summarize(tables[0].head(20))}


def worker(task: dict, timeout: float, queue: mp.Queue) -> None:
    original_get = requests.get
    calls = []
    first_text = None

    def guarded_get(url, *args, **kwargs):
        nonlocal first_text
        host = urlsplit(url).hostname
        if host != task["allowed_host"]:
            raise PermissionError(f"host {host!r} is outside this task's allowlist")
        if len(calls) >= task["request_budget"]:
            raise RequestBudgetExceeded(f"underlying GET budget {task['request_budget']} exhausted")
        kwargs.setdefault("timeout", timeout)
        kwargs.setdefault("allow_redirects", False)
        started = time.monotonic()
        response = original_get(url, *args, **kwargs)
        calls.append({
            "method": "GET", "host": host, "url_path": urlsplit(response.url).path,
            "http_status": response.status_code,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "public_response_headers": {key.lower(): value for key, value in response.headers.items() if key.lower() in PUBLIC_HEADERS},
        })
        if first_text is None:
            first_text = response.text
        return response

    requests.get = guarded_get
    started = time.monotonic()
    try:
        frame = getattr(ak, task["fn"])(**task["kwargs"])
        outcome = {"status": "success", **summarize(frame)}
    except RequestBudgetExceeded as exc:
        outcome = {"status": "partial_request_budget", "error_type": type(exc).__name__, "error": str(exc), "first_page_table": raw_table_summary(first_text or "")}
    except Exception as exc:
        outcome = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)[:500], "first_page_table": raw_table_summary(first_text or "")}
    finally:
        requests.get = original_get
    queue.put({**task, **outcome, "elapsed_seconds": round(time.monotonic() - started, 3), "http_calls": calls})


def run(task: dict, timeout: float) -> dict:
    queue: mp.Queue = mp.Queue(1)
    process = mp.Process(target=worker, args=(task, timeout, queue))
    process.start()
    process.join(timeout * task["request_budget"] + 3)
    if process.is_alive():
        process.terminate(); process.join(2)
        return {**task, "status": "process_timeout", "error": "process-level timeout"}
    return queue.get() if not queue.empty() else {**task, "status": "failed", "error": f"worker exited {process.exitcode} without result"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--output", type=Path, default=Path("artifacts/authorized_fund_notice_result.json"))
    parser.add_argument("--only", action="append", default=[], help="只运行指定任务 id；可重复")
    args = parser.parse_args()
    started = datetime.now(BEIJING)
    tasks = TASKS
    if args.only:
        tasks = [task for task in tasks if task["id"] in set(args.only)]
        unknown = set(args.only) - {task["id"] for task in tasks}
        if unknown:
            parser.error(f"unknown task ids: {', '.join(sorted(unknown))}")
    document = {
        "purpose": "allowlist-constrained one-invocation fund and notice verification; not an investment conclusion",
        "started_at_beijing": started.isoformat(), "finished_at_beijing": None,
        "environment": {"python": platform.python_version(), "akshare": ak.__version__, "requests": requests.__version__},
        "authorization_confirmation": {
            FUND_HOST: {"methods": ["GET", "HEAD", "OPTIONS"], "newly_allowed_by_user": True, "methods_used": ["GET"]},
            NOTICE_HOST: {"previously_exercised_eastmoney_path": True, "methods_used": ["GET"]},
        },
        "constraints": {"one_akshare_invocation_per_task": True, "per_request_timeout_seconds": args.timeout, "no_retry": True, "no_unlisted_host": True},
        "results": [run(task, args.timeout) for task in tasks],
    }
    document["finished_at_beijing"] = datetime.now(BEIJING).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {args.output}")


if __name__ == "__main__":
    main()
