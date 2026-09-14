"""Durable typed service ingress; HTTP never waits for a model or broker."""
from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import stat
import threading
from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from .adapters import AdapterError, http_transport
from .adapters.scheduler import SchedulePlanner
from .adapters.telegram import READ_COMMANDS, TelegramAdapter
from .config import HumanRequired, aware_time, canonical, load_secrets, utcnow
from .reporting import write_report


PORTFOLIO_CONTROLS = frozenset({'add_portfolio_ticker', 'remove_portfolio_ticker',
                              'add_portfolio_except_ticker', 'remove_portfolio_except_ticker'})
CONTROLS = frozenset({'pause', 'stop', 'schedule_on', 'schedule_off', 'new', 'resume'}) | PORTFOLIO_CONTROLS
PROTECTION = frozenset({'risk_monitor', 'reconcile', 'time_limit_exit'})
REVIEWS = frozenset({'full_review', 'event_review'})


class PeerAuthenticator:
    """A separately approved gateway/proxy signs exact bytes and transport time.

    The delivered gateway wire contract has no authentication header. This
    optional deployment contract must be verified before ingress is enabled.
    """
    def __init__(self, profile, config_hash, *, env=None, clock=utcnow):
        self.clock = clock
        required = {'schema_version', 'identity', 'allowed_source_ips', 'secret_env',
                    'verified', 'evidence_id', 'expires_at', 'config_hash'}
        if (set(profile) != required or profile['schema_version'] != 1 or profile['verified'] is not True
                or not profile['evidence_id'] or profile['config_hash'] != config_hash):
            raise HumanRequired('Trusted gateway transport proof is missing or mismatched')
        self.identity = profile['identity']
        self.addresses = {str(ipaddress.ip_address(value)) for value in profile['allowed_source_ips']}
        self.expires_at = aware_time(profile['expires_at'])
        secret = (os.environ if env is None else env).get(profile['secret_env'], '')
        if not self.identity or not self.addresses or len(secret) < 32:
            raise HumanRequired('Trusted gateway identity, addresses and secret are required')
        self.secret = secret.encode()

    @classmethod
    def from_file(cls, path, config_hash, **kwargs):
        path = Path(path).absolute()
        for current in (path, *path.parents):
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise HumanRequired('Trusted peer profile path must be root-owned and not writable by the app')
        return cls(json.loads(path.read_text()), config_hash, **kwargs)

    def verify(self, raw_body: bytes, client_ip: str, headers) -> str:
        now = self.clock()
        if now >= self.expires_at or str(ipaddress.ip_address(client_ip)) not in self.addresses:
            raise AdapterError('UNTRUSTED_GATEWAY_TRANSPORT')
        timestamp = headers.get('X-Danta-Timestamp', '')
        signature = headers.get('X-Danta-Signature', '')
        try:
            when = aware_time(timestamp)
        except (ValueError, TypeError):
            raise AdapterError('INVALID_GATEWAY_PROOF') from None
        if abs((now - when).total_seconds()) > 30:
            raise AdapterError('EXPIRED_GATEWAY_PROOF')
        message = timestamp.encode() + b'\nPOST\n/telegram\n' + raw_body
        expected = hmac.new(self.secret, message, hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise AdapterError('INVALID_GATEWAY_PROOF')
        return self.identity


class Service:
    def __init__(self, app, *, telegram=None, peer_auth=None, clock=utcnow):
        self.app, self.store, self.config, self.clock = app, app.store, app.config, clock
        self.telegram, self.peer_auth = telegram, peer_auth
        self.stop = threading.Event()
        self.threads = []
        self.scheduler = self.config.data['schedules']['scheduler']
        monitoring = self.config.app['monitoring']
        self.planner = SchedulePlanner(self.scheduler['jobs'],
            quote_poll_seconds=monitoring['quote_poll_fallback_seconds'],
            order_poll_seconds=monitoring['order_poll_active_seconds'])
        tg = self.config.app['telegram']
        if tg['ingress_enabled']:
            self.config.require_external('telegram_ingress', self.app.approval)
            if not tg['enabled'] or not tg['trusted_peer_profile']:
                raise HumanRequired('Telegram ingress needs enabled adapter and verified transport profile')
            if self.peer_auth is None:
                self.peer_auth = PeerAuthenticator.from_file(tg['trusted_peer_profile'], self.config.config_hash,
                    env=load_secrets(self.config.directory), clock=clock)
        if self.telegram is None and tg['enabled']:
            self.config.require_external('telegram_send', self.app.approval)
            gateway = load_secrets(self.config.directory).get(tg['gateway_url_env'])
            if not gateway:
                raise HumanRequired('Telegram gateway URL is unresolved')
            self.telegram = TelegramAdapter(self.store.db, enabled=tg['ingress_enabled'],
                trusted_peers=[self.peer_auth.identity] if self.peer_auth else [],
                allowed_senders=tg['allowed_sender_ids'], allowed_chats=tg['allowed_chat_ids'],
                gateway_url=gateway, authorize=self._authorize_control,
                transport=http_transport(allowed_origins={gateway.rstrip('/')}, network_enabled=True))
        if self.app.bundle.synthetic and (self.scheduler['enabled'] or monitoring['enabled']):
            raise HumanRequired('Synthetic snapshots cannot be repeatedly scheduled as current market observations')
        self._recover()

    def _authorize_control(self, command, user_id, chat_id):
        if command == 'reasoning_effort':
            return  # Query allowed; dispatch separately authorizes a change request.
        if command in CONTROLS or command == 'review':
            self.config.require_external('telegram_control', self.app.approval)
        elif command not in READ_COMMANDS:
            raise HumanRequired('This command requires the trusted local operator configuration/approval workflow')

    def _recover(self):
        with self.store.transaction():
            rows = self.store.db.execute("SELECT request_id FROM requests WHERE request_key LIKE 'service:%' AND status='RUNNING'").fetchall()
            for row in rows:
                workflow = self.store.db.execute('SELECT status,result FROM requests WHERE request_key=?',
                    ('workflow:' + row['request_id'],)).fetchone()
                result = json.loads(workflow['result']) if workflow and workflow['result'] else {
                    'status': 'INTERRUPTED_RECONCILE_REQUIRED', 'reexecute_trade': False}
                self.store.db.execute('UPDATE requests SET status=?,result=? WHERE request_id=?',
                    ('RECOVERED_RESULT' if workflow and workflow['result'] else 'INTERRUPTED', canonical(result), row['request_id']))
                self.store.event(row['request_id'], 'SERVICE_REQUEST_RECOVERED', result, notify=True)
            self.store.db.execute("UPDATE outbox SET state='PENDING' WHERE state='SENDING'")
            if self.telegram is not None:
                # No receipt timestamp means the previous HTTP acknowledgement was not durable.
                self.store.db.execute("UPDATE telegram_requests SET status='INTERRUPTED' WHERE status='PENDING'")

    def receive_http(self, raw_body, client_ip, headers):
        if not self.config.app['telegram']['ingress_enabled'] or not self.telegram or not self.peer_auth:
            raise AdapterError('TELEGRAM_INGRESS_DISABLED')
        self.config.assert_current()
        self.config.require_external('telegram_ingress', self.app.approval)
        if len(raw_body) > 65536:
            raise AdapterError('TELEGRAM_PAYLOAD_TOO_LARGE')
        peer = self.peer_auth.verify(raw_body, client_ip, headers)
        try:
            body = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError):
            raise AdapterError('INVALID_TELEGRAM_JSON') from None
        with self.store.lock:
            acknowledgement, request = self.telegram.receive(body, trusted_peer=peer)
            receipt = self.store.db.execute('SELECT status FROM telegram_requests WHERE request_id=?', (request.request_id,)).fetchone()
            if receipt['status'] == 'INTERRUPTED':
                raise AdapterError('INTERRUPTED_RECEIPT_REQUIRES_NEW_UPDATE')
            payload = {'source': 'telegram', 'kind': 'telegram', 'command': request.command,
                'telegram_request_id': request.request_id, 'route': request.route,
                'chat_id': request.chat_id, 'user_id': request.user_id, 'text': request.text}
            queue_id, fresh = self.store.accept_request('service:telegram:' + request.request_id, payload)
            with self.store.transaction():
                if fresh:
                    self.store.set('service_deadline:' + queue_id, (self.clock() + timedelta(seconds=120)).isoformat())
                    self.store.db.execute("UPDATE telegram_requests SET status='QUEUED' WHERE request_id=?", (request.request_id,))
        return acknowledgement

    def queue_tick(self):
        """Calendar job identity is durable; no catch-up review after its deadline."""
        now = self.clock()
        self.config.assert_current()
        if self.app.bundle.synthetic:
            return 0
        calendar = self.app.bundle.calendar
        session = calendar.active(now)
        if session is None:
            session = next((s for s in reversed(calendar.sessions) if s.closes_at <= now < s.closes_at + timedelta(hours=12)), None)
        if session is None:
            return 0
        with self.store.lock:
            seen = {row[0][len('service:schedule:'):] for row in self.store.db.execute(
                "SELECT request_key FROM requests WHERE request_key LIKE 'service:schedule:%'")}
            discretionary = self.store.get('discretionary_schedule', self.scheduler['enabled']) and not self.store.get('paused', False)
        events = [{'event_id': event.event_id, 'verified_at': event.available_at, 'verified': True}
                  for event in self.app.bundle.events if event.official and event.primary_source_complete
                  and event.timing_quality in {'EXACT', 'FIRST_COLLECTED'}
                  and session.opens_at <= event.available_at <= now]
        intents = self.planner.due(now, session_id=session.session_id,
            continuous_open=session.opens_at, continuous_close=session.closes_at,
            enabled=self.scheduler['enabled'], discretionary_enabled=discretionary,
            last_seen=seen, events=events)
        queued = 0
        for intent in intents:
            # start_monitor owns the regular protection/reconciliation loop.
            if intent.kind in {'risk_monitor', 'reconcile'}:
                continue
            if intent.kind in PROTECTION and not self.config.app['monitoring']['enabled']:
                continue
            payload = {'source': 'scheduler', 'kind': intent.kind, **intent.payload,
                       'due_at': intent.due_at.isoformat(), 'expires_at': intent.expires_at.isoformat()}
            request_id, fresh = self.store.accept_request('service:schedule:' + intent.key, payload)
            if fresh:
                with self.store.transaction():
                    self.store.set('service_deadline:' + request_id, intent.expires_at.isoformat())
                queued += 1
        return queued

    @staticmethod
    def _review_job(payload):
        return payload['kind'] in REVIEWS | {'collect_disclosures', 'finalize_and_report'} or payload.get('command') == 'review'

    def run_once(self, *, review=False):
        row = None
        with self.store.transaction():
            for candidate in self.store.db.execute("SELECT * FROM requests WHERE request_key LIKE 'service:%' AND status='ACCEPTED' ORDER BY rowid"):
                payload = json.loads(candidate['payload'])
                if self._review_job(payload) == review:
                    row = dict(candidate)
                    self.store.db.execute("UPDATE requests SET status='RUNNING' WHERE request_id=?", (row['request_id'],))
                    break
        if row is None:
            return False
        payload = json.loads(row['payload'])
        try:
            with self.store.lock:
                deadline = self.store.get('service_deadline:' + row['request_id'])
            if not deadline:
                result = {'status': 'INTERRUPTED_RECEIPT', 'reexecute_trade': False}
            elif payload['kind'] not in PROTECTION and self.clock() >= aware_time(deadline):
                result = {'status': 'EXPIRED', 'reason': 'Request execution deadline passed'}
            else:
                result = self._dispatch(payload, row['request_id'])
        except Exception as error:
            result = {'status': getattr(error, 'state', 'FAILED'), 'error_type': type(error).__name__}
            if isinstance(error, HumanRequired):
                result['reason'] = str(error)
        with self.store.transaction():
            self.store.db.execute('UPDATE requests SET status=?,result=? WHERE request_id=?',
                ('COMPLETE', canonical(result), row['request_id']))
            self.store.event(row['request_id'], 'SERVICE_RESULT', result)
            if payload.get('source') == 'telegram':
                self.store.db.execute("UPDATE telegram_requests SET status='COMPLETE' WHERE request_id=?", (payload['telegram_request_id'],))
                notification = {'route': payload['route'], 'chat_id': payload['chat_id'], 'text': canonical(result)}
                self.store.db.execute('INSERT OR IGNORE INTO outbox(event_key,payload) VALUES (?,?)',
                    ('service:' + row['request_id'], canonical(notification)))
        return True

    def _dispatch(self, payload, request_id):
        self.config.assert_current()
        kind = payload['kind']
        command = payload.get('command')
        if kind == 'telegram':
            if command not in READ_COMMANDS and command != 'chat':
                self._authorize_control(command, payload['user_id'], payload['chat_id'])
            if command in PORTFOLIO_CONTROLS:
                arguments = payload['text'].split()[1:]
                if len(arguments) != 1:
                    raise ValueError('Candidate-list commands require exactly one ticker')
                return self.app.update_candidate_list(command, arguments[0])
            if command == 'resume':
                if len(payload['text'].split()) != 1:
                    raise ValueError('Resume accepts no arguments; trusted approval is checked separately')
                return self.app.resume()
            if command in {'pause', 'stop'}:
                return self.app.pause()
            if command in {'schedule_on', 'schedule_off'}:
                if command == 'schedule_on' and (self.app.bundle.synthetic or not self.scheduler['enabled']):
                    raise HumanRequired('Enable an approved fresh-data scheduler configuration first')
                with self.store.transaction():
                    self.store.set('discretionary_schedule', command == 'schedule_on')
                return {'status': command.upper(), 'protection': 'CONTINUES'}
            if command in {'chat', 'session', 'new'}:
                key = 'chat_session:' + payload['chat_id'] + ':' + payload['user_id']
                with self.store.transaction():
                    session_id = self.store.get(key)
                    if command == 'new' or session_id is None:
                        session_id = str(uuid4())
                        self.store.set(key, session_id)
                return {'status': 'CHAT_ONLY', 'session_id': session_id,
                        'reply_text': '일반 대화는 거래·설정 변경 권한이 없습니다. /status /report /usage /session을 사용할 수 있습니다.',
                        'model_called': False, 'orders_created': False}
            if command == 'version':
                return {'code_id': self.app.code_id, 'config_hash': self.config.config_hash,
                        'strategy_hash': self.config.strategy_hash}
            if command == 'reasoning_effort':
                arguments = payload['text'].split()[1:]
                current = self.config.app['model']['reasoning_effort']
                if not arguments:
                    return {'reasoning_effort': current, 'changed': False}
                self.config.require_external('telegram_control', self.app.approval)
                if (len(arguments) != 1 or len(arguments[0]) > 32
                        or not arguments[0].isascii() or not arguments[0].isidentifier()):
                    raise ValueError('Reasoning-effort requests require one short identifier')
                result = {'status': 'APPROVAL_REQUIRED', 'request_id': request_id,
                    'current_effort': current, 'requested_effort': arguments[0], 'changed': False,
                    'config_hash': self.config.config_hash, 'created_at': self.clock().isoformat(),
                    'requested_by': {'user_id': payload['user_id'], 'chat_id': payload['chat_id']},
                    'reason': 'Model support and a new trusted configuration/approval must be verified'}
                with self.store.transaction():
                    self.store.set('approval_request:' + request_id, result)
                    self.store.event(request_id, 'REASONING_EFFORT_CHANGE_REQUESTED', result)
                return result
            if command == 'usage':
                with self.store.lock:
                    attempts = [json.loads(row[0]) for row in self.store.db.execute(
                        "SELECT payload FROM journal WHERE kind IN ('MODEL_ATTEMPT','MODEL_OUTCOME') ORDER BY sequence DESC LIMIT 100")]
                return {'status': 'RECORDED_ATTEMPTS' if attempts else 'UNKNOWN', 'attempts': attempts,
                        'operating_cost': 'UNCONFIRMED', 'subscription_quota_is_not_api_billing': True}
            if command == 'show_touch_point':
                return {'theses': [thesis.model_dump(mode='json') for thesis in self.app.theses()],
                        'as_of': self.app.bundle.now.isoformat()}
            if command == 'status':
                return self.app.status()
            if command == 'report':
                return self._report()
            if command == 'review':
                kind = 'full_review'
            else:
                raise HumanRequired('Use the trusted local operator workflow for this command')
        if kind in REVIEWS:
            with self.store.lock:
                paused = self.store.get('paused', False) or self.store.get('drawdown_paused', False)
                schedule_off = payload['source'] == 'scheduler' and not self.store.get('discretionary_schedule', self.scheduler['enabled'])
            if paused or schedule_off:
                return {'status': 'PAUSED', 'protection': 'CONTINUES'}
            if self.app.bundle.synthetic and payload['source'] == 'scheduler':
                raise HumanRequired('Synthetic input cannot drive a recurring market strategy')
            return self.app.review(kind=kind, event_id=payload.get('event_id'), request_key='workflow:' + request_id)
        if kind in PROTECTION:
            self.app.reconcile()
            return {'status': 'PROTECTION_CHECKED', 'result': self.app.protect()}
        if kind == 'collect_disclosures':
            if self.app.refresh is None:
                raise HumanRequired('Verified disclosure refresh adapter is unavailable')
            self.app.bundle = self.app.refresh()
            return {'status': 'COLLECTED', 'event_count': len(self.app.bundle.events)}
        if kind == 'finalize_and_report':
            if self.app.refresh:
                self.app.bundle = self.app.refresh()
            return self._report()
        raise ValueError('Unknown typed service request')

    def _report(self):
        date = self.clock().astimezone(ZoneInfo('Asia/Seoul')).date().isoformat()
        with self.store.lock:
            outcomes = [json.loads(row[0]) for row in self.store.db.execute(
                "SELECT payload FROM journal WHERE kind='RUN_OUTCOME' AND substr(created_at,1,10)=?", (date,))]
        data = {'schema_version': 1, 'created_at': self.clock().isoformat(), 'date': date,
                'config_hash': self.config.config_hash, 'strategy_hash': self.config.strategy_hash,
                'code_id': self.app.code_id, 'status': self.app.status(), 'runs': outcomes}
        directory = self.config.state_dir / 'reports' / date
        paths = write_report(data, directory / 'daily.json', directory / 'daily.html', '일일 판단·성과')
        return {'status': 'REPORT_READY', 'report': data, 'paths': paths}

    def outbox_once(self):
        if self.telegram is None or not self.config.app['telegram']['enabled']:
            return False
        self.config.assert_current()
        self.config.require_external('telegram_send', self.app.approval)
        with self.store.transaction():
            row = self.store.db.execute("SELECT * FROM outbox WHERE state='PENDING' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return False
            row = dict(row)
            self.store.db.execute("UPDATE outbox SET state='SENDING',attempts=attempts+1 WHERE id=?", (row['id'],))
        payload = json.loads(row['payload'])
        tg = self.config.app['telegram']
        route = payload.get('route') or tg['route']
        chat = payload.get('chat_id')
        if chat is None and len(tg['allowed_chat_ids']) == 1:
            chat = tg['allowed_chat_ids'][0]
        try:
            if not route or str(chat) not in set(map(str, tg['allowed_chat_ids'])):
                raise HumanRequired('Notification destination is unresolved')
            text = payload.get('text') or canonical(payload)
            if len(text) > 3500:
                text = text[:3400] + '\n… 전체 결과는 저장된 보고서에서 확인하세요.'
            self.telegram.send_message(route, chat, text)
            state = 'DELIVERED'
        except Exception as error:
            state = 'PENDING'
            with self.store.transaction():
                self.store.set('outbox_last_outcome:' + str(row['id']),
                    {'status': 'DELIVERY_UNCONFIRMED', 'error_type': type(error).__name__})
        with self.store.transaction():
            self.store.db.execute('UPDATE outbox SET state=? WHERE id=?', (state, row['id']))
        return state == 'DELIVERED'

    def start(self):
        if self.config.app['monitoring']['enabled']:
            self.app.start_monitor(self.config.app['monitoring']['quote_poll_fallback_seconds'])
        def worker(review):
            while not self.stop.wait(.1):
                try:
                    if not review:
                        self.queue_tick()
                    self.run_once(review=review)
                except Exception as error:
                    with self.store.transaction():
                        self.store.event('service', 'SERVICE_WORKER_FAILED', {'error_type': type(error).__name__})
                    self.stop.set()
        def notify():
            while not self.stop.wait(5):
                try:
                    self.outbox_once()
                except Exception as error:
                    with self.store.transaction():
                        self.store.event('service', 'NOTIFY_BLOCKED', {'error_type': type(error).__name__})
        for name, target, args in [('control', worker, (False,)), ('review', worker, (True,)), ('outbox', notify, ())]:
            thread = threading.Thread(target=target, args=args, name='danta-' + name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=20)
        if any(thread.is_alive() for thread in self.threads):
            raise HumanRequired('Service worker still running; retain writer lock until it exits')


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass  # Payloads, usernames and authentication headers are never HTTP logs.

        def respond(self, status, data):
            body = canonical(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.respond(200 if self.path == '/healthz' else 404,
                {'status': 'RUNNING' if not service.stop.is_set() else 'STOPPING', 'strategy': 'STRATEGY_UNPROVEN'}
                if self.path == '/healthz' else {'status': 'NOT_FOUND'})

        def do_POST(self):
            if self.path != '/telegram':
                self.respond(404, {'status': 'NOT_FOUND'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if self.headers.get('Transfer-Encoding') or not 0 < length <= 65536:
                    raise AdapterError('INVALID_REQUEST_SIZE')
                if self.headers.get_content_type() != 'application/json':
                    raise AdapterError('JSON_CONTENT_TYPE_REQUIRED')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise AdapterError('TRUNCATED_REQUEST')
                self.respond(202, service.receive_http(raw, self.client_address[0], self.headers))
            except (AdapterError, HumanRequired, ValueError, OSError) as error:
                self.respond(403, {'accepted': False, 'error_type': type(error).__name__})
    return Handler


def serve(config, args=None, *, application_factory=None, stop_event=None):
    """Run the same app as the CLI; default config opens no listening socket."""
    if application_factory is None:
        from .cli import make_application
        application_factory = make_application
    app = application_factory(config, args)
    service, server, http_thread = None, None, None
    try:
        service = Service(app)
        if config.app['telegram']['ingress_enabled']:
            host = config.app['app']['listen_host']
            # Non-loopback binding requires the already verified signed-peer gate.
            ipaddress.ip_address(host)
            server = ThreadingHTTPServer((host, config.app['app']['listen_port']), handler_for(service))
            server.daemon_threads = True
            http_thread = threading.Thread(target=server.serve_forever, name='danta-ingress', daemon=True)
            http_thread.start()
        service.start()
        while not service.stop.wait(.5):
            if stop_event is not None and stop_event.is_set():
                break
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if service:
            service.close()
        app.close()
