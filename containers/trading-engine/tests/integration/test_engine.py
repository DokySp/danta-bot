"""Synthetic ledger and workflow regression checks; no account or network access."""
from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import unittest
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
import yaml

from danta.application import Application, MarketBundle, fixture_decision
from danta.config import ConfigurationError, HumanRequired, ROOT, canonical, load_config, utcnow
from danta.execution import Executor, FixtureBroker, OrderIntent, needed_quantity
from danta.store import Store


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_dir = self.root / "config"
        shutil.copytree(ROOT / "config", self.config_dir, ignore=shutil.ignore_patterns("secrets.yaml"))
        app = self.config_dir / "app.yaml"
        app.write_text(app.read_text().replace("state_dir: ./var/offline/research", f"state_dir: {self.root / 'state'}"))
        self.config = load_config(self.config_dir)
        self.data = json.loads((ROOT / "tests/fixtures/offline-e2e.json").read_text())
        self.bundle = MarketBundle(self.data, self.config.research, mode="offline")
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.tmp.cleanup()

    def store(self, *, name="db", identity="test-account"):
        store = Store(self.root / name / "state.sqlite", mode="offline", account_identity=self.tmp.name + identity, initial_cash=D("10000000"))
        self.stores.append(store)
        return store

    def executor(self, broker=None):
        store, broker = self.store(), broker or FixtureBroker()
        return Executor(store, broker, mode="offline", authorize=lambda *_: None, preflight=lambda *_: None)

    def intent(self, executor, *, side="BUY", quantity=10, plan="plan-1"):
        return OrderIntent(run_id="test-run", plan_id=plan, thesis_id="thesis-1", instrument_id="TEST:AAA", side=side,
            quantity=quantity, limit_price=D("100") if side == "BUY" else None,
            expires_at=self.bundle.now + timedelta(seconds=120) if side == "BUY" else None,
            reason="TEST", account_version=executor.store.get("account_version"), policy_hash="synthetic-policy",
            reserve_cash=D(quantity * 100) if side == "BUY" else D(0), reserve_risk=D(quantity * 10) if side == "BUY" else D(0))

    def test_O01_I01_offline_end_to_end_without_network(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            app = Application(self.config, self.bundle)
            try:
                result = app.review(request_key="full-review-1")
                self.assertEqual(result["run_status"], "COMPLETE")
                self.assertEqual(result["order_status"], "FIXTURE_FILLED")
                self.assertEqual(result["provenance"], "FIXTURE_ONLY")
                self.assertEqual(result["performance_status"], "STRATEGY_UNPROVEN")
                self.assertGreater(app.store.quantity("TEST:AAA"), 0)
                held = app.store.quantity("TEST:AAA")
                repeat = app.review(request_key="full-review-1")
                self.assertEqual(repeat["run_id"], result["run_id"])
                app.review(request_key="full-review-2")
                self.assertEqual(app.store.quantity("TEST:AAA"), held)
                self.assertEqual(app.broker.submissions, 1)
                self.assertEqual(len(app.store.holdings()), 1)
            finally:
                app.close()

    def test_O02_strict_config_rejects_duplicate_unknown_bool_and_ratio(self):
        app_file = self.config_dir / "app.yaml"
        original = app_file.read_text()
        for bad in [original + "schema_version: 1\n", original + "unknown: value\n", original.replace("enabled: false", 'enabled: "false"', 1)]:
            app_file.write_text(bad)
            with self.assertRaises(ConfigurationError):
                load_config(self.config_dir)
        app_file.write_text(original)
        strategy_file = self.config_dir / "strategy.yaml"
        strategy_file.write_text(strategy_file.read_text().replace('entry_risk_fraction: "0.0025"', 'entry_risk_fraction: "1.01"'))
        with self.assertRaises(ConfigurationError):
            load_config(self.config_dir)

    def test_stale_and_missing_holding_quote_never_certify_nav_or_disable_clock(self):
        app = Application(self.config, self.bundle)
        try:
            app.review(request_key="buy-before-missing-data")
            symbol = "TEST:AAA"
            app.bundle.now += timedelta(seconds=60)
            self.assertFalse(app.portfolio().complete)
            self.assertEqual(app.record_nav()["coverage"], "INSUFFICIENT_COVERAGE")
            self.assertEqual(app.protect()[0]["action"], "MONITOR_DEGRADED")
            del app.bundle.quotes[symbol]
            self.assertEqual(app.portfolio().holdings[0].valuation_quality, "MISSING")
            self.assertEqual(app.protect()[0]["action"], "MONITOR_DEGRADED")
        finally:
            app.close()

    def test_future_received_quote_cannot_pass_dispatch_preflight(self):
        app = Application(self.config, self.bundle)
        try:
            self.bundle.quotes["TEST:AAA"] = self.bundle.quotes["TEST:AAA"].model_copy(update={"received_at": self.bundle.now + timedelta(seconds=1)})
            with self.assertRaisesRegex(ValueError, "MONITOR_DEGRADED"):
                app._preflight(self.intent(app.executor), self.bundle.now)
            self.assertEqual(app.broker.submissions, 0)
        finally:
            app.close()

    def test_O03_I04_mode_flip_is_not_authority(self):
        app_file = self.config_dir / "app.yaml"
        app_file.write_text(app_file.read_text().replace("mode: offline", "mode: live"))
        live = load_config(self.config_dir)
        broker = FixtureBroker()
        with self.assertRaises(HumanRequired):
            Application(live, self.bundle, broker=broker)
        self.assertEqual(broker.submissions, 0)
        self.assertIsNone(live.data["strategy"]["strategy"]["live_mandate"]["capital_krw"])

    def test_O04_policy_frozen_and_changed_config_rejected(self):
        original = self.config.config_hash
        self.config.app["execution"]["enabled"] = True
        self.assertFalse(self.config.app["execution"]["enabled"])
        app_file = self.config_dir / "app.yaml"
        app_file.write_text(app_file.read_text().replace("timeout_seconds: 180", "timeout_seconds: 181"))
        with self.assertRaises(HumanRequired):
            self.config.assert_current()
        self.assertEqual(self.config.config_hash, original)

    def test_O05_request_dedup_different_payload_rejected(self):
        store = self.store()
        first = store.accept_request("trusted:route:100", {"text": "/review"})
        repeat = store.accept_request("trusted:route:100", {"text": "/review"})
        self.assertEqual(first[0], repeat[0])
        self.assertFalse(repeat[1])
        with self.assertRaises(ValueError):
            store.accept_request("trusted:route:100", {"text": "/pause"})

    def test_O06_second_profile_cannot_be_second_writer(self):
        self.store(name="profile-a")
        with self.assertRaises(HumanRequired):
            self.store(name="profile-b")

    def test_O07_O08_fixed_target_and_reserved_idempotency(self):
        self.assertEqual(needed_quantity(10, 3, 15), 2)
        ex = self.executor()
        intent = self.intent(ex)
        first = ex.submit(intent, self.bundle.now)
        repeat = ex.submit(intent, self.bundle.now)
        self.assertEqual(first["id"], repeat["id"])
        self.assertEqual(ex.broker.submissions, 1)
        self.assertEqual(ex.store.reservation(), D(1000))

    def test_O10_O11_timeout_is_unknown_and_never_resent(self):
        class LostResponse(FixtureBroker):
            def submit(self, intent):
                super().submit(intent)
                raise TimeoutError("synthetic lost response")
        ex = self.executor(LostResponse())
        intent = self.intent(ex)
        self.assertEqual(ex.submit(intent, self.bundle.now)["state"], "UNKNOWN")
        self.assertEqual(ex.submit(intent, self.bundle.now)["state"], "UNKNOWN")
        self.assertEqual(ex.broker.submissions, 1)
        with self.assertRaises(HumanRequired):
            ex.reconcile()

    def test_O12_O13_cumulative_partial_cancel_race_and_correction(self):
        ex = self.executor()
        order = ex.submit(self.intent(ex), self.bundle.now)
        ex.broker.fill(order["broker_id"], 3, D(99), D(1), self.bundle.now)
        ex.reconcile()
        self.assertEqual(ex.store.quantity("TEST:AAA"), 3)
        ex.reconcile()
        self.assertEqual(ex.store.quantity("TEST:AAA"), 3)
        ex.cancel(order["id"], self.bundle.now)
        self.assertEqual(ex.store.order(order["id"])["state"], "CANCEL_REQUESTED")
        ex.broker.fill(order["broker_id"], 5, D(99), D(2), self.bundle.now)
        ex.broker.orders[order["broker_id"]]["state"] = "CANCELED"
        ex.reconcile()
        self.assertEqual(ex.store.quantity("TEST:AAA"), 5)
        self.assertEqual(ex.store.order(order["id"])["state"], "PARTIAL_CANCELED")
        self.assertEqual(ex.store.reservation(), 0)
        before = D(ex.store.get("cash_krw"))
        self.assertFalse(ex.store.apply_cumulative_fill(order["id"], quantity=3, notional=D(297), fees=D(1), revision=1, observed_at=self.bundle.now.isoformat()))
        ex.store.apply_cumulative_fill(order["id"], quantity=5, notional=D(490), fees=D(2), revision=100, observed_at=self.bundle.now.isoformat(), correction=True)
        self.assertEqual(D(ex.store.get("cash_krw")), before + 5)

    def test_O14_O15_incomplete_account_and_external_ownership(self):
        ex = self.executor()
        self.assertEqual(ex.reconcile({"complete": False})["status"], "DATA_INCOMPLETE")
        with self.assertRaises(HumanRequired):
            ex.submit(self.intent(ex), self.bundle.now)
        with ex.store.transaction():
            ex.store.set("reconciled", True)
            ex.store.db.execute("INSERT INTO holdings VALUES ('TEST:AAA','manual','external',100,'10000')")
        with self.assertRaises(ValueError):
            ex.submit(self.intent(ex, side="SELL", quantity=1), self.bundle.now)

    def test_O17_expiry_releases_only_after_cancellation_reconciliation(self):
        ex = self.executor()
        order = ex.submit(self.intent(ex), self.bundle.now)
        ex.broker.fill(order["broker_id"], 2, D(100), D(0), self.bundle.now)
        ex.reconcile()
        ex.expire_entries(self.bundle.now + timedelta(seconds=121))
        self.assertGreater(ex.store.reservation(), 0)
        ex.reconcile()
        self.assertEqual(ex.store.quantity("TEST:AAA"), 2)
        self.assertEqual(ex.store.reservation(), 0)
        self.assertEqual(ex.broker.submissions, 1)

    def test_O09_O22_protection_executes_while_model_waits(self):
        app = Application(self.config, self.bundle)
        try:
            app.review()
            thesis = app.theses()[0]
            entered, release = threading.Event(), threading.Event()
            def blocked_model(frozen):
                entered.set()
                self.assertTrue(release.wait(3))
                return fixture_decision(frozen)
            app.decide = blocked_model
            errors = []
            def review():
                try:
                    app.review()
                except ValueError as error:
                    errors.append(str(error))
            thread = threading.Thread(target=review)
            thread.start()
            self.assertTrue(entered.wait(3))
            quote = app.bundle.quotes[thesis.instrument_id]
            app.bundle.quotes[thesis.instrument_id] = quote.model_copy(update={"bid": thesis.current_stop - D(1), "ask": thesis.current_stop})
            plans = app.protect()
            self.assertTrue(any(item["action"] == "EXIT_PROTECTION" for item in plans))
            self.assertEqual(app.broker.submissions, 2)
            release.set()
            thread.join(3)
            self.assertTrue(any("STALE_ACCOUNT_VERSION" in item for item in errors))
        finally:
            app.close()

    def test_O26_outbox_is_not_a_trade_retry(self):
        ex = self.executor()
        ex.submit(self.intent(ex), self.bundle.now)
        with ex.store.transaction():
            ex.store.db.execute("UPDATE outbox SET attempts=attempts+1")
            ex.store.db.execute("UPDATE outbox SET attempts=attempts+1")
        self.assertEqual(ex.broker.submissions, 1)
        self.assertGreater(ex.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    def test_O27_backup_restore_preserves_unknown_reservations(self):
        store = self.store()
        ex = Executor(store, FixtureBroker(), mode="offline", authorize=lambda *_: None, preflight=lambda *_: None)
        order = ex.submit(self.intent(ex), self.bundle.now)
        with store.transaction():
            store.db.execute("UPDATE intents SET state='SUBMITTING' WHERE id=?", (order["id"],))
        backup, restored = self.root / "backup.sqlite", self.root / "restored.sqlite"
        store.backup(backup)
        Store.restore(backup, restored)
        self.stores.remove(store)
        store.close()
        reopened = Store(restored, mode="offline", account_identity=self.tmp.name + "test-account")
        self.stores.append(reopened)
        self.assertEqual(reopened.order(order["id"])["state"], "UNKNOWN")
        self.assertFalse(reopened.get("reconciled"))
        self.assertEqual(reopened.reservation(), D(1000))

    def external_app(self, mode, *, enabled=False):
        data = self.config.data
        data["app"]["app"]["mode"] = mode
        data["app"]["execution"]["enabled"] = enabled
        (self.config_dir / "app.yaml").write_text(yaml.safe_dump(data["app"]))
        config = load_config(self.config_dir)
        recorded = {**self.data, "provenance": "RECORDED_SNAPSHOT",
                    "costs": {**self.data["costs"], "synthetic": False}}
        bundle = MarketBundle(recorded, config.research, mode=mode)
        broker = FixtureBroker()
        broker.environment = None if mode == "shadow" else "demo"
        approval = {"id": "synthetic-review-approval", "config_hash": config.config_hash,
                    "expires_at": (utcnow() + timedelta(hours=1)).isoformat(),
                    "capabilities": ["account_read", "demo_orders", "resume", "candidate_control"]}
        return Application(config, bundle, broker=broker, decide=fixture_decision,
                           refresh=lambda: bundle, approval=approval, clock=lambda: bundle.now)

    def test_execution_disabled_and_changed_activation_block_dispatch(self):
        for enabled in (False, True):
            app = self.external_app("broker_demo", enabled=enabled)
            try:
                with app.store.transaction():
                    app.store.set("activated", True)  # Legacy bool never creates current authority.
                intent = OrderIntent(run_id="test", plan_id="test", thesis_id="test", instrument_id="TEST:AAA",
                    side="BUY", quantity=1, limit_price=D(100), expires_at=app.bundle.now + timedelta(seconds=120),
                    reason="TEST", account_version=app.store.get("account_version"), policy_hash=app.config.config_hash)
                with self.assertRaisesRegex(HumanRequired, "activation" if enabled else "EXECUTION_DISABLED"):
                    app.executor.submit(intent, app.bundle.now)
                if enabled:
                    with app.store.transaction():
                        app.store.set("activation", {"config_hash": app.config.config_hash, "code_id": "old-code",
                                                     "approval_id": app.approval["id"]})
                    with self.assertRaisesRegex(HumanRequired, "activation"):
                        app.executor.submit(intent, app.bundle.now)
                self.assertEqual(app.broker.submissions, 0)
            finally:
                app.close()

    def test_shadow_records_accepted_plan_without_intents_theses_or_fills(self):
        app = self.external_app("shadow")
        try:
            result = app.review(request_key="shadow-review")
            self.assertEqual(result["run_status"], "COMPLETE")
            self.assertEqual(result["order_status"], "SHADOW_PLAN_ONLY")
            plans = json.loads((app.config.state_dir / "runs" / app.bundle.now.date().isoformat() /
                                result["run_id"] / "plan.json").read_text())["data"]["plans"]
            self.assertGreater(plans[0]["quantity"], 0)
            self.assertEqual(app.broker.submissions, 0)
            self.assertEqual(app.store.db.execute("SELECT count(*) FROM intents").fetchone()[0], 0)
            self.assertEqual(app.theses(), [])
            self.assertEqual(app.store.holdings(), [])
        finally:
            app.close()

    def test_decision_age_starts_at_completion_and_includes_post_model_refresh(self):
        for refresh_delay in (0, 121):
            with self.subTest(refresh_delay=refresh_delay):
                clock = [self.bundle.now]
                finished = [False]
                app = Application(self.config, self.bundle, clock=lambda: clock[0])
                def refresh():
                    if finished[0]:
                        clock[0] += timedelta(seconds=refresh_delay)
                        finished[0] = False
                    data = json.loads(json.dumps(self.data))
                    data["as_of"] = clock[0].isoformat()
                    for quote in data["quotes"]:
                        for field in ("observed_at", "received_at", "last_observed_at"):
                            quote[field] = clock[0].isoformat()
                    return MarketBundle(data, self.config.research, mode="offline")
                def model(frozen):
                    clock[0] += timedelta(seconds=180)
                    finished[0] = True
                    return fixture_decision(frozen)
                app.refresh, app.decide = refresh, model
                try:
                    if refresh_delay:
                        with self.assertRaisesRegex(ValueError, "STALE_DECISION"):
                            app.review()
                        self.assertEqual(app.broker.submissions, 0)
                    else:
                        self.assertEqual(app.review()["order_status"], "FIXTURE_FILLED")
                finally:
                    app.close()
                    shutil.rmtree(self.config.state_dir)

    def test_candidate_controls_are_durable_and_do_not_change_held_quantity(self):
        app = Application(self.config, self.bundle)
        try:
            app.update_candidate_list("remove_portfolio_ticker", "AAA")
            self.assertEqual(app.review()["decision_status"], "NO_CANDIDATES")
            app.update_candidate_list("add_portfolio_ticker", "TEST:AAA")
            self.assertEqual(app.review()["order_status"], "FIXTURE_FILLED")
            held = app.store.quantity("TEST:AAA")
            app.update_candidate_list("add_portfolio_except_ticker", "AAA")
            self.assertEqual(app.store.quantity("TEST:AAA"), held)
            self.assertEqual(app.broker.submissions, 1)
            with self.assertRaisesRegex(ValueError, "UNKNOWN_OR_AMBIGUOUS"):
                app.update_candidate_list("add_portfolio_ticker", "MISSING")
        finally:
            app.close()
        reopened = Application(self.config, self.bundle)
        try:
            self.assertEqual(reopened.store.get("candidate_controls")["excluded"], ["TEST:AAA"])
            self.assertEqual(reopened.store.quantity("TEST:AAA"), held)
        finally:
            reopened.close()

    def test_preflight_checks_expiry_after_refresh_before_broker_dispatch(self):
        app = Application(self.config, self.bundle)
        try:
            intent = OrderIntent(run_id="expiry", plan_id="expiry", thesis_id="expiry", instrument_id="TEST:AAA",
                side="BUY", quantity=1, limit_price=D(100), expires_at=self.bundle.now + timedelta(seconds=120),
                reason="TEST", account_version=app.store.get("account_version"), policy_hash=app.config.config_hash)
            def refresh():
                app.bundle.now += timedelta(seconds=121)
                return app.bundle
            app.refresh = refresh
            with self.assertRaisesRegex(ValueError, "ENTRY_EXPIRED"):
                app.executor.submit(intent, self.bundle.now)
            self.assertEqual(app.broker.submissions, 0)
        finally:
            app.close()

    def test_resume_and_candidate_changes_cannot_bypass_missing_authority(self):
        app = self.external_app("shadow")
        try:
            app.approval["capabilities"].remove("candidate_control")
            with self.assertRaisesRegex(HumanRequired, "candidate_control"):
                app.update_candidate_list("remove_portfolio_ticker", "AAA")
            app.pause()
            with app.store.transaction():
                app.store.set("drawdown_paused", True)
            with self.assertRaisesRegex(HumanRequired, "Drawdown"):
                app.resume()
            self.assertTrue(app.store.get("paused"))
            app.approval["capabilities"].append("resume_drawdown")
            with patch.object(app.broker, "snapshot", return_value={"complete": False}):
                with self.assertRaisesRegex(HumanRequired, "reconciliation"):
                    app.resume()
            self.assertEqual(app.resume()["status"], "RESUMED")
            self.assertFalse(app.store.get("paused"))
        finally:
            app.close()

    def test_authorization_change_during_preflight_never_reaches_broker(self):
        app = self.external_app("broker_demo", enabled=True)
        try:
            with app.store.transaction():
                app.store.set("activation", {"config_hash": app.config.config_hash, "code_id": app.code_id,
                                             "approval_id": app.approval["id"]})
            original = app.executor.preflight
            def preflight(intent, now):
                original(intent, now)
                path = self.config_dir / "app.yaml"
                data = yaml.safe_load(path.read_text())
                data["execution"]["enabled"] = False
                path.write_text(yaml.safe_dump(data))
            app.executor.preflight = preflight
            with self.assertRaisesRegex(HumanRequired, "POLICY_CHANGED"):
                app.review()
            self.assertEqual(app.broker.submissions, 0)
            self.assertEqual(app.store.reservation(), 0)
            self.assertEqual(app.store.db.execute("SELECT state FROM intents").fetchone()[0], "INVALIDATED")
            self.assertEqual(app.store.db.execute("SELECT count(*) FROM journal WHERE kind='ORDER_NOT_SENT'").fetchone()[0], 1)
        finally:
            app.close()

    def test_cancel_rechecks_authority_and_preserves_reservation_when_not_sent(self):
        executor = self.executor()
        order = executor.submit(self.intent(executor), self.bundle.now)
        calls = []
        def authorize(_intent, operation):
            calls.append(operation)
            if len(calls) == 2:
                raise HumanRequired("Expired approval")
        executor.authorize = authorize
        with self.assertRaisesRegex(HumanRequired, "Expired approval"):
            executor.cancel(order["id"])
        self.assertEqual(executor.broker.cancel_requests, 0)
        self.assertEqual(executor.store.order(order["id"])["state"], "ACKNOWLEDGED")
        self.assertEqual(executor.store.reservation(), D(1000))


if __name__ == "__main__":
    unittest.main()
