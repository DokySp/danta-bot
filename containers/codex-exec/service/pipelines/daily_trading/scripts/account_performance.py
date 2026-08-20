#!/usr/bin/env python3
"""Domestic trading-account performance from existing daily run artifacts."""

from __future__ import annotations

import fcntl
import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
EXTERNAL_FLOW_LEDGER = Path("memory/account-performance/external-flows.jsonl")


def _json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _started_at(value: Any) -> datetime | None:
    text = str(value or "").strip().replace(" KST", "+09:00")
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed.astimezone(KST))


def parse_session_date(value: str) -> date:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc


def external_flow_ledger_path(workspace_dir: Path) -> Path:
    return workspace_dir / EXTERNAL_FLOW_LEDGER


def record_external_flow(workspace_dir: Path, session_date: date, action: str) -> Path:
    if action not in {"exclude", "clear"}:
        raise ValueError(f"unsupported external-flow action: {action}")
    path = external_flow_ledger_path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "schema_version": "1",
        "session_date": session_date.isoformat(),
        "action": action,
        "recorded_at": datetime.now(KST).isoformat(timespec="seconds"),
    }
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return path


def excluded_external_flow_dates(workspace_dir: Path) -> set[str]:
    path = external_flow_ledger_path(workspace_dir)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    state: dict[str, bool] = {}
    for line in lines:
        try:
            row = json.loads(line)
            session_date = parse_session_date(str(row.get("session_date") or "")).isoformat()
        except (json.JSONDecodeError, ValueError, AttributeError):
            continue
        action = str(row.get("action") or "")
        if action in {"exclude", "clear"}:
            state[session_date] = action == "exclude"
    return {session_date for session_date, excluded in state.items() if excluded}


def _fill_key(fill: dict[str, Any]) -> tuple[Any, ...]:
    order_id = str(fill.get("order_id") or "").strip()
    if order_id:
        return (
            str(fill.get("order_date") or ""),
            order_id,
            str(fill.get("symbol_id") or ""),
            str(fill.get("direction") or ""),
        )
    return (
        str(fill.get("filled_at") or ""),
        str(fill.get("symbol_id") or ""),
        str(fill.get("direction") or ""),
        _number(fill.get("filled_quantity")),
        _number(fill.get("filled_price")),
    )


def _compound_return(values: list[float]) -> float | None:
    if not values:
        return None
    factor = 1.0
    for value in values:
        factor *= 1.0 + value / 100.0
    return round((factor - 1.0) * 100.0, 4)


def _max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    level = peak = 1.0
    maximum = 0.0
    for value in values:
        level *= 1.0 + value / 100.0
        peak = max(peak, level)
        maximum = max(maximum, (peak - level) / peak * 100.0)
    return round(maximum, 4)


