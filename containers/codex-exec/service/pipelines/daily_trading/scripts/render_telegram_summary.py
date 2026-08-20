#!/usr/bin/env python3
"""Render a fixed Telegram response (parse_mode=HTML) from pipeline-summary.json."""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

STATUS_LABELS = {
    "success": "✅ 성공",
    "partial": "⚠️ 부분 완료",
    "failed": "❌ 실패",
}
RESULT_LABELS = {
    "submitted": "제출",
    "blocked": "차단",
    "failed": "실패",
    "skipped": "스킵",
}
DIRECTION_LABELS = {
    "buy": "매수",
    "sell": "매도",
    "none": "-",
}
REASON_LABELS = {
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
    # kept so same-day artifacts written before that change still render truthfully.
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
LIFECYCLE_CANCEL_REASONS = {"active_order_cancel_submitted"}
LIFECYCLE_CORRECTION_REASONS = {"active_order_correction_submitted", "active_order_cancel_and_replacement_submitted"}
# canonical_action/requested_action (derive_action) are increase|hold|reduce|exit, never buy/sell.
CANONICAL_ACTION_LABELS = {"increase": "확대", "reduce": "축소", "exit": "청산", "hold": "유지"}
GUARD_STATUS_LABELS = {"allowed": "허용", "blocked": "차단", "no_change": "변경없음 처리"}
SCOPE_REASON_LABELS = {
    "held_position": "보유 심사대상",
    "active_order": "활성주문 심사대상",
    "symbol_news": "종목뉴스 심사대상",
    "unheld_score_rank": "비보유 상위선정",
}
JUDGE_ERROR_LABELS = {
    "missing_judge_symbol": "Judge 결과 누락",
    "duplicate_judge_symbol": "Judge 결과 중복",
    "judge_symbol_outside_candidate_set": "후보 외 종목 응답",
}
BROKER_STATUS_LABELS = {
    "filled": "KIS 체결",
    "partially_filled": "KIS 일부 체결",
    "partially_filled_rejected": "KIS 일부 체결 후 거절",
    "partially_filled_canceled": "KIS 일부 체결 후 취소",
    "pending": "KIS 미체결",
    "accepted": "KIS 접수",
    "rejected": "KIS 거절",
    "canceled": "KIS 취소",
    "unconfirmed": "KIS 상태 미확인",
}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_text(path: Path, text_value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text_value, encoding="utf-8")
    tmp.replace(path)


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def decimal(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def money(value: Any) -> str:
    if value is None:
        return "조회 실패"
    return f"{as_int(value):,}원"


def signed_money(value: Any) -> str:
    if value is None:
        return "조회 실패"
    amount = as_int(value)
    sign = "+" if amount > 0 else ""
    return f"{sign}{amount:,}원"


def token_count(value: Any) -> str:
    amount = as_int(value)
    return f"{amount:,}" if amount > 0 else "unknown"


def text(value: Any) -> str:
    return str(value if value is not None else "").replace("\n", " ").strip()


def esc(value: Any) -> str:
    return html.escape(text(value))


def status_label(value: Any) -> str:
    raw = text(value)
    return STATUS_LABELS.get(raw, raw or "-")


def result_label(value: Any) -> str:
    raw = text(value)
    return RESULT_LABELS.get(raw, raw or "-")


def direction_label(value: Any) -> str:
    raw = text(value)
    return DIRECTION_LABELS.get(raw, raw or "-")


def reason_label(value: Any) -> str:
    raw = text(value)
    return REASON_LABELS.get(raw, raw or "-")


def broker_status_label(value: Any) -> str:
    raw = text(value)
    return BROKER_STATUS_LABELS.get(raw, raw or "")


def clock(value: Any) -> str:
    raw = text(value)
    if len(raw) >= 16 and raw[10] == "T":
        return raw[11:16]
    return ""


def symbol_html(item: dict[str, Any]) -> str:
    symbol_id = esc(item.get("symbol_id"))
    symbol_name = esc(item.get("symbol_name"))
    if symbol_id and symbol_name and symbol_id != symbol_name:
        return f"<code>{symbol_id}</code> {symbol_name}"
    return f"<code>{symbol_id or symbol_name}</code>"


def order_line(item: dict[str, Any]) -> str:
    reason_raw = str(item.get("reason") or "")
    result = result_label(item.get("result") or "-")
    reason = esc(reason_label(reason_raw or "-"))
    order_id = esc(item.get("order_or_reservation_id") or "")
    suffix = f" / <code>{order_id}</code>" if order_id else ""
    broker = item.get("broker_reconciliation") if isinstance(item.get("broker_reconciliation"), dict) else {}
    broker_text = esc(broker_status_label(broker.get("status")))
    broker_suffix = f" · {broker_text}" if broker_text else ""
    if reason_raw in LIFECYCLE_CANCEL_REASONS:
        # direction=none + quantity=0 here is an existing-order CANCELLATION event, not a "- 0주"
        # sell/buy of nothing -- the lifecycle reason must take precedence over direction/quantity.
        return f"- {symbol_html(item)}: 기존주문 취소 · {result}({reason}){broker_suffix}{suffix}"
    if reason_raw in LIFECYCLE_CORRECTION_REASONS:
        # A correction row carries the CORRECTED order's real buy/sell direction (not none), so
        # showing it as an ordinary "매수 3주" line hides that this is a correction, not a new order.
        quantity = as_int(item.get("quantity"))
        return f"- {symbol_html(item)}: 기존주문 정정({direction_label(item.get('direction') or 'none')} {quantity}주) · {result}({reason}){broker_suffix}{suffix}"
    direction = direction_label(item.get("direction") or "none")
    quantity = as_int(item.get("quantity"))
    requested_quantity = as_int(item.get("requested_quantity"))
    adjustment = item.get("quantity_adjustment") if isinstance(item.get("quantity_adjustment"), dict) else {}
    quantity_text = f"{requested_quantity}주→{quantity}주" if requested_quantity and requested_quantity != quantity else f"{quantity}주"
    adjustment_reason = esc(reason_label(adjustment.get("reason"))) if adjustment.get("reason") else ""
    adjustment_suffix = f", 조정={adjustment_reason}" if adjustment_reason else ""
    return f"- {symbol_html(item)}: {direction} {quantity_text} · {result}({reason}{adjustment_suffix}){broker_suffix}{suffix}"


def guard_intervened(row: dict[str, Any]) -> bool:
    """decision_guard.status is only ever "blocked" or "no_change" — there is no "capped"
    status — so a capped/adjusted decision must be detected from the reason_code text or from
    the judge's requested target being clamped away from the canonical target."""
    guard = row.get("decision_guard") if isinstance(row.get("decision_guard"), dict) else {}
    status = text(guard.get("status"))
    reason = text(guard.get("reason_code"))
    if status in {"blocked", "no_change"} or "capped" in reason:
        return True
    requested_target = row.get("requested_target_position_value_krw")
    canonical_target = row.get("target_position_value_krw")
    return requested_target is not None and canonical_target is not None and requested_target != canonical_target


def is_material_symbol_decision(row: dict[str, Any]) -> bool:
    if guard_intervened(row):
        return True
    if text(row.get("requested_action") or "hold") != text(row.get("canonical_action") or "hold"):
        return True
    canonical_action = text(row.get("canonical_action") or "hold")
    if canonical_action in {"increase", "reduce", "exit"}:
        return True
    expected_quantity = row.get("expected_holding_quantity")
    if expected_quantity is not None and as_int(row.get("final_holding_quantity")) != as_int(expected_quantity):
        return True
    return False


def material_symbol_priority(row: dict[str, Any]) -> int:
    if guard_intervened(row):
        return 0
    if text(row.get("requested_action") or "hold") != text(row.get("canonical_action") or "hold"):
        return 1
    return 2


def material_symbol_reason(row: dict[str, Any]) -> str:
    guard = row.get("decision_guard") if isinstance(row.get("decision_guard"), dict) else {}
    if guard_intervened(row):
        status = text(guard.get("status"))
        reason = text(guard.get("reason_code"))
        label = GUARD_STATUS_LABELS.get(status, status or "-")
        reason_label_text = reason_label(reason) if reason else ""
        return f"가드 {label}" + (f"({reason_label_text})" if reason_label_text else "")
    requested = text(row.get("requested_action") or "hold")
    canonical = text(row.get("canonical_action") or "hold")
    if requested != canonical:
        requested_label = CANONICAL_ACTION_LABELS.get(requested, requested)
        canonical_label = CANONICAL_ACTION_LABELS.get(canonical, canonical)
        return f"{requested_label}→{canonical_label} 조정"
    return ""


def symbol_decision_line(row: dict[str, Any]) -> str:
    action_raw = text(row.get("canonical_action")) or "hold"
    action = CANONICAL_ACTION_LABELS.get(action_raw, action_raw)
    final_qty = as_int(row.get("final_holding_quantity"))
    adjustment = material_symbol_reason(row) if (
        guard_intervened(row)
        or text(row.get("requested_action") or "hold") != action_raw
    ) else ""
    return f"- {symbol_html(row)}: {action} · 최종 {final_qty}주{(' · ' + adjustment) if adjustment else ''}"


def render(summary: dict[str, Any]) -> str:
    legacy_account = summary.get("account_display_summary") if isinstance(summary.get("account_display_summary"), dict) else {}
    account_asset = summary.get("account_asset_summary") if isinstance(summary.get("account_asset_summary"), dict) else {}
    reporting_view = summary.get("reporting_view") if isinstance(summary.get("reporting_view"), dict) else {}
    reporting_account = reporting_view.get("account") if isinstance(reporting_view.get("account"), dict) else {}
    reporting_orders = reporting_view.get("orders") if isinstance(reporting_view.get("orders"), dict) else {}
    reporting_domains = reporting_view.get("evidence_domains") if isinstance(reporting_view.get("evidence_domains"), dict) else {}
    full_account_view = (
        reporting_account.get("full_account") if isinstance(reporting_account.get("full_account"), dict) else {}
    )
    domestic_account_view = (
        reporting_account.get("domestic_trading_account") if isinstance(reporting_account.get("domestic_trading_account"), dict) else {}
    )
    active_order_view = reporting_orders.get("active") if isinstance(reporting_orders.get("active"), dict) else {}
    # Fall back to legacy raw fields only when the normalized view is absent entirely (older
    # pipeline-summary.json artifacts). A present-but-unavailable normalized view must keep its
    # own (unknown) values instead of being masked by conflicting legacy numbers.
    account = domestic_account_view if domestic_account_view else legacy_account
    full_account_amount = full_account_view.get("total_asset_amount") if full_account_view else account_asset.get("total_asset_amount")
    evidence = summary.get("evidence_summary") if isinstance(summary.get("evidence_summary"), dict) else {}

    def domain_view(key: str) -> dict[str, Any]:
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
            "wanted_symbol_count": counts.get("wanted_symbol_count", raw.get("wanted_symbol_count")),
            "usable_item_count": raw.get("article_count"),
        }

    financial = domain_view("financial")
    symbol_news = domain_view("symbol_news")
    market_news = domain_view("market_news")
    investor_flow = domain_view("investor_flow")
    today_fills = summary.get("today_fills_summary") if isinstance(summary.get("today_fills_summary"), dict) else {}
    execution = summary.get("execution") if isinstance(summary.get("execution"), dict) else {}
    legacy_lifecycle = summary.get("order_lifecycle") if isinstance(summary.get("order_lifecycle"), dict) else {}
    broker_summary = execution.get("broker_reconciliation") if isinstance(execution.get("broker_reconciliation"), dict) else {}
    review = summary.get("review_summary") if isinstance(summary.get("review_summary"), dict) else {}
    tokens = summary.get("token_usage") if isinstance(summary.get("token_usage"), dict) else {}
    total_tokens = ((tokens.get("total") or {}).get("total_tokens")) if isinstance(tokens.get("total"), dict) else 0
    orders = [item for item in execution.get("orders", []) if isinstance(item, dict)]
    submitted_or_blocked = [item for item in orders if item.get("result") in {"submitted", "blocked", "failed"}]
    submitted_orders = [item for item in orders if item.get("result") == "submitted"]
    submitted_count = len(submitted_orders)
    cancellation_count = len([item for item in submitted_orders if item.get("reason") in LIFECYCLE_CANCEL_REASONS])
    correction_count = len([item for item in submitted_orders if item.get("reason") in LIFECYCLE_CORRECTION_REASONS])
    new_order_count = submitted_count - cancellation_count - correction_count
    blocked_failed_count = len([item for item in orders if item.get("result") in {"blocked", "failed"}])
    skipped_count = len([item for item in orders if item.get("result") == "skipped"])
    exchange_text = f" · 거래소 {esc(execution.get('exchange'))}" if execution.get("exchange") else ""
    started_at = text(summary.get("started_at"))
    time_suffix = f" · {esc(clock(started_at))}" if clock(started_at) else ""
    lines = [
        f"<b>daily-trading {status_label(summary.get('status'))}</b>{time_suffix}",
        f"<code>{esc(summary.get('run_id') or '-')}</code>",
        "",
        f"<b>계좌</b> 국내매매 총평가 {money(account.get('total_evaluation_amount'))} · 평가손익 {signed_money(account.get('total_pnl_amount'))}",
        f"- 주문가능 {money(account.get('orderable_cash_amount'))} · 주식평가 {money(account.get('securities_valuation_amount'))}",
    ]
    if full_account_amount is not None:
        lines.append(f"- 전체 계좌 총자산(KIS 잔고) {money(full_account_amount)}")
    legacy_today = legacy_account.get("today_trade_amounts") if isinstance(legacy_account.get("today_trade_amounts"), dict) else {}
    today = today_fills if any(key in today_fills for key in ("session_date", "buy_amount", "sell_amount")) else legacy_today
    session_date = text(today_fills.get("session_date"))
    today_buy = today.get("buy_amount", today.get("today_buy_amount"))
    today_sell = today.get("sell_amount", today.get("today_sell_amount"))
    fill_count = today_fills.get("fill_count")
    if today_fills.get("status") == "success" and not today_fills.get("skipped"):
        fill_count_text = f"{as_int(fill_count)}건"
    elif as_int(fill_count) > 0:
        fill_count_text = f"확인 {as_int(fill_count)}건 ({esc(today_fills.get('status') or 'partial')})"
    else:
        fill_count_text = "조회 실패"
    cumulative_label = "당일 누계" if session_date else "거래 누계"
    cumulative_basis = f"{esc(session_date)} · 이번 run 주문 전 기준" if session_date else "기준일 미확인 · 이번 run 주문 전 기준"
    lines.extend(
        [
            "",
            f"<b>{cumulative_label}</b>({cumulative_basis}) 매수 {money(today_buy)} · 매도 {money(today_sell)} · 체결 {fill_count_text}",
            "",
            f"<b>이번 run</b> {esc(execution.get('request_type') or '-')}{exchange_text} · 신규주문 {new_order_count} · 정정 {correction_count} · 취소 {cancellation_count} · 차단·실패 {blocked_failed_count} · 스킵 {skipped_count}",
        ]
    )
    if as_int(broker_summary.get("submitted_cash_order_count")) > 0:
        unresolved_count = (
            as_int(broker_summary.get("partially_filled_order_count"))
            + as_int(broker_summary.get("pending_order_count"))
            + as_int(broker_summary.get("canceled_order_count"))
            + as_int(broker_summary.get("unconfirmed_order_count"))
        )
        lines.append(
            f"- KIS 확인: 체결 {as_int(broker_summary.get('filled_order_count'))}"
            f" · 거절 {as_int(broker_summary.get('rejected_order_count'))}"
            f" · 대기·기타 {unresolved_count}"
        )
    if active_order_view and active_order_view.get("lookup_status") != "not_looked_up":
        active_count_text = (
            f"{as_int(active_order_view.get('count'))}건" if active_order_view.get("lookup_status") == "complete" else "미조회"
        )
        lines.append(
            f"- 사전 주문상태: 미체결 {active_count_text}"
            f" · 이전 제출 {as_int(legacy_lifecycle.get('previous_submitted_cash_order_count'))}"
            f" · 수량 확인 필요 {as_int(legacy_lifecycle.get('holding_state_issue_count'))}"
        )
    elif legacy_lifecycle.get("status") not in {None, "", "not_run"}:
        lines.append(
            f"- 사전 주문상태: 미체결 {as_int(legacy_lifecycle.get('active_order_count'))}"
            f" · 이전 제출 {as_int(legacy_lifecycle.get('previous_submitted_cash_order_count'))}"
            f" · 수량 확인 필요 {as_int(legacy_lifecycle.get('holding_state_issue_count'))}"
        )
    if execution.get("requires_main_agent_order_execution"):
        lines.append("- 주문 제출 단계가 아직 완료되지 않았습니다.")
    for item in submitted_or_blocked:
        lines.append(order_line(item))
    if any(key in review for key in ("final_sell_count", "final_buy_count", "final_hold_count")):
        final_line = (
            f"- 포지션 변화(보유수량 기준): 매도 {as_int(review.get('final_sell_count'))}"
            f" · 매수 {as_int(review.get('final_buy_count'))}"
            f" · 유지 {as_int(review.get('final_hold_count'))}"
        )
        unresolved = as_int(review.get("unresolved_review_scope_count"))
        if unresolved > 0:
            final_line += f" · 미결 {unresolved}"
        blocked_guard = as_int(review.get("blocked_guard_count"))
        if blocked_guard > 0:
            final_line += f" · 가드 차단 {blocked_guard}"
        lines.append(final_line)
        # A guard policy value is only worth a Telegram line when it materially intervened
        # (something was actually blocked/capped this run) -- otherwise it's noise. AND only the
        # value relevant to the affected guard/reason is shown, not every policy value merely
        # because some intervention happened somewhere.
        intervened_rows = [row for row in review.get("symbols", []) if isinstance(row, dict) and guard_intervened(row)]
        if intervened_rows:
            policy = summary.get("execution_guards_policy") if isinstance(summary.get("execution_guards_policy"), dict) else {}
            policy_lines = []
            # Identify which guard actually intervened from decision_basis and/or the guard's own
            # reason_code/basis text (both profit_protection_blocked-style reason codes and an
            # explicit basis="profit_protection" on the guard are used in practice).
            signals = set()
            for row in intervened_rows:
                guard = row.get("decision_guard") if isinstance(row.get("decision_guard"), dict) else {}
                signals.add(text(row.get("decision_basis")))
                signals.add(text(guard.get("basis")))
                signals.add(text(guard.get("reason_code")))
            if policy and any("profit_protection" in signal for signal in signals):
                policy_lines.append(f"이익보호 최대축소 {decimal(policy.get('profit_protection_max_reduction_pct'), 1)}%")
            if policy and any("concentration_rebalance" in signal for signal in signals):
                policy_lines.append(
                    f"집중도 상한 {decimal(policy.get('concentration_rebalance_cap_pct'), 1)}%"
                    f"/최대축소 {decimal(policy.get('concentration_rebalance_max_reduction_pct'), 1)}%"
                )
            if policy and signals & {"daily_turnover_cap_exceeded", "daily_turnover_context_unavailable"}:
                policy_lines.append(f"일일 회전한도 {decimal(policy.get('max_daily_turnover_pct'), 1)}%")
            if policy_lines:
                lines.append("- 적용 정책: " + " · ".join(policy_lines))
        if any(
            key in review
            for key in ("canonical_increase_count", "canonical_reduce_count", "canonical_exit_count", "canonical_hold_count")
        ):
            lines.append(
                f"- Judge 결정(대기주문 반영 기준): 확대 {as_int(review.get('canonical_increase_count'))}"
                f" · 축소 {as_int(review.get('canonical_reduce_count'))}"
                f" · 청산 {as_int(review.get('canonical_exit_count'))}"
                f" · 유지 {as_int(review.get('canonical_hold_count'))}"
            )
    elif any(key in review for key in ("held_review_scope_count", "active_order_review_scope_count", "unheld_review_scope_count", "hold_symbol_count")):
        active_order_scope = (
            f" · 활성주문 {as_int(review.get('active_order_review_scope_count'))}"
            if "active_order_review_scope_count" in review
            else ""
        )
        lines.append(
            f"- Judge 검토: 보유 심사대상 {as_int(review.get('held_review_scope_count'))}"
            f"{active_order_scope}"
            f" · 비보유 심사대상 {as_int(review.get('unheld_review_scope_count'))}"
            f" · 미선정 {as_int(review.get('hold_symbol_count'))}"
        )
    # In-scope-but-unresolved (missing/duplicate/invalid Judge result) is never the same as
    # "not shortlisted" — show concrete names/reasons instead of collapsing into a count.
    unresolved_rows = [item for item in review.get("unresolved_review_scope", []) if isinstance(item, dict)]
    if unresolved_rows:
        lines.append("")
        lines.append("<b>Judge 미해결 종목</b>(심사대상이었으나 결과 없음/오류)")
        for row in unresolved_rows:
            scope_reason_raw = text(row.get("scope_reason"))
            scope_reason_label = SCOPE_REASON_LABELS.get(scope_reason_raw, scope_reason_raw or "-")
            error_code_raw = text(row.get("judge_error_code"))
            error_suffix = (
                f" · {JUDGE_ERROR_LABELS.get(error_code_raw, reason_label(error_code_raw))}" if error_code_raw else ""
            )
            lines.append(f"- {symbol_html(row)}: {scope_reason_label}{error_suffix}")
    review_symbols = [item for item in review.get("symbols", []) if isinstance(item, dict)]
    material_rows = sorted(
        (row for row in review_symbols if is_material_symbol_decision(row)),
        key=material_symbol_priority,
    )
    if material_rows:
        lines.append("")
        lines.append("<b>주요 종목 결정</b>")
        for row in material_rows:
            lines.append(symbol_decision_line(row))
    def domain_issue_text(label: str, payload: dict[str, Any]) -> str:
        usable_items = payload.get("usable_item_count")
        if usable_items is not None:
            return f"{label} {as_int(usable_items)}건"
        usable = payload.get("usable_symbol_count")
        wanted = payload.get("wanted_symbol_count")
        if usable is not None and wanted is not None:
            return f"{label} {as_int(usable)}/{as_int(wanted)}"
        return label

    domain_labels = {
        "financial": "재무",
        "symbol_news": "종목뉴스",
        "market_news": "시장뉴스",
        "investor_flow": "수급",
    }
    issue_domains = [
        domain_issue_text(domain_labels[key], payload)
        for key, payload in (
            ("financial", financial),
            ("symbol_news", symbol_news),
            ("market_news", market_news),
            ("investor_flow", investor_flow),
        )
        if text(payload.get("status")) not in {"", "success", "complete", "supplied"}
    ]
    if issue_domains:
        lines.append(f"- 수집 데이터 일부 확인 필요(실행과 무관): {', '.join(issue_domains)}")
    report_name = Path(text(summary.get("html_report_path"))).name if summary.get("html_report_available") else ""
    lines.extend(
        [
            "",
            f"상세 리포트: <code>{esc(report_name)}</code> 첨부" if report_name else "상세 리포트: 생성 실패 또는 미첨부",
            f"토큰: {token_count(total_tokens)}",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def self_test() -> int:
    """Run the extracted test suite through the legacy CLI contract."""
    codex_exec_root = Path(__file__).resolve().parents[4]
    codex_exec_root_text = str(codex_exec_root)
    if codex_exec_root_text not in sys.path:
        sys.path.insert(0, codex_exec_root_text)

    from service.pipelines.daily_trading.tests.test_render_telegram_summary import (
        self_test as run_external_self_test,
    )

    return run_external_self_test()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render daily-trading Telegram summary.")
    parser.add_argument("--summary", type=Path, help="pipeline-summary.json path")
    parser.add_argument("--output", type=Path, help="telegram-summary.txt path")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.summary or not args.output:
        parser.error("--summary and --output are required unless --self-test is used")
    rendered = render(load_json(args.summary))
    write_text(args.output, rendered)
    print(json.dumps({"status": "success", "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
