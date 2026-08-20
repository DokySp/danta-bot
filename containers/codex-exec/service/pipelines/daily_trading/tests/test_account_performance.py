from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ..scripts.account_performance import (
    build_account_performance_context,
    excluded_external_flow_dates,
    record_external_flow,
)


POLICY = {
    "performance_review": {
        "primary_window_trading_days": 3,
        "auxiliary_window_trading_days": 1,
        "benchmark_index": "KOSPI",
        "primary_return_target_pct": 0.0,
        "primary_excess_return_target_pct": 0.0,
        "max_drawdown_pct": 8.0,
        "max_symbol_weight_pct": 35.0,
        "max_daily_gross_turnover_pct": 30.0,
    }
}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_run(
    runs: Path,
    run_id: str,
    started_at: str,
    total: int,
    index_value: float,
    *,
    securities: int = 800_000,
    fill: dict | None = None,
    fill_status: str = "success",
) -> None:
    run = runs / run_id
    write_json(
        run / "account-before-order.json",
        {
            "status": "success",
            "started_at": started_at,
            "account_summary": {
                "total_evaluation_amount": total,
                "securities_valuation_amount": securities,
            },
            "symbols": [
                {"symbol_id": "005930", "symbol_name": "삼성전자", "valuation_amount": 300_000},
                {"symbol_id": "000660", "symbol_name": "SK하이닉스", "valuation_amount": securities - 300_000},
            ],
        },
    )
    write_json(
        run / "market-index-snapshot.json",
        {"indexes": [{"symbol": "KOSPI", "status": "success", "value": index_value}]},
    )
    write_json(
        run / "today-fills.json",
        {"status": fill_status, "fill_scope": "account", "fills": [fill] if fill else []},
    )


class AccountPerformanceTest(unittest.TestCase):
    def test_chains_daily_returns_excludes_reported_flow_and_deduplicates_turnover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name)
            runs = workspace / "reports" / "runs"
            fill = {
                "order_date": "20260818",
                "order_id": "1",
                "symbol_id": "005930",
                "direction": "buy",
                "filled_amount": 100_000,
            }
            write_run(runs, "d18-open", "2026-08-18T09:00:00+09:00", 1_000_000, 100.0, fill=fill)
            write_run(runs, "d18-close", "2026-08-18T15:30:00+09:00", 1_100_000, 105.0, fill=fill)
            write_run(runs, "d19-open", "2026-08-19T09:00:00+09:00", 1_100_000, 105.0)
            write_run(runs, "d19-close", "2026-08-19T15:30:00+09:00", 1_210_000, 115.5)
            write_run(runs, "d20-open", "2026-08-20T09:00:00+09:00", 1_100_000, 105.0)
            write_run(
                runs,
                "d20-close",
                "2026-08-20T15:30:00+09:00",
                1_045_000,
                103.95,
                fill={
                    "order_date": "20260820",
                    "order_id": "2",
                    "symbol_id": "000660",
                    "direction": "sell",
                    "filled_amount": 330_000,
                },
            )
            record_external_flow(workspace, date(2026, 8, 19), "exclude")

            context = build_account_performance_context(
                workspace_dir=workspace,
                runs_root=runs,
                started_at="2026-08-20T16:00:00+09:00",
                policy=POLICY,
            )

            primary = context["periods"]["primary"]
            self.assertEqual(primary["included_return_days"], 2)
            self.assertEqual(primary["excluded_dates"], ["2026-08-19"])
            self.assertEqual(primary["account_return_pct"], 4.5)
            self.assertEqual(primary["benchmark_return_pct"], 3.95)
            self.assertEqual(primary["excess_return_pct"], 0.55)
            self.assertEqual(primary["max_drawdown_pct"], 5.0)
            self.assertEqual(primary["max_daily_gross_turnover_pct"], 30.0)
            self.assertEqual(primary["goal_status"], "met")
            self.assertEqual(context["current_risk"]["largest_symbol_weight_pct"], 62.5)
            self.assertFalse(context["current_risk"]["within_symbol_weight_reference"])

            record_external_flow(workspace, date(2026, 8, 19), "clear")
            self.assertEqual(excluded_external_flow_dates(workspace), set())

    def test_includes_non_universe_positions_in_largest_symbol_weight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name)
            runs = workspace / "reports" / "runs"
            write_run(runs, "open", "2026-08-20T09:00:00+09:00", 1_000_000, 100.0)
            write_run(runs, "close", "2026-08-20T15:30:00+09:00", 1_000_000, 101.0)
            close_path = runs / "close/account-before-order.json"
            close = json.loads(close_path.read_text(encoding="utf-8"))
            close["symbols"] = [
                {"symbol_id": "005930", "symbol_name": "삼성전자", "valuation_amount": 100_000},
                {"symbol_id": "000660", "symbol_name": "SK하이닉스", "valuation_amount": 100_000},
            ]
            close["non_universe_account_positions"] = [
                {"symbol_id": "999999", "symbol_name": "계좌외종목", "valuation_amount": 600_000}
            ]
            close_path.write_text(json.dumps(close), encoding="utf-8")

            context = build_account_performance_context(
                workspace_dir=workspace,
                runs_root=runs,
                started_at="2026-08-20T16:00:00+09:00",
                policy={**POLICY, "performance_review": {**POLICY["performance_review"], "primary_window_trading_days": 1}},
            )

            self.assertEqual(context["current_risk"]["largest_symbol_id"], "999999")
            self.assertEqual(context["current_risk"]["largest_symbol_weight_pct"], 75.0)

    def test_partial_fill_collection_does_not_publish_or_judge_turnover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name)
            runs = workspace / "reports" / "runs"
            write_run(runs, "open", "2026-08-20T09:00:00+09:00", 1_000_000, 100.0)
            write_run(
                runs,
                "close",
                "2026-08-20T15:30:00+09:00",
                1_010_000,
                101.0,
                fill={
                    "order_date": "20260820",
                    "order_id": "partial",
                    "symbol_id": "005930",
                    "direction": "buy",
                    "filled_amount": 400_000,
                },
                fill_status="partial",
            )
            policy = {
                **POLICY,
                "performance_review": {
                    **POLICY["performance_review"],
                    "primary_window_trading_days": 1,
                },
            }

            context = build_account_performance_context(
                workspace_dir=workspace,
                runs_root=runs,
                started_at="2026-08-20T16:00:00+09:00",
                policy=policy,
            )

            self.assertIsNone(context["latest_day"]["gross_turnover_pct"])
            self.assertEqual(context["latest_day"]["observed_gross_turnover_pct"], 40.0)
            primary = context["periods"]["primary"]
            self.assertIsNone(primary["max_daily_gross_turnover_pct"])
            self.assertEqual(primary["incomplete_turnover_dates"], ["2026-08-20"])
            self.assertEqual(primary["turnover_reference_breached_dates"], [])


if __name__ == "__main__":
    unittest.main()
