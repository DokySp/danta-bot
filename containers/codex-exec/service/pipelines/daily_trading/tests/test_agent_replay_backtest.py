from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time, timezone
from pathlib import Path

from ..scripts import run_subagent
from ..scripts.agent_replay_backtest import (
    align_replay_generated_at,
    benchmark_history,
    discover_run_rows,
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
