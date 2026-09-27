#!/usr/bin/env python3
"""2026-09-24 目标日低量复测；严格记录请求并在关键列表失败时停止下游。"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
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
TARGET_DATE = "20260924"
PUBLIC_HEADERS = {"content-type", "content-length", "date", "server", "via", "x-cache", "retry-after"}

CORE_TASKS = [
    {"id": "stock_daily_000001", "fn": "stock_zh_a_hist", "kwargs": {"symbol": "000001", "start_date": "20260601", "end_date": TARGET_DATE, "adjust": "", "timeout": 10}, "host": "push2his.eastmoney.com", "budget": 1},
    {"id": "index_sh000001", "fn": "stock_zh_index_daily_em", "kwargs": {"symbol": "sh000001", "start_date": "20260601", "end_date": TARGET_DATE}, "host": "push2his.eastmoney.com", "budget": 1},
    {"id": "industry_list", "fn": "stock_board_industry_name_em", "kwargs": {}, "host": ["17.push2.eastmoney.com", "push2.eastmoney.com"], "budget": 2, "key_list": True},
    {"id": "concept_list", "fn": "stock_board_concept_name_em", "kwargs": {}, "host": ["17.push2.eastmoney.com", "push2.eastmoney.com"], "budget": 4, "key_list": True},
    {"id": "industry_fund_home", "fn": "stock_fund_flow_industry", "kwargs": {"symbol": "即时"}, "host": "data.10jqka.com.cn", "budget": 2, "partial_ok": True},
    {"id": "concept_fund_home", "fn": "stock_fund_flow_concept", "kwargs": {"symbol": "即时"}, "host": "data.10jqka.com.cn", "budget": 2, "partial_ok": True},
    {"id": "individual_fund_000001", "fn": "stock_individual_fund_flow", "kwargs": {"stock": "000001", "market": "sz"}, "host": "push2his.eastmoney.com", "budget": 1},
    {"id": "notice_risk_20260924", "fn": "stock_notice_report", "kwargs": {"symbol": "风险提示", "date": TARGET_DATE}, "host": "np-anotice-stock.eastmoney.com", "budget": 2},
    {"id": "news_000001", "fn": "stock_news_em", "kwargs": {"symbol": "000001"}, "host": "search-api-web.eastmoney.com", "budget": 1},
]

DOWNSTREAM_TASKS = [
    {"id": "industry_bank_members", "fn": "stock_board_industry_cons_em", "kwargs": {"symbol": "银行"}, "host": "17.push2.eastmoney.com", "budget": 2},
    {"id": "concept_ai_members", "fn": "stock_board_concept_cons_em", "kwargs": {"symbol": "人工智能"}, "host": "17.push2.eastmoney.com", "budget": 2},
    {"id": "industry_bank_history", "fn": "stock_board_industry_hist_em", "kwargs": {"symbol": "银行", "start_date": "20260601", "end_date": TARGET_DATE, "period": "日k", "adjust": ""}, "host": "17.push2.eastmoney.com", "budget": 3},
    {"id": "concept_ai_history", "fn": "stock_board_concept_hist_em", "kwargs": {"symbol": "人工智能", "start_date": "20260601", "end_date": TARGET_DATE, "period": "daily", "adjust": ""}, "host": "17.push2.eastmoney.com", "budget": 3},
]


class BudgetExceeded(RuntimeError):
    pass


def summarize(frame: pd.DataFrame) -> dict:
    columns = [str(c) for c in frame.columns]
    date_column = next((c for c in columns if c in {"日期", "公告日期", "发布时间", "date"}), None)
    result = {"rows": len(frame), "columns": columns, "missing": {str(c): int(frame[c].isna().sum()) for c in frame.columns}}
    if date_column and not frame.empty:
        dates = frame[date_column].dropna().astype(str)
        result.update({"date_column": date_column, "date_min": dates.min(), "date_max": dates.max(), "contains_target_date": any(value.startswith("2026-09-24") for value in dates)})
    ids = [c for c in frame.columns if str(c) in {"板块代码", "板块名称", "行业", "代码", "名称", "股票代码", "股票简称", "公告标题", "新闻标题"}]
    result["sample_identity"] = frame.loc[:, ids].head(3).astype(str).to_dict("records") if ids else []
    return result


def first_table(text: str) -> dict:
    try:
        return summarize(pd.read_html(StringIO(text))[0].head(20))
    except Exception as exc:
        return {"parse_error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def worker(task: dict, timeout: float, queue: mp.Queue) -> None:
    original_get = requests.get
    original_session_request = requests.sessions.Session.request
    function_request_module = getattr(ak, task["fn"]).__globals__.get("requests")
    original_function_get = getattr(function_request_module, "get", None)
    calls, first_response = [], None
    inside_direct_get = False

    def allowed_hosts() -> set[str]:
        return {task["host"]} if isinstance(task["host"], str) else set(task["host"])

    def validate(host: str) -> None:
        if host not in allowed_hosts():
            raise PermissionError(f"host {host!r} outside allowlist")
        if len(calls) >= task["budget"]:
            raise BudgetExceeded(f"GET budget {task['budget']} exhausted")

    def record(response, host: str, started: float) -> None:
        nonlocal first_response
        calls.append({"method": "GET", "host": host, "path": urlsplit(response.url).path, "status": response.status_code, "elapsed_seconds": round(time.monotonic() - started, 3), "headers": {k.lower(): v for k, v in response.headers.items() if k.lower() in PUBLIC_HEADERS}})
        first_response = first_response or response.text

    def guarded_get(url, *args, **kwargs):
        nonlocal inside_direct_get
        host = urlsplit(url).hostname
        validate(host)
        kwargs.setdefault("timeout", timeout)
        kwargs.setdefault("allow_redirects", False)
        started = time.monotonic()
        inside_direct_get = True
        try:
            response = original_function_get(url, *args, **kwargs) if function_request_module is not requests else original_get(url, *args, **kwargs)
        finally:
            inside_direct_get = False
        record(response, host, started)
        return response

    def guarded_session_request(session, method, url, *args, **kwargs):
        if inside_direct_get:
            return original_session_request(session, method, url, *args, **kwargs)
        if method.upper() != "GET":
            raise PermissionError(f"method {method!r} outside allowlist")
        host = urlsplit(url).hostname
        validate(host)
        kwargs.setdefault("timeout", timeout)
        kwargs.setdefault("allow_redirects", False)
        started = time.monotonic()
        response = original_session_request(session, method, url, *args, **kwargs)
        record(response, host, started)
        return response

    requests.get = guarded_get
    requests.sessions.Session.request = guarded_session_request
    if function_request_module is not None and function_request_module is not requests:
        function_request_module.get = guarded_get
    started = time.monotonic()
    try:
        frame = getattr(ak, task["fn"])(**task["kwargs"])
        outcome = {"status": "success", **summarize(frame)}
    except BudgetExceeded as exc:
        outcome = {"status": "partial_request_budget", "error": str(exc), "first_page": first_table(first_response or "")}
    except Exception as exc:
        outcome = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)[:800]}
    finally:
        requests.get = original_get
        requests.sessions.Session.request = original_session_request
        if function_request_module is not None and function_request_module is not requests:
            function_request_module.get = original_function_get
    queue.put({**task, **outcome, "elapsed_seconds": round(time.monotonic() - started, 3), "http_calls": calls})


def run(task: dict, timeout: float) -> dict:
    queue: mp.Queue = mp.Queue(1)
    process = mp.Process(target=worker, args=(task, timeout, queue))
    process.start(); process.join(timeout * task["budget"] + 3)
    if process.is_alive():
        process.terminate(); process.join(2)
        return {**task, "status": "process_timeout", "http_calls": [], "error": "process timeout"}
    return queue.get() if not queue.empty() else {**task, "status": "failed", "http_calls": [], "error": f"worker exit {process.exitcode}"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--output", type=Path, default=Path("artifacts/target_date_recheck_20260924.json"))
    args = parser.parse_args()
    started = datetime.now(BEIJING)
    results = [run(task, args.timeout) for task in CORE_TASKS]
    list_results = [item for item in results if item.get("key_list")]
    key_lists_complete = len(list_results) == 2 and all(item["status"] == "success" and item.get("rows", 0) > 0 for item in list_results)
    if key_lists_complete:
        results.extend(run(task, args.timeout) for task in DOWNSTREAM_TASKS)
        coverage_inventory = {"status": "not_run_in_sample_script", "reason": "requires a separately bounded full-coverage run after representative linkage passes"}
    else:
        results.extend({**task, "status": "skipped_dependency", "reason": "industry/concept key lists were not both successful; downstream scan stopped"} for task in DOWNSTREAM_TASKS)
        coverage_inventory = {"status": "not_verified", "reason": "key lists failed; stock range, list counts, pagination totals, full members and histories were not scanned or estimated from invented data"}
    output = {"purpose": "target-date low-volume recheck; not an investment conclusion", "target_trade_date": "2026-09-24", "started_at_beijing": started.isoformat(), "finished_at_beijing": datetime.now(BEIJING).isoformat(), "environment": {"akshare": ak.__version__}, "constraints": {"one_function_invocation_per_task": True, "timeout_seconds": args.timeout, "no_retry": True, "no_redirect": True}, "key_lists_complete": key_lists_complete, "coverage_inventory": coverage_inventory, "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {args.output}; key_lists_complete={key_lists_complete}")


if __name__ == "__main__":
    main()
