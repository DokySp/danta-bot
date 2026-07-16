#!/usr/bin/env python3
"""Tests for the cumulative single-file daily-trading HTML report."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ..scripts.render_html_report import build_html, order_status_badge, render_combined_chart, render_header


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def make_run(runs_root: Path, run_id: str, started_at: str, *, target: bool) -> Path:
    run_dir = runs_root / run_id
    account = {
        "cash_amount": 3_000_000,
        "orderable_cash_amount": 2_500_000,
        "securities_valuation_amount": 7_000_000,
        "total_evaluation_amount": 10_000_000 + (100_000 if target else 0),
        "total_pnl_amount": 100_000 + (10_000 if target else 0),
        "today_trade_amounts": {"buy_amount": 200_000, "sell_amount": 100_000},
    }
    summary = {
        "run_id": run_id,
        "started_at": started_at,
        "status": "success",
        "account_display_summary": account,
        "account_asset_summary": {"total_asset_amount": 10_500_000},
        "evidence_summary": {
            "symbol_count": 2,
            "price_only_symbol_count": 0,
            "financial": {
                "status": "supplied",
                "cache_counts": {"usable_symbol_count": 2, "wanted_symbol_count": 2},
            },
            "news": {
                "status": "supplied",
                "cache_counts": {"usable_symbol_count": 2, "wanted_symbol_count": 2},
            },
        },
        "token_usage": {"total": {"total_tokens": 123}},
        "stages": [{"stage": "execution-plan", "status": "success", "detail": "status=success"}],
    }
    if target:
        summary["order_lifecycle"] = {
            "status": "partial",
            "active_order_count": 1,
            "previous_submitted_cash_order_count": 2,
            "holding_state_issue_count": 1,
        }
    orders = []
    if target:
        orders.append(
            {
                "symbol_id": "000001",
                "symbol_name": "알파전자",
                "direction": "buy",
                "validated_order_quantity": 1,
                "order_price": 100_000,
                "order_or_reservation_id": "order-1",
                "result": "submitted",
            }
        )
    decision_symbols = [
        {
            "symbol_id": "000001",
            "symbol_name": "알파전자",
            "evidence_mode": "full",
            "price": {"current_or_last": 100_000},
            "account_exposure": {"current_live_holding_quantity": 2},
            "financial_summary": {
                "cache_status": "hit",
                "quality_value_usable": True,
                "items": ["업종 반도체", "PER 12.3", "영업이익 증가"],
            },
            "news_summary": [
                {
                    "article_date": "2026-07-15 08:30",
                    "content": "신규 수주 공시",
                    "sentiment": "positive",
                }
            ],
        },
        {
            "symbol_id": "000002",
            "symbol_name": "베타소재",
            "evidence_mode": "full",
            "price": {"current_or_last": 50_000},
            "account_exposure": {"current_live_holding_quantity": 1},
            "financial_summary": {
                "cache_status": "hit",
                "quality_value_usable": True,
                "items": ["업종 반도체", "부채비율 안정"],
            },
            "news_summary": [
                {
                    "article_date": "2026-07-15 09:10",
                    "content": "원가 부담 확대",
                    "sentiment": "negative",
                }
            ]
            if target
            else [],
        },
    ]
    analyst_symbols = [
        {
            "symbol_id": "000001",
            "symbol_name": "알파전자",
            "final_first_score": 7.5,
            "aggregation_score_count": 1,
            "agent_scores": [
                {
                    "agent_role": "analyst-momentum-cycle",
                    "score": 7.5,
                    "reason_code": "momentum_positive",
                    "one_line_reason": "추세가 유지됩니다.",
                    "missing_data": [],
                    "excluded_from_aggregation": False,
                }
            ],
        },
        {
            "symbol_id": "000002",
            "symbol_name": "베타소재",
            "final_first_score": 4.0,
            "aggregation_score_count": 1,
            "agent_scores": [
                {
                    "agent_role": "analyst-news-flow",
                    "score": 4.0,
                    "reason_code": "news_negative",
                    "one_line_reason": "원가 뉴스가 부정적입니다.",
                    "missing_data": [],
                    "excluded_from_aggregation": False,
                }
            ],
        },
    ]
    if not target:
        analyst_symbols = analyst_symbols[:1]
    write_json(run_dir / "pipeline-summary.json", summary)
    write_json(run_dir / "execution.json", {"status": "success", "request_type": "real-submit", "orders": orders})
    write_json(
        run_dir / "decision-brief.json",
        {
            "strategy_context": {"regime": "risk_on", "advisory_reason": "국내 지수 강세"},
            "symbols": decision_symbols,
        },
    )
    write_json(
        run_dir / "market-index-snapshot.json",
        {
            "indexes": [
                {"symbol": "KOSPI", "name": "코스피", "value": 3210.5, "change_percent": 1.25, "status": "success", "source": "KIS"},
                {"symbol": "KOSDAQ", "name": "코스닥", "value": 850.2, "change_percent": 0.42, "status": "success", "source": "KIS"},
            ]
        },
    )
    write_json(run_dir / "analyst-review.json", {"symbols": analyst_symbols})
    write_json(
        run_dir / "judge-debate.json",
        {
            "phases": [
                {
                    "phase": "opening",
                    "sides": {
                        "bull": {
                            "output": {
                                "symbols": [
                                    {
                                        "symbol_id": "000001",
                                        "symbol_name": "알파전자",
                                        "arguments": [{"argument_id": "b1", "kind": "claim", "statement": "수주 모멘텀", "evidence_refs": ["news"], "targets": []}],
                                        "concessions": [],
                                        "unresolved_conflicts": [],
                                        "final_position": "매수 우위",
                                        "recommended_action": "buy",
                                        "target_holding_quantity": 3,
                                    }
                                ]
                            }
                        },
                        "bear": {"output": {"symbols": []}},
                    },
                }
            ]
        },
    )
    write_json(
        run_dir / "judge-review.json",
        {
            "symbols": [
                {
                    "symbol_id": "000001",
                    "symbol_name": "알파전자",
                    "final_holding_quantity": 3,
                    "target_position_value_krw": 300_000,
                    "relative_attractiveness_rank": 1,
                    "reason_code": "buy",
                    "one_line_reason": "상대 매력도 우위",
                }
            ]
        },
    )
    write_json(
        run_dir / "account-before-order.json",
        {
            "symbols": [
                {"symbol_id": "000001", "symbol_name": "알파전자", "current_live_holding_quantity": 2, "valuation_amount": 200_000, "pnl_amount": 20_000, "pnl_rate": 11.1},
                {"symbol_id": "000002", "symbol_name": "베타소재", "current_live_holding_quantity": 1, "valuation_amount": 50_000, "pnl_amount": -5_000, "pnl_rate": -9.1},
            ]
        },
    )
    fills = [
        {
            "order_id": "order-1" if target else "manual-outside-1",
            "symbol_id": "000001" if target else "999999",
            "symbol_name": "알파전자" if target else "외부종목",
            "direction": "buy" if target else "sell",
            "filled_quantity": 1,
            "filled_price": 100_000 if target else 12_000,
            "filled_amount": 100_000 if target else 12_000,
            "filled_at": "2026-07-15T10:02:00+09:00" if target else started_at,
            "source_actor": "bot" if target else "non_bot_user",
        }
    ]
    write_json(
        run_dir / "today-fills.json",
        {"status": "success", "skipped": False, "fill_scope": "account", "fills": fills},
    )
    if target:
        write_json(
            run_dir / "order-lifecycle.json",
            {
                "previous_submitted_cash_orders": [
                    {
                        "order_id": "order-1",
                        "broker_reconciliation": {
                            "status": "rejected",
                            "rejected_quantity": 1,
                            "filled_quantity": 0,
                        },
                    }
                ]
            },
        )
    return run_dir


def self_test() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        runs_root = Path(temporary) / "reports" / "runs"
        make_run(runs_root, "run-0900", "2026-07-15T09:00:00+09:00", target=False)
        make_run(runs_root, "run-0900-second", "2026-07-15T09:00:30+09:00", target=False)
        make_run(runs_root, "run-1000", "2026-07-15T10:00:00+09:00", target=True)
        rendered = build_html(runs_root, "run-1000")
        required = [
            "10:00까지의 당일 전체 거래",
            "data-time-target=\"run-0-0900\"",
            "data-time-target=\"run-1-0900\"",
            "data-time-target=\"run-2-1000\"",
            "알파전자",
            "베타소재",
            "Analyst only",
            "Final Judge",
            "추세가 유지됩니다.",
            "수주 모멘텀",
            "권고 행동 buy",
            "목표 보유 3주",
            "외부종목",
            "계좌 전체 일별 체결 조회",
            "신규 수주 공시",
            "원가 부담 확대",
            'sentiment positive',
            'sentiment negative',
            "KOSPI 3210.50 (+1.25%)",
            "regimeLabel&quot;:&quot;강세",
            "regime&quot;:&quot;risk_on",
            "KIS 총자산",
            'class="chart-range-slider"',
            'max="2"',
            "slider.addEventListener('input'",
            "같은 업종 종목은 같은 색상",
            "주문 생명주기 사전조회 partial",
            "현재 미체결 1건 · 같은 날 이전 제출 2건 · 보유수량 확인 필요 1건",
            "KIS 거절 1주",
            "row.textContent = value",
        ]
        forbidden = [
            "2026-07-14",
            "14:30",
            "초기 원금",
            "계좌 누적수익률",
            "https://",
            str(runs_root),
            'class="series-line pnl-line"',
            "innerHTML",
            "재무 수집 supplied",
        ]
        missing = [value for value in required if value not in rendered]
        present = [value for value in forbidden if value in rendered]
        selector_start = rendered.rfind("전체 Analyst 대상 종목")
        score_order_ok = rendered.find("베타소재", selector_start) < rendered.find("알파전자", selector_start)
        no_kospi_chart = render_combined_chart(
            [
                {
                    "summary": {
                        "started_at": "2026-07-15T10:00:00+09:00",
                        "account_display_summary": {
                            "total_evaluation_amount": 10_100_000,
                            "total_pnl_amount": 110_000,
                        },
                    },
                    "market": {},
                    "decision": {"strategy_context": {"regime": "neutral"}},
                }
            ]
        )
        no_kospi_ok = "10,100,000" in no_kospi_chart and "KOSPI 조회 실패" in no_kospi_chart
        if missing or present or not score_order_ok or not no_kospi_ok:
            print(json.dumps({"status": "failed", "missing": missing, "forbidden_present": present, "score_order_ok": score_order_ok, "no_kospi_ok": no_kospi_ok}, ensure_ascii=False))
            return 1
    print(json.dumps({"status": "success"}, ensure_ascii=False))
    return 0


class RenderHtmlReportSelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        self.assertEqual(self_test(), 0)

    def test_header_explains_random_run_id_suffix(self) -> None:
        rendered = render_header(
            {
                "run_id": "20260714T143005+0900-aeeb370f",
                "started_at": "2026-07-14T14:30:05+09:00",
                "status": "success",
            },
            1,
            [],
            [],
        )

        self.assertIn("실행 ID", rendered)
        self.assertIn("마지막 8자리는 해시가 아니라", rendered)
        self.assertIn("같은 초에 시작한 실행을 구분하는 임의 식별자", rendered)

    def test_rejected_order_uses_broker_status_instead_of_unconfirmed_fill(self) -> None:
        rendered = order_status_badge(
            {
                "broker_reconciliation": {
                    "status": "rejected",
                    "filled_quantity": 0,
                    "rejected_quantity": 1,
                    "remaining_quantity": 0,
                }
            }
        )

        self.assertIn("KIS 거절 1주", rendered)
        self.assertNotIn("체결 미확인", rendered)

    def test_partial_rejection_overrides_matching_fill_as_full_fill(self) -> None:
        rendered = order_status_badge(
            {
                "validated_order_quantity": 2,
                "broker_reconciliation": {
                    "status": "partially_filled_rejected",
                    "filled_quantity": 1,
                    "rejected_quantity": 1,
                    "remaining_quantity": 0,
                },
            },
            {"filled_quantity": 1, "filled_at": "2026-07-15T10:01:00+09:00"},
        )

        self.assertIn("KIS 일부 체결 1주 · 잔여 거절", rendered)
        self.assertNotIn('badge ok', rendered)

    def test_later_full_fill_overrides_pending_broker_snapshot(self) -> None:
        rendered = order_status_badge(
            {
                "validated_order_quantity": 2,
                "broker_reconciliation": {
                    "status": "pending",
                    "filled_quantity": 0,
                    "remaining_quantity": 2,
                },
            },
            {"filled_quantity": 2, "filled_at": "2026-07-15T10:01:00+09:00"},
        )

        self.assertIn("체결 10:01", rendered)
        self.assertIn('badge ok', rendered)


if __name__ == "__main__":
    raise SystemExit(self_test())
