#!/usr/bin/env python3
"""Collect point-in-time broad-market history for daily-trading scans."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.request import Request, urlopen

try:
    from . import collect_main_evidence
except ImportError:  # pragma: no cover - direct script fallback
    import collect_main_evidence  # type: ignore


MASTER_URLS = {
    "KOSPI": "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
    "KOSDAQ": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
}
KOSPI_WIDTHS = (
    2, 1, 4, 4, 4,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    9, 5, 5, 1, 1, 1, 2, 1, 1, 1, 2, 2, 2, 3, 1, 3, 12, 12, 8, 15, 21, 2, 7,
    1, 1, 1, 1, 1, 9, 9, 9, 5, 9, 8, 9, 3, 1, 1, 1,
)
KOSDAQ_WIDTHS = (
    2, 1, 4, 4, 4, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 9, 5, 5, 1, 1, 1, 2, 1, 1, 1, 2, 2, 2, 3, 1, 3, 12, 12,
    8, 15, 21, 2, 7, 1, 1, 1, 1, 9, 9, 9, 5, 9, 8, 9, 3, 1, 1, 1,
)
MASTER_LAYOUTS = {
    "KOSPI": {
        "widths": KOSPI_WIDTHS,
        "indexes": {
            "group_code": 0,
            "market_cap_size": 1,
            "low_liquidity": 6,
            "etp_code": 12,
            "spac": 19,
            "investment_caution": -1,
            "trading_halt": 34,
            "liquidation": 35,
            "managed": 36,
            "market_warning": 37,
            "unfaithful_disclosure": 39,
            "preferred": 54,
        },
    },
    "KOSDAQ": {
        "widths": KOSDAQ_WIDTHS,
        "indexes": {
            "group_code": 0,
            "market_cap_size": 1,
            "low_liquidity": 6,
            "etp_code": 8,
            "spac": 14,
            "investment_caution": 20,
            "trading_halt": 29,
            "liquidation": 30,
            "managed": 31,
            "market_warning": 32,
            "unfaithful_disclosure": 34,
            "preferred": 49,
        },
    },
}
SCHEMA_VERSION = "1"
KIS_HISTORY_PAGE_SIZE = 100
HISTORICAL_MASTER_FIELDS = ("group_code", "etp_code", "preferred")


@dataclass(frozen=True)
class MasterSymbol:
    symbol_id: str
    symbol_name: str
    exchange: str
    fields: dict[str, str]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def yyyymmdd(value: date) -> str:
    return value.strftime("%Y%m%d")


def normalized_date(value: Any) -> str:
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return ""
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:])).isoformat()
    except ValueError:
        return ""


def split_fixed_width(value: str, widths: Iterable[int]) -> list[str]:
    fields: list[str] = []
    offset = 0
    for width in widths:
        fields.append(value[offset : offset + width].strip())
        offset += width
    return fields


def parse_master_text(content: bytes, exchange: str) -> list[MasterSymbol]:
    layout = MASTER_LAYOUTS[exchange]
    widths = layout["widths"]
    indexes = layout["indexes"]
    tail_width = sum(widths)
    rows: list[MasterSymbol] = []
    for line in content.decode("cp949", errors="ignore").splitlines():
        if len(line) <= tail_width + 21:
            continue
        head = line[:-tail_width]
        raw_fields = split_fixed_width(line[-tail_width:], widths)
        symbol_id = head[:9].strip()
        symbol_name = head[21:].strip()
        if not symbol_id or not symbol_name:
            continue
        rows.append(
            MasterSymbol(
                symbol_id=symbol_id[-6:] if len(symbol_id) > 6 else symbol_id,
                symbol_name=symbol_name,
                exchange=exchange,
                fields={
                    name: raw_fields[index] if index >= 0 else ""
                    for name, index in indexes.items()
                },
            )
        )
    return rows


def parse_master_archive(payload: bytes, exchange: str) -> list[MasterSymbol]:
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            members = [name for name in archive.namelist() if name.lower().endswith(".mst")]
            if not members:
                raise ValueError(f"{exchange} master archive has no .mst member")
            member = members[0]
            content = archive.read(member)
    except zipfile.BadZipFile:
        content = payload
    return parse_master_text(content, exchange)


def historical_common_stock(symbol: MasterSymbol) -> bool:
    fields = symbol.fields
    return (
        len(symbol.symbol_id) == 6
        and all(character.isdigit() or "A" <= character <= "Z" for character in symbol.symbol_id)
        and fields.get("group_code") == "ST"
        and fields.get("preferred") == "0"
        and not fields.get("etp_code")
    )


def live_tradeable_common_stock(symbol: MasterSymbol) -> bool:
    fields = symbol.fields
    return (
        historical_common_stock(symbol)
        and fields.get("low_liquidity") == "N"
        and fields.get("spac") == "N"
        and fields.get("investment_caution") in {"", "N"}
        and fields.get("trading_halt") == "N"
        and fields.get("liquidation") == "N"
        and fields.get("managed") == "N"
        and fields.get("market_warning") == "00"
        and fields.get("unfaithful_disclosure") == "N"
    )


def download_market_universe(
    opener: Callable[..., Any] = urlopen,
    selector: Callable[[MasterSymbol], bool] = historical_common_stock,
) -> tuple[list[MasterSymbol], dict[str, Any]]:
    selected: list[MasterSymbol] = []
    source_counts: dict[str, dict[str, int]] = {}
    for exchange, url in MASTER_URLS.items():
        request = Request(url, headers={"User-Agent": "danta-bot-market-scanner/1"})
        with opener(request, timeout=60) as response:
            payload = response.read()
        parsed = parse_master_archive(payload, exchange)
        if not parsed:
            raise ValueError(f"{exchange} master contained no parseable symbols")
        eligible = [symbol for symbol in parsed if selector(symbol)]
        if not eligible:
            raise ValueError(f"{exchange} master contained no selected symbols")
        selected.extend(eligible)
        source_counts[exchange] = {"raw": len(parsed), "eligible": len(eligible)}
    deduplicated = {symbol.symbol_id: symbol for symbol in selected}
    return [deduplicated[key] for key in sorted(deduplicated)], {
        "downloaded_at": utc_now(),
        "sources": dict(MASTER_URLS),
        "counts": source_counts,
        "selection": selector.__name__,
        "survivorship_bias": (
            "current master membership and instrument types only; historical membership and delisted symbols "
            "are unavailable, and current risk/status fields are not used"
        ),
    }


def open_history_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS symbols (
            symbol_id TEXT PRIMARY KEY,
            symbol_name TEXT NOT NULL,
            exchange TEXT NOT NULL,
            master_fields_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bars (
            symbol_id TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open_price INTEGER,
            high_price INTEGER,
            low_price INTEGER,
            close_price INTEGER,
            volume INTEGER,
            trading_value INTEGER,
            PRIMARY KEY (symbol_id, trade_date)
        );
        CREATE INDEX IF NOT EXISTS bars_trade_date ON bars(trade_date);
        CREATE TABLE IF NOT EXISTS symbol_collection (
            symbol_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            requested_start TEXT NOT NULL,
            requested_end TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            error TEXT NOT NULL
        );
        """
    )
    return connection


