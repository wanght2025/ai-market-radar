"""Bounded, resumable collection and validation of market data snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from storage import read_json, write_json


CST = timezone(timedelta(hours=8))
SCHEMA = 1
REQUIRED_DAILY_COLUMNS = {
    "index": ("date", "close"),
    "stock_daily": ("日期", "收盘"),
    "stock_flow": ("日期", "主力净流入-净额"),
    "market": ("日期", "代码", "收盘", "涨跌幅", "成交额"),
    "market_batch": ("日期", "代码", "收盘", "涨跌幅", "成交额"),
    "market_history": ("日期", "代码", "收盘", "涨跌幅", "成交额"),
    "board_flow": ("日期", "主力净流入-净额"),
    "board_flows": ("日期", "主力净流入-净额"),
    "news": ("date",),
    "notices": ("date",),
    "news_feed": ("date",),
    "policy": ("date",),
}


def now_cst() -> datetime:
    return datetime.now(CST)


def clean_code(value: object) -> str:
    code = str(value).strip()
    if code.endswith(".0"):
        code = code[:-2]
    if not code.isdigit() or len(code) > 6:
        raise ValueError(f"无效股票代码：{code}")
    return code.zfill(6)


def stock_market(code: str) -> str:
    if code.startswith(("4", "8", "92")):
        return "bj"
    if code.startswith(("5", "6", "9")):
        return "sh"
    return "sz"


def list_rows(rows: object, code_field: str, name_field: str, stock: bool = False) -> list[dict]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("接口没有返回任何记录")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or code_field not in row or name_field not in row:
            raise ValueError(f"缺少 {code_field}/{name_field} 字段")
        code = clean_code(row[code_field]) if stock else str(row[code_field]).strip()
        name = str(row[name_field]).strip()
        if not code or not name or name.lower() in ("nan", "none") or code in seen:
            raise ValueError("列表含空值或重复代码")
        seen.add(code)
        result.append({"code": code, "name": name})
    return result


def latest_date(rows: list[dict], field: str) -> str:
    if not rows or any(field not in row or not row[field] for row in rows):
        raise ValueError(f"缺少交易日期字段 {field}")
    values = []
    for row in rows:
        value = str(row[field])[:10]
        values.append(date.fromisoformat(value).isoformat())
    return max(values)


def membership_signature(data: Path, manifest: dict) -> str | None:
    """Compare actual classification contents, including old manifests."""
    if not manifest:
        return None
    base = data/'snapshots'/'industry-sw'/manifest['generation']/'members'
    members = {}
    for board in manifest.get('boards', []):
        saved = read_json(base/f"{board['code']}.json")
        if not saved:
            return None
        members[board['code']] = sorted(r['code'] for r in saved['rows'])
    if not members:
        return None
    return hashlib.sha256(json.dumps(members, sort_keys=True).encode()).hexdigest()


class SubprocessFetcher:
    def __init__(self, root: Path, timeout: int = 60):
        self.root = root
        self.timeout = timeout

    def __call__(self, kind: str, params: dict) -> list[dict]:
        with tempfile.TemporaryDirectory(prefix="radar_", dir=self.root / "data", ignore_cleanup_errors=True) as work:
            output = Path(work) / "response.json"
            command = [sys.executable, str(self.root / "akshare_worker.py"), kind,
                       json.dumps(params, ensure_ascii=False), str(output)]
            try:
                result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                        timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"接口超过 {self.timeout} 秒仍未完成") from exc
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout).strip()[-500:] or "子进程失败")
            if result.stderr.strip():
                print(result.stderr.strip()[-2000:],file=sys.stderr)
            rows = read_json(output)
            if not isinstance(rows, list):
                raise ValueError("接口返回格式不是记录列表")
            return rows


class Collector:
    def __init__(self, root: Path, fetcher, clock=now_cst):
        self.root = root
        self.data = root / "data"
        self.reports = root / "reports"
        self.fetcher = fetcher
        self.clock = clock
        self.checkpoint_path = self.data / "checkpoint.json"
        self.checkpoint = read_json(self.checkpoint_path, {"schema": SCHEMA, "tasks": {}})
        if self.checkpoint.get("schema") != SCHEMA:
            raise RuntimeError("checkpoint 版本不匹配，请先备份 data 目录")
        self.calls = 0
        self.limit = 0
        self.messages = []
        self.attempts = 0
        self.retries = 1
        self.interval = 1.0

    def _save_task(self, key: str, status: str, artifact: Path | None = None,
                   error: str = "", observed_date: str = "") -> None:
        entry = {"status": status, "updated_at": self.clock().isoformat(),
                 "error": error, "observed_date": observed_date,
                 "attempt_rounds": self.checkpoint['tasks'].get(key, {}).get('attempt_rounds', 0)}
        if artifact and artifact.exists():
            entry["artifact"] = str(artifact.relative_to(self.root)).replace("\\", "/")
        self.checkpoint["tasks"][key] = entry
        write_json(self.checkpoint_path, self.checkpoint)

    def _already_succeeded(self, key: str, artifact: Path) -> bool:
        item = self.checkpoint["tasks"].get(key, {})
        return item.get("status") == "SUCCESS" and artifact.exists()

    def _fetch(self, key: str, kind: str, params: dict, artifact: Path,
               validator, *, reuse: bool = True, cache_only: bool = False, prefetched_rows=None) -> bool:
        if reuse and self._already_succeeded(key, artifact):
            try:
                saved = read_json(artifact)
                validator([saved] if kind == 'concept_members' and params.get('scope') == 'current-a-v2'
                          else saved["rows"])
                field = REQUIRED_DAILY_COLUMNS.get(kind, (None,))[0]
                dated = not params.get('target_date') or any(
                    str(r.get(field, ''))[:10] == params['target_date'] for r in saved['rows'])
                if saved.get("params") == params and dated:
                    return True
            except (ValueError, KeyError, TypeError):
                pass
        if cache_only:
            return False
        if self.calls >= self.limit:
            self.messages.append(f"{key}：等待下次运行（本次请求预算已用完）")
            if key not in self.checkpoint["tasks"]:
                self.checkpoint['tasks'][key] = {'status':'PENDING', 'error':'等待下次运行',
                                                 'updated_at':self.clock().isoformat()}
            return False
        self.calls += 1
        task = self.checkpoint['tasks'].setdefault(key, {})
        task['attempt_rounds'] = task.get('attempt_rounds', 0) + 1
        try:
            print(f"[{self.calls}/{self.limit}] {kind} {params.get('symbol', params.get('code', ''))}", flush=True)
            for attempt in range(self.retries + 1):
                self.attempts += 1
                try:
                    rows = self.fetcher(kind, params) if prefetched_rows is None else prefetched_rows
                    break
                except Exception:
                    if attempt == self.retries:
                        raise
                    time.sleep(self.interval * (attempt + 1))
            if self.interval:
                time.sleep(self.interval)
            validated = validator(rows)
            normalized, observed_date = validated[:2]
            metadata = validated[2] if len(validated) == 3 else {}
            write_json(artifact, {"kind": kind, "params": params,
                                  "fetched_at": self.clock().isoformat(),
                                  "observed_date": observed_date,
                                  "rows": normalized, **metadata})
            target = params.get("target_date")
            date_field = REQUIRED_DAILY_COLUMNS.get(kind, (None,))[0]
            has_target = not target or (observed_date == target if date_field is None else
                                        any(str(row[date_field])[:10] == target for row in normalized))
            if target and not has_target:
                self._save_task(key, "PARTIAL", artifact,
                                f"目标交易日 {target}，实际最新交易日 {observed_date}", observed_date)
                return False
            self._save_task(key, "SUCCESS", artifact, observed_date=observed_date)
            return True
        except Exception as exc:
            self._save_task(key, "FAILED", error=f"{type(exc).__name__}: {exc}")
            return False

    @staticmethod
    def _catalog_validator(code_field: str, name_field: str, stock: bool = False):
        def validate(rows):
            normalized = isinstance(rows,list) and rows and isinstance(rows[0],dict) and 'code' in rows[0]
            return list_rows(rows,'code' if normalized else code_field,
                             'name' if normalized else name_field,stock), ''
        return validate

    @staticmethod
    def _daily_validator(kind: str):
        fields = REQUIRED_DAILY_COLUMNS[kind]
        day_field = fields[0]

        def validate(rows):
            if not isinstance(rows, list) or not rows:
                raise ValueError("接口没有返回行情或资金记录")
            if any(not isinstance(row, dict) or any(field not in row or row[field] is None
                                                     for field in fields)
                   for row in rows):
                raise ValueError(f"缺少必需字段：{', '.join(fields)}")
            if kind in ('index', 'stock_daily', 'stock_flow'):
                numeric = fields[1]
                if any(not math.isfinite(float(row[numeric])) for row in rows):
                    raise ValueError(f'{numeric} 包含非有效数值')
            return rows, latest_date(rows, day_field)

        return validate

    def _universe(self, refresh: bool) -> dict | None:
        latest = self.data / "snapshots" / "exchange-universe.json"
        if latest.exists() and not refresh:
            return read_json(latest)
        candidate = self.data / "snapshots" / "universe-candidate.json"
        key = "snapshot:universe:refresh" if refresh else "snapshot:universe:initial"
        if self._fetch(key, "universe", {}, candidate,
                       self._catalog_validator("代码", "名称", True), reuse=False):
            os.replace(candidate, latest)
            self._save_task(key, "SUCCESS", latest)
            return read_json(latest)
        return None if getattr(self,'product',False) else read_json(latest)

    def _board_snapshot(self, kind: str, refresh: bool) -> dict | None:
        base = self.data / "snapshots" / ("industry-sw" if kind == "industry" else kind)
        latest_file = base / "latest.json"
        active_file = base / "active.json"
        latest = read_json(latest_file)
        pool = read_json(self.data/'snapshots'/'exchange-universe.json') if kind == 'concept' else None
        pool_hash = hashlib.sha256(json.dumps(pool['rows'],sort_keys=True).encode()).hexdigest() if pool else None
        if kind == 'concept' and (not pool or not latest or latest.get('pool_hash') != pool_hash
                                  or latest.get('validation_schema') != 2):
            latest = None
        if latest:
            member_dir = base / latest["generation"] / "members"
            if any(not (member_dir / f"{board['code']}.json").exists()
                   for board in latest["boards"]):
                latest = None
            elif kind == 'concept':
                try:
                    for board in latest['boards']:
                        saved = read_json(member_dir/f"{board['code']}.json")
                        self._concept_validator(pool,pool_hash)([saved])
                except (ValueError,KeyError,TypeError):
                    latest = None
        if latest and not refresh and not active_file.exists():
            return latest
        active = read_json(active_file)
        if active is None:
            generation = self.clock().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
            list_path = base / generation / "list.json"
            key = f"snapshot:{kind}:list:{generation}"
            if not self._fetch(key, f"{kind}_list", {}, list_path,
                               self._catalog_validator("板块代码", "板块名称")):
                return None if getattr(self,'product',False) else latest
            active = {"generation": generation,
                      "started_at": self.clock().isoformat(),
                      "boards": read_json(list_path)["rows"]}
            write_json(active_file, active)
        generation = active["generation"]
        board_codes = {board["code"] for board in active["boards"]}
        for code in board_codes:
            key = f"snapshot:{kind}:member:{generation}:{code}"
            self.checkpoint["tasks"].setdefault(key, {"status": "PENDING", "updated_at": self.clock().isoformat(),
                                                       "error": "", "observed_date": ""})
        write_json(self.checkpoint_path, self.checkpoint)
        for board in active["boards"]:
            code = board["code"]
            path = base / generation / "members" / f"{code}.json"
            key = f"snapshot:{kind}:member:{generation}:{code}"
            params = {'symbol':code}
            validator = self._catalog_validator("代码", "名称", True)
            if kind == 'concept':
                params.update(scope='current-a-v2',pool_hash=pool_hash)
                validator = self._concept_validator(pool, pool_hash)
            self._fetch(key, f"{kind}_members", params, path, validator)
        member_dir = base / generation / "members"
        files = {path.stem for path in member_dir.glob("*.json")} if member_dir.exists() else set()
        validated_files = kind != 'concept' or all(
            read_json(member_dir/f'{code}.json',{}).get('validation_schema') == 2
            and read_json(member_dir/f'{code}.json',{}).get('pool_hash') == pool_hash
            for code in board_codes)
        if files == board_codes and validated_files and all(
                self._already_succeeded(f"snapshot:{kind}:member:{generation}:{code}",
                                        member_dir / f"{code}.json") for code in board_codes):
            manifest = {"kind": kind, "generation": generation,
                        "started_at": active["started_at"],
                        "completed_at": self.clock().isoformat(),
                        "board_count": len(board_codes),
                        "member_record_count": sum(len(read_json(member_dir / f"{code}.json")["rows"])
                                                   for code in board_codes),
                        "boards": active["boards"]}
            if kind == 'industry':
                manifest['membership_signature'] = membership_signature(self.data, manifest)
            elif all(code.startswith('sina-') for code in board_codes):
                manifest['source'] = '新浪公开概念目录与成员；独立分类，不代表所有供应商概念'
                manifest['scope'] = '采集时点成员快照，不是历史交易日成员重建'
                manifest.update(validation_schema=2,pool_hash=pool_hash,
                                unknown_member_conflicts=0,
                                scope='完整原始成员经当前产品 A 股股票池过滤；源端 count 单独保留，不用于截断明细')
            write_json(base / generation / "manifest.json", manifest)
            write_json(latest_file, manifest)
            active_file.unlink()
            return manifest
        return None if getattr(self,'product',False) else latest

    def _concept_validator(self, pool, pool_hash):
        def validate(payload):
            if not pool or not isinstance(payload,list) or len(payload) != 1:
                raise ValueError('概念原始成员及完整性证据或当前股票池缺失')
            snapshot = payload[0]
            raw = list_rows(snapshot['raw_rows'],'code','name',True)
            proof = snapshot['collection']
            if proof.get('complete') is not True or proof['raw_count'] != len(raw):
                raise ValueError('概念原始成员完整性未确认')
            for direction in ('forward_pages','reverse_pages'):
                pages = proof[direction]
                if not pages or pages[-1]['size'] != 0 or sum(p['size'] for p in pages) != len(raw):
                    raise ValueError('概念分页未完整到终止空页')
                if [p['page'] for p in pages] != list(range(1,len(pages)+1)):
                    raise ValueError('概念分页存在缺页')
            pool_codes = {r['code'] for r in pool['rows']}
            valid = [r for r in raw if r['code'] in pool_codes]
            exclusions = [{'code':r['code'],'name':r['name'],
                'reason':'当前产品 A 股股票池过滤：完整交易所范围快照中不存在该代码',
                'evidence':{'path':'data/snapshots/exchange-universe.json',
                            'pool_hash':pool_hash,'fetched_at':pool['fetched_at'],
                            'pool_count':len(pool_codes),'code_present':False}}
                for r in raw if r['code'] not in pool_codes]
            if len(valid)+len(exclusions) != len(raw):
                raise ValueError('概念原始成员与有效/排除成员不能逐只对应')
            if 'rows' in snapshot and (snapshot.get('pool_hash') != pool_hash
                    or snapshot['rows'] != valid or snapshot.get('exclusions') != exclusions):
                raise ValueError('概念缓存有效成员或排除证据与当前股票池不符')
            return valid, '', {'raw_rows':snapshot['raw_rows'],'collection':proof,
                'exclusions':exclusions,'pool_hash':pool_hash,'validation_schema':2,
                'valid_count':len(valid),'unknown_member_conflicts':0}
        return validate

    def _collect_market(self, target, universe, daily, now, historical):
        market_ok, market_rows = False, []
        def validate_market(rows):
            normalized, observed = self._daily_validator("market")(rows)
            codes = [clean_code(r['代码']) for r in rows]
            if len(codes) != len(set(codes)):
                raise ValueError("全市场行情含重复股票")
            if any(str(r['日期'])[:10] != target for r in rows):
                raise ValueError("全市场含非目标日行情；不能用旧价代替停牌/缺失数据")
            for row in rows:
                row['代码'] = clean_code(row['代码'])
                for field in ('收盘', '涨跌幅', '成交额'):
                    row[field] = float(row[field])
                    if not math.isfinite(row[field]):
                        raise ValueError(f"{field} 非有效数值")
                if row['收盘'] <= 0 or row['成交额'] < 0:
                    raise ValueError("价格或成交额无效")
            return normalized, observed
        if universe:
            codes = sorted(r['code'] for r in universe['rows'])
            batches_ok = []
            # A previous-day close may still be available before the provider resets.
            # Once a historical batch fails, use saved batches and dated history.
            try_live = True
            for offset in range(0, len(codes), 200):
                batch_codes = codes[offset:offset+200]
                token = hashlib.sha256(','.join(batch_codes).encode()).hexdigest()[:16]
                path = daily/'market-batches'/f'{token}.json'
                def validate_batch(rows):
                    normalized, observed = validate_market(rows)
                    if {r['代码'] for r in normalized} != set(batch_codes):
                        raise ValueError('批量行情覆盖不完整')
                    return normalized, observed
                success = self._fetch(f'daily:{target}:market:{token}', 'market_batch',
                    {'target_date': target, 'codes': batch_codes}, path, validate_batch,
                    cache_only=historical and not try_live)
                batches_ok.append(success)
                if not success and historical:
                    try_live = False
                if success:
                    market_rows.extend(read_json(path)['rows'])
            market_ok = bool(batches_ok) and all(batches_ok)
            if historical:
                available = {r['代码'] for r in market_rows}
                names = {r['code']: r['name'] for r in universe['rows']}
                history_codes = sorted(codes, key=lambda c:(self.checkpoint['tasks'].get(
                    f'daily:{target}:history:{c}', {}).get('attempt_rounds', 0), c))
                for code in history_codes:
                    if code in available:
                        continue
                    path = daily/'market-history'/f'{code}.json'
                    def validate_history(rows):
                        normalized, observed = validate_market(rows)
                        if len(normalized) != 1 or normalized[0]['代码'] != code:
                            raise ValueError('历史行情股票代码或行数不符')
                        return normalized, observed
                    if self._fetch(f'daily:{target}:history:{code}', 'market_history',
                                   {'target_date':target, 'code':code, 'name':names[code]},
                                   path, validate_history):
                        market_rows.extend(read_json(path)['rows'])
                market_ok = {r['代码'] for r in market_rows} == set(codes)
            if market_ok:
                write_json(daily/'market.json', {'target_date':target,'rows':market_rows,
                    'source':'Validated snapshots and dated history; per-row source retained','fetched_at':now.isoformat()})
        return market_ok, market_rows
