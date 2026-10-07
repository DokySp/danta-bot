"""Provider limits pause the shared transport without replaying orders."""
import json
import unittest
from unittest.mock import Mock, patch

from danta.adapters import AdapterError, HttpResponse
from danta.adapters.kis import KisAdapter, KisCredentials
from danta.runtime import PriorityTransport


class RequestBudgetContracts(unittest.TestCase):
    def test_intermittent_limits_slow_actual_requests_and_decay_only_after_healthy_window(self):
        now = [100.0]
        sent = []
        limited = HttpResponse(500, b'{"msg_cd":"EGW00215"}')
        healthy = HttpResponse(200, b'{"rt_cd":"0"}')
        replies = iter([limited, limited, healthy, healthy, healthy, healthy])
        def source(*_):
            sent.append(now[0])
            return next(replies)
        with patch('danta.runtime.time.monotonic', side_effect=lambda: now[0]):
            transport = PriorityTransport(source, minimum_interval_seconds='.25', maximum_queue_seconds='2')
            with patch.object(transport.condition, 'wait', side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
                transport('GET', 'https://fixture/trading/inquire-balance')
                now[0] = 180  # The old 60-second reset discarded this failure history.
                transport('GET', 'https://fixture/trading/inquire-balance')
                self.assertEqual(transport.rate_limit_failures, 2)
                now[0] = 190
                transport('GET', 'https://fixture/trading/inquire-balance')
                transport('GET', 'https://fixture/quotations/inquire-price')
                self.assertEqual(sent[-2:], [190, 191])
                now[0] = 900
                transport('GET', 'https://fixture/trading/inquire-balance')
                self.assertEqual(transport.effective_interval, 1)
                now[0] = 1980
                transport('GET', 'https://fixture/trading/inquire-balance')
                self.assertEqual(transport.effective_interval, .5)
        self.assertEqual(len(sent), 6, 'rate failures must never replay a request')

    def test_post_limit_response_preserves_uncertainty_and_does_not_retry(self):
        for status, expected in ((200, 'REJECTED'), (500, 'UNKNOWN')):
            source = Mock(spec=[], return_value=HttpResponse(status, b'{"rt_cd":"1","msg_cd":"EGW00215"}'))
            source.fixture_only = True
            adapter = KisAdapter(environment='real', credentials=KisCredentials('00000000','00','FAKE','FAKE','FAKE'), transport=source)
            result = adapter.submit('005930', 'SELL', 1)
            self.assertEqual(result.status, expected)
            self.assertEqual(result.diagnostic['provider_code'], 'EGW00215')
            self.assertEqual(source.call_count, 1)

    def test_provider_limit_cools_all_endpoints_then_recovers(self):
        for status in (200, 500, 429):
            with self.subTest(status=status), patch('danta.runtime.time.monotonic', return_value=100) as clock:
                source = Mock(return_value=HttpResponse(status, json.dumps({'rt_cd':'1','msg_cd':'EGW00215'}).encode()))
                transport = PriorityTransport(source, minimum_interval_seconds='.25', maximum_queue_seconds='2')
                transport('GET', 'https://fixture/trading/inquire-balance')
                with self.assertRaises(AdapterError) as raised:
                    transport('GET', 'https://fixture/quotations/inquire-price')
                self.assertEqual(source.call_count, 1)
                self.assertEqual(raised.exception.diagnostic['request_stage'], 'RATE_COOLDOWN')
                self.assertFalse(raised.exception.diagnostic['request_sent'])
                clock.return_value = 105
                source.return_value = HttpResponse(200, b'{"rt_cd":"0"}')
                transport('GET', 'https://fixture/quotations/inquire-price')
                self.assertEqual(source.call_count, 2)

    def test_order_blocked_by_cooldown_is_proven_not_sent(self):
        with patch('danta.runtime.time.monotonic', return_value=100):
            source = Mock(return_value=HttpResponse(500, b'{"msg_cd":"EGW00215"}'))
            transport = PriorityTransport(source, minimum_interval_seconds='.25', maximum_queue_seconds='2')
            transport.fixture_only = True
            transport('GET', 'https://fixture/trading/inquire-balance')
            adapter = KisAdapter(environment='real', credentials=KisCredentials('00000000','00','FAKE','FAKE','FAKE'), transport=transport)
            result = adapter.submit('005930', 'SELL', 1)
            self.assertEqual(result.status, 'NOT_SENT')
            self.assertEqual(source.call_count, 1)