def _daily_observations(
    runs_root: Path,
    cutoff: datetime,
    benchmark: str,
    excluded_dates: set[str],
) -> list[dict[str, Any]]:
    days: dict[str, dict[str, Any]] = {}
    if not runs_root.is_dir():
        return []
    for run_dir in (path for path in runs_root.iterdir() if path.is_dir()):
        account = _json(run_dir / "account-before-order.json")
        summary = account.get("account_summary") if isinstance(account.get("account_summary"), dict) else {}
        total = _number(summary.get("total_evaluation_amount"))
        started = _started_at(account.get("started_at"))
        if started is None:
            started = _started_at((_json(run_dir / "pipeline-summary.json")).get("started_at"))
        if started is None or started > cutoff or total is None or total <= 0:
            continue
        if account.get("skipped") is True or str(account.get("status") or "") not in {"success", "partial"}:
            continue
        session_date = started.date().isoformat()
        day = days.setdefault(
            session_date,
            {"date": session_date, "snapshots": [], "indexes": [], "fills": {}, "fill_collections": []},
        )
        securities = _number(summary.get("securities_valuation_amount"))
        holdings = []
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
            valuation = _number(item.get("valuation_amount"))
            if valuation is not None and valuation >= 0:
                holdings.append(
                    {
                        "symbol_id": str(item.get("symbol_id") or ""),
                        "symbol_name": str(item.get("symbol_name") or item.get("symbol_id") or ""),
                        "valuation_amount": valuation,
                    }
                )
        day["snapshots"].append(
            {
                "started_at": started,
                "total_evaluation_amount": total,
                "securities_valuation_amount": securities,
                "holdings": holdings,
            }
        )

        market = _json(run_dir / "market-index-snapshot.json")
        for item in market.get("indexes", []) if isinstance(market.get("indexes"), list) else []:
            if not isinstance(item, dict) or str(item.get("symbol") or "").upper() != benchmark:
                continue
            value = _number(item.get("value"))
            if str(item.get("status") or "") == "success" and value is not None and value > 0:
                day["indexes"].append({"started_at": started, "value": value})

        fills = _json(run_dir / "today-fills.json")
        if fills.get("skipped") is not True and fills.get("fill_scope") == "account":
            day["fill_collections"].append((started, fills.get("status") == "success"))
            for fill in fills.get("fills", []) if isinstance(fills.get("fills"), list) else []:
                if not isinstance(fill, dict) or fill.get("direction") not in {"buy", "sell"}:
                    continue
                amount = _number(fill.get("filled_amount"))
                if amount is None or amount <= 0:
                    quantity = _number(fill.get("filled_quantity"))
                    price = _number(fill.get("filled_price"))
                    amount = quantity * price if quantity and price else None
                if amount is None or amount <= 0:
                    continue
                key = _fill_key(fill)
                previous = day["fills"].get(key)
                if previous is None or amount > previous["amount"]:
                    day["fills"][key] = {"direction": fill.get("direction"), "amount": amount}

    result = []
    for session_date in sorted(days):
        day = days[session_date]
        snapshots = sorted(day["snapshots"], key=lambda item: item["started_at"])
        indexes = sorted(day["indexes"], key=lambda item: item["started_at"])
        opening = snapshots[0]["total_evaluation_amount"]
        closing = snapshots[-1]["total_evaluation_amount"]
        status = "excluded_external_flow" if session_date in excluded_dates else "available" if len(snapshots) >= 2 else "insufficient_snapshots"
        account_return = round((closing / opening - 1.0) * 100.0, 4) if status == "available" else None
        benchmark_return = (
            round((indexes[-1]["value"] / indexes[0]["value"] - 1.0) * 100.0, 4)
            if status == "available" and len(indexes) >= 2
            else None
        )
        gross_turnover_amount = sum(item["amount"] for item in day["fills"].values())
        fill_collections = sorted(day["fill_collections"], key=lambda item: item[0])
        turnover_complete = bool(fill_collections and fill_collections[-1][1])
        observed_turnover_pct = round(gross_turnover_amount / opening * 100.0, 4)
        result.append(
            {
                "date": session_date,
                "status": status,
                "snapshot_count": len(snapshots),
                "opening_total_evaluation_amount": round(opening),
                "closing_total_evaluation_amount": round(closing),
                "account_return_pct": account_return,
                "benchmark_return_pct": benchmark_return,
                "gross_turnover_amount": round(gross_turnover_amount) if turnover_complete else None,
                "gross_turnover_pct": observed_turnover_pct if turnover_complete else None,
                "observed_gross_turnover_amount": round(gross_turnover_amount),
                "observed_gross_turnover_pct": observed_turnover_pct,
                "turnover_collection_status": "complete" if turnover_complete else "partial",
                "closing_snapshot": snapshots[-1],
            }
        )
    return result


