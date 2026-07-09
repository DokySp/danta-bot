#!/usr/bin/env python3
"""Render a fixed Telegram response (parse_mode=HTML) from pipeline-summary.json."""

from __future__ import annotations

import argparse
import html
import json
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


def review_line(item: dict[str, Any]) -> str:
    current_qty = as_int(item.get("current_live_holding_quantity"))
    final_qty = as_int(item.get("final_holding_quantity"))
    reason = esc(item.get("one_line_reason") or item.get("reason_code") or "-")
    order_result = text(item.get("order_result") or "")
    order_suffix = f" (주문={result_label(order_result)})" if order_result else ""
    return f"- {symbol_html(item)}: {current_qty}주→{final_qty}주 · {reason}{order_suffix}"


def timeline_line(item: dict[str, Any]) -> str:
    fill_time = clock(item.get("last_fill_at"))
    time_prefix = f"{fill_time} " if fill_time else ""
    net_quantity = as_int(item.get("net_quantity"))
    actor_parts = []
    bot_net = item.get("bot_net_quantity")
    manual_net = item.get("manual_net_quantity")
    if bot_net is not None and as_int(bot_net) != 0:
        actor_parts.append(f"봇 {as_int(bot_net):+d}")
    if manual_net is not None and as_int(manual_net) != 0:
        actor_parts.append(f"수동 {as_int(manual_net):+d}")
    actor_suffix = f" ({', '.join(actor_parts)})" if actor_parts else ""
    return (
        f"- {symbol_html(item)}: {time_prefix}마지막 {direction_label(item.get('last_direction'))} "
        f"{money(item.get('last_fill_price'))} · 순수량 {net_quantity:+d}{actor_suffix}"
    )


def evidence_line(payload: dict[str, Any], fallback_label: str) -> str:
    display = text(payload.get("display_text"))
    if not display:
        return f"- {fallback_label}: -"
    return f"- {esc(display)}"


