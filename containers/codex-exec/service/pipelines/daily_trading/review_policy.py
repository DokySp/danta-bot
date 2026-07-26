#!/usr/bin/env python3
"""Deterministic broker-preflight review policy for scheduled daily-trading runs.

Decides whether a scheduled daily-trading invocation must run the expensive
full review (universe evidence, Analyst, Debate, Judge, execution) or may end
after a cheap broker preflight. See containers/codex-exec/profiles/base/config/
daily-trading-full-review-times.yaml for the fixed KST review-time schedule.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FULL_REVIEW_TIMES_CONFIG_ENV = "DAILY_TRADING_FULL_REVIEW_TIMES_CONFIG"
FULL_REVIEW_TIMES_CONFIG_FILENAME = "daily-trading-full-review-times.yaml"
_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def full_review_times_config_candidates(
    *, workspace_dir: Path, workspace_repo_root: Path, code_repo_root: Path
) -> list[Path]:
    return [
        Path("/app/config") / FULL_REVIEW_TIMES_CONFIG_FILENAME,
        code_repo_root / "containers/codex-exec/profiles/base/config" / FULL_REVIEW_TIMES_CONFIG_FILENAME,
        workspace_repo_root / "containers/codex-exec/profiles/base/config" / FULL_REVIEW_TIMES_CONFIG_FILENAME,
        workspace_dir / "containers/codex-exec/profiles/base/config" / FULL_REVIEW_TIMES_CONFIG_FILENAME,
    ]


def resolve_full_review_times_config_path(
    *,
    workspace_dir: Path,
    workspace_repo_root: Path,
    code_repo_root: Path,
    configured: str = "",
) -> Path:
    explicit = str(configured or os.getenv(FULL_REVIEW_TIMES_CONFIG_ENV, "")).strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = workspace_dir / path
        if not path.exists():
            raise FileNotFoundError(f"full review times config not found: {path}")
        return path.resolve()
    candidates = full_review_times_config_candidates(
        workspace_dir=workspace_dir, workspace_repo_root=workspace_repo_root, code_repo_root=code_repo_root
    )
    for path in candidates:
        if path.exists():
            return path.resolve()
    searched = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(f"default full review times config not found; searched: {searched}")


def parse_time_minutes(value: str) -> int:
    text = str(value or "").strip()
    match = _TIME_PATTERN.match(text)
    if not match:
        raise ValueError(f"invalid HH:MM time: {value!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


def load_full_review_times(path: Path) -> list[str]:
    import yaml

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"full review times config must be a mapping: {path}")
    raw_times = payload.get("full_review_times")
    if not isinstance(raw_times, list) or not raw_times:
        raise ValueError(f"full_review_times must be a non-empty list: {path}")
    times = [str(item).strip() for item in raw_times]
    minutes = [parse_time_minutes(item) for item in times]
    if minutes != sorted(minutes) or len(set(minutes)) != len(minutes):
        raise ValueError(f"full_review_times must be strictly increasing HH:MM values: {path}")
    return times


def due_slot(times: list[str], now_minutes: int) -> str | None:
    """Return the latest fixed review time at or before now_minutes, if any."""
    due: str | None = None
    for item in times:
        if parse_time_minutes(item) <= now_minutes:
            due = item
        else:
            break
    return due


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_fingerprint_payload(
    *,
    universe: list[str],
    account: dict[str, Any],
    lifecycle: dict[str, Any],
    today_fills: dict[str, Any],
    config_fingerprint: str,
) -> dict[str, Any]:
    """Canonical, timestamp-free broker-state payload used to detect meaningful change.

    Deliberately excludes observed_at/generated_at and any other volatile
    timestamps so two preflights of an unchanged broker state hash identically.
    """
    holdings: list[dict[str, Any]] = []
    for item in account.get("symbols", []) if isinstance(account, dict) else []:
        if not isinstance(item, dict):
            continue
        symbol_id = str(item.get("symbol_id") or "").strip()
        if not symbol_id:
            continue
        holdings.append(
            {
                "symbol_id": symbol_id,
                "current_live_holding_quantity": item.get("current_live_holding_quantity"),
                "today_buy_quantity": item.get("today_buy_quantity"),
                "today_sell_quantity": item.get("today_sell_quantity"),
            }
        )
    holdings.sort(key=lambda item: item["symbol_id"])

    active_orders: list[dict[str, Any]] = []
    for item in lifecycle.get("active_orders", []) if isinstance(lifecycle, dict) else []:
        if not isinstance(item, dict) or str(item.get("active_status") or "") != "active":
            continue
        active_orders.append(
            {
                "identity": str(item.get("order_id") or ""),
                "symbol_id": str(item.get("symbol_id") or ""),
                "direction": str(item.get("direction") or ""),
                "price": item.get("order_price"),
                "remaining_quantity": item.get("remaining_quantity"),
                "status": str(item.get("active_status") or ""),
            }
        )
    active_orders.sort(key=lambda item: (item["symbol_id"], item["identity"]))

    fills: list[dict[str, Any]] = []
    for item in today_fills.get("fills", []) if isinstance(today_fills, dict) else []:
        if not isinstance(item, dict):
            continue
        fills.append(
            {
                "identity": str(item.get("order_id") or ""),
                "symbol_id": str(item.get("symbol_id") or ""),
                "direction": str(item.get("direction") or ""),
                "filled_quantity": item.get("filled_quantity"),
            }
        )
    fills.sort(key=lambda item: (item["symbol_id"], item["identity"]))

    account_summary = account.get("account_summary") if isinstance(account, dict) else {}
    orderable_cash_amount = (account_summary or {}).get("orderable_cash_amount")

    return {
        "universe": sorted(str(symbol) for symbol in universe),
        "holdings": holdings,
        "active_orders": active_orders,
        "today_fills": fills,
        "orderable_cash_amount": orderable_cash_amount,
        "config_fingerprint": config_fingerprint,
    }


# Maps build_fingerprint_payload() keys to the human-facing component names used
# in review-trigger.json's changed_components, per the auditability contract.
_FINGERPRINT_COMPONENT_LABELS = (
    ("universe", "universe"),
    ("holdings", "holdings"),
    ("active_orders", "active_orders"),
    ("today_fills", "fills"),
    ("orderable_cash_amount", "cash"),
    ("config_fingerprint", "config"),
)


def changed_components(prior_payload: Any, current_payload: Any) -> list[str]:
    """Deterministic list of top-level fingerprint components that differ.

    Returns [] when there is no usable prior payload to diff against (e.g. the
    first safe run of a day), since "changed" is meaningless without a prior.
    """
    if not isinstance(prior_payload, dict) or not prior_payload:
        return []
    if not isinstance(current_payload, dict):
        return []
    changed: list[str] = []
    for key, label in _FINGERPRINT_COMPONENT_LABELS:
        if canonical_json(prior_payload.get(key)) != canonical_json(current_payload.get(key)):
            changed.append(label)
    return changed


@dataclass(frozen=True)
class ReviewTriggerState:
    date: str = ""
    fingerprint: str = ""
    last_satisfied_time: str = ""
    fingerprint_payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": "2",
            "date": self.date,
            "fingerprint": self.fingerprint,
            "last_satisfied_time": self.last_satisfied_time,
            "fingerprint_payload": self.fingerprint_payload,
        }

    @classmethod
    def from_json(cls, payload: Any) -> "ReviewTriggerState":
        if not isinstance(payload, dict):
            return cls()
        raw_payload = payload.get("fingerprint_payload")
        return cls(
            date=str(payload.get("date") or ""),
            fingerprint=str(payload.get("fingerprint") or ""),
            last_satisfied_time=str(payload.get("last_satisfied_time") or ""),
            # Absent in schema_version 1 (pre-existing state files); treated as
            # "no prior payload to diff", same as an empty dict.
            fingerprint_payload=raw_payload if isinstance(raw_payload, dict) else {},
        )


def review_trigger_state_path(workspace_dir: Path, env_dv: str) -> Path:
    return workspace_dir / "memory" / "daily-trading" / f"review-trigger-state-{env_dv}.json"


def load_review_trigger_state(path: Path) -> ReviewTriggerState:
    if not path.exists():
        return ReviewTriggerState()
    try:
        return ReviewTriggerState.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return ReviewTriggerState()


def save_review_trigger_state(path: Path, state: ReviewTriggerState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(state.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class SafetyCheck:
    safe: bool
    reasons: list[str] = field(default_factory=list)


def evaluate_safety(
    *,
    lookup_complete: bool,
    orderable_cash_amount: Any,
    holding_state_issue_count: int,
    account_status: str,
    today_fills_complete: bool = True,
    unexpected_non_universe_symbols: list[str] | None = None,
) -> SafetyCheck:
    reasons: list[str] = []
    if account_status != "success":
        # Any non-"success" account status -- "failed", "partial", missing, or
        # any other unrecognized value -- is an incomplete lookup and must
        # block, not just the literal "failed" string.
        reasons.append("account_lookup_failed")
    if not lookup_complete:
        reasons.append("order_lifecycle_lookup_incomplete")
    if orderable_cash_amount is None:
        reasons.append("orderable_cash_unavailable")
    if holding_state_issue_count > 0:
        reasons.append("holding_state_issue_detected")
    if not today_fills_complete:
        reasons.append("today_fills_lookup_incomplete")
    if unexpected_non_universe_symbols:
        reasons.append("unexpected_non_universe_holding")
    return SafetyCheck(safe=not reasons, reasons=reasons)


def unexpected_non_universe_holdings(account: dict[str, Any], portfolio_except: list[str]) -> list[str]:
    """Live holdings outside the universe that are not explicitly excepted.

    account-before-order.json's non_universe_account_positions already only
    contains positions with a positive live holding quantity (see
    collect_account_artifact). A symbol there is a safety concern unless the
    current portfolio_except configuration explicitly lists it.
    """
    if not isinstance(account, dict):
        return []
    excepted = {str(symbol).strip() for symbol in portfolio_except if str(symbol).strip()}
    positions = account.get("non_universe_account_positions")
    if not isinstance(positions, list):
        return []
    unexpected: list[str] = []
    for item in positions:
        if not isinstance(item, dict):
            continue
        symbol_id = str(item.get("symbol_id") or "").strip()
        if symbol_id and symbol_id not in excepted:
            unexpected.append(symbol_id)
    return sorted(set(unexpected))


def decide_full_review(
    *,
    now_minutes: int,
    full_review_times: list[str],
    today: str,
    state: ReviewTriggerState,
    fingerprint: str,
    invocation_type: str,
) -> dict[str, Any]:
    """Rule 4/5 decision for a *safe* preflight. Callers must gate this on safety first."""
    slot = due_slot(full_review_times, now_minutes)
    if invocation_type == "manual":
        return {"decision": "full", "reasons": ["manual_invocation"], "due_slot": slot}

    first_run_of_day = state.date != today
    fingerprint_changed = (not first_run_of_day) and fingerprint != state.fingerprint

    last_satisfied_minutes: int | None = None
    if not first_run_of_day and state.last_satisfied_time:
        last_satisfied_minutes = parse_time_minutes(state.last_satisfied_time)
    slot_minutes = parse_time_minutes(slot) if slot is not None else None
    slot_due = slot is not None and (last_satisfied_minutes is None or last_satisfied_minutes < slot_minutes)

    reasons: list[str] = []
    if first_run_of_day:
        reasons.append("first_safe_run_of_day")
    if fingerprint_changed:
        reasons.append("broker_fingerprint_changed")
    if slot_due:
        reasons.append("fixed_review_time_due")

    decision = "full" if (first_run_of_day or fingerprint_changed or slot_due) else "skipped"
    return {"decision": decision, "reasons": reasons, "due_slot": slot}
