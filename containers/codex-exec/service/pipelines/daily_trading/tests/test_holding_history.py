#!/usr/bin/env python3
"""Tests for broker-confirmed daily-trading holding history."""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ..holding_history import append_holding_history_from_run


class HoldingHistoryTest(unittest.TestCase):
    def test_only_broker_confirmed_fills_change_holdings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            context = SimpleNamespace(run_id="run-1", started_at="2026-07-15T14:30:00+09:00")
            run_dir = workspace / "reports" / "runs" / context.run_id
            run_dir.mkdir(parents=True)
            (run_dir / "execution.json").write_text(
                json.dumps(
                    {
                        "request_type": "real-submit",
                        "orders": [
                            {
                                "symbol_id": "005930",
                                "symbol_name": "삼성전자",
                                "direction": "buy",
                                "result": "submitted",
                                "order_path": "immediate",
                                "order_api": "order_cash",
                                "current_live_holding_quantity": 10,
                                "validated_order_quantity": 1,
                                "order_or_reservation_id": "filled-1",
                                "broker_reconciliation": {"status": "filled", "filled_quantity": 1},
                            },
                            {
                                "symbol_id": "042660",
                                "symbol_name": "한화오션",
                                "direction": "buy",
                                "result": "submitted",
                                "order_path": "immediate",
                                "order_api": "order_cash",
                                "current_live_holding_quantity": 0,
                                "validated_order_quantity": 1,
                                "order_or_reservation_id": "rejected-1",
                                "broker_reconciliation": {"status": "rejected", "filled_quantity": 0},
                            },
                            {
                                "symbol_id": "000660",
                                "symbol_name": "SK하이닉스",
                                "direction": "sell",
                                "result": "submitted",
                                "order_path": "immediate",
                                "order_api": "order_cash",
                                "current_live_holding_quantity": 3,
                                "validated_order_quantity": 2,
                                "order_or_reservation_id": "partial-1",
                                "broker_reconciliation": {"status": "partially_filled", "filled_quantity": 1},
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            csv_path = workspace / "holding-changes.csv"
            previous = os.environ.get("HOLDING_HISTORY_CSV")
            os.environ["HOLDING_HISTORY_CSV"] = str(csv_path)
            try:
                written = append_holding_history_from_run(workspace, context)
            finally:
                if previous is None:
                    os.environ.pop("HOLDING_HISTORY_CSV", None)
                else:
                    os.environ["HOLDING_HISTORY_CSV"] = previous

            self.assertEqual(written, 2)
            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["symbol_id"] for row in rows], ["005930", "000660"])
            self.assertEqual((rows[0]["old_quantity"], rows[0]["new_quantity"]), ("10", "11"))
            self.assertEqual((rows[1]["old_quantity"], rows[1]["new_quantity"]), ("3", "2"))
            self.assertEqual(rows[1]["submitted_quantity"], "1")


if __name__ == "__main__":
    unittest.main()
