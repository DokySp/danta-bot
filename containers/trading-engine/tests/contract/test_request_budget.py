"""Provider limits pause the shared transport without replaying orders."""
import json
import unittest
from unittest.mock import Mock, patch

from danta.adapters import AdapterError, HttpResponse
from danta.adapters.kis import KisAdapter, KisCredentials
from danta.runtime import PriorityTransport


class RequestBudgetContracts(unittest.TestCase):
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
