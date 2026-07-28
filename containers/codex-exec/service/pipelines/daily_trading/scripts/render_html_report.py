#!/usr/bin/env python3
"""Render a single-file cumulative daily-trading HTML report from run artifacts."""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any


ROLE_LABELS = {
    "analyst-momentum-cycle": "Momentum / Cycle",
    "analyst-news-flow": "News / Flow",
    "analyst-quality-value": "Quality / Value",
    "analyst-risk-allocation": "Risk / Allocation",
}
PHASE_LABELS = {
    "opening": "Opening",
    "rebuttal-1": "Rebuttal 1",
}
SIDE_LABELS = {"bull": "Bull", "bear": "Bear"}
# canonical_action/requested_action (derive_action) are increase|hold|reduce|exit, never buy/sell.
CANONICAL_ACTION_LABELS = {"increase": "확대", "reduce": "축소", "exit": "청산", "hold": "유지"}
DECISION_BASIS_LABELS = {
    "none": "기준유지",
    "thesis": "논지",
    "profit_protection": "이익보호",
    "concentration_rebalance": "집중도조정",
}
GUARD_STATUS_LABELS = {"allowed": "허용", "blocked": "차단", "no_change": "변경없음 처리"}
REVIEW_TRIGGER_DECISION_LABELS = {"full": "전체 리뷰 실행", "skipped": "생략(변경 없음)", "safety_block": "안전 차단"}
REVIEW_TRIGGER_REASON_LABELS = {
    "manual_invocation": "수동/전체 실행 요청",
    "first_safe_run_of_day": "당일 최초 안전 실행",
    "broker_fingerprint_changed": "브로커 상태 변경 감지",
    "fixed_review_time_due": "예정 리뷰 시각 도래",
    "account_lookup_failed": "계좌 조회 실패",
    "order_lifecycle_lookup_incomplete": "미체결 주문 조회 미완료",
    "orderable_cash_unavailable": "주문가능금액 조회 불가",
    "holding_state_issue_detected": "보유수량 상태 불일치 감지",
    "today_fills_lookup_incomplete": "당일 체결 조회 미완료",
    "unexpected_non_universe_holding": "유니버스 외 예상외 보유 종목",
}


def review_trigger_reason_label(value: Any) -> str:
    raw = str(value or "")
    return REVIEW_TRIGGER_REASON_LABELS.get(raw, raw or "-")
NOT_RECORDED = "미기록"


def judge_field_display(final_item: dict[str, Any], field: str, labels: dict[str, str]) -> str:
    """v1 judge-review.json artifacts never had decision_basis/requested_action/canonical_action.
    A missing field must show "미기록"(not recorded), never a fabricated none/hold default that
    reads as an actual mechanical decision."""
    if field not in final_item:
        return NOT_RECORDED
    raw = str(final_item.get(field) or "")
    return labels.get(raw, raw or NOT_RECORDED)


JUDGE_SCOPE_STATUS_LABELS = {
    "resolved": "Judge 진행",
    "unresolved_in_scope": "Judge 대상 미해결",
    "not_selected": "Judge 미선정",
    "legacy_unknown": "Judge 상태 확인불가(구버전)",
}


def judge_symbol_scope_status(
    symbol_id: str,
    final_item: dict[str, Any] | None,
    judge_scope_reasons: dict[str, Any],
    has_judge_scope_metadata: bool,
) -> str:
    """A symbol present in judge-review-spec.review_scope_reasons but absent from judge-review.json
    was an in-scope Judge target Judge never returned a valid result for -- it is not "Analyst
    only"/"not selected" (both of those imply Judge never intended to look at it). A v1 run with no
    scope metadata at all cannot honestly claim "not selected" for every unresolved symbol either."""
    if final_item is not None:
        return "resolved"
    if not has_judge_scope_metadata:
        return "legacy_unknown"
    if symbol_id in judge_scope_reasons:
        return "unresolved_in_scope"
    return "not_selected"


