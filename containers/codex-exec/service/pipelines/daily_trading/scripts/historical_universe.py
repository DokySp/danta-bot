#!/usr/bin/env python3
"""Collect public KIND dated ST company membership; no current-master inference.

The scope is one representative code per domestic stock company, not every
common/preferred issue. Names, industries and listing dates are deliberately
excluded because the historical export contains mutable current attributes.
Fetch/cell parsing is adapted from the inspected fetch_kind_snapshot.py probe.
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://kind.krx.co.kr/"
STATUS = "corpgeneral/listedIssueStatus.do"
DETAIL = "corpgeneral/listedissuestatusdetail.do"
MARKETS = ("STK", "KSQ")
SCOPE = "KIND ST/detailType=1 domestic stock companies; one representative code per company"


def cells(row):
    return [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())
            for value in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]


def company_counts(body):
    counts = {}
    for market in MARKETS:
        rows = [cells(row) for row in re.findall(r"<tr[^>]*>.*?</tr>", body, re.S | re.I)
                if f"detailView('{market}', 'ST', '1')" in row]
        if len(rows) != 1 or not rows[0] or not re.fullmatch(r"[0-9,]+", rows[0][0]):
            raise ValueError(f"missing or ambiguous historical company count: {market}")
        counts[market] = int(rows[0][0].replace(",", ""))
        if counts[market] <= 0:
            raise ValueError(f"empty historical company count: {market}")
    return counts


def membership_codes(body, expected):
    rows = [values for row in re.findall(r"<tr[^>]*>.*?</tr>", body, re.S | re.I)
            if len(values := cells(row)) == 6]
    codes = [row[3] for row in rows]
    if (len(codes) != expected or len(set(codes)) != expected
            or any(not re.fullmatch(r"[0-9A-Z]{6}", code) for code in codes)):
        raise ValueError(f"membership coverage failed: expected={expected}, rows={len(codes)}, unique={len(set(codes))}")
    return sorted(codes)


def load_calendar(path, start="2020-01-01", end="2025-12-31", *, require_full=False):
    raw = Path(path).read_bytes()
    payload = json.loads(raw)
    days = payload.get("trading_dates", payload.get("dates")) if isinstance(payload, dict) else payload
    if not isinstance(days, list) or not days:
        raise ValueError("calendar must contain a nonempty trading_dates list")
    for day in days:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or not start <= day <= end:
            raise ValueError("calendar contains an invalid or out-of-range ISO date")
    if days != sorted(set(days)):
        raise ValueError("calendar dates must be sorted and unique")
    if require_full and (len(days) != 1473 or days[0] != "2020-01-02" or days[-1] != "2025-12-30"):
        raise ValueError("full 2020-2025 research calendar requires 1473 sessions and verified boundaries")
    return days, hashlib.sha256(raw).hexdigest()


@contextmanager
def open_database(path):
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(path, days, calendar_hash):
    with open_database(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS universe_snapshots (
                snapshot_date TEXT NOT NULL, market TEXT NOT NULL CHECK(market IN ('STK','KSQ')),
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','failed','complete')),
                expected_count INTEGER, actual_count INTEGER, membership_sha256 TEXT,
                retrieved_at_utc TEXT, status_source_json TEXT, detail_source_json TEXT,
                error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(snapshot_date, market));
            CREATE TABLE IF NOT EXISTS universe_membership (
                snapshot_date TEXT NOT NULL, market TEXT NOT NULL,
                code TEXT NOT NULL CHECK(length(code)=6 AND code NOT GLOB '*[^0-9A-Z]*'),
                PRIMARY KEY(snapshot_date, code),
                FOREIGN KEY(snapshot_date,market) REFERENCES universe_snapshots(snapshot_date,market));
        """)
        existing = dict(db.execute("SELECT key,value FROM metadata"))
        metadata = {"schema_version": "1", "calendar_sha256": calendar_hash, "scope": SCOPE,
                    "trading_days": str(len(days)), "first_date": days[0], "last_date": days[-1],
                    "excluded_attributes": "name,industry,listing_date,current_export_market",
                    "strategy_backtest_ready": "false"}
        if existing and any(existing.get(key) != value for key, value in metadata.items()):
            raise ValueError("database scope or calendar differs; use its original calendar")
        db.executemany("INSERT OR IGNORE INTO metadata VALUES (?,?)", metadata.items())
        db.executemany("INSERT OR IGNORE INTO universe_snapshots(snapshot_date,market) VALUES (?,?)",
                       ((day, market) for day in days for market in MARKETS))
        for day, market, expected, actual, digest in db.execute(
                "SELECT snapshot_date,market,expected_count,actual_count,membership_sha256 "
                "FROM universe_snapshots WHERE status='complete'"):
            codes = [row[0] for row in db.execute(
                "SELECT code FROM universe_membership WHERE snapshot_date=? AND market=? ORDER BY code", (day, market))]
            if expected != len(codes) or actual != len(codes) or digest != code_digest(codes):
                raise ValueError(f"saved membership validation failed: {day}/{market}")


