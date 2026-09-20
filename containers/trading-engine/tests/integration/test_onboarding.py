"""Existing account adoption is ledger provenance, never synthetic order history."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from danta.config import HumanRequired, aware_time
from danta.models import InvestmentThesis
from danta.store import Store


class AccountAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite"
        self.store = self.open_store()
        self.observed_at = "2026-09-21T01:00:00+00:00"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def open_store(self):
        return Store(self.path, mode="offline", account_identity=self.tmp.name,
                     initial_cash=D("999999"))

    def position(self, symbol="TEST:AAA", *, as_model=False):
        thesis = InvestmentThesis(thesis_id=f"inherited-{symbol}", instrument_id=symbol,
            event_ids=[], source_uris=["synthetic:account-snapshot"], economic_path="Approved account adoption",
            horizon_case="Protect the existing position", counterevidence="Historical entry is unknown",
            invalidation_case="Protection threshold", initial_stop=D("90"), current_stop=D("90"),
            initial_r_price=D("10"), average_entry=D("100"), planned_quantity=2, risk_budget=D("20"),
            strategy_hash="strategy-hash", policy_hash="config-hash", created_at=self.observed_at,
            first_fill_time_quality="UNKNOWN", origin="inherited", adopted_at=self.observed_at,
            adopted_session="2026-09-21")
        return {"instrument_id": symbol, "quantity": 2, "valuation_price": D("100"),
                "thesis": thesis if as_model else thesis.model_dump(mode="json")}

    def adopt(self, positions=None, **changes):
        arguments = {"snapshot_id": "snapshot-1", "cash": D("1200.50"), "observed_at": self.observed_at,
                     "source": "synthetic:account-snapshot", "config_hash": "config-hash", "strategy_hash": "strategy-hash"}
        arguments.update(changes)
        return self.store.import_positions(positions=[self.position()] if positions is None else positions, **arguments)

    def state(self):
        return list(self.store.db.iterdump())

    def test_adoption_records_valuation_and_provenance_without_orders_or_fills(self):
        self.assertTrue(self.adopt([self.position(), self.position("TEST:BBB", as_model=True)]))
        self.assertEqual(self.store.get("cash_krw"), "1200.50")
        self.assertEqual(self.store.get("account_version"), 1)
        self.assertFalse(self.store.get("reconciled"))
        self.assertEqual([(row["quantity"], row["cost_basis"]) for row in self.store.holdings()], [(2, "200"), (2, "200")])
        for table in ("intents", "observations", "outbox"):
            self.assertEqual(self.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        rows = self.store.db.execute("SELECT created_at,kind,payload FROM journal WHERE run_id='snapshot-1'").fetchall()
        self.assertEqual([row["kind"] for row in rows], ["INHERITED_POSITION", "INHERITED_POSITION", "ACCOUNT_ADOPTED"])
        self.assertTrue(all(json.loads(row["payload"])["observed_at"] == self.observed_at for row in rows))
        self.assertTrue(all(row["created_at"] != self.observed_at for row in rows))
        for row in rows[:2]:
            self.assertEqual(json.loads(row["payload"])["cost_basis_source"], "ADOPTION_VALUATION")
        thesis = InvestmentThesis.model_validate_json(self.store.db.execute("SELECT payload FROM theses LIMIT 1").fetchone()[0])
        self.assertIsNone(thesis.first_fill_at)
        self.assertIsNone(thesis.first_fill_session)
        self.assertEqual(thesis.first_fill_time_quality, "UNKNOWN")

    def test_identical_snapshot_is_noop_even_after_ledger_changes(self):
        self.adopt()
        with self.store.transaction():
            self.store.set("cash_krw", "800")
            self.store.set("reconciled", True)
            self.store.bump_version()
        before = self.state()
        self.assertFalse(self.adopt())
        self.assertEqual(self.state(), before)

    def test_same_snapshot_changed_payload_rejects_without_writes(self):
        self.adopt()
        for change in ({"cash": D("1201")}, {"source": "another-source"},
                       {"observed_at": "2026-09-21T01:01:00+00:00"}):
            with self.subTest(change=change):
                before = self.state()
                position = self.position()
                if "observed_at" in change:
                    position["thesis"]["adopted_at"] = change["observed_at"]
                with self.assertRaisesRegex(ValueError, "DUPLICATE_SNAPSHOT_DIFFERENT_BODY"):
                    self.adopt([position], **change)
                self.assertEqual(self.state(), before)
        position = self.position()
        position["thesis"]["counterevidence"] = "Changed material"
        with self.assertRaisesRegex(ValueError, "DUPLICATE_SNAPSHOT_DIFFERENT_BODY"):
            self.adopt([position])

    def test_invalid_snapshot_and_forged_model_leave_ledger_unchanged(self):
        before = self.state()
        for field, invalids in {"cash": [True, D("-1"), D("NaN"), D("Infinity"), "invalid"],
                               "snapshot_id": ["", None], "source": [""], "config_hash": [""],
                               "strategy_hash": [""], "observed_at": ["2026-09-21T01:00:00"]}.items():
            for invalid in invalids:
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    self.adopt(**{field: invalid})
                self.assertEqual(self.state(), before)
        for field, invalids in {"quantity": [True, 0, -1, 1.5], "valuation_price": [True, 0, -1, "NaN", "invalid"],
                               "instrument_id": ["", "TEST:MISMATCH"]}.items():
            for invalid in invalids:
                position = self.position()
                position[field] = invalid
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    self.adopt([position])
                self.assertEqual(self.state(), before)
        for update in ({"thesis_id": ""}, {"policy_hash": "mismatch"}, {"strategy_hash": "mismatch"},
                       {"first_fill_time_quality": "FIRST_OBSERVED"}, {"first_fill_time_quality": "EXACT"},
                       {"first_fill_at": aware_time(self.observed_at), "first_fill_session": "2026-09-21"},
                       {"origin": "strategy_entry", "adopted_at": None, "adopted_session": None},
                       {"adopted_at": aware_time("2026-09-21T01:01:00+00:00")},
                       {"average_entry": D("101")}, {"planned_quantity": 3}, {"risk_budget": D("-1")}):
            position = self.position(as_model=True)
            position["thesis"] = position["thesis"].model_copy(update=update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.adopt([position])
            self.assertEqual(self.state(), before)

    def test_duplicate_stocks_and_thesis_ids_are_rejected(self):
        first = self.position()
        second = self.position("TEST:BBB")
        second["thesis"]["thesis_id"] = first["thesis"]["thesis_id"]
        for positions in ([first, copy.deepcopy(first)], [first, second]):
            before = self.state()
            with self.assertRaises(ValueError):
                self.adopt(positions)
            self.assertEqual(self.state(), before)

    def test_transaction_failure_rolls_back_holdings_theses_meta_and_events(self):
        original = self.store.event
        def fail_on_account_event(run_id, kind, payload, **kwargs):
            if kind == "ACCOUNT_ADOPTED":
                raise RuntimeError("simulated commit-path failure")
            return original(run_id, kind, payload, **kwargs)
        before = self.state()
        with patch.object(self.store, "event", side_effect=fail_on_account_event):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                self.adopt([self.position(), self.position("TEST:BBB")])
        self.assertEqual(self.state(), before)
        self.assertTrue(self.adopt())

    def test_nonempty_ledger_and_new_snapshot_cannot_be_reset(self):
        with self.store.transaction():
            self.store.db.execute("INSERT INTO holdings VALUES ('TEST:OLD','external','old',0,'0')")
        before = self.state()
        with self.assertRaisesRegex(HumanRequired, "EMPTY_LEDGER"):
            self.adopt()
        self.assertEqual(self.state(), before)
        with self.store.transaction():
            self.store.db.execute("DELETE FROM holdings")
            self.store.db.execute("INSERT INTO intents(id,idempotency_key,plan_id,thesis_id,instrument_id,side,quantity,state,account_version,policy_hash,reserve_cash,reserve_risk,payload) VALUES ('old','old','old','old','TEST:OLD','BUY',1,'CANCELED',0,'old','0','0','{}')")
        before = self.state()
        with self.assertRaisesRegex(HumanRequired, "EMPTY_LEDGER"):
            self.adopt()
        self.assertEqual(self.state(), before)

    def test_cash_only_adoption_cannot_be_replaced_and_survives_restart(self):
        self.assertTrue(self.adopt([], cash=D(0)))
        before = self.state()
        with self.assertRaisesRegex(HumanRequired, "ALREADY_ADOPTED"):
            self.adopt([], snapshot_id="snapshot-2", cash=D("500"))
        self.assertEqual(self.state(), before)
        self.store.close()
        self.store = self.open_store()
        self.assertEqual(self.state(), before)
        self.assertEqual(self.store.get("cash_krw"), "0")
        self.assertFalse(self.adopt([], cash=D(0)))

    def test_positions_thesis_and_snapshot_survive_restart(self):
        self.adopt()
        before = self.state()
        self.store.close()
        self.store = self.open_store()
        self.assertEqual(self.state(), before)
        self.assertFalse(self.adopt())
        self.assertEqual(self.store.quantity("TEST:AAA"), 2)
        self.assertFalse(self.store.get("reconciled"))


if __name__ == "__main__":
    unittest.main()