def render(summary: dict[str, Any]) -> str:
    account = summary.get("account_display_summary") if isinstance(summary.get("account_display_summary"), dict) else {}
    evidence = summary.get("evidence_summary") if isinstance(summary.get("evidence_summary"), dict) else {}
    financial = evidence.get("financial") if isinstance(evidence.get("financial"), dict) else {}
    news = evidence.get("news") if isinstance(evidence.get("news"), dict) else {}
    today_timeline = summary.get("today_trade_summary") if isinstance(summary.get("today_trade_summary"), dict) else {}
    execution = summary.get("execution") if isinstance(summary.get("execution"), dict) else {}
    review = summary.get("review_summary") if isinstance(summary.get("review_summary"), dict) else {}
    tokens = summary.get("token_usage") if isinstance(summary.get("token_usage"), dict) else {}
    total_tokens = ((tokens.get("total") or {}).get("total_tokens")) if isinstance(tokens.get("total"), dict) else 0
    portfolio_except = [text(item) for item in summary.get("portfolio_except", []) if text(item)] if isinstance(summary.get("portfolio_except"), list) else []
    orders = [item for item in execution.get("orders", []) if isinstance(item, dict)]
    submitted_or_blocked = [item for item in orders if item.get("result") in {"submitted", "blocked", "failed"}]
    submitted_count = len([item for item in orders if item.get("result") == "submitted"])
    blocked_failed_count = len([item for item in orders if item.get("result") in {"blocked", "failed"}])
    skipped_count = len([item for item in orders if item.get("result") == "skipped"])
    review_symbols = [item for item in review.get("symbols", []) if isinstance(item, dict)]
    changed = [
        item
        for item in review_symbols
        if as_int(item.get("current_live_holding_quantity")) != as_int(item.get("final_holding_quantity"))
        or item.get("order_result") in {"submitted", "blocked", "failed"}
    ]
    lines = [
        f"<b>daily-trading {status_label(summary.get('status'))}</b>",
        f"<code>{esc(summary.get('run_id') or '-')}</code>",
        "",
        "<b>계좌</b>",
        f"- 예수금 총액: {money(account.get('cash_amount'))}",
    ]
    if account.get("orderable_cash_amount") is not None:
        lines.append(f"- 주문가능(D+2): {money(account.get('orderable_cash_amount'))}")
    lines.extend(
        [
            f"- 주식평가: {money(account.get('securities_valuation_amount'))}",
            f"- 총평가: {money(account.get('total_evaluation_amount'))}",
            f"- 평가손익(매입가 대비): {signed_money(account.get('total_pnl_amount'))}",
        ]
    )
    today = account.get("today_trade_amounts") if isinstance(account.get("today_trade_amounts"), dict) else {}
    today_buy = as_int(today.get("buy_amount", today.get("today_buy_amount")))
    today_sell = as_int(today.get("sell_amount", today.get("today_sell_amount")))
    if today_buy or today_sell:
        lines.extend(
            [
                "",
                "<b>당일 거래 누계</b>",
                f"- 매수 {money(today_buy)} · 매도 {money(today_sell)}",
            ]
        )
    timeline_symbols = today_timeline.get("symbols") if isinstance(today_timeline.get("symbols"), list) else []
    timeline_symbols = [item for item in timeline_symbols if isinstance(item, dict)]
    if timeline_symbols:
        lines.extend(["", "<b>당일 체결 타임라인</b>"])
        for item in timeline_symbols[:5]:
            lines.append(timeline_line(item))
        if len(timeline_symbols) > 5:
            lines.append(f"- 외 {len(timeline_symbols) - 5}종목")
    lines.extend(
        [
            "",
            "<b>근거</b>",
            evidence_line(financial, "재무"),
            evidence_line(news, "뉴스"),
            "",
            f"<b>주문</b> {esc(execution.get('request_type') or '-')} · {status_label(execution.get('status'))}",
            f"- 계획 {len(orders)}건 · 제출 {submitted_count} · 차단·실패 {blocked_failed_count} · 스킵 {skipped_count}",
        ]
    )
    if portfolio_except:
        codes = " ".join(f"<code>{esc(code)}</code>" for code in portfolio_except)
        lines.append(f"- 제외 종목: {codes}")
    if execution.get("requires_main_agent_order_execution"):
        actions = execution.get("required_main_agent_actions") if isinstance(execution.get("required_main_agent_actions"), list) else []
        lines.append(f"- 추가 실행 필요: {esc(', '.join(text(item) for item in actions)) or 'yes'}")
    for item in submitted_or_blocked[:5]:
        lines.append(order_line(item))
    if len(submitted_or_blocked) > 5:
        lines.append(f"- 외 {len(submitted_or_blocked) - 5}건")
    lines.extend(["", "<b>평결</b>"])
    for item in changed[:5]:
        lines.append(review_line(item))
    if not changed:
        lines.append("- 최종수량 변경 또는 제출 주문 없음")
    if len(changed) > 5:
        lines.append(f"- 외 {len(changed) - 5}건")
    errors = execution.get("errors") if isinstance(execution.get("errors"), list) else []
    if errors:
        lines.extend(["", "<b>오류/보류</b>"])
        for item in errors[:3]:
            if isinstance(item, dict):
                lines.append(f"- {esc(item.get('code') or '-')}: {esc(item.get('message') or '-')}")
    report_name = Path(text(summary.get("report_path"))).name if text(summary.get("report_path")) else "-"
    lines.extend(
        [
            "",
            f"보고서: <code>{esc(report_name)}</code>",
            f"토큰: {token_count(total_tokens)}",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def self_test() -> int:
    submitted_payload = {
        "run_id": "self-test",
        "status": "success",
        "report_path": "/workspace/reports/2026-06-18_포트폴리오.md",
        "portfolio_except": ["000660", "005930"],
        "account_display_summary": {
            "cash_amount": 5183620,
            "orderable_cash_amount": 1043015,
            "securities_valuation_amount": 2000,
            "total_evaluation_amount": 3000,
            "total_pnl_amount": -10,
            # 생산자(run_daily_trading_pipeline.build_account_display_summary) 실제 키
            "today_trade_amounts": {"buy_amount": 100, "sell_amount": 0},
        },
        "evidence_summary": {
            "financial": {"display_text": "재무: 31개 종목 반영"},
            "news": {"display_text": "뉴스: 0건"},
        },
        "today_trade_summary": {
            "symbol_count_with_fills": 6,
            "symbols": [
                {
                    "symbol_id": "000660",
                    "symbol_name": "SK하이닉스",
                    "last_direction": "sell",
                    "last_fill_at": "2026-06-18T12:15:15+09:00",
                    "last_fill_price": 2268000,
                    "net_quantity": 0,
                    "bot_net_quantity": -1,
                    "manual_net_quantity": 1,
                },
                *[
                    {
                        "symbol_id": f"11111{index}",
                        "symbol_name": f"종목{index}",
                        "last_direction": "buy",
                        "last_fill_at": "2026-06-18T09:00:00+09:00",
                        "last_fill_price": 1000,
                        "net_quantity": 1,
                        "bot_net_quantity": 1,
                        "manual_net_quantity": 0,
                    }
                    for index in range(1, 6)
                ],
            ],
        },
        "execution": {
            "request_type": "real-submit",
            "status": "success",
            "order_count": 1,
            "orders": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "direction": "buy",
                    "requested_quantity": 3,
                    "quantity": 1,
                    "quantity_adjustment": {"reason": "buy_quantity_reduced_to_order_available_quantity"},
                    "result": "submitted",
                    "reason": "accepted",
                    "order_or_reservation_id": "r1",
                }
            ],
        },
        "review_summary": {
            "symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 0, "final_holding_quantity": 1, "one_line_reason": "테스트 <근거>"}]
        },
        "token_usage": {"total": {"total_tokens": 123}},
    }
    blocked_payload = {
        "run_id": "self-test-blocked",
        "status": "partial",
        "report_path": "reports/2026-07-02_포트폴리오.md",
        "account_display_summary": {
            "cash_amount": None,
            "securities_valuation_amount": None,
            "total_evaluation_amount": None,
            "total_pnl_amount": None,
        },
        "evidence_summary": {
            "financial": {"display_text": "재무: cache hit"},
            "news": {},
        },
        "execution": {
            "request_type": "real-submit",
            "status": "partial",
            "order_count": 8,
            "orders": [
                {
                    "symbol_id": "402340",
                    "symbol_name": "SK스퀘어",
                    "direction": "sell",
                    "requested_quantity": 1,
                    "quantity": 1,
                    "result": "blocked",
                    "reason": "sell_quantity_exceeds_order_available_quantity",
                    "order_or_reservation_id": "0028360200",
                },
                {
                    "symbol_id": "000660",
                    "symbol_name": "SK하이닉스",
                    "direction": "none",
                    "quantity": 0,
                    "result": "blocked",
                    "reason": "symbol_in_portfolio_except_list",
                },
                *[
                    {
                        "symbol_id": f"00000{index}",
                        "symbol_name": f"스킵종목{index}",
                        "direction": "none",
                        "quantity": 0,
                        "result": "skipped",
                        "reason": "final_equals_expected_holding_quantity",
                    }
                    for index in range(1, 7)
                ],
            ],
        },
        "review_summary": {
            "symbols": [
                {
                    "symbol_id": "402340",
                    "symbol_name": "SK스퀘어",
                    "current_live_holding_quantity": 1,
                    "final_holding_quantity": 0,
                    "one_line_reason": "청산 목표",
                    "order_result": "blocked",
                }
            ]
        },
        "token_usage": {"total": {"total_tokens": 456}},
    }
    submitted_rendered = render(submitted_payload)
    blocked_rendered = render(blocked_payload)
    checks = [
        (
            "submitted",
            submitted_rendered,
            [
                "<b>daily-trading ✅ 성공</b>",
                "<code>self-test</code>",
                "- 예수금 총액: 5,183,620원",
                "- 주문가능(D+2): 1,043,015원",
                "- 평가손익(매입가 대비): -10원",
                "<b>당일 거래 누계</b>",
                "- 매수 100원 · 매도 0원",
                "<b>당일 체결 타임라인</b>",
                "- <code>000660</code> SK하이닉스: 12:15 마지막 매도 2,268,000원 · 순수량 +0 (봇 -1, 수동 +1)",
                "- 외 1종목",
                "- 재무: 31개 종목 반영",
                "<b>주문</b> real-submit · ✅ 성공",
                "- 계획 1건 · 제출 1 · 차단·실패 0 · 스킵 0",
                "- 제외 종목: <code>000660</code> <code>005930</code>",
                "- <code>005930</code> 삼성전자: 매수 3주→1주 · 제출(접수, 조정=매수가능수량으로 축소) / <code>r1</code>",
                "테스트 &lt;근거&gt;",
                "보고서: <code>2026-06-18_포트폴리오.md</code>",
                "토큰: 123",
            ],
            ["재무: 재무:", "today_buy_amount", "/workspace/reports"],
        ),
        (
            "blocked-with-skips",
            blocked_rendered,
            [
                "<b>daily-trading ⚠️ 부분 완료</b>",
                "- 예수금 총액: 조회 실패",
                "- 평가손익(매입가 대비): 조회 실패",
                "- 뉴스: -",
                "- 계획 8건 · 제출 0 · 차단·실패 2 · 스킵 6",
                "- <code>402340</code> SK스퀘어: 매도 1주 · 차단(매도가능수량 초과) / <code>0028360200</code>",
                "- <code>000660</code> SK하이닉스: - 0주 · 차단(제외 종목 차단)",
                "- <code>402340</code> SK스퀘어: 1주→0주 · 청산 목표 (주문=차단)",
            ],
            ["0원", "스킵종목1", "final_equals_expected_holding_quantity", "sell_quantity_exceeds_order_available_quantity"],
        ),
    ]
    failures = []
    for name, rendered, required, forbidden in checks:
        missing = [item for item in required if item not in rendered]
        present = [item for item in forbidden if item in rendered]
        if missing or present:
            failures.append({"case": name, "missing": missing, "forbidden_present": present, "rendered": rendered})
    if failures:
        print(json.dumps({"status": "failed", "failures": failures}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "success"}, ensure_ascii=False))
    return 0


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
