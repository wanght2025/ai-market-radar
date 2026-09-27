#!/usr/bin/env python3
"""对三个代表性东财 HTTP 目标各请求一次并保存脱敏诊断。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import akshare as ak
import requests


BEIJING = ZoneInfo("Asia/Shanghai")
SECRET_KEYS = {"ut", "token", "auth", "key", "cookie"}
PUBLIC_HEADERS = {"content-type", "content-length", "date", "server", "via", "x-cache", "retry-after"}


PROBES = [
    {
        "id": "industry_list_exact_akshare_target",
        "url": "https://17.push2.eastmoney.com/api/qt/clist/get",
        "params": {"pn": "1", "pz": "100", "po": "1", "np": "1", "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2", "fid": "f3", "fs": "m:90 t:2 f:!50", "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,f26,f22,f33,f11,f62,f128,f136,f115,f152,f124,f107,f104,f105,f140,f141,f207,f208,f209,f222"},
    },
    {
        "id": "sector_fund_mapping_exact_akshare_target",
        "url": "https://push2.eastmoney.com/api/qt/clist/get",
        "params": {"fid": "f62", "po": "1", "pz": "100", "pn": "1", "np": "1", "fltt": "2", "invt": "2", "ut": "8dec03ba335b81bf4ebdf7b29ec27d15", "fs": "m:90 t:2", "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124,f1,f13"},
    },
    {
        "id": "stock_daily_known_good_target",
        "url": "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "params": {"fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116", "ut": "7eea3edcaed734bea9cbfc24409ed989", "klt": "101", "fqt": "0", "secid": "0.000001", "beg": "20260901", "end": "20260927"},
    },
]


def redact(text: str) -> str:
    text = re.sub(r"(?i)(ut|token|auth|key|cookie)=([A-Za-z0-9._~-]+)", r"\1=REDACTED", text)
    return re.sub(r"\b[a-fA-F0-9]{24,}\b", "REDACTED", text)


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, "REDACTED" if key.lower() in SECRET_KEYS else value) for key, value in parse_qsl(parts.query)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def probe(spec: dict, timeout: float) -> dict:
    started = time.monotonic()
    try:
        response = requests.get(spec["url"], params=spec["params"], timeout=timeout)
        return {
            "id": spec["id"], "host": urlsplit(spec["url"]).hostname,
            "requested_url": safe_url(response.request.url), "http_status": response.status_code,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "public_response_headers": {k.lower(): redact(v) for k, v in response.headers.items() if k.lower() in PUBLIC_HEADERS},
            "body_prefix_200": redact(response.text[:200]),
        }
    except Exception as exc:
        return {"id": spec["id"], "host": urlsplit(spec["url"]).hostname, "requested_url": safe_url(requests.Request("GET", spec["url"], params=spec["params"]).prepare().url), "http_status": None, "elapsed_seconds": round(time.monotonic() - started, 3), "error_type": type(exc).__name__, "error": redact(str(exc)[:500])}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=12)
    parser.add_argument("--output", type=Path, default=Path("artifacts/eastmoney_http_diagnostic.json"))
    args = parser.parse_args()
    started = datetime.now(BEIJING)
    document = {
        "purpose": "single-request diagnostics; no bypass or alternate host",
        "started_at_beijing": started.isoformat(),
        "finished_at_beijing": None,
        "environment": {"python": platform.python_version(), "akshare": ak.__version__, "requests": requests.__version__, "proxy_environment_present": {name: bool(os.environ.get(name)) for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")}},
        "timeout_seconds": args.timeout,
        "probes": [probe(spec, args.timeout) for spec in PROBES],
    }
    document["finished_at_beijing"] = datetime.now(BEIJING).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {args.output}")


if __name__ == "__main__":
    main()
