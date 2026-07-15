#!/usr/bin/env python3
"""Tests for daily-trading Telegram summary rendering."""

from __future__ import annotations

import json
import unittest

from ..scripts.render_telegram_summary import render


def self_test() -> int:
    submitted_payload = {
        "run_id": "self-test",
        "started_at": "2026-06-18T09:00:00+09:00",
        "status": "success",
        "report_path": "/workspace/reports/2026-06-18_포트폴리오.md",
        "html_report_path": "/workspace/reports/runs/self-test/daily-trading-report.html",
        "html_report_available": True,
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
            "financial": {"status": "supplied", "display_text": "재무: 31개 종목 반영"},
            "news": {"status": "supplied", "display_text": "뉴스: 8개 종목 기사 반영"},
        },
        "today_fills_summary": {"status": "success", "skipped": False, "fill_scope": "account", "fill_count": 7},
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
        "today_fills_summary": {"status": "partial", "skipped": False, "fill_scope": "account", "fill_count": 0},
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
                "<b>daily-trading ✅ 성공</b> · 09:00",
                "<code>self-test</code>",
                "<b>계좌</b> 총평가 3,000원 · 평가손익 -10원",
                "- 주문가능 1,043,015원 · 주식평가 2,000원",
                "<b>당일 누계</b> 매수 100원 · 매도 0원 · 체결 7건",
                "<b>이번 run</b> real-submit · 제출 1 · 차단·실패 0 · 스킵 0",
                "- <code>005930</code> 삼성전자: 매수 3주→1주 · 제출(접수, 조정=매수가능수량으로 축소) / <code>r1</code>",
                "- 평결: 매도 1 · 매수 2 · 유지 28",
                "상세 리포트: <code>daily-trading-report.html</code> 첨부",
                "토큰: 123",
            ],
            ["<b>근거</b>", "<b>당일 체결 타임라인</b>", "테스트 &lt;근거&gt;", "데이터 확인 필요", "/workspace/reports"],
        ),
        (
            "blocked-with-skips",
            blocked_rendered,
            [
                "<b>daily-trading ⚠️ 부분 완료</b>",
                "<b>계좌</b> 총평가 조회 실패 · 평가손익 조회 실패",
                "<b>당일 누계</b> 매수 조회 실패 · 매도 조회 실패 · 체결 조회 실패",
                "<b>이번 run</b> real-submit · 제출 0 · 차단·실패 2 · 스킵 6",
                "- <code>402340</code> SK스퀘어: 매도 1주 · 차단(매도가능수량 초과) / <code>0028360200</code>",
                "- <code>000660</code> SK하이닉스: - 0주 · 차단(제외 종목 차단)",
                "상세 리포트: 생성 실패 또는 미첨부",
            ],
            ["스킵종목1", "청산 목표", "final_equals_expected_holding_quantity", "sell_quantity_exceeds_order_available_quantity"],
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