REGIME_LABELS = {
    "insufficient_market_data": "시장 데이터 부족",
    "neutral": "중립",
    "risk_on": "강세",
    "weak_downside": "약세",
    "panic_downside": "급락",
}
ORDER_REASON_LABELS = {
    "accepted": "접수",
    "cash_order_submitted": "현금주문 제출",
    "reservation_order_submitted": "예약주문 제출",
    "final_equals_expected_holding_quantity": "목표수량 일치",
    "symbol_in_portfolio_except_list": "제외 종목 차단",
    "invalid_final_holding_quantity": "최종수량 값 오류",
    "buy_cash_limit_missing": "매수한도 조회 불가",
    "buy_quantity_exceeds_order_available_quantity": "매수가능수량 초과",
    "sell_quantity_exceeds_order_available_quantity": "매도가능수량 초과",
    "buy_quantity_reduced_to_order_available_quantity": "매수가능수량으로 축소",
    "sell_quantity_reduced_to_order_available_quantity": "매도가능수량으로 축소",
    "buy_quantity_reduced_to_remaining_cash": "잔여현금 기준 축소",
    "buy_cash_gate_reduced_reverse_rank": "현금부족 후순위 축소",
    "existing_matching_reservation_kept": "기존 예약 유지",
    "active_order_cancel_submitted": "기존주문 취소 제출",
    "active_order_cancel_and_replacement_submitted": "기존주문 취소 후 재제출",
    "replacement_order_submission_failed": "대체주문 제출 실패",
    "order_submission_blocked": "주문 제출 차단",
    "submit_requires_explicit_execution_request": "명시적 실행 요청 필요",
    "decision_guard_not_allowed": "정책 가드 차단",
    "profit_protection_pnl_recheck_failed": "이익보호 손익 재확인 실패",
    "profit_protection_reduction_bound_recheck_failed": "이익보호 축소 한도 재확인 실패",
    "concentration_rebalance_recheck_failed": "집중도 재확인 실패",
    "holding_state_not_verified": "보유수량 상태 불일치",
    "stale_active_order_requires_cancellation": "이전 미체결 주문 정리 필요",
    "unverified_holding_requires_active_order_cancellation": "수량 불일치로 기존 미체결 주문 취소 필요",
    "active_order_correction_submitted": "기존주문 정정 제출",
    "decision_guard_action_mismatch": "가드-액션 불일치",
    "decision_guard_basis_mismatch": "가드-근거 불일치",
    "duplicate_execution_symbol": "중복 실행 종목",
    "sell_quantity_capacity_missing": "매도가능수량 조회 불가",
    # Legacy score-band gate reason codes (removed in favor of guarded target-position decisions);
    # kept here so same-day artifacts written before that change still render truthfully instead
    # of falling back to the raw code.
    "sell_blocked_score_band": "점수 밴드 매도 차단(legacy)",
    "buy_blocked_score_band": "점수 밴드 매수 차단(legacy)",
    "score_band_value_missing": "점수 확인 불가 차단(legacy)",
    "debate_incomplete_baseline_forced": "토론 미완료로 기준 유지 강제",
    "decision_basis_required_for_increase": "확대에는 decision_basis 필요",
    "decision_basis_required_for_reduction": "축소에는 decision_basis 필요",
    "thesis_increase_allowed": "논지 기반 확대 허용",
    "thesis_reduction_allowed": "논지 기반 축소 허용",
    "thesis_reduction_gate_blocked": "논지 축소 게이트 차단",
    "profit_protection_blocked": "이익보호 조건 미충족 차단",
    "profit_protection_reduction_allowed": "이익보호 축소 허용",
    "concentration_rebalance_blocked": "집중도 조정 조건 미충족 차단",
    "concentration_rebalance_reduction_allowed": "집중도 조정 축소 허용",
    "capped_reduction_below_one_share": "축소분 1주 미만으로 변경없음 처리",
    "daily_turnover_context_unavailable": "일일 회전한도 컨텍스트 조회 불가",
    "daily_turnover_cap_exceeded": "일일 회전한도 초과",
    "within_daily_turnover_budget": "일일 회전한도 이내",
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def holding_status_display(decision_item: dict[str, Any] | None) -> tuple[str, str]:
    exposure = decision_item.get("account_exposure") if isinstance(decision_item, dict) else None
    if not isinstance(exposure, dict) or "current_live_holding_quantity" not in exposure:
        return "보유 미기록", "unknown"
    raw_quantity = exposure.get("current_live_holding_quantity")
    if isinstance(raw_quantity, bool):
        return "보유 미기록", "unknown"
    try:
        quantity = int(raw_quantity)
    except (TypeError, ValueError):
        return "보유 미기록", "unknown"
    if quantity < 0:
        return "보유 미기록", "unknown"
    if quantity == 0:
        return "비보유", "unheld"
    return f"보유 {number(quantity)}주", "held"


def decimal(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def valid_analyst_score(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        score = float(value)
    except (TypeError, ValueError):
        return False
    return 0 <= score <= 10


def signed_decimal(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):+.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def judge_guard_intervened(final_item: dict[str, Any] | None) -> bool:
    """decision_guard.status is only ever "blocked" or "no_change" (there is no "capped"
    status); a capped/adjusted decision is instead detected from the reason_code text or from
    the judge's requested target being clamped away from the canonical target."""
    if not isinstance(final_item, dict):
        return False
    guard = final_item.get("decision_guard") if isinstance(final_item.get("decision_guard"), dict) else {}
    status = str(guard.get("status") or "")
    if status in {"blocked", "no_change"}:
        return True
    reason_code = str(guard.get("reason_code") or "")
    if "capped" in reason_code:
        return True
    requested_target = final_item.get("requested_target_position_value_krw")
    canonical_target = final_item.get("target_position_value_krw")
    return requested_target is not None and canonical_target is not None and requested_target != canonical_target


def analyst_symbol_group_priority(
    symbol_id: str,
    final_by_symbol: dict[str, Any],
    trade_symbol_ids: set[str],
    attempt_symbol_ids: set[str],
) -> int:
    """Operational grouping order: 거래 확정 > 가드 개입 > 미해결/Judge 미처리 > 나머지."""
    final_item = final_by_symbol.get(symbol_id)
    if symbol_id in trade_symbol_ids:
        return 0
    if judge_guard_intervened(final_item) or symbol_id in attempt_symbol_ids:
        return 1
    if final_item is None:
        return 2
    return 3


def analyst_symbol_sort_key(
    item: dict[str, Any],
    final_by_symbol: dict[str, Any],
    trade_symbol_ids: set[str],
    attempt_symbol_ids: set[str],
) -> tuple[int, str, str]:
    symbol_id = str(item.get("symbol_id") or "")
    priority = analyst_symbol_group_priority(symbol_id, final_by_symbol, trade_symbol_ids, attempt_symbol_ids)
    return priority, str(item.get("symbol_name") or ""), symbol_id


def time_text(value: Any) -> str:
    text = str(value or "")
    return text[11:16] if len(text) >= 16 else "-"


def full_time_text(value: Any) -> str:
    text = str(value or "")
    if len(text) < 16:
        return "-"
    return text[:16].replace("T", " ")


def status_badge(status: Any) -> str:
    value = str(status or "-")
    css = "ok" if value == "success" else "warn" if value == "partial" else "bad"
    return f'<span class="badge {css}">{esc(value)}</span>'


def find_runs(runs_root: Path, target_started_at: str) -> list[dict[str, Any]]:
    day = target_started_at[:10]
    runs: list[dict[str, Any]] = []
    for path in runs_root.iterdir():
        summary_path = path / "pipeline-summary.json"
        if not path.is_dir() or not summary_path.is_file():
            continue
        summary = load_json(summary_path)
        started_at = str(summary.get("started_at") or "")
        if started_at[:10] != day or started_at > target_started_at:
            continue
        runs.append(
            {
                "path": path,
                "summary": summary,
                "execution": load_json(path / "execution.json"),
                "lifecycle": load_json(path / "order-lifecycle.json"),
                "decision": load_json(path / "decision-brief.json"),
                "market": load_json(path / "market-index-snapshot.json"),
                # Only build_summary's full-review path ever adds "review_summary"; a
                # short-circuit (safety_block/skipped preflight) summary never does, so this is a
                # reliable signal that the run never reached decision-brief/analyst/judge at all --
                # it must not be rendered as an empty full-review run.
                "is_preflight_only": "review_summary" not in summary,
            }
        )
    ordered_runs = sorted(runs, key=lambda item: str(item["summary"].get("started_at") or ""))
    latest_broker_by_order_id: dict[str, dict[str, Any]] = {}
    reservation_resulting_order_id: dict[str, str] = {}
    for run in ordered_runs:
        lifecycle_orders = run["lifecycle"].get("previous_submitted_cash_orders", [])
        for item in lifecycle_orders if isinstance(lifecycle_orders, list) else []:
            if not isinstance(item, dict):
                continue
            order_id = str(item.get("order_id") or "").strip()
            broker = item.get("broker_reconciliation")
            if order_id and isinstance(broker, dict):
                latest_broker_by_order_id[order_id] = broker
        active_orders = run["lifecycle"].get("active_orders", [])
        for item in active_orders if isinstance(active_orders, list) else []:
            if not isinstance(item, dict) or item.get("order_kind") != "reservation":
                continue
            reservation_id = str(item.get("rsvn_ord_seq") or item.get("order_id") or "").strip()
            resulting_order_id = str(item.get("odno") or "").strip()
            if reservation_id and resulting_order_id:
                reservation_resulting_order_id[reservation_id] = resulting_order_id
    for run in ordered_runs:
        execution = run["execution"]
        orders = execution.get("orders") if isinstance(execution, dict) else None
        if not isinstance(orders, list):
            continue
        execution["orders"] = [
            {
                **item,
                **(
                    {"broker_reconciliation": latest_broker_by_order_id[str(item.get("order_or_reservation_id") or "").strip()]}
                    if str(item.get("order_or_reservation_id") or "").strip() in latest_broker_by_order_id
                    else {}
                ),
                **(
                    {"resulting_order_id": reservation_resulting_order_id[str(item.get("order_or_reservation_id") or "").strip()]}
                    if str(item.get("order_or_reservation_id") or "").strip() in reservation_resulting_order_id
                    else {}
                ),
            }
            if isinstance(item, dict)
            else item
            for item in orders
        ]
    return ordered_runs


def find_daily_history(
    runs_root: Path,
    target_started_at: str,
    *,
    calendar_days: int = 30,
) -> list[dict[str, Any]]:
    if calendar_days < 1:
        return []
    try:
        target_date = date.fromisoformat(target_started_at[:10])
    except ValueError:
        return []
    first_date = target_date - timedelta(days=calendar_days - 1)
    latest_by_date: dict[date, dict[str, Any]] = {}
    for path in runs_root.iterdir():
        summary_path = path / "pipeline-summary.json"
        if not path.is_dir() or not summary_path.is_file():
            continue
        summary = load_json(summary_path)
        started_at = str(summary.get("started_at") or "")
        if not started_at or started_at > target_started_at:
            continue
        try:
            started_date = date.fromisoformat(started_at[:10])
        except ValueError:
            continue
        if started_date < first_date or started_date > target_date:
            continue
        account = summary.get("account_display_summary")
        account = account if isinstance(account, dict) else {}
        try:
            float(account.get("total_evaluation_amount"))
            float(account.get("total_pnl_amount"))
        except (TypeError, ValueError):
            continue
        prior = latest_by_date.get(started_date)
        if prior is not None and str(prior["summary"].get("started_at") or "") >= started_at:
            continue
        latest_by_date[started_date] = {
            "path": path,
            "summary": summary,
            "decision": load_json(path / "decision-brief.json"),
            "market": load_json(path / "market-index-snapshot.json"),
        }
    return [latest_by_date[key] for key in sorted(latest_by_date)]


def order_link_ids(item: dict[str, Any]) -> set[str]:
    ids = {
        str(item.get("order_or_reservation_id") or "").strip(),
        str(item.get("resulting_order_id") or "").strip(),
    }
    ids.discard("")
    return ids


def resolve_fill(item: dict[str, Any], fill_by_order: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    # The resulting cash-order id is the authoritative fill identifier once a reservation is
    # processed into a cash order; only fall back to the submission id when it is absent.
    for order_id in (
        str(item.get("resulting_order_id") or "").strip(),
        str(item.get("order_or_reservation_id") or "").strip(),
    ):
        if not order_id:
            continue
        fill = fill_by_order.get(order_id)
        if fill is not None:
            return fill
    return None


def execution_counts(execution: dict[str, Any]) -> tuple[int, int, int]:
    orders = execution.get("orders") if isinstance(execution.get("orders"), list) else []
    submitted = sum(1 for item in orders if isinstance(item, dict) and item.get("result") == "submitted")
    blocked = sum(1 for item in orders if isinstance(item, dict) and item.get("result") in {"blocked", "failed"})
    skipped = sum(1 for item in orders if isinstance(item, dict) and item.get("result") == "skipped")
    return submitted, blocked, skipped


def broker_reconciliation(order: dict[str, Any]) -> dict[str, Any]:
    value = order.get("broker_reconciliation")
    return value if isinstance(value, dict) else {}


def broker_status_text(order: dict[str, Any]) -> str:
    broker = broker_reconciliation(order)
    status = str(broker.get("status") or "")
    if status == "filled":
        return f"KIS 체결 {number(broker.get('filled_quantity'))}주"
    if status == "partially_filled":
        return f"KIS 일부 체결 {number(broker.get('filled_quantity'))}주 · 잔량 {number(broker.get('remaining_quantity'))}주"
    if status == "partially_filled_rejected":
        return f"KIS 일부 체결 {number(broker.get('filled_quantity'))}주 · 잔여 거절"
    if status == "partially_filled_canceled":
        return f"KIS 일부 체결 {number(broker.get('filled_quantity'))}주 · 잔여 취소"
    if status == "rejected":
        return f"KIS 거절 {number(broker.get('rejected_quantity'))}주"
    if status == "canceled":
        return f"KIS 취소 {number(broker.get('canceled_quantity'))}주"
    if status in {"pending", "accepted"}:
        return f"KIS 미체결 · 잔량 {number(broker.get('remaining_quantity'))}주"
    if status == "unconfirmed":
        return "KIS 상태 미확인"
    return ""


ADVERSE_TERMINAL_BROKER_STATUSES = {
    "rejected",
    "canceled",
    "partially_filled_rejected",
    "partially_filled_canceled",
}


def requested_order_quantity(order: dict[str, Any]) -> int:
    return int(order.get("validated_order_quantity") or order.get("quantity") or 0)


def attempted_order_quantity(order: dict[str, Any]) -> int:
    return int(
        order.get("requested_order_quantity")
        or order.get("validated_order_quantity")
        or order.get("quantity")
        or 0
    )


def order_reason_label(order: dict[str, Any]) -> str:
    raw = str(order.get("reason") or "")
    return ORDER_REASON_LABELS.get(raw, raw or "-")


LIFECYCLE_ONLY_REASONS = {
    "active_order_cancel_submitted",
    "active_order_correction_submitted",
    # Legacy same-day artifact shape: a cancel followed by an immediate resubmission, reported as
    # a single row. It replaces an existing order rather than opening a fresh position, so it is
    # classified alongside "correction" (not counted as an ordinary new buy/sell submission).
    "active_order_cancel_and_replacement_submitted",
}


def order_lifecycle_kind(order: dict[str, Any]) -> str:
    """Classify an execution order row so lifecycle-only cancel/correction submissions are never
    confused with ordinary new buy/sell orders (direction=none is not the same as a sell)."""
    reason = str(order.get("reason") or "")
    result = str(order.get("result") or "")
    direction = str(order.get("direction") or "")
    if result == "submitted":
        if reason == "active_order_cancel_submitted":
            return "cancellation"
        if reason in {"active_order_correction_submitted", "active_order_cancel_and_replacement_submitted"}:
            return "correction"
        if direction in {"buy", "sell"}:
            return direction
        return "none"
    if result in {"blocked", "failed"}:
        return "blocked"
    return "none"


def order_direction_label(order: dict[str, Any]) -> str:
    """Lifecycle reason takes precedence over direction: a real correction row still carries a
    genuine buy/sell direction (the corrected order's side), so checking direction first would
    still mislabel it as an ordinary 매수/매도 instead of 정정. direction=none must also never be
    shown as a sell just because it isn't "buy"."""
    reason = str(order.get("reason") or "")
    if reason in {"active_order_correction_submitted", "active_order_cancel_and_replacement_submitted"}:
        return "정정"
    if reason == "active_order_cancel_submitted":
        return "취소"
    direction = str(order.get("direction") or "")
    if direction == "buy":
        return "매수"
    if direction == "sell":
        return "매도"
    return "-"


def lifecycle_split_counts(orders: list[dict[str, Any]]) -> tuple[int, int, int]:
    """(new_order_count, correction_count, cancellation_count) among submitted orders, via
    order_lifecycle_kind, so counts distinguish new submissions from correction/cancellation."""
    new_count = 0
    correction_count = 0
    cancellation_count = 0
    for order in orders:
        kind = order_lifecycle_kind(order)
        if kind in {"buy", "sell"}:
            new_count += 1
        elif kind == "correction":
            correction_count += 1
        elif kind == "cancellation":
            cancellation_count += 1
    return new_count, correction_count, cancellation_count


def blocked_attempt_detail(order: dict[str, Any]) -> str:
    attempts = order.get("attempts") if isinstance(order.get("attempts"), list) else []
    for attempt in reversed(attempts):
        if isinstance(attempt, dict) and attempt.get("message"):
            return str(attempt.get("message"))
    return ""


def fresh_recheck_audit_summary(order: dict[str, Any]) -> str:
    """Compact display of the sanitized fresh pre-submit profit_protection/concentration_rebalance
    recheck audit (checked_at, fresh holding qty, pass/fail outcomes, approved bound values) --
    persisted for both pass and fail paths, not just failures."""
    audit_list = order.get("fresh_recheck_audit") if isinstance(order.get("fresh_recheck_audit"), list) else []
    if not audit_list:
        return ""
    parts = []
    for entry in audit_list:
        if not isinstance(entry, dict):
            continue
        piece = f"재확인 {time_text(entry.get('checked_at'))} · 보유 {number(entry.get('fresh_holding_quantity'))}주"
        if "pnl_verification_outcome" in entry:
            piece += f" · 손익검증 {'통과' if entry.get('pnl_verification_outcome') else '실패'}"
        if "reduction_bound_outcome" in entry:
            piece += (
                f" · 축소한도 {'통과' if entry.get('reduction_bound_outcome') else '실패'}"
                f"(승인 {decimal(entry.get('approved_max_reduction_pct'), 1)}%)"
            )
        if "concentration_outcome" in entry:
            piece += (
                f" · 집중도 {'통과' if entry.get('concentration_outcome') else '실패'}"
                f"(승인상한 {decimal(entry.get('approved_concentration_cap_pct'), 1)}%)"
            )
        parts.append(piece)
    return " / ".join(parts)


def fill_is_complete(order: dict[str, Any], fill: dict[str, Any] | None) -> bool:
    if not fill:
        return False
    requested_quantity = requested_order_quantity(order)
    filled_quantity = int(fill.get("filled_quantity") or 0)
    return requested_quantity > 0 and filled_quantity >= requested_quantity


def order_status_text(order: dict[str, Any], fill: dict[str, Any] | None = None) -> str:
    broker_status = str(broker_reconciliation(order).get("status") or "")
    if broker_status in ADVERSE_TERMINAL_BROKER_STATUSES:
        return broker_status_text(order)
    if fill:
        if fill_is_complete(order, fill):
            return f"체결 {time_text(fill.get('filled_at'))}"
        requested_quantity = requested_order_quantity(order)
        filled_quantity = int(fill.get("filled_quantity") or 0)
        if requested_quantity > 0:
            return f"일부 체결 {number(filled_quantity)}/{number(requested_quantity)}주 · {time_text(fill.get('filled_at'))}"
    return broker_status_text(order) or "주문 제출 · 체결 미확인"


def order_status_badge(order: dict[str, Any], fill: dict[str, Any] | None = None) -> str:
    broker_status = str(broker_reconciliation(order).get("status") or "")
    is_complete = fill_is_complete(order, fill) or (not fill and broker_status == "filled")
    css = "ok" if is_complete and broker_status not in ADVERSE_TERMINAL_BROKER_STATUSES else "warn"
    return f'<span class="badge {css}">{esc(order_status_text(order, fill))}</span>'


def index_map(market: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = market.get("indexes") if isinstance(market.get("indexes"), list) else []
    return {str(item.get("symbol")): item for item in rows if isinstance(item, dict)}


def cumulative_today_fills(runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, str]:
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    latest_status = "unavailable"
    latest_scope = "unknown"
    for run in runs:
        payload = load_json(run["path"] / "today-fills.json")
        latest_status = str(payload.get("status") or "unavailable")
        if payload.get("skipped"):
            latest_status = "skipped"
        latest_scope = str(payload.get("fill_scope") or "universe")
        fills = payload.get("fills") if isinstance(payload.get("fills"), list) else []
        for item in fills:
            if not isinstance(item, dict):
                continue
            order_id = str(item.get("order_id") or "")
            key = (
                ("order", order_id)
                if order_id
                else (
                    "anonymous",
                    str(item.get("symbol_id") or ""),
                    str(item.get("direction") or ""),
                    str(item.get("filled_at") or ""),
                    int(item.get("filled_quantity") or 0),
                    int(item.get("filled_price") or 0),
                )
            )
            by_key[key] = item
    fills = sorted(by_key.values(), key=lambda item: (str(item.get("filled_at") or ""), str(item.get("order_id") or "")))
    return fills, latest_status, latest_scope


def render_header(summary: dict[str, Any], run_count: int, fills: list[dict[str, Any]], submitted_orders: list[dict[str, Any]]) -> str:
    legacy_account = summary.get("account_display_summary") if isinstance(summary.get("account_display_summary"), dict) else {}
    legacy_asset = summary.get("account_asset_summary") if isinstance(summary.get("account_asset_summary"), dict) else {}
    reporting_view = summary.get("reporting_view") if isinstance(summary.get("reporting_view"), dict) else {}
    reporting_account = reporting_view.get("account") if isinstance(reporting_view.get("account"), dict) else {}
    full_account_view = (
        reporting_account.get("full_account") if isinstance(reporting_account.get("full_account"), dict) else {}
    )
    domestic_account_view = (
        reporting_account.get("domestic_trading_account") if isinstance(reporting_account.get("domestic_trading_account"), dict) else {}
    )
    # Prefer the normalized view; fall back to the raw legacy fields only when the normalized
    # view is absent entirely (older pipeline-summary.json artifacts). A present-but-unavailable
    # normalized view must keep its own (unknown) values instead of being masked by legacy data.
    account = domestic_account_view if domestic_account_view else legacy_account
    full_account_amount = full_account_view.get("total_asset_amount") if full_account_view else legacy_asset.get("total_asset_amount")
    started_at = str(summary.get("started_at") or "")
    cut_off = time_text(started_at)
    status = str(summary.get("status") or "-")
    run_id = str(summary.get("run_id") or "-")
    run_suffix = run_id.rpartition("-")[2]
    run_id_hint = ""
    if len(run_suffix) == 8 and all(character in "0123456789abcdefABCDEF" for character in run_suffix):
        run_id_hint = "<small>마지막 8자리는 해시가 아니라 같은 초에 시작한 실행을 구분하는 임의 식별자입니다.</small>"
    status_label = "실행 성공" if status == "success" else "부분 완료" if status == "partial" else "실행 실패"
    status_css = "success" if status == "success" else ""
    asset_metric = ""
    if full_account_amount is not None:
        asset_metric = (
            f"<article><span>KIS 총자산</span><strong>{number(full_account_amount)}원</strong>"
            "<small>account asset 조회값 · 원금 수익률 아님</small></article>"
        )
    new_order_count, correction_count, cancellation_count = lifecycle_split_counts(submitted_orders)
    lifecycle_chip = (
        f"<span class=\"chip\">정정·취소 {correction_count + cancellation_count}건</span>"
        if (correction_count + cancellation_count) > 0
        else ""
    )
    return f"""
    <header class="hero">
      <div class="eyebrow">CUMULATIVE DAILY TRADING REPORT</div>
      <h1>당일 누적 거래·판단 리포트</h1>
      <p>{esc(cut_off)}까지 확인된 당일 run·주문·체결과 각 시간대의 Analyst/Judge 판단을 함께 보여줍니다.</p>
      <div class="chips">
        <span class="chip {status_css}">● {esc(status_label)}</span>
        <span class="chip">{esc(started_at)}</span>
        <span class="chip">run {run_count}회</span>
        <span class="chip">체결 {len(fills)}건</span>
        {lifecycle_chip}
        <span class="chip">봇 신규주문 {new_order_count}건</span>
      </div>
      <div class="run-id"><span>실행 ID</span><code>{esc(run_id)}</code>{run_id_hint}</div>
    </header>
    <section class="metrics">
      <article><span>국내매매 총평가</span><strong>{number(account.get('total_evaluation_amount'))}원</strong><small>주식 {number(account.get('securities_valuation_amount'))}원</small></article>
      <article><span>평가손익</span><strong class="negative">{number(account.get('total_pnl_amount'))}원</strong><small>전체 매입가 대비</small></article>
      <article><span>주문가능</span><strong>{number(account.get('orderable_cash_amount'))}원</strong><small>D+2 기준</small></article>
      <article><span>당일 누적 매수</span><strong>{number(((legacy_account.get('today_trade_amounts') or {}).get('buy_amount')))}원</strong><small>계좌 누계</small></article>
      {asset_metric}
    </section>
    """


def render_trade_ledger(
    runs: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    fill_status: str,
    fill_scope: str,
) -> tuple[str, list[dict[str, Any]]]:
    fill_by_order = {str(item.get("order_id")): item for item in fills if item.get("order_id")}
    submitted_orders: list[dict[str, Any]] = []
    blocked_orders: list[dict[str, Any]] = []
    for run in runs:
        started_at = run["summary"].get("started_at")
        orders = run["execution"].get("orders") if isinstance(run["execution"].get("orders"), list) else []
        for item in orders:
            if not isinstance(item, dict):
                continue
            if item.get("result") == "submitted":
                row = dict(item)
                row["run_started_at"] = started_at
                row["fill"] = resolve_fill(item, fill_by_order)
                submitted_orders.append(row)
            elif item.get("result") in {"blocked", "failed"}:
                row = dict(item)
                row["run_started_at"] = started_at
                blocked_orders.append(row)

    ledger_rows: list[tuple[str, str]] = []
    linked_order_ids: set[str] = set()
    for item in submitted_orders:
        fill = item.get("fill") if isinstance(item.get("fill"), dict) else None
        result = order_status_badge(item, fill)
        direction = order_direction_label(item)
        order_id = str(item.get("order_or_reservation_id") or "")
        resulting_order_id = str(item.get("resulting_order_id") or "")
        order_id_cell = (
            f"{esc(order_id)}<br><small>→ {esc(resulting_order_id)}</small>" if resulting_order_id else esc(order_id)
        )
        if fill is not None:
            linked_order_ids.update(order_link_ids(item))
        time_cell = f"주문 {time_text(item.get('run_started_at'))}"
        fill_cell = "-"
        if fill is not None:
            time_cell += f"<br><small>체결 {time_text(fill.get('filled_at'))}</small>"
            fill_cell = (
                f"{number(fill.get('filled_quantity'))}주<br>"
                f"<small>{number(fill.get('filled_price'))}원 · {number(fill.get('filled_amount'))}원</small>"
            )
        submitted_audit_summary = fresh_recheck_audit_summary(item)
        submitted_result_cell = (
            f"{result}<br><small>재확인 감사: {esc(submitted_audit_summary)}</small>" if submitted_audit_summary else result
        )
        ledger_rows.append(
            (
                str(item.get("run_started_at") or ""),
                f"<tr><td>{time_cell}</td><td>봇</td>"
                f"<td><strong>{esc(item.get('symbol_name'))}</strong><br><code>{esc(item.get('symbol_id'))}</code></td>"
                f"<td>{direction}</td>"
                f"<td>{number(item.get('validated_order_quantity') or item.get('quantity'))}주<br><small>{number(item.get('order_price'))}원</small></td>"
                f"<td>{fill_cell}</td><td><code>{order_id_cell}</code></td><td>{submitted_result_cell}</td></tr>",
            )
        )

    for item in fills:
        order_id = str(item.get("order_id") or "")
        if order_id in linked_order_ids:
            continue
        actor = "사용자 직접" if item.get("source_actor") == "non_bot_user" else "봇"
        direction = "매수" if item.get("direction") == "buy" else "매도"
        ledger_rows.append(
            (
                str(item.get("filled_at") or ""),
                f"<tr><td>체결 {time_text(item.get('filled_at'))}</td><td>{esc(actor)}</td>"
                f"<td><strong>{esc(item.get('symbol_name'))}</strong><br><code>{esc(item.get('symbol_id'))}</code></td>"
                f"<td>{direction}</td><td>-</td>"
                f"<td>{number(item.get('filled_quantity'))}주<br><small>{number(item.get('filled_price'))}원 · {number(item.get('filled_amount'))}원</small></td>"
                f"<td><code>{esc(order_id)}</code></td><td><span class=\"badge ok\">체결 확인</span></td></tr>",
            )
        )

    for item in blocked_orders:
        direction = order_direction_label(item)
        is_failed = item.get("result") == "failed"
        result_text = "실패" if is_failed else "차단"
        reason_text = order_reason_label(item)
        detail = blocked_attempt_detail(item)
        detail_html = f"<br><small>{esc(detail)}</small>" if detail else ""
        audit_summary = fresh_recheck_audit_summary(item)
        audit_html = f"<br><small>재확인 감사: {esc(audit_summary)}</small>" if audit_summary else ""
        ledger_rows.append(
            (
                str(item.get("run_started_at") or ""),
                f"<tr class=\"blocked-row\"><td>시도 {time_text(item.get('run_started_at'))}</td><td>봇</td>"
                f"<td><strong>{esc(item.get('symbol_name'))}</strong><br><code>{esc(item.get('symbol_id'))}</code></td>"
                f"<td>{direction}</td>"
                f"<td>{number(attempted_order_quantity(item))}주 요청<br><small>{number(item.get('order_price'))}원</small></td>"
                f"<td>-</td><td>-</td>"
                f"<td><span class=\"badge bad\">{esc(result_text)}</span><br><small>{esc(reason_text)}</small>{detail_html}{audit_html}</td></tr>",
            )
        )
    ledger_html = "".join(row for _, row in sorted(ledger_rows, key=lambda item: item[0]))
    ledger_new_count, ledger_correction_count, ledger_cancellation_count = lifecycle_split_counts(submitted_orders)

    cut_off = time_text(runs[-1]["summary"].get("started_at")) if runs else "-"
    if fill_status == "success" and fill_scope == "account":
        fill_notice = "계좌 전체 일별 체결 조회를 기준으로 주문번호를 제출 run에 연결합니다. 보고서 시점 이후 체결은 추정하지 않습니다."
    else:
        fill_notice = (
            f"체결 수집 상태 {fill_status}, 범위 {fill_scope}이므로 계좌의 당일 전체 체결로 확정할 수 없습니다. "
            "확인된 artifact만 표시합니다."
        )
    content = f"""
    <section class="panel" id="trades">
      <div class="section-head"><div><p class="kicker">DAY LEDGER</p><h2>{esc(cut_off)}까지의 당일 전체 거래</h2></div><span class="badge info">체결 {len(fills)} · 봇 신규주문 {ledger_new_count} · 정정·취소 {ledger_correction_count + ledger_cancellation_count} · 시도 차단/실패 {len(blocked_orders)}</span></div>
      <div class="notice">{esc(fill_notice)}</div>
      <h3>주문·체결 통합 원장</h3>
      <div class="table-wrap"><table><thead><tr><th>시각</th><th>주체</th><th>종목</th><th>방향</th><th>주문</th><th>체결</th><th>주문번호</th><th>{esc(cut_off)} 기준 상태</th></tr></thead><tbody>{ledger_html or '<tr><td colspan="8">확인된 주문·체결 없음</td></tr>'}</tbody></table></div>
    </section>
    """
    return content, submitted_orders


THESIS_ASSESSMENT_LABELS = {"intact": "유지", "damaged": "훼손", "uncertain": "불확실"}
THESIS_ASSESSMENT_CSS = {"intact": "ok", "damaged": "bad", "uncertain": "warn"}
THESIS_GATE_REASON_LABELS = {
    "no_prior_thesis": "유효한 이전 thesis 없음",
    "thesis_not_damaged": "thesis 훼손 판단 아님",
    "invalidation_condition_not_matched": "무효화 조건이 이전 thesis와 불일치",
    "evidence_not_verified": "인용 근거 검증 실패",
    "damaged_evidence_confirmed": "훼손 근거 검증 완료",
}


def render_thesis_condition_items(conditions: Any, matched_ids: set[str]) -> str:
    rows = []
    for condition in conditions if isinstance(conditions, list) else []:
        if not isinstance(condition, dict):
            continue
        condition_id = str(condition.get("condition_id") or "")
        is_matched = condition_id in matched_ids
        matched_badge = '<span class="badge bad">훼손 근거 일치</span> ' if is_matched else ""
        rows.append(
            f"<li class=\"thesis-condition{' matched' if is_matched else ''}\">{matched_badge}"
            f"{esc(condition.get('description'))}"
            f" <span class=\"thesis-cond-key muted\">(내부 비교 키: <code>{esc(condition_id)}</code>)</span></li>"
        )
    return "".join(rows) or "<li>무효화 조건 없음</li>"


def render_thesis_section(final_item: dict[str, Any]) -> str:
    prior_context = final_item.get("prior_thesis_context") if isinstance(final_item.get("prior_thesis_context"), dict) else None
    assessment = final_item.get("thesis_assessment") if isinstance(final_item.get("thesis_assessment"), dict) else None
    gate = final_item.get("protected_loss_gate") if isinstance(final_item.get("protected_loss_gate"), dict) else None
    successor = final_item.get("thesis_definition") if isinstance(final_item.get("thesis_definition"), dict) else None
    if prior_context is None and assessment is None and gate is None and successor is None:
        return ""

    blocks = []
    matched_ids = set(assessment.get("matched_invalidation_condition_ids") or []) if assessment else set()

    if prior_context is not None:
        if prior_context.get("available"):
            blocks.append(
                "<div class=\"thesis-prior\"><h4>이전 thesis (이번 run 평가 대상)</h4>"
                f"<p class=\"thesis-source\">출처: run <code>{esc(prior_context.get('source_run_id'))}</code>"
                f" · {esc(full_time_text(prior_context.get('source_started_at')))}"
                f" · <code>{esc(prior_context.get('source_artifact'))}</code></p>"
                f"<p>{esc(prior_context.get('core_rationale'))}</p>"
                f"<ul class=\"thesis-conditions\">{render_thesis_condition_items(prior_context.get('invalidation_conditions'), matched_ids)}</ul></div>"
            )
        else:
            blocks.append(
                '<div class="thesis-prior"><h4>이전 thesis</h4>'
                '<p class="thesis-source muted">유효한 이전 thesis 없음 (이번 run이 신규 등록 대상)</p></div>'
            )

    if assessment is not None:
        status = str(assessment.get("status") or "uncertain")
        status_label = THESIS_ASSESSMENT_LABELS.get(status, status)
        status_css = THESIS_ASSESSMENT_CSS.get(status, "muted")
        cited = assessment.get("cited_argument_ids") or []
        cited_text = ", ".join(f"<code>{esc(value)}</code>" for value in cited) if cited else "-"
        blocks.append(
            "<div class=\"thesis-assessment\"><h4>이번 run 판단</h4>"
            f"<p><span class=\"badge {status_css}\">{esc(status_label)}</span></p>"
            f"<p>인용 근거: {cited_text}</p></div>"
        )

    if gate is not None:
        allowed = bool(gate.get("allowed"))
        reason_code = str(gate.get("reason") or "")
        reason_label = THESIS_GATE_REASON_LABELS.get(reason_code, reason_code)
        blocks.append(
            "<div class=\"thesis-gate\"><h4>손실 보유 종목 감축 요건</h4>"
            f"<p><span class=\"badge {'ok' if allowed else 'bad'}\">{'요건 충족' if allowed else '요건 미충족'}</span>"
            f" {esc(reason_label)} <code>{esc(reason_code)}</code></p></div>"
        )

    if successor is not None:
        blocks.append(
            "<div class=\"thesis-successor\"><h4>신규/후속 thesis (이후 run 적용)</h4>"
            f"<p>{esc(successor.get('core_rationale'))}</p>"
            f"<ul class=\"thesis-conditions\">{render_thesis_condition_items(successor.get('invalidation_conditions'), set())}</ul></div>"
        )

    return f'<div class="thesis-block">{"".join(blocks)}</div>'


def render_time_symbol_inspector(runs: list[dict[str, Any]], fills: list[dict[str, Any]]) -> str:
    # A preflight-only run never reached Analyst/Judge -- it must not appear in the time wheel as
    # an empty Analyst/Judge run.
    runs = [run for run in runs if not run.get("is_preflight_only")]
    fill_by_order = {
        str(item.get("order_id")): item
        for item in fills
        if isinstance(item, dict) and item.get("order_id")
    }
    order_run_index: dict[str, int] = {}
    for index, run in enumerate(runs):
        execution_orders = run["execution"].get("orders") if isinstance(run["execution"].get("orders"), list) else []
        for order in execution_orders:
            if not isinstance(order, dict) or order.get("result") != "submitted":
                continue
            for order_id in order_link_ids(order):
                order_run_index[order_id] = index

    fills_by_run: dict[int, list[dict[str, Any]]] = {index: [] for index in range(len(runs))}
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        order_id = str(fill.get("order_id") or "")
        if order_id in order_run_index:
            target_index = order_run_index[order_id]
        else:
            fill_time = str(fill.get("filled_at") or "")
            target_index = next(
                (
                    index
                    for index, run in enumerate(runs)
                    if str(run["summary"].get("started_at") or "") >= fill_time
                ),
                len(runs) - 1,
            )
        fills_by_run[target_index].append(fill)

    time_buttons = []
    time_panels = []
    for run_index, run in enumerate(runs):
        run_dir = run["path"]
        summary = run["summary"]
        started_at = str(summary.get("started_at") or "")
        run_time = time_text(started_at)
        time_key = f"run-{run_index}-{run_time.replace(':', '')}"
        is_active_time = run_index == len(runs) - 1

        execution_orders = run["execution"].get("orders") if isinstance(run["execution"].get("orders"), list) else []
        execution_by_symbol = {
            str(item.get("symbol_id")): item for item in execution_orders if isinstance(item, dict)
        }
        run_review_summary = run["summary"].get("review_summary") if isinstance(run["summary"].get("review_summary"), dict) else {}
        review_summary_by_symbol = {
            str(item.get("symbol_id")): item
            for item in run_review_summary.get("symbols", [])
            if isinstance(item, dict)
        }
        submitted_orders = [
            item for item in execution_orders if isinstance(item, dict) and item.get("result") == "submitted"
        ]
        blocked_orders = [
            item for item in execution_orders if isinstance(item, dict) and item.get("result") in {"blocked", "failed"}
        ]
        linked_fills = fills_by_run[run_index]
        submitted_order_ids: set[str] = set()
        for item in submitted_orders:
            submitted_order_ids.update(order_link_ids(item))
        unmatched_fills = [
            item for item in linked_fills if str(item.get("order_id") or "") not in submitted_order_ids
        ]
        analyst = load_json(run_dir / "analyst-review.json")
        debate = load_json(run_dir / "judge-debate.json")
        debate_argument_index = build_debate_argument_index(debate)
        final = load_json(run_dir / "judge-review.json")
        final_by_symbol = {
            str(item.get("symbol_id")): item for item in final.get("symbols", []) if isinstance(item, dict)
        }
        judge_spec = load_json(run_dir / "judge-review-spec.json")
        judge_scope_reasons = judge_spec.get("review_scope_reasons") if isinstance(judge_spec.get("review_scope_reasons"), dict) else {}
        # Some v1 judge-review-spec.json artifacts exist and contain other keys but do not carry
        # review_scope_reasons. Presence of the file/dict alone therefore does not prove that
        # "not selected" can be reconstructed.
        has_judge_scope_metadata = isinstance(judge_spec.get("review_scope_reasons"), dict)
        judge_scope_resolved = set(final_by_symbol)
        # held_position/unheld_score_rank distinguish WHY a symbol entered Judge scope;
        # not_selected (scored but never in scope) and in-scope-unresolved (in scope but Judge
        # never returned a valid result) are separate categories from either of those.
        judge_held_count = sum(1 for reason in judge_scope_reasons.values() if reason == "held_position")
        judge_unheld_count = sum(1 for reason in judge_scope_reasons.values() if reason == "unheld_score_rank")
        judge_scope_unresolved_count = sum(
            1
            for symbol_id in judge_scope_reasons
            if str(symbol_id).strip() and str(symbol_id).strip() not in judge_scope_resolved
        )
        judge_scored_symbol_ids = {
            str(item.get("symbol_id") or "")
            for item in analyst.get("symbols", [])
            if isinstance(item, dict)
            and item.get("symbol_id")
            and valid_analyst_score(item.get("final_first_score"))
        }
        judge_not_selected_count = len(judge_scored_symbol_ids - set(judge_scope_reasons)) if has_judge_scope_metadata else 0
        new_submitted_orders = [
            item for item in submitted_orders if order_lifecycle_kind(item) in {"buy", "sell"}
        ]
        lifecycle_submitted_orders = [
            item for item in submitted_orders if order_lifecycle_kind(item) in {"correction", "cancellation"}
        ]
        trade_symbol_ids = {str(item.get("symbol_id") or "") for item in new_submitted_orders}
        trade_symbol_ids.update(str(item.get("symbol_id") or "") for item in linked_fills)
        attempt_symbol_ids = {str(item.get("symbol_id") or "") for item in blocked_orders}
        attempt_symbol_ids.update(str(item.get("symbol_id") or "") for item in lifecycle_submitted_orders)
        analyst_symbols = sorted(
            (item for item in analyst.get("symbols", []) if isinstance(item, dict)),
            key=lambda item: analyst_symbol_sort_key(item, final_by_symbol, trade_symbol_ids, attempt_symbol_ids),
        )
        decision_by_symbol = {
            str(item.get("symbol_id")): item for item in run["decision"].get("symbols", []) if isinstance(item, dict)
        }

        preferred_symbol = ""
        if new_submitted_orders:
            preferred_symbol = str(new_submitted_orders[0].get("symbol_id") or "")
        elif linked_fills:
            preferred_symbol = str(linked_fills[0].get("symbol_id") or "")
        elif lifecycle_submitted_orders:
            preferred_symbol = str(lifecycle_submitted_orders[0].get("symbol_id") or "")
        elif blocked_orders:
            preferred_symbol = str(blocked_orders[0].get("symbol_id") or "")
        elif final_by_symbol:
            preferred_symbol = next(iter(final_by_symbol))
        elif analyst_symbols:
            preferred_symbol = str(analyst_symbols[0].get("symbol_id") or "")

        activity_cards = []
        for order in submitted_orders:
            lifecycle_kind = order_lifecycle_kind(order)
            direction = order_direction_label(order)
            order_id = str(order.get("order_or_reservation_id") or "")
            resulting_order_id = str(order.get("resulting_order_id") or "")
            order_id_text = f"{order_id} → {resulting_order_id}" if resulting_order_id else order_id
            fill = resolve_fill(order, fill_by_order)
            status_text = order_status_text(order, fill)
            if lifecycle_kind == "cancellation":
                activity_cards.append(
                    f"<article class=\"activity-card order\"><span>{esc(status_text)}</span>"
                    f"<strong>{esc(order.get('symbol_name'))} 기존주문 취소</strong>"
                    f"<small>처리 {run_time} · <code>{esc(order_id_text)}</code></small></article>"
                )
            elif lifecycle_kind == "correction":
                corrected_quantity = number(order.get("validated_order_quantity") or order.get("quantity"))
                corrected_side = (
                    "매수" if order.get("direction") == "buy" else "매도" if order.get("direction") == "sell" else "-"
                )
                activity_cards.append(
                    f"<article class=\"activity-card order\"><span>{esc(status_text)}</span>"
                    f"<strong>{esc(order.get('symbol_name'))} 기존주문 정정({esc(corrected_side)} {corrected_quantity}주)</strong>"
                    f"<small>처리 {run_time} · <code>{esc(order_id_text)}</code></small></article>"
                )
            elif fill_is_complete(order, fill) and str(broker_reconciliation(order).get("status") or "") not in ADVERSE_TERMINAL_BROKER_STATUSES:
                activity_cards.append(
                    f"<article class=\"activity-card filled\"><span>주문 후 체결</span><strong>{esc(order.get('symbol_name'))} {esc(direction)} {number(fill.get('filled_quantity'))}주</strong>"
                    f"<small>주문 {run_time} · {number(order.get('order_price'))}원 → 체결 {time_text(fill.get('filled_at'))} · {number(fill.get('filled_price'))}원 · <code>{esc(order_id_text)}</code></small></article>"
                )
            else:
                activity_cards.append(
                    f"<article class=\"activity-card order\"><span>{esc(status_text)}</span><strong>{esc(order.get('symbol_name'))} {esc(direction)} {number(order.get('validated_order_quantity') or order.get('quantity'))}주</strong>"
                    f"<small>주문 {run_time} · {number(order.get('order_price'))}원 · <code>{esc(order_id_text)}</code></small></article>"
                )
        for fill in unmatched_fills:
            actor = "사용자 직접" if fill.get("source_actor") == "non_bot_user" else "연결 주문 없음"
            direction = "매수" if fill.get("direction") == "buy" else "매도"
            activity_cards.append(
                f"<article class=\"activity-card fill\"><span>{esc(actor)} 체결</span><strong>{esc(fill.get('symbol_name'))} {esc(direction)} {number(fill.get('filled_quantity'))}주</strong>"
                f"<small>체결 {time_text(fill.get('filled_at'))} · {number(fill.get('filled_price'))}원 · <code>{esc(fill.get('order_id'))}</code></small></article>"
            )
        for order in blocked_orders:
            direction = order_direction_label(order)
            result_text = "실패" if order.get("result") == "failed" else "차단"
            reason_text = order_reason_label(order)
            detail = blocked_attempt_detail(order)
            detail_suffix = f" · {esc(detail)}" if detail else ""
            activity_cards.append(
                f"<article class=\"activity-card blocked\"><span>{esc(direction)} 시도 {esc(result_text)}</span>"
                f"<strong>{esc(order.get('symbol_name'))} {esc(direction)} {number(attempted_order_quantity(order))}주 요청</strong>"
                f"<small>주문 {run_time} · {esc(reason_text)}{detail_suffix}</small></article>"
            )
        if not activity_cards:
            activity_cards.append('<div class="empty-state">이 run에 연결된 주문 또는 체결이 없습니다.</div>')

        time_button_new_count, time_button_correction_count, time_button_cancellation_count = lifecycle_split_counts(
            submitted_orders
        )
        time_buttons.append(
            f'<button type="button" role="option" aria-selected="{str(is_active_time).lower()}" class="time-button{" active" if is_active_time else ""}" data-time-target="{esc(time_key)}">'
            f'<strong>{esc(run_time)}</strong><span>Analyst {len(analyst_symbols)} · Judge {len(final_by_symbol)}</span>'
            f'<small>신규주문 {time_button_new_count} · 정정·취소 {time_button_correction_count + time_button_cancellation_count} · 체결 {len(linked_fills)} · 차단 {len(blocked_orders)}</small></button>'
        )

        symbol_buttons = []
        symbol_panels = []
        for analyst_item in analyst_symbols:
            symbol_id = str(analyst_item.get("symbol_id") or "")
            symbol_name = str(analyst_item.get("symbol_name") or symbol_id)
            composite_key = f"{time_key}-{symbol_id}"
            is_active_symbol = symbol_id == preferred_symbol
            final_item = final_by_symbol.get(symbol_id)
            related_orders = [item for item in submitted_orders if str(item.get("symbol_id") or "") == symbol_id]
            related_fills = [item for item in linked_fills if str(item.get("symbol_id") or "") == symbol_id]
            related_blocked = [item for item in blocked_orders if str(item.get("symbol_id") or "") == symbol_id]
            related_lifecycle_orders = [
                item
                for item in related_orders
                if order_lifecycle_kind(item) in {"correction", "cancellation"}
            ]
            related_new_orders = [
                item for item in related_orders if order_lifecycle_kind(item) in {"buy", "sell"}
            ]
            has_trade = bool(related_new_orders or related_fills)
            has_lifecycle = bool(related_lifecycle_orders)
            has_attempt = bool(related_blocked)
            has_guard_intervention = judge_guard_intervened(final_item)
            judge_scope_status = judge_symbol_scope_status(symbol_id, final_item, judge_scope_reasons, has_judge_scope_metadata)
            judge_label = JUDGE_SCOPE_STATUS_LABELS[judge_scope_status]
            holding_label, holding_badge_class = holding_status_display(decision_by_symbol.get(symbol_id))
            judge_badge_class = {
                "resolved": "judge",
                "unresolved_in_scope": "attempt",
                "not_selected": "analyst",
                "legacy_unknown": "analyst",
            }[judge_scope_status]
            attempt_badge = ""
            if has_trade:
                attempt_badge = "<b class=\"mini-badge trade\">거래</b>"
            elif has_lifecycle:
                attempt_badge = "<b class=\"mini-badge analyst\">주문 정정·취소</b>"
            elif has_attempt:
                attempt_badge = "<b class=\"mini-badge attempt\">시도 차단</b>"
            group_class = " group-trade" if has_trade else " group-guard" if has_guard_intervention else ""
            symbol_buttons.append(
                f'<button type="button" class="trade-symbol-button{group_class}{" active" if is_active_symbol else ""}" data-symbol-target="{esc(composite_key)}">'
                f'<span class="symbol-button-left"><span class="symbol-button-status"><b class="mini-badge {judge_badge_class}">{judge_label}</b>'
                f'<b class="mini-badge {holding_badge_class}">{esc(holding_label)}</b>{attempt_badge}</span>'
                f'<strong class="symbol-button-name" title="{esc(symbol_name)}">{esc(symbol_name)}</strong></span>'
                f'<span class="symbol-button-right"><b class="symbol-score">{decimal(analyst_item.get("final_first_score"))}</b><code>{esc(symbol_id)}</code></span></button>'
            )

            score_rows = []
            for score in analyst_item.get("agent_scores", []):
                if not isinstance(score, dict):
                    continue
                excluded = bool(score.get("excluded_from_aggregation"))
                missing = score.get("missing_data") if isinstance(score.get("missing_data"), list) else []
                score_rows.append(
                    f"<tr><td>{esc(ROLE_LABELS.get(str(score.get('agent_role')), score.get('agent_role')))}</td>"
                    f"<td><strong>{decimal(score.get('score'), 1)}</strong></td>"
                    f"<td>{'<span class=\"badge warn\">평균 제외</span>' if excluded else '<span class=\"badge ok\">평균 포함</span>'}</td>"
                    f"<td>{esc(score.get('reason_code'))}</td><td>{esc(score.get('one_line_reason'))}</td>"
                    f"<td>{esc(', '.join(str(value) for value in missing) or '-')}</td></tr>"
                )

            phase_blocks = []
            for phase in debate.get("phases", []):
                if not isinstance(phase, dict):
                    continue
                side_blocks = []
                for side in ("bull", "bear"):
                    payload = ((phase.get("sides") or {}).get(side) or {}).get("output") or {}
                    symbol_item = next(
                        (
                            item
                            for item in payload.get("symbols", [])
                            if isinstance(item, dict) and str(item.get("symbol_id")) == symbol_id
                        ),
                        None,
                    )
                    if symbol_item is not None:
                        side_blocks.append(render_debate_symbol(symbol_item, side, run_index))
                if side_blocks:
                    phase_blocks.append(
                        f"<section class=\"phase compact-phase\"><div class=\"phase-title\"><span>{esc(PHASE_LABELS.get(str(phase.get('phase')), phase.get('phase')))}</span>"
                        f"<small>{esc(symbol_name)} Bull/Bear 판단</small></div>{''.join(side_blocks)}</section>"
                    )

            if final_item:
                account_exposure = (decision_by_symbol.get(symbol_id) or {}).get("account_exposure") or {}
                decision_evidence_html = render_decision_evidence(
                    final_item.get("one_line_reason"), debate_argument_index, run_index, symbol_id
                )
                decision_guard = final_item.get("decision_guard") if isinstance(final_item.get("decision_guard"), dict) else {}
                guard_status = str(decision_guard.get("status") or "")
                guard_reason = str(decision_guard.get("reason_code") or "")
                guard_status_display = GUARD_STATUS_LABELS.get(guard_status, guard_status)
                guard_reason_display = ORDER_REASON_LABELS.get(guard_reason, guard_reason)
                guard_html = (
                    f"<span class=\"badge {'warn' if guard_status == 'blocked' else 'info'}\">가드(guard) {esc(guard_status_display)}({esc(guard_status)}){(': ' + esc(guard_reason_display) + '(' + esc(guard_reason) + ')') if guard_reason else ''}</span>"
                    if guard_status
                    else ""
                )
                symbol_execution_item = execution_by_symbol.get(symbol_id) or {}
                # Prefer the execution row's own value; when no execution row exists for this
                # symbol at all (common: many Judge-scoped symbols never reach execute_orders in
                # the same run), fall back to review_summary.symbols' expected_holding_quantity,
                # which already has the same current+pending buy-pending sell fallback baked in.
                expected_holding_quantity = symbol_execution_item.get("expected_holding_quantity")
                if expected_holding_quantity is None:
                    review_summary_item = review_summary_by_symbol.get(symbol_id) or {}
                    expected_holding_quantity = review_summary_item.get("expected_holding_quantity")
                # expected_holding_quantity (current + pending buy - pending sell) is the baseline
                # canonical_action was actually decided against, distinct from current->final position change.
                expected_text = (
                    f" → 대기반영 {number(expected_holding_quantity)}주"
                    if expected_holding_quantity is not None
                    else ""
                )
                basis_display = judge_field_display(final_item, "decision_basis", DECISION_BASIS_LABELS)
                requested_action_display = judge_field_display(final_item, "requested_action", CANONICAL_ACTION_LABELS)
                canonical_action_display = judge_field_display(final_item, "canonical_action", CANONICAL_ACTION_LABELS)
                judge_html = (
                    f"<article class=\"final-card full\"><div><h3>Final Judge</h3><span class=\"badge info\">rank {number(final_item.get('relative_attractiveness_rank'))}</span>"
                    f"<span class=\"badge info\">근거(basis) {esc(basis_display)}</span>{guard_html}</div>"
                    f"<div class=\"final-numbers\"><span>현재 {number(account_exposure.get('current_live_holding_quantity'))}주{expected_text}</span>"
                    f"<span>최종 보유수량 {number(final_item.get('final_holding_quantity'))}주</span>"
                    f"<span>요청목표 {number(final_item.get('requested_target_position_value_krw'))}원 → 확정목표 {number(final_item.get('target_position_value_krw'))}원</span>"
                    f"<span>{esc(requested_action_display)} → {esc(canonical_action_display)}(대기반영 기준)</span></div>"
                    f"<p><code>{esc(final_item.get('reason_code'))}</code></p><p>{esc(final_item.get('one_line_reason'))}</p>"
                    f"{decision_evidence_html}{render_thesis_section(final_item)}</article>{''.join(phase_blocks)}"
                )
            elif judge_scope_status == "unresolved_in_scope":
                judge_html = '<div class="empty-state">이 종목은 Judge 심사대상(review_scope)이었지만 이 run의 judge-review.json에 유효한 결과가 없습니다(미해결).</div>'
            elif judge_scope_status == "legacy_unknown":
                judge_html = '<div class="empty-state">구버전(v1) run으로 Judge 심사대상 여부를 판단할 스코프 정보가 없습니다. Judge 결과 없음만 확인됩니다.</div>'
            else:
                judge_html = '<div class="empty-state">Analyst 평가는 완료됐지만 이 run의 Judge 심사대상으로 선정되지 않았습니다.</div>'

            trade_notes = []
            related_order_ids: set[str] = set()
            for order in related_orders:
                related_order_ids.update(order_link_ids(order))
            for order in related_orders:
                lifecycle_kind = order_lifecycle_kind(order)
                direction = order_direction_label(order)
                fill = resolve_fill(order, fill_by_order)
                if lifecycle_kind == "cancellation":
                    trade_notes.append(
                        f"{run_time} 봇 기존주문 취소 제출 · {order_status_text(order, fill)}"
                    )
                elif lifecycle_kind == "correction":
                    corrected_side = (
                        "매수" if order.get("direction") == "buy" else "매도" if order.get("direction") == "sell" else "-"
                    )
                    corrected_quantity = number(order.get("validated_order_quantity") or order.get("quantity"))
                    trade_notes.append(
                        f"{run_time} 봇 기존주문 정정({corrected_side} {corrected_quantity}주) · {order_status_text(order, fill)}"
                    )
                elif fill_is_complete(order, fill) and str(broker_reconciliation(order).get("status") or "") not in ADVERSE_TERMINAL_BROKER_STATUSES:
                    trade_notes.append(
                        f"{run_time} 봇 {direction} 주문 · {time_text(fill.get('filled_at'))} {number(fill.get('filled_quantity'))}주 체결"
                    )
                else:
                    trade_notes.append(
                        f"{run_time} 봇 {direction} {number(order.get('validated_order_quantity') or order.get('quantity'))}주 주문 제출 · {order_status_text(order, fill)}"
                    )
            for fill in related_fills:
                if str(fill.get("order_id") or "") in related_order_ids:
                    continue
                actor = "사용자 직접" if fill.get("source_actor") == "non_bot_user" else "연결 주문 없는"
                direction = "매수" if fill.get("direction") == "buy" else "매도"
                trade_notes.append(
                    f"{time_text(fill.get('filled_at'))} {actor} {direction} {number(fill.get('filled_quantity'))}주 체결"
                )
            for order in related_blocked:
                direction = order_direction_label(order)
                result_text = "실패" if order.get("result") == "failed" else "차단"
                reason_text = order_reason_label(order)
                detail = blocked_attempt_detail(order)
                detail_suffix = f" · {detail}" if detail else ""
                trade_notes.append(
                    f"{run_time} 봇 {direction} {number(attempted_order_quantity(order))}주 시도 {result_text} · {reason_text}{detail_suffix}"
                )
            trade_note = " · ".join(trade_notes) or "이 run에 연결된 거래 없음 · Analyst 평가만 표시"
            symbol_panels.append(
                f"<section class=\"symbol-analysis-panel{' active' if is_active_symbol else ''}\" data-symbol-panel=\"{esc(composite_key)}\">"
                f"<div class=\"symbol-focus-head\"><div><p class=\"kicker\">RUN {esc(run_time)} · SYMBOL ANALYSIS</p><h2>{esc(symbol_name)} <code>{esc(symbol_id)}</code></h2>"
                f"<p>{esc(trade_note)}</p></div><div class=\"focus-badges\"><span class=\"badge info\" title=\"참고용 advisory 점수이며 매수/매도 판단이 아닙니다\">Analyst 참고점수(advisory) {decimal(analyst_item.get('final_first_score'))}</span>"
                f"<span class=\"badge {'ok' if holding_badge_class == 'held' else 'muted'}\">{esc(holding_label)}</span>"
                f"<span class=\"badge {'ok' if final_item else 'muted'}\">{esc(judge_label)}</span></div></div>"
                f"<section class=\"inline-analysis\"><h3>Analyst 상세 점수</h3><div class=\"table-wrap\"><table><thead><tr><th>역할</th><th>점수</th><th>집계</th><th>코드</th><th>상세 근거</th><th>누락 데이터</th></tr></thead><tbody>{''.join(score_rows)}</tbody></table></div></section>"
                f"<section class=\"inline-judge\"><h3>Judge 단계별 판단</h3>{judge_html}</section></section>"
            )

        time_panels.append(
            f'<section class="time-analysis-panel{" active" if is_active_time else ""}" data-time-panel="{esc(time_key)}">'
            f'<div class="time-panel-head"><div><p class="kicker">TIME WINDOW</p><h2>{esc(run_time)} 거래·종목 판단</h2>'
            f'<p>{esc(run_time)} run의 주문과 연결 체결, 직접 체결, Analyst/Judge 결과입니다.</p></div>'
            f'<span class="badge info">Analyst {len(analyst_symbols)} · Judge {len(final_by_symbol)}</span>'
            f'<span class="badge info" title="judge-review-spec.json 기준">Judge 심사범위: 보유 {judge_held_count} · 비보유 상위선정 {judge_unheld_count}'
            f' · 미선정 {judge_not_selected_count} · 미해결 {judge_scope_unresolved_count}</span></div>'
            f'<div class="run-activity">{"".join(activity_cards)}</div>'
            f'<h3>전체 Analyst 대상 종목</h3><div class="trade-symbol-selector">{"".join(symbol_buttons)}</div>'
            f'<div class="symbol-analysis-content">{"".join(symbol_panels)}</div></section>'
        )

    return f"""
    <section class="panel" id="trade-symbol-analysis">
      <div class="section-head"><div><p class="kicker">TIME &amp; SYMBOL FOCUS</p><h2>시간대별 거래·전체 종목 판단</h2></div><span class="badge info">시간 휠 → 종목 순서로 선택</span></div>
      <p class="section-note">시간 휠을 위아래로 돌리면 해당 run의 주문과 주문번호로 연결된 체결, Analyst 대상 종목 전체를 표시합니다. Judge 미진입 종목도 Analyst 상세 점수를 확인할 수 있습니다.</p>
      <div class="time-wheel"><span class="time-wheel-caption">실행 시간</span><div class="time-selector" role="listbox" aria-label="실행 시간" aria-orientation="vertical">{''.join(time_buttons)}</div></div>
      <div class="time-analysis-content">{''.join(time_panels)}</div>
    </section>
    """


def render_run_timeline(runs: list[dict[str, Any]]) -> str:
    rows = []
    total_tokens = 0
    for run in runs:
        summary = run["summary"]
        tokens = (((summary.get("token_usage") or {}).get("total") or {}).get("total_tokens") or 0)
        total_tokens += int(tokens)
        if run.get("is_preflight_only"):
            # A preflight-only run (safety_block or a skipped/unchanged scheduled check) never
            # reached decision-brief/Analyst/Judge; showing 0/0/0 and regime "-" reads as an empty
            # full review that ran and found nothing, which is not what happened.
            trigger = summary.get("review_trigger") if isinstance(summary.get("review_trigger"), dict) else {}
            decision_raw = str(trigger.get("decision") or "-")
            decision_label = REVIEW_TRIGGER_DECISION_LABELS.get(decision_raw, decision_raw)
            reasons = trigger.get("reasons") if isinstance(trigger.get("reasons"), list) else []
            reason_text = ", ".join(review_trigger_reason_label(r) for r in reasons) or "-"
            detail_parts = [reason_text]
            due_slot = trigger.get("due_slot")
            if due_slot:
                detail_parts.append(f"적용 정기 슬롯 {due_slot}")
            if decision_raw == "skipped":
                persisted = trigger.get("trigger_state_persisted")
                if persisted is True:
                    detail_parts.append("상태 저장 성공")
                elif persisted is False:
                    detail_parts.append("상태 저장 실패(다음 실행에서 재평가)")
                else:
                    detail_parts.append("상태 저장 결과 미기록")
            rows.append(
                f'<tr class="preflight-row"><td>{time_text(summary.get("started_at"))}</td>'
                f'<td><span class="badge info">사전점검</span></td>'
                f'<td colspan="5">{esc(decision_label)}({esc(decision_raw)}) · {esc(" · ".join(detail_parts))}</td>'
                f"<td>{number(tokens)}</td></tr>"
            )
            continue
        account = summary.get("account_display_summary") if isinstance(summary.get("account_display_summary"), dict) else {}
        submitted, blocked, skipped = execution_counts(run["execution"])
        strategy = run["decision"].get("strategy_context") if isinstance(run["decision"].get("strategy_context"), dict) else {}
        indexes = index_map(run["market"])
        kospi = indexes.get("KOSPI", {})
        kosdaq = indexes.get("KOSDAQ", {})
        rows.append(
            f"<tr><td>{time_text(summary.get('started_at'))}</td><td>{status_badge(summary.get('status'))}</td>"
            f"<td>{esc(strategy.get('regime') or '-')}</td>"
            f"<td>{decimal(kospi.get('change_percent'))}% / {decimal(kosdaq.get('change_percent'))}%</td>"
            f"<td>{submitted} / {blocked} / {skipped}</td>"
            f"<td>{number(account.get('total_evaluation_amount'))}원</td>"
            f"<td>{number(account.get('total_pnl_amount'))}원</td><td>{number(tokens)}</td></tr>"
        )
    return f"""
    <section class="panel" id="runs">
      <div class="section-head"><div><p class="kicker">RUN HISTORY</p><h2>당일 실행 타임라인</h2></div><span class="badge info">누적 토큰 {number(total_tokens)}</span></div>
      <div class="table-wrap"><table><thead><tr><th>시각</th><th>상태</th><th>regime</th><th>KOSPI / KOSDAQ</th><th>제출 / 차단 / 스킵</th><th>총평가</th><th>평가손익</th><th>토큰</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
    </section>
    """


def relative_changes(values: list[float], denominator: float) -> list[float]:
    if not values or denominator == 0:
        return [0.0 for _ in values]
    baseline = values[0]
    return [(value - baseline) / abs(denominator) * 100 for value in values]


def normalized_positions(
    values: list[float],
    top: float,
    plot_height: float,
    *,
    low: float | None = None,
    high: float | None = None,
) -> list[float]:
    if low is None:
        low = min(values) if values else 0.0
    if high is None:
        high = max(values) if values else 1.0
    span = high - low
    if span == 0:
        return [top + plot_height / 2 for _ in values]
    return [top + (high - value) / span * plot_height for value in values]


def render_run_points(xs: list[float], ys_by_index: dict[int, float], class_name: str) -> str:
    return "".join(
        f'<circle class="series-point {class_name}" cx="{xs[index]:.2f}" cy="{y:.2f}" r="3.5"/>'
        for index, y in ys_by_index.items()
    )


def render_combined_chart(
    runs: list[dict[str, Any]],
    *,
    period: str = "intraday",
) -> str:
    period_config = {
        "intraday": {
            "kicker": "INTRADAY COMBINED CHART",
            "title": "계좌·시장 통합 추이",
            "scope": "당일 각 run",
        },
        "week": {
            "kicker": "7-DAY COMBINED CHART",
            "title": "최근 1주 계좌·시장 추이",
            "scope": "최근 7일의 거래일별 마지막 유효 run",
        },
        "month": {
            "kicker": "30-DAY COMBINED CHART",
            "title": "최근 1개월 계좌·시장 추이",
            "scope": "최근 30일의 거래일별 마지막 유효 run",
        },
    }.get(period, {})
    if not period_config:
        raise ValueError(f"unsupported chart period: {period}")
    width, height = 1100, 390
    left, right, top, bottom = 58, 28, 34, 70
    plot_width = width - left - right
    plot_height = height - top - bottom
    rows = []
    chart_runs: list[dict[str, Any]] = []
    total_values: list[float] = []
    pnl_values: list[float] = []
    asset_values: list[float | None] = []
    kospi_values: list[float | None] = []
    kospi_change_values: list[float | None] = []
    for run in runs:
        account = run["summary"].get("account_display_summary")
        account = account if isinstance(account, dict) else {}
        asset = run["summary"].get("account_asset_summary")
        asset = asset if isinstance(asset, dict) else {}
        indexes = index_map(run["market"])
        kospi = indexes.get("KOSPI") or {}
        try:
            total = float(account.get("total_evaluation_amount"))
            pnl = float(account.get("total_pnl_amount"))
        except (TypeError, ValueError):
            continue
        try:
            asset_value = float(asset.get("total_asset_amount"))
        except (TypeError, ValueError):
            asset_value = None
        try:
            kospi_value = float(kospi.get("value"))
            kospi_change = float(kospi.get("change_percent"))
        except (TypeError, ValueError):
            kospi_value = None
            kospi_change = None
        chart_runs.append(run)
        total_values.append(total)
        pnl_values.append(pnl)
        asset_values.append(asset_value)
        kospi_values.append(kospi_value)
        kospi_change_values.append(kospi_change)
    if not chart_runs:
        return (
            '<section class="combined-chart-card"><div class="chart-head"><div>'
            f'<p class="kicker">{esc(period_config["kicker"])}</p><h2>{esc(period_config["title"])}</h2>'
            '</div></div><div class="empty-state">총평가·평가손익 시계열을 그릴 수 있는 run이 없습니다.</div></section>'
        )
    asset_indexes = [index for index, value in enumerate(asset_values) if value is not None]
    available_asset_values = [float(asset_values[index]) for index in asset_indexes]
    kospi_indexes = [index for index, value in enumerate(kospi_values) if value is not None]
    available_kospi_values = [float(kospi_values[index]) for index in kospi_indexes]
    total_changes = relative_changes(total_values, total_values[0])
    # PnL can start at zero or be negative, so express its movement as a percentage-point
    # contribution relative to the account's first total evaluation rather than dividing by PnL.
    pnl_changes = relative_changes(pnl_values, total_values[0])
    asset_changes = relative_changes(
        available_asset_values,
        available_asset_values[0] if available_asset_values else 0.0,
    )
    kospi_changes = relative_changes(
        available_kospi_values,
        available_kospi_values[0] if available_kospi_values else 0.0,
    )
    shared_changes = total_changes + pnl_changes + asset_changes + kospi_changes
    shared_low = min(shared_changes)
    shared_high = max(shared_changes)
    total_y = normalized_positions(total_changes, top, plot_height, low=shared_low, high=shared_high)
    pnl_y = normalized_positions(pnl_changes, top, plot_height, low=shared_low, high=shared_high)
    available_asset_y = normalized_positions(
        asset_changes,
        top,
        plot_height,
        low=shared_low,
        high=shared_high,
    )
    asset_y_by_index = dict(zip(asset_indexes, available_asset_y))
    asset_change_by_index = dict(zip(asset_indexes, asset_changes))
    available_kospi_y = normalized_positions(
        kospi_changes,
        top,
        plot_height,
        low=shared_low,
        high=shared_high,
    )
    kospi_y_by_index = dict(zip(kospi_indexes, available_kospi_y))
    kospi_change_by_index = dict(zip(kospi_indexes, kospi_changes))
    pnl_overlaps_total = len(total_y) == len(pnl_y) and all(
        abs(total_point - pnl_point) < 0.5 for total_point, pnl_point in zip(total_y, pnl_y)
    )
    xs = [left + plot_width * index / max(1, len(chart_runs) - 1) for index in range(len(chart_runs))]
    total_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, total_y))
    pnl_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, pnl_y))
    asset_points = " ".join(f"{xs[index]:.2f},{asset_y_by_index[index]:.2f}" for index in asset_indexes)
    kospi_points = " ".join(f"{xs[index]:.2f},{kospi_y_by_index[index]:.2f}" for index in kospi_indexes)
    grid = []
    for step in range(5):
        y = top + plot_height * step / 4
        grid.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_width}" y2="{y:.2f}" class="chart-grid"/>')
    x_labels = []
    for index, (run, x) in enumerate(zip(chart_runs, xs)):
        strategy_context = run["decision"].get("strategy_context")
        strategy_context = strategy_context if isinstance(strategy_context, dict) else {}
        regime = str(strategy_context.get("regime") or "-")
        started_at = str(run["summary"].get("started_at") or "")
        point_label = time_text(started_at) if period == "intraday" else started_at[5:10]
        label_step = max(1, math.ceil(len(chart_runs) / 6))
        if index % label_step == 0 or index == len(chart_runs) - 1:
            x_labels.append(
                f'<text x="{x:.2f}" y="{height - 29}" text-anchor="middle" class="chart-x">{esc(point_label)}</text>'
            )
        rows.append(
            {
                "time": point_label,
                "x": round(x, 2),
                "total": int(total_values[index]),
                "pnl": int(pnl_values[index]),
                "asset": int(asset_values[index]) if asset_values[index] is not None else None,
                "kospi": round(kospi_values[index], 2) if kospi_values[index] is not None else None,
                "kospiChangePercent": round(kospi_change_values[index], 2)
                if kospi_change_values[index] is not None
                else None,
                "regime": regime,
                "regimeLabel": REGIME_LABELS.get(regime, regime),
                "totalChangePercent": round(total_changes[index], 2),
                "pnlImpactPercent": round(pnl_changes[index], 2),
                "assetChangePercent": round(asset_change_by_index[index], 2) if index in asset_change_by_index else None,
                "kospiChangeFromFirstPercent": round(kospi_change_by_index[index], 2)
                if index in kospi_change_by_index
                else None,
                "totalY": round(total_y[index], 2),
                "pnlY": round(pnl_y[index], 2),
                "assetY": round(asset_y_by_index[index], 2) if index in asset_y_by_index else None,
                "kospiY": round(kospi_y_by_index[index], 2) if index in kospi_y_by_index else None,
            }
        )
    points_json = esc(json.dumps(rows, ensure_ascii=False, separators=(",", ":")))
    pnl_line = "" if pnl_overlaps_total else f'<polyline points="{pnl_points}" class="series-line pnl-line"/>'
    pnl_marker = "" if pnl_overlaps_total else '<circle class="chart-marker pnl-marker" r="6"/>'
    asset_line = f'<polyline points="{asset_points}" class="series-line asset-line"/>' if asset_points else ""
    asset_marker = '<circle class="chart-marker asset-marker" r="6"/>' if asset_points else ""
    kospi_line = f'<polyline points="{kospi_points}" class="series-line kospi-line"/>' if kospi_points else ""
    kospi_marker = '<circle class="chart-marker kospi-marker" r="6"/>' if kospi_points else ""
    total_run_points = render_run_points(xs, dict(enumerate(total_y)), "total-point")
    pnl_run_points = "" if pnl_overlaps_total else render_run_points(xs, dict(enumerate(pnl_y)), "pnl-point")
    asset_run_points = render_run_points(xs, asset_y_by_index, "asset-point")
    kospi_run_points = render_run_points(xs, kospi_y_by_index, "kospi-point")
    zero_y = normalized_positions([0.0], top, plot_height, low=shared_low, high=shared_high)[0]
    if kospi_indexes:
        latest_kospi_index = kospi_indexes[-1]
        kospi_legend = (
            f"KOSPI {decimal(kospi_values[latest_kospi_index])} "
            f"({signed_decimal(kospi_change_values[latest_kospi_index])}%)"
        )
        kospi_range = (
            f"<span>KOSPI {decimal(min(available_kospi_values))}~{decimal(max(available_kospi_values))}</span>"
        )
    else:
        kospi_legend = "KOSPI 조회 실패"
        kospi_range = "<span>KOSPI 조회 실패</span>"
    if asset_indexes:
        latest_asset_index = asset_indexes[-1]
        asset_legend = f"KIS 총자산 {number(asset_values[latest_asset_index])}원"
        asset_range = (
            f"<span>KIS 총자산 {number(min(available_asset_values))}~{number(max(available_asset_values))}원</span>"
        )
    else:
        asset_legend = "KIS 총자산 조회 실패"
        asset_range = "<span>KIS 총자산 조회 실패</span>"
    return f"""
    <section class="combined-chart-card" data-chart-points="{points_json}" data-chart-period="{esc(period)}">
      <div class="chart-head"><div><p class="kicker">{esc(period_config['kicker'])}</p><h2>{esc(period_config['title'])}</h2><p>{esc(period_config['scope'])}의 관측값을 사용합니다. 각 series의 첫 관측값을 0%로 둔 상대 변화율을 하나의 공통 축에 표시합니다. 따라서 시작점은 같지만 끝점은 실제 상대 변화가 같을 때만 만납니다. 평가손익은 첫 총평가액 대비 손익 변화 기여도이며, 총평가와 상대변화 궤적이 같으면 총평가 선만 표시하되 hover에는 두 값을 모두 제공합니다. 계좌선은 입출금을 보정한 수익률이 아니며 확인되지 않은 원금이나 계좌수익률은 추정하지 않습니다.</p></div></div>
      <div class="chart-legend"><span><i style="--legend:#4f6df5"></i>총평가</span><span><i style="--legend:#e14c68"></i>평가손익</span><span><i style="--legend:#8b5cf6"></i>{asset_legend}</span><span><i style="--legend:#0b9a86"></i>{kospi_legend}</span></div>
      <div class="interactive-chart">
        <svg class="interactive-line-chart" viewBox="0 0 {width} {height}" role="img" aria-label="총평가 평가손익 KIS 총자산 KOSPI 통합 시계열">
          {''.join(grid)}
          <line x1="{left}" y1="{zero_y:.2f}" x2="{left + plot_width}" y2="{zero_y:.2f}" class="chart-zero"/>
          <polyline points="{total_points}" class="series-line total-line"/>
          {pnl_line}
          {asset_line}
          {kospi_line}
          {total_run_points}
          {pnl_run_points}
          {asset_run_points}
          {kospi_run_points}
          <line class="chart-cursor" x1="{left}" x2="{left}" y1="{top}" y2="{top + plot_height}"/>
          <circle class="chart-marker total-marker" r="6"/>{pnl_marker}{asset_marker}{kospi_marker}
          {''.join(x_labels)}
          <rect class="chart-hit-area" x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" data-left="{left}" data-width="{plot_width}"/>
        </svg>
        <div class="chart-tooltip" aria-live="polite"></div>
      </div>
      <div class="chart-scrubber">
        <div class="chart-scrubber-label"><span>조회 시점</span><output class="chart-scrubber-time">{esc(rows[-1]['time'])}</output></div>
        <input class="chart-range-slider" type="range" min="0" max="{len(rows) - 1}" step="1" value="{len(rows) - 1}" aria-label="차트 조회 시점">
        <div class="chart-scrubber-ends"><span>{esc(rows[0]['time'])}</span><span>{esc(rows[-1]['time'])}</span></div>
      </div>
      <div class="series-ranges"><span>공통축 {signed_decimal(shared_low)}~{signed_decimal(shared_high)}%</span><span>총평가 {number(min(total_values))}~{number(max(total_values))}원</span><span>평가손익 {number(min(pnl_values))}~{number(max(pnl_values))}원</span>{asset_range}{kospi_range}</div>
    </section>
    """