def code_digest(codes):
    return hashlib.sha256("\n".join(sorted(codes)).encode()).hexdigest()


def validated_complete_codes(path, calendar_path):
    """Read-only batch gate: exact calendar/market pairs and every saved membership hash."""
    days, digest = load_calendar(calendar_path, require_full=True)
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('BEGIN')
        metadata = dict(db.execute('SELECT key,value FROM metadata'))
        if (metadata.get('calendar_sha256') != digest or metadata.get('scope') != SCOPE
                or metadata.get('trading_days') != str(len(days))
                or metadata.get('first_date') != days[0] or metadata.get('last_date') != days[-1]):
            raise ValueError('historical universe metadata/calendar mismatch')
        snapshots = db.execute('SELECT snapshot_date,market,status,expected_count,actual_count,membership_sha256 FROM universe_snapshots').fetchall()
        if (len(snapshots) != 2 * len(days)
                or {(r[0], r[1]) for r in snapshots} != {(day, market) for day in days for market in MARKETS}
                or any(row[2] != 'complete' for row in snapshots)):
            raise ValueError('full historical universe date/market pairs incomplete')
        all_codes, total = set(), 0
        for day, market, _, expected, actual, saved_hash in snapshots:
            codes = [r[0] for r in db.execute('SELECT code FROM universe_membership WHERE snapshot_date=? AND market=? ORDER BY code', (day, market))]
            if (not codes or len(codes) != len(set(codes)) or expected != len(codes) or actual != len(codes)
                    or code_digest(codes) != saved_hash or any(not re.fullmatch(r'[0-9A-Z]{6}', code) for code in codes)):
                raise ValueError('historical universe membership integrity failed')
            all_codes.update(codes)
            total += len(codes)
        if (total != db.execute('SELECT COUNT(*) FROM universe_membership').fetchone()[0]
                or db.execute('SELECT 1 FROM universe_membership GROUP BY snapshot_date,code HAVING COUNT(*)>1 LIMIT 1').fetchone()):
            raise ValueError('historical universe orphan/overlapping membership')
        return sorted(all_codes)
    finally:
        db.close()


def save_membership(db, day, market, expected, codes, status_source, detail_source):
    if len(codes) != expected or len(set(codes)) != expected:
        raise ValueError("official count differs from unique membership")
    with db:
        db.execute("DELETE FROM universe_membership WHERE snapshot_date=? AND market=?", (day, market))
        # The date/code primary key rejects any same-day market overlap atomically.
        db.executemany("INSERT INTO universe_membership VALUES (?,?,?)", ((day, market, code) for code in codes))
        db.execute("UPDATE universe_snapshots SET status='complete',expected_count=?,actual_count=?,"
                   "membership_sha256=?,retrieved_at_utc=?,status_source_json=?,detail_source_json=?,error=NULL "
                   "WHERE snapshot_date=? AND market=?",
                   (expected, len(codes), code_digest(codes), datetime.now(timezone.utc).isoformat(),
                    json.dumps(status_source, sort_keys=True), json.dumps(detail_source, sort_keys=True), day, market))


class KindClient:
    def __init__(self, interval=0.5):
        self.interval = interval
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.request_count = 0

    def fetch(self, path, params):
        url = BASE + path + "?" + urlencode(params)
        for attempt in range(3):
            with self.lock:
                if self.stopped.is_set():
                    raise RuntimeError("collection stopped; resume after checking failure")
                time.sleep(max(0.0, self.next_request - time.monotonic()))
                if self.stopped.is_set():
                    raise RuntimeError("collection stopped; resume after checking failure")
                self.next_request = time.monotonic() + self.interval
                self.request_count += 1
            try:
                with urlopen(Request(url), timeout=30) as response:
                    body = response.read()
                    content_type = response.headers.get("Content-Type", "")
                    text = body.decode("euc-kr" if "ms-excel" in content_type else "utf-8")
                    return text, {"url": url, "method": "GET", "http_status": response.status,
                                  "content_type": content_type, "response_bytes": len(body),
                                  "response_sha256": hashlib.sha256(body).hexdigest(),
                                  "retrieved_at_utc": datetime.now(timezone.utc).isoformat()}
            except HTTPError as exc:
                exc.close()
                if exc.code in (403, 429):
                    self.stopped.set()
                    raise RuntimeError(f"KIND access/rate block HTTP {exc.code}; no retry") from None
                if exc.code < 500 or attempt == 2:
                    raise RuntimeError(f"KIND HTTP {exc.code}") from None
            except (URLError, OSError, TimeoutError):
                if attempt == 2:
                    raise RuntimeError("KIND transport failed after 3 attempts") from None
            time.sleep(2 ** attempt)


