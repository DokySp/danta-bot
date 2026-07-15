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
    "sell_blocked_score_band": "점수 밴드 매도 차단",
    "buy_blocked_score_band": "점수 밴드 매수 차단",
    "score_band_value_missing": "점수 확인 불가 차단",
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
    direction = direction_label(item.get("direction") or "none")
    quantity = as_int(item.get("quantity"))
    requested_quantity = as_int(item.get("requested_quantity"))
    result = result_label(item.get("result") or "-")
    reason = esc(reason_label(item.get("reason") or "-"))
    order_id = esc(item.get("order_or_reservation_id") or "")
    suffix = f" / <code>{order_id}</code>" if order_id else ""
    adjustment = item.get("quantity_adjustment") if isinstance(item.get("quantity_adjustment"), dict) else {}
    quantity_text = f"{requested_quantity}주→{quantity}주" if requested_quantity and requested_quantity != quantity else f"{quantity}주"
    adjustment_reason = esc(reason_label(adjustment.get("reason"))) if adjustment.get("reason") else ""
    adjustment_suffix = f", 조정={adjustment_reason}" if adjustment_reason else ""
    return f"- {symbol_html(item)}: {direction} {quantity_text} · {result}({reason}{adjustment_suffix}){suffix}"


def render(summary: dict[str, Any]) -> str:
    account = summary.get("account_display_summary") if isinstance(summary.get("account_display_summary"), dict) else {}
    evidence = summary.get("evidence_summary") if isinstance(summary.get("evidence_summary"), dict) else {}
    financial = evidence.get("financial") if isinstance(evidence.get("financial"), dict) else {}
    news = evidence.get("news") if isinstance(evidence.get("news"), dict) else {}
    today_fills = summary.get("today_fills_summary") if isinstance(summary.get("today_fills_summary"), dict) else {}
    execution = summary.get("execution") if isinstance(summary.get("execution"), dict) else {}
    review = summary.get("review_summary") if isinstance(summary.get("review_summary"), dict) else {}
    tokens = summary.get("token_usage") if isinstance(summary.get("token_usage"), dict) else {}
    total_tokens = ((tokens.get("total") or {}).get("total_tokens")) if isinstance(tokens.get("total"), dict) else 0
    orders = [item for item in execution.get("orders", []) if isinstance(item, dict)]
    submitted_or_blocked = [item for item in orders if item.get("result") in {"submitted", "blocked", "failed"}]
    submitted_count = len([item for item in orders if item.get("result") == "submitted"])
    blocked_failed_count = len([item for item in orders if item.get("result") in {"blocked", "failed"}])
    skipped_count = len([item for item in orders if item.get("result") == "skipped"])
    started_at = text(summary.get("started_at"))
    time_suffix = f" · {esc(clock(started_at))}" if clock(started_at) else ""
    lines = [
        f"<b>daily-trading {status_label(summary.get('status'))}</b>{time_suffix}",
        f"<code>{esc(summary.get('run_id') or '-')}</code>",
        "",
        f"<b>계좌</b> 총평가 {money(account.get('total_evaluation_amount'))} · 평가손익 {signed_money(account.get('total_pnl_amount'))}",
        f"- 주문가능 {money(account.get('orderable_cash_amount'))} · 주식평가 {money(account.get('securities_valuation_amount'))}",
    ]
    today = account.get("today_trade_amounts") if isinstance(account.get("today_trade_amounts"), dict) else {}
    today_buy = today.get("buy_amount", today.get("today_buy_amount"))
    today_sell = today.get("sell_amount", today.get("today_sell_amount"))
    fill_count = today_fills.get("fill_count")
    if today_fills.get("status") == "success" and not today_fills.get("skipped"):
        fill_count_text = f"{as_int(fill_count)}건"
    elif as_int(fill_count) > 0:
        fill_count_text = f"확인 {as_int(fill_count)}건 ({esc(today_fills.get('status') or 'partial')})"
    else:
        fill_count_text = "조회 실패"
    lines.extend(
        [
            "",
            f"<b>당일 누계</b> 매수 {money(today_buy)} · 매도 {money(today_sell)} · 체결 {fill_count_text}",
            "",
            f"<b>이번 run</b> {esc(execution.get('request_type') or '-')} · 제출 {submitted_count} · 차단·실패 {blocked_failed_count} · 스킵 {skipped_count}",
        ]
    )
    if execution.get("requires_main_agent_order_execution"):
        lines.append("- 주문 제출 단계가 아직 완료되지 않았습니다.")
    for item in submitted_or_blocked[:3]:
        lines.append(order_line(item))
    if len(submitted_or_blocked) > 3:
        lines.append(f"- 외 {len(submitted_or_blocked) - 3}건")
    if any(key in review for key in ("sell_candidate_count", "buy_candidate_count", "hold_symbol_count")):
        lines.append(
            f"- 평결: 매도 {as_int(review.get('sell_candidate_count'))} · 매수 {as_int(review.get('buy_candidate_count'))} · 유지 {as_int(review.get('hold_symbol_count'))}"
        )
    data_issues = sum(
        1
        for payload in (financial, news)
        if text(payload.get("status")) not in {"", "success", "complete", "supplied"}
    )
    if data_issues:
        lines.append(f"- 데이터 확인 필요 {data_issues}개 영역")
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