def render_chart_periods(
    runs_root: Path,
    target_started_at: str,
    intraday_runs: list[dict[str, Any]],
) -> str:
    history_runs = find_daily_history(runs_root, target_started_at, calendar_days=30)
    try:
        target_date = date.fromisoformat(target_started_at[:10])
    except ValueError:
        target_date = None
    week_start = target_date - timedelta(days=6) if target_date is not None else None
    week_runs = [
        run
        for run in history_runs
        if week_start is not None
        and date.fromisoformat(str(run["summary"].get("started_at") or "")[:10]) >= week_start
    ]
    period_specs = [
        ("intraday", "당일", intraday_runs),
        ("week", "1주", week_runs),
        ("month", "1개월", history_runs),
    ]
    buttons = []
    panels = []
    for index, (period, label, period_runs) in enumerate(period_specs):
        is_active = index == 0
        buttons.append(
            f'<button type="button" class="chart-period-button{" active" if is_active else ""}" '
            f'data-chart-period-target="{esc(period)}" role="tab" aria-selected="{str(is_active).lower()}">'
            f"{esc(label)}</button>"
        )
        panels.append(
            f'<div class="chart-period-panel{" active" if is_active else ""}" '
            f'data-chart-period-panel="{esc(period)}" role="tabpanel">'
            f"{render_combined_chart(period_runs, period=period)}</div>"
        )
    return (
        '<section class="chart-period-switcher">'
        '<div class="chart-period-tabs" role="tablist" aria-label="계좌·시장 그래프 기간">'
        f'{"".join(buttons)}</div>{"".join(panels)}</section>'
    )


