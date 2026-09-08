from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time, timezone
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from ..scripts import agent_replay_backtest as replay, run_subagent
from ..scripts.agent_replay_backtest import (
    align_replay_generated_at,
    backtest,
    benchmark_history,
    discover_run_rows,
    execution_preflight,
    future_input_timestamps,
    performance_period,
    rebuilt_market_news_context,
    select_replay_days,
    simulate_targets,
    trailing_return,
    virtualize_inputs,
)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def brief(started_at: str, *, price_a: int = 100, price_b: int = 100) -> dict:
    symbols = []
    for symbol_id, symbol_name, price, change in (
        ("A", "Alpha", price_a, -5.0),
        ("B", "Beta", price_b, 5.0),
    ):
        symbols.append(
            {
                "symbol_id": symbol_id,
                "symbol_name": symbol_name,
                "eligible_for_review": True,
                "price": {"current_or_last": price, "observed_at": started_at},
                "chart_context": {"daily_summary": {"change_20_period_pct": change}},
                "orderbook_summary": {
                    "best_ask": price + 1,
                    "best_bid": price - 1,
                    "ask_quantity_1": 100,
                    "bid_quantity_1": 100,
                },
                "account_exposure": {"current_live_holding_quantity": 99},
                "symbol_strategy_context": {"current_holding": True},
                "today_trade_price_context": {"has_same_day_trade": True},
                "today_trade_timeline_context": {"fills": [{"actual": True}]},
            }
        )
    return {
        "status": "success",
        "run_id": "source",
        "started_at": started_at,
        "source_artifacts": {"information_cutoff": started_at},
        "portfolio": {"holding": ["A"], "specified": ["A", "B"], "universe": ["A", "B"]},
        "account_exposure_summary": {"total_evaluation_amount": 999999},
        "account_performance_context": {"periods": {"primary": {"account_return_pct": 99}}},
        "symbols": symbols,
    }


def account(started_at: str, total: int = 2_000) -> dict:
    return {
        "status": "success",
        "started_at": started_at,
        "account_summary": {
            "orderable_cash_amount": 1_000,
            "securities_valuation_amount": 1_000,
            "total_evaluation_amount": total,
        },
        "symbols": [
            {
                "symbol_id": "A",
                "symbol_name": "Alpha",
                "current_live_holding_quantity": 10,
                "current_price": 100,
                "average_purchase_price": 90,
                "valuation_amount": 1_000,
            },
            {
                "symbol_id": "B",
                "symbol_name": "Beta",
                "current_live_holding_quantity": 0,
                "current_price": 100,
                "valuation_amount": 0,
            },
        ],
        "active_orders": [{"actual": True}],
        "active_order_lookup_performed": True,
    }


def market(started_at: str, value: int) -> dict:
    return {
        "status": "success",
        "started_at": started_at,
        "indexes": [{"symbol": "KOSPI", "status": "success", "value": value}],
    }


def write_run(
    root: Path, name: str, started_at: str, value: int, *, total: int = 2_000, market_open: bool = True,
) -> None:
    run = root / name
    write_json(run / "account-before-order.json", account(started_at, total))
    write_json(run / "decision-brief.json", brief(started_at))
    write_json(run / "market-index-snapshot.json", market(started_at, value))
    write_json(
        run / "price-chart.json",
        {"market_open_day_checked": True, "market_open_day": market_open},
    )
    write_json(run / "check-portfolio.json", {"holding": ["A"], "specified": ["A", "B"]})


