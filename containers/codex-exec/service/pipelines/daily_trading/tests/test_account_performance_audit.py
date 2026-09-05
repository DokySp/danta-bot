from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ..scripts.account_performance import KST
from ..scripts.account_performance_audit import build_performance_audit


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_run(
    runs: Path,
    run_id: str,
    started_at: str,
    total: int,
    index_value: float,
    price: int,
    *,
    fill: dict | None = None,
    fills: list[dict] | None = None,
    previous_fill: dict | None = None,
    fill_status: str = "success",
    fill_skipped: bool = False,
    decision_price: int | None = None,
    generated_at: str | None = None,
    include_position: bool = True,
) -> None:
    run = runs / run_id
    account = {
        "status": "success",
        "started_at": started_at,
        "account_summary": {
            "total_evaluation_amount": total,
            "securities_valuation_amount": price * 9,
        },
        "symbols": (
            [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "current_live_holding_quantity": 9,
                    "current_price": price,
                    "valuation_amount": price * 9,
                }
            ]
            if include_position
            else []
        ),
    }
    if generated_at:
        account["generated_at"] = generated_at
    write_json(
        run / "account-before-order.json",
        account,
    )
    market = {"indexes": [{"symbol": "KOSPI", "status": "success", "value": index_value}]}
    if generated_at:
        market["generated_at"] = generated_at
    write_json(
        run / "market-index-snapshot.json",
        market,
    )
    if decision_price is not None:
        decision = {
            "symbols": [
                {
                    "symbol_id": "005930",
                    "price": {"current_or_last": decision_price},
                }
            ]
        }
        if generated_at:
            decision["generated_at"] = generated_at
        write_json(run / "decision-brief.json", decision)
    current_fills = list(fills) if fills is not None else [fill] if fill else []
    payload = {
        "status": fill_status,
        "skipped": fill_skipped,
        "fill_scope": "account",
        "fills": current_fills,
    }
    if generated_at:
        payload["generated_at"] = generated_at
    if previous_fill:
        payload["previous_session"] = {"fills": [previous_fill]}
    write_json(run / "today-fills.json", payload)


