"""Provider limits pause the shared transport without replaying orders."""
import json
import inspect
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from danta.adapters import AdapterError, HttpResponse
from danta.adapters.kis import KisAdapter, KisCredentials
from danta.runtime import PriorityTransport


class RequestBudgetContracts(unittest.TestCase):
    def test_preempted_dispatch_cannot_be_overtaken_or_burst(self):
        paused, resume, second_sent = threading.Event(), threading.Event(), threading.Event()
        sent, errors = [], []
        def source(*_):
            name = threading.current_thread().name
            sent.append((name, time.monotonic()))
            if name == 'second':
                second_sent.set()
            return HttpResponse(200, b'{"rt_cd":"0","output1":[]}')
        source.fixture_only = True
        transport = PriorityTransport(source, minimum_interval_seconds='.025', maximum_queue_seconds='1')
        adapter = KisAdapter(environment='real', credentials=KisCredentials('00000000','00','FAKE','FAKE','FAKE'), transport=transport)
        lines, first_line = inspect.getsourcelines(PriorityTransport.__call__)
        send_line = first_line + next(i for i, line in enumerate(lines) if 'response = self.transport(' in line)
        def trace(frame, event, _arg):
            if frame.f_code is PriorityTransport.__call__.__code__ and event == 'line' and frame.f_lineno == send_line:
                paused.set()
                if not resume.wait(2):
                    raise TimeoutError('fixture dispatch barrier')
            return trace
        def read(first=False):
            try:
                if first:
                    sys.settrace(trace)
                adapter._request('/uapi/domestic-stock/v1/trading/inquire-balance', 'TTTC8434R', {})
            except BaseException as error:
                errors.append(error)
            finally:
                sys.settrace(None)
        first = threading.Thread(target=read, args=(True,), name='first')
        second = threading.Thread(target=read, name='second')
        first.start()
        try:
            self.assertTrue(paused.wait(1))
            second.start()
            overtook = second_sent.wait(.1)
        finally:
            resume.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(overtook, 'a reserved dispatch must retain ownership until raw I/O completes')
        self.assertEqual([name for name, _ in sent], ['first', 'second'])
        self.assertGreaterEqual(sent[1][1] - sent[0][1], .025)

    def test_rate_limited_balance_and_buying_power_recover_after_shared_cooldown(self):
        for read in (lambda adapter: adapter.read_account(), lambda adapter: adapter.read_buying_power('005930', '100')):
            now, sent, sleeps = [100.0], [], []
            responses = iter([HttpResponse(500, b'{"msg_cd":"EGW00215"}'),
                              HttpResponse(200, b'{"rt_cd":"0","output1":[],"output":{"nrcvb_buy_amt":"1000"}}')])
            def source(*_):
                sent.append(now[0])
                return next(responses)
            source.fixture_only = True
            def sleep(delay):
                sleeps.append(delay)
                now[0] += delay
            with patch('danta.runtime.time.monotonic', side_effect=lambda: now[0]), patch('danta.adapters.kis.time.sleep', side_effect=sleep):
                transport = PriorityTransport(source, minimum_interval_seconds='.25', maximum_queue_seconds='2')
                adapter = KisAdapter(environment='real', credentials=KisCredentials('00000000','00','FAKE','FAKE','FAKE'), transport=transport)
                result = read(adapter)
            self.assertEqual(sent, [100, 105])
            self.assertEqual(sleeps, [5])
            if hasattr(result, 'quality'):
                self.assertEqual(result.quality, 'COMPLETE')
            else:
                self.assertEqual(result['nrcvb_buy_amt'], '1000')

    def test_provider_delay_and_retry_budget_keep_persistent_limits_fail_closed(self):
        for delay, persistent, expected_calls, expected_sleeps in ((6, False, 2, [6]), (11, False, 1, []), (5, True, 2, [5])):
            with self.subTest(delay=delay, persistent=persistent):
                now, calls, sleeps = [100.0], [], []
                def source(*_):
                    calls.append(now[0])
                    if len(calls) == 1 or persistent:
                        return HttpResponse(429, b'{"msg_cd":"EGW00215"}', {'Retry-After': str(delay)})
                    return HttpResponse(200, b'{"rt_cd":"0","output1":[]}')
                source.fixture_only = True
                def sleep(seconds):
                    sleeps.append(seconds)
                    now[0] += seconds
                with patch('danta.runtime.time.monotonic', side_effect=lambda: now[0]), patch('danta.adapters.kis.time.sleep', side_effect=sleep):
                    transport = PriorityTransport(source, minimum_interval_seconds='.25', maximum_queue_seconds='2')
                    adapter = KisAdapter(environment='real', credentials=KisCredentials('00000000','00','FAKE','FAKE','FAKE'), transport=transport)
                    result = adapter.read_account()
                self.assertEqual(len(calls), expected_calls)
                self.assertEqual(sleeps, expected_sleeps)
                self.assertEqual(result.quality, 'COMPLETE' if delay == 6 else 'FETCH_FAILED')
                if result.quality != 'COMPLETE':
                    self.assertEqual(result.metadata['provider_code'], 'EGW00215')
                    self.assertEqual(result.metadata['attempt_count'], expected_calls)
                    self.assertEqual(result.metadata['retry_after_seconds'], 10 if persistent else 11)

    def test_waiting_order_times_out_without_sending_or_blocking_the_queue_lock(self):
        entered, resume = threading.Event(), threading.Event()
        sent, errors = [], []
        def source(method, *_):
            sent.append(method)
            entered.set()
            if not resume.wait(2):
                raise TimeoutError('fixture response barrier')
            return HttpResponse(200, b'{"rt_cd":"0"}')
        source.fixture_only = True
        transport = PriorityTransport(source, minimum_interval_seconds='.01', maximum_queue_seconds='.05')
        def read():
            try:
                transport('GET', 'https://fixture/bulk')
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=read)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            adapter = KisAdapter(environment='real', credentials=KisCredentials('00000000','00','FAKE','FAKE','FAKE'), transport=transport)
            result = adapter.submit('005930', 'SELL', 1)
            self.assertEqual(result.status, 'NOT_SENT')
            self.assertEqual(result.diagnostic['request_stage'], 'RATE_QUEUE')
            self.assertFalse(result.diagnostic['request_sent'])
        finally:
            resume.set()
            worker.join(2)
        self.assertEqual(errors, [])
        self.assertFalse(worker.is_alive())
        self.assertEqual(sent, ['GET'])
        self.assertEqual(transport.queue, [])

    def test_priority_and_gate_cleanup_survive_inflight_failure(self):
        entered, queued, resume = threading.Event(), threading.Event(), threading.Event()
        sent, errors = [], []
        def source(_method, url, *_):
            sent.append(url)
            if url == 'first':
                entered.set()
                if not resume.wait(2):
                    raise TimeoutError('fixture response barrier')
                raise AdapterError('TRANSPORT_FAILED')
            return HttpResponse(200, b'{"rt_cd":"0"}')
        transport = PriorityTransport(source, minimum_interval_seconds='.01', maximum_queue_seconds='1')
        original_wait = transport.condition.wait
        def wait(delay):
            if len(transport.queue) == 2:
                queued.set()
            return original_wait(delay)
        def read(url):
            try:
                transport('GET', url)
            except AdapterError as error:
                errors.append(error.code)
        workers = [threading.Thread(target=read, args=(url,)) for url in ('first', 'bulk', '/trading/urgent')]
        with patch.object(transport.condition, 'wait', side_effect=wait):
            workers[0].start()
            try:
                self.assertTrue(entered.wait(1))
                workers[1].start()
                workers[2].start()
                self.assertTrue(queued.wait(1))
            finally:
                resume.set()
                for worker in workers:
                    if worker.ident is not None:
                        worker.join(2)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, ['TRANSPORT_FAILED'])
        self.assertEqual(sent, ['first', '/trading/urgent', 'bulk'])
        with self.assertRaisesRegex(AdapterError, 'AUTH_FAILED'):
            transport('GET', 'forbidden', before_send=Mock(side_effect=AdapterError('AUTH_FAILED')))
        transport('GET', 'after-auth-failure')
        self.assertEqual(sent[-1], 'after-auth-failure')
        self.assertNotIn('forbidden', sent)

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
            transport = PriorityTransport(source, minimum_interval_seconds='.25', maximum_queue_seconds='2')
            adapter = KisAdapter(environment='real', credentials=KisCredentials('00000000','00','FAKE','FAKE','FAKE'), transport=transport)
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