def _period(
    days: list[dict[str, Any]],
    window: int,
    policy: dict[str, Any],
    *,
    evaluate_goal: bool,
) -> dict[str, Any]:
    selected = days[-window:]
    included = [item for item in selected if item["status"] == "available"]
    returns = [float(item["account_return_pct"]) for item in included]
    benchmark_complete = bool(included) and all(item.get("benchmark_return_pct") is not None for item in included)
    account_return = _compound_return(returns)
    benchmark_return = _compound_return([float(item["benchmark_return_pct"]) for item in included]) if benchmark_complete else None
    excess_return = round(account_return - benchmark_return, 4) if account_return is not None and benchmark_return is not None else None
    coverage_complete = len(selected) == window and not any(item["status"] == "insufficient_snapshots" for item in selected)
    return_target = float(policy["primary_return_target_pct"])
    excess_target = float(policy["primary_excess_return_target_pct"])
    return_met = account_return > return_target if evaluate_goal and coverage_complete and account_return is not None else None
    excess_met = excess_return >= excess_target if evaluate_goal and coverage_complete and excess_return is not None else None
    turnover_complete = [item for item in included if item.get("gross_turnover_pct") is not None]
    max_turnover = max((float(item["gross_turnover_pct"]) for item in turnover_complete), default=None)
    return {
        "requested_trading_days": window,
        "observed_trading_days": len(selected),
        "included_return_days": len(included),
        "excluded_dates": [item["date"] for item in selected if item["status"] == "excluded_external_flow"],
        "insufficient_snapshot_dates": [item["date"] for item in selected if item["status"] == "insufficient_snapshots"],
        "account_return_pct": account_return,
        "benchmark_return_pct": benchmark_return,
        "excess_return_pct": excess_return,
        "max_drawdown_pct": _max_drawdown(returns),
        "max_daily_gross_turnover_pct": round(max_turnover, 4) if max_turnover is not None else None,
        "turnover_complete_days": len(turnover_complete),
        "incomplete_turnover_dates": [
            item["date"] for item in included if item.get("turnover_collection_status") != "complete"
        ],
        "turnover_reference_breached_dates": [
            item["date"]
            for item in turnover_complete
            if float(item["gross_turnover_pct"]) > float(policy["max_daily_gross_turnover_pct"])
        ],
        "coverage_status": "complete" if coverage_complete and benchmark_complete else "partial",
        "return_target_met": return_met,
        "excess_return_target_met": excess_met,
        "goal_status": (
            "reference_only"
            if not evaluate_goal
            else "met"
            if return_met is True and excess_met is True
            else "not_met"
            if return_met is False or excess_met is False
            else "insufficient_coverage"
        ),
    }


def build_account_performance_context(
    *,
    workspace_dir: Path,
    runs_root: Path,
    started_at: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    review = policy.get("performance_review") if isinstance(policy.get("performance_review"), dict) else {}
    cutoff = _started_at(started_at) or datetime.now(KST)
    benchmark = str(review.get("benchmark_index") or "KOSPI").upper()
    days = _daily_observations(
        runs_root,
        cutoff,
        benchmark,
        excluded_external_flow_dates(workspace_dir),
    )
    primary_window = int(review.get("primary_window_trading_days") or 20)
    auxiliary_window = int(review.get("auxiliary_window_trading_days") or 5)
    latest = days[-1] if days else None
    latest_snapshot = latest.get("closing_snapshot") if isinstance(latest, dict) else None
    securities = _number(latest_snapshot.get("securities_valuation_amount")) if isinstance(latest_snapshot, dict) else None
    holdings = latest_snapshot.get("holdings", []) if isinstance(latest_snapshot, dict) else []
    largest = max(holdings, key=lambda item: item["valuation_amount"], default=None) if isinstance(holdings, list) else None
    largest_weight = (
        round(float(largest["valuation_amount"]) / securities * 100.0, 4)
        if largest is not None and securities is not None and securities > 0
        else None
    )
    compact_latest = dict(latest) if isinstance(latest, dict) else None
    if compact_latest is not None:
        compact_latest.pop("closing_snapshot", None)
    return {
        "schema_version": "1",
        "scope": "domestic_trading_account",
        "as_of": cutoff.isoformat(),
        "benchmark_index": benchmark,
        "advisory_semantics": "Performance goals and risk references inform Judge sizing and reporting only; they are not order allow/block rules.",
        "external_flow_policy": "Each day uses its first and last usable domestic-account snapshots. Reported external-flow dates are excluded; flows outside that interval reset the next opening baseline.",
        "references": {
            **review,
            "primary_return_comparison": "gt",
            "primary_excess_return_comparison": "gte",
        },
        "periods": {
            "primary": _period(days, primary_window, review, evaluate_goal=True),
            "auxiliary": _period(days, auxiliary_window, review, evaluate_goal=False),
        },
        "latest_day": compact_latest,
        "current_risk": {
            "largest_symbol_id": largest.get("symbol_id") if largest else "",
            "largest_symbol_name": largest.get("symbol_name") if largest else "",
            "largest_symbol_weight_pct": largest_weight,
            "max_symbol_weight_reference_pct": review.get("max_symbol_weight_pct"),
            "within_symbol_weight_reference": (
                largest_weight <= float(review["max_symbol_weight_pct"])
                if largest_weight is not None and review.get("max_symbol_weight_pct") is not None
                else None
            ),
        },
    }
