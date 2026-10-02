"""Idle account reuse never replaces fresh order/review reads."""
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from danta.runtime import KisBrokerPort


class AccountCacheContracts(unittest.TestCase):
    def test_only_idle_complete_account_is_reused_until_expiry_or_ledger_change(self):
        now = datetime(2026,9,23,tzinfo=timezone.utc)
        broker = KisBrokerPort(SimpleNamespace(environment='real'),{},None,clock=lambda:now)
        broker.store = SimpleNamespace(working=Mock(return_value=[]),get=Mock(return_value=1))
        broker._snapshot = Mock(side_effect=lambda:{'complete':True,'ownership_complete':True,'orders':[]})
        first = broker.snapshot()
        now += timedelta(seconds=5)
        self.assertIs(broker.snapshot(maximum_age_seconds=60),first)
        self.assertEqual(broker._snapshot.call_count,1)
        broker.snapshot()  # A preflight must always read the broker.
        self.assertEqual(broker._snapshot.call_count,2)
        broker.store.get.return_value = 2
        broker.snapshot(maximum_age_seconds=60)
        self.assertEqual(broker._snapshot.call_count,3)
        now += timedelta(seconds=60)
        broker.snapshot(maximum_age_seconds=60)
        self.assertEqual(broker._snapshot.call_count,4)
        broker.store.working.return_value = [{}]
        broker.snapshot(maximum_age_seconds=60)
        self.assertEqual(broker._snapshot.call_count,5)
        broker.store.working.return_value = []
        broker._snapshot = Mock(return_value={'complete':False})
        broker.snapshot()
        self.assertIsNone(broker.snapshot_cache)

    def test_order_during_refresh_prevents_late_cache_publication(self):
        broker = KisBrokerPort(SimpleNamespace(environment='real'), {}, None)
        def snapshot():
            broker._invalidate_snapshot()
            return {'complete':True, 'ownership_complete':True, 'orders':[]}
        broker._snapshot = snapshot
        self.assertTrue(broker.snapshot()['complete'])
        self.assertIsNone(broker.snapshot_cache)

    def test_concurrent_fresh_reads_share_one_flight_but_an_order_invalidates_it(self):
        for invalidate in (False, True):
            broker = KisBrokerPort(SimpleNamespace(environment='real'), {}, None)
            entered, waiting, release = threading.Event(), threading.Event(), threading.Event()
            def snapshot():
                entered.set()
                self.assertTrue(release.wait(3))
                return {'complete': True, 'ownership_complete': True, 'orders': []}
            broker._snapshot = Mock(side_effect=snapshot)
            original_wait = broker.snapshot_condition.wait
            def wait(*args):
                waiting.set()
                return original_wait(*args)
            broker.snapshot_condition.wait = wait
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(broker.snapshot)
                self.assertTrue(entered.wait(2))
                second = pool.submit(broker.snapshot)
                self.assertTrue(waiting.wait(2))
                if invalidate:
                    broker._invalidate_snapshot()
                release.set()
                a, b = first.result(3), second.result(3)
            self.assertEqual(broker._snapshot.call_count, 2 if invalidate else 1)
            if not invalidate:
                self.assertIs(a, b)