def pie_slice_path(cx: float, cy: float, radius: float, start_angle: float, end_angle: float) -> str:
    start_radians = math.radians(start_angle - 90)
    end_radians = math.radians(end_angle - 90)
    start_x = cx + radius * math.cos(start_radians)
    start_y = cy + radius * math.sin(start_radians)
    end_x = cx + radius * math.cos(end_radians)
    end_y = cy + radius * math.sin(end_radians)
    large_arc = 1 if end_angle - start_angle > 180 else 0
    return (
        f"M {cx:.2f} {cy:.2f} L {start_x:.2f} {start_y:.2f} "
        f"A {radius:.2f} {radius:.2f} 0 {large_arc} 1 {end_x:.2f} {end_y:.2f} Z"
    )


def financial_industry_by_symbol(decision: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    symbols = decision.get("symbols") if isinstance(decision.get("symbols"), list) else []
    for item in symbols:
        if not isinstance(item, dict):
            continue
        financial = item.get("financial_summary") if isinstance(item.get("financial_summary"), dict) else {}
        details = financial.get("items") if isinstance(financial.get("items"), list) else []
        industry = next(
            (
                str(detail).removeprefix("업종 ").strip()
                for detail in details
                if str(detail).startswith("업종 ") and str(detail).removeprefix("업종 ").strip()
            ),
            "업종 미확인",
        )
        result[str(item.get("symbol_id") or "")] = industry
    return result


def render_holdings(target_dir: Path, summary: dict[str, Any]) -> str:
    account = load_json(target_dir / "account-before-order.json")
    decision = load_json(target_dir / "decision-brief.json")
    industry_by_symbol = financial_industry_by_symbol(decision)
    symbols = account.get("symbols") if isinstance(account.get("symbols"), list) else []
    positions = [item for item in symbols if isinstance(item, dict) and int(item.get("current_live_holding_quantity") or 0) > 0]
    positions.sort(key=lambda item: int(item.get("valuation_amount") or 0), reverse=True)
    pie_total = sum(int(item.get("valuation_amount") or 0) for item in positions)
    palette = ("#5367E8", "#0B9A86", "#E66786", "#E69B36", "#7B5ED7", "#3583D8", "#A46A4E", "#6B7A90", "#B864B9", "#D05B50")
    industries = sorted({industry_by_symbol.get(str(item.get("symbol_id") or ""), "업종 미확인") for item in positions})
    industry_colors = {industry: palette[index % len(palette)] for index, industry in enumerate(industries)}
    industry_totals = {industry: 0 for industry in industries}
    for item in positions:
        symbol_id = str(item.get("symbol_id") or "")
        industry_totals[industry_by_symbol.get(symbol_id, "업종 미확인")] += int(item.get("valuation_amount") or 0)
    industry_order = sorted(industries, key=lambda industry: (-industry_totals[industry], industry))
    industry_rank = {industry: index for index, industry in enumerate(industry_order)}
    pie_positions = sorted(
        positions,
        key=lambda item: (
            industry_rank[industry_by_symbol.get(str(item.get("symbol_id") or ""), "업종 미확인")],
            -int(item.get("valuation_amount") or 0),
        ),
    )

    rows = []
    slices = []
    start_angle = 0.0
    for item in pie_positions:
        symbol_id = str(item.get("symbol_id") or "")
        symbol_name = str(item.get("symbol_name") or symbol_id)
        valuation = int(item.get("valuation_amount") or 0)
        pie_weight = (valuation / pie_total * 100) if pie_total else 0
        industry = industry_by_symbol.get(symbol_id, "업종 미확인")
        end_angle = start_angle + (valuation / pie_total * 360 if pie_total else 0)
        path = pie_slice_path(180, 180, 142, start_angle, end_angle)
        color = industry_colors[industry]
        slices.append(
            f'<path class="pie-slice" d="{path}" fill="{color}" tabindex="0" '
            f'data-symbol-name="{esc(symbol_name)}" data-symbol-id="{esc(symbol_id)}" data-industry="{esc(industry)}" '
            f'data-valuation="{valuation}" data-weight="{pie_weight:.4f}" data-quantity="{number(item.get("current_live_holding_quantity"))}" '
            f'data-pnl="{number(item.get("pnl_amount"))}" data-pnl-rate="{decimal(item.get("pnl_rate"))}" '
            f'aria-label="{esc(symbol_name)} {esc(industry)} 평가액 {number(valuation)}원 비중 {decimal(pie_weight)}%">'
            f'<title>{esc(symbol_name)} · {esc(industry)} · {number(valuation)}원 · {decimal(pie_weight)}%</title></path>'
        )
        start_angle = end_angle
    for item in positions:
        symbol_id = str(item.get("symbol_id") or "")
        symbol_name = str(item.get("symbol_name") or symbol_id)
        valuation = int(item.get("valuation_amount") or 0)
        weight = (valuation / pie_total * 100) if pie_total else 0
        industry = industry_by_symbol.get(symbol_id, "업종 미확인")
        pnl_css = "positive" if float(item.get("pnl_amount") or 0) >= 0 else "negative"
        rows.append(
            f"<tr><td><strong>{esc(symbol_name)}</strong><br><code>{esc(symbol_id)}</code></td><td>{esc(industry)}</td>"
            f"<td>{number(item.get('current_live_holding_quantity'))}주</td><td>{number(item.get('current_price'))}원</td><td>{number(valuation)}원</td>"
            f"<td>{decimal(weight)}%</td><td class=\"{pnl_css}\">{number(item.get('pnl_amount'))}원</td>"
            f"<td class=\"{pnl_css}\">{decimal(item.get('pnl_rate'))}%</td></tr>"
        )
    legend = []
    for industry in industry_order:
        total = industry_totals[industry]
        weight = (total / pie_total * 100) if pie_total else 0
        legend.append(
            f'<article class="sector-legend-item"><i style="--sector-color:{industry_colors[industry]}"></i><div>'
            f'<strong>{esc(industry)}</strong><small>{number(total)}원 · {decimal(weight)}%</small></div></article>'
        )
    cut_off = time_text(summary.get("started_at"))
    return f"""
    <section class="panel" id="holdings">
      <div class="section-head"><div><p class="kicker">PORTFOLIO</p><h2>{esc(cut_off)} 주문 제출 직전 보유 현황</h2></div><span class="badge info">보유 {len(positions)}종목</span></div>
      <div class="notice">수량·현재가·평가액은 해당 run의 주문 전 계좌 조회 기준입니다. 제출 주문은 체결이 확인되기 전까지 보유수량 변화로 반영하지 않습니다.</div>
      <div class="portfolio-chart-layout">
        <div class="portfolio-pie" data-pie-total="{pie_total}">
          <svg class="portfolio-pie-svg" viewBox="0 0 360 360" role="img" aria-label="보유 종목 평가액 비중 파이차트">{''.join(slices)}</svg>
          <div class="pie-tooltip" aria-live="polite"></div>
        </div>
        <div class="sector-legend"><div><p class="kicker">SECTOR COLORS</p><h3>업종별 색상</h3><p>같은 업종 종목은 같은 색상으로 표시합니다.</p></div>{''.join(legend)}</div>
      </div>
      <p class="source-note">업종은 {esc(cut_off)} decision-brief의 종목별 financial_summary를 사용했습니다. 파이는 현금 제외 주식 평가액 {number(pie_total)}원을 기준으로 합니다.</p>
      <div class="table-wrap holdings-table"><table><thead><tr><th>종목</th><th>업종</th><th>수량</th><th>현재가</th><th>평가액</th><th>주식 내 비중</th><th>평가손익</th><th>수익률</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
    </section>
    """


def render_policy_panel(summary: dict[str, Any]) -> str:
    """Effective execution-guard values in force for this run (unheld top-K, profit-protection
    and concentration reduction bounds, daily turnover cap) -- projected from
    pipeline-summary.json's execution_guards_policy so they are auditable, not just implicit in
    code/config."""
    policy = summary.get("execution_guards_policy") if isinstance(summary.get("execution_guards_policy"), dict) else {}
    if not policy:
        return ""
    return f"""
    <section class="panel" id="policy">
      <div class="section-head"><div><p class="kicker">EXECUTION GUARDS</p><h2>적용 중인 실행 정책</h2></div></div>
      <div class="table-wrap"><table><thead><tr><th>정책</th><th>값</th></tr></thead><tbody>
        <tr><td>비보유 상위선정 top-K</td><td>{number(policy.get('unheld_review_top_k'))}</td></tr>
        <tr><td>이익보호 최대 축소율</td><td>{decimal(policy.get('profit_protection_max_reduction_pct'), 1)}%</td></tr>
        <tr><td>집중도 상한</td><td>{decimal(policy.get('concentration_rebalance_cap_pct'), 1)}%</td></tr>
        <tr><td>집중도 조정 최대 축소율</td><td>{decimal(policy.get('concentration_rebalance_max_reduction_pct'), 1)}%</td></tr>
        <tr><td>일일 회전한도</td><td>{decimal(policy.get('max_daily_turnover_pct'), 1)}%</td></tr>
      </tbody></table></div>
    </section>
    """


def render_review_trigger_panel(summary: dict[str, Any]) -> str:
    """Final review-trigger decision/persistence state for the target run, rendered after
    finalize_review_gate_state so full_review_completed and trigger_state_persisted reflect what
    actually happened, not the pre-finalize snapshot."""
    trigger = summary.get("review_trigger") if isinstance(summary.get("review_trigger"), dict) else {}
    if not trigger:
        return ""
    decision_raw = str(trigger.get("decision") or "-")
    decision_label = REVIEW_TRIGGER_DECISION_LABELS.get(decision_raw, decision_raw)
    reasons = trigger.get("reasons") if isinstance(trigger.get("reasons"), list) else []
    reason_text = ", ".join(review_trigger_reason_label(r) for r in reasons) or "-"
    changed = trigger.get("changed_components") if isinstance(trigger.get("changed_components"), list) else []
    safety_reasons = trigger.get("safety_reasons") if isinstance(trigger.get("safety_reasons"), list) else []
    persisted = trigger.get("trigger_state_persisted")
    if decision_raw == "safety_block":
        persist_badge = '<span class="badge info">저장 대상 아님</span>'
    elif persisted is True:
        persist_badge = '<span class="badge ok">저장 성공</span>'
    elif persisted is False:
        persist_badge = '<span class="badge warn">저장 실패</span>'
    else:
        persist_badge = '<span class="badge warn">저장 결과 미기록</span>'
    return f"""
    <section class="panel" id="review-trigger">
      <div class="section-head"><div><p class="kicker">REVIEW TRIGGER</p><h2>리뷰 트리거 최종 상태</h2></div></div>
      <div class="table-wrap"><table><thead><tr><th>항목</th><th>값</th></tr></thead><tbody>
        <tr><td>결정</td><td>{esc(decision_label)}({esc(decision_raw)})</td></tr>
        <tr><td>사유</td><td>{esc(reason_text)}</td></tr>
        <tr><td>적용 정기 슬롯</td><td>{esc(str(trigger.get('due_slot') or '-'))}</td></tr>
        <tr><td>변경 감지 항목</td><td>{esc(', '.join(str(c) for c in changed) or '-')}</td></tr>
        <tr><td>안전 문제</td><td>{esc(', '.join(review_trigger_reason_label(r) for r in safety_reasons) or '-')}</td></tr>
        <tr><td>전체 리뷰 완료</td><td>{'예' if trigger.get('full_review_completed') else '아니오'}</td></tr>
        <tr><td>트리거 상태 저장</td><td>{persist_badge}</td></tr>
      </tbody></table></div>
    </section>
    """


def render_market_and_quality(target_dir: Path, summary: dict[str, Any]) -> str:
    decision = load_json(target_dir / "decision-brief.json")
    market = load_json(target_dir / "market-index-snapshot.json")
    strategy = decision.get("strategy_context") if isinstance(decision.get("strategy_context"), dict) else {}
    index_rows = []
    for item in market.get("indexes", []):
        if not isinstance(item, dict):
            continue
        badge = status_badge(item.get("status"))
        index_rows.append(
            f"<tr><td><strong>{esc(item.get('name'))}</strong><br><code>{esc(item.get('symbol'))}</code></td>"
            f"<td>{number(item.get('value'))}</td><td class=\"{'negative' if float(item.get('change_percent') or 0) < 0 else 'positive'}\">{decimal(item.get('change_percent'))}%</td>"
            f"<td>{esc(item.get('source'))}</td><td>{badge}</td></tr>"
        )
    evidence = summary.get("evidence_summary") if isinstance(summary.get("evidence_summary"), dict) else {}
    reporting_view = summary.get("reporting_view") if isinstance(summary.get("reporting_view"), dict) else {}
    reporting_domains = reporting_view.get("evidence_domains") if isinstance(reporting_view.get("evidence_domains"), dict) else {}

    def domain_view(key: str, default_wanted: Any) -> dict[str, Any]:
        view = reporting_domains.get(key) if isinstance(reporting_domains, dict) else None
        if isinstance(view, dict):
            return {
                "status": view.get("status"),
                "display_text": view.get("coverage_text"),
                "usable_symbol_count": view.get("usable_symbol_count"),
                "wanted_symbol_count": view.get("wanted_symbol_count"),
                "usable_item_count": view.get("usable_item_count"),
            }
        raw = evidence.get(key) if isinstance(evidence.get(key), dict) else {}
        counts = raw.get("cache_counts") if isinstance(raw.get("cache_counts"), dict) else {}
        return {
            "status": raw.get("status"),
            "display_text": raw.get("display_text"),
            "usable_symbol_count": counts.get("usable_symbol_count", raw.get("usable_symbol_count")),
            "wanted_symbol_count": counts.get("wanted_symbol_count", default_wanted),
            "usable_item_count": raw.get("article_count"),
        }

    symbol_count = evidence.get("symbol_count")
    financial = domain_view("financial", symbol_count)
    symbol_news = domain_view("symbol_news", symbol_count)
    market_news = domain_view("market_news", None)
    investor_flow = domain_view("investor_flow", symbol_count)
    investor_flow_usable = investor_flow.get("usable_symbol_count")
    investor_flow_wanted = investor_flow.get("wanted_symbol_count")
    lifecycle = summary.get("order_lifecycle") if isinstance(summary.get("order_lifecycle"), dict) else {}
    reporting_view = summary.get("reporting_view") if isinstance(summary.get("reporting_view"), dict) else {}
    reporting_orders = reporting_view.get("orders") if isinstance(reporting_view.get("orders"), dict) else {}
    active_order_view = reporting_orders.get("active") if isinstance(reporting_orders.get("active"), dict) else {}
    partial_stages = [item for item in summary.get("stages", []) if isinstance(item, dict) and item.get("status") != "success"]
    stage_rows = "".join(
        f"<tr><td>{esc(item.get('stage'))}</td><td>{status_badge(item.get('status'))}</td><td>{esc(item.get('detail'))}</td></tr>"
        for item in partial_stages
    )
    warnings = []
    normal_evidence_statuses = {None, "", "success", "complete", "supplied"}
    if symbol_news.get("status") not in normal_evidence_statuses:
        warnings.append(
            f'<div class="warning"><strong>종목뉴스 수집 {esc(symbol_news.get("status"))}</strong><p>{esc(symbol_news.get("display_text") or "일부 종목뉴스 근거를 사용할 수 없습니다.")}</p></div>'
        )
    if market_news.get("status") not in normal_evidence_statuses:
        warnings.append(
            f'<div class="warning"><strong>시장뉴스 수집 {esc(market_news.get("status"))}</strong><p>{esc(market_news.get("display_text") or "일부 국내·해외 시장뉴스 근거를 사용할 수 없습니다.")}</p></div>'
        )
    if financial.get("status") not in normal_evidence_statuses:
        warnings.append(
            f'<div class="warning"><strong>재무 수집 {esc(financial.get("status"))}</strong><p>{esc(financial.get("display_text") or "일부 재무 근거를 사용할 수 없습니다.")}</p></div>'
        )
    if investor_flow.get("status") not in normal_evidence_statuses:
        warnings.append(
            f'<div class="warning"><strong>장중 수급 수집 {esc(investor_flow.get("status"))}</strong><p>{esc(investor_flow.get("display_text") or "일부 종목의 장중 수급 추정치를 사용할 수 없습니다.")}</p></div>'
        )
    if lifecycle.get("status") not in {None, "", "not_run"}:
        issue_count = number(lifecycle.get("holding_state_issue_count"))
        warning_class = "warning bad-border" if int(lifecycle.get("holding_state_issue_count") or 0) > 0 else "warning"
        # Prefer the lifecycle-confirmed normalized count; a present-but-unconfirmed view must
        # render 미조회, never a guessed count, even when raw lifecycle data conflicts.
        if active_order_view:
            active_count_text = (
                f"{number(active_order_view.get('count'))}건" if active_order_view.get("lookup_status") == "complete" else "미조회"
            )
        else:
            active_count_text = f"{number(lifecycle.get('active_order_count'))}건"
        warnings.append(
            f'<div class="{warning_class}"><strong>주문 생명주기 사전조회 {esc(lifecycle.get("status"))}</strong>'
            f'<p>현재 미체결 {active_count_text} · 같은 날 이전 제출 '
            f'{number(lifecycle.get("previous_submitted_cash_order_count"))}건 · 보유수량 확인 필요 {issue_count}건</p></div>'
        )
    if partial_stages:
        warnings.append(
            '<div class="warning"><strong>부분·실패 stage 존재</strong><p>아래 stage 표의 상태와 설명을 확인하세요. 주문 상태와 수집 상태는 별도로 해석합니다.</p></div>'
        )
    warning_html = "".join(warnings) or '<div class="empty-state">추가 데이터 품질 경고 없음</div>'
    return f"""
    <section class="panel" id="quality">
      <div class="section-head"><div><p class="kicker">MARKET &amp; DATA</p><h2>시장·데이터 품질</h2></div><span class="badge warn">regime {esc(strategy.get('regime'))}</span></div>
      <p>{esc(strategy.get('advisory_reason'))}</p>
      <div class="table-wrap"><table><thead><tr><th>지수</th><th>값</th><th>등락률</th><th>출처</th><th>판정</th></tr></thead><tbody>{''.join(index_rows)}</tbody></table></div>
      <div class="coverage">
        <article><span>재무 coverage</span><strong>{number(financial.get('usable_symbol_count'))} / {number(financial.get('wanted_symbol_count'))}</strong><small>{esc(financial.get('status'))}</small></article>
        <article><span>종목뉴스 coverage</span><strong>{number(symbol_news.get('usable_symbol_count'))} / {number(symbol_news.get('wanted_symbol_count'))}</strong><small>{esc(symbol_news.get('status'))}</small></article>
        <article><span>시장뉴스 기사</span><strong>{number(market_news.get('usable_item_count'))}건</strong><small>{esc(market_news.get('status'))}</small></article>
        <article><span>수급 coverage</span><strong>{number(investor_flow_usable)} / {number(investor_flow_wanted)}</strong><small>{esc(investor_flow.get('status'))}</small></article>
        <article><span>가격 전용 종목</span><strong>{number(evidence.get('price_only_symbol_count'))}</strong><small>전체 {number(evidence.get('symbol_count'))}종목</small></article>
      </div>
      <h3>완전 성공이 아닌 stage</h3>
      <div class="table-wrap"><table><thead><tr><th>stage</th><th>상태</th><th>설명</th></tr></thead><tbody>{stage_rows or '<tr><td colspan="3">없음</td></tr>'}</tbody></table></div>
      <div class="warning-list">
        {warning_html}
      </div>
    </section>
    """


def render_financial_details(target_dir: Path) -> str:
    decision = load_json(target_dir / "decision-brief.json")
    symbols = decision.get("symbols") if isinstance(decision.get("symbols"), list) else []
    cards = []
    for index, item in enumerate(symbols, start=1):
        if not isinstance(item, dict):
            continue
        financial = item.get("financial_summary") if isinstance(item.get("financial_summary"), dict) else {}
        financial_items = financial.get("items") if isinstance(financial.get("items"), list) else []
        financial_html = "".join(f"<li>{esc(value)}</li>" for value in financial_items) or "<li>사용 가능한 재무 상세 없음</li>"
        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        cards.append(
            f"<article class=\"financial-card\"><div class=\"evidence-title\"><span class=\"index\">{index}</span><div>"
            f"<h3>{esc(item.get('symbol_name'))} <code>{esc(item.get('symbol_id'))}</code></h3>"
            f"<p>현재/최근가 {number(price.get('current_or_last'))}원 · evidence {esc(item.get('evidence_mode'))}</p></div></div>"
            f"<div class=\"financial-body\"><h4>재무·가치 정보</h4><ul>{financial_html}</ul>"
            f"<p class=\"source-note\">cache 상태: {esc(financial.get('cache_status') or '-')} · quality/value 사용 가능: {esc(financial.get('quality_value_usable'))}</p></div></article>"
        )
    cut_off = time_text(load_json(target_dir / "pipeline-summary.json").get("started_at"))
    return f"""
    <section class="panel" id="financial-details">
      <div class="section-head"><div><p class="kicker">FINANCIAL EVIDENCE</p><h2>전체 {len(cards)}종목 재무 상세</h2></div><span class="badge info">{esc(cut_off)} 기준</span></div>
      <p class="section-note">{esc(cut_off)} Analyst 입력에 포함된 compact 재무·가치 항목을 종목별로 표시합니다.</p>
      <div class="financial-list">{''.join(cards)}</div>
    </section>
    """


def render_news_timeline(runs: list[dict[str, Any]]) -> str:
    seen: set[tuple[str, str, str, str]] = set()
    blocks = []
    for run in runs:
        summary = run["summary"]
        decision = run["decision"]
        current: set[tuple[str, str, str, str]] = set()
        for symbol in decision.get("symbols", []):
            if not isinstance(symbol, dict):
                continue
            for news in symbol.get("symbol_news_summary", []):
                if not isinstance(news, dict):
                    continue
                current.add(
                    (
                        str(symbol.get("symbol_id") or ""),
                        str(symbol.get("symbol_name") or ""),
                        str(news.get("article_date") or ""),
                        str(news.get("content") or ""),
                    )
                )
        new_items = sorted(current - seen, key=lambda item: (item[2], item[0], item[3]))
        seen.update(current)
        symbol_news_summary = ((summary.get("evidence_summary") or {}).get("symbol_news") or {})
        counts = symbol_news_summary.get("cache_counts") if isinstance(symbol_news_summary.get("cache_counts"), dict) else {}
        articles = []
        for symbol_id, symbol_name, article_date, content in new_items:
            articles.append(
                f"<article class=\"news-item\"><div><strong>{esc(symbol_name)}</strong> <code>{esc(symbol_id)}</code>"
                f"<time>{esc(article_date)}</time></div><p>{esc(content)}</p></article>"
            )
        if not articles:
            articles.append('<div class="empty-state">직전 run 이후 새로 관측된 기사 없음</div>')
        blocks.append(
            f"<section class=\"news-run\"><div class=\"news-run-head\"><div><span class=\"news-time\">{esc(time_text(summary.get('started_at')))}</span>"
            f"<strong>coverage {number(counts.get('usable_symbol_count'))}/{number(counts.get('wanted_symbol_count'))}</strong></div>"
            f"<div><span>해당 run 기사 {len(current)}건</span><span class=\"badge {'ok' if new_items else 'info'}\">신규 관측 {len(new_items)}건</span></div></div>"
            f"<div class=\"news-run-body\">{''.join(articles)}</div></section>"
        )
    return f"""
    <section class="panel" id="news-timeline">
      <div class="section-head"><div><p class="kicker">SYMBOL NEWS TIMELINE</p><h2>시간별 종목뉴스 수집 이력</h2></div><span class="badge info">고유 신규 관측 {len(seen)}건</span></div>
      <p class="section-note">각 run에서 KIS 종목번호로 수집한 뉴스만 표시합니다. 종목 coverage, 기사 수와 이전 run들에서 관측되지 않았던 신규 기사를 구분합니다.</p>
      <div class="news-timeline">{''.join(blocks)}</div>
    </section>
    """


def render_market_news_timeline(runs: list[dict[str, Any]]) -> str:
    seen: set[str] = set()
    blocks = []
    for run in runs:
        summary = run["summary"]
        decision = run["decision"]
        context = decision.get("market_news_context") if isinstance(decision.get("market_news_context"), dict) else {}
        items = context.get("items") if isinstance(context.get("items"), list) else []
        current: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            title = " ".join(str(item.get("title") or "").split())
            if not title:
                continue
            url = str(item.get("url") or "").strip()
            key = f"url:{url}" if url else f"title:{title.casefold()}"
            current[key] = item
        new_keys = [key for key in current if key not in seen]
        seen.update(current)
        articles = []
        for key in sorted(
            new_keys,
            key=lambda item_key: str(current[item_key].get("published_at") or current[item_key].get("collected_at") or ""),
            reverse=True,
        ):
            item = current[key]
            title = str(item.get("title") or "")
            url = str(item.get("url") or "")
            title_html = (
                f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(title)}</a>'
                if url.startswith(("http://", "https://"))
                else esc(title)
            )
            classifications = item.get("classifications") if isinstance(item.get("classifications"), list) else []
            meta = " · ".join(
                value
                for value in (
                    str(item.get("domain") or ""),
                    str(item.get("source_country") or ""),
                    ", ".join(str(value) for value in classifications),
                )
                if value
            )
            articles.append(
                f'<article class="news-item"><div><strong>{title_html}</strong>'
                f'<time>{esc(item.get("published_at") or item.get("collected_at"))}</time></div>'
                f'<p>{esc(meta)}</p></article>'
            )
        if not articles:
            articles.append('<div class="empty-state">직전 run 이후 새로 선택된 시장뉴스 없음</div>')
        blocks.append(
            f'<section class="news-run"><div class="news-run-head"><div><span class="news-time">{esc(time_text(summary.get("started_at")))}</span>'
            f'<strong>{esc(context.get("status") or "missing")}</strong></div>'
            f'<div><span>구간 {esc(full_time_text(context.get("window_start")))} ~ {esc(full_time_text(context.get("window_end")))}</span>'
            f'<span class="badge {"ok" if new_keys else "info"}">신규 선택 {len(new_keys)}건</span></div></div>'
            f'<div class="news-run-body">{"".join(articles)}</div></section>'
        )
    return f"""
    <section class="panel" id="market-news-timeline">
      <div class="section-head"><div><p class="kicker">MARKET NEWS TIMELINE</p><h2>국내·해외 시장뉴스 구간 이력</h2></div><span class="badge info">고유 선택 {len(seen)}건</span></div>
      <p class="section-note">저장 DB에서 직전 거래 run 이후 현재 run까지 선택된 시장·거시·지정학 뉴스를 표시합니다. 이 뉴스는 전체 재검토 신호이며 개별 종목 자동 주문 신호가 아닙니다.</p>
      <div class="news-timeline">{''.join(blocks)}</div>
    </section>
    """