class AgentReplayBacktestTest(unittest.TestCase):
    def test_frozen_replay_uses_production_plan_and_no_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs, advice = root / "archive", root / "advice"
            for session in ("2026-08-05", "2026-08-06"):
                for label, stamp in (("decision", "09:05"), ("fill", "09:20"), ("close", "15:15")):
                    name = f"{session}-{label}"
                    write_run(runs, name, f"{session}T{stamp}:00+09:00", 100)
                    payload = replay.read_json(runs / name / "decision-brief.json")
                    for row in payload["symbols"]:
                        row["exchange_preflight"] = {"exchange": "KRX"}
                        row["orderbook_summary"].update(best_ask=100, best_bid=100)
                        row["position_management_context"] = {"enabled": True, "entry_allowed": False}
                    write_json(runs / name / "decision-brief.json", payload)
                source = advice / "runs" / f"{session.replace('-', '')}T090500+0900-replay"
                write_json(source / "decision-brief.json", {"replay_context": {"source_run_id": f"{session}-decision"}})
                write_json(source / "judge-review.json", {"status": "success", "symbols": [
                    {"symbol_id": "A", "target_position_value_krw": 1000},
                    {"symbol_id": "B", "target_position_value_krw": 0, "requested_target_position_value_krw": 500,
                     "reason_code": "entry_overextended", "one_line_reason": "policy blocked increase",
                     "position_management": {"adjustment_reason": "entry_overextended",
                                             "judge_reason_code": "agent_increase",
                                             "judge_one_line_reason": "original Agent evidence"}},
                ]})
            args = replay.build_parser().parse_args([
                "--runs-root", str(runs), "--workspace-dir", str(root), "--market-news-db", str(root / "missing.sqlite3"),
                "--frozen-targets-root", str(advice), "--start", "2026-08-05", "--end", "2026-08-06",
                "--output-root", str(root / "baseline"),
            ])
            with patch.object(replay, "run_daily_agents", side_effect=AssertionError("model call forbidden")), \
                    patch.object(replay.build_run_artifacts, "build_execution_plan",
                                 wraps=replay.build_run_artifacts.build_execution_plan) as plans:
                result = backtest(args)
            self.assertEqual(plans.call_count, 2)
            self.assertEqual(result["mode"], "conditional_frozen_judge_replay")
            self.assertEqual(result["coverage"]["new_model_calls"], 0)
            self.assertEqual(result["coverage"]["expected_model_calls"], 0)
            self.assertEqual(sum(len(day["fills"]) for day in result["daily"]), 1)
            self.assertIn("Agent 재판단 백테스트 아님", replay.markdown_report(result))
            # The second decision uses the first day's virtual holdings/cash,
            # not the archived real account. No chart-driven entry cap remains.
            second_dir = root / "baseline/runs/20260806T090500+0900-replay"
            second = replay.read_json(second_dir / "account-before-order.json")
            self.assertEqual({s["symbol_id"]: s["current_live_holding_quantity"] for s in second["symbols"]}["B"], 5)
            for row in replay.read_json(second_dir / "frozen-review-core.json")["symbols"]:
                self.assertNotIn("position_management_context", row)
            restored = replay.read_json(second_dir / "judge-review.json")["symbols"][1]
            self.assertEqual(restored["final_holding_quantity"], 5)
            self.assertEqual(restored["reason_code"], "agent_increase")
            self.assertEqual(restored["one_line_reason"], "original Agent evidence")
            self.assertNotIn("position_management", restored)
            with self.assertRaisesRegex(ValueError, "identical archived"):
                replay.normalize_frozen_judge(second_dir, advice / "runs/20260805T090500+0900-replay")
            manifest = replay.read_json(args.output_root / "manifest.json")
            self.assertEqual(manifest["review_contract_version"], 10)
            manifest["review_contract_version"] = 9
            write_json(args.output_root / "manifest.json", manifest)
            with patch.object(replay, "run_daily_agents", side_effect=AssertionError("model call forbidden")):
                with self.assertRaisesRegex(ValueError, "different configuration"):
                    backtest(args)
            source_dir = advice / "runs" / second_dir.name
            original = replay.read_json(source_dir / "judge-review.json")
            for missing in ("judge_reason_code", "judge_one_line_reason", "requested_target_position_value_krw"):
                with self.subTest(missing=missing):
                    invalid = json.loads(json.dumps(original))
                    row = invalid["symbols"][1]
                    if missing.startswith("judge_"):
                        row["position_management"].pop(missing)
                    else:
                        row.pop(missing)
                    write_json(source_dir / "judge-review.json", invalid)
                    with self.assertRaisesRegex(ValueError, "lacks original Agent"):
                        replay.normalize_frozen_judge(second_dir, source_dir)

    @staticmethod
    def fake_analyst_group(specs: list[dict], max_workers: int) -> dict:
        wrappers = []
        for spec in specs:
            model, effort = run_subagent.launcher_model_effort(spec["stage"], spec["agent_role"])
            wrapper = {
                **{key: spec[key] for key in ("stage", "agent_role", "task_name", "run_id")},
                "status": "success", "model": model, "model_reasoning_effort": effort,
                "spec_fingerprint": run_subagent.spec_fingerprint(spec),
                "token_usage": {"total_tokens": 10},
                "parsed_json": {"symbols": [
                    {"symbol_id": symbol, "views": {
                        role: {"score": 6, "reason_code": "hold_neutral", "one_line_reason": "Evidence", "missing_data": []}
                        for role in run_subagent.COMBINED_ANALYST_REVIEW_ROLE_OUTPUTS[spec["agent_role"]]
                    }} for symbol in spec["symbol_ids"]
                ]},
            }
            write_json(run_subagent.wrapper_paths(spec)[0], wrapper)
            wrappers.append(wrapper)
        return {"status": "success", "wrappers": wrappers}

    def test_comparison_reuses_identical_analysts_but_runs_distinct_judges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline, candidate = root / "baseline" / "same-day", root / "candidate" / "same-day"
            for output in (baseline, candidate):
                write_run(output.parent, output.name, "2026-08-27T09:05:00+09:00", 3000)
            candidate_account = account("2026-08-27T09:05:00+09:00")
            candidate_account["symbols"][0]["current_live_holding_quantity"] = 7
            write_json(candidate / "account-before-order.json", candidate_account)
            candidate_brief = brief("2026-08-27T09:05:00+09:00")
            candidate_brief["symbols"][0]["account_exposure"]["current_live_holding_quantity"] = 7
            write_json(candidate / "decision-brief.json", candidate_brief)
            judge_snapshot = root / "baseline-judge.md"
            judge_snapshot.write_text("Original Judge instructions", encoding="utf-8")
            judge_calls = []

            def fake_judge(spec: dict) -> dict:
                judge_calls.append((spec, replay.read_json(Path(spec["output_dir"]) / "account-before-order.json")))
                return {"status": "success", "stage": "judge-review", "token_usage": {"total_tokens": 5}}

            with patch.object(run_subagent, "run_group", side_effect=self.fake_analyst_group) as group, \
                    patch.object(run_subagent, "run_one", side_effect=fake_judge), \
                    patch.object(replay.Pipeline, "write_judge_review", return_value={"status": "success", "symbols": []}):
                _, baseline_wrappers = replay.run_daily_agents(baseline, root, judge_prompt=judge_snapshot)
                _, candidate_wrappers = replay.run_daily_agents(candidate, root, analyst_reuse_dir=baseline)
            self.assertEqual(group.call_count, 1)
            self.assertEqual(len(judge_calls), 2)
            self.assertEqual(judge_calls[0][0]["artifact_paths"]["persona"], str(judge_snapshot))
            self.assertEqual(judge_calls[1][0]["artifact_paths"]["persona"], str(replay.PIPELINE_DIR / "prompts" / "judge.md"))
            self.assertEqual([item[1]["symbols"][0]["current_live_holding_quantity"] for item in judge_calls], [10, 7])
            self.assertEqual(len(baseline_wrappers), 3)
            self.assertEqual(sum(bool(item.get("reused_analyst_source")) for item in candidate_wrappers), 2)
            self.assertFalse(candidate_wrappers[-1].get("reused_existing_wrapper"))
            self.assertEqual(replay.read_json(baseline / "analyst-review.json"), replay.read_json(candidate / "analyst-review.json"))

    def test_analyst_reuse_rejects_changed_evidence_before_judge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline, candidate = root / "baseline" / "same-day", root / "candidate" / "same-day"
            for output in (baseline, candidate):
                write_run(output.parent, output.name, "2026-08-27T09:05:00+09:00", 3000)
            with patch.object(run_subagent, "run_group", side_effect=self.fake_analyst_group), \
                    patch.object(run_subagent, "run_one", return_value={"status": "success"}), \
                    patch.object(replay.Pipeline, "write_judge_review", return_value={"status": "success", "symbols": []}):
                replay.run_daily_agents(baseline, root)
            changed = brief("2026-08-27T09:05:00+09:00", price_a=101)
            write_json(candidate / "decision-brief.json", changed)
            with patch.object(run_subagent, "run_group") as group, patch.object(run_subagent, "run_one") as judge:
                with self.assertRaisesRegex(ValueError, "input/model mismatch"):
                    replay.run_daily_agents(candidate, root, analyst_reuse_dir=baseline)
                group.assert_not_called()
                judge.assert_not_called()

    def test_analyst_reuse_identity_covers_models_rules_and_original_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline, candidate = root / "baseline" / "same-day", root / "candidate" / "same-day"
            for output in (baseline, candidate):
                write_run(output.parent, output.name, "2026-08-27T09:05:00+09:00", 3000)
            with patch.object(run_subagent, "run_group", side_effect=self.fake_analyst_group), \
                    patch.object(run_subagent, "run_one", return_value={"status": "success"}), \
                    patch.object(replay.Pipeline, "write_judge_review", return_value={"status": "success", "symbols": []}):
                replay.run_daily_agents(baseline, root)
            with patch.object(run_subagent, "launcher_model_effort", return_value=("different-model", "xhigh")), \
                    patch.object(run_subagent, "run_one") as judge:
                with self.assertRaisesRegex(ValueError, "input/model mismatch"):
                    replay.run_daily_agents(candidate, root, analyst_reuse_dir=baseline)
                judge.assert_not_called()
            spec = replay.read_json(baseline / "analyst-review-specs.json")["specs"][0]
            original_identity = replay.analyst_prompt_identity(spec)
            altered_rule = root / "altered-rule.md"
            altered_rule.write_text("Changed rule", encoding="utf-8")
            for key in ("persona", "review_format"):
                with self.subTest(key=key):
                    altered = {**spec, "artifact_paths": {**spec["artifact_paths"], key: str(altered_rule)}}
                    self.assertNotEqual(original_identity, replay.analyst_prompt_identity(altered))
            wrapper_path, _ = run_subagent.wrapper_paths(spec)
            wrapper = replay.read_json(wrapper_path)
            wrapper["parsed_json"]["symbols"][0]["views"][next(iter(wrapper["parsed_json"]["symbols"][0]["views"]))]["score"] = 10
            write_json(wrapper_path, wrapper)
            with patch.object(run_subagent, "run_one") as judge:
                with self.assertRaisesRegex(ValueError, "source wrapper changed"):
                    replay.run_daily_agents(candidate, root, analyst_reuse_dir=baseline)
                judge.assert_not_called()

    def test_sale_proceeds_cannot_fund_same_batch_buy(self) -> None:
        state = {"cash": 0.0, "positions": {"A": {"quantity": 10, "average_price": 90}}}
        result = simulate_targets(state, {"symbols": [
            {"symbol_id": "A", "final_holding_quantity": 0},
            {"symbol_id": "B", "final_holding_quantity": 10},
        ]}, {"A": {"sell_price": 100, "sell_quantity": 10},
             "B": {"buy_price": 100, "buy_quantity": 10}}, {"A": 100, "B": 100}, cost_bps=0)
        self.assertEqual([row["symbol_id"] for row in result["fills"]], ["A"])
        self.assertEqual(result["decisions"][1]["unsubmitted_quantity"], 10)
        self.assertEqual(state, {"cash": 1000.0, "positions": {}})

    def test_cash_gate_keeps_row_order_not_symbol_or_rank_order(self) -> None:
        state = {"cash": 1000.0, "positions": {}}
        result = simulate_targets(state, {"symbols": [
            {"symbol_id": "Z", "final_holding_quantity": 10, "relative_attractiveness_rank": 2},
            {"symbol_id": "A", "final_holding_quantity": 10, "relative_attractiveness_rank": 1},
        ]}, {s: {"buy_price": 100, "buy_quantity": 10} for s in ("Z", "A")},
            {"Z": 100, "A": 100}, cost_bps=0)
        self.assertEqual([row["symbol_id"] for row in result["fills"]], ["Z"])
        self.assertEqual(result["reserved_buy_notional"], 1000)

    def test_limit_must_be_met_for_both_sides_and_cash_stays_reserved(self) -> None:
        state = {"cash": 1000.0, "positions": {"S": {"quantity": 10, "average_price": 90}}}
        result = simulate_targets(state, {"symbols": [
            {"symbol_id": "B", "final_holding_quantity": 10},
            {"symbol_id": "S", "final_holding_quantity": 0},
            {"symbol_id": "C", "final_holding_quantity": 1},
        ]}, {"B": {"buy_price": 101, "buy_quantity": 10},
             "S": {"sell_price": 99, "sell_quantity": 10},
             "C": {"buy_price": 100, "buy_quantity": 10}},
            {"B": 100, "S": 100, "C": 100}, cost_bps=0)
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["unfilled_order_quantity"], 20)
        self.assertEqual(result["unsubmitted_order_quantity"], 1)
        self.assertEqual(state["cash"], 1000)

    def test_partial_fill_reports_unfilled_and_fee_reserve(self) -> None:
        state = {"cash": 1000.0, "positions": {}}
        result = simulate_targets(state, {"symbols": [{"symbol_id": "A", "final_holding_quantity": 10}]},
            {"A": {"buy_price": 100, "buy_quantity": 3}}, {"A": 100}, cost_bps=20)
        decision = result["decisions"][0]
        self.assertEqual((decision["validated_quantity"], decision["filled_quantity"],
                          decision["unfilled_quantity"], decision["unsubmitted_quantity"]), (9, 3, 6, 1))
        self.assertAlmostEqual(state["cash"], 699.4)
        self.assertEqual(result["reserved_buy_notional"], 900)

    def test_execution_plan_limits_and_blocked_routes_are_used(self) -> None:
        state = {"cash": 1000.0, "positions": {}}
        plan = {"status": "partial", "errors": [{"code": "order_submission_blocked", "required": True}], "orders": [
            {"symbol_id": "A", "final_holding_quantity": 1, "order_price": 90, "order_path": "immediate"},
            {"symbol_id": "B", "final_holding_quantity": 1, "order_price": 100,
             "result": "blocked", "reason": "exchange_preflight_blocked"},
        ]}
        result = simulate_targets(state, {"symbols": []},
            {s: {"buy_price": 95, "buy_quantity": 10} for s in ("A", "B")},
            {"A": 100, "B": 100}, cost_bps=0, execution_plan=plan)
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["decisions"][0]["limit_price"], 90)
        self.assertEqual(result["decisions"][1]["execution_reason"], "exchange_preflight_blocked")
        self.assertNotIn("attempts", plan["orders"][0])

    def test_missing_orders_from_plan_schema_errors_cannot_be_silent_holds(self) -> None:
        for code in ("duplicate_judge_symbol", "invalid_final_holding_quantity"):
            state = {"cash": 1000.0, "positions": {}}
            plan = {"status": "partial", "errors": [{"code": code, "required": True}], "orders": []}
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                simulate_targets(state, {"symbols": []}, {}, {}, cost_bps=0, execution_plan=plan)
            self.assertEqual(state, {"cash": 1000.0, "positions": {}})
        with self.assertRaisesRegex(ValueError, "invalid production Judge"):
            simulate_targets({"cash": 1000, "positions": {}},
                {"status": "partial", "errors": [{"code": "invalid_target", "required": True}], "symbols": []},
                {}, {}, cost_bps=0, execution_plan={"orders": []})

    def test_invalid_targets_and_costs_fail_without_trading(self) -> None:
        for value in (-1, None, 1.5, True):
            state = {"cash": 1000.0, "positions": {}}
            with self.subTest(value=value), self.assertRaises(ValueError):
                simulate_targets(state, {"symbols": [{"symbol_id": "A", "final_holding_quantity": value}]},
                                 {}, {"A": 100}, cost_bps=0)
            self.assertEqual(state["cash"], 1000)
        for cost in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                simulate_targets({"cash": 1000, "positions": {}}, {"symbols": []}, {}, {}, cost_bps=cost)

    def test_preflight_blocks_missing_routes_before_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            for label, stamp in (("decision", "09:05"), ("fill", "09:20"), ("close", "15:15")):
                write_run(root, label, f"2026-08-05T{stamp}:00+09:00", 100)
            args = SimpleNamespace(runs_root=root, start=date(2026, 8, 5), end=date(2026, 8, 5),
                decision_time=time(9, 5), output_root=root / "output", preflight_only=True)
            with patch("service.pipelines.daily_trading.scripts.agent_replay_backtest.run_daily_agents") as agents:
                preflight = backtest(args)
                self.assertEqual(preflight["status"], "blocked")
                self.assertIn("archived_exchange_route_missing", preflight["blocking_reasons"])
                self.assertTrue((args.output_root / "execution-preflight.json").exists())
                args.preflight_only = False
                with self.assertRaisesRegex(ValueError, "before model calls"):
                    backtest(args)
                agents.assert_not_called()

    def test_preflight_distinguishes_supported_input_and_active_orders(self) -> None:
        source = {"brief": brief("2026-08-05T09:05:00+09:00"),
                  "account": account("2026-08-05T09:05:00+09:00"),
                  "started_at": datetime.fromisoformat("2026-08-05T09:05:00+09:00"),
                  "path": Path("source"), "market_open_day": True}
        for item in source["brief"]["symbols"]:
            item["exchange_preflight"] = {"exchange": "KRX"}
        days = [{"date": "2026-08-05", "decision": source,
                 "fill": {"started_at": datetime.fromisoformat("2026-08-05T09:20:00+09:00")}}]
        self.assertEqual(execution_preflight(days)["status"], "ready_for_approximate_replay")
        source["account"]["active_orders"] = [{"active_status": "inactive", "remaining_quantity": 1}]
        self.assertEqual(execution_preflight(days)["status"], "ready_for_approximate_replay")
        source["account"]["active_orders"] = [{"active_status": "active", "remaining_quantity": 1}]
        self.assertIn("initial_active_orders_not_supported", execution_preflight(days)["blocking_reasons"])

    def test_session_selection_requires_positive_archived_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            for name, started_at in (
                ("decision", "2026-08-05T09:05:00+09:00"),
                ("fill", "2026-08-05T09:20:00+09:00"),
                ("close", "2026-08-05T15:15:00+09:00"),
            ):
                write_run(root, name, started_at, 100, market_open=False)
            selection = (date(2026, 8, 5), date(2026, 8, 5), time(9, 5))
            self.assertEqual(select_replay_days(discover_run_rows(root), *selection), [])
            for run in root.iterdir():
                write_json(run / "price-chart.json", {"symbols": [{"charts": {"daily": [{"date": "20260804"}]}}]})
            self.assertEqual(select_replay_days(discover_run_rows(root), *selection), [])
            for run in root.iterdir():
                write_json(run / "price-chart.json", {"symbols": [{"charts": {"daily": [{"date": "20260805"}]}}]})
            self.assertEqual(len(select_replay_days(discover_run_rows(root), *selection)), 1)

    def test_simulation_fills_judge_targets_without_replay_strategy(self) -> None:
        state = {
            "cash": 1_000.0,
            "positions": {
                "A": {"symbol_name": "Alpha", "quantity": 10, "average_price": 90.0}
            },
        }
        result = simulate_targets(
            state,
            {
                "symbols": [
                    {
                        "symbol_id": "A",
                        "symbol_name": "Alpha",
                        "final_holding_quantity": 9,
                    },
                    {
                        "symbol_id": "B",
                        "symbol_name": "Beta",
                        "final_holding_quantity": 1,
                    },
                ]
            },
            {
                "A": {"sell_price": 100.0, "sell_quantity": 10},
                "B": {"buy_price": 100.0, "buy_quantity": 10},
            },
            {"A": 100.0, "B": 100.0},
            cost_bps=0.0,
        )

        self.assertEqual([item["direction"] for item in result["fills"]], ["sell", "buy"])
        self.assertEqual(result["gross_turnover_amount"], 200.0)
        self.assertNotIn("core_transactions", result)
        self.assertNotIn("core_decision", result)
        self.assertEqual(state["cash"], 1_000.0)
        self.assertNotIn("benchmark_core_units", state)

    def test_virtual_performance_uses_persisted_gross_turnover_field(self) -> None:
        period = performance_period(
            [
                {
                    "opening_nav": 1_000.0,
                    "closing_nav": 1_050.0,
                    "benchmark_open": 100.0,
                    "benchmark_close": 102.0,
                    "gross_turnover_pct": 4.5,
                }
            ],
            5,
        )
        self.assertEqual(period["account_return_pct"], 5.0)
        self.assertEqual(period["max_daily_gross_turnover_pct"], 4.5)

    def test_rebuilt_news_excludes_future_collection_and_future_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            database = Path(tmp_name) / "news.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE articles (
                    id INTEGER PRIMARY KEY, canonical_url TEXT, title_hash TEXT, title TEXT,
                    url TEXT, domain TEXT, source_country TEXT, source_language TEXT,
                    published_at TEXT, collected_at TEXT
                );
                CREATE TABLE article_provenance (
                    article_id INTEGER, source_id TEXT, provider TEXT,
                    provider_article_id TEXT, classification TEXT, first_collected_at TEXT
                );
                CREATE TABLE collection_runs (
                    id INTEGER PRIMARY KEY, source_id TEXT, started_at TEXT, finished_at TEXT,
                    status TEXT, window_start TEXT, window_end TEXT,
                    fetched_count INTEGER, inserted_count INTEGER, duplicate_count INTEGER, error TEXT
                );
                """
            )
            articles = [
                (1, "", "one", "known in time", "", "d", "KR", "ko", "2026-08-04T00:00:00+00:00", "2026-08-04T00:01:00+00:00"),
                (2, "", "two", "collected later", "", "d", "KR", "ko", "2026-08-04T00:00:00+00:00", "2026-08-04T01:00:00+00:00"),
                (3, "", "three", "provenance later", "", "d", "KR", "ko", "2026-08-04T00:00:00+00:00", "2026-08-04T00:01:00+00:00"),
            ]
            connection.executemany("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?)", articles)
            connection.executemany(
                "INSERT INTO article_provenance VALUES (?,?,?,?,?,?)",
                [
                    (1, "domestic", "provider", "1", "market", "2026-08-04T00:01:00+00:00"),
                    (2, "domestic", "provider", "2", "market", "2026-08-04T01:00:00+00:00"),
                    (3, "domestic", "provider", "3", "market", "2026-08-04T01:00:00+00:00"),
                ],
            )
            connection.commit()
            connection.close()

            context = rebuilt_market_news_context(
                database,
                datetime(2026, 8, 4, 0, 30, tzinfo=timezone.utc),
                None,
            )

            self.assertEqual([item["title"] for item in context["items"]], ["known in time"])

    def test_recursive_input_cutoff_finds_future_nested_information_time(self) -> None:
        cutoff = datetime(2026, 8, 4, 0, 30, tzinfo=timezone.utc)
        payload = {
            "generated_at": "2026-08-04T00:30:00+00:00",
            "nested": [
                {"published_at": "2026-08-04T00:29:00+00:00"},
                {"price": {"observed_at": "2026-08-04T00:31:00+00:00"}},
            ],
        }

        self.assertEqual(
            future_input_timestamps(payload, cutoff, "decision-brief.json"),
            ["decision-brief.json.nested[1].price.observed_at=2026-08-04T00:31:00+00:00"],
        )

    def test_recursive_input_cutoff_finds_future_chart_date_in_decision_brief(self) -> None:
        cutoff = datetime(2026, 8, 4, 0, 30, tzinfo=timezone.utc)
        payload = {
            "symbols": [
                {
                    "chart_context": {
                        "recent_daily": [{"date": "20260805", "close": 100}],
                    }
                }
            ]
        }

        self.assertEqual(
            future_input_timestamps(payload, cutoff, "decision-brief.json"),
            [
                "decision-brief.json.symbols[0].chart_context.recent_daily[0].date="
                "2026-08-05T00:00:00+00:00"
            ],
        )

    def test_analyst_generated_at_is_aligned_to_replay_information_cutoff(self) -> None:
        analyst = {"generated_at": "2026-09-02T12:00:00+09:00", "reviews": []}
        decision_brief = {
            "source_artifacts": {"information_cutoff": "2026-08-04T09:05:13+09:00"}
        }

        cutoff = align_replay_generated_at(analyst, decision_brief)

        self.assertEqual(cutoff.isoformat(), "2026-08-04T09:05:13+09:00")
        self.assertEqual(analyst["generated_at"], "2026-08-04T09:05:13+09:00")
        self.assertEqual(future_input_timestamps(analyst, cutoff, "analyst-review.json"), [])

    def test_aligned_analyst_time_survives_actual_judge_input_slice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            output = Path(tmp_name)
            decision_brief = brief("2026-08-04T09:05:13+09:00")
            decision_brief["source_artifacts"] = {
                "information_cutoff": "2026-08-04T09:05:13+09:00"
            }
            analyst = {
                "generated_at": "2026-09-02T12:00:00+09:00",
                "stage": "analyst-review",
                "symbols": [{"symbol_id": "A"}],
            }
            cutoff = align_replay_generated_at(analyst, decision_brief)
            write_json(output / "decision-brief.json", decision_brief)
            write_json(output / "analyst-review.json", analyst)

            paths = run_subagent.write_review_input_slices(
                {
                    "stage": "judge-review",
                    "prompt": "",
                    "artifact_paths": {
                        "decision_brief": str(output / "decision-brief.json"),
                        "analyst_review": str(output / "analyst-review.json"),
                    },
                    "symbol_ids": ["A"],
                    "workspace_dir": str(output),
                    "output_dir": str(output),
                    "task_name": "second-judge",
                    "agent_role": "judge",
                    "started_at": "2026-08-04T09:05:13+09:00",
                }
            )
            judge_input = json.loads(Path(paths["analyst_review"]).read_text())

            self.assertEqual(judge_input["generated_at"], cutoff.isoformat())
            self.assertEqual(
                future_input_timestamps(judge_input, cutoff, "second-judge.analyst-review-slice.json"),
                [],
            )

    def test_selects_exact_daily_decision_then_next_observation_and_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            write_run(root, "open", "2026-08-04T08:50:00+09:00", 99)
            write_run(root, "decision", "2026-08-04T09:05:13+09:00", 100)
            write_run(root, "fill", "2026-08-04T09:20:13+09:00", 101)
            write_run(root, "close", "2026-08-04T15:15:13+09:00", 110)

            rows = discover_run_rows(root)
            days = select_replay_days(rows, date(2026, 8, 4), date(2026, 8, 4), time(9, 5))

            self.assertEqual(len(days), 1)
            self.assertEqual(days[0]["decision"]["path"].name, "decision")
            self.assertEqual(days[0]["fill"]["path"].name, "fill")
            self.assertEqual(days[0]["close"]["path"].name, "close")

    def test_selects_first_fill_after_decision_information_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            write_run(root, "decision", "2026-08-04T09:05:00+09:00", 100)
            decision_brief = json.loads((root / "decision" / "decision-brief.json").read_text(encoding="utf-8"))
            decision_brief["generated_at"] = "2026-08-04T09:10:00+09:00"
            write_json(root / "decision" / "decision-brief.json", decision_brief)
            write_run(root, "before-cutoff", "2026-08-04T09:06:00+09:00", 101)
            write_run(root, "fill", "2026-08-04T09:20:00+09:00", 102)
            write_run(root, "close", "2026-08-04T15:15:00+09:00", 110)

            days = select_replay_days(
                discover_run_rows(root),
                date(2026, 8, 4),
                date(2026, 8, 4),
                time(9, 5),
            )

            self.assertEqual(days[0]["fill"]["path"].name, "fill")

    def test_benchmark_history_omits_run_outside_decision_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            write_run(root, "far", "2026-07-17T07:00:41+09:00", 100)
            write_run(root, "near", "2026-07-20T09:05:41+09:00", 101)

            history = benchmark_history(discover_run_rows(root), time(9, 5))

            self.assertEqual(history, [("2026-07-20", 101)])

    def test_trailing_return_uses_only_twenty_period_old_level(self) -> None:
        history = [(f"d{index:02d}", 100.0 + index) for index in range(21)]
        self.assertIsNone(trailing_return(history, "d19", 20))
        self.assertAlmostEqual(trailing_return(history, "d20", 20), 20.0)
        self.assertAlmostEqual(trailing_return(history, "d20", 20, current_value=110.0), 10.0)

    def test_benchmark_history_uses_snapshot_nearest_decision_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            write_run(root, "before", "2026-08-04T09:04:30+09:00", 100)
            write_run(root, "selected", "2026-08-04T09:05:10+09:00", 101)
            write_run(root, "future", "2026-08-04T09:06:50+09:00", 999)
            write_run(root, "close", "2026-08-04T15:15:00+09:00", 110)

            history = benchmark_history(discover_run_rows(root), time(9, 5))

            self.assertEqual(history, [("2026-08-04", 101)])

    def test_virtual_inputs_replace_actual_account_and_trade_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            source_dir = root / "source"
            write_run(root, "baseline", "2026-07-07T09:05:13+09:00", 99)
            write_run(root, "source", "2026-08-04T09:05:13+09:00", 100)
            source_brief = json.loads((source_dir / "decision-brief.json").read_text())
            source_brief["generated_at"] = "2026-08-04T09:06:30+09:00"
            source_brief["symbols"][0]["price"]["observed_at"] = "2026-08-04T09:05:40+09:00"
            source_brief["symbols"][0]["active_rotation_momentum"] = {"excess_return_pct_point": 99}
            write_json(source_dir / "decision-brief.json", source_brief)
            rows = discover_run_rows(root)
            day = {"date": "2026-08-04", "decision": rows[-1], "fill": rows[-1], "close": rows[-1]}
            state = {
                "cash": 1_000.0,
                "positions": {
                    "A": {
                        "symbol_name": "Alpha",
                        "quantity": 10,
                        "average_price": 90.0,
                    }
                },
            }
            output = root / "replay" / "run"

            history = [
                {
                    "date": "2026-08-03",
                    "opening_nav": 2_000.0,
                    "closing_nav": 2_010.0,
                    "benchmark_open": 100.0,
                    "benchmark_close": 101.0,
                    "gross_turnover_pct": 5.0,
                    "fills": [
                        {
                            "symbol_id": "A",
                            "direction": "buy",
                            "filled_quantity": 2,
                            "filled_price": 95.0,
                            "filled_at": "2026-08-03T09:20:13+09:00",
                        }
                    ],
                }
            ]
            replay_brief = virtualize_inputs(
                day,
                output,
                state,
                history,
                1.25,
                turnover_reference_pct=7.5,
            )
            replay_account = json.loads((output / "account-before-order.json").read_text())
            replay_fills = json.loads((output / "today-fills.json").read_text())

            self.assertEqual(replay_brief["started_at"], source_brief["generated_at"])
            self.assertEqual(replay_brief["source_artifacts"]["collection_started_at"], source_brief["started_at"])
            self.assertEqual(replay.read_json(source_dir / "decision-brief.json")["started_at"], source_brief["started_at"])
            with patch.object(run_subagent, "run_group", side_effect=self.fake_analyst_group) as group, \
                    patch.object(run_subagent, "run_one", return_value={"status": "success"}) as judge, \
                    patch.object(replay.Pipeline, "write_judge_review", return_value={"status": "success", "symbols": []}):
                replay.run_daily_agents(output, root)
            for spec in [*group.call_args.args[0], judge.call_args.args[0]]:
                self.assertEqual(spec["started_at"], source_brief["generated_at"])
                slices = run_subagent.write_review_input_slices(spec)
                prompt = run_subagent.build_prompt(run_subagent.spec_with_review_slices(spec, slices))
                self.assertIn("started_at: " + source_brief["generated_at"] + "\n", prompt)

            self.assertEqual(replay_brief["account_exposure_summary"]["total_evaluation_amount"], 2_000.0)
            self.assertEqual(replay_brief["account_performance_context"]["latest_day"], history[0])
            self.assertEqual(
                replay_brief["account_performance_context"]["references"]["max_daily_gross_turnover_pct"],
                7.5,
            )
            self.assertNotIn("active_rotation_policy", replay_brief["strategy_context"])
            self.assertNotIn("active_rotation_momentum", replay_brief["symbols"][0])
            judge_core = run_subagent.build_review_core_payload(replay_brief, ["A"], "judge")
            self.assertNotIn("active_rotation_momentum", judge_core["symbols"][0])
            self.assertFalse(replay_brief["symbols"][0]["today_trade_timeline_context"]["has_same_day_trade"])
            self.assertEqual(replay_brief["symbols"][1]["account_exposure"]["current_live_holding_quantity"], 0)
            self.assertEqual(replay_account["active_orders"], [])
            self.assertEqual(replay_account["account_summary"]["orderable_cash_amount"], 1_000.0)
            self.assertTrue(replay_account["symbols"][0]["snapshot_row_available"])
            self.assertEqual(replay_fills["stage"], "today-fills")
            self.assertEqual(replay_fills["previous_session"]["session_date"], "2026-08-03")
            self.assertEqual(replay_fills["previous_session"]["fill_collection_status"], "complete")
            self.assertEqual(replay_fills["previous_session"]["fills"], history[0]["fills"])
            self.assertTrue(source_dir.is_dir())


if __name__ == "__main__":
    unittest.main()
