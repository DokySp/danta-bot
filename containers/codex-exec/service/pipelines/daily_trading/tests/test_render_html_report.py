#!/usr/bin/env python3
"""Tests for the cumulative single-file daily-trading HTML report."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ..scripts.render_html_report import (
    analyst_score_class,
    build_html,
    cumulative_today_fills,
    find_runs,
    order_status_badge,
    render_combined_chart,
    render_header,
    render_market_and_quality,
    render_thesis_section,
    render_trade_ledger,
    resolve_fill,
)


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
            "investor_flow": {
                "status": "partial" if target else "supplied",
                "usable_symbol_count": 1 if target else 2,
                "display_text": "장중 수급: 1개 종목 추정치 반영, 일부 종목 수급 없음",
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
    judge_review_symbol = {
        "symbol_id": "000001",
        "symbol_name": "알파전자",
        "final_holding_quantity": 3,
        "target_position_value_krw": 300_000,
        "relative_attractiveness_rank": 1,
        "reason_code": "buy",
        "one_line_reason": "상대 매력도 우위",
    }
    if target:
        judge_review_symbol.update(
            {
                "prior_thesis_context": {
                    "available": True,
                    "source_run_id": "run-0900",
                    "source_started_at": "2026-07-15T09:00:00+09:00",
                    "source_artifact": "judge-review.json",
                    "core_rationale": "메모리 사이클 반등과 원가 경쟁력 유지",
                    "invalidation_conditions": [
                        {"condition_id": "margin-compression", "description": "매출총이익률이 직전 가이던스 아래로 하락"}
                    ],
                },
                "thesis_assessment": {
                    "status": "damaged",
                    "matched_invalidation_condition_ids": ["margin-compression"],
                    "cited_argument_ids": ["000001-bear-opening-1"],
                },
                "protected_loss_gate": {"allowed": True, "reason": "damaged_evidence_confirmed"},
                "thesis_definition": {
                    "defined_at_run_id": "run-1000",
                    "core_rationale": "재고 정상화 이후 신규 성장 사이클 진입",
                    "invalidation_conditions": [
                        {"condition_id": "demand-collapse", "description": "출하량이 계획 대비 큰 폭으로 하락"}
                    ],
                },
            }
        )
    write_json(
        run_dir / "judge-review.json",
        {"symbols": [judge_review_symbol]},
    )
    write_json(
        run_dir / "account-before-order.json",
        {
            "symbols": [
                {"symbol_id": "000001", "symbol_name": "알파전자", "current_live_holding_quantity": 2, "current_price": 100_000, "valuation_amount": 200_000, "pnl_amount": 20_000, "pnl_rate": 11.1},
                {"symbol_id": "000002", "symbol_name": "베타소재", "current_live_holding_quantity": 1, "current_price": 50_000, "valuation_amount": 50_000, "pnl_amount": -5_000, "pnl_rate": -9.1},
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
            "이전 thesis (이번 run 평가 대상)",
            "run-0900",
            "메모리 사이클 반등과 원가 경쟁력 유지",
            "margin-compression",
            "매출총이익률이 직전 가이던스 아래로 하락",
            "훼손 근거 일치",
            "000001-bear-opening-1",
            "손실 보유 종목 감축 게이트",
            "훼손 근거 검증 완료",
            "damaged_evidence_confirmed",
            "신규/후속 thesis",
            "재고 정상화 이후 신규 성장 사이클 진입",
            "demand-collapse",
            "외부종목",
            "계좌 전체 일별 체결 조회",
            "주문·체결 통합 원장",
            'class="time-wheel"',
            'role="listbox"',
            "scroll-snap-type:y mandatory",
            "selector.addEventListener('scroll'",
            "event.key === 'ArrowUp'",
            "신규 수주 공시",
            "원가 부담 확대",
            'sentiment positive',
            'sentiment negative',
            "KOSPI 3210.50 (+1.25%)",
            "regimeLabel&quot;:&quot;강세",
            "regime&quot;:&quot;risk_on",
            "KIS 총자산 10,500,000원",
            'class="series-line asset-line"',
            "asset&quot;:10500000",
            "'KIS 총자산 ' + Number(point.asset)",
            'class="trade-symbol-button score-low"',
            'class="trade-symbol-button score-high active"',
            'class="chart-range-slider"',
            'max="2"',
            "slider.addEventListener('input'",
            "같은 업종 종목은 같은 색상",
            "<th>수량</th><th>현재가</th><th>평가액</th>",
            "<td>2주</td><td>100,000원</td><td>200,000원</td>",
            "주문 생명주기 사전조회 partial",
            "현재 미체결 1건 · 같은 날 이전 제출 2건 · 보유수량 확인 필요 1건",
            "KIS 거절 1주",
            "row.textContent = value",
            "수급 coverage",
            "1 / 2",
            "장중 수급 수집 partial",
            "장중 수급: 1개 종목 추정치 반영, 일부 종목 수급 없음",
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
            "확인된 체결 전체",
            "봇이 제출한 주문 전체",
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
        no_kospi_ok = (
            "10,100,000" in no_kospi_chart
            and "KOSPI 조회 실패" in no_kospi_chart
            and "KIS 총자산 조회 실패" in no_kospi_chart
            and 'class="series-line asset-line"' not in no_kospi_chart
        )
        if missing or present or not score_order_ok or not no_kospi_ok:
            print(json.dumps({"status": "failed", "missing": missing, "forbidden_present": present, "score_order_ok": score_order_ok, "no_kospi_ok": no_kospi_ok}, ensure_ascii=False))
            return 1
    print(json.dumps({"status": "success"}, ensure_ascii=False))
    return 0


class RenderHtmlReportSelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        self.assertEqual(self_test(), 0)

    def test_render_header_prefers_reporting_view_over_conflicting_legacy_fields(self) -> None:
        rendered = render_header(
            {
                "run_id": "reporting-view-probe",
                "started_at": "2026-07-16T09:00:00+09:00",
                "status": "success",
                "account_display_summary": {
                    "total_evaluation_amount": 999999,
                    "securities_valuation_amount": 999999,
                    "total_pnl_amount": 999999,
                    "orderable_cash_amount": 999999,
                },
                "account_asset_summary": {"total_asset_amount": 999999},
                "reporting_view": {
                    "account": {
                        "full_account": {"total_asset_amount": 17039121, "available": True},
                        "domestic_trading_account": {
                            "total_evaluation_amount": 12601357,
                            "securities_valuation_amount": 12000000,
                            "total_pnl_amount": 300000,
                            "orderable_cash_amount": 500000,
                            "available": True,
                        },
                    }
                },
            },
            1,
            [],
            [],
        )

        self.assertIn("12,601,357원", rendered)
        self.assertIn("KIS 총자산</span><strong>17,039,121원", rendered)
        self.assertNotIn("999,999", rendered)

    def test_render_header_falls_back_to_legacy_fields_without_reporting_view(self) -> None:
        rendered = render_header(
            {
                "run_id": "legacy-probe",
                "started_at": "2026-07-16T09:00:00+09:00",
                "status": "success",
                "account_display_summary": {"total_evaluation_amount": 5_000_000},
                "account_asset_summary": {"total_asset_amount": 6_000_000},
            },
            1,
            [],
            [],
        )

        self.assertIn("5,000,000원", rendered)
        self.assertIn("KIS 총자산</span><strong>6,000,000원", rendered)

    def test_render_market_and_quality_prefers_reporting_view_evidence_domains(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target_dir = Path(temporary)
            rendered = render_market_and_quality(
                target_dir,
                {
                    "evidence_summary": {
                        "symbol_count": 2,
                        "financial": {"status": "supplied", "display_text": "legacy stale text", "cache_counts": {"usable_symbol_count": 2, "wanted_symbol_count": 2}},
                        "news": {"status": "supplied", "display_text": "legacy stale text", "cache_counts": {"usable_symbol_count": 2, "wanted_symbol_count": 2}},
                        "investor_flow": {"status": "supplied", "display_text": "legacy stale text"},
                    },
                    "reporting_view": {
                        "evidence_domains": {
                            "financial": {"status": "supplied", "coverage_text": "재무: 2개 종목 반영", "usable_symbol_count": 2, "wanted_symbol_count": 2},
                            "news": {"status": "cache_missing", "coverage_text": "뉴스: 캐시 파일 없음", "usable_symbol_count": 0, "wanted_symbol_count": 2},
                            "investor_flow": {"status": "partial", "coverage_text": "장중 수급: 1개 종목 추정치 반영, 일부 종목 수급 없음", "usable_symbol_count": 1, "wanted_symbol_count": 2},
                        }
                    },
                    "stages": [],
                },
            )

        self.assertIn("뉴스 수집 cache_missing", rendered)
        self.assertIn("뉴스: 캐시 파일 없음", rendered)
        self.assertIn("장중 수급 수집 partial", rendered)
        self.assertIn("수급 coverage</span><strong>1 / 2", rendered)
        self.assertNotIn("legacy stale text", rendered)

    def test_render_market_and_quality_prefers_lifecycle_confirmed_active_count_over_conflicting_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target_dir = Path(temporary)
            rendered = render_market_and_quality(
                target_dir,
                {
                    "evidence_summary": {"symbol_count": 0},
                    "order_lifecycle": {
                        "status": "partial",
                        "active_order_count": 99,
                        "previous_submitted_cash_order_count": 2,
                        "holding_state_issue_count": 1,
                    },
                    "reporting_view": {
                        "orders": {"active": {"count": 3, "lookup_status": "complete"}},
                        "evidence_domains": {},
                    },
                    "stages": [],
                },
            )

        self.assertIn("현재 미체결 3건", rendered)
        self.assertNotIn("현재 미체결 99건", rendered)

    def test_render_market_and_quality_shows_unconfirmed_active_count_as_unlooked_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target_dir = Path(temporary)
            rendered = render_market_and_quality(
                target_dir,
                {
                    "evidence_summary": {"symbol_count": 0},
                    "order_lifecycle": {
                        "status": "partial",
                        "active_order_count": 5,
                        "previous_submitted_cash_order_count": 1,
                        "holding_state_issue_count": 0,
                    },
                    "reporting_view": {
                        "orders": {"active": {"count": None, "lookup_status": "legacy_lookup_without_lifecycle_confirmation"}},
                        "evidence_domains": {},
                    },
                    "stages": [],
                },
            )

        self.assertIn("현재 미체결 미조회", rendered)
        self.assertNotIn("현재 미체결 5건", rendered)
        self.assertNotIn("현재 미체결 0건", rendered)

    def test_render_header_domestic_unavailable_view_not_masked_by_conflicting_legacy(self) -> None:
        rendered = render_header(
            {
                "run_id": "domestic-unavailable-probe",
                "started_at": "2026-07-16T09:00:00+09:00",
                "status": "partial",
                "account_display_summary": {
                    "total_evaluation_amount": 555555,
                    "securities_valuation_amount": 555555,
                    "total_pnl_amount": 555555,
                    "orderable_cash_amount": 555555,
                },
                "reporting_view": {
                    "account": {
                        "domestic_trading_account": {
                            "total_evaluation_amount": None,
                            "securities_valuation_amount": None,
                            "total_pnl_amount": None,
                            "orderable_cash_amount": None,
                            "available": False,
                        },
                    }
                },
            },
            1,
            [],
            [],
        )

        self.assertNotIn("555,555", rendered)

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

    def test_analyst_score_classes_include_requested_boundaries(self) -> None:
        self.assertEqual(analyst_score_class(4), " score-low")
        self.assertEqual(analyst_score_class(6), " score-high")
        self.assertEqual(analyst_score_class(5), "")
        self.assertEqual(analyst_score_class(None), "")

    def test_render_thesis_section_shows_prior_source_matched_conditions_gate_and_successor(self) -> None:
        final_item = {
            "prior_thesis_context": {
                "available": True,
                "source_run_id": "prior-run-<xss>",
                "source_started_at": "2026-06-01T09:00:00+09:00",
                "source_artifact": "judge-review.json",
                "core_rationale": "quality moat & pricing power",
                "invalidation_conditions": [
                    {"condition_id": "margin-compression", "description": "gross margin drops below prior guidance"},
                    {"condition_id": "demand-collapse", "description": "unit shipments fall below plan"},
                ],
            },
            "thesis_assessment": {
                "status": "damaged",
                "matched_invalidation_condition_ids": ["margin-compression"],
                "cited_argument_ids": ["005930-bear-opening-1"],
            },
            "protected_loss_gate": {"allowed": True, "reason": "damaged_evidence_confirmed"},
            "thesis_definition": {
                "defined_at_run_id": "current-run",
                "core_rationale": "new cycle thesis after damage",
                "invalidation_conditions": [{"condition_id": "new-cond", "description": "new condition"}],
            },
        }
        rendered = render_thesis_section(final_item)
        self.assertIn("이전 thesis", rendered)
        self.assertIn("prior-run-&lt;xss&gt;", rendered)
        self.assertNotIn("<xss>", rendered)
        self.assertIn("2026-06-01 09:00", rendered)
        self.assertIn("judge-review.json", rendered)
        self.assertIn("quality moat &amp; pricing power", rendered)
        self.assertIn("margin-compression", rendered)
        self.assertIn("demand-collapse", rendered)
        self.assertIn("훼손 근거 일치", rendered)
        self.assertIn("훼손", rendered)
        self.assertIn("005930-bear-opening-1", rendered)
        self.assertIn("허용", rendered)
        self.assertIn("damaged_evidence_confirmed", rendered)
        self.assertIn("훼손 근거 검증 완료", rendered)
        self.assertIn("신규/후속 thesis", rendered)
        self.assertIn("new cycle thesis after damage", rendered)
        self.assertIn("new-cond", rendered)
        # Matched marking must appear only next to the actually-matched condition.
        margin_index = rendered.find("margin-compression")
        demand_index = rendered.find("demand-collapse")
        matched_index = rendered.find("훼손 근거 일치")
        self.assertLess(matched_index, margin_index)
        self.assertLess(margin_index, demand_index)
        self.assertNotIn(str(Path("reports") / "runs"), rendered)

    def test_render_thesis_section_marks_no_prior_without_fabricating_source(self) -> None:
        final_item = {
            "prior_thesis_context": {"available": False},
            "thesis_assessment": {
                "status": "damaged",
                "matched_invalidation_condition_ids": ["margin-compression"],
                "cited_argument_ids": [],
            },
            "protected_loss_gate": {"allowed": False, "reason": "no_prior_thesis"},
        }
        rendered = render_thesis_section(final_item)
        self.assertIn("신규 등록 대상", rendered)
        self.assertNotIn("source_run_id", rendered)
        self.assertNotIn("출처: run", rendered)
        self.assertIn("차단", rendered)
        self.assertIn("no_prior_thesis", rendered)
        self.assertIn("유효한 이전 thesis 없음", rendered)

    def test_render_thesis_section_is_empty_for_legacy_symbol_without_thesis_fields(self) -> None:
        legacy_item = {
            "symbol_id": "000001",
            "final_holding_quantity": 3,
            "target_position_value_krw": 300_000,
            "relative_attractiveness_rank": 1,
            "reason_code": "buy",
            "one_line_reason": "상대 매력도 우위",
        }
        self.assertEqual(render_thesis_section(legacy_item), "")

    def test_trade_ledger_merges_order_with_linked_fill(self) -> None:
        rendered, submitted_orders = render_trade_ledger(
            [
                {
                    "summary": {"started_at": "2026-07-15T10:00:00+09:00"},
                    "execution": {
                        "orders": [
                            {
                                "symbol_id": "000001",
                                "symbol_name": "알파전자",
                                "direction": "buy",
                                "quantity": 1,
                                "order_price": 100_000,
                                "order_or_reservation_id": "order-1",
                                "result": "submitted",
                            }
                        ]
                    },
                }
            ],
            [
                {
                    "order_id": "order-1",
                    "symbol_id": "000001",
                    "symbol_name": "알파전자",
                    "direction": "buy",
                    "filled_quantity": 1,
                    "filled_price": 100_000,
                    "filled_amount": 100_000,
                    "filled_at": "2026-07-15T10:02:00+09:00",
                    "source_actor": "bot",
                }
            ],
            "success",
            "account",
        )

        self.assertEqual(len(submitted_orders), 1)
        self.assertIn("주문·체결 통합 원장", rendered)
        self.assertNotIn("확인된 체결 전체", rendered)
        self.assertNotIn("봇이 제출한 주문 전체", rendered)
        self.assertEqual(rendered.count("order-1"), 1)

    def test_trade_ledger_joins_reservation_to_resulting_cash_fill_without_duplicate(self) -> None:
        rendered, submitted_orders = render_trade_ledger(
            [
                {
                    "summary": {"started_at": "2026-07-16T10:00:00+09:00"},
                    "execution": {
                        "orders": [
                            {
                                "symbol_id": "021240",
                                "symbol_name": "코웨이",
                                "direction": "buy",
                                "validated_order_quantity": 1,
                                "order_price": 95_700,
                                "order_path": "reservation",
                                "order_or_reservation_id": "103586",
                                "resulting_order_id": "0001452900",
                                "result": "submitted",
                            }
                        ]
                    },
                }
            ],
            [
                {
                    "order_id": "0001452900",
                    "symbol_id": "021240",
                    "symbol_name": "코웨이",
                    "direction": "buy",
                    "filled_quantity": 1,
                    "filled_price": 95_700,
                    "filled_amount": 95_700,
                    "filled_at": "2026-07-16T10:02:00+09:00",
                    "source_actor": "bot",
                }
            ],
            "success",
            "account",
        )

        self.assertEqual(len(submitted_orders), 1)
        self.assertIsNotNone(submitted_orders[0]["fill"])
        self.assertIn("103586", rendered)
        # Fill must join into the reservation's row, not appear again as a standalone unlinked row.
        self.assertEqual(rendered.count("0001452900"), 1)
        self.assertNotIn("확인된 체결 전체", rendered)

    def test_resolve_fill_prefers_resulting_order_id_over_reservation_id(self) -> None:
        item = {"order_or_reservation_id": "103586", "resulting_order_id": "0001452900"}
        fill_by_order = {
            "103586": {"filled_quantity": 999, "filled_at": "wrong-fill"},
            "0001452900": {"filled_quantity": 1, "filled_at": "correct-fill"},
        }

        fill = resolve_fill(item, fill_by_order)

        self.assertEqual(fill["filled_at"], "correct-fill")

    def test_resolve_fill_falls_back_to_reservation_id_when_resulting_id_unmatched(self) -> None:
        item = {"order_or_reservation_id": "103586", "resulting_order_id": "0001452900"}
        fill_by_order = {"103586": {"filled_at": "reservation-fill"}}

        fill = resolve_fill(item, fill_by_order)

        self.assertEqual(fill["filled_at"], "reservation-fill")

    def test_trade_ledger_end_to_end_join_via_lifecycle_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-1"
            write_json(
                run_dir / "pipeline-summary.json",
                {"run_id": "run-1", "started_at": "2026-07-16T10:00:00+09:00", "status": "success"},
            )
            write_json(
                run_dir / "execution.json",
                {
                    "status": "success",
                    "orders": [
                        {
                            "symbol_id": "021240",
                            "symbol_name": "코웨이",
                            "direction": "buy",
                            "validated_order_quantity": 1,
                            "order_price": 95_700,
                            "order_path": "reservation",
                            "order_or_reservation_id": "103586",
                            "result": "submitted",
                        }
                    ],
                },
            )
            write_json(
                run_dir / "order-lifecycle.json",
                {
                    "active_orders": [
                        {
                            "symbol_id": "021240",
                            "order_id": "103586",
                            "order_kind": "reservation",
                            "rsvn_ord_seq": "103586",
                            "odno": "0001452900",
                            "active_status": "inactive",
                        }
                    ],
                    "previous_submitted_cash_orders": [],
                },
            )
            write_json(
                run_dir / "today-fills.json",
                {
                    "status": "success",
                    "fill_scope": "account",
                    "fills": [
                        {
                            "order_id": "0001452900",
                            "symbol_id": "021240",
                            "symbol_name": "코웨이",
                            "direction": "buy",
                            "filled_quantity": 1,
                            "filled_price": 95_700,
                            "filled_amount": 95_700,
                            "filled_at": "2026-07-16T10:02:00+09:00",
                            "source_actor": "bot",
                        }
                    ],
                },
            )

            runs = find_runs(Path(temporary), "2026-07-16T23:59:59+09:00")
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["execution"]["orders"][0]["resulting_order_id"], "0001452900")

            fills, status, scope = cumulative_today_fills(runs)
            rendered, submitted_orders = render_trade_ledger(runs, fills, status, scope)

        self.assertIsNotNone(submitted_orders[0]["fill"])
        self.assertEqual(rendered.count("0001452900"), 1)

    def test_cumulative_today_fills_keeps_latest_order_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first_run = Path(temporary) / "run-1"
            second_run = Path(temporary) / "run-2"
            common = {
                "order_id": "order-1",
                "symbol_id": "000001",
                "symbol_name": "알파전자",
                "direction": "buy",
                "filled_price": 100_000,
                "filled_amount": 100_000,
                "filled_at": "2026-07-15T10:00:00+09:00",
            }
            write_json(
                first_run / "today-fills.json",
                {"status": "success", "fill_scope": "account", "fills": [dict(common, filled_quantity=1)]},
            )
            write_json(
                second_run / "today-fills.json",
                {
                    "status": "success",
                    "fill_scope": "account",
                    "fills": [dict(common, filled_quantity=2, filled_amount=200_000)],
                },
            )

            fills, status, scope = cumulative_today_fills([{"path": first_run}, {"path": second_run}])

        self.assertEqual(status, "success")
        self.assertEqual(scope, "account")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["filled_quantity"], 2)
        self.assertEqual(fills[0]["filled_amount"], 200_000)

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

    def _combined_chart_run(
        self,
        started_at: str,
        *,
        total: float,
        pnl: float,
        asset: float | None = None,
        kospi: float | None = None,
        kospi_change: float | None = None,
    ) -> dict:
        account_asset_summary = {"total_asset_amount": asset} if asset is not None else {}
        indexes = (
            [{"symbol": "KOSPI", "value": kospi, "change_percent": kospi_change}]
            if kospi is not None
            else []
        )
        return {
            "summary": {
                "started_at": started_at,
                "account_display_summary": {"total_evaluation_amount": total, "total_pnl_amount": pnl},
                "account_asset_summary": account_asset_summary,
            },
            "market": {"indexes": indexes},
            "decision": {"strategy_context": {"regime": "neutral"}},
        }

    def test_combined_chart_marks_every_run_on_every_rendered_series(self) -> None:
        runs = [
            self._combined_chart_run(
                "2026-07-15T09:00:00+09:00", total=10_000_000, pnl=100_000, asset=10_500_000, kospi=3200.0, kospi_change=1.0
            ),
            self._combined_chart_run(
                "2026-07-15T09:30:00+09:00", total=10_050_000, pnl=90_000, asset=10_400_000, kospi=3210.0, kospi_change=1.1
            ),
            self._combined_chart_run(
                "2026-07-15T10:00:00+09:00", total=10_100_000, pnl=80_000, asset=10_300_000, kospi=3220.0, kospi_change=1.2
            ),
        ]

        rendered = render_combined_chart(runs)

        self.assertIn('class="series-line pnl-line"', rendered)
        self.assertEqual(rendered.count('class="series-point total-point"'), 3)
        self.assertEqual(rendered.count('class="series-point pnl-point"'), 3)
        self.assertEqual(rendered.count('class="series-point asset-point"'), 3)
        self.assertEqual(rendered.count('class="series-point kospi-point"'), 3)

    def test_combined_chart_omits_pnl_points_when_pnl_overlaps_total(self) -> None:
        runs = [
            self._combined_chart_run("2026-07-15T09:00:00+09:00", total=10_000_000, pnl=90_000),
            self._combined_chart_run("2026-07-15T09:30:00+09:00", total=10_050_000, pnl=95_000),
        ]

        rendered = render_combined_chart(runs)

        self.assertNotIn('class="series-line pnl-line"', rendered)
        self.assertNotIn('class="series-point pnl-point"', rendered)
        self.assertEqual(rendered.count('class="series-point total-point"'), 2)

    def test_combined_chart_skips_points_for_runs_missing_asset_or_kospi(self) -> None:
        runs = [
            self._combined_chart_run(
                "2026-07-15T09:00:00+09:00", total=10_000_000, pnl=100_000, asset=10_500_000, kospi=3200.0, kospi_change=1.0
            ),
            self._combined_chart_run(
                "2026-07-15T09:30:00+09:00", total=10_050_000, pnl=90_000, asset=None, kospi=3210.0, kospi_change=1.1
            ),
            self._combined_chart_run(
                "2026-07-15T10:00:00+09:00", total=10_100_000, pnl=80_000, asset=10_600_000, kospi=None, kospi_change=None
            ),
        ]

        rendered = render_combined_chart(runs)

        self.assertEqual(rendered.count('class="series-point total-point"'), 3)
        self.assertEqual(rendered.count('class="series-point asset-point"'), 2)
        self.assertEqual(rendered.count('class="series-point kospi-point"'), 2)


if __name__ == "__main__":
    raise SystemExit(self_test())
