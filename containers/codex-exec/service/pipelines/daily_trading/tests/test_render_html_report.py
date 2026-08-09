#!/usr/bin/env python3
"""Tests for the cumulative single-file daily-trading HTML report.

`self_test` is the compatibility body invoked by the production CLI's
`--self-test` command and must keep its exact contract: print a
`{"status": "success"}` or `{"status": "failed", ...}` JSON line and
return `0`/`1`. Its two scenarios (the three-run cumulative report, and
the no-KOSPI combined chart) now live in `scenario_*` helpers, and each
of their checks lives in its own `check_*` helper. `self_test` and the
granular `TestCase` methods below both call those same helpers, so each
behavior has exactly one implementation. The wrapper-orchestration test
mocks the helpers rather than re-rendering the report, so discovery
does not execute the real work twice. The large set of independent
granular tests that already existed below the umbrella is unchanged.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ..scripts.render_html_report import (
    CANONICAL_ACTION_LABELS,
    DECISION_BASIS_LABELS,
    NOT_RECORDED,
    analyst_symbol_group_priority,
    argument_anchor_id,
    blocked_attempt_badge_text,
    build_html,
    cumulative_today_fills,
    find_daily_history,
    find_runs,
    fresh_recheck_audit_summary,
    judge_field_display,
    order_direction_label,
    order_status_badge,
    parse_cited_argument_ids,
    render_combined_chart,
    render_chart_periods,
    render_header,
    render_market_and_quality,
    render_symbol_history_chart,
    render_thesis_section,
    render_time_symbol_inspector,
    render_trade_ledger,
    resolve_fill,
    valid_analyst_score,
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
            "symbol_news": {
                "status": "supplied",
                "cache_counts": {"usable_symbol_count": 2, "wanted_symbol_count": 2},
            },
            "market_news": {"status": "supplied", "article_count": 1},
            "investor_flow": {
                "status": "partial" if target else "supplied",
                "usable_symbol_count": 1 if target else 2,
                "display_text": "장중 수급: 1개 종목 추정치 반영, 일부 종목 수급 없음",
            },
        },
        "token_usage": {"total": {"total_tokens": 123}},
        "stages": [{"stage": "execution-plan", "status": "success", "detail": "status=success"}],
        "review_summary": {"symbol_count": 2},
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
        orders.append(
            {
                "symbol_id": "000002",
                "symbol_name": "베타소재",
                "direction": "buy",
                "quantity": 0,
                "order_price": 50_000,
                "order_or_reservation_id": "skipped-order-must-not-render",
                "result": "skipped",
                "reason": "final_equals_expected_holding_quantity",
            }
        )
    decision_symbols = [
        {
            "symbol_id": "000001",
            "symbol_name": "알파전자",
            "evidence_mode": "full",
            "eligible_for_review": True,
            "product_type": "stock",
            "price": {"current_or_last": 100_000, "observed_at": started_at},
            "account_exposure": {"current_live_holding_quantity": 2},
            "price_chart_signals": [{"name": "day_change_pct", "value": 1.25}],
            "chart_context": {
                "daily_summary": {
                    "latest_date": "20260715",
                    "latest_close": 100_000,
                    "change_1_period_pct": 1.25,
                    "change_5_period_pct": 3.5,
                    "distance_ma20_pct": 2.1,
                    "latest_volume": 12_345,
                }
            },
            "investor_flow_summary": {
                "foreign_net_buy_quantity": 120,
                "institution_net_buy_quantity": -20,
                "combined_net_buy_quantity": 100,
            },
            "orderbook_summary": {"best_ask": 100_100, "best_bid": 100_000, "spread_pct": 0.1, "depth_imbalance": 0.25},
            "trade_flow_summary": {"oldest_price": 99_900, "latest_price": 100_000, "recent_price_change_pct": 0.1, "tick_count": 10},
            "required_missing": [],
            "warnings": [],
            "errors": [],
            "financial_summary": {
                "cache_status": "hit",
                "quality_value_usable": True,
                "items": ["업종 반도체", "PER 12.3", "영업이익 증가"],
            },
            "symbol_news_summary": [
                {
                    "article_date": "2026-07-15 08:30",
                    "content": "신규 수주 공시",
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
            "symbol_news_summary": [
                {
                    "article_date": "2026-07-15 09:10",
                    "content": "원가 부담 확대",
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
        "canonical_action": "increase",
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
    if target:
        write_json(
            run_dir / "judge-review-spec.json",
            {"schema_version": "2", "review_scope_reasons": {"000001": "held_position", "000002": "held_position"}},
        )
    write_json(
        run_dir / "account-before-order.json",
        {
            "symbols": [
                {"symbol_id": "000001", "symbol_name": "알파전자", "current_live_holding_quantity": 2, "current_price": 100_000, "average_purchase_price": 90_000, "valuation_amount": 200_000, "pnl_amount": 20_000, "pnl_rate": 11.1},
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


REQUIRED_CUMULATIVE_REPORT_STRINGS = [
    'data-tab="symbol-detail"',
    'id="symbol-detail"',
    'class="symbol-detail-select"',
    'data-symbol-detail="000001"',
    "가격·보유수량 추이",
    'aria-label="종목 가격·보유수량 그래프 기간"',
    'class="symbol-price-line"',
    'class="symbol-quantity-line"',
    "당일 주문·체결",
    "계좌 전체 당일 체결 조회를 기준으로 표시합니다.",
    "현재 수집 정보",
    "외국인 120주",
    "당일 등락률",
    "최신 Analyst·Judge 판단",
    "행동 <strong>확대</strong>",
    'class="badge bad">KIS 거절 1주</span>',
    "selector.addEventListener('change', selectSymbol)",
    "10:00까지의 당일 전체 거래",
    "data-time-target=\"run-0-0900\"",
    "data-time-target=\"run-1-0900\"",
    "data-time-target=\"run-2-1000\"",
    "알파전자",
    "베타소재",
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
    "손실 보유 종목 감축 요건",
    "훼손 근거 검증 완료",
    "damaged_evidence_confirmed",
    "신규/후속 thesis",
    "재고 정상화 이후 신규 성장 사이클 진입",
    "demand-collapse",
    "투자 논지 (Thesis)",
    "외부종목",
    "계좌 전체 일별 체결 조회",
    "주문·체결 통합 원장",
    'class="time-wheel"',
    'role="group" aria-label="실행 회차"',
    "1회차 · 09:00",
    "2회차 · 09:00",
    "3회차 · 10:00",
    'class="symbol-button-meta"',
    "신규 수주 공시",
    "원가 부담 확대",
    "KOSPI 3210.50 (+1.25%)",
    "regimeLabel&quot;:&quot;강세",
    "regime&quot;:&quot;risk_on",
    "KIS 총자산 10,500,000원",
    'class="series-line pnl-line"',
    'class="series-line asset-line"',
    "asset&quot;:10500000",
    "'KIS 총자산 ' + Number(point.asset)",
    'data-chart-period-target="week"',
    'data-chart-period-target="month"',
    "최근 1주 계좌·시장 추이",
    "최근 1개월 계좌·시장 추이",
    "button.dataset.chartPeriodTarget",
    'class="trade-symbol-button group-trade active"',
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

FORBIDDEN_CUMULATIVE_REPORT_STRINGS = [
    "2026-07-14",
    "14:30",
    "초기 원금",
    "계좌 누적수익률",
    "https://",
    "innerHTML",
    "재무 수집 supplied",
    "확인된 체결 전체",
    "봇이 제출한 주문 전체",
    'class="sentiment',
    "sentiment positive",
    "sentiment negative",
    "score-low",
    "score-high",
    "scroll-snap-type:y mandatory",
    "selector.addEventListener('scroll'",
    "event.key === 'ArrowUp'",
    "시간 휠",
    "skipped-order-must-not-render",
]


def scenario_build_cumulative_report() -> tuple[str, Path, tempfile.TemporaryDirectory]:
    """Build a 3-run cumulative HTML report (2 baseline runs + 1 target run).

    Returns the temp dir handle too so callers control cleanup timing
    (the checks below need the directory to still exist).
    """
    temp_dir = tempfile.TemporaryDirectory()
    runs_root = Path(temp_dir.name) / "reports" / "runs"
    make_run(runs_root, "run-0900", "2026-07-15T09:00:00+09:00", target=False)
    make_run(runs_root, "run-0900-second", "2026-07-15T09:00:30+09:00", target=False)
    make_run(runs_root, "run-1000", "2026-07-15T10:00:00+09:00", target=True)
    rendered = build_html(runs_root, "run-1000")
    return rendered, runs_root, temp_dir


def check_cumulative_report_contains_required_strings(rendered: str) -> list[str]:
    return [value for value in REQUIRED_CUMULATIVE_REPORT_STRINGS if value not in rendered]


def check_cumulative_report_excludes_forbidden_strings(rendered: str, runs_root: Path) -> list[str]:
    forbidden = [*FORBIDDEN_CUMULATIVE_REPORT_STRINGS, str(runs_root)]
    return [value for value in forbidden if value in rendered]


def check_cumulative_report_orders_analyst_symbols_by_group(rendered: str) -> bool:
    # 알파전자 has a submitted trade this run (operational priority 0); 베타소재 has neither a
    # trade nor a Judge decision (priority 2, unresolved) — trade must sort ahead of unresolved,
    # replacing the removed low-score-first ordering.
    selector_start = rendered.rfind("전체 Analyst 대상 종목")
    return rendered.find("알파전자", selector_start) < rendered.find("베타소재", selector_start)


def scenario_render_combined_chart_without_kospi() -> str:
    return render_combined_chart(
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


def check_combined_chart_falls_back_when_kospi_and_asset_missing(no_kospi_chart: str) -> bool:
    return (
        "10,100,000" in no_kospi_chart
        and "KOSPI 조회 실패" in no_kospi_chart
        and "KIS 총자산 조회 실패" in no_kospi_chart
        and 'class="series-line asset-line"' not in no_kospi_chart
    )


def self_test() -> int:
    rendered, runs_root, temp_dir = scenario_build_cumulative_report()
    try:
        missing = check_cumulative_report_contains_required_strings(rendered)
        present = check_cumulative_report_excludes_forbidden_strings(rendered, runs_root)
        score_order_ok = check_cumulative_report_orders_analyst_symbols_by_group(rendered)
    finally:
        temp_dir.cleanup()

    no_kospi_chart = scenario_render_combined_chart_without_kospi()
    no_kospi_ok = check_combined_chart_falls_back_when_kospi_and_asset_missing(no_kospi_chart)

    if missing or present or not score_order_ok or not no_kospi_ok:
        print(json.dumps({"status": "failed", "missing": missing, "forbidden_present": present, "score_order_ok": score_order_ok, "no_kospi_ok": no_kospi_ok}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "success"}, ensure_ascii=False))
    return 0


class RenderHtmlReportSelfTest(unittest.TestCase):
    def test_self_test_suite_runs_every_check_and_reports_success(self) -> None:
        """Wrapper-orchestration check only: real behavior is covered by the
        granular tests below, so this mocks every helper instead of
        re-rendering the cumulative report and combined chart a second
        time."""
        with patch(f"{__name__}.scenario_build_cumulative_report") as scenario, patch(
            f"{__name__}.check_cumulative_report_contains_required_strings", return_value=[]
        ) as check_missing, patch(
            f"{__name__}.check_cumulative_report_excludes_forbidden_strings", return_value=[]
        ) as check_forbidden, patch(
            f"{__name__}.check_cumulative_report_orders_analyst_symbols_by_group", return_value=True
        ) as check_order, patch(
            f"{__name__}.scenario_render_combined_chart_without_kospi", return_value=""
        ) as chart_scenario, patch(
            f"{__name__}.check_combined_chart_falls_back_when_kospi_and_asset_missing", return_value=True
        ) as check_chart:
            temp_dir = tempfile.TemporaryDirectory()
            self.addCleanup(temp_dir.cleanup)
            scenario.return_value = ("<html></html>", Path(temp_dir.name), temp_dir)

            result = self_test()

        self.assertEqual(result, 0)
        scenario.assert_called_once_with()
        check_missing.assert_called_once()
        check_forbidden.assert_called_once()
        check_order.assert_called_once()
        chart_scenario.assert_called_once_with()
        check_chart.assert_called_once()

    def test_cumulative_report_contains_required_strings(self) -> None:
        rendered, runs_root, temp_dir = scenario_build_cumulative_report()
        self.addCleanup(temp_dir.cleanup)
        missing = check_cumulative_report_contains_required_strings(rendered)
        self.assertEqual(missing, [])

    def test_cumulative_report_shows_run_activity_before_the_day_ledger(self) -> None:
        rendered, _runs_root, temp_dir = scenario_build_cumulative_report()
        self.addCleanup(temp_dir.cleanup)
        self.assertLess(rendered.index('id="trade-symbol-analysis"'), rendered.index('id="trades"'))

    def test_cumulative_report_puts_day_ledger_in_a_time_grouped_tab(self) -> None:
        rendered, _runs_root, temp_dir = scenario_build_cumulative_report()
        self.addCleanup(temp_dir.cleanup)
        trading_start = rendered.index('<section class="tab-page" id="trading"')
        ledger_start = rendered.index('<section class="tab-page" id="day-ledger"')
        evidence_start = rendered.index('<section class="tab-page" id="evidence"')

        self.assertIn('data-tab="day-ledger"', rendered)
        self.assertNotIn("DAY LEDGER", rendered[trading_start:ledger_start])
        self.assertIn("DAY LEDGER", rendered[ledger_start:evidence_start])
        self.assertIn('<div class="ledger-hours">', rendered[ledger_start:evidence_start])
        self.assertIn("09시대", rendered[ledger_start:evidence_start])
        self.assertIn("10시대", rendered[ledger_start:evidence_start])

    def test_cumulative_report_prominently_shows_judge_route_and_score_per_symbol(self) -> None:
        rendered, _runs_root, temp_dir = scenario_build_cumulative_report()
        self.addCleanup(temp_dir.cleanup)
        symbol_detail_start = rendered.index('<section class="tab-page" id="symbol-detail"')
        trading_start = rendered.index('<section class="tab-page" id="trading"')
        ledger_start = rendered.index('<section class="tab-page" id="day-ledger"')

        for page in (rendered[symbol_detail_start:trading_start], rendered[trading_start:ledger_start]):
            self.assertIn('class="judge-route-summary"', page)
            self.assertIn("Analyst 집계점수", page)
            self.assertIn("7.5 / 10", page)
            self.assertIn("Judge 진입 사유", page)
            self.assertIn("보유 종목", page)
            self.assertIn("<code>held_position</code>", page)
        self.assertIn('mini-badge analyst">Analyst 7.5', rendered[trading_start:ledger_start])
        symbol_page = rendered[symbol_detail_start:trading_start]
        external_start = symbol_page.index('data-symbol-detail="999999"')
        external_end = symbol_page.find('<article class="symbol-detail-panel"', external_start + 1)
        external_panel = symbol_page[external_start : external_end if external_end >= 0 else len(symbol_page)]
        self.assertIn("Analyst 결과 없음", external_panel)
        self.assertNotIn("Judge 진입 사유", external_panel)

    def test_cumulative_report_promotes_thesis_before_its_final_judge_card(self) -> None:
        rendered, _runs_root, temp_dir = scenario_build_cumulative_report()
        self.addCleanup(temp_dir.cleanup)
        thesis_index = rendered.find("투자 논지 (Thesis)")
        target_final_judge_index = rendered.find("Final Judge", thesis_index)

        self.assertGreaterEqual(thesis_index, 0)
        self.assertGreater(target_final_judge_index, thesis_index)
        self.assertIn('<span class="badge bad">Thesis 훼손</span>', rendered)

    def test_cumulative_report_excludes_forbidden_strings(self) -> None:
        rendered, runs_root, temp_dir = scenario_build_cumulative_report()
        self.addCleanup(temp_dir.cleanup)
        present = check_cumulative_report_excludes_forbidden_strings(rendered, runs_root)
        self.assertEqual(present, [])

    def test_cumulative_report_orders_analyst_symbols_by_score(self) -> None:
        rendered, _runs_root, temp_dir = scenario_build_cumulative_report()
        self.addCleanup(temp_dir.cleanup)
        self.assertTrue(check_cumulative_report_orders_analyst_symbols_by_group(rendered))

    def test_combined_chart_falls_back_when_kospi_and_asset_missing(self) -> None:
        no_kospi_chart = scenario_render_combined_chart_without_kospi()
        self.assertTrue(check_combined_chart_falls_back_when_kospi_and_asset_missing(no_kospi_chart))

    def test_cumulative_report_hides_touch_overlays_and_styles_the_scrubber(self) -> None:
        rendered, _runs_root, temp_dir = scenario_build_cumulative_report()
        self.addCleanup(temp_dir.cleanup)

        self.assertIn('class="chart-scrubber-copy"', rendered)
        self.assertIn(".chart-range-slider::-webkit-slider-thumb", rendered)
        self.assertIn("if (event.pointerType !== 'mouse') hidePoint();", rendered)
        self.assertIn("slider.addEventListener('pointerup', hidePoint);", rendered)
        self.assertIn("if (event.pointerType !== 'mouse') hideSlice();", rendered)
        self.assertIn("if (slice.matches(':focus-visible')) showSlice(slice);", rendered)

    def test_symbol_history_chart_renders_price_and_step_holding_series(self) -> None:
        rendered = render_symbol_history_chart(
            [
                {"label": "09:00", "price": 100_000, "quantity": 2},
                {"label": "10:00", "price": 105_000, "quantity": 3},
            ],
            "intraday",
        )

        self.assertIn('aria-label="당일 가격과 보유수량 추이"', rendered)
        self.assertIn('class="symbol-price-line"', rendered)
        self.assertIn('class="symbol-quantity-line"', rendered)
        self.assertIn("가격 100,000~105,000원", rendered)
        self.assertIn("보유 2~3주", rendered)
        self.assertIn("09:00 가격 100,000원", rendered)
        self.assertIn("10:00 보유 3주", rendered)

    def test_symbol_detail_does_not_claim_no_trades_when_fill_scope_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root = Path(temporary) / "runs"
            run_dir = make_run(runs_root, "run-1000", "2026-07-15T10:00:00+09:00", target=True)
            write_json(run_dir / "execution.json", {"status": "success", "request_type": "real-submit", "orders": []})
            write_json(
                run_dir / "today-fills.json",
                {"status": "unavailable", "skipped": False, "fill_scope": "universe", "fills": []},
            )

            rendered = build_html(runs_root, "run-1000")

        self.assertIn(
            "체결 수집 상태 unavailable, 범위 universe이므로 확인된 주문·체결만 표시합니다.",
            rendered,
        )
        self.assertIn('<td colspan="7">확인된 당일 주문·체결 없음</td>', rendered)

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
                        "symbol_news": {"status": "supplied", "display_text": "legacy stale text", "cache_counts": {"usable_symbol_count": 2, "wanted_symbol_count": 2}},
                        "market_news": {"status": "supplied", "display_text": "legacy stale market text", "article_count": 3},
                        "investor_flow": {"status": "supplied", "display_text": "legacy stale text"},
                    },
                    "reporting_view": {
                        "evidence_domains": {
                            "financial": {"status": "supplied", "coverage_text": "재무: 2개 종목 반영", "usable_symbol_count": 2, "wanted_symbol_count": 2},
                            "symbol_news": {"status": "cache_missing", "coverage_text": "종목뉴스: 캐시 파일 없음", "usable_symbol_count": 0, "wanted_symbol_count": 2},
                            "market_news": {"status": "partial", "coverage_text": "시장뉴스: 2건 반영, 일부 수집원 실패", "usable_item_count": 2},
                            "investor_flow": {"status": "partial", "coverage_text": "장중 수급: 1개 종목 추정치 반영, 일부 종목 수급 없음", "usable_symbol_count": 1, "wanted_symbol_count": 2},
                        }
                    },
                    "stages": [],
                },
            )

        self.assertIn("종목뉴스 수집 cache_missing", rendered)
        self.assertIn("종목뉴스: 캐시 파일 없음", rendered)
        self.assertIn("시장뉴스 수집 partial", rendered)
        self.assertIn("시장뉴스 기사</span><strong>2건", rendered)
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

    def test_analyst_symbol_group_priority_ranks_trade_over_guard_over_unresolved(self) -> None:
        final_by_symbol = {
            "000010": {"decision_guard": {"status": "blocked"}},
        }
        # A symbol with an actual trade outranks a symbol whose Judge decision was guard-blocked,
        # which in turn outranks a symbol Judge never resolved (no final_by_symbol entry) — this
        # replaces the removed low-score-first ordering with operationally meaningful grouping.
        self.assertEqual(
            analyst_symbol_group_priority("000020", final_by_symbol, {"000020"}, set()),
            0,
        )
        self.assertEqual(
            analyst_symbol_group_priority("000010", final_by_symbol, set(), set()),
            1,
        )
        self.assertEqual(
            analyst_symbol_group_priority("000030", final_by_symbol, set(), set()),
            2,
        )

    def test_order_direction_label_never_shows_lifecycle_only_submission_as_sell(self) -> None:
        # direction=none + reason=active_order_cancel_submitted is a cancellation of an existing
        # order, not a new sell order; it must never be mislabeled as 매도 just because
        # direction != "buy".
        cancel_only = {"direction": "none", "reason": "active_order_cancel_submitted", "result": "submitted"}
        self.assertEqual(order_direction_label(cancel_only), "취소")

        correction_only = {"direction": "none", "reason": "active_order_correction_submitted", "result": "submitted"}
        self.assertEqual(order_direction_label(correction_only), "정정")

        real_sell = {"direction": "sell", "reason": "accepted", "result": "submitted"}
        self.assertEqual(order_direction_label(real_sell), "매도")

        blocked_none_direction = {"direction": "none", "reason": "invalid_final_holding_quantity", "result": "blocked"}
        self.assertEqual(order_direction_label(blocked_none_direction), "-")

        # A REAL correction row carries the corrected order's actual buy/sell direction (not
        # direction=none), so checking direction before the lifecycle reason would still mislabel
        # it as an ordinary 매수/매도 instead of 정정.
        real_correction_with_buy_direction = {
            "direction": "buy",
            "reason": "active_order_correction_submitted",
            "result": "submitted",
        }
        self.assertEqual(order_direction_label(real_correction_with_buy_direction), "정정")

        legacy_cancel_and_replacement = {
            "direction": "sell",
            "reason": "active_order_cancel_and_replacement_submitted",
            "result": "submitted",
        }
        self.assertEqual(order_direction_label(legacy_cancel_and_replacement), "정정")

    def test_blocked_attempt_badge_text_keeps_direction_and_outcome(self) -> None:
        self.assertEqual(
            blocked_attempt_badge_text([{"direction": "buy", "result": "blocked"}]),
            "매수 시도 차단",
        )
        self.assertEqual(
            blocked_attempt_badge_text([{"direction": "sell", "result": "failed"}]),
            "매도 시도 실패",
        )
        self.assertEqual(
            blocked_attempt_badge_text(
                [
                    {"direction": "buy", "result": "blocked"},
                    {"direction": "sell", "result": "failed"},
                ]
            ),
            "매수·매도 시도 차단/실패",
        )

    def test_judge_field_display_never_fabricates_none_or_hold_for_missing_v1_fields(self) -> None:
        # v1 judge-review.json artifacts never had decision_basis/requested_action/canonical_action
        # at all; defaulting a missing key to "none"/"hold" would fabricate a mechanical decision
        # that was never actually made.
        v1_final_item = {"symbol_id": "005930", "final_holding_quantity": 3}
        self.assertEqual(judge_field_display(v1_final_item, "decision_basis", DECISION_BASIS_LABELS), NOT_RECORDED)
        self.assertEqual(judge_field_display(v1_final_item, "canonical_action", CANONICAL_ACTION_LABELS), NOT_RECORDED)

        v2_final_item = {"decision_basis": "profit_protection", "canonical_action": "reduce"}
        self.assertEqual(judge_field_display(v2_final_item, "decision_basis", DECISION_BASIS_LABELS), "이익보호")
        self.assertEqual(judge_field_display(v2_final_item, "canonical_action", CANONICAL_ACTION_LABELS), "축소")

        # An explicit canonical_action="hold" IS a real recorded decision and must render as such,
        # not as NOT_RECORDED -- only an absent key is unavailable.
        explicit_hold_item = {"canonical_action": "hold"}
        self.assertEqual(judge_field_display(explicit_hold_item, "canonical_action", CANONICAL_ACTION_LABELS), "유지")

    def test_render_policy_panel_shows_effective_guard_values(self) -> None:
        from ..scripts.render_html_report import render_policy_panel

        rendered = render_policy_panel(
            {
                "execution_guards_policy": {
                    "unheld_review_top_k": 5,
                    "profit_protection_max_reduction_pct": 25.0,
                    "concentration_rebalance_cap_pct": 15.0,
                    "concentration_rebalance_max_reduction_pct": 30.0,
                    "max_daily_turnover_pct": 20.0,
                }
            }
        )
        self.assertIn("적용 중인 실행 정책", rendered)
        self.assertIn("25.0%", rendered)
        self.assertIn("15.0%", rendered)
        self.assertEqual(render_policy_panel({}), "")

    def test_valid_analyst_score_matches_pipeline_not_selected_count_contract(self) -> None:
        self.assertTrue(valid_analyst_score(0))
        self.assertTrue(valid_analyst_score("10"))
        self.assertFalse(valid_analyst_score(None))
        self.assertFalse(valid_analyst_score(True))
        self.assertFalse(valid_analyst_score("not-a-score"))
        self.assertFalse(valid_analyst_score(10.1))

    def test_judge_symbol_scope_status_distinguishes_resolved_unresolved_not_selected_and_legacy(self) -> None:
        from ..scripts.render_html_report import judge_symbol_scope_status

        final_by_symbol = {"000001": {"final_holding_quantity": 5}}
        scope_reasons = {"000002": "unheld_score_rank"}
        # Resolved: judge-review.json has a row for this symbol.
        self.assertEqual(
            judge_symbol_scope_status("000001", final_by_symbol.get("000001"), scope_reasons, True),
            "resolved",
        )
        # In review_scope_reasons but no judge-review.json row -- unresolved-in-scope, not "not
        # selected" and not "Analyst only".
        self.assertEqual(
            judge_symbol_scope_status("000002", final_by_symbol.get("000002"), scope_reasons, True),
            "unresolved_in_scope",
        )
        # Never in review_scope_reasons at all, with valid v2 scope metadata present -- genuinely
        # not selected.
        self.assertEqual(
            judge_symbol_scope_status("000003", final_by_symbol.get("000003"), scope_reasons, True),
            "not_selected",
        )
        # v1 run: no scope metadata exists at all, so "not selected" would be fabricated precision.
        self.assertEqual(
            judge_symbol_scope_status("000003", final_by_symbol.get("000003"), scope_reasons, False),
            "legacy_unknown",
        )

    def test_inspector_treats_existing_v1_spec_without_scope_key_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            write_json(
                run_dir / "analyst-review.json",
                {"symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "final_first_score": 5, "agent_scores": []}]},
            )
            write_json(run_dir / "judge-review.json", {"symbols": []})
            write_json(run_dir / "judge-review-spec.json", {"schema_version": "1", "symbol_ids": ["005930"]})
            write_json(run_dir / "judge-debate.json", {})
            rendered = render_time_symbol_inspector(
                [
                    {
                        "path": run_dir,
                        "summary": {"started_at": "2026-07-27T10:00:00+09:00", "review_summary": {"symbols": []}},
                        "execution": {"orders": []},
                        "decision": {},
                    }
                ],
                [],
            )

        self.assertIn("Judge 상태 확인불가(구버전)", rendered)
        self.assertIn("사유 미기록(구버전)", rendered)
        self.assertNotIn(">Judge 미선정<", rendered)

    def test_inspector_not_selected_count_uses_only_valid_analyst_scores(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            write_json(
                run_dir / "analyst-review.json",
                {
                    "symbols": [
                        {"symbol_id": "000001", "symbol_name": "유효", "final_first_score": 5, "agent_scores": []},
                        {"symbol_id": "000002", "symbol_name": "누락", "final_first_score": None, "agent_scores": []},
                        {"symbol_id": "000003", "symbol_name": "범위밖", "final_first_score": 11, "agent_scores": []},
                    ]
                },
            )
            write_json(run_dir / "judge-review.json", {"symbols": []})
            write_json(run_dir / "judge-review-spec.json", {"schema_version": "2", "review_scope_reasons": {}})
            write_json(run_dir / "judge-debate.json", {})
            rendered = render_time_symbol_inspector(
                [
                    {
                        "path": run_dir,
                        "summary": {"started_at": "2026-07-27T10:00:00+09:00", "review_summary": {"symbols": []}},
                        "execution": {"orders": []},
                        "decision": {},
                    }
                ],
                [],
            )

        self.assertIn("Judge 심사범위: 보유 0 · 비보유 심사대상 0 · 미선정 1 · 미해결 0", rendered)

    def test_inspector_uses_review_summary_expected_holding_and_labels_final_as_quantity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            write_json(
                run_dir / "analyst-review.json",
                {"symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "final_first_score": 5, "agent_scores": []}]},
            )
            write_json(
                run_dir / "judge-review.json",
                {
                    "symbols": [
                        {
                            "symbol_id": "005930",
                            "symbol_name": "삼성전자",
                            "final_holding_quantity": 5,
                            "requested_target_position_value_krw": 500_000,
                            "target_position_value_krw": 500_000,
                            "requested_action": "hold",
                            "canonical_action": "hold",
                            "decision_basis": "none",
                            "decision_guard": {"status": "allowed", "reason_code": "within_daily_turnover_budget"},
                        }
                    ]
                },
            )
            write_json(
                run_dir / "judge-review-spec.json",
                {"schema_version": "2", "review_scope_reasons": {"005930": "held_position"}},
            )
            write_json(run_dir / "judge-debate.json", {})
            rendered = render_time_symbol_inspector(
                [
                    {
                        "path": run_dir,
                        "summary": {
                            "started_at": "2026-07-27T10:00:00+09:00",
                            "review_summary": {"symbols": [{"symbol_id": "005930", "expected_holding_quantity": 5}]},
                        },
                        "execution": {"orders": []},
                        "decision": {
                            "symbols": [
                                {"symbol_id": "005930", "account_exposure": {"current_live_holding_quantity": 3}}
                            ]
                        },
                    }
                ],
                [],
            )

        self.assertIn("현재 3주 → 대기반영 5주", rendered)
        self.assertIn("최종 보유수량 5주", rendered)
        self.assertIn("Analyst 집계점수", rendered)
        self.assertIn("5.0 / 10", rendered)
        self.assertIn("Judge 진입 사유", rendered)
        self.assertIn("보유 종목", rendered)
        self.assertIn("<code>held_position</code>", rendered)
        self.assertNotIn("최종(포지션 변화) 5주", rendered)

    def test_inspector_keeps_holding_status_in_detail_without_expanding_symbol_choices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            write_json(
                run_dir / "analyst-review.json",
                {
                    "symbols": [
                        {"symbol_id": "005930", "symbol_name": "삼성전자", "final_first_score": 5, "agent_scores": []},
                        {"symbol_id": "000660", "symbol_name": "SK하이닉스", "final_first_score": 4, "agent_scores": []},
                        {"symbol_id": "035420", "symbol_name": "NAVER", "final_first_score": 3, "agent_scores": []},
                    ]
                },
            )
            write_json(run_dir / "judge-review.json", {"symbols": []})
            write_json(run_dir / "judge-review-spec.json", {"schema_version": "2", "review_scope_reasons": {}})
            write_json(run_dir / "judge-debate.json", {})
            rendered = render_time_symbol_inspector(
                [
                    {
                        "path": run_dir,
                        "summary": {"started_at": "2026-07-27T10:00:00+09:00", "review_summary": {"symbols": []}},
                        "execution": {"orders": []},
                        "decision": {
                            "symbols": [
                                {"symbol_id": "005930", "account_exposure": {"current_live_holding_quantity": 3}},
                                {"symbol_id": "000660", "account_exposure": {"current_live_holding_quantity": 0}},
                                {"symbol_id": "035420", "account_exposure": {}},
                            ]
                        },
                    }
                ],
                [],
            )

        self.assertNotIn('class="mini-badge held"', rendered)
        self.assertNotIn('class="mini-badge unheld"', rendered)
        self.assertNotIn('class="mini-badge unknown"', rendered)
        self.assertIn('<span class="badge ok">보유 3주</span>', rendered)
        self.assertIn('<span class="badge muted">비보유</span>', rendered)
        self.assertIn('<span class="badge muted">보유 미기록</span>', rendered)

    def test_inspector_does_not_badge_lifecycle_only_cancellation_as_trade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            write_json(
                run_dir / "analyst-review.json",
                {"symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "final_first_score": 5, "agent_scores": []}]},
            )
            write_json(run_dir / "judge-review.json", {"symbols": []})
            write_json(run_dir / "judge-review-spec.json", {"schema_version": "2", "review_scope_reasons": {}})
            write_json(run_dir / "judge-debate.json", {})
            rendered = render_time_symbol_inspector(
                [
                    {
                        "path": run_dir,
                        "summary": {"started_at": "2026-07-27T10:00:00+09:00", "review_summary": {"symbols": []}},
                        "execution": {
                            "orders": [
                                {
                                    "symbol_id": "005930",
                                    "symbol_name": "삼성전자",
                                    "direction": "none",
                                    "validated_order_quantity": 0,
                                    "result": "submitted",
                                    "reason": "active_order_cancel_submitted",
                                }
                            ]
                        },
                        "decision": {},
                    }
                ],
                [],
            )

        self.assertIn("주문 정정·취소", rendered)
        self.assertIn("기존주문 취소", rendered)
        self.assertNotIn('mini-badge trade">거래', rendered)
        self.assertNotIn("취소 0주", rendered)

    def test_fresh_recheck_audit_summary_renders_for_a_successful_entry_too(self) -> None:
        order = {
            "fresh_recheck_audit": [
                {
                    "checked_at": "2026-07-27T09:10:00+09:00",
                    "fresh_holding_quantity": 10,
                    "pnl_verification_outcome": True,
                    "reduction_bound_outcome": True,
                    "approved_max_reduction_pct": 25.0,
                }
            ]
        }
        rendered = fresh_recheck_audit_summary(order)
        self.assertIn("보유 10주", rendered)
        self.assertIn("손익검증 통과", rendered)
        self.assertIn("축소한도 통과", rendered)
        self.assertIn("25.0%", rendered)

    def test_trade_ledger_shows_fresh_recheck_audit_on_submitted_order(self) -> None:
        rendered, submitted_orders = render_trade_ledger(
            [
                {
                    "summary": {"started_at": "2026-07-27T10:00:00+09:00"},
                    "execution": {
                        "orders": [
                            {
                                "symbol_id": "005930",
                                "symbol_name": "삼성전자",
                                "direction": "sell",
                                "validated_order_quantity": 1,
                                "order_price": 70_000,
                                "result": "submitted",
                                "fresh_recheck_audit": [
                                    {
                                        "checked_at": "2026-07-27T10:00:01+09:00",
                                        "fresh_holding_quantity": 10,
                                        "pnl_verification_outcome": True,
                                        "reduction_bound_outcome": True,
                                        "approved_max_reduction_pct": 25.0,
                                    }
                                ],
                            }
                        ]
                    },
                }
            ],
            [],
            "success",
            "account",
        )

        self.assertEqual(len(submitted_orders), 1)
        self.assertIn("재확인 감사:", rendered)
        self.assertIn("손익검증 통과", rendered)

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
        self.assertIn('class="thesis-card thesis-bad"', rendered)
        self.assertIn("투자 논지 (Thesis)", rendered)
        self.assertIn('<span class="badge bad">Thesis 훼손</span>', rendered)
        self.assertIn("이전 thesis", rendered)
        self.assertIn("prior-run-&lt;xss&gt;", rendered)
        self.assertNotIn("<xss>", rendered)
        self.assertIn("2026-06-01 09:00", rendered)
        self.assertIn("judge-review.json", rendered)
        self.assertIn("quality moat &amp; pricing power", rendered)
        self.assertIn("margin-compression", rendered)
        self.assertIn("demand-collapse", rendered)
        self.assertIn("내부 비교 키", rendered)
        self.assertIn("gross margin drops below prior guidance", rendered)
        self.assertLess(
            rendered.find("gross margin drops below prior guidance"),
            rendered.find("margin-compression"),
        )
        self.assertIn("훼손 근거 일치", rendered)
        self.assertIn("훼손", rendered)
        self.assertIn("005930-bear-opening-1", rendered)
        self.assertIn("요건 충족", rendered)
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
        self.assertIn("요건 미충족", rendered)
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

    def test_daily_history_uses_latest_chartable_run_per_date_within_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root = Path(temporary)

            def add_run(run_id: str, started_at: str, total: int | None) -> None:
                account = (
                    {"total_evaluation_amount": total, "total_pnl_amount": total // 100}
                    if total is not None
                    else {}
                )
                write_json(
                    runs_root / run_id / "pipeline-summary.json",
                    {
                        "run_id": run_id,
                        "started_at": started_at,
                        "account_display_summary": account,
                    },
                )

            add_run("outside-month", "2026-06-27T15:00:00+09:00", 1_000)
            add_run("day-old", "2026-07-21T09:00:00+09:00", 2_000)
            add_run("day-latest", "2026-07-21T15:00:00+09:00", 2_100)
            add_run("invalid-latest", "2026-07-22T15:00:00+09:00", None)
            add_run("target", "2026-07-27T11:00:00+09:00", 3_000)
            add_run("after-target", "2026-07-27T13:00:00+09:00", 3_100)

            history = find_daily_history(
                runs_root,
                "2026-07-27T12:00:00+09:00",
                calendar_days=30,
            )

        self.assertEqual(
            [item["summary"]["run_id"] for item in history],
            ["day-latest", "target"],
        )

    def test_chart_periods_offer_intraday_week_and_month_views(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root = Path(temporary)
            for run_id, started_at, total in (
                ("week-start", "2026-07-21T15:00:00+09:00", 10_000),
                ("target", "2026-07-27T11:00:00+09:00", 10_500),
            ):
                write_json(
                    runs_root / run_id / "pipeline-summary.json",
                    {
                        "run_id": run_id,
                        "started_at": started_at,
                        "account_display_summary": {
                            "total_evaluation_amount": total,
                            "total_pnl_amount": total // 100,
                        },
                    },
                )
            intraday_runs = [
                self._combined_chart_run(
                    "2026-07-27T11:00:00+09:00",
                    total=10_500,
                    pnl=105,
                )
            ]

            rendered = render_chart_periods(
                runs_root,
                "2026-07-27T11:00:00+09:00",
                intraday_runs,
            )

        self.assertIn('data-chart-period-target="intraday"', rendered)
        self.assertIn('data-chart-period-target="week"', rendered)
        self.assertIn('data-chart-period-target="month"', rendered)
        self.assertIn("최근 1주 계좌·시장 추이", rendered)
        self.assertIn("최근 1개월 계좌·시장 추이", rendered)
        self.assertIn(">07-21<", rendered)
        self.assertEqual(rendered.count('class="chart-period-panel active"'), 1)

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
            self._combined_chart_run("2026-07-15T09:30:00+09:00", total=10_050_000, pnl=140_000),
        ]

        rendered = render_combined_chart(runs)

        self.assertNotIn('class="series-line pnl-line"', rendered)
        self.assertNotIn('class="series-point pnl-point"', rendered)
        self.assertEqual(rendered.count('class="series-point total-point"'), 2)

    def test_combined_chart_uses_one_relative_change_axis_without_forcing_endpoints_together(self) -> None:
        runs = [
            self._combined_chart_run(
                "2026-07-15T09:00:00+09:00",
                total=100,
                pnl=0,
                asset=200,
                kospi=1000,
                kospi_change=0,
            ),
            self._combined_chart_run(
                "2026-07-15T09:30:00+09:00",
                total=90,
                pnl=-5,
                asset=198,
                kospi=990,
                kospi_change=-1,
            ),
        ]

        rendered = render_combined_chart(runs)

        self.assertIn("첫 관측값을 0%로 둔 상대 변화율을 하나의 공통 축", rendered)
        self.assertIn('points="58.00,34.00 1072.00,320.00" class="series-line total-line"', rendered)
        self.assertIn('points="58.00,34.00 1072.00,177.00" class="series-line pnl-line"', rendered)
        self.assertIn('points="58.00,34.00 1072.00,62.60" class="series-line asset-line"', rendered)
        self.assertIn('points="58.00,34.00 1072.00,62.60" class="series-line kospi-line"', rendered)
        self.assertIn('class="chart-zero"', rendered)

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

    def test_trade_ledger_shows_blocked_buy_attempt_without_counting_as_submitted(self) -> None:
        rendered, submitted_orders = render_trade_ledger(
            [
                {
                    "summary": {"started_at": "2026-07-20T13:00:48+09:00"},
                    "execution": {
                        "orders": [
                            {
                                "symbol_id": "078930",
                                "symbol_name": "GS",
                                "direction": "buy",
                                "order_price": 84_300,
                                "requested_order_quantity": 1,
                                "validated_order_quantity": 1,
                                "order_or_reservation_id": "",
                                "reason": "buy_quantity_exceeds_order_available_quantity",
                                "result": "blocked",
                                "attempts": [
                                    {
                                        "api_name": "inquire_psbl_order",
                                        "attempt": 1,
                                        "error_code": "cash_gate",
                                        "message": "max_buy_qty=0",
                                        "result": "blocked",
                                    }
                                ],
                            }
                        ]
                    },
                }
            ],
            [],
            "success",
            "account",
        )

        self.assertEqual(submitted_orders, [])
        self.assertIn("GS", rendered)
        self.assertIn("매수", rendered)
        self.assertIn("1주 요청", rendered)
        self.assertIn("매수가능수량 초과", rendered)
        self.assertIn("max_buy_qty=0", rendered)
        self.assertIn("차단", rendered)
        self.assertNotIn("체결 확인", rendered)

    def test_time_symbol_inspector_shows_blocked_sell_attempt_as_preferred_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-1"
            write_json(
                run_dir / "analyst-review.json",
                {
                    "symbols": [
                        {"symbol_id": "005930", "symbol_name": "삼성전자", "final_first_score": 5.0}
                    ]
                },
            )
            run = {
                "path": run_dir,
                "summary": {"started_at": "2026-07-20T14:00:49+09:00"},
                "execution": {
                    "orders": [
                        {
                            "symbol_id": "005930",
                            "symbol_name": "삼성전자",
                            "direction": "sell",
                            "order_price": 70_000,
                            "requested_order_quantity": 3,
                            "validated_order_quantity": 3,
                            "order_or_reservation_id": "",
                            "reason": "sell_quantity_exceeds_order_available_quantity",
                            "result": "blocked",
                            "attempts": [
                                {"message": "max_sell_qty=0", "result": "blocked"},
                            ],
                        }
                    ]
                },
                "decision": {},
            }

            rendered = render_time_symbol_inspector([run], [])

        self.assertIn("삼성전자", rendered)
        self.assertIn("매도", rendered)
        self.assertIn("3주 요청", rendered)
        self.assertIn("매도가능수량 초과", rendered)
        self.assertIn("max_sell_qty=0", rendered)
        self.assertIn('class="mini-badge attempt">매도 시도 차단</b>', rendered)
        self.assertIn('active" data-symbol-target="run-0-1400-005930"', rendered)

    def _write_debate_and_judge_run(
        self, run_dir: Path, *, one_line_reason: str, extra_bear_argument: bool = True
    ) -> dict:
        write_json(
            run_dir / "analyst-review.json",
            {"symbols": [{"symbol_id": "000001", "symbol_name": "알파전자", "final_first_score": 5.0}]},
        )
        bear_arguments = [
            {"argument_id": "000001-bear-opening-1", "kind": "claim", "statement": "밸류에이션 부담", "evidence_refs": [], "targets": []},
        ]
        if extra_bear_argument:
            bear_arguments.append(
                {"argument_id": "000001-bear-opening-2", "kind": "claim", "statement": "수급 악화", "evidence_refs": [], "targets": []}
            )
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
                                            "arguments": [
                                                {"argument_id": "000001-bull-opening-1", "kind": "claim", "statement": "성장 모멘텀 강함", "evidence_refs": [], "targets": []}
                                            ],
                                            "concessions": [],
                                            "unresolved_conflicts": [],
                                            "final_position": "매수 우위",
                                            "recommended_action": "buy",
                                            "target_holding_quantity": 1,
                                        }
                                    ]
                                }
                            },
                            "bear": {
                                "output": {
                                    "symbols": [
                                        {
                                            "symbol_id": "000001",
                                            "symbol_name": "알파전자",
                                            "arguments": bear_arguments,
                                            "concessions": [],
                                            "unresolved_conflicts": [],
                                            "final_position": "매도 우위",
                                            "recommended_action": "hold",
                                            "target_holding_quantity": 0,
                                        }
                                    ]
                                }
                            },
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
                        "final_holding_quantity": 1,
                        "target_position_value_krw": 100_000,
                        "relative_attractiveness_rank": 1,
                        "reason_code": "buy",
                        "one_line_reason": one_line_reason,
                    }
                ]
            },
        )
        return {
            "path": run_dir,
            "summary": {"started_at": "2026-07-20T14:00:49+09:00"},
            "execution": {"orders": []},
            "decision": {},
        }

    def test_time_symbol_inspector_shows_final_judge_before_phase_details(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-1"
            run = self._write_debate_and_judge_run(
                run_dir, one_line_reason="000001-bull-opening-1의 근거로 매수한다."
            )
            rendered = render_time_symbol_inspector([run], [])

        final_index = rendered.index('<article class="final-card full">')
        phase_index = rendered.index('class="phase compact-phase"')
        self.assertLess(final_index, phase_index)

    def test_time_symbol_inspector_renders_decision_evidence_link_and_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-1"
            run = self._write_debate_and_judge_run(
                run_dir,
                one_line_reason="000001-bull-opening-1의 성장 모멘텀이 000001-bear-opening-1/2의 우려보다 크다.",
            )
            rendered = render_time_symbol_inspector([run], [])

        self.assertIn('href="#arg-0-000001-bull-opening-1"', rendered)
        self.assertIn('href="#arg-0-000001-bear-opening-1"', rendered)
        self.assertIn('href="#arg-0-000001-bear-opening-2"', rendered)
        self.assertIn("성장 모멘텀 강함", rendered)
        self.assertIn("밸류에이션 부담", rendered)
        self.assertIn("수급 악화", rendered)
        self.assertIn('id="arg-0-000001-bull-opening-1"', rendered)
        self.assertIn('id="arg-0-000001-bear-opening-1"', rendered)
        self.assertIn('id="arg-0-000001-bear-opening-2"', rendered)
        evidence_index = rendered.index('class="decision-evidence"')
        final_index = rendered.index('<article class="final-card full">')
        self.assertLess(final_index, evidence_index)

    def test_time_symbol_inspector_anchors_are_unique_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir_a = Path(temporary) / "run-1"
            run_a = self._write_debate_and_judge_run(
                run_dir_a, one_line_reason="000001-bull-opening-1의 근거로 매수한다.", extra_bear_argument=False
            )
            run_a["summary"] = {"started_at": "2026-07-20T14:00:49+09:00"}
            run_dir_b = Path(temporary) / "run-2"
            run_b = self._write_debate_and_judge_run(
                run_dir_b, one_line_reason="000001-bull-opening-1의 근거로 매수한다.", extra_bear_argument=False
            )
            run_b["summary"] = {"started_at": "2026-07-20T15:00:00+09:00"}

            rendered = render_time_symbol_inspector([run_a, run_b], [])

        self.assertIn('id="arg-0-000001-bull-opening-1"', rendered)
        self.assertIn('id="arg-1-000001-bull-opening-1"', rendered)
        self.assertIn('href="#arg-0-000001-bull-opening-1"', rendered)
        self.assertIn('href="#arg-1-000001-bull-opening-1"', rendered)

    def test_time_symbol_inspector_ignores_unknown_or_missing_cited_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-1"
            run = self._write_debate_and_judge_run(
                run_dir,
                one_line_reason="999999-bull-opening-9의 근거로 매수하지만 실제 debate에는 없다.",
            )
            rendered = render_time_symbol_inspector([run], [])

        self.assertIn("999999-bull-opening-9의 근거로 매수하지만 실제 debate에는 없다.", rendered)
        self.assertNotIn('href="#arg-0-999999-bull-opening-9"', rendered)
        self.assertNotIn('class="decision-evidence"', rendered)

    def test_time_symbol_inspector_handles_missing_debate_artifact_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-1"
            write_json(
                run_dir / "analyst-review.json",
                {"symbols": [{"symbol_id": "000001", "symbol_name": "알파전자", "final_first_score": 5.0}]},
            )
            write_json(
                run_dir / "judge-review.json",
                {
                    "symbols": [
                        {
                            "symbol_id": "000001",
                            "symbol_name": "알파전자",
                            "final_holding_quantity": 1,
                            "target_position_value_krw": 100_000,
                            "relative_attractiveness_rank": 1,
                            "reason_code": "buy",
                            "one_line_reason": "000001-bull-opening-1의 근거로 매수한다.",
                        }
                    ]
                },
            )
            run = {
                "path": run_dir,
                "summary": {"started_at": "2026-07-20T14:00:49+09:00"},
                "execution": {"orders": []},
                "decision": {},
            }

            rendered = render_time_symbol_inspector([run], [])

        self.assertIn("000001-bull-opening-1의 근거로 매수한다.", rendered)
        self.assertNotIn('class="decision-evidence"', rendered)

    def test_parse_cited_argument_ids_expands_shorthand_and_dedupes_in_order(self) -> None:
        text = (
            "010950-bull-rebuttal-1-2의 강한 추세에도 010950-bear-rebuttal-1-1/3의 과열이 크고 "
            "010950-bull-rebuttal-1-2도 다시 언급된다."
        )

        ids = parse_cited_argument_ids(text)

        self.assertEqual(
            ids,
            ["010950-bull-rebuttal-1-2", "010950-bear-rebuttal-1-1", "010950-bear-rebuttal-1-3"],
        )

    def test_parse_cited_argument_ids_rejects_substring_of_longer_digit_run(self) -> None:
        text = "1010950-bull-opening-1 is not a standalone symbol argument id"

        ids = parse_cited_argument_ids(text)

        self.assertEqual(ids, [])

    def test_parse_cited_argument_ids_rejects_ascii_letter_prefix(self) -> None:
        self.assertEqual(parse_cited_argument_ids("x010950-bull-opening-1"), [])

    def test_parse_cited_argument_ids_rejects_ascii_letter_suffix(self) -> None:
        self.assertEqual(parse_cited_argument_ids("010950-bull-opening-1x"), [])

    def test_parse_cited_argument_ids_rejects_dangling_trailing_slash(self) -> None:
        self.assertEqual(parse_cited_argument_ids("010950-bull-opening-1/"), [])

    def test_parse_cited_argument_ids_accepts_id_followed_by_korean_particle(self) -> None:
        self.assertEqual(
            parse_cited_argument_ids("010950-bull-opening-1의 근거"), ["010950-bull-opening-1"]
        )

    def test_parse_cited_argument_ids_rejects_underscore_prefix(self) -> None:
        self.assertEqual(parse_cited_argument_ids("_010950-bull-opening-1"), [])

    def test_parse_cited_argument_ids_rejects_hyphen_prefix(self) -> None:
        self.assertEqual(parse_cited_argument_ids("-010950-bull-opening-1"), [])

    def test_parse_cited_argument_ids_rejects_underscore_suffix(self) -> None:
        self.assertEqual(parse_cited_argument_ids("010950-bull-opening-1_extra"), [])

    def test_parse_cited_argument_ids_rejects_hyphen_suffix(self) -> None:
        self.assertEqual(parse_cited_argument_ids("010950-bull-opening-1-extra"), [])

    def test_parse_cited_argument_ids_accepts_id_wrapped_in_punctuation_with_korean_suffix(self) -> None:
        self.assertEqual(
            parse_cited_argument_ids("(010950-bull-opening-1)의 근거"), ["010950-bull-opening-1"]
        )

    def test_argument_anchor_id_rejects_malformed_argument_id(self) -> None:
        self.assertIsNone(argument_anchor_id(0, "bad id"))
        self.assertIsNone(argument_anchor_id(0, "000001-bull-opening-1\n"))
        self.assertEqual(argument_anchor_id(0, "000001-bull-opening-1"), "arg-0-000001-bull-opening-1")

    def test_time_symbol_inspector_does_not_link_real_argument_belonging_to_other_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-1"
            write_json(
                run_dir / "analyst-review.json",
                {
                    "symbols": [
                        {"symbol_id": "000001", "symbol_name": "알파전자", "final_first_score": 5.0},
                        {"symbol_id": "000002", "symbol_name": "베타소재", "final_first_score": 5.0},
                    ]
                },
            )
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
                                                "arguments": [
                                                    {"argument_id": "000001-bull-opening-1", "kind": "claim", "statement": "알파전자 성장 모멘텀", "evidence_refs": [], "targets": []}
                                                ],
                                                "concessions": [],
                                                "unresolved_conflicts": [],
                                                "final_position": "매수 우위",
                                                "recommended_action": "buy",
                                                "target_holding_quantity": 1,
                                            },
                                            {
                                                "symbol_id": "000002",
                                                "symbol_name": "베타소재",
                                                "arguments": [
                                                    {"argument_id": "000002-bull-opening-1", "kind": "claim", "statement": "베타소재 반등 기대", "evidence_refs": [], "targets": []}
                                                ],
                                                "concessions": [],
                                                "unresolved_conflicts": [],
                                                "final_position": "매수 우위",
                                                "recommended_action": "buy",
                                                "target_holding_quantity": 1,
                                            },
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
                            "final_holding_quantity": 1,
                            "target_position_value_krw": 100_000,
                            "relative_attractiveness_rank": 1,
                            "reason_code": "buy",
                            # Cites a real argument ID, but it belongs to symbol 000002, not this
                            # 000001 Final Judge entry.
                            "one_line_reason": "000002-bull-opening-1의 근거로 매수한다.",
                        }
                    ]
                },
            )
            run = {
                "path": run_dir,
                "summary": {"started_at": "2026-07-20T14:00:49+09:00"},
                "execution": {"orders": []},
                "decision": {},
            }

            rendered = render_time_symbol_inspector([run], [])

        self.assertIn("000002-bull-opening-1의 근거로 매수한다.", rendered)
        self.assertNotIn('href="#arg-0-000002-bull-opening-1"', rendered)
        self.assertNotIn('class="decision-evidence"', rendered)

    def test_time_symbol_inspector_skips_dom_anchor_for_malformed_argument_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run-1"
            write_json(
                run_dir / "analyst-review.json",
                {"symbols": [{"symbol_id": "000001", "symbol_name": "알파전자", "final_first_score": 5.0}]},
            )
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
                                                "arguments": [
                                                    {"argument_id": "bad id", "kind": "claim", "statement": "malformed id from an old artifact", "evidence_refs": [], "targets": []}
                                                ],
                                                "concessions": [],
                                                "unresolved_conflicts": [],
                                                "final_position": "매수 우위",
                                                "recommended_action": "buy",
                                                "target_holding_quantity": 1,
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
                            "final_holding_quantity": 1,
                            "target_position_value_krw": 100_000,
                            "relative_attractiveness_rank": 1,
                            "reason_code": "buy",
                            "one_line_reason": "malformed id를 참고해 매수한다.",
                        }
                    ]
                },
            )
            run = {
                "path": run_dir,
                "summary": {"started_at": "2026-07-20T14:00:49+09:00"},
                "execution": {"orders": []},
                "decision": {},
            }

            rendered = render_time_symbol_inspector([run], [])

        self.assertIn("malformed id from an old artifact", rendered)
        self.assertNotIn(' id="arg-0-bad id"', rendered)
        self.assertNotIn('href="#arg-0-bad id"', rendered)


if __name__ == "__main__":
    raise SystemExit(self_test())
