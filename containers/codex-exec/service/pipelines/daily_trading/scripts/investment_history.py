"""Reconstruct fill-backed investment episodes from existing run artifacts.

This is accounting/provenance, never an order gate or a trading strategy.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return (stamp.replace(tzinfo=KST) if stamp.tzinfo is None else stamp).astimezone(KST)
    except ValueError:
        return None


def quantity(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
        return number if number >= 0 else None
    except ValueError:
        return None


def symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol_id") or row.get("symbol") or row.get("code") or "")


def rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def rationale(judge: dict[str, Any], key: str, before: datetime, *, simulated: bool = False) -> dict[str, Any]:
    # Replay writes artifacts now, but its started_at is the virtual decision
    # information cutoff. The wall clock is not a simulated market observation.
    decided = timestamp(judge.get("started_at") if simulated else judge.get("generated_at") or judge.get("started_at"))
    matching = [row for row in rows(judge.get("symbols")) if symbol(row) == key]
    if judge.get("status") != "success" or decided is None or decided > before or len(matching) != 1:
        return {"status": "unavailable", "reason": "matching_decision_unavailable"}
    row = matching[0]
    return {
        "status": "linked", "source_run_id": str(judge.get("run_id") or ""),
        "decided_at": decided.isoformat(),
        **({"decision_clock": "replay_information_cutoff"} if simulated else {}),
        **{field: copy.deepcopy(row[field]) for field in (
            "reason_code", "one_line_reason", "additional_buy_reason", "thesis_definition", "plan_review",
        ) if field in row},
    }


def build_history(output_dir: Path, cutoff: datetime, read_json: Callable, *, simulated: bool = False) -> dict[str, Any]:
    """Read-only, deterministic snapshot; unknown history is not backfilled.

    Daily broker buy/sell totals anchor the quantities; per-order cumulative
    observations become deltas, so repeated/partial-fill snapshots are not new
    trades. A discontinuity invalidates attribution until a known flat state.
    """
    def read(path: Path) -> dict[str, Any]:
        try:
            data = read_json(path)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    current = read(output_dir / "account-before-order.json")
    environment = current.get("execution_environment")
    result: dict[str, Any] = {
        "schema_version": "1", "information_cutoff": cutoff.isoformat(),
        "evidence_mode": "simulated_fills" if simulated else "broker_fills",
        "execution_environment": environment, "symbols": {},
    }
    if not output_dir.parent.is_dir():
        return result
    accounts: dict[tuple[str, str], tuple[datetime, dict[str, Any]]] = {}
    observations: dict[tuple[str, str, str, str], list[tuple[datetime, datetime, int, str]]] = {}
    orders: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    closed_days: set[str] = set()
    final_fill_days: set[tuple[str, str]] = set()
    invalid_days: set[tuple[str, str]] = set()

    # ponytail: one O(runs + fills) scan per Judge input; add an index only if
    # archived-run volume makes this measurable, not a second memory service.
    for run_dir in sorted(path for path in output_dir.parent.iterdir() if path.is_dir()):
        account = read(run_dir / "account-before-order.json")
        observed = timestamp(account.get("generated_at") or account.get("started_at"))
        if observed is None or observed > cutoff or account.get("skipped") or account.get("status") not in {"success", "partial"}:
            continue
        if account.get("execution_environment") != environment:
            continue
        brief = read(run_dir / "decision-brief.json")
        source = brief.get("source_artifacts")
        is_replay = isinstance(source, dict) and source.get("mode") == "archived_point_in_time_replay"
        if is_replay != simulated:
            continue
        chart = read(run_dir / "price-chart.json")
        chart_known = timestamp(chart.get("generated_at") or chart.get("started_at"))
        if (chart_known is not None and chart_known <= cutoff
                and chart.get("market_open_day_checked") is True and chart.get("market_open_day") is False):
            closed_days.add(observed.date().isoformat())
        for row in [*rows(account.get("symbols")), *rows(account.get("non_universe_account_positions"))]:
            if not isinstance(row, dict) or not symbol(row) or row.get("snapshot_row_available") is not True:
                continue
            if row.get("holding_state_status", "consistent") != "consistent":
                continue
            stamp = timestamp(row.get("observed_at")) or observed
            if stamp > cutoff:
                continue
            key = (stamp.date().isoformat(), symbol(row))
            if key not in accounts or accounts[key][0] <= stamp:
                accounts[key] = (stamp, row)

        judge = read(run_dir / "judge-review.json")
        if simulated:
            simulation = read(run_dir / "simulated-judge-review.json")
            if simulation.get("status") != "success":
                continue
            sim_fills = rows(simulation.get("fills"))
            for index, fill in enumerate(sim_fills):
                stamp = timestamp(fill.get("filled_at"))
                if stamp is None or stamp > cutoff:
                    continue
                key = (stamp.date().isoformat(), symbol(fill), str(fill.get("direction") or ""), f"{run_dir.name}:{index}")
                amount = quantity(fill.get("filled_quantity"))
                if key[1] and key[2] in {"buy", "sell"} and amount is not None and amount > 0:
                    observations[key] = [(stamp, stamp, amount, run_dir.name)]
                    orders[key] = [rationale(judge, key[1], stamp, simulated=True)]
            # The saved simulated final quantity is a virtual account fact,
            # not a requested target and not a broker fill.
            stamps = [timestamp(fill.get("filled_at")) for fill in sim_fills]
            stamps = [stamp for stamp in stamps if stamp is not None and stamp <= cutoff]
            if stamps:
                stamp = max(stamps)
                for row in rows(simulation.get("symbols")):
                    key = (stamp.date().isoformat(), symbol(row))
                    own = [fill for fill in sim_fills if symbol(fill) == key[1] and timestamp(fill.get("filled_at")) in stamps]
                    if key in accounts and accounts[key][0] > stamp:
                        continue
                    accounts[key] = (stamp, {
                        "current_live_holding_quantity": row.get("simulated_final_quantity"),
                        "today_buy_quantity": sum(quantity(fill.get("filled_quantity")) or 0 for fill in own if fill.get("direction") == "buy"),
                        "today_sell_quantity": sum(quantity(fill.get("filled_quantity")) or 0 for fill in own if fill.get("direction") == "sell"),
                    })
            continue

        for filename, field in (("execution.json", "orders"), ("order-lifecycle.json", "previous_submitted_cash_orders")):
            execution = read(run_dir / filename)
            known = timestamp(execution.get("generated_at"))
            if known is None or known > cutoff or execution.get("execution_environment", environment) != environment:
                continue
            for order in rows(execution.get(field)):
                if not isinstance(order, dict) or (field == "orders" and order.get("result") != "submitted"):
                    continue
                # Reservation IDs are not cash-order IDs; do not guess their mapping.
                if order.get("order_path") != "immediate":
                    continue
                stamp = timestamp(order.get("started_at") or execution.get("started_at"))
                order_id = str(order.get("order_or_reservation_id") or order.get("order_id") or "")
                side = str(order.get("direction") or "")
                if stamp is None or stamp > cutoff or not order_id or side not in {"buy", "sell"}:
                    continue
                source_id = str(order.get("run_id") or execution.get("run_id") or run_dir.name)
                source_judge = judge if source_id == run_dir.name else read(output_dir.parent / Path(source_id).name / "judge-review.json")
                key = (stamp.date().isoformat(), symbol(order), side, order_id)
                item = rationale(source_judge, key[1], known)
                if item not in orders.setdefault(key, []):
                    orders[key].append(item)
                broker = order.get("broker_reconciliation")
                broker = broker if isinstance(broker, dict) else {}
                confirmed = timestamp(broker.get("observed_at")) or known
                amount = quantity(broker.get("filled_quantity"))
                if (broker.get("status") in {"filled", "partially_filled", "partially_filled_rejected", "partially_filled_canceled"}
                        and confirmed <= cutoff and amount is not None and amount > 0):
                    observations.setdefault(key, []).append((confirmed, confirmed, amount, run_dir.name))

        fills = read(run_dir / "today-fills.json")
        known = timestamp(fills.get("generated_at"))
        if known is None or known > cutoff or fills.get("execution_environment") != environment:
            continue
        previous = fills.get("previous_session")
        previous = previous if isinstance(previous, dict) else {}
        previous_day = str(previous.get("session_date") or "").replace("-", "")
        if previous.get("fill_collection_status") == "complete" and len(previous_day) == 8 and previous_day.isdigit():
            day = f"{previous_day[:4]}-{previous_day[4:6]}-{previous_day[6:]}"
            if day < known.date().isoformat():
                final_fill_days.update((day, symbol(row)) for row in rows(fills.get("symbols")))
        for fill in [*rows(fills.get("fills")), *rows(previous.get("fills"))]:
            if not isinstance(fill, dict):
                continue
            stamp = timestamp(fill.get("filled_at"))
            seen = timestamp(fill.get("observed_at")) or known
            side, order_id = str(fill.get("direction") or ""), str(fill.get("order_id") or "")
            amount = quantity(fill.get("filled_quantity"))
            if stamp is None or stamp > cutoff or seen > cutoff or amount is None or amount <= 0 or side not in {"buy", "sell"} or not order_id:
                continue
            day = stamp.date().isoformat()
            if str(fill.get("order_date") or "").replace("-", "") != day.replace("-", ""):
                invalid_days.add((day, symbol(fill)))
                continue
            key = (day, symbol(fill), side, order_id)
            observations.setdefault(key, []).append((seen, stamp, amount, run_dir.name))

    days: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key, samples in observations.items():
        cumulative = 0
        for seen, reported, amount, source_id in sorted(set(samples)):
            if amount < cumulative:
                invalid_days.add(key[:2])
            if amount <= cumulative:
                continue
            linked = [item for item in orders.get(key, []) if item.get("status") == "linked"
                      and timestamp(item.get("decided_at")) <= seen]
            basis = linked[0] if len(linked) == 1 else {"status": "unavailable", "reason": "order_decision_not_uniquely_matched"}
            event = {
                "event_id": ":".join(key) + f":{amount}", "order_id": key[3],
                "direction": key[2], "quantity": amount - cumulative,
                "reported_at": reported.isoformat(), "confirmed_at": seen.isoformat(),
                "source_run_id": source_id, "rationale": copy.deepcopy(basis),
            }
            days.setdefault(key[:2], []).append(event)
            cumulative = amount

    # On confirmed closed days KIS may still return the preceding session's
    # buy/sell counters. Preserve the holding observation, not stale turnover.
    for key, (observed, account) in accounts.items():
        if key[0] in closed_days and not days.get(key):
            accounts[key] = (observed, {**account, "today_buy_quantity": 0, "today_sell_quantity": 0})

    # A late fill may first appear in the next session. Only a complete
    # previous-session fill query plus the next opening balance can certify
    # the prior closing quantity; matching balances alone cannot prove it.
    by_symbol: dict[str, list[tuple[str, str]]] = {}
    for day_key in sorted(accounts):
        by_symbol.setdefault(day_key[1], []).append(day_key)
    for keys in by_symbol.values():
        for previous_key, next_key in zip(keys, keys[1:]):
            if previous_key not in final_fill_days:
                continue
            observed, prior = accounts[previous_key]
            following = accounts[next_key][1]
            amounts = [quantity(row.get(field)) for row in (prior, following) for field in (
                "current_live_holding_quantity", "today_buy_quantity", "today_sell_quantity",
            )]
            if any(value is None for value in amounts):
                continue
            live, buys, sells, next_live, next_buys, next_sells = amounts
            events = days.get(previous_key, [])
            total_buy = sum(event["quantity"] for event in events if event["direction"] == "buy")
            total_sell = sum(event["quantity"] for event in events if event["direction"] == "sell")
            closing = live + total_buy - buys - total_sell + sells
            if total_buy >= buys and total_sell >= sells and closing == next_live - next_buys + next_sells:
                accounts[previous_key] = (observed, {**prior, "current_live_holding_quantity": closing,
                    "today_buy_quantity": total_buy, "today_sell_quantity": total_sell})

    states: dict[str, dict[str, Any]] = {}
    for (day, key), (observed, account) in sorted(accounts.items()):
        live, buys, sells = (quantity(account.get(field)) for field in (
            "current_live_holding_quantity", "today_buy_quantity", "today_sell_quantity",
        ))
        if live is None:
            continue
        state = states.setdefault(key, {"active": None, "closed": [], "quantity": None, "day": None})
        events = sorted(days.get((day, key), []), key=lambda item: (item["confirmed_at"], item["reported_at"], item["event_id"]))
        complete = buys is not None and sells is not None and (day, key) not in invalid_days
        complete = complete and buys == sum(item["quantity"] for item in events if item["direction"] == "buy")
        complete = complete and sells == sum(item["quantity"] for item in events if item["direction"] == "sell")
        opening = live - buys + sells if complete else None
        if opening is not None and opening < 0:
            complete = False
        gap = False
        if state["day"] is not None:
            check = datetime.fromisoformat(state["day"]).date() + timedelta(days=1)
            while check.isoformat() < day:
                if check.weekday() < 5 and check.isoformat() not in closed_days:
                    gap = True
                check += timedelta(days=1)
        if not complete or gap or (state["quantity"] is not None and state["quantity"] != opening):
            state["active"] = None
        running = opening if complete else live
        if running and state["active"] is None:
            state["active"] = {"investment_id": f"{key}:unknown:{day}", "entry": None, "changes": [], "origin_status": "unavailable"}
        if complete:
            for event in events:
                after = running + (event["quantity"] if event["direction"] == "buy" else -event["quantity"])
                if after < 0:
                    complete = False
                    state["active"] = None
                    break
                change = {**event, "before_quantity": running, "after_quantity": after}
                if running == 0 and after > 0:
                    state["active"] = {"investment_id": event["event_id"], "entry": change, "changes": [], "origin_status": event["rationale"]["status"]}
                elif state["active"] is not None:
                    state["active"]["changes"].append(change)
                if after == 0 and state["active"] is not None:
                    state["active"]["exit"] = change
                    state["closed"].append(state["active"])
                    state["active"] = None
                running = after
        state.update(quantity=live, day=day, status="reconciled" if complete else "incomplete")
        if live == 0:
            state["active"] = None
        result["symbols"][key] = {
            "status": state["status"], "as_of": observed.isoformat(), "current_quantity": live,
            "active_investment": copy.deepcopy(state["active"]),
            "closed_investments": copy.deepcopy(state["closed"]),
        }
    current_rows = {symbol(row): row for row in rows(current.get("symbols"))}
    current_known = timestamp(current.get("generated_at") or current.get("started_at"))
    for key, row in result["symbols"].items():
        own = current_rows.get(key, {})
        own_stamp = timestamp(own.get("observed_at") or current.get("generated_at") or current.get("started_at"))
        row["current_snapshot_verified"] = (
            current.get("status") in {"success", "partial"} and not current.get("skipped")
            and current_known is not None and current_known <= cutoff
            and own.get("snapshot_row_available") is True
            and own.get("holding_state_status", "consistent") == "consistent"
            and own_stamp is not None and own_stamp <= cutoff
            and row["as_of"] == own_stamp.isoformat()
        )
    return result


def review_context(history: dict[str, Any], key: str, current_quantity: Any) -> dict[str, Any]:
    """Bound prompt size without dropping the immutable entry from the full artifact."""
    row = history.get("symbols", {}).get(key, {})
    context = {"status": "unavailable", "evidence_mode": history.get("evidence_mode"),
               "source_artifact": "investment-history.json", "active_investment": None}
    if (not row.get("current_snapshot_verified") or quantity(current_quantity) is None
            or row.get("current_quantity") != quantity(current_quantity)):
        context["reason"] = "current_holding_not_reconciled"
        return context
    context["status"] = row.get("status", "unavailable")
    active = copy.deepcopy(row.get("active_investment"))
    if active:
        active["change_count"] = len(active["changes"])
        active["changes"] = active["changes"][-12:]
    context["active_investment"] = active
    closed = row.get("closed_investments", [])
    if closed:
        latest = closed[-1]
        context["last_closed_investment"] = {field: copy.deepcopy(latest.get(field)) for field in ("investment_id", "origin_status", "entry", "exit")}
    return context
