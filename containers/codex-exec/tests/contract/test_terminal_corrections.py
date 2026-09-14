"""Broker-observed revisions remain authoritative after an order closes."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from danta.execution import Executor, FixtureBroker, OrderIntent
from danta.store import Store


class TerminalCorrections(unittest.TestCase):
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
