#!/usr/bin/env python3
"""Tests for daily-trading Telegram summary rendering.

`self_test` is the compatibility body invoked by the production CLI's
`--self-test` command and must keep its exact contract: print a
`{"status": "success"}` or `{"status": "failed", "failures": [...]}`
JSON line and return `0`/`1`. Each rendering scenario now lives in its
own module-level payload constant, rendered once via `scenario_render`
and checked via `case_failures` (single shared implementation of the
required/forbidden substring check). `self_test` and the granular
`TestCase` methods below both call those same helpers, so each behavior
has exactly one implementation. The wrapper-orchestration test mocks
`scenario_render`/`case_failures` rather than re-rendering every
payload, so discovery does not execute the real work twice.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from ..scripts.render_telegram_summary import order_line, render

PAYLOAD_SUBMITTED = {
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
    "account_asset_summary": {"total_asset_amount": 17039121},
    "evidence_summary": {
        "financial": {"status": "supplied", "display_text": "재무: 31개 종목 반영"},
        "symbol_news": {"status": "supplied", "display_text": "종목뉴스: 8개 종목 기사 반영"},
        "market_news": {"status": "supplied", "display_text": "시장뉴스: 6건 반영", "article_count": 6},
        "investor_flow": {"status": "partial", "display_text": "장중 수급: 일부 종목 추정치 반영"},
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
        # 최종 결정 카운트(현재→최종 보유수량 방향)와 진단용 Judge 후보 카운트가 함께 온 정상 신규 run.
        "final_sell_count": 0,
        "final_buy_count": 1,
        "final_hold_count": 0,
        "unresolved_review_scope_count": 2,
        "held_review_scope_count": 1,
        "unheld_review_scope_count": 2,
        "hold_symbol_count": 28,
        "symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 0, "final_holding_quantity": 1, "one_line_reason": "테스트 <근거>"}]
    },
    "token_usage": {"total": {"total_tokens": 123}},
}

PAYLOAD_BLOCKED = {
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
        "symbol_news": {},
        "market_news": {},
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
        # 미결 후보가 없는 정상 run: 최종 결정 라인만 표시하고 `미결`은 붙지 않는다.
        "final_sell_count": 1,
        "final_buy_count": 0,
        "final_hold_count": 0,
        "unresolved_review_scope_count": 0,
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

PAYLOAD_LEGACY = {
    # 최종 결정 카운트가 없는 구버전 summary: Judge 후보로만 표기하고 최종 평결로 오인하지 않는다.
    "run_id": "self-test-legacy",
    "status": "success",
    "account_display_summary": {},
    "evidence_summary": {"financial": {}, "symbol_news": {}, "market_news": {}},
    "today_fills_summary": {"status": "success", "skipped": False, "fill_count": 0},
    "execution": {"request_type": "analysis", "status": "success", "orders": []},
    "review_summary": {
        "held_review_scope_count": 2,
        "unheld_review_scope_count": 3,
        "hold_symbol_count": 25,
    },
    "token_usage": {"total": {"total_tokens": 0}},
}

PAYLOAD_REPORTING_VIEW_PREFERRED = {
    # reporting_view가 있으면 계좌/증거 도메인 표시는 legacy raw 필드가 아니라
    # reporting_view 값을 우선해야 한다(값이 서로 충돌하는 상황으로 검증).
    "run_id": "self-test-reporting-view",
    "status": "success",
    "account_display_summary": {
        "total_evaluation_amount": 999999,
        "orderable_cash_amount": 999999,
        "securities_valuation_amount": 999999,
        "total_pnl_amount": 999999,
    },
    "account_asset_summary": {"total_asset_amount": 999999},
    "evidence_summary": {
        "financial": {"status": "supplied", "display_text": "legacy stale text"},
        "symbol_news": {"status": "supplied", "display_text": "legacy stale text"},
        "market_news": {"status": "supplied", "display_text": "legacy stale market text"},
        "investor_flow": {"status": "supplied", "display_text": "legacy stale text"},
    },
    "order_lifecycle": {
        # legacy raw lifecycle deliberately conflicts with the normalized view below.
        "status": "success",
        "active_order_count": 99,
        "previous_submitted_cash_order_count": 2,
        "holding_state_issue_count": 0,
    },
    "reporting_view": {
        "account": {
            "full_account": {"total_asset_amount": 17039121, "available": True},
            "domestic_trading_account": {
                "total_evaluation_amount": 12601357,
                "orderable_cash_amount": 500000,
                "securities_valuation_amount": 12000000,
                "total_pnl_amount": 300000,
                "available": True,
            },
        },
        "orders": {
            "active": {"count": 3, "lookup_status": "complete"},
        },
        "evidence_domains": {
            "financial": {"status": "supplied", "coverage_text": "재무: 31개 종목 반영"},
            "symbol_news": {
                "status": "cache_missing",
                "coverage_text": "종목뉴스: 캐시 파일 없음",
                "usable_symbol_count": 9,
                "wanted_symbol_count": 31,
            },
            "market_news": {
                "status": "partial",
                "coverage_text": "시장뉴스: 4건 반영, 일부 수집원 실패",
                "usable_item_count": 4,
            },
            "investor_flow": {
                "status": "partial",
                "coverage_text": "장중 수급: 9개 종목 추정치 반영, 일부 종목 수급 없음",
                "usable_symbol_count": 26,
                "wanted_symbol_count": 31,
            },
        },
    },
    "today_fills_summary": {"status": "success", "skipped": False, "fill_scope": "account", "fill_count": 0},
    "execution": {"request_type": "analysis", "status": "success", "orders": []},
    "review_summary": {},
    "token_usage": {"total": {"total_tokens": 1}},
}

PAYLOAD_ACTIVE_ORDER_UNCONFIRMED = {
    # A normalized active-order view that is present but not lifecycle-confirmed must render
    # 미조회, never a guessed count, even when legacy raw lifecycle data conflicts.
    "run_id": "self-test-active-order-unconfirmed",
    "status": "success",
    "account_display_summary": {"total_evaluation_amount": 1000},
    "order_lifecycle": {
        "status": "partial",
        "active_order_count": 5,
        "previous_submitted_cash_order_count": 1,
        "holding_state_issue_count": 0,
    },
    "reporting_view": {
        "account": {},
        "orders": {
            "active": {"count": None, "lookup_status": "legacy_lookup_without_lifecycle_confirmation"},
        },
        "evidence_domains": {},
    },
    "evidence_summary": {},
    "today_fills_summary": {"status": "success", "skipped": False, "fill_count": 0},
    "execution": {"request_type": "analysis", "status": "success", "orders": []},
    "review_summary": {},
    "token_usage": {"total": {"total_tokens": 1}},
}

PAYLOAD_DOMESTIC_UNAVAILABLE = {
    # A normalized domestic view that is present but explicitly unavailable must not be
    # masked by conflicting legacy raw account_display_summary numbers.
    "run_id": "self-test-domestic-unavailable",
    "status": "partial",
    "account_display_summary": {
        "total_evaluation_amount": 555555,
        "orderable_cash_amount": 555555,
        "securities_valuation_amount": 555555,
        "total_pnl_amount": 555555,
    },
    "reporting_view": {
        "account": {
            "domestic_trading_account": {
                "total_evaluation_amount": None,
                "orderable_cash_amount": None,
                "securities_valuation_amount": None,
                "total_pnl_amount": None,
                "available": False,
            },
        },
        "evidence_domains": {},
    },
    "evidence_summary": {},
    "today_fills_summary": {"status": "success", "skipped": False, "fill_count": 0},
    "execution": {"request_type": "analysis", "status": "success", "orders": []},
    "review_summary": {},
    "token_usage": {"total": {"total_tokens": 1}},
}


def build_rejected_payload() -> dict:
    rejected_payload = json.loads(json.dumps(PAYLOAD_SUBMITTED, ensure_ascii=False))
    rejected_payload["run_id"] = "self-test-rejected"
    rejected_payload["status"] = "partial"
    rejected_payload["execution"]["status"] = "partial"
    rejected_payload["execution"]["broker_reconciliation"] = {
        "status": "partial",
        "submitted_cash_order_count": 1,
        "filled_order_count": 0,
        "partially_filled_order_count": 0,
        "pending_order_count": 0,
        "rejected_order_count": 1,
        "canceled_order_count": 0,
        "unconfirmed_order_count": 0,
    }
    rejected_payload["execution"]["orders"][0]["broker_reconciliation"] = {
        "status": "rejected",
        "filled_quantity": 0,
        "rejected_quantity": 1,
        "remaining_quantity": 0,
    }
    rejected_payload["order_lifecycle"] = {
        "status": "partial",
        "active_order_count": 1,
        "previous_submitted_cash_order_count": 2,
        "holding_state_issue_count": 1,
    }
    return rejected_payload


CASES = {
    "submitted": (
        PAYLOAD_SUBMITTED,
        [
            "<b>daily-trading ✅ 성공</b> · 09:00",
            "<code>self-test</code>",
            "<b>계좌</b> 국내매매 총평가 3,000원 · 평가손익 -10원",
            "- 주문가능 1,043,015원 · 주식평가 2,000원",
            "- 전체 계좌 총자산(KIS 잔고) 17,039,121원",
            "<b>당일 누계</b>(이번 run 주문 전 기준) 매수 100원 · 매도 0원 · 체결 7건",
            "<b>이번 run</b> real-submit · 신규주문 1 · 정정 0 · 취소 0 · 차단·실패 0 · 스킵 0",
            "- <code>005930</code> 삼성전자: 매수 3주→1주 · 제출(접수, 조정=매수가능수량으로 축소) / <code>r1</code>",
            "- 포지션 변화(보유수량 기준): 매도 0 · 매수 1 · 유지 0 · 미결 2",
            "- 수집 데이터 일부 확인 필요(실행과 무관): 수급",
            "상세 리포트: <code>daily-trading-report.html</code> 첨부",
            "토큰: 123",
        ],
        ["<b>근거</b>", "<b>당일 체결 타임라인</b>", "테스트 &lt;근거&gt;", "데이터 확인 필요", "/workspace/reports", "평결:", "Judge 후보", "Judge 검토", "미선정"],
    ),
    "blocked-with-skips": (
        PAYLOAD_BLOCKED,
        [
            "<b>daily-trading ⚠️ 부분 완료</b>",
            "<b>계좌</b> 국내매매 총평가 조회 실패 · 평가손익 조회 실패",
            "<b>당일 누계</b>(이번 run 주문 전 기준) 매수 조회 실패 · 매도 조회 실패 · 체결 조회 실패",
            "<b>이번 run</b> real-submit · 신규주문 0 · 정정 0 · 취소 0 · 차단·실패 2 · 스킵 6",
            "- <code>402340</code> SK스퀘어: 매도 1주 · 차단(매도가능수량 초과) / <code>0028360200</code>",
            "- <code>000660</code> SK하이닉스: - 0주 · 차단(제외 종목 차단)",
            "- 포지션 변화(보유수량 기준): 매도 1 · 매수 0 · 유지 0",
            "상세 리포트: 생성 실패 또는 미첨부",
        ],
        ["스킵종목1", "청산 목표", "final_equals_expected_holding_quantity", "sell_quantity_exceeds_order_available_quantity", "미결", "평결:", "Judge 후보", "Judge 검토", "미선정"],
    ),
    "legacy-candidate-only": (
        PAYLOAD_LEGACY,
        [
            "<b>당일 누계</b>(이번 run 주문 전 기준) 매수 조회 실패 · 매도 조회 실패 · 체결 0건",
            # hold_symbol_count(=scored_count - len(review_scope_reasons))는 Judge의 보유 판단이 아니라
            # 심사대상으로 선정되지 않은 scored 종목 수이므로 "유지"가 아닌 "미선정"으로 표기해야 한다.
            "- Judge 검토: 보유 심사대상 2 · 비보유 상위선정 3 · 미선정 25",
        ],
        ["최종 결정", "미결", "평결:", "Judge 후보", "유지"],
    ),
    "reporting-view-preferred-over-legacy": (
        PAYLOAD_REPORTING_VIEW_PREFERRED,
        [
            "<b>계좌</b> 국내매매 총평가 12,601,357원 · 평가손익 +300,000원",
            "- 주문가능 500,000원 · 주식평가 12,000,000원",
            "- 전체 계좌 총자산(KIS 잔고) 17,039,121원",
            "- 사전 주문상태: 미체결 3건",
            "- 수집 데이터 일부 확인 필요(실행과 무관): 종목뉴스 9/31, 시장뉴스 4건, 수급 26/31",
        ],
        ["999,999원", "legacy stale text", "미체결 99"],
    ),
    "active-order-unconfirmed-without-lifecycle": (
        PAYLOAD_ACTIVE_ORDER_UNCONFIRMED,
        ["- 사전 주문상태: 미체결 미조회"],
        ["미체결 5건", "미체결 0건"],
    ),
    "domestic-account-unavailable-not-masked-by-legacy": (
        PAYLOAD_DOMESTIC_UNAVAILABLE,
        ["<b>계좌</b> 국내매매 총평가 조회 실패 · 평가손익 조회 실패"],
        ["555,555원"],
    ),
    "broker-rejected": (
        None,  # built lazily by build_rejected_payload() since it derives from PAYLOAD_SUBMITTED
        [
            "<b>daily-trading ⚠️ 부분 완료</b> · 09:00",
            "- KIS 확인: 체결 0 · 거절 1 · 대기·기타 0",
            "- 사전 주문상태: 미체결 1 · 이전 제출 2 · 수량 확인 필요 1",
            "- <code>005930</code> 삼성전자: 매수 3주→1주 · 제출(접수, 조정=매수가능수량으로 축소) · KIS 거절 / <code>r1</code>",
        ],
        ["KIS 체결", "KIS 상태 미확인"],
    ),
}


def payload_for_case(name: str) -> dict:
    payload, _required, _forbidden = CASES[name]
    return build_rejected_payload() if payload is None else payload


def scenario_render(name: str) -> str:
    return render(payload_for_case(name))


def case_failures(name: str, rendered: str) -> dict | None:
    _payload, required, forbidden = CASES[name]
    missing = [item for item in required if item not in rendered]
    present = [item for item in forbidden if item in rendered]
    if missing or present:
        return {"case": name, "missing": missing, "forbidden_present": present, "rendered": rendered}
    return None


def check_case_renders_without_failures(name: str) -> None:
    rendered = scenario_render(name)
    failure = case_failures(name, rendered)
    if failure is not None:
        raise AssertionError(json.dumps(failure, ensure_ascii=False))


def self_test() -> int:
    failures = []
    for name in CASES:
        rendered = scenario_render(name)
        failure = case_failures(name, rendered)
        if failure is not None:
            failures.append(failure)
    if failures:
        print(json.dumps({"status": "failed", "failures": failures}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "success"}, ensure_ascii=False))
    return 0


class RenderTelegramSummarySelfTest(unittest.TestCase):
    def test_self_test_suite_checks_every_case_and_reports_success(self) -> None:
        """Wrapper-orchestration check only: real behavior is covered by the
        granular tests below, so this mocks the render/check helpers instead
        of re-rendering every payload a second time."""
        with patch(f"{__name__}.scenario_render", return_value="") as scenario, patch(
            f"{__name__}.case_failures", return_value=None
        ) as case_check:
            result = self_test()

        self.assertEqual(result, 0)
        self.assertEqual(scenario.call_count, len(CASES))
        self.assertEqual(case_check.call_count, len(CASES))

    def test_self_test_suite_reports_failure_when_a_case_fails(self) -> None:
        with patch(f"{__name__}.scenario_render", return_value=""), patch(
            f"{__name__}.case_failures",
            side_effect=lambda name, rendered: {"case": name, "missing": ["x"], "forbidden_present": [], "rendered": rendered},
        ):
            result = self_test()

        self.assertEqual(result, 1)


class RenderCaseTest(unittest.TestCase):
    def test_submitted(self) -> None:
        check_case_renders_without_failures("submitted")

    def test_blocked_with_skips(self) -> None:
        check_case_renders_without_failures("blocked-with-skips")

    def test_legacy_candidate_only(self) -> None:
        check_case_renders_without_failures("legacy-candidate-only")

    def test_reporting_view_preferred_over_legacy(self) -> None:
        check_case_renders_without_failures("reporting-view-preferred-over-legacy")

    def test_active_order_unconfirmed_without_lifecycle(self) -> None:
        check_case_renders_without_failures("active-order-unconfirmed-without-lifecycle")

    def test_domestic_account_unavailable_not_masked_by_legacy(self) -> None:
        check_case_renders_without_failures("domestic-account-unavailable-not-masked-by-legacy")

    def test_broker_rejected(self) -> None:
        check_case_renders_without_failures("broker-rejected")


class OrderLineLifecycleTest(unittest.TestCase):
    def test_correction_with_real_buy_direction_is_not_shown_as_an_ordinary_buy(self) -> None:
        item = {
            "symbol_id": "005930",
            "symbol_name": "삼성전자",
            "direction": "buy",
            "quantity": 3,
            "result": "submitted",
            "reason": "active_order_correction_submitted",
            "order_or_reservation_id": "r1",
        }
        line = order_line(item)
        self.assertIn("기존주문 정정", line)
        self.assertIn("매수 3주", line)  # the corrected order's real direction/qty, in context
        self.assertNotIn("- <code>005930</code> 삼성전자: 매수 3주 ·", line)  # not the ordinary-order shape

    def test_cancellation_is_not_shown_as_a_sell_of_zero_shares(self) -> None:
        item = {
            "symbol_id": "005930",
            "symbol_name": "삼성전자",
            "direction": "none",
            "quantity": 0,
            "result": "submitted",
            "reason": "active_order_cancel_submitted",
            "order_or_reservation_id": "r1",
        }
        line = order_line(item)
        self.assertIn("기존주문 취소", line)
        self.assertNotIn("- 0주", line)


class ReviewTriggerRenderTest(unittest.TestCase):
    BASE_PAYLOAD = {
        "run_id": "trigger-test",
        "status": "success",
        "account_display_summary": {},
        "evidence_summary": {},
        "today_fills_summary": {"status": "success", "skipped": False, "fill_count": 0},
        "execution": {"request_type": "real-submit", "status": "success", "orders": []},
        "review_summary": {},
        "token_usage": {"total": {"total_tokens": 0}},
    }

    def test_due_slot_uses_applied_slot_wording_not_next_slot_wording(self) -> None:
        payload = dict(self.BASE_PAYLOAD)
        payload["review_trigger"] = {
            "decision": "full",
            "reasons": ["fixed_review_time_due"],
            "due_slot": "09:05",
            "full_review_completed": True,
            "trigger_state_persisted": True,
        }
        rendered = render(payload)
        self.assertIn("적용 정기 슬롯 09:05", rendered)
        self.assertNotIn("다음 예정 슬롯", rendered)

    def test_persistence_failure_is_visible_not_reported_as_success(self) -> None:
        payload = dict(self.BASE_PAYLOAD)
        payload["review_trigger"] = {
            "decision": "full",
            "reasons": ["broker_fingerprint_changed"],
            "due_slot": None,
            "full_review_completed": True,
            "trigger_state_persisted": False,
        }
        rendered = render(payload)
        self.assertIn("저장 실패", rendered)

    def test_persistence_success_does_not_show_a_failure_warning(self) -> None:
        payload = dict(self.BASE_PAYLOAD)
        payload["review_trigger"] = {
            "decision": "full",
            "reasons": ["broker_fingerprint_changed"],
            "due_slot": None,
            "full_review_completed": True,
            "trigger_state_persisted": True,
        }
        rendered = render(payload)
        self.assertNotIn("저장 실패", rendered)

    def test_safety_trigger_reasons_are_localized(self) -> None:
        payload = dict(self.BASE_PAYLOAD)
        payload["review_trigger"] = {
            "decision": "safety_block",
            "reasons": ["account_lookup_failed"],
            "safety_reasons": ["account_lookup_failed"],
        }
        rendered = render(payload)
        self.assertIn("계좌 조회 실패", rendered)
        self.assertNotIn("account_lookup_failed", rendered)


class PolicyMentionTest(unittest.TestCase):
    BASE_PAYLOAD = {
        "run_id": "policy-test",
        "status": "success",
        "account_display_summary": {},
        "evidence_summary": {},
        "today_fills_summary": {"status": "success", "skipped": False, "fill_count": 0},
        "execution": {"request_type": "real-submit", "status": "success", "orders": []},
        "token_usage": {"total": {"total_tokens": 0}},
        "execution_guards_policy": {
            "unheld_review_top_k": 5,
            "profit_protection_max_reduction_pct": 25.0,
            "concentration_rebalance_cap_pct": 15.0,
            "concentration_rebalance_max_reduction_pct": 30.0,
            "max_daily_turnover_pct": 20.0,
        },
    }

    def test_policy_line_appears_when_guard_materially_intervened(self) -> None:
        payload = dict(self.BASE_PAYLOAD)
        payload["review_summary"] = {
            "final_sell_count": 0,
            "final_buy_count": 0,
            "final_hold_count": 1,
            "blocked_guard_count": 1,
            "symbols": [
                {
                    "symbol_id": "005930",
                    "current_live_holding_quantity": 5,
                    "final_holding_quantity": 5,
                    "decision_guard": {"status": "blocked", "reason_code": "profit_protection_blocked"},
                }
            ],
        }
        rendered = render(payload)
        self.assertIn("적용 정책: 이익보호 최대축소 25.0%", rendered)

    def test_policy_line_only_mentions_the_affected_guard_not_every_policy_value(self) -> None:
        payload = dict(self.BASE_PAYLOAD)
        payload["review_summary"] = {
            "final_sell_count": 0,
            "final_buy_count": 0,
            "final_hold_count": 1,
            "blocked_guard_count": 1,
            "symbols": [
                {
                    "symbol_id": "005930",
                    "current_live_holding_quantity": 5,
                    "final_holding_quantity": 5,
                    "decision_guard": {"status": "blocked", "reason_code": "profit_protection_blocked"},
                }
            ],
        }
        rendered = render(payload)
        self.assertIn("이익보호 최대축소", rendered)
        # Only profit_protection intervened -- concentration/turnover values must not appear.
        self.assertNotIn("집중도 상한", rendered)
        self.assertNotIn("일일 회전한도", rendered)

    def test_guard_reason_code_is_localized_not_raw_enum(self) -> None:
        payload = dict(self.BASE_PAYLOAD)
        payload["review_summary"] = {
            "final_sell_count": 0,
            "final_buy_count": 0,
            "final_hold_count": 1,
            "blocked_guard_count": 1,
            "symbols": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "current_live_holding_quantity": 5,
                    "final_holding_quantity": 5,
                    "requested_action": "reduce",
                    "canonical_action": "hold",
                    "decision_guard": {"status": "blocked", "reason_code": "profit_protection_blocked"},
                }
            ],
        }
        rendered = render(payload)
        self.assertIn("이익보호 조건 미충족 차단", rendered)
        self.assertNotIn("profit_protection_blocked", rendered)

    def test_allowed_capped_guard_status_is_localized(self) -> None:
        payload = dict(self.BASE_PAYLOAD)
        payload["review_summary"] = {
            "final_sell_count": 1,
            "final_buy_count": 0,
            "final_hold_count": 0,
            "symbols": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "current_live_holding_quantity": 10,
                    "expected_holding_quantity": 10,
                    "final_holding_quantity": 8,
                    "requested_target_position_value_krw": 500_000,
                    "target_position_value_krw": 800_000,
                    "requested_action": "reduce",
                    "canonical_action": "reduce",
                    "decision_basis": "profit_protection",
                    "decision_guard": {
                        "status": "allowed",
                        "reason_code": "profit_protection_reduction_allowed",
                        "basis": "profit_protection",
                    },
                }
            ],
        }
        rendered = render(payload)
        self.assertIn("가드 허용(이익보호 축소 허용)", rendered)
        self.assertNotIn("가드 allowed", rendered)

    def test_pending_order_baseline_hold_is_not_described_as_a_new_increase(self) -> None:
        payload = dict(self.BASE_PAYLOAD)
        payload["review_summary"] = {
            "final_sell_count": 0,
            "final_buy_count": 1,
            "final_hold_count": 0,
            "symbols": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "current_live_holding_quantity": 10,
                    "expected_holding_quantity": 15,
                    "final_holding_quantity": 15,
                    "delta_quantity": 5,
                    "requested_action": "hold",
                    "canonical_action": "hold",
                    "decision_basis": "none",
                    "decision_guard": {},
                }
            ],
        }
        rendered = render(payload)
        self.assertNotIn("<b>주요 종목 결정</b>", rendered)
        self.assertNotIn("유지(기준유지) 최종 15주 · 증가 5주", rendered)

    def test_material_decision_line_shows_expected_to_final_baseline(self) -> None:
        payload = dict(self.BASE_PAYLOAD)
        payload["review_summary"] = {
            "final_sell_count": 1,
            "final_buy_count": 0,
            "final_hold_count": 0,
            "symbols": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "current_live_holding_quantity": 10,
                    "expected_holding_quantity": 12,
                    "final_holding_quantity": 8,
                    "requested_action": "reduce",
                    "canonical_action": "reduce",
                    "decision_basis": "thesis",
                    "decision_guard": {"status": "allowed", "reason_code": "thesis_reduction_allowed"},
                }
            ],
        }
        rendered = render(payload)
        self.assertIn("대기반영 12주→최종 8주", rendered)
        self.assertIn("대기반영 기준 감소 4주", rendered)

    def test_unresolved_judge_error_reuses_localized_reason_label(self) -> None:
        payload = dict(self.BASE_PAYLOAD)
        payload["review_summary"] = {
            "unresolved_review_scope_count": 1,
            "unresolved_review_scope": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "scope_reason": "held_position",
                    "judge_error_code": "invalid_final_holding_quantity",
                }
            ],
        }
        rendered = render(payload)
        self.assertIn("최종수량 값 오류", rendered)
        self.assertNotIn("invalid_final_holding_quantity", rendered)

    def test_policy_line_absent_without_material_guard_intervention(self) -> None:
        payload = dict(self.BASE_PAYLOAD)
        payload["review_summary"] = {
            "final_sell_count": 0,
            "final_buy_count": 1,
            "final_hold_count": 0,
            "symbols": [
                {"symbol_id": "005930", "current_live_holding_quantity": 0, "final_holding_quantity": 1}
            ],
        }
        rendered = render(payload)
        self.assertNotIn("적용 정책", rendered)


if __name__ == "__main__":
    unittest.main()