def collect_day(path, client, day):
    with open_database(path) as db:
        pending = [row[0] for row in db.execute(
            "SELECT market FROM universe_snapshots WHERE snapshot_date=? AND status!='complete' ORDER BY market DESC", (day,))]
        if not pending:
            return
        status_source = None
        try:
            with db:
                db.execute("UPDATE universe_snapshots SET attempts=attempts+1 WHERE snapshot_date=? AND status!='complete'", (day,))
            body, status_source = client.fetch(STATUS, {"method": "readListedIssueStatus", "selDate": day})
            counts = company_counts(body)
            with db:
                db.executemany("UPDATE universe_snapshots SET expected_count=?,status_source_json=? "
                               "WHERE snapshot_date=? AND market=? AND status!='complete'",
                               ((counts[market], json.dumps(status_source, sort_keys=True), day, market) for market in pending))
            for market in pending:
                body, detail_source = client.fetch(DETAIL, {
                    "method": "searchListedIssueStatDetailSub", "forward": "listedissuestatdetail_down",
                    "selDate": day.replace("-", ""), "mktId": market, "secugrpId": "ST",
                    "detailType": "1", "currentPageSize": "3000", "pageIndex": "1"})
                with db:
                    db.execute("UPDATE universe_snapshots SET detail_source_json=? WHERE snapshot_date=? AND market=?",
                               (json.dumps(detail_source, sort_keys=True), day, market))
                codes = membership_codes(body, counts[market])
                save_membership(db, day, market, counts[market], codes, status_source, detail_source)
        except Exception as exc:
            client.stopped.set()
            with db:
                db.execute("UPDATE universe_snapshots SET status='failed',error=?,retrieved_at_utc=?,"
                           "status_source_json=COALESCE(?,status_source_json) WHERE snapshot_date=? AND status!='complete'",
                           (str(exc), datetime.now(timezone.utc).isoformat(),
                            json.dumps(status_source, sort_keys=True) if status_source else None, day))
            raise


def collect(path, days, client, workers=2, limit_days=None):
    if workers not in (1, 2) or (limit_days is not None and limit_days <= 0):
        raise ValueError("workers must be 1 or 2; limit_days must be positive")
    with open_database(path) as db:
        pending = {row[0] for row in db.execute("SELECT snapshot_date FROM universe_snapshots WHERE status!='complete'")}
    selected = [day for day in days if day in pending][:limit_days]
    started = time.monotonic()
    errors = []
    completed = 0
    iterator = iter(selected)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        active = {executor.submit(collect_day, path, client, day): day for day in [next(iterator, None) for _ in range(workers)] if day}
        while active:
            finished, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in finished:
                day = active.pop(future)
                try:
                    future.result()
                    completed += 1
                except Exception as exc:
                    errors.append({"date": day, "error": str(exc)})
                print(json.dumps({"date": day, "completed_days_this_run": completed,
                                  "requests": client.request_count, "elapsed_seconds": round(time.monotonic() - started, 1),
                                  "stopped": client.stopped.is_set()}), flush=True)
            while len(active) < workers and not client.stopped.is_set():
                day = next(iterator, None)
                if day is None:
                    break
                active[executor.submit(collect_day, path, client, day)] = day
    with open_database(path) as db:
        statuses = dict(db.execute("SELECT status,COUNT(*) FROM universe_snapshots GROUP BY status"))
        memberships = db.execute("SELECT COUNT(*) FROM universe_membership").fetchone()[0]
    return {"status": "complete" if statuses.get("complete", 0) == 2 * len(days) else "incomplete",
            "snapshot_status_counts": statuses, "membership_rows": memberships, "requests_this_run": client.request_count,
            "completed_days_this_run": completed, "elapsed_seconds": round(time.monotonic() - started, 1),
            "errors": errors, "scope": SCOPE, "strategy_backtest_ready": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--limit-days", type=int)
    args = parser.parse_args()
    days, digest = load_calendar(args.calendar, require_full=True)
    initialize(args.output, days, digest)
    result = collect(args.output, days, KindClient(), args.workers, args.limit_days)
    print(json.dumps(result), flush=True)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
