#!/usr/bin/env python3
"""Closed-loop daily agent replay using archived point-in-time artifacts."""

from __future__ import annotations

import argparse
import copy
import json
import math
import shutil
import sqlite3
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    from . import build_run_artifacts, run_subagent
    from .run_daily_trading_pipeline import Pipeline
    from ...news_context.builder import item_keys, merge_unique, select_market_items
except ImportError:  # pragma: no cover - direct script fallback
    import build_run_artifacts  # type: ignore
    import run_subagent  # type: ignore
    from run_daily_trading_pipeline import Pipeline  # type: ignore
    from service.pipelines.news_context.builder import item_keys, merge_unique, select_market_items  # type: ignore


KST = timezone(timedelta(hours=9))
PIPELINE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_ROOT = Path("/home/uhug/mnt/ugreen-docker/codex-exec/reports/runs")
DEFAULT_MARKET_NEWS_DB = Path("/home/uhug/mnt/ugreen-docker/codex-exec/memory/market-news/market-news.sqlite3")
DEFAULT_WORKSPACE = Path(__file__).resolve().parents[6]
DECISION_TIME_TOLERANCE_SECONDS = 120


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def as_int(value: Any) -> int:
    parsed = number(value)
    return max(0, int(parsed)) if parsed is not None else 0


def affordable_quantity(budget: float, price: float) -> int:
    return max(0, int(math.floor(budget / price + 1e-9))) if price > 0 else 0


def symbol_key(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("symbol_id") or item.get("symbol") or item.get("code") or "").strip()


def parse_started(payload: dict[str, Any], fallback_name: str = "") -> datetime | None:
    raw = str(payload.get("started_at") or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=KST)
        except ValueError:
            pass
    try:
        return datetime.strptime(fallback_name[:15], "%Y%m%dT%H%M%S").replace(tzinfo=KST)
    except ValueError:
        return None


def parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def decision_information_cutoff(row: dict[str, Any]) -> datetime:
    generated = parse_datetime((row.get("brief") or {}).get("generated_at"))
    started = row["started_at"]
    return generated if generated is not None and generated >= started else started


def future_input_timestamps(payload: Any, cutoff: datetime, path: str = "$") -> list[str]:
    future: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{path}.{key}"
            is_information_time = (
                key.endswith("_at")
                or key in {"as_of", "window_start", "window_end"}
                or key == "date"
            )
            observed = parse_datetime(value) if is_information_time else None
            if observed is not None and observed > cutoff:
                future.append(f"{child}={observed.isoformat()}")
            future.extend(future_input_timestamps(value, cutoff, child))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            future.extend(future_input_timestamps(value, cutoff, f"{path}[{index}]"))
    return future


def align_replay_generated_at(payload: dict[str, Any], decision_brief: dict[str, Any]) -> datetime:
    cutoff = parse_datetime((decision_brief.get("source_artifacts") or {}).get("information_cutoff"))
    if cutoff is None:
        raise ValueError("replay decision brief has no information cutoff")
    payload["generated_at"] = cutoff.isoformat()
    return cutoff


def index_value(payload: dict[str, Any], symbol: str = "KOSPI") -> float | None:
    for item in payload.get("indexes", []) if isinstance(payload.get("indexes"), list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("symbol") or "").upper() != symbol or item.get("status") != "success":
            continue
        parsed = number(item.get("value"))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def symbol_prices(brief: dict[str, Any], account: dict[str, Any] | None = None) -> dict[str, float]:
    prices: dict[str, float] = {}
    for item in brief.get("symbols", []) if isinstance(brief.get("symbols"), list) else []:
        if not isinstance(item, dict):
            continue
        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        parsed = number(price.get("current_or_last"))
        key = symbol_key(item)
        if key and parsed is not None and parsed > 0:
            prices[key] = parsed
    if account:
        rows = [
            *(account.get("symbols", []) if isinstance(account.get("symbols"), list) else []),
            *(
                account.get("non_universe_account_positions", [])
                if isinstance(account.get("non_universe_account_positions"), list)
                else []
            ),
        ]
        for item in rows:
            if not isinstance(item, dict):
                continue
            key = symbol_key(item)
            parsed = number(item.get("current_price"))
            quantity = as_int(item.get("current_live_holding_quantity"))
            valuation = number(item.get("valuation_amount"))
            if (parsed is None or parsed <= 0) and quantity and valuation is not None:
                parsed = valuation / quantity
            if key and parsed is not None and parsed > 0:
                prices.setdefault(key, parsed)
    return prices


def fill_quotes(brief: dict[str, Any]) -> dict[str, dict[str, float]]:
    quotes: dict[str, dict[str, float]] = {}
    for item in brief.get("symbols", []) if isinstance(brief.get("symbols"), list) else []:
        if not isinstance(item, dict):
            continue
        key = symbol_key(item)
        book = item.get("orderbook_summary") if isinstance(item.get("orderbook_summary"), dict) else {}
        ask = number(book.get("best_ask"))
        bid = number(book.get("best_bid"))
        ask_qty = number(book.get("ask_quantity_1"))
        bid_qty = number(book.get("bid_quantity_1"))
        if key:
            quotes[key] = {
                "buy_price": ask or 0.0,
                "sell_price": bid or 0.0,
                "buy_quantity": max(0.0, ask_qty or 0.0),
                "sell_quantity": max(0.0, bid_qty or 0.0),
            }
    return quotes


def market_open_day_from_price_chart(payload: Any, session_date: date) -> bool | None:
    """Keep session validation in the research tool, outside stable trading code."""
    if not isinstance(payload, dict):
        return None
    if payload.get("market_open_day_checked") is True and isinstance(payload.get("market_open_day"), bool):
        return bool(payload["market_open_day"])
    expected = session_date.strftime("%Y%m%d")
    for symbol in payload.get("symbols", []) if isinstance(payload.get("symbols"), list) else []:
        charts = symbol.get("charts") if isinstance(symbol, dict) and isinstance(symbol.get("charts"), dict) else {}
        daily = charts.get("daily") if isinstance(charts.get("daily"), list) else []
        if any(
            isinstance(row, dict)
            and str(row.get("date") or "").replace("-", "")[:8] == expected
            for row in daily
        ):
            return True
    return None