def set_metadata(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
    )


def collection_covers(
    connection: sqlite3.Connection,
    symbol_id: str,
    start: date,
    end: date,
) -> bool:
    row = connection.execute(
        "SELECT status, requested_start, requested_end FROM symbol_collection WHERE symbol_id = ?",
        (symbol_id,),
    ).fetchone()
    return bool(
        row
        and row["status"] == "success"
        and row["requested_start"] <= start.isoformat()
        and row["requested_end"] >= end.isoformat()
    )


def store_symbol_history(
    connection: sqlite3.Connection,
    symbol: MasterSymbol,
    bars: list[dict[str, Any]],
    start: date,
    end: date,
    *,
    observed_at: str,
    error: str = "",
) -> int:
    connection.execute(
        "INSERT OR REPLACE INTO symbols VALUES (?, ?, ?, ?)",
        (
            symbol.symbol_id,
            symbol.symbol_name,
            symbol.exchange,
            json.dumps(
                {name: symbol.fields.get(name, "") for name in HISTORICAL_MASTER_FIELDS},
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    )
    valid_rows = []
    for raw in bars:
        trade_date = normalized_date(raw.get("date"))
        close_price = collect_main_evidence.parse_int(raw.get("close"))
        if not trade_date or close_price is None or close_price <= 0:
            continue
        valid_rows.append(
            (
                symbol.symbol_id,
                trade_date,
                collect_main_evidence.parse_int(raw.get("open")),
                collect_main_evidence.parse_int(raw.get("high")),
                collect_main_evidence.parse_int(raw.get("low")),
                close_price,
                collect_main_evidence.parse_int(raw.get("volume")),
                collect_main_evidence.parse_int(raw.get("trading_value")),
            )
        )
    connection.executemany(
        """
        INSERT OR REPLACE INTO bars(
            symbol_id, trade_date, open_price, high_price, low_price,
            close_price, volume, trading_value
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        valid_rows,
    )
    if not valid_rows and not error:
        error = "KIS returned no valid daily bars"
    status = "success" if valid_rows and not error else "failed"
    connection.execute(
        "INSERT OR REPLACE INTO symbol_collection VALUES (?, ?, ?, ?, ?, ?)",
        (symbol.symbol_id, status, start.isoformat(), end.isoformat(), observed_at, error[:500]),
    )
    return len(valid_rows) if status == "success" else 0


def bars_before(
    connection: sqlite3.Connection,
    symbol_id: str,
    decision_date: date,
    *,
    limit: int = 60,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT trade_date, open_price, high_price, low_price, close_price, volume, trading_value
        FROM bars
        WHERE symbol_id = ? AND trade_date < ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (symbol_id, decision_date.isoformat(), max(1, limit)),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def bar_on(
    connection: sqlite3.Connection,
    symbol_id: str,
    session_date: date,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT trade_date, open_price, high_price, low_price, close_price, volume, trading_value
        FROM bars WHERE symbol_id = ? AND trade_date = ?
        """,
        (symbol_id, session_date.isoformat()),
    ).fetchone()
    return dict(row) if row else None


def fetch_symbol_bars(
    symbol: MasterSymbol,
    start: date,
    end: date,
    *,
    app_key: str,
    app_secret: str,
    token: str,
    retries: int,
) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    current_end = end
    completed = False
    for _page in range(20):
        body, _headers = collect_main_evidence.call_endpoint(
            "inquire_daily_itemchartprice",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol.symbol_id,
                "FID_INPUT_DATE_1": yyyymmdd(start),
                "FID_INPUT_DATE_2": yyyymmdd(current_end),
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",
            },
            app_key,
            app_secret,
            token,
            retries,
            env_dv="real",
        )
        raw_rows = collect_main_evidence.output_rows_from_body(body, "output2")
        rows = [collect_main_evidence.compact_ohlcv_bar(row) for row in raw_rows]
        dated_rows = [(normalized_date(row.get("date")), row) for row in rows]
        dated_rows = [(trade_date, row) for trade_date, row in dated_rows if trade_date]
        if raw_rows and len(dated_rows) != len(raw_rows):
            raise RuntimeError(f"KIS history page contained invalid dates for {symbol.symbol_id}")
        if any((collect_main_evidence.parse_int(row.get("close")) or 0) <= 0 for _date, row in dated_rows):
            raise RuntimeError(f"KIS history page contained invalid close prices for {symbol.symbol_id}")
        for trade_date, row in dated_rows:
            if start.isoformat() <= trade_date <= end.isoformat():
                by_date[trade_date] = row
        if not dated_rows or len(raw_rows) < KIS_HISTORY_PAGE_SIZE:
            completed = True
            break
        oldest = min(trade_date for trade_date, _row in dated_rows)
        if oldest <= start.isoformat():
            completed = True
            break
        next_end = date.fromisoformat(oldest) - timedelta(days=1)
        if next_end >= current_end:
            raise RuntimeError(f"KIS history cursor did not advance for {symbol.symbol_id}")
        current_end = next_end
    if not completed:
        raise RuntimeError(f"KIS history pagination limit reached for {symbol.symbol_id}")
    return [by_date[key] for key in sorted(by_date)]


def collect_history(
    output_db: Path,
    start: date,
    end: date,
    *,
    request_interval_seconds: float = 0.06,
    retries: int = 3,
    max_symbols: int = 0,
    access_token: str = "",
) -> dict[str, Any]:
    symbols, master = download_market_universe()
    if max_symbols > 0:
        symbols = symbols[:max_symbols]
    env_dv = collect_main_evidence.normalize_trading_env("real")
    app_key, app_secret = collect_main_evidence.kis_credentials(env_dv)
    token = access_token
    token_status = "supplied"
    if not token:
        token, token_status, _expires_at = collect_main_evidence.fetch_token(
            app_key, app_secret, env_dv, retries
        )
    observed_at = utc_now()
    connection = open_history_database(output_db)
    collected = skipped = failed = 0
    try:
        set_metadata(connection, "schema_version", SCHEMA_VERSION)
        set_metadata(connection, "master", master)
        set_metadata(connection, "requested_start", start.isoformat())
        set_metadata(connection, "requested_end", end.isoformat())
        connection.commit()
        for index, symbol in enumerate(symbols, start=1):
            if collection_covers(connection, symbol.symbol_id, start, end):
                skipped += 1
                continue
            error = ""
            bars: list[dict[str, Any]] = []
            for attempt in range(retries + 1):
                try:
                    bars = fetch_symbol_bars(
                        symbol,
                        start,
                        end,
                        app_key=app_key,
                        app_secret=app_secret,
                        token=token,
                        retries=1,
                    )
                    if bars:
                        break
                    error = "KIS returned no daily bars"
                except Exception as exc:  # noqa: BLE001 - persist sanitized per-symbol failure
                    error = collect_main_evidence.safe_error(exc, required=False)["message"]
                if attempt < retries:
                    time.sleep(max(0.5, request_interval_seconds * 10))
            stored_count = store_symbol_history(
                connection,
                symbol,
                bars,
                start,
                end,
                observed_at=observed_at,
                error="" if bars else error,
            )
            if stored_count:
                collected += 1
            else:
                failed += 1
            if index % 25 == 0:
                connection.commit()
                print(
                    f"[{index}/{len(symbols)}] collected={collected} skipped={skipped} failed={failed}",
                    flush=True,
                )
            if request_interval_seconds > 0:
                time.sleep(request_interval_seconds)
        set_metadata(connection, "completed_at", utc_now())
        connection.commit()
        bar_count = int(connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0])
    finally:
        connection.close()
    return {
        "status": "success" if failed == 0 else "partial",
        "output_db": str(output_db),
        "universe_count": len(symbols),
        "collected_count": collected,
        "reused_count": skipped,
        "failed_count": failed,
        "bar_count": bar_count,
        "token_status": token_status,
        "master": master,
    }


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect broad-market daily bars for point-in-time scans.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect-history")
    collect.add_argument("--output-db", type=Path, required=True)
    collect.add_argument("--start", type=parse_date, required=True)
    collect.add_argument("--end", type=parse_date, required=True)
    collect.add_argument("--request-interval-ms", type=int, default=60)
    collect.add_argument("--retries", type=int, default=3)
    collect.add_argument("--max-symbols", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.start > args.end:
        raise SystemExit("--start must be on or before --end")
    if args.request_interval_ms < 0 or args.retries < 0 or args.max_symbols < 0:
        raise SystemExit("request interval, retries, and max symbols must be non-negative")
    result = collect_history(
        args.output_db,
        args.start,
        args.end,
        request_interval_seconds=args.request_interval_ms / 1000.0,
        retries=args.retries,
        max_symbols=args.max_symbols,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] in {"success", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
