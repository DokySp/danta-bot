#!/usr/bin/env python3
"""Render a fixed Telegram response from pipeline-summary.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def money(value: Any) -> str:
    return f"{as_int(value):,}원"


def signed_money(value: Any) -> str:
    amount = as_int(value)
    sign = "+" if amount > 0 else ""
    return f"{sign}{amount:,}원"


def token_count(value: Any) -> str:
    amount = as_int(value)
    return f"{amount:,}" if amount > 0 else "unknown"


def text(value: Any) -> str:
    return str(value if value is not None else "").replace("\n", " ").strip()


def order_line(item: dict[str, Any]) -> str:
    symbol = text(f"{item.get('symbol_id', '')} {item.get('symbol_name', '')}".strip())
    direction = text(item.get("direction") or "none")
    quantity = as_int(item.get("quantity"))
    requested_quantity = as_int(item.get("requested_quantity"))
    result = text(item.get("result") or "-")
    reason = text(item.get("reason") or "-")
    order_id = text(item.get("order_or_reservation_id") or "")
    suffix = f" / {order_id}" if order_id else ""
    adjustment = item.get("quantity_adjustment") if isinstance(item.get("quantity_adjustment"), dict) else {}
    quantity_text = f"{requested_quantity}주 -> {quantity}주" if requested_quantity and requested_quantity != quantity else f"{quantity}주"
    adjustment_reason = text(adjustment.get("reason") or "")
    adjustment_suffix = f", 조정={adjustment_reason}" if adjustment_reason else ""
    return f"- {symbol}: {direction} {quantity_text}, {result} ({reason}{adjustment_suffix}){suffix}"


def verdict_line(item: dict[str, Any]) -> str:
    symbol = text(f"{item.get('symbol_id', '')} {item.get('symbol_name', '')}".strip())
    current_qty = as_int(item.get("current_live_holding_quantity"))
    target_qty = as_int(item.get("target_holding_quantity"))
    reason = text(item.get("one_line_reason") or item.get("reason_code") or "-")
    order_result = text(item.get("order_result") or "")
    order_suffix = f", 주문={order_result}" if order_result else ""
    return f"- {symbol}: {current_qty}주 -> {target_qty}주 ({reason}{order_suffix})"


def render(summary: dict[str, Any]) -> str:
    account = summary.get("account_display_summary") if isinstance(summary.get("account_display_summary"), dict) else {}
    evidence = summary.get("evidence_summary") if isinstance(summary.get("evidence_summary"), dict) else {}
    financial = evidence.get("financial") if isinstance(evidence.get("financial"), dict) else {}
    news = evidence.get("news") if isinstance(evidence.get("news"), dict) else {}
    execution = summary.get("execution") if isinstance(summary.get("execution"), dict) else {}
    verdict = summary.get("verdict_summary") if isinstance(summary.get("verdict_summary"), dict) else {}
    tokens = summary.get("token_usage") if isinstance(summary.get("token_usage"), dict) else {}
    total_tokens = ((tokens.get("total") or {}).get("total_tokens")) if isinstance(tokens.get("total"), dict) else 0
    orders = [item for item in execution.get("orders", []) if isinstance(item, dict)]
    submitted_or_blocked = [item for item in orders if item.get("result") in {"submitted", "blocked", "failed"}]
    verdict_symbols = [item for item in verdict.get("symbols", []) if isinstance(item, dict)]
    changed = [
        item
        for item in verdict_symbols
        if as_int(item.get("current_live_holding_quantity")) != as_int(item.get("target_holding_quantity"))
        or item.get("order_result") in {"submitted", "blocked", "failed"}
    ]
    lines = [
        f"daily-trading 결과: {text(summary.get('status') or '-')}",
        f"run_id: {text(summary.get('run_id') or '-')}",
        "",
        "계좌",
        f"- 현금: {money(account.get('cash_amount'))}",
        f"- 주식평가: {money(account.get('securities_valuation_amount'))}",
        f"- 총평가: {money(account.get('total_evaluation_amount'))}",
        f"- 평가손익: {signed_money(account.get('total_pnl_amount'))}",
    ]
    today = account.get("today_trade_amounts") if isinstance(account.get("today_trade_amounts"), dict) else {}
    if as_int(today.get("today_buy_amount")) or as_int(today.get("today_sell_amount")):
        lines.extend(
            [
                "",
                "당일 거래 누계",
                f"- 매수: {money(today.get('today_buy_amount'))}",
                f"- 매도: {money(today.get('today_sell_amount'))}",
            ]
        )
    lines.extend(
        [
            "",
            "근거",
            f"- 재무: {text(financial.get('display_text') or '-')}",
            f"- 뉴스: {text(news.get('display_text') or '-')}",
            "",
            "주문",
            f"- 요청 유형: {text(execution.get('request_type') or '-')}",
            f"- 상태: {text(execution.get('status') or '-')}",
            f"- 주문 수: {as_int(execution.get('order_count'))}",
        ]
    )
    if execution.get("requires_main_agent_order_execution"):
        actions = execution.get("required_main_agent_actions") if isinstance(execution.get("required_main_agent_actions"), list) else []
        lines.append(f"- 추가 실행 필요: {', '.join(text(item) for item in actions) or 'yes'}")
    for item in submitted_or_blocked[:5]:
        lines.append(order_line(item))
    if len(submitted_or_blocked) > 5:
        lines.append(f"- 외 {len(submitted_or_blocked) - 5}건")
    lines.extend(["", "평결"])
    for item in changed[:5]:
        lines.append(verdict_line(item))
    if not changed:
        lines.append("- 목표수량 변경 또는 제출 주문 없음")
    if len(changed) > 5:
        lines.append(f"- 외 {len(changed) - 5}건")
    errors = execution.get("errors") if isinstance(execution.get("errors"), list) else []
    if errors:
        lines.extend(["", "오류/보류"])
        for item in errors[:3]:
            if isinstance(item, dict):
                lines.append(f"- {text(item.get('code') or '-')}: {text(item.get('message') or '-')}")
    lines.extend(
        [
            "",
            f"보고서: {text(summary.get('report_path') or '-')}",
            f"총 사용 토큰: {token_count(total_tokens)}",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def self_test() -> int:
    payload = {
        "run_id": "self-test",
        "status": "success",
        "report_path": "reports/2026-06-18_포트폴리오.md",
        "account_display_summary": {
            "cash_amount": 1000,
            "securities_valuation_amount": 2000,
            "total_evaluation_amount": 3000,
            "total_pnl_amount": -10,
            "today_trade_amounts": {"today_buy_amount": 100, "today_sell_amount": 0},
        },
        "evidence_summary": {
            "financial": {"display_text": "재무 cache hit"},
            "news": {"display_text": "뉴스 0건"},
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
        "verdict_summary": {
            "symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 0, "target_holding_quantity": 1, "one_line_reason": "테스트"}]
        },
        "token_usage": {"total": {"total_tokens": 123}},
    }
    rendered = render(payload)
    required = ["daily-trading 결과: success", "계좌", "주문", "005930 삼성전자: buy 3주 -> 1주", "조정=buy_quantity_reduced_to_order_available_quantity", "평결", "총 사용 토큰: 123"]
    missing = [item for item in required if item not in rendered]
    if missing:
        print(json.dumps({"status": "failed", "missing": missing}, ensure_ascii=False))
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