class AccountPerformanceAuditTest(unittest.TestCase):
    def test_completed_day_backtest_includes_overnight_and_excludes_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name)
            runs = workspace / "reports" / "runs"
            buy = {
                "filled_at": "2026-08-18T10:00:00+09:00",
                "order_date": "20260818",
                "order_id": "buy-1",
                "symbol_id": "005930",
                "direction": "buy",
                "filled_amount": 100,
            }
            sell = {
                "filled_at": "2026-08-19T10:00:00+09:00",
                "order_date": "20260819",
                "order_id": "sell-1",
                "symbol_id": "005930",
                "direction": "sell",
                "filled_amount": 200,
            }
            write_run(runs, "d18-open", "2026-08-18T08:00:00+09:00", 1_000, 100, 100, fill=buy)
            write_run(runs, "d18-close", "2026-08-18T15:15:00+09:00", 1_050, 102, 105, fill=buy)
            write_run(runs, "d19-open", "2026-08-19T08:00:00+09:00", 1_100, 105, 110)
            write_run(runs, "d19-close", "2026-08-19T15:15:00+09:00", 1_210, 110, 120, previous_fill=sell)
            write_run(runs, "sunday", "2026-08-23T22:00:00+09:00", 1_210, 110, 120)
            write_run(runs, "provisional-open", "2026-09-01T08:00:00+09:00", 1_220, 111, 121)

            audit = build_performance_audit(
                runs_root=runs,
                workspace_dir=workspace,
                cutoff=datetime(2026, 9, 1, 11, 30, tzinfo=KST),
                window=2,
            )

            latest = audit["latest_window"]
            self.assertEqual(latest["start_date"], "2026-08-18")
            self.assertEqual(latest["end_date"], "2026-08-19")
            self.assertEqual(latest["strategies"]["actual"]["return_pct"], 21.0)
            self.assertEqual(latest["benchmark_return_pct"], 10.0)
            self.assertEqual(latest["strategies"]["actual"]["kospi_excess_return_pct"], 11.0)
            self.assertEqual(latest["strategies"]["no_trade"]["return_pct"], 18.0)
            self.assertEqual(latest["actual_minus_no_trade_return_pct"], 3.0)
            self.assertEqual(latest["strategies"]["actual"]["gross_turnover_amount"], 300)
            self.assertEqual(latest["strategies"]["actual"]["gross_turnover_pct"], 30.0)
            self.assertEqual(audit["data_quality"]["insufficient_snapshot_dates"], ["2026-08-23"])
            self.assertEqual(audit["data_quality"]["provisional_dates"], ["2026-09-01"])
            self.assertEqual(audit["rolling_windows"]["actual_beats_both_rate_pct"], 100.0)

    def test_reports_drawdown_from_completed_daily_levels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name)
            runs = workspace / "reports" / "runs"
            write_run(runs, "d18-open", "2026-08-18T08:00:00+09:00", 1_000, 100, 100)
            write_run(runs, "d18-close", "2026-08-18T19:40:00+09:00", 1_100, 110, 110)
            write_run(runs, "d19-open", "2026-08-19T08:00:00+09:00", 1_100, 110, 110)
            write_run(runs, "d19-close", "2026-08-19T19:40:00+09:00", 990, 99, 99)

            audit = build_performance_audit(
                runs_root=runs,
                workspace_dir=workspace,
                cutoff=datetime(2026, 8, 20, 8, 0, tzinfo=KST),
                window=2,
            )

            latest = audit["latest_window"]
            self.assertEqual(latest["strategies"]["actual"]["return_pct"], -1.0)
            self.assertEqual(latest["strategies"]["actual"]["max_drawdown_pct"], 10.0)

    def test_excludes_weekend_even_with_multiple_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name)
            runs = workspace / "reports" / "runs"
            write_run(runs, "sun-open", "2026-08-23T08:00:00+09:00", 1_000, 100, 100)
            write_run(runs, "sun-close", "2026-08-23T19:40:00+09:00", 1_100, 110, 110)

            audit = build_performance_audit(
                runs_root=runs,
                workspace_dir=workspace,
                cutoff=datetime(2026, 8, 24, 8, 0, tzinfo=KST),
                window=1,
            )

            self.assertEqual(audit["data_quality"]["completed_trading_days"], 0)
            self.assertEqual(audit["data_quality"]["non_trading_observation_dates"], ["2026-08-23"])
            self.assertIsNone(audit["latest_window"])

    def test_no_trade_uses_account_price_and_rejects_missing_closing_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name)
            runs = workspace / "reports" / "runs"
            write_run(runs, "d18-open", "2026-08-18T08:00:00+09:00", 1_000, 100, 100, decision_price=100)
            write_run(runs, "d18-close", "2026-08-18T19:40:00+09:00", 1_090, 109, 110, decision_price=90)

            audit = build_performance_audit(
                runs_root=runs,
                workspace_dir=workspace,
                cutoff=datetime(2026, 8, 18, 20, 0, tzinfo=KST),
                window=1,
            )
            self.assertEqual(audit["latest_window"]["strategies"]["no_trade"]["return_pct"], 9.0)

            write_run(runs, "d19-open", "2026-08-19T08:00:00+09:00", 1_090, 109, 110)
            write_run(
                runs,
                "d19-close",
                "2026-08-19T19:40:00+09:00",
                1_090,
                109,
                110,
                include_position=False,
            )
            audit = build_performance_audit(
                runs_root=runs,
                workspace_dir=workspace,
                cutoff=datetime(2026, 8, 19, 20, 0, tzinfo=KST),
                window=1,
            )
            latest = audit["latest_window"]
            self.assertEqual(latest["no_trade_missing_price_symbols"], ["005930"])
            self.assertIsNone(latest["strategies"]["no_trade"]["return_pct"])

    def test_no_trade_keeps_price_return_across_external_flow_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name)
            runs = workspace / "reports" / "runs"
            write_run(runs, "d18-open", "2026-08-18T08:00:00+09:00", 1_000, 100, 100)
            write_run(runs, "d18-close", "2026-08-18T19:40:00+09:00", 1_090, 110, 110)
            write_run(runs, "d19-open", "2026-08-19T08:00:00+09:00", 1_090, 110, 110)
            write_run(runs, "d19-close", "2026-08-19T19:40:00+09:00", 1_189, 121, 121)
            ledger = workspace / "memory" / "account-performance" / "external-flows.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text('{"session_date":"2026-08-18","action":"exclude"}\n', encoding="utf-8")

            audit = build_performance_audit(
                runs_root=runs,
                workspace_dir=workspace,
                cutoff=datetime(2026, 8, 20, 8, 0, tzinfo=KST),
                window=2,
            )
            self.assertEqual(audit["latest_window"]["strategies"]["no_trade"]["return_pct"], 18.9)

    def test_turnover_preserves_distinct_rows_and_requires_complete_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name)
            runs = workspace / "reports" / "runs"
            common = {
                "order_date": "20260818",
                "order_id": "order-1",
                "symbol_id": "005930",
                "direction": "buy",
            }
            write_run(
                runs,
                "d18-open",
                "2026-08-18T08:00:00+09:00",
                1_000,
                100,
                100,
                fills=[
                    dict(common, filled_at="2026-08-18T09:00:00+09:00", filled_amount=100),
                    dict(common, filled_at="2026-08-18T09:01:00+09:00", filled_amount=200),
                ],
            )
            write_run(runs, "d18-close", "2026-08-18T19:40:00+09:00", 1_000, 100, 100)
            audit = build_performance_audit(
                runs_root=runs,
                workspace_dir=workspace,
                cutoff=datetime(2026, 8, 18, 20, 0, tzinfo=KST),
                window=1,
            )
            self.assertEqual(audit["latest_window"]["strategies"]["actual"]["gross_turnover_amount"], 300)

            write_run(
                runs,
                "d18-partial",
                "2026-08-18T19:45:00+09:00",
                1_000,
                100,
                100,
                fill_status="partial",
            )
            audit = build_performance_audit(
                runs_root=runs,
                workspace_dir=workspace,
                cutoff=datetime(2026, 8, 18, 20, 0, tzinfo=KST),
                window=1,
            )
            latest = audit["latest_window"]
            self.assertEqual(latest["turnover_collection_status"], "partial")
            self.assertIsNone(latest["strategies"]["actual"]["gross_turnover_amount"])
            self.assertEqual(latest["coverage_status"], "partial")

    def test_excludes_artifacts_generated_after_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name)
            runs = workspace / "reports" / "runs"
            write_run(runs, "open", "2026-08-18T08:00:00+09:00", 1_000, 100, 100)
            write_run(
                runs,
                "future-close",
                "2026-08-18T19:30:00+09:00",
                1_100,
                110,
                110,
                generated_at="2026-08-18T19:31:00+09:00",
            )
            audit = build_performance_audit(
                runs_root=runs,
                workspace_dir=workspace,
                cutoff=datetime(2026, 8, 18, 19, 30, tzinfo=KST),
                window=1,
            )
            self.assertIsNone(audit["latest_window"])
            self.assertEqual(audit["data_quality"]["provisional_dates"], ["2026-08-18"])


if __name__ == "__main__":
    unittest.main()