def list_items(values: Any, empty: str = "없음") -> str:
    if not isinstance(values, list) or not values:
        return f"<li>{esc(empty)}</li>"
    return "".join(f"<li>{esc(value)}</li>" for value in values)


CITED_ARGUMENT_ID_PATTERN = re.compile(
    r"(?<![0-9A-Za-z_-])\d{6}-(?:bull|bear)-(?:opening|rebuttal-\d+)-\d+(?:/\d+)*(?![0-9A-Za-z_/-])"
)
ARGUMENT_ID_PATTERN = re.compile(r"\d{6}-(?:bull|bear)-(?:opening|rebuttal-\d+)-\d+")


def parse_cited_argument_ids(text: Any) -> list[str]:
    """Extract decisive argument IDs from a Judge one_line_reason, expanding slash shorthand.

    `010950-bear-rebuttal-1-1/3` cites both `...-1-1` and `...-1-3`; the shorthand only ever
    replaces the trailing argument number, never the phase/side/symbol prefix.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for match in CITED_ARGUMENT_ID_PATTERN.finditer(str(text or "")):
        base, _, last_segment = match.group(0).rpartition("-")
        for number_text in last_segment.split("/"):
            candidate = f"{base}-{number_text}"
            if candidate not in seen:
                seen.add(candidate)
                ids.append(candidate)
    return ids


def build_debate_argument_index(debate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    phases = debate.get("phases") if isinstance(debate.get("phases"), list) else []
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        sides = phase.get("sides") if isinstance(phase.get("sides"), dict) else {}
        for side in ("bull", "bear"):
            payload = ((sides.get(side) or {}).get("output") or {})
            symbols = payload.get("symbols") if isinstance(payload.get("symbols"), list) else []
            for symbol_item in symbols:
                if not isinstance(symbol_item, dict):
                    continue
                arguments = symbol_item.get("arguments") if isinstance(symbol_item.get("arguments"), list) else []
                for argument in arguments:
                    if not isinstance(argument, dict):
                        continue
                    argument_id = str(argument.get("argument_id") or "")
                    if argument_id:
                        index[argument_id] = {
                            "statement": argument.get("statement"),
                            "side": side,
                            "symbol_id": str(symbol_item.get("symbol_id") or ""),
                        }
    return index


def argument_anchor_id(run_index: int, argument_id: str) -> str | None:
    if not ARGUMENT_ID_PATTERN.fullmatch(argument_id):
        return None
    return f"arg-{run_index}-{argument_id}"


def render_decision_evidence(
    reason_text: Any, argument_index: dict[str, dict[str, Any]], run_index: int, symbol_id: str
) -> str:
    items = []
    for argument_id in parse_cited_argument_ids(reason_text):
        info = argument_index.get(argument_id)
        if info is None or info.get("symbol_id") != symbol_id:
            continue
        anchor = argument_anchor_id(run_index, argument_id)
        if anchor is None:
            continue
        side_label = SIDE_LABELS.get(str(info.get("side")), str(info.get("side")))
        items.append(
            f'<li class="decision-evidence-item {esc(info.get("side"))}">'
            f'<a href="#{esc(anchor)}"><code>{esc(argument_id)}</code></a> '
            f'<span class="badge info">{esc(side_label)}</span>'
            f'<p>{esc(info.get("statement"))}</p></li>'
        )
    if not items:
        return ""
    return f'<div class="decision-evidence"><h4>판단 근거 인용</h4><ul>{"".join(items)}</ul></div>'


def render_debate_symbol(item: dict[str, Any], side: str, run_index: int) -> str:
    arguments = item.get("arguments") if isinstance(item.get("arguments"), list) else []
    argument_rows = []
    for argument in arguments:
        if not isinstance(argument, dict):
            continue
        refs = argument.get("evidence_refs") if isinstance(argument.get("evidence_refs"), list) else []
        targets = argument.get("targets") if isinstance(argument.get("targets"), list) else []
        argument_id = str(argument.get("argument_id") or "")
        anchor_id = argument_anchor_id(run_index, argument_id)
        anchor = f' id="{esc(anchor_id)}"' if anchor_id else ""
        argument_rows.append(
            f"<article class=\"argument\"{anchor}><div><code>{esc(argument.get('argument_id'))}</code> <span class=\"badge info\">{esc(argument.get('kind'))}</span></div>"
            f"<p>{esc(argument.get('statement'))}</p><small>근거: {esc(' · '.join(str(value) for value in refs) or '-')}</small>"
            f"<small>대상: {esc(' · '.join(str(value) for value in targets) or '-')}</small></article>"
        )
    recommended_action = str(item.get("recommended_action") or "미제공")
    target_quantity = (
        f"{number(item.get('target_holding_quantity'))}주"
        if item.get("target_holding_quantity") is not None
        else "미제공"
    )
    return f"""
    <article class="debate-symbol {esc(side)}">
      <div class="card-title"><div><h4>{esc(item.get('symbol_name'))} <code>{esc(item.get('symbol_id'))}</code></h4><p>{esc(SIDE_LABELS.get(side, side))} 코멘트 전체</p></div></div>
      <div class="arguments">{''.join(argument_rows)}</div>
      <div class="debate-meta"><div><strong>양보한 근거</strong><ul>{list_items(item.get('concessions'))}</ul></div><div><strong>미해결 충돌</strong><ul>{list_items(item.get('unresolved_conflicts'))}</ul></div></div>
      <div class="position"><strong>단계 결론</strong><p>{esc(item.get('final_position'))}</p>
        <div class="final-numbers"><span>권고 행동 {esc(recommended_action)}</span><span>목표 보유 {esc(target_quantity)}</span></div>
      </div>
    </article>
    """


def build_html(runs_root: Path, target_run: str) -> str:
    target_dir = runs_root / target_run
    summary = load_json(target_dir / "pipeline-summary.json")
    if not summary:
        raise ValueError(f"pipeline-summary.json is missing or invalid: {target_dir}")
    target_started_at = str(summary.get("started_at") or "")
    if not target_started_at:
        raise ValueError(f"pipeline summary started_at is missing: {target_dir}")
    runs = find_runs(runs_root, target_started_at)
    if not runs:
        raise ValueError(f"no daily-trading runs found through {target_started_at}")
    fills, fill_status, fill_scope = cumulative_today_fills(runs)
    trade_html, submitted_orders = render_trade_ledger(runs, fills, fill_status, fill_scope)
    overview = render_header(summary, len(runs), fills, submitted_orders) + render_chart_periods(
        runs_root,
        target_started_at,
        runs,
    )
    trades = trade_html + render_time_symbol_inspector(runs, fills)
    evidence = render_news_timeline(runs) + render_market_news_timeline(runs) + render_financial_details(target_dir)
    operations = (
        render_run_timeline(runs)
        + render_holdings(target_dir, summary)
        + render_market_and_quality(target_dir, summary)
        + render_policy_panel(summary)
        + render_review_trigger_panel(summary)
    )

    tab_buttons = [
        '<button type="button" class="tab-button active" data-tab="overview" role="tab" aria-selected="true">개요</button>',
        '<button type="button" class="tab-button" data-tab="trading" role="tab" aria-selected="false">거래·종목판단</button>',
        '<button type="button" class="tab-button" data-tab="evidence" role="tab" aria-selected="false">재무·뉴스</button>',
        '<button type="button" class="tab-button" data-tab="operations" role="tab" aria-selected="false">실행 기록</button>',
    ]
    tab_pages = [
        f'<section class="tab-page active" id="overview" role="tabpanel">{overview}</section>',
        f'<section class="tab-page" id="trading" role="tabpanel">{trades}</section>',
        f'<section class="tab-page" id="evidence" role="tabpanel">{evidence}</section>',
        f'<section class="tab-page" id="operations" role="tabpanel">{operations}</section>',
    ]

    body = f"""
    <div class="app-header">
      <div class="brand"><span class="brand-mark">D</span><div><strong>Danta Report</strong><small>cumulative trading intelligence</small></div></div>
      <div class="report-date"><span>REPORT CUT-OFF</span><strong>{esc(target_started_at[:16].replace('T', ' '))} KST</strong></div>
    </div>
    <nav class="tab-bar" role="tablist" aria-label="리포트 페이지">{''.join(tab_buttons)}</nav>
    {''.join(tab_pages)}
    """
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>당일 누적 daily-trading 리포트 · {esc(target_started_at)}</title>
  <style>
    :root {{ color-scheme:light; --bg:#f1f4f9; --surface:rgba(255,255,255,.96); --subtle:#f7f9fc; --text:#172033; --muted:#68758b; --line:#dce4ef; --accent:#4e5ce8; --accent-2:#0b8d81; --accent-bg:#eef0ff; --ok:#087a55; --ok-bg:#e8f7f1; --warn:#9a5b00; --warn-bg:#fff7df; --bad:#bd2c3a; --bad-bg:#fff0f1; --bull:#176b4d; --bear:#a33342; --shadow:0 16px 45px rgba(31,47,77,.09); }}
    * {{ box-sizing:border-box; }}
    html {{ scroll-behavior:smooth; }}
    body {{ margin:0; background:radial-gradient(circle at 4% 2%,rgba(78,92,232,.10),transparent 30%),radial-gradient(circle at 96% 4%,rgba(11,141,129,.10),transparent 26%),var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans KR",sans-serif; line-height:1.55; }}
    button {{ font:inherit; }}
    main {{ width:min(1240px,calc(100% - 28px)); margin:18px auto 64px; }}
    h1,h2,h3,h4,p {{ margin-top:0; }} h1 {{ margin-bottom:10px; font-size:clamp(27px,4vw,42px); letter-spacing:-.045em; }} h2 {{ margin-bottom:5px; font-size:23px; letter-spacing:-.03em; }} h3 {{ margin:24px 0 10px; font-size:17px; }} h4 {{ margin-bottom:4px; font-size:16px; }}
    code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.9em; overflow-wrap:anywhere; }}
    .app-header {{ display:flex; align-items:center; justify-content:space-between; gap:14px; padding:8px 4px 15px; }} .brand {{ display:flex; align-items:center; gap:10px; }} .brand-mark {{ display:grid; width:38px; height:38px; place-items:center; border-radius:12px; background:linear-gradient(145deg,var(--accent),var(--accent-2)); color:#fff; font-weight:900; box-shadow:0 8px 20px rgba(78,92,232,.26); }} .brand strong,.brand small,.report-date span,.report-date strong {{ display:block; }} .brand small,.report-date span {{ color:var(--muted); font-size:11px; }} .report-date {{ text-align:right; }}
    .tab-bar {{ position:sticky; z-index:20; top:8px; display:flex; gap:7px; padding:7px; margin-bottom:15px; overflow-x:auto; border:1px solid rgba(220,228,239,.9); border-radius:16px; background:rgba(255,255,255,.82); box-shadow:0 10px 30px rgba(31,47,77,.08); backdrop-filter:blur(16px); }} .tab-button {{ flex:0 0 auto; padding:10px 14px; border:0; border-radius:11px; background:transparent; color:var(--muted); cursor:pointer; font-size:13px; font-weight:800; transition:.18s ease; }} .tab-button:hover {{ background:var(--subtle); color:var(--text); }} .tab-button.active {{ background:linear-gradient(135deg,var(--accent),#6d55de); color:#fff; box-shadow:0 7px 18px rgba(78,92,232,.25); }}
    .tab-page {{ display:none; animation:page-in .22s ease; }} .tab-page.active {{ display:block; }} @keyframes page-in {{ from {{ opacity:.3; transform:translateY(5px); }} to {{ opacity:1; transform:none; }} }}
    .hero {{ position:relative; overflow:hidden; padding:36px; border-radius:26px; background:linear-gradient(135deg,#15234f 0%,#3f4fc4 57%,#087d76 100%); color:#fff; box-shadow:0 24px 60px rgba(30,47,90,.18); }} .hero::after {{ content:""; position:absolute; width:260px; height:260px; right:-80px; top:-120px; border-radius:50%; background:rgba(255,255,255,.10); }}
    .hero p {{ max-width:850px; color:#e3eaff; }} .eyebrow,.kicker {{ margin-bottom:9px; color:#94a3d8; font-size:12px; font-weight:800; letter-spacing:.1em; }}
    .chips {{ display:flex; flex-wrap:wrap; gap:8px; margin:18px 0 10px; }} .chip {{ padding:6px 10px; border:1px solid rgba(255,255,255,.24); border-radius:999px; background:rgba(255,255,255,.1); font-size:13px; }} .chip.success {{ color:#b7f7dc; }} .run-id {{ display:flex; align-items:baseline; flex-wrap:wrap; gap:7px; color:#cbd7ff; }} .run-id span {{ font-size:12px; font-weight:800; }} .run-id small {{ width:100%; color:#aebce9; font-size:11px; }}
    .metrics,.coverage {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:16px 0; }}
    .metrics article,.coverage article {{ padding:17px; border:1px solid var(--line); border-radius:16px; background:var(--surface); box-shadow:var(--shadow); }}
    .metrics span,.coverage span,.metrics small,.coverage small {{ display:block; color:var(--muted); font-size:12px; }} .metrics strong,.coverage strong {{ display:block; margin:4px 0; font-size:20px; letter-spacing:-.025em; }}
    .panel,.decision-hero {{ padding:25px; margin-top:16px; border:1px solid rgba(220,228,239,.95); border-radius:21px; background:var(--surface); box-shadow:var(--shadow); }}
    .section-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:16px; }} .section-note {{ color:var(--muted); }}
    .badge {{ display:inline-flex; padding:4px 8px; border-radius:999px; font-size:11px; font-weight:800; white-space:nowrap; }} .badge.ok {{ color:var(--ok); background:var(--ok-bg); }} .badge.warn {{ color:var(--warn); background:var(--warn-bg); }} .badge.bad {{ color:var(--bad); background:var(--bad-bg); }} .badge.info {{ color:var(--accent); background:var(--accent-bg); }} .badge.muted {{ color:var(--muted); background:#edf1f6; }}
    .notice {{ margin:10px 0 18px; padding:13px 15px; border-left:4px solid var(--accent); border-radius:10px; background:var(--accent-bg); font-size:13px; }}
    .table-wrap {{ width:100%; overflow-x:auto; border:1px solid var(--line); border-radius:12px; }} table {{ width:100%; border-collapse:collapse; min-width:760px; }} th,td {{ padding:11px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; font-size:13px; }} th {{ position:sticky; top:0; background:#eef3f9; color:#475569; font-size:12px; }} tr:last-child td {{ border-bottom:0; }}
    .positive {{ color:var(--ok); }} .negative {{ color:var(--bad); }}
    .warning-list {{ display:grid; gap:9px; margin-top:18px; }} .warning {{ padding:14px; border-left:4px solid var(--warn); border-radius:10px; background:var(--warn-bg); }} .warning.bad-border {{ border-left-color:var(--bad); background:var(--bad-bg); }} .warning p {{ margin:4px 0 0; font-size:13px; }}
    .chart-period-tabs {{ display:flex; width:max-content; gap:5px; padding:5px; margin:16px 0 -6px auto; border:1px solid var(--line); border-radius:12px; background:var(--surface); }} .chart-period-button {{ padding:7px 13px; border:0; border-radius:8px; background:transparent; color:var(--muted); cursor:pointer; font-size:12px; font-weight:800; }} .chart-period-button.active {{ background:var(--accent); color:#fff; }} .chart-period-panel {{ display:none; }} .chart-period-panel.active {{ display:block; animation:page-in .18s ease; }}
    .combined-chart-card {{ padding:24px; margin-top:16px; border:1px solid var(--line); border-radius:22px; background:var(--surface); box-shadow:var(--shadow); }} .chart-legend {{ display:flex; flex-wrap:wrap; gap:14px; margin:13px 0 4px; color:var(--muted); font-size:12px; }} .chart-legend span {{ display:flex; align-items:center; gap:6px; }} .chart-legend i {{ width:22px; height:4px; border-radius:999px; background:var(--legend); }} .interactive-chart {{ position:relative; }} .interactive-line-chart {{ display:block; width:100%; height:auto; overflow:visible; }} .series-line {{ fill:none; stroke-width:4; stroke-linecap:round; stroke-linejoin:round; }} .total-line {{ stroke:#4f6df5; }} .pnl-line {{ stroke:#e14c68; stroke-dasharray:11 7; }} .asset-line {{ stroke:#8b5cf6; stroke-dasharray:4 5; }} .kospi-line {{ stroke:#0b9a86; }} .chart-zero {{ stroke:#9ca7b8; stroke-width:1.5; }} .chart-cursor {{ stroke:#617089; stroke-width:1.5; stroke-dasharray:5 5; opacity:0; }} .chart-marker {{ stroke:#fff; stroke-width:3; opacity:0; }} .total-marker {{ fill:#4f6df5; }} .pnl-marker {{ fill:#e14c68; }} .asset-marker {{ fill:#8b5cf6; }} .kospi-marker {{ fill:#0b9a86; }} .series-point {{ stroke:#fff; stroke-width:1.5; opacity:.85; pointer-events:none; }} .total-point {{ fill:#4f6df5; }} .pnl-point {{ fill:#e14c68; }} .asset-point {{ fill:#8b5cf6; }} .kospi-point {{ fill:#0b9a86; }} .chart-hit-area {{ fill:transparent; cursor:crosshair; pointer-events:all; touch-action:none; }} .chart-tooltip {{ position:absolute; z-index:5; min-width:190px; padding:11px 13px; border:1px solid rgba(220,228,239,.9); border-radius:12px; background:rgba(20,29,52,.94); color:#fff; box-shadow:0 12px 32px rgba(18,27,50,.25); opacity:0; pointer-events:none; transform:translate(-50%,-112%); transition:opacity .12s ease; font-size:12px; }} .chart-tooltip.visible {{ opacity:1; }} .chart-tooltip strong,.chart-tooltip span {{ display:block; }} .chart-tooltip strong {{ margin-bottom:5px; }} .chart-tooltip span {{ color:#d8e0f3; }} .chart-scrubber {{ padding:8px 5px 0; }} .chart-scrubber-label,.chart-scrubber-ends {{ display:flex; align-items:center; justify-content:space-between; gap:12px; }} .chart-scrubber-label {{ margin-bottom:3px; color:var(--muted); font-size:12px; font-weight:800; }} .chart-scrubber-time {{ color:var(--accent); font-weight:900; }} .chart-range-slider {{ width:100%; accent-color:var(--accent); cursor:ew-resize; touch-action:pan-x; }} .chart-scrubber-ends {{ color:var(--muted); font-size:10px; }} .series-ranges {{ display:flex; justify-content:flex-end; flex-wrap:wrap; gap:13px; color:var(--muted); font-size:11px; }}
    .portfolio-chart-layout {{ display:grid; grid-template-columns:minmax(300px,.85fr) minmax(320px,1.15fr); align-items:center; gap:28px; padding:20px; margin-bottom:12px; border:1px solid var(--line); border-radius:16px; background:var(--subtle); }} .portfolio-pie {{ position:relative; width:min(100%,430px); margin:auto; }} .portfolio-pie-svg {{ display:block; width:100%; height:auto; filter:drop-shadow(0 12px 22px rgba(24,36,64,.12)); }} .pie-slice {{ stroke:#fff; stroke-width:2; cursor:pointer; outline:none; transition:opacity .15s ease,stroke-width .15s ease; }} .pie-slice:hover,.pie-slice.active,.pie-slice:focus {{ opacity:.8; stroke-width:5; }} .pie-tooltip {{ position:absolute; z-index:6; min-width:205px; padding:11px 13px; border-radius:12px; background:rgba(20,29,52,.95); color:#fff; box-shadow:0 12px 32px rgba(18,27,50,.25); opacity:0; pointer-events:none; transform:translate(-50%,-112%); transition:opacity .1s ease; font-size:12px; }} .pie-tooltip.visible {{ opacity:1; }} .pie-tooltip strong,.pie-tooltip span {{ display:block; }} .pie-tooltip strong {{ margin-bottom:4px; }} .pie-tooltip span {{ color:#d8e0f3; }} .sector-legend {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:9px; }} .sector-legend>div:first-child {{ grid-column:1/-1; }} .sector-legend h3 {{ margin:0; }} .sector-legend>div:first-child p:last-child {{ margin:3px 0 5px; color:var(--muted); font-size:12px; }} .sector-legend-item {{ display:flex; align-items:center; gap:9px; padding:10px; border:1px solid var(--line); border-radius:11px; background:#fff; }} .sector-legend-item i {{ width:13px; height:34px; flex:0 0 13px; border-radius:999px; background:var(--sector-color); }} .sector-legend-item strong,.sector-legend-item small {{ display:block; }} .sector-legend-item small {{ color:var(--muted); font-size:10px; }} .holdings-table {{ margin-top:14px; }}
    .chart-grid-wrap {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; margin-top:16px; }} .chart-card {{ padding:20px; overflow:hidden; border:1px solid var(--line); border-radius:20px; background:var(--surface); box-shadow:var(--shadow); }} .chart-head {{ display:flex; justify-content:space-between; gap:12px; }} .chart-head h3 {{ margin:0; font-size:19px; }} .chart-head p {{ margin:5px 0 0; color:var(--muted); font-size:12px; }} .chart-stat {{ min-width:100px; text-align:right; }} .chart-stat span,.chart-stat strong {{ display:block; }} .chart-stat span {{ color:var(--muted); font-size:11px; }} .line-chart {{ display:block; width:100%; height:auto; margin-top:5px; overflow:visible; }} .chart-grid {{ stroke:#e5eaf2; stroke-width:1; }} .chart-y,.chart-x {{ fill:#738096; font-size:15px; }} .chart-point {{ stroke:#fff; stroke-width:3; }} .chart-range {{ display:flex; justify-content:flex-end; flex-wrap:wrap; gap:14px; color:var(--muted); font-size:11px; }} .chart-range strong {{ color:var(--text); }}
    .financial-list {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }} .financial-card {{ padding:17px; border:1px solid var(--line); border-radius:15px; background:var(--subtle); }} .financial-body {{ padding:13px; border-radius:11px; background:#fff; }} .financial-body ul {{ margin:0; padding-left:20px; }} .evidence-title {{ display:flex; gap:10px; align-items:flex-start; margin-bottom:12px; }} .evidence-title h3 {{ margin:0; }} .evidence-title p {{ margin:4px 0 0; color:var(--muted); font-size:12px; }} .source-note {{ margin:12px 0 0; color:var(--muted); font-size:11px; }}
    .news-timeline {{ position:relative; display:grid; gap:12px; padding-left:22px; }} .news-timeline::before {{ content:""; position:absolute; left:7px; top:9px; bottom:9px; width:2px; background:linear-gradient(var(--accent),var(--accent-2)); }} .news-run {{ position:relative; padding:16px; border:1px solid var(--line); border-radius:15px; background:var(--subtle); }} .news-run::before {{ content:""; position:absolute; left:-22px; top:22px; width:11px; height:11px; border:3px solid var(--bg); border-radius:50%; background:var(--accent); }} .news-run-head {{ display:flex; justify-content:space-between; gap:10px; margin-bottom:9px; }} .news-run-head>div {{ display:flex; align-items:center; flex-wrap:wrap; gap:8px; }} .news-time {{ display:grid; width:50px; height:28px; place-items:center; border-radius:8px; background:var(--accent); color:#fff; font-size:12px; font-weight:900; }} .news-run-head span:not(.news-time):not(.badge) {{ color:var(--muted); font-size:11px; }} .news-run-body {{ padding:4px 12px; border-radius:11px; background:#fff; }} .news-item {{ padding:10px 0; border-bottom:1px solid var(--line); }} .news-item:last-child {{ border-bottom:0; }} .news-item time {{ margin-left:8px; color:var(--muted); font-size:11px; }} .news-item p {{ margin:5px 0 0; }} .empty-state {{ padding:12px; border-radius:9px; background:var(--subtle); color:var(--muted); font-size:12px; }}
    .time-wheel {{ position:relative; width:min(100%,360px); margin:14px auto 20px; }} .time-wheel-caption {{ display:block; margin-bottom:6px; color:var(--muted); font-size:11px; font-weight:800; text-align:center; }} .time-wheel::after {{ position:absolute; right:0; bottom:68px; left:0; height:68px; border:1px solid rgba(78,92,232,.28); border-radius:14px; background:rgba(78,92,232,.06); content:""; pointer-events:none; }} .time-selector {{ position:relative; display:flex; height:204px; padding-block:68px; overflow-x:hidden; overflow-y:auto; flex-direction:column; scroll-snap-type:y mandatory; scrollbar-width:none; overscroll-behavior-y:contain; -webkit-mask-image:linear-gradient(transparent,#000 29%,#000 71%,transparent); mask-image:linear-gradient(transparent,#000 29%,#000 71%,transparent); }} .time-selector::-webkit-scrollbar {{ display:none; }} .time-button {{ display:grid; min-height:68px; flex:0 0 68px; padding:10px 16px; border:0; border-radius:13px; background:transparent; color:var(--text); cursor:pointer; grid-template-columns:74px minmax(0,1fr); grid-template-rows:1fr 1fr; align-items:center; text-align:left; scroll-snap-align:center; scroll-snap-stop:always; opacity:.48; transform:scale(.92); transition:opacity .16s ease,transform .16s ease,color .16s ease; }} .time-button:hover {{ color:var(--accent); opacity:.75; }} .time-button.active {{ color:var(--accent); opacity:1; transform:scale(1); }} .time-button strong {{ grid-row:1/-1; font-size:22px; }} .time-button span,.time-button small {{ color:inherit; font-size:10px; }} .time-analysis-panel {{ display:none; }} .time-analysis-panel.active {{ display:block; animation:page-in .18s ease; }} .time-panel-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; padding-top:4px; }} .time-panel-head p {{ color:var(--muted); }} .run-activity {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:9px; margin:12px 0 19px; }} .activity-card {{ padding:13px; border:1px solid var(--line); border-radius:12px; background:#fff; }} .activity-card.order {{ border-left:4px solid var(--accent); }} .activity-card.filled {{ border-left:4px solid var(--ok); background:linear-gradient(145deg,#fff,var(--ok-bg)); }} .activity-card.fill {{ border-left:4px solid var(--accent-2); }} .activity-card.blocked {{ border-left:4px solid var(--bad); background:linear-gradient(145deg,#fff,var(--bad-bg)); }} .activity-card span,.activity-card strong,.activity-card small {{ display:block; }} .activity-card span,.activity-card small {{ color:var(--muted); font-size:11px; }}
    .trade-symbol-selector {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:9px; margin-bottom:16px; }} .trade-symbol-button {{ display:grid; min-width:0; padding:12px; border:1px solid var(--line); border-radius:13px; background:var(--subtle); color:var(--text); text-align:left; cursor:pointer; grid-template-columns:minmax(0,1fr) auto; align-items:center; gap:10px; transition:.16s ease; }} .trade-symbol-button.group-trade {{ border-color:#a9ddcc; background:var(--ok-bg); }} .trade-symbol-button.group-guard {{ border-color:#f3bbc2; background:var(--bad-bg); }} .trade-symbol-button:hover {{ border-color:#aab4ff; transform:translateY(-1px); }} .trade-symbol-button.active {{ border-color:var(--accent); box-shadow:0 8px 22px rgba(78,92,232,.12); }} .trade-symbol-button.group-trade.active {{ background:linear-gradient(145deg,var(--ok-bg),#eefaf7); }} .trade-symbol-button.group-guard.active {{ background:linear-gradient(145deg,var(--bad-bg),#fff4f5); }} .symbol-button-left {{ display:flex; min-width:0; flex-direction:column; }} .symbol-button-status {{ display:flex; min-height:19px; flex-wrap:wrap; gap:4px; }} .symbol-button-name {{ display:-webkit-box; min-width:0; min-height:2.6em; margin-top:4px; overflow:hidden; color:var(--text); font-size:13px; line-height:1.3; text-overflow:ellipsis; white-space:normal; -webkit-box-orient:vertical; -webkit-line-clamp:2; }} .symbol-button-right {{ display:flex; flex:0 0 auto; align-items:flex-end; flex-direction:column; gap:3px; text-align:right; }} .symbol-button-right code {{ color:var(--muted); font-size:10px; }} .symbol-score {{ display:block; padding:0; background:transparent; color:var(--text); font-size:13px; font-weight:800; line-height:1.2; }} .trade-symbol-button.active .symbol-score {{ background:transparent; color:var(--accent); }} .mini-badge {{ padding:2px 5px; border-radius:999px; font-size:9px; }} .mini-badge.judge,.mini-badge.held {{ color:var(--ok); background:var(--ok-bg); }} .mini-badge.analyst,.mini-badge.unheld,.mini-badge.unknown {{ color:var(--muted); background:#e9eef5; }} .mini-badge.trade {{ color:var(--accent); background:var(--accent-bg); }} .mini-badge.attempt {{ color:var(--bad); background:var(--bad-bg); }} .symbol-analysis-panel {{ display:none; padding:20px; border:1px solid var(--line); border-radius:16px; background:linear-gradient(145deg,#fff,var(--subtle)); }} .symbol-analysis-panel.active {{ display:block; animation:page-in .18s ease; }} .symbol-focus-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }} .symbol-focus-head h2 {{ margin-bottom:5px; }} .symbol-focus-head p {{ color:var(--muted); }} .focus-badges {{ display:flex; flex-wrap:wrap; justify-content:flex-end; gap:6px; }} .inline-analysis,.inline-judge {{ margin-top:20px; }} .compact-phase {{ margin-top:18px; padding-top:15px; }} .final-card.full {{ margin-top:12px; }}
    .decision-hero {{ background:linear-gradient(145deg,#fff,#f4f6ff); }} .decision-hero>div:first-child p:last-child {{ color:var(--muted); }} .decision-meta {{ display:flex; flex-wrap:wrap; gap:8px; }} .decision-meta>span {{ padding:7px 9px; border:1px solid var(--line); border-radius:9px; background:#fff; font-size:12px; }} .decision-orders {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:14px; }} .decision-orders article {{ padding:13px; border-radius:12px; background:linear-gradient(135deg,var(--accent-bg),#eefaf7); }} .decision-orders span,.decision-orders strong,.decision-orders small {{ display:block; }} .decision-orders span,.decision-orders small {{ color:var(--muted); font-size:11px; }}
    .analyst-list {{ display:grid; gap:14px; }} .analyst-card {{ padding:17px; border:1px solid var(--line); border-radius:14px; background:var(--subtle); }} .card-title {{ display:flex; align-items:flex-start; gap:10px; margin-bottom:12px; }} .card-title h3,.card-title h4 {{ margin:0; }} .card-title p {{ margin:3px 0 0; color:var(--muted); font-size:13px; }} .index {{ display:grid; flex:0 0 30px; height:30px; place-items:center; border-radius:9px; background:var(--accent-bg); color:var(--accent); font-weight:900; }}
    .phase,.final-section {{ margin-top:26px; padding-top:22px; border-top:3px solid var(--line); }} .phase-title {{ display:flex; align-items:baseline; gap:12px; margin-bottom:12px; }} .phase-title span {{ font-size:22px; font-weight:900; }} .phase-title small {{ color:var(--muted); }}
    .debate-side {{ padding:17px; margin:12px 0; border:1px solid var(--line); border-radius:16px; background:var(--subtle); }} .debate-side>h3 {{ margin-top:0; }} .bull-text {{ color:var(--bull); }} .bear-text {{ color:var(--bear); }}
    .debate-symbol {{ padding:16px; margin-top:12px; border:1px solid var(--line); border-left:5px solid var(--bull); border-radius:12px; background:#fff; }} .debate-symbol.bear {{ border-left-color:var(--bear); }}
    .arguments {{ display:grid; gap:9px; }} .argument {{ padding:12px; border:1px solid var(--line); border-radius:10px; background:var(--subtle); }} .argument p {{ margin:7px 0; }} .argument small {{ display:block; color:var(--muted); overflow-wrap:anywhere; }}
    .debate-meta {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:10px; }} .debate-meta>div,.position {{ padding:12px; border-radius:10px; background:var(--accent-bg); }} .debate-meta ul {{ margin:7px 0 0; padding-left:20px; }} .position {{ margin-top:10px; }} .position p {{ margin:5px 0 0; }}
    .final-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }} .final-card {{ padding:15px; border:1px solid var(--line); border-radius:12px; background:var(--subtle); }} .final-card h3 {{ display:inline; }} .final-card p {{ margin:8px 0 0; }} .final-numbers {{ display:flex; flex-wrap:wrap; gap:7px; margin-top:10px; }} .final-numbers span {{ padding:5px 8px; border-radius:8px; background:var(--accent-bg); font-size:12px; font-weight:700; }}
    .decision-evidence {{ margin-top:12px; padding-top:12px; border-top:1px dashed var(--line); }} .decision-evidence h4 {{ margin:0 0 6px; font-size:12px; color:var(--muted); }} .decision-evidence ul {{ display:grid; gap:6px; margin:0; padding:0; list-style:none; }} .decision-evidence-item {{ padding:8px 10px; border-radius:9px; background:#fff; border-left:4px solid var(--bull); }} .decision-evidence-item.bear {{ border-left-color:var(--bear); }} .decision-evidence-item a {{ color:var(--accent); font-weight:700; text-decoration:none; }} .decision-evidence-item a:hover {{ text-decoration:underline; }} .decision-evidence-item p {{ margin:4px 0 0; font-size:12px; }}
    .thesis-block {{ display:grid; gap:8px; margin-top:12px; padding-top:12px; border-top:1px dashed var(--line); }} .thesis-block h4 {{ margin:0 0 4px; font-size:12px; color:var(--muted); }} .thesis-block p {{ margin:0; font-size:12px; }} .thesis-source {{ color:var(--muted); }} .thesis-conditions {{ display:grid; gap:4px; margin:4px 0 0; padding-left:16px; font-size:12px; }} .thesis-condition.matched {{ font-weight:700; }}
    footer {{ padding:24px 4px 0; color:var(--muted); font-size:12px; text-align:center; }}
    @media(max-width:1000px) {{ .trade-symbol-selector {{ grid-template-columns:repeat(4,minmax(0,1fr)); }} }}
    @media(max-width:900px) {{ .chart-grid-wrap,.financial-list,.portfolio-chart-layout {{ grid-template-columns:1fr; }} .trade-symbol-selector {{ grid-template-columns:repeat(3,minmax(0,1fr)); }} .run-activity {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
    @media(max-width:800px) {{ main {{ width:min(100% - 16px,720px); margin-top:8px; }} .app-header {{ padding-inline:4px; }} .hero,.panel,.decision-hero,.combined-chart-card {{ padding:19px; border-radius:17px; }} .metrics,.coverage,.trade-symbol-selector {{ grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }} .section-head,.symbol-focus-head,.time-panel-head {{ display:block; }} .section-head>.badge,.symbol-focus-head>.badge,.time-panel-head>.badge {{ margin-top:8px; }} .focus-badges {{ justify-content:flex-start; margin-top:8px; }} .debate-meta,.final-grid,.decision-orders {{ grid-template-columns:1fr; }} .news-run-head {{ display:block; }} .news-run-head>div+div {{ margin-top:7px; }} }}
    @media(max-width:480px) {{ .metrics strong,.coverage strong {{ font-size:16px; }} .phase-title {{ display:block; }} .phase-title small {{ display:block; margin-top:4px; }} .trade-symbol-selector,.run-activity,.sector-legend {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .portfolio-chart-layout {{ padding:12px; }} }}
    @media print {{ body {{ background:#fff; }} main {{ width:100%; margin:0; }} .app-header,.tab-bar {{ display:none; }} .tab-page {{ display:block !important; }} .hero,.panel,.metrics article,.chart-card {{ box-shadow:none; break-inside:avoid; }} .table-wrap {{ overflow:visible; }} }}
  </style>
</head>
<body><main>{body}<footer>daily-trading 실행 artifact에서 생성한 단일 파일 리포트입니다. raw prompt, 세션 ID, 절대 경로와 비밀값은 포함하지 않습니다.</footer></main>
<script>
  (() => {{
    const setTooltipRows = (tooltip, rows) => {{
      tooltip.replaceChildren();
      rows.forEach((value, index) => {{
        const row = document.createElement(index === 0 ? 'strong' : 'span');
        row.textContent = value;
        tooltip.appendChild(row);
      }});
    }};
    const buttons = [...document.querySelectorAll('.tab-button')];
    const pages = [...document.querySelectorAll('.tab-page')];
    const activate = (id, updateHash = true) => {{
      const target = pages.find((page) => page.id === id) || pages[0];
      pages.forEach((page) => page.classList.toggle('active', page === target));
      buttons.forEach((button) => {{
        const active = button.dataset.tab === target.id;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', String(active));
      }});
      if (updateHash) history.replaceState(null, '', `#${{target.id}}`);
      window.scrollTo({{ top: 0, behavior: 'smooth' }});
    }};
    buttons.forEach((button) => button.addEventListener('click', () => activate(button.dataset.tab)));

    document.querySelectorAll('.time-selector').forEach((selector) => {{
      const timeButtons = [...selector.querySelectorAll('.time-button')];
      const container = selector.closest('#trade-symbol-analysis');
      const timePanels = [...container.querySelectorAll('.time-analysis-panel')];
      let scrollFrame = null;
      const selectTime = (timeId) => {{
        timeButtons.forEach((button) => {{
          const active = button.dataset.timeTarget === timeId;
          button.classList.toggle('active', active);
          button.setAttribute('aria-selected', String(active));
        }});
        timePanels.forEach((panel) => panel.classList.toggle('active', panel.dataset.timePanel === timeId));
      }};
      const centerTimeButton = (button, behavior = 'smooth') => {{
        selector.scrollTo({{
          top: button.offsetTop - (selector.clientHeight - button.offsetHeight) / 2,
          behavior,
        }});
      }};
      const selectNearestTime = () => {{
        scrollFrame = null;
        const center = selector.getBoundingClientRect().top + selector.clientHeight / 2;
        const nearest = timeButtons.reduce((best, button) => {{
          const rect = button.getBoundingClientRect();
          const distance = Math.abs(rect.top + rect.height / 2 - center);
          return !best || distance < best.distance ? {{ button, distance }} : best;
        }}, null);
        if (nearest) selectTime(nearest.button.dataset.timeTarget);
      }};
      timeButtons.forEach((button) => button.addEventListener('click', () => {{
        selectTime(button.dataset.timeTarget);
        centerTimeButton(button);
      }}));
      selector.addEventListener('scroll', () => {{
        if (scrollFrame !== null) cancelAnimationFrame(scrollFrame);
        scrollFrame = requestAnimationFrame(selectNearestTime);
      }}, {{ passive: true }});
      selector.addEventListener('keydown', (event) => {{
        if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
        event.preventDefault();
        const currentIndex = Math.max(0, timeButtons.findIndex((button) => button.classList.contains('active')));
        const offset = event.key === 'ArrowUp' ? -1 : 1;
        const next = timeButtons[Math.max(0, Math.min(timeButtons.length - 1, currentIndex + offset))];
        if (!next) return;
        next.focus();
        selectTime(next.dataset.timeTarget);
        centerTimeButton(next);
      }});
      const initialTime = timeButtons.find((button) => button.classList.contains('active')) || timeButtons[0];
      if (initialTime) {{
        selectTime(initialTime.dataset.timeTarget);
        requestAnimationFrame(() => centerTimeButton(initialTime, 'auto'));
      }}
    }});

    document.querySelectorAll('.trade-symbol-selector').forEach((selector) => {{
      const symbolButtons = [...selector.querySelectorAll('.trade-symbol-button')];
      const container = selector.closest('.time-analysis-panel');
      const symbolPanels = [...container.querySelectorAll('.symbol-analysis-panel')];
      const selectSymbol = (symbolId) => {{
        symbolButtons.forEach((button) => {{
          const active = button.dataset.symbolTarget === symbolId;
          button.classList.toggle('active', active);
          button.setAttribute('aria-pressed', String(active));
        }});
        symbolPanels.forEach((panel) => panel.classList.toggle('active', panel.dataset.symbolPanel === symbolId));
      }};
      symbolButtons.forEach((button) => button.addEventListener('click', () => selectSymbol(button.dataset.symbolTarget)));
      const initialSymbol = symbolButtons.find((button) => button.classList.contains('active')) || symbolButtons[0];
      if (initialSymbol) selectSymbol(initialSymbol.dataset.symbolTarget);
    }});

    document.querySelectorAll('.chart-period-switcher').forEach((switcher) => {{
      const periodButtons = [...switcher.querySelectorAll('.chart-period-button')];
      const periodPanels = [...switcher.querySelectorAll('.chart-period-panel')];
      const selectPeriod = (period) => {{
        periodButtons.forEach((button) => {{
          const active = button.dataset.chartPeriodTarget === period;
          button.classList.toggle('active', active);
          button.setAttribute('aria-selected', String(active));
        }});
        periodPanels.forEach((panel) => panel.classList.toggle('active', panel.dataset.chartPeriodPanel === period));
      }};
      periodButtons.forEach((button) => button.addEventListener('click', () => selectPeriod(button.dataset.chartPeriodTarget)));
    }});

    document.querySelectorAll('.combined-chart-card').forEach((card) => {{
      const points = JSON.parse(card.dataset.chartPoints || '[]');
      const svg = card.querySelector('.interactive-line-chart');
      const hitArea = card.querySelector('.chart-hit-area');
      const tooltip = card.querySelector('.chart-tooltip');
      const cursor = card.querySelector('.chart-cursor');
      const slider = card.querySelector('.chart-range-slider');
      const sliderValue = card.querySelector('.chart-scrubber-time');
      const markers = {{
        total: card.querySelector('.total-marker'),
        pnl: card.querySelector('.pnl-marker'),
        asset: card.querySelector('.asset-marker'),
        kospi: card.querySelector('.kospi-marker'),
      }};
      let dragging = false;

      const hidePoint = () => {{
        if (dragging) return;
        cursor.style.opacity = '0';
        Object.values(markers).filter(Boolean).forEach((marker) => marker.style.opacity = '0');
        tooltip.classList.remove('visible');
      }};
      const showPointAtIndex = (requestedIndex, event = null) => {{
        const matrix = svg.getScreenCTM();
        if (!points.length || !matrix) return;
        const index = Math.max(0, Math.min(points.length - 1, requestedIndex));
        const point = points[index];
        if (slider) {{
          slider.value = String(index);
          slider.setAttribute('aria-valuetext', point.time);
        }}
        if (sliderValue) sliderValue.textContent = point.time;

        cursor.setAttribute('x1', point.x);
        cursor.setAttribute('x2', point.x);
        cursor.style.opacity = '1';
        for (const key of Object.keys(markers)) {{
          if (!markers[key]) continue;
          if (point[key + 'Y'] === null) {{
            markers[key].style.opacity = '0';
            continue;
          }}
          markers[key].setAttribute('cx', point.x);
          markers[key].setAttribute('cy', point[key + 'Y']);
          markers[key].style.opacity = '1';
        }}
        const signedPercent = (value) => (Number(value) >= 0 ? '+' : '') + Number(value).toLocaleString('ko-KR', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }}) + '%';
        const kospiText = point.kospi === null
          ? 'KOSPI 조회 실패'
          : 'KOSPI ' + Number(point.kospi).toLocaleString('ko-KR', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }}) + ' (첫 관측 대비 ' + signedPercent(point.kospiChangeFromFirstPercent) + ')';
        const assetText = point.asset === null
          ? 'KIS 총자산 조회 실패'
          : 'KIS 총자산 ' + Number(point.asset).toLocaleString('ko-KR') + '원 (첫 관측 대비 ' + signedPercent(point.assetChangePercent) + ')';
        setTooltipRows(tooltip, [
          point.time,
          '총평가 ' + Number(point.total).toLocaleString('ko-KR') + '원 (첫 관측 대비 ' + signedPercent(point.totalChangePercent) + ')',
          '평가손익 ' + Number(point.pnl).toLocaleString('ko-KR') + '원 (첫 총평가 대비 기여 ' + signedPercent(point.pnlImpactPercent) + 'p)',
          assetText,
          kospiText,
          'regime ' + point.regimeLabel + ' (' + point.regime + ')',
        ]);
        const chartRect = card.querySelector('.interactive-chart').getBoundingClientRect();
        let clientX = event ? event.clientX : null;
        let clientY = event ? event.clientY : null;
        if (!event) {{
          const selectedPoint = svg.createSVGPoint();
          selectedPoint.x = point.x;
          selectedPoint.y = point.totalY;
          const screenPoint = selectedPoint.matrixTransform(matrix);
          clientX = screenPoint.x;
          clientY = screenPoint.y;
        }}
        const tooltipX = Math.max(105, Math.min(chartRect.width - 105, clientX - chartRect.left));
        const tooltipY = Math.max(90, Math.min(chartRect.height - 10, clientY - chartRect.top));
        tooltip.style.left = tooltipX + 'px';
        tooltip.style.top = tooltipY + 'px';
        tooltip.classList.add('visible');
      }};
      const showPoint = (event) => {{
        if (!points.length || !svg.getScreenCTM()) return;
        const svgPoint = svg.createSVGPoint();
        svgPoint.x = event.clientX;
        svgPoint.y = event.clientY;
        const local = svgPoint.matrixTransform(svg.getScreenCTM().inverse());
        const left = Number(hitArea.dataset.left);
        const width = Number(hitArea.dataset.width);
        const ratio = Math.max(0, Math.min(1, (local.x - left) / width));
        const index = Math.round(ratio * (points.length - 1));
        showPointAtIndex(index, event);
      }};

      hitArea.addEventListener('pointerdown', (event) => {{
        dragging = true;
        hitArea.setPointerCapture(event.pointerId);
        showPoint(event);
      }});
      hitArea.addEventListener('pointermove', showPoint);
      hitArea.addEventListener('pointerup', (event) => {{
        showPoint(event);
        dragging = false;
        if (hitArea.hasPointerCapture(event.pointerId)) hitArea.releasePointerCapture(event.pointerId);
      }});
      hitArea.addEventListener('pointercancel', () => {{ dragging = false; hidePoint(); }});
      hitArea.addEventListener('pointerleave', hidePoint);
      if (slider) slider.addEventListener('input', () => showPointAtIndex(Number(slider.value)));
    }});

    document.querySelectorAll('.portfolio-pie').forEach((chart) => {{
      const slices = [...chart.querySelectorAll('.pie-slice')];
      const tooltip = chart.querySelector('.pie-tooltip');
      const hideSlice = () => {{
        slices.forEach((slice) => slice.classList.remove('active'));
        tooltip.classList.remove('visible');
      }};
      const showSlice = (slice, event) => {{
        slices.forEach((item) => item.classList.toggle('active', item === slice));
        setTooltipRows(tooltip, [
          slice.dataset.symbolName + ' ' + slice.dataset.symbolId,
          '업종 ' + slice.dataset.industry,
          '평가액 ' + Number(slice.dataset.valuation).toLocaleString('ko-KR') + '원 · ' + Number(slice.dataset.weight).toLocaleString('ko-KR', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }}) + '%',
          '보유 ' + slice.dataset.quantity + '주 · 평가손익 ' + slice.dataset.pnl + '원 (' + slice.dataset.pnlRate + '%)',
        ]);
        const rect = chart.getBoundingClientRect();
        const hasPointer = event && Number.isFinite(event.clientX) && Number.isFinite(event.clientY);
        const x = hasPointer ? event.clientX - rect.left : rect.width / 2;
        const y = hasPointer ? event.clientY - rect.top : rect.height / 2;
        tooltip.style.left = Math.max(112, Math.min(rect.width - 112, x)) + 'px';
        tooltip.style.top = Math.max(90, Math.min(rect.height - 8, y)) + 'px';
        tooltip.classList.add('visible');
      }};
      slices.forEach((slice) => {{
        slice.addEventListener('pointerenter', (event) => showSlice(slice, event));
        slice.addEventListener('pointermove', (event) => showSlice(slice, event));
        slice.addEventListener('pointerdown', (event) => showSlice(slice, event));
        slice.addEventListener('pointerleave', hideSlice);
        slice.addEventListener('focus', () => showSlice(slice));
        slice.addEventListener('blur', hideSlice);
      }});
    }});

    activate(location.hash.slice(1) || 'overview', false);
  }})();
</script></body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", type=Path)
    parser.add_argument("--target-run")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        codex_exec_root = Path(__file__).resolve().parents[4]
        codex_exec_root_text = str(codex_exec_root)
        if codex_exec_root_text not in sys.path:
            sys.path.insert(0, codex_exec_root_text)
        from service.pipelines.daily_trading.tests.test_render_html_report import self_test

        return self_test()
    if not args.runs_root or not args.target_run or not args.output:
        parser.error("--runs-root, --target-run, and --output are required unless --self-test is used")
    raw_rendered = build_html(args.runs_root, args.target_run)
    rendered = "\n".join(line.rstrip() for line in raw_rendered.splitlines()) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"output": str(args.output), "bytes": len(rendered.encode('utf-8'))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
