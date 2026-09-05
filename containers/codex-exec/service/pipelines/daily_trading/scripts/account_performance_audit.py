#!/usr/bin/env python3
"""Read-only account performance audit and buy-and-hold backtest."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, time
from pathlib import Path
from statistics import median
from typing import Any

try:
    from .account_performance import (
        KST,
        _fill_key,
        _json,
        _number,
        _started_at,
        excluded_external_flow_dates,
    )
except ImportError:  # pragma: no cover - direct script fallback
    from account_performance import (  # type: ignore
        KST,
        _fill_key,
        _json,
        _number,
        _started_at,
        excluded_external_flow_dates,
    )


COMPLETED_DAY_AT = time(19, 30)


def _positive_number(value: Any) -> float | None:
    parsed = _number(value)
    return parsed if parsed is not None and parsed > 0 else None


def _symbol_key(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("symbol_id") or item.get("code") or "").strip()


def _artifact_available(payload: dict[str, Any], cutoff: datetime) -> bool:
    for field in ("observed_at", "generated_at"):
        available_at = _started_at(payload.get(field))
        if available_at is not None and available_at > cutoff:
            return False
    return True


def _account_prices_and_positions(
    account: dict[str, Any],
    decision_brief: dict[str, Any],
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    prices: dict[str, float] = {}
    positions: dict[str, dict[str, float]] = {}
    account_positions = [
        *(account.get("symbols", []) if isinstance(account.get("symbols"), list) else []),
        *(
            account.get("non_universe_account_positions", [])
            if isinstance(account.get("non_universe_account_positions"), list)
            else []
        ),
    ]
    for item in account_positions:
        if not isinstance(item, dict):
            continue
        symbol = _symbol_key(item)
        if not symbol:
            continue
        quantity = _number(item.get("current_live_holding_quantity")) or 0.0
        valuation = _number(item.get("valuation_amount")) or 0.0
        price = _positive_number(item.get("current_price"))
        if price is None and quantity > 0 and valuation > 0:
            price = valuation / quantity
        if price is not None:
            prices[symbol] = price
        if quantity > 0:
            positions[symbol] = {"quantity": quantity, "valuation_amount": valuation}

    for item in decision_brief.get("symbols", []) if isinstance(decision_brief.get("symbols"), list) else []:
        if not isinstance(item, dict):
            continue
        symbol = _symbol_key(item)
        price_payload = item.get("price") if isinstance(item.get("price"), dict) else {}
        price = _positive_number(price_payload.get("current_or_last"))
        if symbol and price is not None:
            prices.setdefault(symbol, price)
    return prices, positions


def _fill_date(fill: dict[str, Any]) -> str:
    filled_at = _started_at(fill.get("filled_at"))
    if filled_at is not None:
        return filled_at.date().isoformat()
    order_date = str(fill.get("order_date") or "")
    if len(order_date) == 8 and order_date.isdigit():
        return f"{order_date[:4]}-{order_date[4:6]}-{order_date[6:]}"
    return ""


def _collect_days_and_fills(
    runs_root: Path,
    cutoff: datetime,
    benchmark: str,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    raw_days: dict[str, list[dict[str, Any]]] = {}
    fills: dict[tuple[Any, ...], tuple[float, str]] = {}
    fill_collections: dict[str, list[tuple[datetime, bool]]] = {}
    if not runs_root.is_dir():
        return [], {}

    for run_dir in (path for path in runs_root.iterdir() if path.is_dir()):
        account = _json(run_dir / "account-before-order.json")
        if not _artifact_available(account, cutoff):
            continue
        started = _started_at(account.get("started_at"))
        if started is None:
            started = _started_at((_json(run_dir / "pipeline-summary.json")).get("started_at"))
        if started is None or started > cutoff:
            continue

        today_fills = _json(run_dir / "today-fills.json")
        if _artifact_available(today_fills, cutoff):
            session_date = started.date().isoformat()
            fill_collections.setdefault(session_date, []).append(
                (
                    started,
                    today_fills.get("fill_scope") == "account"
                    and today_fills.get("status") == "success"
                    and today_fills.get("skipped") is not True,
                )
            )
        if _artifact_available(today_fills, cutoff) and today_fills.get("fill_scope") == "account":
            candidates = list(today_fills.get("fills", [])) if isinstance(today_fills.get("fills"), list) else []
            previous = today_fills.get("previous_session")
            if isinstance(previous, dict) and isinstance(previous.get("fills"), list):
                candidates.extend(previous["fills"])
            artifact_fills: dict[tuple[Any, ...], dict[tuple[Any, ...], tuple[float, str]]] = {}
            for fill in candidates:
                if not isinstance(fill, dict) or fill.get("direction") not in {"buy", "sell"}:
                    continue
                fill_started = _started_at(fill.get("filled_at"))
                if fill_started is not None and fill_started > cutoff:
                    continue
                amount = _positive_number(fill.get("filled_amount"))
                if amount is None:
                    quantity = _positive_number(fill.get("filled_quantity"))
                    price = _positive_number(fill.get("filled_price"))
                    amount = quantity * price if quantity is not None and price is not None else None
                session_date = _fill_date(fill)
                if amount is None or not session_date:
                    continue
                key = _fill_key(fill)
                variant_key = (
                    str(fill.get("filled_at") or ""),
                    _number(fill.get("filled_quantity")),
                    _number(fill.get("filled_price")),
                    amount,
                    str(fill.get("exchange_id") or ""),
                    str(fill.get("order_employee_no") or ""),
                )
                artifact_fills.setdefault(key, {})[variant_key] = (amount, session_date)
            for key, variants in artifact_fills.items():
                amount = sum(item[0] for item in variants.values())
                session_date = next(iter(variants.values()))[1]
                if key not in fills or amount > fills[key][0]:
                    fills[key] = (amount, session_date)

        summary = account.get("account_summary") if isinstance(account.get("account_summary"), dict) else {}
        total = _positive_number(summary.get("total_evaluation_amount"))
        securities = _number(summary.get("securities_valuation_amount"))
        if (
            total is None
            or account.get("skipped") is True
            or str(account.get("status") or "") not in {"success", "partial"}
        ):
            continue
        market = _json(run_dir / "market-index-snapshot.json")
        if not _artifact_available(market, cutoff):
            continue
        benchmark_value = next(
            (
                _positive_number(item.get("value"))
                for item in market.get("indexes", [])
                if isinstance(item, dict)
                and str(item.get("symbol") or "").upper() == benchmark
                and str(item.get("status") or "") == "success"
            ),
            None,
        )
        if benchmark_value is None:
            continue
        decision_brief = _json(run_dir / "decision-brief.json")
        if not _artifact_available(decision_brief, cutoff):
            decision_brief = {}
        prices, positions = _account_prices_and_positions(account, decision_brief)
        raw_days.setdefault(started.date().isoformat(), []).append(
            {
                "started_at": started,
                "total": total,
                "securities": securities,
                "benchmark": benchmark_value,
                "prices": prices,
                "positions": positions,
            }
        )

    turnover_by_date: dict[str, float] = {}
    for amount, session_date in fills.values():
        turnover_by_date[session_date] = turnover_by_date.get(session_date, 0.0) + amount
    turnover_complete_by_date = {
        session_date: max(collections, key=lambda item: item[0])[1]
        for session_date, collections in fill_collections.items()
    }

    days = []
    for session_date in sorted(raw_days):
        snapshots = sorted(raw_days[session_date], key=lambda item: item["started_at"])
        latest = snapshots[-1]["started_at"]
        status = (
            "non_trading_observation"
            if latest.weekday() >= 5
            else "provisional"
            if latest.date() == cutoff.date()
            and latest.timetz().replace(tzinfo=None) < COMPLETED_DAY_AT
            else "missing_trading_day"
            if len(snapshots) < 2
            else "available"
        )
        days.append(
            {
                "date": session_date,
                "status": status,
                "snapshot_count": len(snapshots),
                "opening": snapshots[0],
                "closing": snapshots[-1],
                "closing_prices": dict(snapshots[-1]["prices"]),
                "gross_turnover_amount": turnover_by_date.get(session_date, 0.0),
                "turnover_collection_status": (
                    "complete" if turnover_complete_by_date.get(session_date, False) else "partial"
                ),
            }
        )
    return days, turnover_by_date


def _metrics_from_levels(
    opening: float,
    closings: list[tuple[str, float]],
    excluded_dates: set[str],
) -> tuple[float | None, float | None]:
    if opening <= 0 or not closings:
        return None, None
    previous = opening
    level = peak = 1.0
    maximum_drawdown = 0.0
    included = 0
    for session_date, closing in closings:
        if closing <= 0:
            return None, None
        if session_date in excluded_dates:
            previous = closing
            continue
        daily_return = closing / previous
        level *= daily_return
        peak = max(peak, level)
        maximum_drawdown = max(maximum_drawdown, (peak - level) / peak * 100.0)
        previous = closing
        included += 1
    if included == 0:
        return None, None
    return round((level - 1.0) * 100.0, 4), round(maximum_drawdown, 4)


def _no_trade_levels(days: list[dict[str, Any]]) -> tuple[list[tuple[str, float]], list[str]]:
    opening = days[0]["opening"]
    positions = opening["positions"]
    invested = sum(item["valuation_amount"] for item in positions.values())
    cash = opening["total"] - invested
    missing: set[str] = set()
    levels: list[tuple[str, float]] = []
    for day in days:
        value = cash
        for symbol, position in positions.items():
            price = day["closing_prices"].get(symbol)
            if price is None:
                missing.add(symbol)
                continue
            value += position["quantity"] * price
        levels.append((day["date"], value))
    return levels, sorted(missing)


def _strategy_metrics(
    *,
    return_pct: float | None,
    drawdown_pct: float | None,
    benchmark_return_pct: float | None,
    turnover_amount: float | None,
    opening_value: float,
    max_daily_turnover_pct: float | None,
) -> dict[str, Any]:
    return {
        "return_pct": return_pct,
        "kospi_excess_return_pct": (
            round(return_pct - benchmark_return_pct, 4)
            if return_pct is not None and benchmark_return_pct is not None
            else None
        ),
        "max_drawdown_pct": drawdown_pct,
        "gross_turnover_amount": round(turnover_amount) if turnover_amount is not None else None,
        "gross_turnover_pct": (
            round(turnover_amount / opening_value * 100.0, 4)
            if turnover_amount is not None and opening_value > 0
            else None
        ),
        "max_daily_gross_turnover_pct": (
            round(max_daily_turnover_pct, 4) if max_daily_turnover_pct is not None else None
        ),
    }


def _window_result(
    days: list[dict[str, Any]],
    excluded_dates: set[str],
    missing_trading_dates: set[str],
) -> dict[str, Any]:
    opening = days[0]["opening"]
    actual_closings = [(day["date"], day["closing"]["total"]) for day in days]
    benchmark_closings = [(day["date"], day["closing"]["benchmark"]) for day in days]
    actual_return, actual_drawdown = _metrics_from_levels(opening["total"], actual_closings, excluded_dates)
    benchmark_return, _ = _metrics_from_levels(opening["benchmark"], benchmark_closings, excluded_dates)
    no_trade_closings, missing_prices = _no_trade_levels(days)
    no_trade_return, no_trade_drawdown = (
        _metrics_from_levels(opening["total"], no_trade_closings, set())
        if not missing_prices
        else (None, None)
    )
    turnover_complete = all(day["turnover_collection_status"] == "complete" for day in days)
    turnover_amount = sum(day["gross_turnover_amount"] for day in days) if turnover_complete else None
    daily_turnover = [
        day["gross_turnover_amount"] / day["opening"]["total"] * 100.0
        for day in days
        if day["opening"]["total"] > 0
    ]
    actual = _strategy_metrics(
        return_pct=actual_return,
        drawdown_pct=actual_drawdown,
        benchmark_return_pct=benchmark_return,
        turnover_amount=turnover_amount,
        opening_value=opening["total"],
        max_daily_turnover_pct=max(daily_turnover, default=0.0) if turnover_complete else None,
    )
    no_trade = _strategy_metrics(
        return_pct=no_trade_return,
        drawdown_pct=no_trade_drawdown,
        benchmark_return_pct=benchmark_return,
        turnover_amount=0.0,
        opening_value=opening["total"],
        max_daily_turnover_pct=0.0,
    )
    coverage_gaps = sorted(
        session_date
        for session_date in missing_trading_dates
        if days[0]["date"] <= session_date <= days[-1]["date"]
    )
    coverage_complete = not coverage_gaps and turnover_complete
    return {
        "start_date": days[0]["date"],
        "end_date": days[-1]["date"],
        "trading_days": len(days),
        "opening_account_value": round(opening["total"]),
        "closing_account_value": round(days[-1]["closing"]["total"]),
        "benchmark_return_pct": benchmark_return,
        "strategies": {"actual": actual, "no_trade": no_trade},
        "actual_minus_no_trade_return_pct": (
            round(actual_return - no_trade_return, 4)
            if actual_return is not None and no_trade_return is not None
            else None
        ),
        "coverage_status": "complete" if coverage_complete else "partial",
        "missing_trading_day_dates": coverage_gaps,
        "turnover_collection_status": "complete" if turnover_complete else "partial",
        "no_trade_missing_price_symbols": missing_prices,
    }


def build_performance_audit(
    *,
    runs_root: Path,
    workspace_dir: Path,
    cutoff: datetime,
    benchmark: str = "KOSPI",
    window: int = 20,
) -> dict[str, Any]:
    if window <= 0:
        raise ValueError("window must be positive")
    benchmark = benchmark.upper()
    days, _ = _collect_days_and_fills(runs_root, cutoff, benchmark)
    completed = [day for day in days if day["status"] == "available"]
    missing_trading_dates = {day["date"] for day in days if day["status"] == "missing_trading_day"}
    excluded_dates = excluded_external_flow_dates(workspace_dir)
    windows = [
        _window_result(
            completed[index : index + window],
            excluded_dates,
            missing_trading_dates,
        )
        for index in range(max(0, len(completed) - window + 1))
    ]
    valid_windows = [
        item
        for item in windows
        if item["strategies"]["actual"]["return_pct"] is not None
        and item["strategies"]["no_trade"]["return_pct"] is not None
        and item["benchmark_return_pct"] is not None
        and item["coverage_status"] == "complete"
    ]
    actual_excesses = [item["strategies"]["actual"]["kospi_excess_return_pct"] for item in valid_windows]
    actual_minus_hold = [item["actual_minus_no_trade_return_pct"] for item in valid_windows]
    return {
        "schema_version": "1",
        "stage": "account-performance-audit",
        "mode": "historical_completed_days",
        "as_of": cutoff.isoformat(),
        "benchmark_index": benchmark,
        "window_trading_days": window,
        "live_behavior_impact": "none; this audit is not supplied to Judge or execution",
        "data_quality": {
            "observed_dates": len(days),
            "completed_trading_days": len(completed),
            "insufficient_snapshot_dates": [
                day["date"]
                for day in days
                if day["status"] in {"missing_trading_day", "non_trading_observation"}
            ],
            "missing_trading_day_dates": sorted(missing_trading_dates),
            "non_trading_observation_dates": [
                day["date"] for day in days if day["status"] == "non_trading_observation"
            ],
            "provisional_dates": [day["date"] for day in days if day["status"] == "provisional"],
            "excluded_external_flow_dates": sorted(excluded_dates),
        },
        "full_period": _window_result(completed, excluded_dates, missing_trading_dates) if completed else None,
        "latest_window": windows[-1] if windows else None,
        "rolling_windows": {
            "count": len(windows),
            "valid_count": len(valid_windows),
            "actual_beats_kospi_count": sum(value > 0 for value in actual_excesses),
            "actual_beats_kospi_rate_pct": round(sum(value > 0 for value in actual_excesses) / len(valid_windows) * 100.0, 4)
            if valid_windows
            else None,
            "actual_beats_no_trade_count": sum(value > 0 for value in actual_minus_hold),
            "actual_beats_no_trade_rate_pct": round(sum(value > 0 for value in actual_minus_hold) / len(valid_windows) * 100.0, 4)
            if valid_windows
            else None,
            "actual_beats_both_count": sum(
                excess > 0 and versus_hold > 0
                for excess, versus_hold in zip(actual_excesses, actual_minus_hold, strict=True)
            ),
            "actual_beats_both_rate_pct": round(
                sum(
                    excess > 0 and versus_hold > 0
                    for excess, versus_hold in zip(actual_excesses, actual_minus_hold, strict=True)
                )
                / len(valid_windows)
                * 100.0,
                4,
            )
            if valid_windows
            else None,
            "median_actual_kospi_excess_return_pct": round(median(actual_excesses), 4) if actual_excesses else None,
            "median_actual_minus_no_trade_return_pct": round(median(actual_minus_hold), 4) if actual_minus_hold else None,
        },
        "limitations": [
            "No-trade keeps the opening holdings and cash unchanged and marks them with archived prices.",
            "Dividends, corporate actions, borrow costs, and unavailable prices are not estimated.",
            "Actual turnover deduplicates account fills from both current- and previous-session evidence.",
            "Turnover is null when the latest account-wide fill collection for any included date is incomplete.",
            "Historical dates use their latest archived snapshot; the cutoff date additionally requires 19:30 KST.",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--workspace-dir", type=Path)
    parser.add_argument("--as-of", help="ISO timestamp. Defaults to the current KST time.")
    parser.add_argument("--benchmark", default="KOSPI")
    parser.add_argument("--window", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cutoff = _started_at(args.as_of) if args.as_of else datetime.now(KST)
    if cutoff is None:
        raise SystemExit("--as-of must be an ISO timestamp")
    workspace_dir = (args.workspace_dir or args.runs_root.parent.parent).resolve()
    result = build_performance_audit(
        runs_root=args.runs_root.resolve(),
        workspace_dir=workspace_dir,
        cutoff=cutoff,
        benchmark=args.benchmark,
        window=args.window,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
