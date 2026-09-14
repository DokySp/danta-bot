"""Service integration uses real SQLite/adapters and fake app/transport; no sockets."""
from datetime import timedelta
from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from danta.adapters import AdapterError, HttpResponse
from danta.adapters.telegram import TelegramAdapter
from danta.config import HumanRequired, ROOT, canonical, load_config, utcnow
from danta.market import SessionCalendar
from danta.models import Session
from danta.service import PeerAuthenticator, Service, serve
from danta.store import Store


SECRET = 'synthetic-authentication-key-for-test-only'


class FakeApp:
    def __init__(self, config, now):
        self.config, self.code_id = config, 'synthetic-service-code'
        self.approval = {'capabilities': ['telegram_ingress', 'telegram_send', 'telegram_control'],
                         'config_hash': config.config_hash, 'expires_at': (utcnow() + timedelta(hours=1)).isoformat()}
        self.store = Store(config.state_dir / 'state.sqlite', mode='paper',
                           account_identity='synthetic-service-account', initial_cash=Decimal('10000000'),
                           lock_dir=config.state_dir / 'locks')
        opening = now - timedelta(minutes=20)
        session = Session(session_id=now.date().isoformat(), ordinal=0,
                          opens_at=opening, closes_at=opening + timedelta(hours=6))
        calendar = SessionCalendar([session], provenance='synthetic', verified=True, synthetic=True)
        self.bundle = SimpleNamespace(synthetic=False, calendar=calendar, events=[], now=now)
        self.review_calls, self.protect_calls, self.reconcile_calls = 0, 0, 0
        self.candidate_updates, self.resume_calls = [], 0
        self.resume_error = None
        self.review_entered, self.review_release = threading.Event(), threading.Event()
        self.review_release.set()
        self.monitor_started = False
        self.refresh = None

    def review(self, **kwargs):
        self.review_calls += 1
        self.review_entered.set()
        if not self.review_release.wait(5):
            raise AssertionError('test failed to release the fake model')
        return {'status': 'FAKE_REVIEW_COMPLETE', 'kwargs': kwargs}

    def reconcile(self):
        self.reconcile_calls += 1
        return {'status': 'FAKE_RECONCILED'}

    def protect(self):
        self.protect_calls += 1
        return []

    def pause(self):
        with self.store.transaction():
            self.store.set('paused', True)
        return {'status': 'PAUSED', 'protection': 'CONTINUES'}

    def status(self):
        return {'status': 'FAKE_STATUS', 'paused': self.store.get('paused'),
                'strategy': 'STRATEGY_UNPROVEN'}

    def update_candidate_list(self, command, ticker):
        self.candidate_updates.append((command, ticker))
        return {'status': 'FAKE_CANDIDATE_LIST_UPDATED', 'command': command, 'ticker': ticker}

    def resume(self):
        self.resume_calls += 1
        if self.resume_error:
            raise self.resume_error
        return {'status': 'FAKE_RESUMED'}

    def theses(self):
        return []

    def start_monitor(self, interval):
        self.monitor_started = True

    def close(self):
        self.store.close()


class ServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.no_network = patch('socket.socket', side_effect=AssertionError('Network forbidden in service tests'))
        self.no_network.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.now = utcnow()
        data = load_config(ROOT / 'config').data
        data['app']['app']['mode'] = 'paper'
        data['app']['app']['state_dir'] = str(Path(self.tmp.name) / 'state')
        data['app']['telegram'].update(enabled=True, ingress_enabled=True,
            trusted_peer_profile='synthetic-injected-profile', allowed_sender_ids=['user-1'],
            allowed_chat_ids=['chat-1'], route='v1')
        self.directory = Path(self.tmp.name) / 'config'
        self.directory.mkdir()
        for key, value in data.items():
            (self.directory / (key + '.yaml')).write_text(yaml.safe_dump(value, allow_unicode=True))
        self.config = load_config(self.directory)
        self.app = FakeApp(self.config, self.now)
        self.sent = []
        self.fail_send = False
        self.adapter = TelegramAdapter(self.app.store.db, enabled=True, trusted_peers=['verified-peer'],
            allowed_senders=['user-1'], allowed_chats=['chat-1'], transport=self.transport,
            authorize=lambda *args: None)
        profile = {'schema_version': 1, 'identity': 'verified-peer', 'allowed_source_ips': ['127.0.0.1'],
            'secret_env': 'SYNTHETIC_KEY', 'verified': True, 'evidence_id': 'synthetic-test-proof',
            'expires_at': (self.now + timedelta(hours=1)).isoformat(), 'config_hash': self.config.config_hash}
        self.auth = PeerAuthenticator(profile, self.config.config_hash,
            env={'SYNTHETIC_KEY': SECRET}, clock=lambda: self.now)
        self.service = Service(self.app, telegram=self.adapter, peer_auth=self.auth, clock=lambda: self.now)
        self.adapter.authorize = self.service._authorize_control

    def tearDown(self):
        self.app.review_release.set()
        self.service.close()
        self.app.close()
        self.tmp.cleanup()
        self.no_network.stop()

    def transport(self, method, url, headers, body, timeout):
        self.sent.append(json.loads(body))
        if self.fail_send:
            raise AdapterError('SYNTHETIC_TIMEOUT')
        return HttpResponse(200, b'{"ok":true}')

    def receive(self, text='/status', update=1, **overrides):
        body = {'source': 'telegram', 'route': 'v1', 'update_id': update,
                'chat_id': 'chat-1', 'user_id': 'user-1', 'text': text, **overrides}
        raw = canonical(body).encode()
        timestamp = self.now.isoformat()
        signature = hmac.new(SECRET.encode(), timestamp.encode() + b'\nPOST\n/telegram\n' + raw, hashlib.sha256).hexdigest()
        return self.service.receive_http(raw, '127.0.0.1',
            {'X-Danta-Timestamp': timestamp, 'X-Danta-Signature': signature})

    def last_result(self):
        row = self.app.store.db.execute("SELECT result FROM requests WHERE request_key LIKE 'service:%' ORDER BY rowid DESC LIMIT 1").fetchone()
        return json.loads(row[0])

    def test_auth_durable_receipt_and_identical_update_deduplication(self):
        first = self.receive()
        duplicate = self.receive()
        self.assertTrue(first['accepted'])
        self.assertEqual(first['request_id'], duplicate['request_id'])
        self.assertEqual(self.app.review_calls, 0)
        self.assertEqual(self.app.store.db.execute("SELECT COUNT(*) FROM requests WHERE request_key LIKE 'service:%'").fetchone()[0], 1)
        self.assertTrue(self.service.run_once())
        self.receive()
        self.assertFalse(self.service.run_once())
        self.assertEqual(self.app.store.db.execute('SELECT status FROM telegram_requests').fetchone()[0], 'COMPLETE')
        with self.assertRaises(AdapterError):
            self.receive('/pause')

    def test_body_sender_and_peer_claim_cannot_replace_transport_proof(self):
        raw = canonical({'source': 'telegram', 'trusted_peer': 'verified-peer'}).encode()
        with self.assertRaises(AdapterError):
            self.service.receive_http(raw, '127.0.0.1', {})
        with self.assertRaises(AdapterError):
            self.receive(user_id='not-allowed')
        timestamp = (self.now - timedelta(minutes=1)).isoformat()
        signature = hmac.new(SECRET.encode(), timestamp.encode() + b'\nPOST\n/telegram\n' + raw, hashlib.sha256).hexdigest()
        with self.assertRaises(AdapterError):
            self.auth.verify(raw, '127.0.0.1', {'X-Danta-Timestamp': timestamp, 'X-Danta-Signature': signature})

    def test_chat_and_new_never_call_model_or_clear_trading_ledger(self):
        self.receive('지금 전량 매수해줘 /review', update=1)
        self.service.run_once()
        self.receive('/new', update=2)
        self.service.run_once()
        self.assertEqual(self.app.review_calls, 0)
        self.assertEqual(self.app.store.get('cash_krw'), '10000000')
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM intents').fetchone()[0], 0)
        self.assertGreater(self.app.store.db.execute('SELECT COUNT(*) FROM journal').fetchone()[0], 0)

    def test_slow_model_does_not_block_pause_or_deadline_protection(self):
        self.app.review_release.clear()
        self.receive('/review', update=1)
        worker = threading.Thread(target=self.service.run_once, kwargs={'review': True})
        worker.start()
        try:
            self.assertTrue(self.app.review_entered.wait(1))
            self.receive('/pause', update=2)
            self.assertTrue(self.service.run_once())
            self.assertTrue(self.app.store.get('paused'))
            request_id, _ = self.app.store.accept_request('service:deadline:synthetic',
                {'source': 'scheduler', 'kind': 'time_limit_exit'})
            with self.app.store.transaction():
                self.app.store.set('service_deadline:' + request_id, (self.now - timedelta(seconds=1)).isoformat())
            self.assertTrue(self.service.run_once())
            self.assertEqual(self.app.protect_calls, 1)
            self.assertEqual(self.app.reconcile_calls, 1)
            self.assertTrue(worker.is_alive())
        finally:
            self.app.review_release.set()
            worker.join(2)

    def test_outbox_retry_does_not_repeat_review(self):
        self.receive('/review')
        self.service.run_once(review=True)
        self.fail_send = True
        self.assertFalse(self.service.outbox_once())
        self.fail_send = False
        self.assertTrue(self.service.outbox_once())
        self.assertEqual(self.app.review_calls, 1)
        self.assertEqual(self.sent[0], self.sent[1])
        self.assertEqual(self.app.store.db.execute('SELECT attempts FROM outbox').fetchone()[0], 2)

    def test_restart_recovers_result_and_never_reexecutes_interrupted_trade(self):
        self.receive('/review')
        row = self.app.store.db.execute("SELECT request_id FROM requests WHERE request_key LIKE 'service:%'").fetchone()
        with self.app.store.transaction():
            self.app.store.db.execute("UPDATE requests SET status='RUNNING' WHERE request_id=?", (row[0],))
        self.service._recover()
        self.assertFalse(self.service.run_once(review=True))
        result = json.loads(self.app.store.db.execute('SELECT result FROM requests WHERE request_id=?', (row[0],)).fetchone()[0])
        self.assertEqual(result['status'], 'INTERRUPTED_RECONCILE_REQUIRED')
        self.assertEqual(self.app.review_calls, 0)

    def test_expired_review_and_schedule_off_are_enforced_at_execution(self):
        self.receive('/review')
        self.now += timedelta(seconds=121)
        self.service.run_once(review=True)
        self.assertEqual(self.app.review_calls, 0)
        self.receive('/schedule_off', update=2)
        self.service.run_once()
        self.assertFalse(self.app.store.get('discretionary_schedule'))
        self.assertFalse(self.app.store.get('paused'))

    def test_scheduler_uses_durable_typed_identity(self):
        self.service.scheduler['enabled'] = True
        with self.app.store.transaction():
            self.app.store.set('discretionary_schedule', True)
        self.assertGreater(self.service.queue_tick(), 0)
        self.assertEqual(self.service.queue_tick(), 0)
        rows = [json.loads(row[0]) for row in self.app.store.db.execute(
            "SELECT payload FROM requests WHERE request_key LIKE 'service:schedule:%'")]
        self.assertTrue(any(row['kind'] == 'full_review' for row in rows))
        self.assertTrue(all('expires_at' in row for row in rows))

    def test_event_schedule_accepts_first_collected_but_rejects_uncertain_or_unavailable(self):
        self.service.scheduler['enabled'] = True
        self.app.bundle.events = [SimpleNamespace(event_id=identity, official=official,
            primary_source_complete=complete, timing_quality=timing, available_at=available)
            for identity, timing, official, complete, available in (
                ('exact', 'EXACT', True, True, self.now - timedelta(minutes=1)),
                ('first', 'FIRST_COLLECTED', True, True, self.now - timedelta(minutes=1)),
                ('uncertain', 'UNCERTAIN', True, True, self.now - timedelta(minutes=1)),
                ('unofficial', 'FIRST_COLLECTED', False, True, self.now - timedelta(minutes=1)),
                ('incomplete', 'FIRST_COLLECTED', True, False, self.now - timedelta(minutes=1)),
                ('future', 'FIRST_COLLECTED', True, True, self.now + timedelta(minutes=1)),
                ('previous', 'FIRST_COLLECTED', True, True, self.now - timedelta(days=1)),
            )]
        self.service.queue_tick()
        events = [json.loads(row[0]) for row in self.app.store.db.execute(
            "SELECT payload FROM requests WHERE request_key LIKE 'service:schedule:%'")]
        self.assertEqual({event['event_id'] for event in events if event['kind'] == 'event_review'}, {'exact', 'first'})
        self.assertEqual(self.service.queue_tick(), 0)

    def test_candidate_commands_delegate_one_argument_without_calling_trade_workflow(self):
        commands = ('add_portfolio_ticker', 'remove_portfolio_ticker',
                    'add_portfolio_except_ticker', 'remove_portfolio_except_ticker')
        for update, command in enumerate(commands, 1):
            self.receive('/' + command + ' SYNTHETIC001', update=update)
            self.assertTrue(self.service.run_once())
            self.assertEqual(self.last_result()['status'], 'FAKE_CANDIDATE_LIST_UPDATED')
        self.assertEqual(self.app.candidate_updates, [(command, 'SYNTHETIC001') for command in commands])
        for update, text in enumerate(('/add_portfolio_ticker', '/remove_portfolio_except_ticker ONE TWO'), 10):
            self.receive(text, update=update)
            self.service.run_once()
            self.assertEqual(self.last_result()['error_type'], 'ValueError')
        self.assertEqual(len(self.app.candidate_updates), 4)
        self.assertEqual(self.app.review_calls, 0)
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM intents').fetchone()[0], 0)

    def test_candidate_commands_and_resume_recheck_control_approval(self):
        self.receive('/add_portfolio_ticker SYNTHETIC001')
        self.app.approval['capabilities'].remove('telegram_control')
        self.service.run_once()
        self.assertEqual(self.last_result()['error_type'], 'HumanRequired')
        self.assertEqual(self.app.candidate_updates, [])
        for update, command in enumerate(('add_portfolio_ticker', 'remove_portfolio_ticker',
                'add_portfolio_except_ticker', 'remove_portfolio_except_ticker', 'resume'), 2):
            with self.assertRaises(HumanRequired):
                self.receive('/' + command + (' SYNTHETIC001' if command != 'resume' else ''), update=update)
        self.assertEqual(self.app.resume_calls, 0)

    def test_resume_delegates_common_approval_checks_and_rejects_arguments(self):
        self.receive('/resume')
        self.service.run_once()
        self.assertEqual(self.last_result()['status'], 'FAKE_RESUMED')
        self.receive('/resume override', update=2)
        self.service.run_once()
        self.assertEqual(self.last_result()['error_type'], 'ValueError')
        self.assertEqual(self.app.resume_calls, 1)
        self.app.resume_error = HumanRequired('Synthetic drawdown approval missing')
        self.receive('/resume', update=3)
        self.service.run_once()
        self.assertEqual(self.last_result()['error_type'], 'HumanRequired')
        self.assertEqual(self.last_result()['reason'], 'Synthetic drawdown approval missing')

    def test_reasoning_effort_change_is_durable_approval_request_without_policy_change(self):
        current = self.config.app['model']['reasoning_effort']
        version = self.app.store.get('account_version')
        self.receive('/reasoning_effort high')
        self.service.run_once()
        result = self.last_result()
        self.assertEqual(result['status'], 'APPROVAL_REQUIRED')
        self.assertEqual(result['requested_effort'], 'high')
        self.assertFalse(result['changed'])
        self.assertEqual(self.app.store.get('approval_request:' + result['request_id']), result)
        self.assertEqual(self.app.store.get('account_version'), version)
        self.assertEqual(self.config.app['model']['reasoning_effort'], current)
        self.receive('/reasoning_effort high')
        self.assertFalse(self.service.run_once())
        self.assertEqual(self.app.store.db.execute("SELECT COUNT(*) FROM journal WHERE kind='REASONING_EFFORT_CHANGE_REQUESTED'").fetchone()[0], 1)
        self.assertEqual(self.app.review_calls, 0)

    def test_reasoning_effort_query_is_read_only_and_change_needs_control_approval(self):
        self.app.approval['capabilities'].remove('telegram_control')
        self.receive('/reasoning_effort')
        self.service.run_once()
        self.assertFalse(self.last_result()['changed'])
        self.receive('/reasoning_effort high', update=2)
        self.service.run_once()
        self.assertEqual(self.last_result()['error_type'], 'HumanRequired')
        self.assertEqual(self.app.store.db.execute("SELECT COUNT(*) FROM journal WHERE kind='REASONING_EFFORT_CHANGE_REQUESTED'").fetchone()[0], 0)
        self.app.approval['capabilities'].append('telegram_control')
        for update, text in enumerate(('/reasoning_effort high extra', '/reasoning_effort bad/value'), 3):
            self.receive(text, update=update)
            self.service.run_once()
            self.assertEqual(self.last_result()['error_type'], 'ValueError')

    def test_unverified_ingress_and_recurring_synthetic_input_are_blocked(self):
        self.app.approval['capabilities'].remove('telegram_ingress')
        with self.assertRaises(HumanRequired):
            self.receive()
        self.app.approval['capabilities'].append('telegram_ingress')
        self.app.bundle.synthetic = True
        self.service.scheduler['enabled'] = True
        # A synthetic bundle never enters the recurring queue, even after enabling it in memory.
        self.assertEqual(self.service.queue_tick(), 0)

    def test_default_serve_opens_no_socket(self):
        data = self.config.data
        data['app']['telegram']['enabled'] = False
        data['app']['telegram']['ingress_enabled'] = False
        for key, value in data.items():
            (self.directory / (key + '.yaml')).write_text(yaml.safe_dump(value, allow_unicode=True))
        configuration = load_config(self.directory)
        stop = threading.Event()
        stop.set()
        self.service.close()
        self.app.close()
        self.app = FakeApp(configuration, self.now)
        # serve owns close; replace the app close callback only to keep tearDown single-close.
        with patch.object(self.app, 'close') as close:
            serve(configuration, application_factory=lambda config, args: self.app, stop_event=stop)
            close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
