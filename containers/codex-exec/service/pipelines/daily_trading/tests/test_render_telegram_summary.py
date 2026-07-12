#!/usr/bin/env python3
"""Tests for daily-trading Telegram summary rendering."""

from __future__ import annotations

import json
import unittest

from ..scripts.render_telegram_summary import render


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
            "sell_candidate_count": 1,
            "buy_candidate_count": 2,
            "hold_symbol_count": 28,
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
                "- 후보: 매도 1 · 매수 2 · 유지 28",
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


class RenderTelegramSummarySelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        self.assertEqual(self_test(), 0)
