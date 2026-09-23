"""Durable typed service ingress; HTTP never waits for a model or broker."""
from __future__ import annotations

import html
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import sqlite3
import os
from pathlib import Path
import signal
import sys
import threading
import traceback
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from .adapters import AdapterError, http_transport
from .adapters.scheduler import SchedulePlanner
from .adapters.telegram import MAX_REQUEST_BYTES, READ_COMMANDS, TelegramAdapter
from .config import HumanRequired, aware_time, canonical, digest, load_secrets, utcnow
from .reporting import render_notification, reported_fee, write_report


PORTFOLIO_CONTROLS = frozenset({'add_portfolio_ticker', 'remove_portfolio_ticker',
                              'add_portfolio_except_ticker', 'remove_portfolio_except_ticker'})
CONTROLS = frozenset({'pause', 'stop', 'schedule_on', 'schedule_off', 'new', 'resume'}) | PORTFOLIO_CONTROLS
PROTECTION = frozenset({'risk_monitor', 'reconcile', 'time_limit_exit'})
REVIEWS = frozenset({'full_review', 'event_review'})


def log_event(event, **fields):
    # Only caller-selected metadata; never exception text, requests or credentials.
    print(canonical({'event': event, **fields}), file=sys.stderr, flush=True)


class Service:
    def __init__(self, app, *, telegram=None, clock=utcnow):
        self.app, self.store, self.config, self.clock = app, app.store, app.config, clock
        self.telegram = telegram
        self.stop = threading.Event()
        self.worker_failed = False
        self.threads = []
        self.active_chats = {}
        self.chat_lock = threading.RLock()
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
        if len(raw_body) > MAX_REQUEST_BYTES:
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
                'chat_id': request.chat_id, 'user_id': request.user_id, 'text': request.text,
                'attachments': body.get('attachments', [])}
            queue_id, fresh = self.store.accept_request('service:telegram:' + request.request_id, payload,
                deadline=(self.clock() + timedelta(seconds=120)).isoformat())
            with self.store.transaction():
                if fresh:
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
        with self.store.lock:
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
            # A worker must not claim the request before its deadline is durable.
            request_id, fresh = self.store.accept_request('service:schedule:' + intent.key, payload,
                deadline=intent.expires_at.isoformat())
            if fresh:
                queued += 1
        return queued

    @staticmethod
    def _review_job(payload):
        return payload['kind'] in REVIEWS | {'collect_disclosures', 'finalize_and_report'} or payload.get('command') in {'review', 'chat'}

    def run_once(self, *, review=False, chat=None):
        row = None
        # Idle workers are readers. Take the writer lock only to claim actual work.
        candidates = self.store.read("SELECT * FROM requests WHERE request_key LIKE 'service:%' AND status='ACCEPTED' ORDER BY rowid")
        candidates = [candidate for candidate in candidates if
                      self._review_job(json.loads(candidate['payload'])) == review and
                      (chat is None or (json.loads(candidate['payload']).get('command') == 'chat') == chat)]
        if not candidates:
            return False
        with self.store.transaction():
            for candidate in candidates:
                payload = json.loads(candidate['payload'])
                if self._review_job(payload) == review and (chat is None or (payload.get('command') == 'chat') == chat):
                    claimed = self.store.db.execute("UPDATE requests SET status='RUNNING' WHERE request_id=? AND status='ACCEPTED'", (candidate['request_id'],))
                    if claimed.rowcount:
                        row = dict(candidate)
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
                with self.progress(payload, row['request_id']) as update:
                    result = self._dispatch(payload, row['request_id'], on_progress=update)
        except Exception as error:
            result = {'status': getattr(error, 'state', 'FAILED'), 'error_type': type(error).__name__}
            if isinstance(error, HumanRequired):
                result['reason'] = str(error)
        document = result.pop('_document', None)
        markup = result.pop('_reply_markup', None)
        with self.store.transaction():
            self.store.db.execute('UPDATE requests SET status=?,result=? WHERE request_id=?',
                ('COMPLETE', canonical(result), row['request_id']))
            self.store.event(row['request_id'], 'SERVICE_RESULT', result)
            if payload.get('source') == 'telegram':
                self.store.db.execute("UPDATE telegram_requests SET status='COMPLETE' WHERE request_id=?", (payload['telegram_request_id'],))
                notification = {'route': payload['route'], 'chat_id': payload['chat_id'],
                                'text': render_notification(result, symbols=self._symbols())}
                if markup:
                    notification['reply_markup'] = markup
                self.store.db.execute('INSERT OR IGNORE INTO outbox(event_key,payload) VALUES (?,?)',
                    ('service:' + row['request_id'], canonical(notification)))
            if document:
                self.store.queue_document('report:' + row['request_id'], **document,
                    route=payload.get('route'), chat_id=payload.get('chat_id'))
        return True

    @staticmethod
    def _chat_key(payload):
        return canonical([payload['route'], payload['chat_id'], payload['user_id']])

    def _cancel_chat(self, payload):
        key, count = self._chat_key(payload), 0
        with self.chat_lock:
            active = self.active_chats.get(key)
            if active:
                active.set()
            with self.store.transaction():
                for row in self.store.db.execute("SELECT request_id,payload,status FROM requests WHERE status IN ('ACCEPTED','RUNNING')").fetchall():
                    queued = json.loads(row['payload'])
                    if queued.get('command') == 'chat' and self._chat_key(queued) == key:
                        self.store.set('chat_cancelled:' + row['request_id'], True)
                        if row['status'] == 'ACCEPTED':
                            result = {'status': 'CHAT_CANCELLED', 'model_called': False, 'orders_created': False}
                            self.store.db.execute("UPDATE requests SET status='COMPLETE',result=? WHERE request_id=?", (canonical(result), row['request_id']))
                            self.store.db.execute("UPDATE telegram_requests SET status='COMPLETE' WHERE request_id=?", (queued['telegram_request_id'],))
                        count += 1
        return count

    @contextmanager
    def progress(self, payload, request_id):
        if not self.telegram or payload.get('source') != 'telegram' or payload.get('command') not in {'chat', 'review'}:
            yield lambda _text: None
            return
        ended, changed = threading.Event(), threading.Event()
        latest = ['계좌·주문 상태와 보호 조건을 확인하고 있습니다.' if payload['command'] == 'review' else
                  '제공된 대화와 운영 기록을 확인하고 있습니다.']
        changed.set()
        draft_id = int(digest(request_id)[:8], 16) % 2147483647 + 1
        def update(text):
            latest[0] = text[:3000]
            changed.set()
        def display():
            while not ended.is_set():
                try:
                    self.config.require_external('telegram_send', self.app.approval)
                    self.telegram.send_typing(payload['route'], payload['chat_id'])
                    if changed.is_set() and not ended.is_set():
                        changed.clear()
                        self.telegram.send_draft(payload['route'], payload['chat_id'], draft_id, latest[0])
                except Exception as error:
                    log_event('PROGRESS_UNAVAILABLE', error_type=type(error).__name__)
                if ended.wait(4):
                    break
        thread = threading.Thread(target=display, name='telegram-progress', daemon=True)
        thread.start()
        try:
            yield update
        finally:
            ended.set()
            thread.join(timeout=31)  # Two bounded gateway calls; finish before final text can be sent.

    def _symbols(self):
        return getattr(self.app.bundle, 'data', {}).get('instrument_names', {})

    @staticmethod
    def _keyboard(rows):
        return {'inline_keyboard': [[{'text': text, 'callback_data': command} for text, command in row] for row in rows]}

    def _dispatch(self, payload, request_id, *, on_progress=None):
        self.config.assert_current()
        kind = payload['kind']
        command = payload.get('command')
        if kind == 'telegram':
            if command not in READ_COMMANDS and command != 'chat':
                self._authorize_control(command, payload['user_id'], payload['chat_id'])
            if command in PORTFOLIO_CONTROLS:
                arguments = payload['text'].split()[1:]
                if not arguments:
                    return {'reply_text': '종목 코드를 함께 입력해 주세요.\n/' + command + ' 005930\n보유 수량을 바꾸거나 자동 매도하지 않습니다.'}
                if len(arguments) != 1:
                    raise ValueError('Candidate-list commands require exactly one ticker')
                return self.app.update_candidate_list(command, arguments[0])
            if command == 'resume':
                if len(payload['text'].split()) != 1:
                    raise ValueError('Resume accepts no arguments; trusted approval is checked separately')
                return self.app.resume()
            if command == 'stop':
                count = self._cancel_chat(payload)
                return {'status': 'CANCEL_REQUESTED' if count else 'NO_ACTIVE_CHAT', 'protection': 'CONTINUES'}
            if command == 'pause':
                return self.app.pause()
            if command in {'schedule_on', 'schedule_off'}:
                if command == 'schedule_on' and (self.app.bundle.synthetic or not self.scheduler['enabled']):
                    raise HumanRequired('Enable an approved fresh-data scheduler configuration first')
                with self.store.transaction():
                    self.store.set('discretionary_schedule', command == 'schedule_on')
                return {'status': command.upper(), 'protection': 'CONTINUES'}
            if command in {'chat', 'session', 'new'}:
                scope = self._chat_key(payload)
                key = 'chat_session:' + scope
                if command == 'new':
                    self._cancel_chat(payload)
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
                if not text or len(text) > 16384:
                    raise ValueError('Chat text must contain 1 to 16384 characters')
                messages = history + [{'role': 'user', 'content': text}]
                cancel = threading.Event()
                with self.chat_lock:
                    self.active_chats[scope] = cancel
                    if self.store.get('chat_cancelled:' + request_id, False):
                        cancel.set()
                try:
                    data = self._report_data()
                    context = {key: data[key] for key in ('created_at', 'status', 'theses', 'instruments')}
                    context.update(orders=data['orders'][-20:], fills=data['fills'][-20:], runs=data['runs'][-5:])
                    context['diagnostics'] = data['diagnostics'][-20:]
                    context['operator_contract'] = {
                        'authentication': 'Codex 로그인 상태이며 KIS 증권사 인증 상태가 아니다.',
                        'status': '/status는 저장된 현재 상태를 표시하며 계좌 재조회나 장애 복구를 실행하지 않는다.',
                        'resume': '/resume는 명시적으로 일시정지한 투자를 재개한다. paused=false이면 재개 조치를 안내하지 않는다.',
                        'monitor': '보호 감시는 오류 후에도 자동 재시도한다. HumanRequired라는 예외 이름만으로 사람을 기다리는 상태라고 판단하지 않는다.',
                        'evidence': '각 사건의 시각을 구분한다. 과거 투자 검토 실패를 최근 감시 장애의 원인으로 연결하지 않는다.',
                    }
                    included = {row.get('instrument_id') for row in context['status'].get('holdings', []) + context['orders']}
                    context['instruments'] = {key: name for key, name in context['instruments'].items() if key in included}
                    context['coverage'] = '당일 최근 주문 20건·체결 20건·검토 5건, 현재 저장된 계좌 상태. 실시간 추가 조회 없음.'
                    result = callback(request_id=request_id, session_id=session_id, messages=messages,
                        account_context=context, attachments=payload.get('attachments', []),
                        on_progress=on_progress, cancel=cancel)
                finally:
                    with self.chat_lock:
                        self.active_chats.pop(scope, None)
                with self.store.transaction():
                    if self.store.get(key) != session_id:
                        return {'status': 'SESSION_CHANGED', 'model_called': result['model_called'], 'orders_created': False}
                    if cancel.is_set():
                        return {'status': 'CHAT_CANCELLED', 'model_called': result['model_called'], 'orders_created': False}
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
                runtime = getattr(self.app.refresh, '__self__', None)
                try:
                    quota = runtime.codex.read_rate_limits() if runtime and hasattr(runtime, 'codex') else {'status': 'UNAVAILABLE'}
                except (AdapterError, OSError):
                    quota = {'status': 'UNAVAILABLE'}
                return {'status': 'USAGE', 'reason': quota['status'], 'rate_limits': quota.get('rate_limits'),
                        'rate_limits_by_id': quota.get('rate_limits_by_id'), 'checked_at': quota.get('checked_at'), 'attempts': attempts,
                        'operating_cost': 'UNCONFIRMED', 'subscription_quota_is_not_api_billing': True}
            if command == 'show_touch_point':
                return {'theses': [thesis.model_dump(mode='json') for thesis in self.app.theses()],
                        'as_of': self.app.bundle.now.isoformat()}
            if command == 'status':
                arguments = payload['text'].split()[1:]
                if arguments == ['candidates']:
                    controls = self.store.get('candidate_controls', {'removed': [], 'excluded': []})
                    return {'reply_text': render_notification({'status': 'CANDIDATE_CONTROLS', 'controls': controls}, symbols=self._symbols()),
                        '_reply_markup': self._keyboard([[('후보 복원', '/add_portfolio_ticker'), ('후보 제거', '/remove_portfolio_ticker')],
                            [('매수 제외', '/add_portfolio_except_ticker'), ('제외 해제', '/remove_portfolio_except_ticker')]])}
                if arguments == ['effort']:
                    return {'reasoning_effort': self.config.app['model']['reasoning_effort'], 'changed': False,
                        '_reply_markup': self._keyboard([[('현재 수준 유지', '/status'), ('high 변경 요청', '/reasoning_effort high')],
                            [('medium 변경 요청', '/reasoning_effort medium'), ('xhigh 변경 요청', '/reasoning_effort xhigh')]])}
                state = self.app.status()
                scheduled = self.store.get('discretionary_schedule', self.scheduler['enabled'])
                return {**state, 'version': os.environ.get('APP_VERSION', 'dev'),
                    'scheduler_status': 'SCHEDULE_ON' if scheduled else 'SCHEDULE_OFF',
                    'reasoning_effort': self.config.app['model']['reasoning_effort'],
                    'session_active': bool(self.store.get('chat_session:' + self._chat_key(payload))),
                    '_reply_markup': self._keyboard([[('전체 투자 검토', '/review'), ('투자 재개' if state.get('paused') else '투자 일시정지', '/resume' if state.get('paused') else '/pause')],
                        [('예약 검토 끄기' if scheduled else '예약 검토 켜기', '/schedule_off' if scheduled else '/schedule_on'), ('후보·제외 종목', '/status candidates')],
                        [('추론 수준', '/status effort'), ('리포트', '/report'), ('사용량', '/usage')]])}
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
            return self.app.review(kind=kind, event_id=payload.get('event_id'), request_key='workflow:' + request_id,
                                   on_progress=on_progress if payload.get('source') == 'telegram' else None)
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

    def _report_data(self):
        now = self.clock()
        day = now.astimezone(ZoneInfo('Asia/Seoul')).date()
        start = datetime.combine(day, time(), ZoneInfo('Asia/Seoul')).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        with self.store.lock:
            journal = [dict(row) for row in self.store.read(
                'SELECT created_at,run_id,kind,payload FROM journal WHERE created_at>=? AND created_at<? ORDER BY sequence',
                (start.isoformat(), end.isoformat()))]
            for row in journal:
                row['payload'] = json.loads(row['payload'])
                if row['kind'] == 'INTENT_RESERVED':
                    intent = row['payload']
                    intent['intent_id'] = digest([intent['plan_id'], intent['plan_revision'], intent['side']])
            touched = {row['payload']['intent_id'] for row in journal if row['payload'].get('intent_id')}
            touched.update(row['payload']['id'] for row in journal if row['kind'] == 'INTENT_RESERVED' and row['payload'].get('id'))
            orders, by_id = [], {}
            symbols = self._symbols()
            for row in self.store.read('SELECT * FROM intents ORDER BY rowid'):
                raw = dict(row)
                intent = json.loads(raw['payload'])
                record = {key: raw[key] for key in ('instrument_id', 'side', 'quantity', 'state', 'cumulative_quantity', 'cumulative_notional')}
                record.update(reason=intent.get('reason'), run_id=intent.get('run_id'), name=symbols.get(raw['instrument_id']))
                by_id[raw['id']] = record
                if raw['id'] in touched:
                    record['created_at'] = next((item['created_at'] for item in journal
                        if item['payload'].get('intent_id', item['payload'].get('id')) == raw['id']), None)
                    orders.append(record)
            fills = []
            for row in journal:
                if row['kind'] in {'CUMULATIVE_FILL', 'FILL_CORRECTION'}:
                    fill = row['payload']
                    order = by_id.get(fill.get('intent_id'), {})
                    fills.append({**{key: order.get(key) for key in ('instrument_id', 'name', 'side', 'reason')},
                        'at': fill.get('observed_at', row['created_at']), 'quantity': fill.get('quantity_delta'),
                        'amount_krw': fill.get('notional_delta_krw'), 'fee_krw': reported_fee(fill),
                        'correction': row['kind'] == 'FILL_CORRECTION'})
            state = self.app.status()
            for holding in state.get('holdings', []):
                holding['name'] = symbols.get(holding['instrument_id'])
                quote = getattr(self.app.bundle, 'quotes', {}).get(holding['instrument_id'])
                if quote:
                    from .strategy import quote_fresh
                    if quote_fresh(quote, now, self.app.profile['orders']['quote_max_age_seconds']):
                        holding.update(price=str(quote.bid), value=str(quote.bid * holding['quantity']),
                            price_observed_at=quote.observed_at.isoformat(), valuation_quality='EXACT')
                    else:
                        holding['valuation_quality'] = 'STALE'
            return {'schema_version': 1, 'created_at': now.isoformat(), 'date': day.isoformat(),
                    'config_hash': self.config.config_hash, 'strategy_hash': self.config.strategy_hash,
                    'code_id': self.app.code_id, 'status': state,
                    'runs': [row['payload'] for row in journal if row['kind'] == 'RUN_OUTCOME'],
                    'orders': orders, 'fills': fills, 'instruments': symbols,
                    'nav': self.store.get('nav_points', []),
                    'theses': [thesis.model_dump(mode='json') for thesis in self.app.theses()],
                    'diagnostics': [{'at': row['created_at'], 'kind': row['kind'], **row['payload']} for row in journal
                        if row['kind'] in {'ACCOUNT_INCOMPLETE', 'ACCOUNT_RECOVERED', 'MONITOR_DEGRADED', 'MONITOR_RECOVERED', 'SERVICE_WORKER_FAILED', 'MODEL_OUTCOME'}]}

    def _report(self):
        data = self._report_data()
        directory = self.config.state_dir / 'reports' / data['date']
        paths = write_report(data, directory / 'daily.json', directory / 'daily.html', '일일 판단·성과')
        document = {'filename': f"daily-{data['date']}.html", 'content': Path(paths['html']).read_text(encoding='utf-8')}
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
                if payload.get('intent_id'):
                    order = self.store.order(payload['intent_id'])
                    intent = json.loads(order['payload'])
                    payload = {**{key: order[key] for key in ('instrument_id', 'side', 'quantity')},
                               'reason': intent.get('reason'), **payload}
                text = payload.get('text')
                if text:
                    try:
                        decoded = json.loads(text)
                        if isinstance(decoded, dict):
                            text = render_notification(decoded, symbols=self._symbols())
                    except ValueError:
                        pass
                else:
                    text = render_notification(payload, symbols=self._symbols())
                if len(text) > 3500:
                    from .reporting import _document
                    with self.store.transaction():
                        self.store.queue_document('long:' + str(row['id']), 'response.html',
                            _document('전체 응답', '<pre>' + html.escape(text) + '</pre>'), route=route, chat_id=chat)
                    text = text[:1000] + '\n\n전체 내용은 첨부 HTML로 이어서 보내드립니다.'
                self.telegram.send_message(route, chat, text, reply_markup=payload.get('reply_markup'))
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
        def worker(review, chat):
            while not self.stop.wait(.1):
                try:
                    if not review:
                        self.queue_tick()
                    self.run_once(review=review, chat=chat)
                except Exception as error:
                    self.worker_failed = True
                    frames = [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                              for frame in traceback.extract_tb(error.__traceback__)[-5:]]
                    detail = {'error_type': type(error).__name__, 'frames': frames}
                    if isinstance(error,sqlite3.Error):
                        detail['sqlite_error'] = getattr(error,'sqlite_errorname','UNKNOWN')
                    log_event('SERVICE_WORKER_FAILED', **detail)
                    try:
                        with self.store.transaction():
                            self.store.event('service', 'SERVICE_WORKER_FAILED', detail, notify=True)
                    except sqlite3.Error as journal_error:
                        log_event('SERVICE_FAILURE_RECORD_FAILED', sqlite_error=getattr(journal_error,'sqlite_errorname','UNKNOWN'))
                    finally:
                        self.stop.set()
        def notify():
            while not self.stop.wait(1):
                try:
                    self.outbox_once()
                except Exception as error:
                    with self.store.transaction():
                        self.store.event('service', 'NOTIFY_BLOCKED', {'error_type': type(error).__name__})
        for name, target, args in [('control', worker, (False, False)), ('review', worker, (True, False)),
                                   ('chat', worker, (True, True)), ('outbox', notify, ())]:
            thread = threading.Thread(target=target, args=args, name='danta-' + name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop.set()
        with self.chat_lock:
            for cancel in self.active_chats.values():
                cancel.set()
        for thread in self.threads:
            thread.join(timeout=20)
        if any(thread.is_alive() for thread in self.threads):
            raise HumanRequired('Service worker still running; retain writer lock until it exits')


class RuntimeHost:
    """Keep deployment diagnostics available while the trading runtime is blocked."""

    def __init__(self, config, stop_event=None):
        from .application import code_identity
        self.config, self.code_id = config, code_identity()
        self.service = None
        self.stop = stop_event or threading.Event()
        self.status = 'STARTING'
        self.issues = []

    def health(self):
        status = self.status
        if self.stop.is_set():
            status = 'STOPPING'
        elif self.service and (self.service.stop.is_set() or self.service.worker_failed):
            status = 'FAILED'
        elif self.service:
            try:
                self.service.config.assert_current()
                self.service.config.require_external('telegram_ingress', self.service.app.approval)
            except (HumanRequired, ValueError, OSError):
                status = 'CONFIGURATION_CHANGED_OR_APPROVAL_EXPIRED'
        components = {}
        if self.service:
            health = self.service.app.status()
            components = {key: health.get(key) for key in ('authentication', 'model_status', 'account_status', 'monitor_status', 'review_status')}
        return {'status': status, 'ready': status == 'READY', 'mode': self.config.mode,
                'issues': self.issues, 'components': components}

    def version(self):
        return {'version': os.environ.get('APP_VERSION', 'dev'), 'code_id': self.code_id,
                'config_hash': self.config.config_hash, 'strategy_hash': self.config.strategy_hash,
                'runtime_status': self.health()['status']}

    def requirements(self, args):
        app, tg = self.config.app, self.config.app['telegram']
        if app['broker']['capability_manifest'] == 'automatic':
            # Private IDs, login, account census and public source checks are
            # resolved by startup; they do not require a pre-generated manifest.
            return []
        issues = []
        if self.config.mode == 'offline':
            issues.append('app.yaml: app.mode=offline (계좌·모델·매매 실행 꺼짐)')
        if not tg['enabled'] or not tg['ingress_enabled']:
            issues.append('app.yaml: telegram.enabled / ingress_enabled 설정 필요')
        if not tg['allowed_sender_ids'] or not tg['allowed_chat_ids']:
            issues.append('app.yaml: Telegram 허용 sender/chat 설정 필요')
        approval = getattr(args, 'approval_file', None) or self.config.directory / 'runtime.json'
        if not Path(approval).is_file():
            issues.append('config/runtime.json: 운영 승인 파일 없음')
        manifest = app['broker']['capability_manifest']
        if not manifest or not (self.config.directory / manifest).is_file():
            issues.append('config/runtime-manifest.json: 검증된 운영 정보 없음')
        return issues


def handler_for(host):
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
            if self.path == '/version':
                self.respond(200, host.version())
            elif self.path in {'/healthz', '/readyz'}:
                health = host.health()
                self.respond(200 if self.path == '/healthz' or health['ready'] else 503, health)
            else:
                self.respond(404, {'status': 'NOT_FOUND'})

        def do_POST(self):
            if self.path != '/telegram':
                self.respond(404, {'status': 'NOT_FOUND'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if self.headers.get('Transfer-Encoding') or not 0 < length <= MAX_REQUEST_BYTES:
                    raise AdapterError('INVALID_REQUEST_SIZE')
                if self.headers.get_content_type() != 'application/json':
                    raise AdapterError('JSON_CONTENT_TYPE_REQUIRED')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise AdapterError('TRUNCATED_REQUEST')
                health = host.health()
                if not health['ready']:
                    self.respond(503, {'accepted': False, 'status': health['status'],
                        'reply_text': '트레이딩 엔진은 실행 중이지만 거래 서비스가 준비되지 않았습니다.\n'
                            + '\n'.join(health['issues'] or [health['status']])})
                    return
                self.respond(202, host.service.receive_http(raw))
            except (AdapterError, HumanRequired, ValueError, OSError) as error:
                log_event('INGRESS_REJECTED', error_type=type(error).__name__)
                code = getattr(error, 'code', '')
                explanations = {'UNKNOWN_COMMAND': '지원하지 않는 명령어입니다. /status에서 운영 메뉴를 확인해 주세요.',
                    'INVALID_ATTACHMENTS': '첨부 파일 형식을 확인할 수 없습니다. UTF-8 텍스트 파일을 보내 주세요.',
                    'ATTACHMENTS_TOO_LARGE': '첨부 텍스트는 합계 32KiB까지 전달할 수 있습니다.',
                    'ATTACHMENTS_REQUIRE_CHAT': '첨부 파일은 명령어 대신 일반 메시지와 함께 보내 주세요.',
                    'TELEGRAM_PAYLOAD_TOO_LARGE': '메시지와 첨부가 너무 큽니다. 나누어 보내 주세요.'}
                self.respond(400 if code in explanations else 403, {'accepted': False, 'error_type': type(error).__name__,
                    'reply_text': explanations.get(code, '요청 권한 또는 운영 설정을 확인할 수 없습니다. /status에서 상태를 확인해 주세요.')})
    return Handler


def serve(config, args=None, *, application_factory=None, stop_event=None):
    """Start HTTP first; fixture research is CLI-only, never a deployed worker."""
    if application_factory is None:
        from .cli import make_application
        application_factory = make_application
    host = RuntimeHost(config, stop_event)
    address = config.app['app']['listen_host']
    ipaddress.ip_address(address)
    server = ThreadingHTTPServer((address, config.app['app']['listen_port']), handler_for(host))
    server.daemon_threads = True
    http_thread = threading.Thread(target=server.serve_forever, name='danta-ingress', daemon=True)
    app, service = None, None
    previous_signals = {}
    try:
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGTERM, signal.SIGINT):
                previous_signals[number] = signal.signal(number, lambda *_: host.stop.set())
        http_thread.start()
        log_event('HTTP_LISTENING', host=address, port=server.server_port, mode=config.mode,
                  version=host.version()['version'])
        while not host.stop.is_set():
            host.issues = host.requirements(args)
            if host.issues:
                host.status = 'WAITING_FOR_CONFIGURATION'
            else:
                try:
                    app = application_factory(config, args)
                    if not host.stop.is_set() and config.mode in {'live', 'broker_demo'}:
                        app.activate(app.config.config_hash)
                    if not host.stop.is_set():
                        service = Service(app)
                        if not host.stop.is_set():
                            service.start()
                            host.service, host.status = service, 'READY'
                except Exception as error:
                    if service:
                        service.close()
                        service = None
                    if app:
                        app.close()
                        app = None
                    host.status = 'INITIALIZATION_FAILED'
                    # Only short engine-defined codes may cross the diagnostic
                    # boundary; provider bodies and parser exceptions may be private.
                    import re
                    reason = str(error) if isinstance(error, (HumanRequired, AdapterError)) and re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', str(error)) else type(error).__name__
                    host.issues = ['운영 초기화 실패: ' + reason]
                    log_event('RUNTIME_INITIALIZATION_FAILED', error_type=type(error).__name__, reason=reason)
            log_event('RUNTIME_STATE', **host.health())
            if service or config.app['broker']['capability_manifest'] != 'automatic' or host.stop.wait(30):
                break
            # Login and transient provider failures recover without another
            # container recreation. A policy edit is loaded only between runs.
            from .config import load_config
            config = load_config(config.directory)
            host.config = config
            host.status, host.issues = 'STARTING', []
        while not host.stop.wait(.5):
            if service and service.stop.is_set():
                break
    finally:
        host.stop.set()
        log_event('SERVICE_STOPPING')
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=5)
        if service:
            service.close()
        if app:
            app.close()
        for number, previous in previous_signals.items():
            signal.signal(number, previous)
    if service and service.worker_failed:
        raise OSError('SERVICE_WORKER_FAILED')