def discover_run_rows(runs_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        account_path = run_dir / "account-before-order.json"
        brief_path = run_dir / "decision-brief.json"
        market_path = run_dir / "market-index-snapshot.json"
        if not (account_path.is_file() and brief_path.is_file() and market_path.is_file()):
            continue
        account = read_json(account_path)
        brief = read_json(brief_path)
        market = read_json(market_path)
        price_chart = read_json(run_dir / "price-chart.json")
        started = parse_started(account, run_dir.name) or parse_started(brief, run_dir.name)
        if started is None:
            continue
        benchmark = index_value(market)
        if benchmark is None or str(brief.get("status") or "") not in {"success", "partial"}:
            continue
        rows.append(
            {
                "path": run_dir,
                "started_at": started,
                "date": started.date().isoformat(),
                "account": account,
                "brief": brief,
                "market": market,
                "benchmark": benchmark,
                "market_open_day": market_open_day_from_price_chart(price_chart, started.date()),
            }
        )
    return rows


def select_replay_days(
    rows: list[dict[str, Any]],
    start: date,
    end: date,
    decision_at: time,
    tolerance_seconds: int = DECISION_TIME_TOLERANCE_SECONDS,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        current = row["started_at"].date()
        if start <= current <= end and row.get("market_open_day") is True:
            grouped.setdefault(row["date"], []).append(row)

    target_seconds = decision_at.hour * 3600 + decision_at.minute * 60 + decision_at.second
    selected: list[dict[str, Any]] = []
    for session_date in sorted(grouped):
        candidates = sorted(grouped[session_date], key=lambda item: item["started_at"])
        decision = min(
            candidates,
            key=lambda item: abs(
                item["started_at"].hour * 3600
                + item["started_at"].minute * 60
                + item["started_at"].second
                - target_seconds
            ),
        )
        distance = abs(
            decision["started_at"].hour * 3600
            + decision["started_at"].minute * 60
            + decision["started_at"].second
            - target_seconds
        )
        if distance > tolerance_seconds:
            continue
        information_cutoff = decision_information_cutoff(decision)
        later = [item for item in candidates if item["started_at"] > information_cutoff]
        if not later:
            raise ValueError(f"{session_date}: no post-information-cutoff observation")
        fill = later[0]
        close = candidates[-1]
        selected.append({"date": session_date, "decision": decision, "fill": fill, "close": close})
    return selected


def daily_decision_history(
    rows: list[dict[str, Any]],
    decision_at: time,
    tolerance_seconds: int = DECISION_TIME_TOLERANCE_SECONDS,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        started = row["started_at"]
        if started.weekday() >= 5:
            continue
        grouped.setdefault(row["date"], []).append(row)
    target_seconds = decision_at.hour * 3600 + decision_at.minute * 60 + decision_at.second
    selected: list[dict[str, Any]] = []
    for session_date in sorted(grouped):
        nearest = min(
            grouped[session_date],
            key=lambda item: (
                abs(
                    item["started_at"].hour * 3600
                    + item["started_at"].minute * 60
                    + item["started_at"].second
                    - target_seconds
                ),
                -item["started_at"].timestamp(),
            ),
        )
        distance_seconds = abs(
            nearest["started_at"].hour * 3600
            + nearest["started_at"].minute * 60
            + nearest["started_at"].second
            - target_seconds
        )
        if distance_seconds <= tolerance_seconds:
            selected.append(nearest)
    return selected


def benchmark_history(rows: list[dict[str, Any]], decision_at: time) -> list[tuple[str, float]]:
    return [
        (item["date"], item["benchmark"])
        for item in daily_decision_history(rows, decision_at)
    ]


def trailing_return(
    history: list[tuple[str, float]],
    session_date: str,
    periods: int,
    current_value: float | None = None,
) -> float | None:
    dates = [item[0] for item in history]
    if session_date not in dates:
        return None
    index = dates.index(session_date)
    if index < periods:
        return None
    current = current_value if current_value is not None else history[index][1]
    baseline = history[index - periods][1]
    return (current / baseline - 1.0) * 100.0 if baseline > 0 else None


def rebuilt_market_news_context(
    database: Path,
    current_started_at: datetime,
    previous_started_at: datetime | None,
    *,
    max_items: int = 30,
    max_lookback_hours: int = 72,
) -> dict[str, Any]:
    end_dt = current_started_at.astimezone(timezone.utc).replace(microsecond=0)
    fallback_start = end_dt - timedelta(hours=max_lookback_hours)
    previous = previous_started_at.astimezone(timezone.utc).replace(microsecond=0) if previous_started_at else None
    start_dt = max(previous, fallback_start) if previous else fallback_start
    start = start_dt.isoformat()
    end = end_dt.isoformat()
    empty = {
        "schema_version": "1",
        "status": "failed",
        "context_status": "partial",
        "window_start": start,
        "window_end": end,
        "window_source": "previous_daily_replay_run" if previous and previous >= fallback_start else "fallback_lookback",
        "deduplicated_count": 0,
        "raw_count": 0,
        "selected_count": 0,
        "source_statuses": {},
        "items": [],
    }
    if not database.is_file():
        return empty

    query = """
        SELECT a.title, a.url, a.canonical_url, a.domain, a.source_country,
               a.source_language, a.published_at, a.collected_at,
               GROUP_CONCAT(DISTINCT p.source_id),
               GROUP_CONCAT(DISTINCT p.provider),
               GROUP_CONCAT(DISTINCT p.classification)
        FROM articles a
        LEFT JOIN article_provenance p
          ON p.article_id = a.id AND p.first_collected_at <= :window_end
        WHERE a.collected_at <= :window_end
          AND (
              (a.published_at != '' AND a.published_at >= :window_start AND a.published_at <= :window_end)
              OR
              (a.published_at = '' AND a.collected_at >= :window_start AND a.collected_at <= :window_end)
          )
          AND EXISTS (
              SELECT 1 FROM article_provenance sf
              WHERE sf.article_id = a.id
                AND sf.source_id = :source_id
                AND sf.first_collected_at <= :window_end
          )
        GROUP BY a.id
        ORDER BY CASE WHEN a.published_at = '' THEN a.collected_at ELSE a.published_at END DESC
        LIMIT 300
    """

    def split(value: Any) -> list[str]:
        return sorted({item for item in str(value or "").split(",") if item})

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        candidates = []
        for source_id in ("domestic", "global"):
            for row in connection.execute(
                query,
                {"window_start": start, "window_end": end, "source_id": source_id},
            ).fetchall():
                candidates.append(
                    {
                        "title": row[0],
                        "url": row[1],
                        "canonical_url": row[2],
                        "domain": row[3],
                        "source_country": row[4],
                        "source_language": row[5],
                        "published_at": row[6],
                        "collected_at": row[7],
                        "source_ids": split(row[8]),
                        "providers": split(row[9]),
                        "classifications": split(row[10]),
                    }
                )
        status_rows = connection.execute(
            """
            SELECT source_id, status, window_start, window_end, error
            FROM collection_runs
            WHERE id IN (
                SELECT MAX(id) FROM collection_runs
                WHERE finished_at <= ?
                GROUP BY source_id
            )
            """,
            (end,),
        ).fetchall()
        earliest = connection.execute(
            "SELECT MIN(collected_at) FROM articles WHERE collected_at <= ?",
            (end,),
        ).fetchone()[0]
    finally:
        connection.close()

    canonical: dict[str, dict[str, Any]] = {}
    owners: dict[str, str] = {}
    for item in candidates:
        keys = item_keys(item)
        owner = next((owners[key] for key in keys if key in owners), "")
        if owner:
            merge_unique(canonical[owner], item)
        else:
            owner = f"article:{len(canonical) + 1}"
            canonical[owner] = dict(item)
        for key in keys:
            owners[key] = owner
    merged = sorted(
        canonical.values(),
        key=lambda item: str(item.get("published_at") or item.get("collected_at") or ""),
        reverse=True,
    )
    selected = select_market_items(merged, max_items) if merged else []
    partial_start = bool(earliest and str(earliest) > start)
    status = "failed" if not selected else "partial" if partial_start else "supplied"
    source_statuses = {
        str(row[0]): {
            "status": row[1] or "unknown",
            "window_start": row[2] or "",
            "window_end": row[3] or "",
            "error": str(row[4] or "")[:300],
        }
        for row in status_rows
    }
    compact_items = [
        {
            "title": " ".join(str(item.get("title") or "").split())[:500],
            "published_at": item.get("published_at") or "",
            "collected_at": item.get("collected_at") or "",
            "url": item.get("url") or "",
            "domain": item.get("domain") or "",
            "source_country": item.get("source_country") or "",
            "source_language": item.get("source_language") or "",
            "source_ids": list(item.get("source_ids") or [])[:5],
            "providers": list(item.get("providers") or [])[:5],
            "classifications": list(item.get("classifications") or [])[:5],
        }
        for item in selected
        if str(item.get("title") or "").strip()
    ]
    return {
        **empty,
        "status": status,
        "context_status": "success" if status == "supplied" else "partial",
        "deduplicated_count": len(candidates) - len(merged),
        "raw_count": len(candidates),
        "selected_count": len(compact_items),
        "source_statuses": source_statuses,
        "items": compact_items,
    }


def initial_state(day: dict[str, Any]) -> dict[str, Any]:
    account = day["decision"]["account"]
    prices = symbol_prices(day["decision"]["brief"], account)
    summary = account.get("account_summary") if isinstance(account.get("account_summary"), dict) else {}
    cash = number(summary.get("orderable_cash_amount"))
    if cash is None:
        raise ValueError("initial account has no orderable_cash_amount")
    positions: dict[str, dict[str, Any]] = {}
    rows = [
        *(account.get("symbols", []) if isinstance(account.get("symbols"), list) else []),
        *(
            account.get("non_universe_account_positions", [])
            if isinstance(account.get("non_universe_account_positions"), list)
            else []
        ),
    ]
    for item in rows:
        if not isinstance(item, dict):
            continue
        key = symbol_key(item)
        quantity = as_int(item.get("current_live_holding_quantity"))
        if not key or quantity <= 0:
            continue
        price = prices.get(key)
        if price is None:
            raise ValueError(f"initial position {key} has no price")
        average = number(item.get("average_purchase_price")) or price
        positions[key] = {
            "symbol_name": str(item.get("symbol_name") or key),
            "quantity": quantity,
            "average_price": average,
        }
    return {
        "cash": cash,
        "positions": positions,
    }


def portfolio_value(
    state: dict[str, Any],
    prices: dict[str, float],
) -> tuple[float, list[str]]:
    total = float(state["cash"])
    missing: list[str] = []
    for key, position in state["positions"].items():
        price = prices.get(key)
        if price is None or price <= 0:
            missing.append(key)
            continue
        total += as_int(position.get("quantity")) * price
    return total, missing


def max_drawdown(levels: list[float]) -> float | None:
    if not levels or any(level <= 0 for level in levels):
        return None
    peak = levels[0]
    drawdown = 0.0
    for level in levels:
        peak = max(peak, level)
        drawdown = max(drawdown, (peak - level) / peak * 100.0)
    return drawdown


def performance_period(rows: list[dict[str, Any]], requested_days: int) -> dict[str, Any]:
    selected = rows[-requested_days:]
    if not selected:
        return {
            "coverage_status": "unavailable",
            "observed_trading_days": 0,
            "requested_trading_days": requested_days,
            "account_return_pct": None,
            "benchmark_return_pct": None,
            "excess_return_pct": None,
            "max_drawdown_pct": None,
            "max_daily_gross_turnover_pct": None,
        }
    account_return = (selected[-1]["closing_nav"] / selected[0]["opening_nav"] - 1.0) * 100.0
    benchmark_return = (selected[-1]["benchmark_close"] / selected[0]["benchmark_open"] - 1.0) * 100.0
    levels = [selected[0]["opening_nav"], *(row["closing_nav"] for row in selected)]
    return {
        "coverage_status": "complete" if len(selected) == requested_days else "partial",
        "observed_trading_days": len(selected),
        "requested_trading_days": requested_days,
        "included_return_days": len(selected),
        "account_return_pct": round(account_return, 4),
        "benchmark_return_pct": round(benchmark_return, 4),
        "excess_return_pct": round(account_return - benchmark_return, 4),
        "max_drawdown_pct": round(max_drawdown(levels) or 0.0, 4),
        "max_daily_gross_turnover_pct": round(max(row["gross_turnover_pct"] for row in selected), 4),
    }


def virtual_performance_context(
    history: list[dict[str, Any]],
    state: dict[str, Any],
    prices: dict[str, float],
    started_at: str,
    source: dict[str, Any],
    max_daily_gross_turnover_pct: float = 10.0,
) -> dict[str, Any]:
    references = copy.deepcopy(source.get("references")) if isinstance(source.get("references"), dict) else {}
    references.update(
        {
            "benchmark_index": "KOSPI",
            "primary_window_trading_days": 20,
            "auxiliary_window_trading_days": 5,
            "max_daily_gross_turnover_pct": max_daily_gross_turnover_pct,
        }
    )
    current_nav, missing = portfolio_value(state, prices)
    weights = [
        (key, position, as_int(position.get("quantity")) * prices.get(key, 0.0) / current_nav * 100.0)
        for key, position in state["positions"].items()
        if current_nav > 0 and key in prices
    ]
    largest = max(weights, key=lambda item: item[2]) if weights else None
    return {
        "schema_version": "1",
        "scope": "domestic_trading_account",
        "as_of": started_at,
        "benchmark_index": "KOSPI",
        "advisory_semantics": "Replay performance informs Judge sizing and reporting only.",
        "latest_day": copy.deepcopy(history[-1]) if history else None,
        "periods": {
            "primary": performance_period(history, 20),
            "auxiliary": performance_period(history, 5),
        },
        "current_risk": {
            "largest_symbol_id": largest[0] if largest else "",
            "largest_symbol_name": largest[1].get("symbol_name", largest[0]) if largest else "",
            "largest_symbol_weight_pct": round(largest[2], 4) if largest else 0.0,
            "missing_price_symbols": missing,
        },
        "references": references,
    }


def virtualize_inputs(
    day: dict[str, Any],
    output_dir: Path,
    state: dict[str, Any],
    history: list[dict[str, Any]],
    benchmark_return_20: float | None,
    market_news_context: dict[str, Any] | None = None,
    turnover_reference_pct: float = 30.0,
) -> dict[str, Any]:
    source = day["decision"]
    brief = copy.deepcopy(source["brief"])
    account = copy.deepcopy(source["account"])
    prices = symbol_prices(brief, account)
    benchmark_value = float(source["benchmark"])
    nav, missing = portfolio_value(state, prices)
    if missing:
        raise ValueError(f"{day['date']}: missing decision prices for virtual holdings: {','.join(missing)}")
    securities = nav - float(state["cash"])
    held = sorted(key for key, item in state["positions"].items() if as_int(item.get("quantity")) > 0)

    portfolio = copy.deepcopy(brief.get("portfolio")) if isinstance(brief.get("portfolio"), dict) else {}
    portfolio["holding"] = held
    brief["portfolio"] = portfolio
    brief["run_id"] = output_dir.name
    information_cutoff = decision_information_cutoff(source)
    brief["source_artifacts"] = {
        "mode": "archived_point_in_time_replay",
        "source_run_id": source["path"].name,
        "information_cutoff": information_cutoff.isoformat(),
    }
    brief["account_exposure_summary"] = {
        "cash_amount": round(float(state["cash"]), 4),
        "orderable_cash_amount": round(float(state["cash"]), 4),
        "securities_valuation_amount": round(securities, 4),
        "total_evaluation_amount": round(nav, 4),
        "today_buy_amount": 0,
        "today_sell_amount": 0,
        "total_pnl_amount": round(
            sum(
                (prices[key] - float(position["average_price"])) * as_int(position["quantity"])
                for key, position in state["positions"].items()
            ),
            4,
        ),
    }
    source_performance = brief.get("account_performance_context") if isinstance(brief.get("account_performance_context"), dict) else {}
    brief["account_performance_context"] = virtual_performance_context(
        history,
        state,
        prices,
        str(brief.get("started_at") or source["started_at"].isoformat()),
        source_performance,
        turnover_reference_pct,
    )
    strategy_policy, strategy_policy_path = build_run_artifacts.load_strategy_policy_config("")
    brief["strategy_context"] = build_run_artifacts.build_strategy_context(
        strategy_policy,
        strategy_policy_path,
        source["market"],
    )
    brief["replay_context"] = {
        "decision_frequency": "once_per_trading_day",
        "decision_time_kst": source["started_at"].strftime("%H:%M:%S"),
        "benchmark_return_20_period_pct": round(benchmark_return_20, 4) if benchmark_return_20 is not None else None,
        "source_run_id": source["path"].name,
    }
    if market_news_context is not None:
        brief["market_news_context"] = copy.deepcopy(market_news_context)

    brief_symbols = brief.get("symbols") if isinstance(brief.get("symbols"), list) else []
    names: dict[str, str] = {}
    for item in brief_symbols:
        if not isinstance(item, dict):
            continue
        item.pop("active_rotation_momentum", None)
        key = symbol_key(item)
        if not key:
            continue
        names[key] = str(item.get("symbol_name") or key)
        position = state["positions"].get(key)
        quantity = as_int(position.get("quantity")) if position else 0
        average = float(position.get("average_price")) if position else 0.0
        valuation = quantity * prices.get(key, 0.0)
        pnl = quantity * (prices.get(key, 0.0) - average) if quantity else 0.0
        pnl_rate = ((prices[key] / average - 1.0) * 100.0) if quantity and average > 0 else 0.0
        item["account_exposure"] = {
            "current_live_holding_quantity": quantity,
            "expected_holding_quantity": quantity,
            "pending_and_reserved_buy_quantity": 0,
            "pending_and_reserved_sell_quantity": 0,
            "holding_state_status": "consistent",
            "holding_state_reasons": [],
            "valuation_amount": round(valuation, 4),
            "pnl_amount": round(pnl, 4),
            "pnl_rate": round(pnl_rate, 4),
        }
        strategy = copy.deepcopy(item.get("symbol_strategy_context")) if isinstance(item.get("symbol_strategy_context"), dict) else {}
        strategy.update(
            {
                "current_holding": quantity > 0,
                "current_live_holding_quantity": quantity,
                "concentration_pct": round(valuation / nav * 100.0, 4) if nav > 0 else 0.0,
                "loss_position": pnl < 0,
                "pnl_rate": round(pnl_rate, 4),
            }
        )
        item["symbol_strategy_context"] = strategy
        item["today_trade_price_context"] = {
            "artifact_status": "success",
            "collection_status": "complete",
            "collection_error_count": 0,
            "collection_reason": "virtual replay starts each daily cycle with no same-day simulated fill",
            "has_same_day_buy": False,
            "has_same_day_trade": False,
        }
        item["today_trade_timeline_context"] = {
            "artifact_status": "success",
            "collection_status": "complete",
            "collection_error_count": 0,
            "collection_reason": "virtual replay starts each daily cycle with no same-day simulated fill",
            "fills": [],
            "has_same_day_buy": False,
            "has_same_day_trade": False,
        }

    source_account_rows = {
        symbol_key(item): item
        for item in [
            *(account.get("symbols", []) if isinstance(account.get("symbols"), list) else []),
            *(
                account.get("non_universe_account_positions", [])
                if isinstance(account.get("non_universe_account_positions"), list)
                else []
            ),
        ]
        if isinstance(item, dict) and symbol_key(item)
    }
    account_rows = []
    for key in sorted(set(names) | set(state["positions"])):
        row = copy.deepcopy(source_account_rows.get(key, {}))
        position = state["positions"].get(key)
        quantity = as_int(position.get("quantity")) if position else 0
        price = prices.get(key)
        if quantity and (price is None or price <= 0):
            raise ValueError(f"{day['date']}: virtual account position {key} has no price")
        average = float(position.get("average_price")) if position else 0.0
        valuation = quantity * (price or 0.0)
        row.update(
            {
                "symbol_id": key,
                "symbol_name": names.get(key) or (position or {}).get("symbol_name") or key,
                "current_live_holding_quantity": quantity,
                "current_price": price,
                "average_purchase_price": round(average, 4) if quantity else None,
                "purchase_amount": round(average * quantity, 4) if quantity else None,
                "valuation_amount": round(valuation, 4),
                "pnl_amount": round((price - average) * quantity, 4) if quantity and price else 0,
                "pnl_rate": round((price / average - 1.0) * 100.0, 4) if quantity and price and average else 0.0,
                "ord_psbl_qty": quantity,
                "pending_and_reserved_buy_quantity": 0,
                "pending_and_reserved_sell_quantity": 0,
                "today_buy_quantity": 0,
                "today_sell_quantity": 0,
                "snapshot_row_available": True,
                "holding_state_status": "consistent",
                "holding_state_reasons": [],
            }
        )
        account_rows.append(row)
    account["run_id"] = output_dir.name
    account["status"] = "success"
    account["skipped"] = False
    account["symbols"] = account_rows
    account["non_universe_account_positions"] = []
    account["active_orders"] = []
    account["active_order_lookup_performed"] = True
    account["account_summary"] = {
        "cash_amount": round(float(state["cash"]), 4),
        "orderable_cash_amount": round(float(state["cash"]), 4),
        "securities_valuation_amount": round(securities, 4),
        "today_buy_amount": 0,
        "today_sell_amount": 0,
        "total_evaluation_amount": round(nav, 4),
        "total_pnl_amount": brief["account_exposure_summary"]["total_pnl_amount"],
    }

    check_portfolio = read_json(source["path"] / "check-portfolio.json") or copy.deepcopy(portfolio)
    check_portfolio["holding"] = held
    market = copy.deepcopy(source["market"])
    market["run_id"] = output_dir.name
    previous_day = history[-1] if history else None
    previous_fills = copy.deepcopy(previous_day.get("fills", [])) if isinstance(previous_day, dict) else []
    previous_session = {
        "status": "partial" if previous_day else "unavailable",
        "session_date": str(previous_day.get("date") or "") if previous_day else "",
        "fill_collection_status": "complete" if previous_day else "unavailable",
        "fills": previous_fills if isinstance(previous_fills, list) else [],
        "realized_pnl": {
            "status": "unavailable",
            "scope": "symbol_session",
            "reason": "broker_realized_pnl_unavailable_in_virtual_replay" if previous_day else "no_previous_replay_session",
        },
        "errors": [],
    }
    empty_fills = {
        "schema_version": "1",
        "run_id": output_dir.name,
        "started_at": str(brief.get("started_at") or source["started_at"].isoformat()),
        "stage": "today-fills",
        "status": "success",
        "skipped": False,
        "fill_scope": "account",
        "fills": [],
        "previous_session": previous_session,
        "replay": True,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "decision-brief.json", brief)
    write_json(output_dir / "account-before-order.json", account)
    write_json(output_dir / "check-portfolio.json", check_portfolio)
    write_json(output_dir / "today-fills.json", empty_fills)
    write_json(output_dir / "market-index-snapshot.json", market)
    for name in ("price-chart.json",):
        source_path = source["path"] / name
        if source_path.is_file():
            shutil.copy2(source_path, output_dir / name)
    if market_news_context is not None:
        write_json(
            output_dir / "news-context.json",
            {
                "schema_version": "1",
                "status": market_news_context.get("context_status") or "partial",
                "generated_at": market_news_context.get("window_end") or "",
                "window_start": market_news_context.get("window_start") or "",
                "window_end": market_news_context.get("window_end") or "",
                "window_source": market_news_context.get("window_source") or "",
                "deduplicated_count": market_news_context.get("deduplicated_count") or 0,
                "market_news": {
                    "status": market_news_context.get("status") or "failed",
                    "db_path": "archived_market_news_sqlite",
                    "raw_count": market_news_context.get("raw_count") or 0,
                    "selected_count": market_news_context.get("selected_count") or 0,
                    "source_statuses": market_news_context.get("source_statuses") or {},
                    "items": market_news_context.get("items") or [],
                },
            },
        )
    return brief


def run_daily_agents(output_dir: Path, workspace_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    common = {
        "output_dir": output_dir,
        "decision_brief": "",
        "symbol_ids": "",
        "run_id": "",
        "started_at": "",
        "relative_paths": False,
        "review_extra_instructions_file": "",
    }
    first_args = SimpleNamespace(
        **common,
        workspace_dir=str(workspace_dir),
        pipeline_dir=str(PIPELINE_DIR),
        output=output_dir / "analyst-review-specs.json",
    )
    first_specs = build_run_artifacts.build_first_specs(first_args)
    for analyst_spec in first_specs.get("specs", []):
        analyst_spec["tool_policy"] = run_subagent.STRICT_ARTIFACT_TOOL_POLICY
    write_json(output_dir / "analyst-review-specs.json", first_specs)
    analyst_group = run_subagent.run_group(first_specs["specs"], max_workers=2)
    if analyst_group.get("status") != "success":
        raise RuntimeError(f"analyst agents failed: {analyst_group.get('wrappers')}")
    merge_args = SimpleNamespace(
        output_dir=output_dir,
        decision_brief="",
        symbol_ids="",
        output=output_dir / "analyst-review.json",
    )
    analyst = build_run_artifacts.build_analyst_review(merge_args)
    if str(analyst.get("status") or "") not in {"success", "partial"}:
        raise RuntimeError(f"analyst merge failed: {analyst.get('status')}")
    cutoff = align_replay_generated_at(analyst, read_json(output_dir / "decision-brief.json"))
    future_analyst_inputs = future_input_timestamps(analyst, cutoff, "analyst-review.json")
    if future_analyst_inputs:
        raise ValueError("analyst review after replay cutoff: " + ",".join(future_analyst_inputs))
    write_json(output_dir / "analyst-review.json", analyst)
    second_args = SimpleNamespace(
        **common,
        portfolio_json=str(output_dir / "check-portfolio.json"),
        analyst_review="",
        workspace_dir=str(workspace_dir),
        pipeline_dir=str(PIPELINE_DIR),
        strategy_policy_config="",
        output=output_dir / "judge-review-spec.json",
    )
    judge_spec = build_run_artifacts.build_second_spec(second_args)
    judge_spec["tool_policy"] = run_subagent.STRICT_ARTIFACT_TOOL_POLICY
    write_json(output_dir / "judge-review-spec.json", judge_spec)
    wrappers = list(analyst_group.get("wrappers", []))
    if not judge_spec.get("symbol_ids"):
        artifact = {
            "schema_version": "4",
            "run_id": output_dir.name,
            "started_at": read_json(output_dir / "decision-brief.json").get("started_at", ""),
            "stage": "judge-review",
            "status": "success",
            "skipped": True,
            "skip_reason": "no selected symbols",
            "errors": [],
            "symbols": [],
        }
        write_json(output_dir / "judge-review.json", artifact)
        return artifact, wrappers
    wrapper = run_subagent.run_one(judge_spec)
    wrappers.append(wrapper)
    if wrapper.get("status") != "success":
        raise RuntimeError(f"judge failed: {wrapper.get('errors')}")
    pipeline = object.__new__(Pipeline)
    pipeline.output_dir = output_dir
    pipeline.run_id = output_dir.name
    pipeline.started_at = str(judge_spec.get("started_at") or "")
    artifact = pipeline.write_judge_review(wrapper)
    return artifact, wrappers


def simulate_targets(
    state: dict[str, Any],
    judge: dict[str, Any],
    quotes: dict[str, dict[str, float]],
    decision_prices: dict[str, float],
    *,
    cost_bps: float,
) -> dict[str, Any]:
    """Fill the production Judge targets without adding a replay-only strategy rule."""
    opening_nav, missing = portfolio_value(state, decision_prices)
    if missing:
        raise ValueError(f"missing decision prices: {','.join(missing)}")
    cost_rate = cost_bps / 10_000.0
    turnover = 0.0
    costs = 0.0
    fills: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    candidates: list[tuple[int, str, dict[str, Any], int, int]] = []
    for item in judge.get("symbols", []) if isinstance(judge.get("symbols"), list) else []:
        if not isinstance(item, dict):
            continue
        symbol_id = symbol_key(item)
        current = as_int(state["positions"].get(symbol_id, {}).get("quantity"))
        target = as_int(item.get("final_holding_quantity"))
        direction = "sell" if target < current else "buy" if target > current else "hold"
        decisions.append(
            {
                "symbol_id": symbol_id,
                "symbol_name": str(item.get("symbol_name") or symbol_id),
                "judge_target_quantity": target,
                "current_quantity": current,
                "relative_attractiveness_rank": as_int(item.get("relative_attractiveness_rank")),
                "requested_action": direction,
                "reason_code": str(item.get("reason_code") or ""),
            }
        )
        if direction != "hold":
            candidates.append((0 if direction == "sell" else 1, symbol_id, item, current, target))

    for _priority, symbol_id, item, current, target in sorted(candidates):
        direction = "sell" if target < current else "buy"
        quote = quotes.get(symbol_id, {})
        fill_price = float(quote.get(f"{direction}_price") or 0.0)
        quote_quantity = int(quote.get(f"{direction}_quantity") or 0)
        desired_quantity = abs(target - current)
        fill_quantity = min(desired_quantity, quote_quantity)
        if direction == "buy":
            fill_quantity = min(
                fill_quantity,
                affordable_quantity(float(state["cash"]), fill_price * (1.0 + cost_rate)),
            )
        if fill_price <= 0 or fill_quantity <= 0:
            continue
        notional = fill_quantity * fill_price
        cost = notional * cost_rate
        if direction == "sell":
            position = state["positions"][symbol_id]
            position["quantity"] = as_int(position.get("quantity")) - fill_quantity
            state["cash"] = float(state["cash"]) + notional - cost
            if position["quantity"] <= 0:
                del state["positions"][symbol_id]
        else:
            old = state["positions"].get(symbol_id)
            old_quantity = as_int(old.get("quantity")) if old else 0
            old_average = float(old.get("average_price")) if old else 0.0
            new_quantity = old_quantity + fill_quantity
            state["positions"][symbol_id] = {
                "symbol_name": str(item.get("symbol_name") or symbol_id),
                "quantity": new_quantity,
                "average_price": (old_quantity * old_average + notional) / new_quantity,
            }
            state["cash"] = float(state["cash"]) - notional - cost
        fills.append(
            {
                "symbol_id": symbol_id,
                "symbol_name": str(item.get("symbol_name") or symbol_id),
                "direction": direction,
                "filled_quantity": fill_quantity,
                "filled_price": fill_price,
                "notional": notional,
                "modeled_cost": cost,
            }
        )
        turnover += notional
        costs += cost

    for decision in decisions:
        decision["simulated_final_quantity"] = as_int(
            state["positions"].get(decision["symbol_id"], {}).get("quantity")
        )
    return {
        "opening_nav": opening_nav,
        "gross_turnover_amount": turnover,
        "gross_turnover_pct": turnover / opening_nav * 100.0 if opening_nav > 0 else 0.0,
        "modeled_cost_amount": costs,
        "fills": fills,
        "decisions": decisions,
    }


def no_trade_value(initial: dict[str, Any], prices: dict[str, float]) -> tuple[float, list[str]]:
    return portfolio_value(initial, prices)


def rounded_metrics(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def build_result(
    config: dict[str, Any],
    replay_days: list[dict[str, Any]],
    state: dict[str, Any],
    original_state: dict[str, Any],
    daily: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    new_model_calls: int | None = None,
) -> dict[str, Any]:
    first = daily[0]
    last = daily[-1]
    replay_return = (last["closing_nav"] / first["opening_nav"] - 1.0) * 100.0
    benchmark_return = (last["benchmark_close"] / first["benchmark_open"] - 1.0) * 100.0
    close_prices = symbol_prices(replay_days[-1]["close"]["brief"], replay_days[-1]["close"]["account"])
    no_trade_final, no_trade_missing = no_trade_value(original_state, close_prices)
    no_trade_return = (
        (no_trade_final / first["opening_nav"] - 1.0) * 100.0 if not no_trade_missing else None
    )
    actual_open = number(replay_days[0]["decision"]["account"].get("account_summary", {}).get("total_evaluation_amount"))
    actual_close = number(replay_days[-1]["close"]["account"].get("account_summary", {}).get("total_evaluation_amount"))
    actual_return = (
        (actual_close / actual_open - 1.0) * 100.0
        if actual_open and actual_close and actual_open > 0
        else None
    )
    levels = [first["opening_nav"], *(item["closing_nav"] for item in daily)]
    actual_levels = [
        value
        for value in [
            actual_open,
            *(
                number(day["close"]["account"].get("account_summary", {}).get("total_evaluation_amount"))
                for day in replay_days
            ),
        ]
        if value is not None
    ]
    no_trade_levels = [first["opening_nav"]]
    for day in replay_days:
        day_prices = symbol_prices(day["close"]["brief"], day["close"]["account"])
        day_value, day_missing = no_trade_value(original_state, day_prices)
        if day_missing:
            no_trade_levels = []
            break
        no_trade_levels.append(day_value)
    benchmark_levels = [first["benchmark_open"], *(item["benchmark_close"] for item in daily)]
    observed_replay_levels = [
        value
        for item in daily
        for value in (item["opening_nav"], item["closing_nav"])
    ]
    turnover = sum(item["gross_turnover_amount"] for item in daily)
    total_overlay = last["closing_nav"] - no_trade_final if not no_trade_missing else None
    cost = sum(item["modeled_cost_amount"] for item in daily)
    token_usage: dict[str, int] = {}
    for wrapper in calls:
        usage = wrapper.get("token_usage") if isinstance(wrapper.get("token_usage"), dict) else {}
        for key, value in usage.items():
            token_usage[key] = token_usage.get(key, 0) + as_int(value)
    degraded_wrappers = sum(bool(item.get("degraded_dependencies")) for item in calls)
    degraded_dependency_codes = sorted(
        {
            str(dependency.get("error_code") or "unknown")
            for item in calls
            for dependency in (
                item.get("degraded_dependencies")
                if isinstance(item.get("degraded_dependencies"), list)
                else []
            )
            if isinstance(dependency, dict)
        }
    )
    news_gaps = [item["date"] for item in daily if item.get("market_news_status") != "supplied"]
    partial_sources = [
        item["date"]
        for item, source in zip(daily, replay_days)
        if str(source["decision"]["brief"].get("status") or "") == "partial"
    ]
    limitations = [
        "Single LLM sample; model nondeterminism is not estimated.",
        "Current Analyst/Judge prompts are replayed against archived point-in-time inputs; historical prompt bodies were not archived.",
        "Virtual fills use the next archived observation's best ask for buys and best bid for sells, capped by level-1 quantity.",
        "Virtual execution sells first and immediately reuses virtual sale proceeds; it does not reproduce stable production order sequencing, broker cash gates, or deferred retries.",
        f"A flat {config['cost_bps']} bps is modeled on each traded notional because archived broker fee/tax data is unavailable.",
        "Intraday emergency exits are outside this once-daily replay.",
        "Headline max drawdown uses the initial decision NAV and one archived closing NAV per trading day; decision-and-close observed-point MDD is also reported, but full intraday paths are unavailable.",
        "This historical sample does not establish out-of-sample performance.",
    ]
    if degraded_wrappers:
        limitations.append(
            f"Model wrappers reported degraded dependencies on {degraded_wrappers} calls "
            f"({', '.join(degraded_dependency_codes)}); wrapper success does not prove those dependencies were available."
        )
    if news_gaps:
        limitations.append("Market-news coverage was not complete on: " + ", ".join(news_gaps) + ".")
    if partial_sources:
        limitations.append("Archived decision input was partial on: " + ", ".join(partial_sources) + ".")
    return {
        "schema_version": "1",
        "mode": "single_sample_daily_closed_loop_agent_replay",
        "config": config,
        "coverage": {
            "trading_days": len(daily),
            "start_date": first["date"],
            "end_date": last["date"],
            "decision_cycles": len(daily),
            "expected_model_calls": len(daily) * 3,
            "observed_wrappers": len(calls),
            "new_model_calls": len(calls) if new_model_calls is None else new_model_calls,
            "failed_wrappers": sum(item.get("status") != "success" for item in calls),
            "degraded_wrappers": degraded_wrappers,
            "degraded_dependency_codes": degraded_dependency_codes,
            "token_usage": token_usage,
        },
        "strategies": {
            "replay": {
                "return_pct": rounded_metrics(replay_return),
                "kospi_excess_return_pct": rounded_metrics(replay_return - benchmark_return),
                "max_drawdown_pct": rounded_metrics(max_drawdown(levels)),
                "observed_decision_and_close_max_drawdown_pct": rounded_metrics(max_drawdown(observed_replay_levels)),
                "gross_turnover_amount": rounded_metrics(turnover),
                "gross_turnover_pct": rounded_metrics(turnover / first["opening_nav"] * 100.0),
                "stock_turnover_amount": rounded_metrics(turnover),
                "max_daily_gross_turnover_pct": rounded_metrics(max(item["gross_turnover_pct"] for item in daily)),
                "modeled_cost_amount": rounded_metrics(cost),
                "ending_value": rounded_metrics(last["closing_nav"]),
                "ending_cash_amount": last.get("ending_cash_amount"),
                "ending_cash_pct": last.get("ending_cash_pct"),
                "stock_selection_contribution_amount": rounded_metrics(total_overlay),
            },
            "same_boundary_actual": {
                "return_pct": rounded_metrics(actual_return),
                "kospi_excess_return_pct": rounded_metrics(actual_return - benchmark_return) if actual_return is not None else None,
                "max_drawdown_pct": rounded_metrics(max_drawdown(actual_levels)),
            },
            "no_trade": {
                "return_pct": rounded_metrics(no_trade_return),
                "kospi_excess_return_pct": rounded_metrics(no_trade_return - benchmark_return) if no_trade_return is not None else None,
                "max_drawdown_pct": rounded_metrics(max_drawdown(no_trade_levels)),
                "ending_value": rounded_metrics(no_trade_final) if not no_trade_missing else None,
                "missing_price_symbols": no_trade_missing,
            },
        },
        "benchmark": {
            "symbol": "KOSPI",
            "return_pct": rounded_metrics(benchmark_return),
            "max_drawdown_pct": rounded_metrics(max_drawdown(benchmark_levels)),
        },
        "comparisons": {
            "replay_minus_actual_pct_point": rounded_metrics(replay_return - actual_return) if actual_return is not None else None,
            "replay_minus_no_trade_pct_point": rounded_metrics(replay_return - no_trade_return) if no_trade_return is not None else None,
        },
        "daily": daily,
        "limitations": limitations,
    }


def markdown_report(result: dict[str, Any]) -> str:
    replay = result["strategies"]["replay"]
    actual = result["strategies"]["same_boundary_actual"]
    no_trade = result["strategies"]["no_trade"]
    benchmark = result["benchmark"]
    comparison_rows = [
        f"| replay | {replay['return_pct']:.4f}% | {replay['kospi_excess_return_pct']:.4f}%p | {replay['max_drawdown_pct']:.4f}% | {replay['gross_turnover_pct']:.4f}% |",
    ]
    comparison_rows.extend(
        [
            f"| same-boundary actual | {actual['return_pct']:.4f}% | {actual['kospi_excess_return_pct']:.4f}%p | {actual['max_drawdown_pct']:.4f}% | - |",
            f"| no-trade | {no_trade['return_pct']:.4f}% | {no_trade['kospi_excess_return_pct']:.4f}%p | {no_trade['max_drawdown_pct']:.4f}% | 0% |",
            f"| KOSPI | {benchmark['return_pct']:.4f}% | - | {benchmark['max_drawdown_pct']:.4f}% | - |",
        ]
    )
    lines = [
        "# Daily agent replay backtest",
        "",
        f"- 기간: {result['coverage']['start_date']} ~ {result['coverage']['end_date']} ({result['coverage']['trading_days']}거래일)",
        f"- 판단: 거래일당 1회, 총 {result['coverage']['decision_cycles']}회; Analyst 2 + Judge 1",
        f"- 모델 wrapper: 총 {result['coverage']['observed_wrappers']}개; 이번 실행 신규 {result['coverage']['new_model_calls']}개",
        "",
        "| 전략 | 수익률 | KOSPI 초과수익 | MDD | 총회전율 |",
        "|---|---:|---:|---:|---:|",
        *comparison_rows,
        "",
        f"모형 비용: {replay['modeled_cost_amount']:,.2f}원",
        f"결정·종가 관측점 MDD: {replay['observed_decision_and_close_max_drawdown_pct']:.4f}%",
        "",
        "## Limitations",
        "",
        *(f"- {item}" for item in result["limitations"]),
        "",
    ]
    return "\n".join(lines)


def backtest(args: argparse.Namespace) -> dict[str, Any]:
    rows = discover_run_rows(args.runs_root)
    days = select_replay_days(rows, args.start, args.end, args.decision_time)
    if not days:
        raise ValueError("no replay days found")
    expected_dates = [item["date"] for item in days]
    decision_history = daily_decision_history(rows, args.decision_time)
    history = [(item["date"], item["benchmark"]) for item in decision_history]
    strategy_policy, strategy_policy_path = build_run_artifacts.load_strategy_policy_config("")
    turnover_reference_pct = float(
        strategy_policy.get("performance_review", {}).get("max_daily_gross_turnover_pct") or 0
    )
    config = {
        "strategy_mode": "production_judge_targets",
        "strategy_policy_path": str(strategy_policy_path),
        "strategy_policy_sha256": build_run_artifacts.file_sha256(strategy_policy_path),
        "runs_root": str(args.runs_root),
        "market_news_db": str(args.market_news_db),
        "workspace_dir": str(args.workspace_dir),
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "decision_time_kst": args.decision_time.strftime("%H:%M:%S"),
        "decision_time_tolerance_seconds": DECISION_TIME_TOLERANCE_SECONDS,
        "information_cutoff": "archived_decision_brief_generated_at",
        "fill_observation": "next archived run after information cutoff",
        "fill_price": "buy=best_ask,sell=best_bid",
        "fill_quantity_cap": "level_1_quantity",
        "cost_bps": args.cost_bps,
        "daily_gross_turnover_reference_pct": turnover_reference_pct,
        "source_dates": expected_dates,
    }
    args.output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_root / "manifest.json"
    previous_manifest = read_json(manifest_path)
    if previous_manifest and previous_manifest != config:
        raise ValueError("output root already contains a replay with different configuration")
    write_json(manifest_path, config)

    state = initial_state(days[0])
    original_state = copy.deepcopy(state)
    daily: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    progress = read_json(args.output_root / "progress.json")
    if progress:
        completed_dates = progress.get("completed_dates")
        if not isinstance(completed_dates, list) or completed_dates != expected_dates[: len(completed_dates)]:
            raise ValueError("progress dates do not match replay configuration")
        state = progress.get("state") if isinstance(progress.get("state"), dict) else state
        original_state = progress.get("original_state") if isinstance(progress.get("original_state"), dict) else original_state
        daily = progress.get("daily") if isinstance(progress.get("daily"), list) else []
        calls = progress.get("calls") if isinstance(progress.get("calls"), list) else []
    previous_call_count = len(calls)

    for day_index, day in enumerate(days[len(daily) :], start=len(daily)):
        output_dir = args.output_root / "runs" / f"{day['date'].replace('-', '')}T090500+0900-replay"
        benchmark_return_20 = trailing_return(
            history,
            day["date"],
            20,
            current_value=float(day["decision"]["benchmark"]),
        )
        information_cutoff = decision_information_cutoff(day["decision"])
        previous_started_at = decision_information_cutoff(days[day_index - 1]["decision"]) if day_index else None
        market_news_context = rebuilt_market_news_context(
            args.market_news_db,
            information_cutoff,
            previous_started_at,
        )
        print(f"[{day_index + 1}/{len(days)}] {day['date']} virtual input", flush=True)
        brief = virtualize_inputs(
            day,
            output_dir,
            state,
            daily,
            benchmark_return_20,
            market_news_context,
            turnover_reference_pct,
        )
        future_inputs = [
            timestamp
            for name in (
                "decision-brief.json",
                "account-before-order.json",
                "check-portfolio.json",
                "today-fills.json",
                "market-index-snapshot.json",
                "price-chart.json",
                "news-context.json",
            )
            for path in (output_dir / name,)
            if path.is_file()
            for timestamp in future_input_timestamps(read_json(path), information_cutoff, path.name)
        ]
        if future_inputs:
            raise ValueError(f"{day['date']}: replay input after information cutoff: {','.join(future_inputs)}")
        print(f"[{day_index + 1}/{len(days)}] {day['date']} Analyst 2 + Judge 1", flush=True)
        judge, wrappers = run_daily_agents(output_dir, args.workspace_dir)
        calls.extend(wrappers)
        decision_prices = symbol_prices(brief, read_json(output_dir / "account-before-order.json"))
        simulation = simulate_targets(
            state,
            judge,
            fill_quotes(day["fill"]["brief"]),
            decision_prices,
            cost_bps=args.cost_bps,
        )
        recorded_fills = [
            {
                **fill,
                "filled_at": day["fill"]["started_at"].isoformat(),
                "fill_source": "next_archived_level_1_quote",
            }
            for fill in simulation["fills"]
        ]
        close_prices = symbol_prices(day["close"]["brief"], day["close"]["account"])
        closing_nav, missing = portfolio_value(state, close_prices)
        if missing:
            raise ValueError(f"{day['date']}: missing closing prices for virtual holdings: {','.join(missing)}")
        day_result = {
            "date": day["date"],
            "source_run_id": day["decision"]["path"].name,
            "decision_started_at": day["decision"]["started_at"].isoformat(),
            "fill_observation_started_at": day["fill"]["started_at"].isoformat(),
            "close_observation_started_at": day["close"]["started_at"].isoformat(),
            "opening_nav": rounded_metrics(simulation["opening_nav"]),
            "closing_nav": rounded_metrics(closing_nav),
            "benchmark_open": day["decision"]["benchmark"],
            "benchmark_close": day["close"]["benchmark"],
            "benchmark_return_20_period_pct": rounded_metrics(benchmark_return_20),
            "market_news_status": market_news_context.get("status"),
            "market_news_selected_count": market_news_context.get("selected_count"),
            "gross_turnover_amount": rounded_metrics(simulation["gross_turnover_amount"]),
            "gross_turnover_pct": rounded_metrics(simulation["gross_turnover_pct"]),
            "modeled_cost_amount": rounded_metrics(simulation["modeled_cost_amount"]),
            "fills": recorded_fills,
            "ending_cash_amount": rounded_metrics(float(state["cash"])),
            "ending_cash_pct": rounded_metrics(float(state["cash"]) / closing_nav * 100.0 if closing_nav > 0 else 0.0),
            "simulated_decisions": simulation["decisions"],
            "analyst_status": read_json(output_dir / "analyst-review.json").get("status"),
            "judge_status": judge.get("status"),
        }
        write_json(
            output_dir / "simulated-judge-review.json",
            {
                "schema_version": "1",
                "run_id": output_dir.name,
                "started_at": day["decision"]["started_at"].isoformat(),
                "status": "success",
                "simulation": config,
                "symbols": simulation["decisions"],
                "fills": recorded_fills,
            },
        )
        fills_payload = read_json(output_dir / "today-fills.json")
        fills_payload["fills"] = recorded_fills
        write_json(output_dir / "today-fills.json", fills_payload)
        daily.append(day_result)
        write_json(
            args.output_root / "progress.json",
            {
                "completed_dates": [item["date"] for item in daily],
                "state": state,
                "original_state": original_state,
                "daily": daily,
                "calls": calls,
            },
        )
        print(
            f"[{day_index + 1}/{len(days)}] {day['date']} close={closing_nav:,.0f} turnover={simulation['gross_turnover_pct']:.2f}% fills={len(simulation['fills'])}",
            flush=True,
        )

    result = build_result(
        config,
        days,
        state,
        original_state,
        daily,
        calls,
        new_model_calls=len(calls) - previous_call_count,
    )
    write_json(args.output_root / "backtest-result.json", result)
    (args.output_root / "backtest-report.md").write_text(markdown_report(result), encoding="utf-8")
    return result


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_time(value: str) -> time:
    return time.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay one archived Analyst/Judge decision cycle per trading day.")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--workspace-dir", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--market-news-db", type=Path, default=DEFAULT_MARKET_NEWS_DB)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--start", type=parse_date, default=date(2026, 8, 4))
    parser.add_argument("--end", type=parse_date, default=date(2026, 9, 1))
    parser.add_argument("--decision-time", type=parse_time, default=time(9, 5))
    parser.add_argument("--cost-bps", type=float, default=20.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.start > args.end:
        raise SystemExit("--start must be on or before --end")
    if args.cost_bps < 0:
        raise SystemExit("--cost-bps must be non-negative")
    result = backtest(args)
    print(json.dumps(result["strategies"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
