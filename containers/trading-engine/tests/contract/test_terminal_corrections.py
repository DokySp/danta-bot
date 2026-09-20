"""Broker-observed revisions remain authoritative after an order closes."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

from danta.application import Application
from danta.config import HumanRequired, aware_time
from danta.execution import Executor, FixtureBroker, OrderIntent
from danta.risk import evaluate_exit
from danta.store import Store
from tests.unit import test_strategy as helpers


class TerminalCorrections(unittest.TestCase):
    def test_zero_fill_has_no_execution_time_and_cost_uncertainty_invalidates_version(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime.now(timezone.utc)
            with Store(Path(directory)/"state.sqlite", mode="offline", account_identity=directory, initial_cash=D(10000)) as store:
                broker = FixtureBroker()
                executor = Executor(store, broker, mode="offline", authorize=lambda *_: None, preflight=lambda *_: None)
                intent = OrderIntent(run_id="evidence", plan_id="plan", thesis_id="thesis", instrument_id="TEST:AAA", side="BUY",
                    quantity=2, limit_price=D(100), expires_at=now+timedelta(seconds=120), reason="FIXTURE",
                    account_version=store.get("account_version"), policy_hash="fixture", reserve_cash=D(200), reserve_risk=D(20))
                order = executor.submit(intent, now)
                broker.fill(order["broker_id"], 0, D(100), D(0), now)
                executor.reconcile()
                self.assertIsNone(broker.orders[order["broker_id"]]["first_fill_at"])
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM observations WHERE kind='FIRST_FILL'").fetchone()[0], 0)
                first = now+timedelta(seconds=5)
                broker.fill(order["broker_id"], 2, D(100), D(0), first)
                executor.reconcile()
                evidence = json.loads(store.db.execute("SELECT payload FROM observations WHERE kind='FIRST_FILL'").fetchone()[0])
                self.assertEqual(aware_time(evidence["first_fill_at"]), first)
                cash, version = store.get("cash_krw"), store.get("account_version")
                observed = broker.orders[order["broker_id"]]
                observed.update(cumulative_fees=None, revision=observed["revision"]+1)
                executor.reconcile()
                self.assertFalse(store.get("costs_complete"))
                self.assertGreater(store.get("account_version"), version)
                self.assertEqual(store.get("cash_krw"), cash)
                self.assertEqual(store.quantity("TEST:AAA"), 2)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM journal WHERE kind='FILL_EVIDENCE_UPDATED'").fetchone()[0], 1)
                version = store.get("account_version")
                executor.reconcile()
                self.assertEqual(store.get("account_version"), version)

    def test_first_fill_time_can_be_corrected_without_replaying_cash_or_quantity(self):
        case = helpers.synthetic_case()
        profile,calendar,_ticks,now,*_ = case
        first_session = calendar.sessions[119]
        exact = first_session.opens_at+timedelta(minutes=21)
        observed = now
        thesis = helpers.synthetic_thesis(case,quantity=10).model_copy(update={
            "first_fill_at":None,"first_fill_session":None,"first_fill_time_quality":"UNKNOWN","max_holding_sessions":1})
        with tempfile.TemporaryDirectory() as directory:
            with Store(Path(directory)/"state.sqlite",mode="offline",account_identity=directory,initial_cash=D(1000000)) as store:
                broker = FixtureBroker()
                executor = Executor(store,broker,mode="offline",authorize=lambda *_:None,preflight=lambda *_:None)
                app = Application.__new__(Application)
                app.store,app.bundle = store,SimpleNamespace(now=observed,calendar=calendar)
                with store.transaction():
                    app._save_thesis(thesis)
                intent = OrderIntent(run_id="fill-time",plan_id="plan",thesis_id=thesis.thesis_id,instrument_id=thesis.instrument_id,
                    side="BUY",quantity=10,limit_price=D(10500),expires_at=exact+timedelta(seconds=60),reason="FIXTURE",
                    account_version=store.get("account_version"),policy_hash="fixture-policy",reserve_cash=D(105000),reserve_risk=D(5000))
                order = executor.submit(intent,exact-timedelta(seconds=60))
                broker.fill(order["broker_id"],10,D(10500),D(0),observed)
                revision = broker.orders[order["broker_id"]]
                revision.update(first_fill_at=None,fill_time_quality="FIRST_OBSERVED",cumulative_fees=None,
                                fill_session_id=first_session.session_id)
                executor.reconcile()
                app._sync_theses()
                provisional = app.theses()[0]
                self.assertEqual(provisional.first_fill_at,observed)
                self.assertEqual(provisional.first_fill_time_quality,"FIRST_OBSERVED")
                self.assertEqual(provisional.first_fill_session,first_session.session_id)
                self.assertFalse(store.get("costs_complete"))
                cash,version = store.get("cash_krw"),store.get("account_version")
                revision.update(first_fill_at=exact.isoformat(),fill_time_quality="EXACT",revision=revision["revision"]+1)
                executor.reconcile()
                app._sync_theses()
                corrected = app.theses()[0]
                self.assertEqual(corrected.first_fill_at,exact)
                self.assertEqual(corrected.first_fill_time_quality,"EXACT")
                self.assertGreater(store.get("account_version"),version)
                evidence = store.db.execute("SELECT available_at,payload FROM observations WHERE id=?",(f"first-fill:{thesis.thesis_id}",)).fetchone()
                self.assertEqual(aware_time(evidence[0]),observed)
                self.assertEqual(aware_time(json.loads(evidence[1])["first_fill_at"]),exact)
                self.assertFalse(store.get("costs_complete"))
                # Fee-only confirmation must not discard the previously exact clock.
                version = store.get("account_version")
                revision.update(first_fill_at=None,fill_time_quality="FIRST_OBSERVED",cumulative_fees="0",revision=revision["revision"]+1)
                executor.reconcile()
                app._sync_theses()
                self.assertTrue(store.get("costs_complete"))
                self.assertGreater(store.get("account_version"),version)
                self.assertEqual(app.theses()[0].first_fill_at,exact)
                self.assertEqual(app.theses()[0].first_fill_time_quality,"EXACT")
                quantity,version = store.quantity(thesis.instrument_id),store.get("account_version")
                executor.reconcile()
                app._sync_theses()
                self.assertEqual(store.get("account_version"),version)
                self.assertEqual(store.get("cash_krw"),cash)
                self.assertEqual(quantity,10)
                self.assertEqual(store.quantity(thesis.instrument_id),quantity)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM journal WHERE kind='CUMULATIVE_FILL'").fetchone()[0],1)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM journal WHERE kind='FILL_EVIDENCE_UPDATED'").fetchone()[0],2)
                holding = helpers.synthetic_holding(case,app.theses()[0],quantity=10)
                self.assertIn("EXIT_TIME_LIMIT",evaluate_exit(app.theses()[0],holding,case[7],calendar,now,profile).reasons)

    def test_increasing_quantity_requires_correction_for_decreasing_amounts(self):
        for notional, fees in ((250,1),(250,5),(500,1)):
            with self.subTest(notional=notional,fees=fees), tempfile.TemporaryDirectory() as directory:
                now = datetime.now(timezone.utc)
                with Store(Path(directory)/"state.sqlite",mode="offline",account_identity=directory,initial_cash=D(10000)) as store:
                    broker = FixtureBroker()
                    executor = Executor(store,broker,mode="offline",authorize=lambda *_:None,preflight=lambda *_:None)
                    intent = OrderIntent(run_id="decreasing-amount",plan_id="plan",thesis_id="thesis",instrument_id="TEST:AAA",side="BUY",quantity=10,
                        limit_price=D(100),expires_at=now+timedelta(seconds=120),reason="FIXTURE",account_version=store.get("account_version"),
                        policy_hash="fixture-policy",reserve_cash=D(1000),reserve_risk=D(100))
                    order = executor.submit(intent,now)
                    broker.fill(order["broker_id"],3,D(100),D(3),now)
                    executor.reconcile()
                    before = list(store.db.iterdump())
                    revised = broker.orders[order["broker_id"]]
                    revised.update(cumulative_quantity=5,cumulative_notional=str(notional),cumulative_fees=str(fees),revision=revised["revision"]+1)
                    with self.assertRaisesRegex(HumanRequired,"without broker correction evidence"):
                        executor.reconcile()
                    self.assertEqual(list(store.db.iterdump()),before)
                    revised["correction"] = True
                    executor.reconcile()
                    self.assertEqual(store.quantity("TEST:AAA"),5)
                    self.assertEqual(D(store.get("cash_krw")),D(10000-notional-fees))

    def test_missing_fees_preserve_last_amount_until_actual_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime.now(timezone.utc)
            with Store(Path(directory)/"state.sqlite",mode="offline",account_identity=directory,initial_cash=D(10000)) as store:
                broker = FixtureBroker()
                executor = Executor(store,broker,mode="offline",authorize=lambda *_:None,preflight=lambda *_:None)
                intent = OrderIntent(run_id="fee-settlement",plan_id="plan",thesis_id="thesis",instrument_id="TEST:AAA",side="BUY",quantity=10,
                    limit_price=D(100),expires_at=now+timedelta(seconds=120),reason="FIXTURE",account_version=store.get("account_version"),
                    policy_hash="fixture-policy",reserve_cash=D(1000),reserve_risk=D(100))
                order = executor.submit(intent,now)
                broker.fill(order["broker_id"],3,D(100),D(3),now)
                executor.reconcile()
                broker.fill(order["broker_id"],5,D(100),D(0),now)
                broker.orders[order["broker_id"]]["cumulative_fees"] = None
                executor.reconcile()
                self.assertEqual(store.get("cash_krw"),"9497")
                self.assertFalse(store.get("costs_complete"))
                self.assertTrue(store.get("unconfirmed_cost:"+order["id"]))
                before = list(store.db.iterdump())
                with self.assertRaisesRegex(HumanRequired,"without broker correction evidence"):
                    store.apply_cumulative_fill(order["id"],quantity=6,notional=D(400),fees=D(1),
                                                revision=100,observed_at=now.isoformat())
                self.assertEqual(list(store.db.iterdump()),before)
                broker.fill(order["broker_id"],5,D(100),D(1),now)
                executor.reconcile()
                self.assertEqual(store.quantity("TEST:AAA"),5)
                self.assertEqual(store.get("cash_krw"),"9499")
                self.assertTrue(store.get("costs_complete"))
                self.assertFalse(store.get("unconfirmed_cost:"+order["id"]))

    def test_closed_confirmed_order_accepts_later_correction_and_missing_history(self):
        for quantity in (10,3):
            with self.subTest(quantity=quantity), tempfile.TemporaryDirectory() as directory:
                now = datetime.now(timezone.utc)
                with Store(Path(directory)/"state.sqlite",mode="offline",account_identity=directory,initial_cash=D(10000)) as store:
                    broker = FixtureBroker()
                    executor = Executor(store,broker,mode="offline",authorize=lambda *_:None,preflight=lambda *_:None)
                    intent = OrderIntent(run_id="correction-run",plan_id="plan",thesis_id="thesis",instrument_id="TEST:AAA",side="BUY",quantity=10,
                        limit_price=D(100),expires_at=now+timedelta(seconds=120),reason="FIXTURE",account_version=store.get("account_version"),
                        policy_hash="fixture-policy",reserve_cash=D(1000),reserve_risk=D(100))
                    order = executor.submit(intent,now)
                    broker.fill(order["broker_id"],quantity,D(99),D(1),now)
                    executor.reconcile()
                    if quantity < 10:
                        executor.cancel(order["id"],now)
                        executor.reconcile()
                    expected_state = "FILLED" if quantity == 10 else "PARTIAL_CANCELED"
                    self.assertEqual(store.order(order["id"])["state"],expected_state)
                    self.assertTrue(store.get("costs_complete"))
                    self.assertFalse(store.get("unconfirmed_cost:"+order["id"],False))
                    before = D(store.get("cash_krw"))
                    revised = broker.orders[order["broker_id"]]
                    revised.update(cumulative_notional=str(quantity*98),cumulative_fees="2",revision=revised["revision"]+1,correction=True)
                    executor.reconcile()
                    self.assertEqual(D(store.get("cash_krw")),before+D(quantity)-1)
                    self.assertEqual(store.quantity("TEST:AAA"),quantity)
                    self.assertEqual(store.order(order["id"])["state"],expected_state)
                    self.assertEqual(store.reservation(),0)
                    corrections = list(store.db.execute("SELECT payload FROM journal WHERE kind='FILL_CORRECTION'"))
                    self.assertEqual(len(corrections),1)
                    self.assertEqual(json.loads(corrections[0][0])["fee_delta_krw"],"1")
                    cash = store.get("cash_krw")
                    executor.reconcile()
                    self.assertEqual(store.get("cash_krw"),cash)
                    self.assertEqual(len(list(store.db.execute("SELECT 1 FROM journal WHERE kind='FILL_CORRECTION'"))),1)
                    self.assertEqual(executor.reconcile({"complete":True,"ownership_complete":True,"orders":[],"strategy_quantities":{"TEST:AAA":quantity}})["status"],"RECONCILED")
                    self.assertEqual(store.order(order["id"])["state"],expected_state)

    def test_canceled_confirmed_order_accepts_later_cumulative_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime.now(timezone.utc)
            with Store(Path(directory)/"state.sqlite",mode="offline",account_identity=directory,initial_cash=D(10000)) as store:
                broker = FixtureBroker()
                executor = Executor(store,broker,mode="offline",authorize=lambda *_:None,preflight=lambda *_:None)
                intent = OrderIntent(run_id="late-fill",plan_id="plan",thesis_id="thesis",instrument_id="TEST:AAA",side="BUY",quantity=10,
                    limit_price=D(100),expires_at=now+timedelta(seconds=120),reason="FIXTURE",account_version=store.get("account_version"),
                    policy_hash="fixture-policy",reserve_cash=D(1000),reserve_risk=D(100))
                order = executor.submit(intent,now)
                broker.fill(order["broker_id"],3,D(99),D(1),now)
                executor.reconcile()
                executor.cancel(order["id"],now)
                executor.reconcile()
                broker.fill(order["broker_id"],5,D(99),D(2),now)
                broker.orders[order["broker_id"]]["state"] = "CANCELED"
                executor.reconcile()
                self.assertEqual(store.quantity("TEST:AAA"),5)
                self.assertEqual(store.get("cash_krw"),"9503")
                self.assertEqual(store.order(order["id"])["state"],"PARTIAL_CANCELED")
                self.assertEqual(store.reservation(),0)


if __name__ == "__main__":
    unittest.main()
