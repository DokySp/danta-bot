"""Service integration uses real SQLite/adapters and fake app/transport; no sockets."""
from contextlib import redirect_stdout
from datetime import timedelta
from decimal import Decimal
import base64
import html
import io
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from danta.adapters import AdapterError, HttpResponse
from danta.adapters.telegram import TelegramAdapter
from danta.cli import main
from danta.config import HumanRequired, ROOT, canonical, load_config, utcnow
from danta.market import SessionCalendar
from danta.models import Session
from danta.safety import CredentialError
from danta.service import RuntimeHost, Service, app_version, serve
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
        return {'status': 'FAKE_REVIEW_COMPLETE', 'kwargs': {key: value for key, value in kwargs.items() if key != 'on_progress'}}

    def reconcile(self):
        self.reconcile_calls += 1
        return {'status': 'FAKE_RECONCILED'}

    def protect(self):
        self.protect_calls += 1
        return []

    def finalize_nav(self):
        return {'status': 'NAV_NOT_FINALIZED', 'issues': ['SYNTHETIC_SERVICE_TEST']}

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
    def test_idle_worker_does_not_take_ledger_writer_lock(self):
        with patch.object(self.app.store,'transaction',side_effect=AssertionError('idle writer lock')):
            self.assertFalse(self.service.run_once(review=True))
            self.assertFalse(self.service.run_once())

    def setUp(self):
        self.no_network = patch('socket.socket', side_effect=AssertionError('Network forbidden in service tests'))
        self.no_network.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.now = utcnow()
        data = load_config(ROOT / 'tests/fixtures/config').data
        data['app']['app']['mode'] = 'paper'
        data['app']['app']['state_dir'] = str(Path(self.tmp.name) / 'state')
        data['app']['telegram'].update(enabled=True, ingress_enabled=True,
            allowed_sender_ids=['user-1'],
            allowed_chat_ids=['chat-1'], route='trading-engine')
        self.directory = Path(self.tmp.name) / 'config'
        self.directory.mkdir()
        for key, value in data.items():
            (self.directory / (key + '.yaml')).write_text(yaml.safe_dump(value, allow_unicode=True))
        secrets_file = self.directory / 'secrets.yaml'
        secrets_file.write_text('{}\n')
        secrets_file.chmod(0o600)
        self.config = load_config(self.directory)
        self.app = FakeApp(self.config, self.now)
        self.sent = []
        self.sent_urls = []
        self.fail_send = False
        self.adapter = TelegramAdapter(self.app.store.db, enabled=True,
            allowed_senders=['user-1'], allowed_chats=['chat-1'], transport=self.transport,
            authorize=lambda *args: None)
        self.service = Service(self.app, telegram=self.adapter, clock=lambda: self.now)
        self.adapter.authorize = self.service._authorize_control

    def tearDown(self):
        self.app.review_release.set()
        self.service.close()
        self.app.close()
        self.tmp.cleanup()
        self.no_network.stop()

    def transport(self, method, url, headers, body, timeout):
        self.sent.append(json.loads(body))
        self.sent_urls.append(url)
        if self.fail_send:
            raise AdapterError('SYNTHETIC_TIMEOUT')
        return HttpResponse(200, b'{"ok":true}')

    def receive(self, text='/status', update=1, **overrides):
        body = {'source': 'telegram', 'route': 'trading-engine', 'update_id': update,
                'chat_id': 'chat-1', 'user_id': 'user-1', 'text': text, **overrides}
        raw = canonical(body).encode()
        return self.service.receive_http(raw)

    def last_result(self):
        row = self.app.store.db.execute("SELECT result FROM requests WHERE request_key LIKE 'service:%' ORDER BY rowid DESC LIMIT 1").fetchone()
        return json.loads(row[0])

    def test_image_version_overrides_retained_environment_on_both_status_paths(self):
        version_file = Path(self.tmp.name) / 'VERSION'
        with patch('danta.service.VERSION_FILE', version_file), patch.dict(os.environ, APP_VERSION=' stale-env '):
            version_file.write_text('v20260923-002\n')
            self.receive('/status')
            self.service.run_once()
            self.assertEqual(self.last_result()['version'], 'v20260923-002')
            self.assertEqual(RuntimeHost(self.config).version()['version'], 'v20260923-002')
            for content in (b'', b'\xff', None):
                if content is None:
                    version_file.unlink()
                else:
                    version_file.write_bytes(content)
                self.assertEqual(app_version(), 'stale-env')
            with patch.dict(os.environ, APP_VERSION=' '):
                self.assertEqual(app_version(), 'dev')

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

    def test_unsigned_ingress_still_checks_sender_chat_route_and_payload(self):
        for overrides in ({'user_id': 'not-allowed'}, {'chat_id': 'not-allowed'},
                          {'route': 'other-engine'}, {'source': 'other-source'}):
            with self.subTest(overrides=overrides), self.assertRaises(AdapterError):
                self.receive(**overrides)
        with self.assertRaises(AdapterError):
            self.service.receive_http(b'{invalid-json')
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM telegram_requests').fetchone()[0], 0)

    def test_ingress_initializes_without_shared_secret_or_peer_profile(self):
        secret_file = self.directory / 'secrets.yaml'
        secret_file.write_text(yaml.safe_dump({'TELEGRAM_GATEWAY_URL': 'http://telegram-gateway:8080'}))
        service = Service(self.app, clock=lambda: self.now)
        self.addCleanup(service.close)
        body = {'source': 'telegram', 'route': 'trading-engine', 'update_id': 1,
                'chat_id': 'chat-1', 'user_id': 'user-1', 'text': '/status'}
        self.assertTrue(service.receive_http(canonical(body).encode())['accepted'])

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
        messages = [body for url, body in zip(self.sent_urls, self.sent) if url.endswith("/sendMessage")]
        self.assertEqual(messages[0], messages[1])
        self.assertEqual(self.app.store.db.execute('SELECT attempts FROM outbox').fetchone()[0], 2)

    def test_report_document_survives_overwrite_retry_and_restart_without_duplicate_text(self):
        app_file = self.directory / 'app.yaml'
        settings = yaml.safe_load(app_file.read_text())
        settings['telegram']['allowed_chat_ids'].append('chat-2')
        app_file.write_text(yaml.safe_dump(settings, allow_unicode=True))
        self.config = self.app.config = load_config(self.directory)
        self.app.approval['config_hash'] = self.config.config_hash
        self.adapter.chats.add('chat-2')
        self.service.close()
        self.service = Service(self.app, telegram=self.adapter, clock=lambda: self.now)

        self.receive('/report', chat_id='chat-2')
        self.assertTrue(self.service.run_once())
        result = self.last_result()
        original = Path(result['paths']['html']).read_bytes()
        self.assertNotIn('_document', result)
        self.assertNotIn('<!doctype', canonical(result))
        self.receive('/report', chat_id='chat-2')
        self.assertFalse(self.service.run_once())
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 2)

        self.assertTrue(self.service.outbox_once())
        self.assertTrue(self.sent_urls[-1].endswith('/sendMessage'))
        self.assertEqual(self.sent[-1]['chat_id'], 'chat-2')
        self.assertNotIn('<!doctype', self.sent[-1]['text'])
        Path(result['paths']['html']).write_text('<html>later report</html>')
        self.fail_send = True
        self.assertFalse(self.service.outbox_once())
        self.assertTrue(self.sent_urls[-1].endswith('/sendDocument'))
        self.assertEqual(base64.b64decode(self.sent[-1]['content_base64']), original)

        with self.app.store.transaction():
            self.app.store.db.execute("UPDATE outbox SET state='SENDING' WHERE state='PENDING'")
        self.service.close()
        self.service = Service(self.app, telegram=self.adapter, clock=lambda: self.now)
        self.fail_send = False
        self.assertTrue(self.service.outbox_once())
        self.assertFalse(self.service.outbox_once())
        self.assertEqual(self.sent[1], self.sent[2])
        self.assertEqual(self.sent[2]['route'], 'trading-engine')
        self.assertEqual(self.sent[2]['chat_id'], 'chat-2')
        self.assertEqual(self.sent[2]['filename'], 'daily-' + result['report']['date'] + '.html')
        self.assertEqual(sum(url.endswith('/sendMessage') for url in self.sent_urls), 1)
        self.assertEqual([tuple(row) for row in self.app.store.db.execute('SELECT state,attempts FROM outbox ORDER BY id')],
                         [('DELIVERED', 1), ('DELIVERED', 2)])
        self.assertEqual(self.app.review_calls, 0)
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM intents').fetchone()[0], 0)

    def test_scheduled_daily_report_queues_document_for_default_chat(self):
        self.app.finalize_nav = lambda: {'status': 'NAV_FINALIZED'}
        request_id, _ = self.app.store.accept_request('service:schedule:daily-report',
            {'source': 'scheduler', 'kind': 'finalize_and_report'})
        with self.app.store.transaction():
            self.app.store.set('service_deadline:' + request_id, (self.now + timedelta(minutes=1)).isoformat())
        self.assertTrue(self.service.run_once(review=True))
        result = self.last_result()
        self.assertEqual(result['status'], 'REPORT_READY')
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
        self.assertTrue(self.service.outbox_once())
        self.assertTrue(self.sent_urls[0].endswith('/sendDocument'))
        self.assertEqual((self.sent[0]['route'], self.sent[0]['chat_id']), ('trading-engine', 'chat-1'))
        self.assertEqual(base64.b64decode(self.sent[0]['content_base64']), Path(result['paths']['html']).read_bytes())
        self.assertFalse(self.service.run_once(review=True))
        self.assertFalse(self.service.outbox_once())

    def test_daily_finalization_retries_durably_then_queues_one_success_report(self):
        self.service.scheduler['enabled'] = True
        session = self.app.bundle.calendar.sessions[0]
        self.now = session.closes_at + timedelta(minutes=30)
        self.assertEqual(self.service.queue_tick(), 1)
        self.assertTrue(self.service.run_once(review=True))
        self.assertEqual(self.last_result()['status'], 'NAV_NOT_FINALIZED')
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)
        self.assertEqual(self.service.queue_tick(), 0)
        self.service.close()
        self.service = Service(self.app, telegram=self.adapter, clock=lambda: self.now)
        self.service.scheduler['enabled'] = True
        self.now += timedelta(minutes=5) - timedelta(seconds=1)
        self.assertEqual(self.service.queue_tick(), 0)
        self.now += timedelta(seconds=1)
        self.app.finalize_nav = lambda: {'status': 'NAV_FINALIZED'}
        self.assertEqual(self.service.queue_tick(), 1)
        self.assertEqual(self.service.queue_tick(), 0)
        self.assertTrue(self.service.run_once(review=True))
        self.assertEqual(self.last_result()['status'], 'REPORT_READY')
        self.now += timedelta(minutes=5)
        self.assertEqual(self.service.queue_tick(), 0)
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 1)
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

    def test_daily_finalization_does_not_retry_after_session_window(self):
        self.service.scheduler['enabled'] = True
        session = self.app.bundle.calendar.sessions[0]
        self.now = session.closes_at + timedelta(hours=12) - timedelta(minutes=1)
        self.assertEqual(self.service.queue_tick(), 1)
        self.assertTrue(self.service.run_once(review=True))
        self.now += timedelta(minutes=5)
        self.assertEqual(self.service.queue_tick(), 0)
        self.assertFalse(self.service.run_once(review=True))

    def test_daily_finalization_retries_transport_failure_then_succeeds(self):
        self.service.scheduler['enabled'] = True
        self.now = self.app.bundle.calendar.sessions[0].closes_at + timedelta(minutes=30)
        with patch.object(self.app, 'finalize_nav', side_effect=[AdapterError('TRANSPORT_FAILED'),
                                                               {'status': 'NAV_FINALIZED'}]) as finalize:
            self.assertEqual(self.service.queue_tick(), 1)
            self.assertTrue(self.service.run_once(review=True))
            self.assertEqual(self.last_result()['finalization']['issues'], ['TRANSPORT_FAILED'])
            self.assertEqual(self.service.queue_tick(), 0)
            self.now += timedelta(minutes=5)
            self.assertEqual(self.service.queue_tick(), 1)
            self.assertTrue(self.service.run_once(review=True))
            self.assertEqual(self.last_result()['status'], 'REPORT_READY')
            self.assertEqual(finalize.call_count, 2)
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 1)
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

    def test_daily_finalization_does_not_retry_authorization_failure(self):
        self.service.scheduler['enabled'] = True
        self.now = self.app.bundle.calendar.sessions[0].closes_at + timedelta(minutes=30)
        with patch.object(self.app, 'finalize_nav', side_effect=AdapterError('AUTH_FAILED')) as finalize:
            self.assertEqual(self.service.queue_tick(), 1)
            self.assertTrue(self.service.run_once(review=True))
            self.assertEqual(self.last_result()['status'], 'FAILED')
            self.assertNotIn('retry_at', self.last_result())
            self.now += timedelta(minutes=5)
            self.assertEqual(self.service.queue_tick(), 0)
            self.assertFalse(self.service.run_once(review=True))
            self.assertEqual(finalize.call_count, 1)
        self.assertEqual(self.app.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)

    def test_document_delivery_rechecks_destination_approval_and_secret_content(self):
        synthetic_value = 'synthetic-private<&>value-for-document-test'
        secrets_file = self.directory / 'secrets.yaml'
        secrets_file.write_text(yaml.safe_dump({'KIS_APP_SECRET': synthetic_value}))
        for identity, chat, content in (
            ('wrong-chat', 'unapproved-chat', '<html>safe report</html>'),
            ('credential-pattern', 'chat-1', '<html>' + 'sk-' + 'x' * 30 + '</html>'),
            ('configured-secret', 'chat-1', '<html>' + html.escape(synthetic_value) + '</html>'),
        ):
            with self.subTest(identity=identity):
                with self.app.store.transaction():
                    self.app.store.db.execute('DELETE FROM outbox')
                    if identity == 'credential-pattern':
                        with self.assertRaises(CredentialError):
                            self.app.store.queue_document(identity, 'report.html', content, route='trading-engine', chat_id=chat)
                        self.app.store.db.execute('INSERT INTO outbox(event_key,payload) VALUES (?,?)',
                            (identity, canonical({'route': 'trading-engine', 'chat_id': chat,
                                'document': {'filename': 'report.html', 'content': content}})))
                    else:
                        self.app.store.queue_document(identity, 'report.html', content, route='trading-engine', chat_id=chat)
                self.assertFalse(self.service.outbox_once())
                self.assertEqual(self.sent, [])
                self.assertEqual(self.app.store.db.execute('SELECT state FROM outbox').fetchone()[0], 'BLOCKED')
                with self.app.store.transaction():
                    self.app.store.event('service', 'SAFE_LATER_NOTIFICATION', {'status': 'safe later notification'}, notify=True)
                self.service._recover()
                self.assertTrue(self.service.outbox_once())
                self.assertEqual(len(self.sent), 1)
                self.assertTrue(self.sent_urls[-1].endswith('/sendMessage'))
                self.assertEqual(self.app.store.db.execute('SELECT state FROM outbox ORDER BY id LIMIT 1').fetchone()[0], 'BLOCKED')
                self.sent.clear()
                self.sent_urls.clear()
        with self.app.store.transaction():
            self.app.store.db.execute('DELETE FROM outbox')
            self.app.store.queue_document('expired-approval', 'report.html', '<html>safe report</html>',
                                          route='trading-engine', chat_id='chat-1')
        self.app.approval['expires_at'] = (utcnow() - timedelta(seconds=1)).isoformat()
        with self.assertRaises(HumanRequired):
            self.service.outbox_once()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.app.store.db.execute('SELECT attempts FROM outbox').fetchone()[0], 0)

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

    def test_scheduled_review_deadline_is_durable_before_worker_claim(self):
        self.service.scheduler['enabled'] = True
        accept = self.app.store.accept_request
        def claim_immediately(key, payload, **kwargs):
            result = accept(key, payload, **kwargs)
            if payload['kind'] == 'full_review':
                self.assertTrue(self.service.run_once(review=True, chat=False))
            return result
        with patch.object(self.app.store, 'accept_request', side_effect=claim_immediately):
            self.service.queue_tick()
        self.assertEqual(self.app.review_calls, 1)
        self.assertEqual(self.service.queue_tick(), 0)
        with patch.object(self.app.store, 'set', side_effect=RuntimeError('synthetic deadline write failure')):
            with self.assertRaises(RuntimeError):
                accept('service:atomic-failure', {'kind': 'full_review'}, deadline=self.now.isoformat())
        self.assertFalse(self.app.store.read("SELECT 1 FROM requests WHERE request_key='service:atomic-failure'"))

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
            if len(text.split()) == 1:
                self.assertIn('005930', self.last_result()['reply_text'])
            else:
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

    def test_unconfigured_serve_keeps_diagnostics_without_creating_a_fixture_worker(self):
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
        with patch('danta.service.ThreadingHTTPServer') as server, patch('danta.cli.make_application') as factory:
            server.return_value.server_port = 8080
            serve(configuration, application_factory=factory, stop_event=stop)
            server.assert_called_once()
            factory.assert_not_called()
        with patch('danta.cli.make_application', return_value=self.app), \
                patch('danta.service.ThreadingHTTPServer', return_value=SimpleNamespace(
                    server_port=8080, serve_forever=lambda: None, shutdown=lambda: None, server_close=lambda: None)), \
                patch('danta.service.serve', side_effect=lambda *args, **kwargs: serve(*args, **kwargs, stop_event=stop)), \
                patch.object(self.app, 'close') as close, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--config-dir', str(self.directory), 'serve']), 0)
            self.assertEqual(json.loads(output.getvalue()), {'status': 'STOPPED'})
            close.assert_not_called()

    def test_worker_stops_even_if_failure_journal_cannot_be_written(self):
        with patch.object(self.service,'queue_tick',side_effect=sqlite3.OperationalError('fixture')), \
                patch.object(self.service.store,'event',side_effect=sqlite3.OperationalError('fixture journal')):
            self.service.start()
            try:
                self.assertTrue(self.service.stop.wait(3))
                self.assertTrue(self.service.worker_failed)
            finally:
                self.service.close()

    def test_worker_failure_reaches_serve_and_cli_after_cleanup_without_error_text(self):
        data = self.config.data
        data['app']['telegram']['enabled'] = False
        data['app']['telegram']['ingress_enabled'] = False
        for key, value in data.items():
            (self.directory / (key + '.yaml')).write_text(yaml.safe_dump(value, allow_unicode=True))
        configuration = load_config(self.directory)
        self.service.close()
        self.app.close()
        self.app = FakeApp(configuration, self.now)
        for use_cli in (False, True):
            with self.subTest(use_cli=use_cli), patch.object(self.app, 'close') as close, \
                    patch('danta.service.ThreadingHTTPServer', return_value=SimpleNamespace(
                        server_port=8080, serve_forever=lambda: None, shutdown=lambda: None, server_close=lambda: None)), \
                    patch('danta.service.RuntimeHost.requirements', return_value=[]), \
                    patch.object(Service, 'queue_tick', side_effect=RuntimeError(SECRET)):
                if use_cli:
                    with patch('danta.cli.make_application', return_value=self.app), redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(main(['--config-dir', str(self.directory), 'serve']), 1)
                    self.assertEqual(json.loads(output.getvalue()),
                        {'status': 'FAILED', 'error_type': 'OSError', 'reason': 'SERVICE_WORKER_FAILED'})
                else:
                    with self.assertRaisesRegex(OSError, '^SERVICE_WORKER_FAILED$'):
                        serve(configuration, application_factory=lambda config, args: self.app)
                close.assert_called_once()
                self.assertFalse(any(thread.name.startswith('danta-') for thread in threading.enumerate()))
        failures = [json.loads(row[0]) for row in self.app.store.db.execute(
            "SELECT payload FROM journal WHERE kind='SERVICE_WORKER_FAILED'")]
        self.assertEqual([row['error_type'] for row in failures], ['RuntimeError'] * 2)
        self.assertTrue(all(row['frames'] and all(set(frame) == {'file', 'line', 'function'} for frame in row['frames']) for row in failures))


if __name__ == '__main__':
    unittest.main()
