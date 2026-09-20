"""Synthetic token renewal, persistence and broker no-retry checks."""

import fcntl
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from danta.adapters import AdapterError, HttpResponse
from danta.adapters.kis import KisAdapter, KisCredentials, KisTokenCache
from danta.runtime import KisBrokerPort, PriorityTransport
from danta.execution import Executor, FixtureBroker, OrderIntent
from danta.store import Store


NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


class Transport:
    fixture_only = True

    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, method, url, headers=None, body=None, timeout=15):
        self.calls.append((method, url, headers, body))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def token_response(token="fixture-token", expiry=NOW + timedelta(hours=24)):
    return HttpResponse(200, json.dumps({"access_token": token, "access_token_token_expired":
        expiry.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")}).encode())


class KisTokenCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "account" / "kis-token.json"
        self.now = NOW

    def cache(self, transport, **kwargs):
        return KisTokenCache(kwargs.pop("environment", "demo"), kwargs.pop("app_key", "fixture-key"),
            kwargs.pop("app_secret", "fixture-secret"), kwargs.pop("path", self.path), transport=transport,
            clock=lambda: self.now, **kwargs)

    def test_restart_reuse_and_renew_at_sixty_seconds(self):
        transport = Transport(token_response("fixture-first"), token_response("fixture-renewed", NOW + timedelta(days=2)))
        self.assertEqual(self.cache(transport)(), "fixture-first")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.with_name(self.path.name + ".lock").stat().st_mode & 0o777, 0o600)
        payload = self.path.read_text()
        self.assertNotIn("fixture-key", payload)
        self.assertNotIn("fixture-secret", payload)
        self.now += timedelta(hours=24, seconds=-61)
        self.assertEqual(self.cache(transport)(), "fixture-first")
        self.assertEqual(len(transport.calls), 1)
        self.now += timedelta(seconds=1)
        self.assertEqual(self.cache(transport)(), "fixture-renewed")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(json.loads(transport.calls[0][3])["grant_type"], "client_credentials")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_concurrent_instances_only_issue_once(self):
        transport = Transport(token_response())
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda _: self.cache(transport)(), range(16)))
        self.assertEqual(results, ["fixture-token"] * 16)
        self.assertEqual(len(transport.calls), 1)

    def test_environment_app_and_secret_changes_do_not_reuse_cache(self):
        transport = Transport(*(token_response("fixture-" + str(i)) for i in range(4)))
        for index, kwargs in enumerate(({}, {"environment": "real"}, {"app_key": "fixture-other"}, {"app_secret": "fixture-rotated"})):
            self.assertEqual(self.cache(transport, **kwargs)(), "fixture-" + str(index))
        self.assertEqual(len(transport.calls), 4)

    def test_invalid_provider_expiry_and_bad_cache_fail_without_values(self):
        for reply in (token_response(expiry=NOW), token_response(expiry=NOW + timedelta(seconds=60)),
                      HttpResponse(200, b'{"access_token":"fixture-private","access_token_token_expired":"fixture-private"}'),
                      token_response("fixture-private\ninvalid")):
            with self.subTest(reply_type=type(reply).__name__):
                transport = Transport(reply)
                with self.assertRaises(AdapterError) as caught:
                    self.cache(transport)()
                self.assertNotIn("fixture-private", str(caught.exception))
                self.assertFalse(self.path.exists())
                self.assertEqual(len(transport.calls), 1)
        self.cache(Transport(token_response()))()
        data = json.loads(self.path.read_text())
        data["expires_at"] = "2026-09-15T00:00:00"
        self.path.write_text(json.dumps(data))
        transport = Transport()
        with self.assertRaisesRegex(AdapterError, "TOKEN_CACHE_INVALID"):
            self.cache(transport)()
        self.assertEqual(transport.calls, [])

    def test_issue_latency_is_included_in_expiry_validation(self):
        transport = Transport(token_response(expiry=NOW + timedelta(seconds=61)))
        original = transport.__call__
        def delayed(*args, **kwargs):
            self.now += timedelta(seconds=2)
            return original(*args, **kwargs)
        delayed.fixture_only = True
        with self.assertRaisesRegex(AdapterError, "TOKEN_EXPIRY_INVALID"):
            self.cache(delayed)()
        self.assertFalse(self.path.exists())

    def test_unsafe_files_permissions_and_symlinks_fail_before_request(self):
        self.path.parent.mkdir(mode=0o700)
        outside = Path(self.directory.name) / "untouched"
        outside.write_text("fixture-private")
        for name in (self.path, self.path.with_name(self.path.name + ".lock")):
            for kind in ("symlink", "fifo", "mode", "hardlink"):
                with self.subTest(name=name.name, kind=kind):
                    if kind == "symlink":
                        name.symlink_to(outside)
                    elif kind == "fifo":
                        os.mkfifo(name, 0o600)
                    elif kind == "hardlink":
                        os.link(outside, name)
                    else:
                        name.write_text("fixture-private")
                        name.chmod(0o644)
                    transport = Transport()
                    with self.assertRaises(AdapterError) as caught:
                        self.cache(transport)()
                    self.assertNotIn("fixture-private", str(caught.exception))
                    self.assertEqual(transport.calls, [])
                    name.unlink()
            lock = self.path.with_name(self.path.name + ".lock")
            lock.unlink(missing_ok=True)
        link = Path(self.directory.name) / "linked-parent"
        link.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaisesRegex(AdapterError, "TOKEN_CACHE_UNSAFE"):
            self.cache(Transport(), path=link / self.path.name)()
        self.path.parent.chmod(0o777)
        with self.assertRaisesRegex(AdapterError, "TOKEN_CACHE_UNSAFE"):
            self.cache(Transport())()
        self.path.parent.chmod(0o700)
        self.assertEqual(outside.read_text(), "fixture-private")

    def test_atomic_replace_failure_preserves_cache_and_cleans_temporary_file(self):
        self.cache(Transport(token_response()))()
        before = self.path.read_bytes()
        self.now += timedelta(days=1)
        with patch("danta.adapters.kis.os.replace", side_effect=OSError("fixture-private")):
            with self.assertRaisesRegex(AdapterError, "TOKEN_CACHE_IO_FAILED"):
                self.cache(Transport(token_response(expiry=self.now + timedelta(days=1))))()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_cached_token_still_requires_external_authorization(self):
        self.cache(Transport(token_response()))()
        with self.assertRaisesRegex(AdapterError, "AUTHORIZATION_REQUIRED"):
            self.cache(Transport(), mode="shadow")()
        calls = []
        self.assertEqual(self.cache(Transport(), mode="shadow", authorize=lambda *args: calls.append(args))(), "fixture-token")
        self.assertEqual(calls, [("broker_auth", "demo")])
        with self.assertRaisesRegex(AdapterError, "OFFLINE_NETWORK_BLOCKED"):
            self.cache(None)()

    def test_broker_rechecks_grant_after_token_and_never_retries_post(self):
        calls = []
        def authorize(operation, environment):
            calls.append(operation)
            if calls == ["broker_write", "broker_auth", "broker_write"]:
                raise AdapterError("APPROVAL_EXPIRED")
        token_transport = Transport(token_response())
        self.cache(token_transport)()
        broker_transport = Transport()
        broker = KisAdapter(environment="demo", credentials=KisCredentials("00000000", "01", "fixture-key", "fixture-secret", ""),
            transport=broker_transport, mode="broker_demo", authorize=authorize,
            token_provider=self.cache(token_transport, mode="broker_demo", authorize=authorize))
        result = broker.submit("000001", "BUY", 1, limit_price="1000")
        self.assertEqual((result.status, result.code), ("NOT_SENT", "APPROVAL_EXPIRED"))
        self.assertEqual(len(token_transport.calls), 1)
        self.assertEqual(broker_transport.calls, [])
        broker.authorize = lambda *args: None
        broker.token_provider = self.cache(Transport(), mode="broker_demo", authorize=lambda *args: None)
        broker_transport.results = [TimeoutError("fixture-private")]
        self.assertEqual(broker.submit("000001", "BUY", 1, limit_price="1000").status, "UNKNOWN")
        self.assertEqual(len(broker_transport.calls), 1)
        self.assertEqual(broker_transport.calls[0][2]["authorization"], "Bearer fixture-token")
        broker_transport.results = [HttpResponse(401, b"fixture-private")]
        with self.assertRaisesRegex(AdapterError, "AUTH_FAILED"):
            broker.cancel("42", "999", 1)
        self.assertEqual(len(broker_transport.calls), 2)

    def test_token_transport_failure_never_sends_order_or_leaks_error(self):
        token_transport = Transport(TimeoutError("fixture-private"))
        broker_transport = Transport()
        broker = KisAdapter(environment="demo", credentials=KisCredentials("00000000", "01", "fixture-key", "fixture-secret", ""),
            transport=broker_transport, token_provider=self.cache(token_transport))
        with self.assertRaisesRegex(AdapterError, "AUTH_TRANSPORT_FAILED"):
            broker.token_provider()
        result = broker.submit("000001", "BUY", 1, limit_price="1000")
        self.assertEqual((result.status, result.code), ("NOT_SENT", "TOKEN_REFRESH_REQUIRED"))
        self.assertEqual(len(token_transport.calls), 1)
        self.assertEqual(broker_transport.calls, [])


    def test_post_cache_miss_expiry_and_lock_never_renew_or_wait(self):
        transport = Transport(token_response())
        cache = self.cache(transport)
        with self.assertRaisesRegex(AdapterError, "TOKEN_REFRESH_REQUIRED"):
            cache(allow_refresh=False)
        self.assertEqual(transport.calls, [])
        cache()
        with self.path.with_name(self.path.name + '.lock').open('r+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(AdapterError, "TOKEN_REFRESH_REQUIRED"):
                cache(allow_refresh=False)
        self.now += timedelta(hours=24, seconds=-60)
        with self.assertRaisesRegex(AdapterError, "TOKEN_REFRESH_REQUIRED"):
            cache(allow_refresh=False)
        self.assertEqual(len(transport.calls), 1)

    def test_actual_post_checks_decision_quote_and_session_deadlines_after_queue(self):
        cache = self.cache(Transport(token_response()))
        cache()
        for boundary in ('decision', 'quote', 'session'):
            with self.subTest(boundary=boundary):
                self.now = NOW
                transport = Transport()
                queued = PriorityTransport(transport, minimum_interval_seconds='0.0001', maximum_queue_seconds='1')
                adapter = KisAdapter(environment='demo', credentials=KisCredentials('00000000', '01', 'fixture', 'fixture', ''),
                    transport=queued, token_provider=cache, clock=lambda: self.now)
                broker = KisBrokerPort(adapter, {}, None, clock=lambda: self.now)
                broker.latest_bundle = SimpleNamespace(
                    quotes={'KRX:000001': SimpleNamespace(observed_at=NOW - timedelta(seconds=4 if boundary == 'quote' else 0))},
                    calendar=SimpleNamespace(active=lambda _: SimpleNamespace(closes_at=NOW + timedelta(seconds=1 if boundary == 'session' else 60))))
                # Simulate time spent in the real priority queue, without sleeping.
                queued.next_at = float('inf')
                def advance(_timeout):
                    self.now += timedelta(seconds=2)
                    queued.next_at = 0
                with patch.object(adapter, 'read_buying_power', return_value={'nrcvb_buy_qty':'1','nrcvb_buy_amt':'1000'}), \
                        patch.object(queued.condition, 'wait', side_effect=advance):
                    result = broker.submit({'instrument_id':'KRX:000001', 'side':'BUY', 'quantity':1, 'limit_price':'1000',
                        'expires_at':(NOW + timedelta(seconds=1 if boundary == 'decision' else 60)).isoformat()})
                self.assertEqual(result, {'status':'NOT_SENT', 'reason':'ORDER_VALIDITY_EXPIRED'})
                self.assertEqual(transport.calls, [])

    def test_unsent_submit_releases_reservation_and_unsent_cancel_restores_state(self):
        with Store(Path(self.directory.name)/'ledger.sqlite', mode='offline', account_identity=self.directory.name,
                   initial_cash=Decimal(10000)) as store:
            broker = FixtureBroker()
            executor = Executor(store, broker, mode='offline', authorize=lambda *_: None, preflight=lambda *_: None)
            def intent(plan):
                return OrderIntent(run_id='fixture-run', plan_id=plan, thesis_id='fixture-thesis', instrument_id='TEST:AAA',
                    side='BUY', quantity=1, limit_price=Decimal(100), expires_at=NOW+timedelta(seconds=120), reason='FIXTURE',
                    account_version=store.get('account_version'), policy_hash='fixture-policy', reserve_cash=Decimal(100), reserve_risk=Decimal(1))
            with patch.object(broker, 'submit', return_value={'status':'NOT_SENT','reason':'TOKEN_REFRESH_REQUIRED'}):
                result = executor.submit(intent('blocked'), NOW)
            self.assertEqual(result['state'], 'INVALIDATED')
            self.assertEqual(store.reservation(), 0)
            self.assertTrue(store.get('reconciled'))
            acknowledged = executor.submit(intent('accepted'), NOW)
            with patch.object(broker, 'cancel', return_value={'status':'NOT_SENT','reason':'TOKEN_REFRESH_REQUIRED'}):
                result = executor.cancel(acknowledged['id'], NOW)
            self.assertEqual(result['state'], 'ACKNOWLEDGED')
            self.assertEqual(store.reservation(), 100)
            self.assertTrue(store.get('reconciled'))
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM journal WHERE kind IN ('ORDER_NOT_SENT','CANCEL_NOT_SENT')").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
