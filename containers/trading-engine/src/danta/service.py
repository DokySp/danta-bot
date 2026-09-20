"""Durable typed service ingress; HTTP never waits for a model or broker."""
from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
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


class Service:
    def __init__(self, app, *, telegram=None, clock=utcnow):
        self.app, self.store, self.config, self.clock = app, app.store, app.config, clock
        self.telegram = telegram
        self.stop = threading.Event()
        self.worker_failed = False
        self.threads = []
        self.scheduler = self.config.data['schedules']['scheduler']
        monitoring = self.config.app['monitoring']
        self.planner = SchedulePlanner(self.scheduler['jobs'],
            quote_poll_seconds=monitoring['quote_poll_fallback_seconds'],
            order_poll_seconds=monitoring['order_poll_active_seconds'])
        tg = self.config.app['telegram']
        if tg['ingress_enabled']:
            self.config.require_external('telegram_ingress', self.app.approval)
            if not tg['enabled'] or not tg['allowed_sender_ids'] or not tg['allowed_chat_ids']:
                raise HumanRequired('Telegram ingress needs enabled adapter and allowed senders/chats')
        if self.telegram is None and tg['enabled']:
            self.config.require_external('telegram_send', self.app.approval)
            gateway = load_secrets(self.config.directory).get(tg['gateway_url_env'])
            if not gateway:
                raise HumanRequired('Telegram gateway URL is unresolved')
            self.telegram = TelegramAdapter(self.store.db, enabled=tg['ingress_enabled'],
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
            rows = self.store.db.execute("SELECT request_id,payload FROM requests WHERE request_key LIKE 'service:%' AND status='RUNNING'").fetchall()
            for row in rows:
                payload = json.loads(row['payload'])
                if payload.get('source') == 'scheduler' and payload.get('kind') == 'finalize_and_report':
                    # NAV finalization is idempotent; result and document outbox commit together.
                    # Resume only this report path, never an interrupted trading request.
                    self.store.db.execute("UPDATE requests SET status='ACCEPTED' WHERE request_id=?", (row['request_id'],))
                    self.store.event(row['request_id'], 'SERVICE_REQUEST_RECOVERED', {'status': 'REPORT_REQUEUED'})
                    continue
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

    def receive_http(self, raw_body):
        if not self.config.app['telegram']['ingress_enabled'] or not self.telegram:
            raise AdapterError('TELEGRAM_INGRESS_DISABLED')
        self.config.assert_current()
        self.config.require_external('telegram_ingress', self.app.approval)
        if len(raw_body) > 65536:
            raise AdapterError('TELEGRAM_PAYLOAD_TOO_LARGE')
        try:
            body = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError):
            raise AdapterError('INVALID_TELEGRAM_JSON') from None
        with self.store.lock:
            acknowledgement, request = self.telegram.receive(body)
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
        queued = 0
        with self.store.transaction():
            discretionary = self.store.get('discretionary_schedule', self.scheduler['enabled']) and not self.store.get('paused', False)
            scheduled = self.store.db.execute(
                "SELECT request_id,request_key,status,result FROM requests WHERE request_key LIKE 'service:schedule:%'").fetchall()
            seen = {row['request_key'][len('service:schedule:'):] for row in scheduled}
            for row in scheduled:
                result = json.loads(row['result']) if row['result'] else {}
                if (self.scheduler['enabled'] and discretionary and row['status'] == 'COMPLETE'
                        and row['request_key'].startswith('service:schedule:' + session.session_id + ':')
                        and result.get('status') == 'NAV_NOT_FINALIZED' and result.get('retry_at')
                        and aware_time(result['retry_at']) <= now
                        < aware_time(self.store.get('service_deadline:' + row['request_id']))):
                    self.store.db.execute("UPDATE requests SET status='ACCEPTED' WHERE request_id=?", (row['request_id'],))
                    queued += 1
        events = [{'event_id': event.event_id, 'verified_at': event.available_at, 'verified': True}
                  for event in self.app.bundle.events if event.official and event.primary_source_complete
                  and event.timing_quality in {'EXACT', 'FIRST_COLLECTED'}
                  and session.opens_at <= event.available_at <= now]
        intents = self.planner.due(now, session_id=session.session_id,
            continuous_open=session.opens_at, continuous_close=session.closes_at,
            enabled=self.scheduler['enabled'], discretionary_enabled=discretionary,
            last_seen=seen, events=events)
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
        return payload['kind'] in REVIEWS | {'collect_disclosures', 'finalize_and_report'} or payload.get('command') in {'review', 'chat'}

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
        document = result.pop('_document', None)
        with self.store.transaction():
            self.store.db.execute('UPDATE requests SET status=?,result=? WHERE request_id=?',
                ('COMPLETE', canonical(result), row['request_id']))
            self.store.event(row['request_id'], 'SERVICE_RESULT', result)
            if payload.get('source') == 'telegram':
                self.store.db.execute("UPDATE telegram_requests SET status='COMPLETE' WHERE request_id=?", (payload['telegram_request_id'],))
                notification = {'route': payload['route'], 'chat_id': payload['chat_id'],
                                'text': result.get('reply_text') or canonical(result)}
                self.store.db.execute('INSERT OR IGNORE INTO outbox(event_key,payload) VALUES (?,?)',
                    ('service:' + row['request_id'], canonical(notification)))
            if document:
                self.store.queue_document('report:' + row['request_id'], **document,
                    route=payload.get('route'), chat_id=payload.get('chat_id'))
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
                key = 'chat_session:' + canonical([payload['route'], payload['chat_id'], payload['user_id']])
                with self.store.transaction():
                    session_id = self.store.get(key)
                    if command == 'new' or session_id is None:
                        if session_id:
                            self.store.set('chat_history:' + session_id, [])
                        session_id = str(uuid4())
                        self.store.set(key, session_id)
                    history = self.store.get('chat_history:' + session_id, [])
                if command != 'chat':
                    return {'status': 'NEW_SESSION' if command == 'new' else 'SESSION', 'session_id': session_id,
                            'model_called': False, 'orders_created': False}
                callback = getattr(self.app, 'chat', None)
                if callback is None:
                    raise HumanRequired('General conversation model is unavailable in this runtime')
                self.config.require_external('model_call', self.app.approval)
                text = payload['text'].strip()
                if text.startswith('/chat '):
                    text = text[6:].strip()
                if not text or len(text) > 4000:
                    raise ValueError('Chat text must contain 1 to 4000 characters')
                messages = history + [{'role': 'user', 'content': text}]
                result = callback(request_id=request_id, session_id=session_id, messages=messages)
                with self.store.transaction():
                    if self.store.get(key) != session_id:
                        return {'status': 'SESSION_CHANGED', 'model_called': result['model_called'], 'orders_created': False}
                    if result['status'] == 'CHAT_COMPLETE':
                        messages = messages + [{'role': 'assistant', 'content': result['reply_text']}]
                        # ponytail: retain 10 recent turns; add explicit archival retrieval if longer context is needed.
                        self.store.set('chat_history:' + session_id, messages[-20:])
                return result
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
            try:
                finalization = self.app.finalize_nav()
            except AdapterError as error:
                if error.code not in {'TRANSPORT_FAILED', 'AUTH_TRANSPORT_FAILED', 'RATE_LIMITED',
                                      'TRANSIENT_FAILURE', 'MONITOR_DEGRADED_RATE_BUDGET'}:
                    raise
                finalization = {'status': 'NAV_NOT_FINALIZED', 'issues': [error.code]}
            if finalization['status'] == 'NAV_NOT_FINALIZED':
                return {'status': 'NAV_NOT_FINALIZED', 'finalization': finalization,
                        'retry_at': (self.clock() + timedelta(minutes=5)).isoformat()}
            return {**self._report(), 'finalization': finalization}
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
            document = {'filename': f'daily-{date}.html', 'content': Path(paths['html']).read_text(encoding='utf-8')}
        return {'status': 'REPORT_READY', 'report': data, 'paths': paths, '_document': document}

    def _document_secret_scan(self, content):
        text = html.unescape(content.decode('utf-8'))
        secrets = load_secrets(self.config.directory)
        return not any(value and value in text for name, value in secrets.items()
            if name in {'KIS_ACCOUNT_REF', 'KIS_APP_KEY', 'KIS_APP_SECRET', 'DART_API_KEY'})

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
                if 'document' in payload:
                    raise AdapterError('DOCUMENT_DESTINATION_UNRESOLVED')
                raise HumanRequired('Notification destination is unresolved')
            if 'document' in payload:
                document = payload['document']
                self.telegram.send_document(route, chat, document['filename'], document['content'].encode('utf-8'),
                    secret_scan=self._document_secret_scan)
            else:
                text = payload.get('text') or canonical(payload)
                if len(text) > 3500:
                    text = text[:3400] + '\n… 전체 결과는 저장된 보고서에서 확인하세요.'
                self.telegram.send_message(route, chat, text)
            state = 'DELIVERED'
        except Exception as error:
            blocked = ('document' in payload and isinstance(error, AdapterError)
                and (error.code.startswith('DOCUMENT_') or error.code == 'INVALID_DOCUMENT'))
            state = 'BLOCKED' if blocked else 'PENDING'
            with self.store.transaction():
                self.store.set('outbox_last_outcome:' + str(row['id']),
                    {'status': 'DOCUMENT_BLOCKED' if blocked else 'DELIVERY_UNCONFIRMED',
                     'error_type': type(error).__name__, 'reason': error.code if blocked else None})
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
                    self.worker_failed = True
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
                self.respond(202, service.receive_http(raw))
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
            # Compose exposes this endpoint only within the gateway Docker network.
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
    if service.worker_failed:
        raise OSError('SERVICE_WORKER_FAILED')
