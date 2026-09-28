"""Telegram recovery regressions using synthetic records, no network or orders."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from danta.adapters import AdapterError
from danta.adapters.codex_cli import CodexAdapter, auth_preflight, classify_failure, public_progress
from danta.adapters.market_tools import validate_attachments
from danta.adapters.telegram import MAX_REQUEST_BYTES
from danta.config import canonical
from danta.runtime import RuntimeState
from tests.integration import test_service as fixtures


class OperatorExperienceTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ServiceIntegrationTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def test_stop_cancels_active_and_queued_chat_without_pausing_trading(self):
        case = self.case
        case.app.approval['capabilities'].append('model_call')
        entered, canceled = threading.Event(), threading.Event()
        captured = []
        def reply(**kwargs):
            captured.append(kwargs)
            entered.set()
            if not kwargs['cancel'].wait(3):
                raise AssertionError('stop did not cancel the running model')
            canceled.set()
            return {'status': 'MODEL_CANCELED', 'model_called': True, 'orders_created': False}
        case.app.chat = reply
        ack = case.receive('현재 계좌 설명해줘', update=1)
        self.assertNotIn('reply_text', ack)
        worker = threading.Thread(target=case.service.run_once, kwargs={'review': True})
        worker.start()
        self.assertTrue(entered.wait(2))
        case.receive('이 요청은 취소돼야 해', update=2)
        case.receive('/stop', update=3)
        case.service.run_once()
        worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertTrue(canceled.is_set())
        self.assertFalse(case.app.store.get('paused'))
        self.assertFalse(case.service.run_once(review=True))
        self.assertEqual(case.app.review_calls, 0)
        self.assertIn('status', captured[0]['account_context'])
        self.assertIn('실시간 추가 조회 없음', captured[0]['account_context']['coverage'])
        self.assertFalse(any(thread.name == 'telegram-progress' for thread in threading.enumerate()))

    def test_daily_report_uses_korean_day_and_protection_fills_not_only_reviews(self):
        case = self.case
        case.service.clock = lambda: datetime(2026, 9, 20, 16, tzinfo=timezone.utc)
        payload = {'instrument_id': 'KRX:005930', 'name': '합성종목', 'side': 'SELL',
                   'reason': 'EXIT_PROTECTION', 'quantity_delta': 2,
                   'notional_delta_krw': '140000', 'fee_delta_krw': '20'}
        for at, kind, data in (
            ('2026-09-20T14:59:59+00:00', 'RUN_OUTCOME', {'reason': 'previous-korean-day'}),
            ('2026-09-20T15:01:00+00:00', 'RUN_OUTCOME', {'reason': 'current-korean-day'}),
            ('2026-09-20T15:02:00+00:00', 'CUMULATIVE_FILL', payload),
            ('2026-09-20T15:03:00+00:00', 'FILL_CORRECTION', {**payload, 'quantity_delta': -1}),
            ('2026-09-20T15:04:00+00:00', 'CUMULATIVE_FILL', {**payload, 'fee_delta_krw': '0'}),
            ('2026-09-20T15:05:00+00:00', 'CUMULATIVE_FILL', {**payload, 'fee_delta_krw': '0', 'fees_confirmed': True}),
        ):
            with case.app.store.transaction():
                case.app.store.db.execute('INSERT INTO journal(created_at,run_id,kind,payload) VALUES (?,?,?,?)',
                    (at, 'protection', kind, canonical(data)))
        report = case.service._report_data()
        self.assertEqual(report['date'], '2026-09-21')
        self.assertEqual([row['reason'] for row in report['runs']], ['current-korean-day'])
        self.assertEqual([row['quantity'] for row in report['fills']], [2, -1, 2, 2])
        self.assertEqual([row['correction'] for row in report['fills']], [False, True, False, False])
        self.assertEqual([row['fee_krw'] for row in report['fills']], ['20', '20', None, '0'])

    def test_typing_and_public_draft_stop_before_final_outbox_delivery(self):
        case = self.case
        payload = {'source': 'telegram', 'command': 'chat', 'route': 'trading-engine', 'chat_id': 'chat-1'}
        typed, drafted = threading.Event(), threading.Event()
        def draft(_route, _chat, draft_id, _text):
            self.assertGreater(draft_id, 0)
            self.assertLess(draft_id, 2 ** 31)
            drafted.set()
        with patch.object(case.adapter, 'send_typing', side_effect=lambda *args: typed.set()), \
             patch.object(case.adapter, 'send_draft', side_effect=draft):
            with case.service.progress(payload, 'synthetic-progress') as update:
                update('확인 가능한 자료를 조회하고 있습니다.')
                self.assertTrue(typed.wait(1))
                # Draft cadence is bounded, independently of model output rate.
                self.assertTrue(drafted.wait(5))
        self.assertFalse(any(thread.name == 'telegram-progress' for thread in threading.enumerate()))

    def test_review_receives_progress_callback(self):
        case = self.case
        captured = []
        case.app.review = lambda **kwargs: (captured.append(kwargs) or {'status': 'FAKE_REVIEW_COMPLETE'})
        case.receive('/review')
        case.service.run_once(review=True)
        self.assertTrue(callable(captured[0]['on_progress']))

    def test_repeated_account_warning_keeps_ledger_but_sends_one_notice(self):
        store = self.case.app.store
        with store.transaction():
            for _ in range(20):
                store.event('reconcile', 'ACCOUNT_INCOMPLETE', {'diagnostics': ['TRANSPORT_FAILED']}, notify=True)
        self.assertEqual(store.read("SELECT COUNT(*) FROM journal WHERE kind='ACCOUNT_INCOMPLETE'")[0][0], 20)
        self.assertEqual(store.read('SELECT COUNT(*) FROM outbox')[0][0], 1)

    def test_error_timestamps_do_not_repeat_notices_but_changed_causes_are_retained(self):
        store = self.case.app.store
        with store.transaction():
            for index in range(3):
                store.event('reconcile', 'ACCOUNT_INCOMPLETE', {'diagnostics': [{
                    'endpoint':'inquire-psbl-order', 'reason':'TRANSIENT_FAILURE', 'http_status':500,
                    'provider_code':'EGW00215', 'requested_at':str(index), 'elapsed_seconds':index}]}, notify=True)
            store.event('reconcile', 'ACCOUNT_INCOMPLETE', {'diagnostics': [{
                'endpoint':'inquire-psbl-order', 'reason':'AUTH_FAILED', 'http_status':401}]}, notify=True)
        rows = store.read("SELECT payload FROM journal WHERE kind='ACCOUNT_INCOMPLETE'")
        self.assertEqual(len(rows), 4)
        self.assertEqual([json.loads(row[0])['diagnostics'][0]['requested_at'] for row in rows[:3]], ['0','1','2'])
        self.assertEqual(store.read('SELECT COUNT(*) FROM outbox')[0][0], 2)

    def test_suppressed_failure_does_not_send_an_unpaired_recovery(self):
        store = self.case.app.store
        with store.transaction():
            for _ in range(2):
                store.event('monitor', 'MONITOR_DEGRADED', {'diagnostics': ['TRANSPORT_FAILED']}, notify=True)
                store.event('monitor', 'MONITOR_RECOVERED', {'checked_at': '2026-09-21T10:00:00+00:00'}, notify=True)
        self.assertEqual(store.read("SELECT COUNT(*) FROM journal WHERE kind IN ('MONITOR_DEGRADED','MONITOR_RECOVERED')")[0][0], 4)
        notices = [json.loads(row[0])['kind'] for row in store.read('SELECT payload FROM outbox')]
        self.assertEqual(notices, ['MONITOR_DEGRADED', 'MONITOR_RECOVERED'])

    def test_chat_receives_dated_incidents_and_real_operator_contract(self):
        case = self.case
        case.app.approval['capabilities'].append('model_call')
        captured = []
        case.app.chat = lambda **kwargs: (captured.append(kwargs) or
            {'status': 'CHAT_COMPLETE', 'reply_text': '합성 응답', 'model_called': True, 'orders_created': False})
        with case.app.store.transaction():
            case.app.store.event('monitor', 'MONITOR_DEGRADED', {'diagnostics': [{'endpoint': 'orders', 'reason': 'TRANSIENT_FAILURE', 'http_status': 503}]})
        case.receive('감시 오류의 원인이 뭐야?')
        case.service.run_once(review=True)
        context = captured[0]['account_context']
        self.assertEqual(context['diagnostics'][-1]['diagnostics'][0]['http_status'], 503)
        self.assertIn('at', context['diagnostics'][-1])
        self.assertIn('재조회나 장애 복구를 실행하지 않는다', context['operator_contract']['status'])
        self.assertIn('KIS', context['operator_contract']['authentication'])
        self.assertEqual(case.app.reconcile_calls, 0)

    def test_full_long_reply_is_immutable_attachment_and_status_has_controls(self):
        case = self.case
        case.receive('/status')
        case.service.run_once()
        case.service.outbox_once()
        self.assertIn('inline_keyboard', case.sent[-1]['reply_markup'])
        text = '전체 내용 보존 ' * 600
        with case.app.store.transaction():
            case.app.store.db.execute('INSERT INTO outbox(event_key,payload) VALUES (?,?)',
                ('long-reply-test', canonical({'text': text, 'route': 'trading-engine', 'chat_id': 'chat-1'})))
        case.service.outbox_once()
        document = json.loads(case.app.store.read("SELECT payload FROM outbox WHERE event_key LIKE 'long:%'")[0][0])
        self.assertIn(text, document['document']['content'])
        self.assertIn('첨부 HTML', case.sent[-1]['text'])

    def test_text_attachments_are_validated_before_durable_acceptance(self):
        case = self.case
        body = {'source': 'telegram', 'route': 'trading-engine', 'update_id': 20,
                'chat_id': 'chat-1', 'user_id': 'user-1', 'text': '파일 설명해줘',
                'attachments': [{'file_name': 'notes.txt', 'content': '실제 첨부 내용'}]}
        case.service.receive_http(canonical(body).encode())
        stored = json.loads(case.app.store.read('SELECT payload FROM requests')[0][0])
        self.assertEqual(stored['attachments'], body['attachments'])
        case.service.run_once(review=True, chat=True)
        # Legal Telegram emoji text plus escaped attachment content and raw metadata.
        large = {**body, 'update_id': 21, 'text': '😀' * 4096, 'raw_message': {'text': '😀' * 4096},
                 'attachments': [{'file_name': 'lines.txt', 'content': '\n' * 32768}]}
        encoded = canonical(large).encode()
        self.assertGreater(len(encoded), 65536)
        self.assertTrue(case.service.receive_http(encoded)['accepted'])
        with self.assertRaisesRegex(AdapterError, 'TELEGRAM_PAYLOAD_TOO_LARGE'):
            case.service.receive_http(b' ' * (MAX_REQUEST_BYTES + 1))
        for attachments in ([{'file_name': '../private', 'content': 'text'}],
                            [{'file_name': 'huge.txt', 'content': 'x' * 32769}],
                            [{'file_name': 'notes.txt', 'content': 'ok', 'path': '/private'}]):
            with self.assertRaises(AdapterError):
                validate_attachments(attachments)


class RuntimeRecoveryTests(unittest.TestCase):
    def test_auth_mode_mismatch_is_nondestructive_and_classified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'auth.json'
            content = json.dumps({'auth_mode': 'apikey', 'OPENAI_API_KEY': 'synthetic-not-a-key'})
            path.write_text(content)
            self.assertEqual(auth_preflight(tmp, 'chatgpt'), 'AUTH_MODE_MISMATCH')
            self.assertEqual(path.read_text(), content)
            path.write_text(json.dumps({'auth_mode': 'chatgptAuthTokens', 'tokens': {'access_token': 'synthetic'}}))
            self.assertEqual(auth_preflight(tmp, 'chatgpt'), 'AUTHENTICATED')
        self.assertEqual(classify_failure('ChatGPT login is required, but an API key is currently being used. Logging out.'), 'AUTH_MODE_MISMATCH')

    def test_model_process_progress_cancel_and_private_reasoning_filter(self):
        self.assertIsNone(public_progress(json.dumps({'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'private'}})))
        self.assertEqual(public_progress(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'phase': 'commentary', 'text': '공개 안내'}})), '공개 안내')
        self.assertIsNone(public_progress(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'phase': 'final_answer', 'text': 'final'}})))
        cancel, progressed = threading.Event(), threading.Event()
        script = "import json,sys,time; sys.stdin.read(); print(json.dumps({'type':'turn.started'}),flush=True); time.sleep(20)"
        def progress(text):
            progressed.set()
            cancel.set()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(InterruptedError):
            CodexAdapter._run_process([sys.executable, '-c', script], env={'PATH': os.defpath}, cwd=tmp,
                input='fixture', timeout=5, on_progress=progress, cancel=cancel)
        self.assertTrue(progressed.is_set())

    def test_subscription_rpc_uses_existing_auth_and_keeps_quota_circuit_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'auth.json').write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {'access_token': 'synthetic'}}))
            cli = path / 'fake-codex'
            cli.write_text('#!' + sys.executable + '\n' + '''import json,sys
assert '--ignore-user-config' not in sys.argv
assert 'app-server' in sys.argv
for line in sys.stdin:
    request=json.loads(line)
    if request.get('id')==1:
        print(json.dumps({'id':1,'result':{}}),flush=True)
    if request.get('id')==2:
        window={'primary':{'usedPercent':18,'windowDurationMins':300},'secondary':{'usedPercent':75,'windowDurationMins':10080}}
        print(json.dumps({'id':2,'result':{'rateLimitsByLimitId':{'codex':window}}}),flush=True)
''')
            cli.chmod(0o700)
            circuit = {'codex_cli:fixture': {'reset_at': None}}
            adapter = CodexAdapter(executable=str(cli), model_id='fixture', reasoning_effort='medium', mode='paper',
                auth_home=tmp, auth_mode='chatgpt', authorize=lambda *_: None, circuit_state=circuit)
            result = adapter.read_rate_limits()
            self.assertEqual(result['status'], 'CURRENT')
            self.assertEqual(result['rate_limits']['primary']['usedPercent'], 18)
            self.assertIn('codex_cli:fixture', circuit)

    def test_order_revision_save_does_not_rewrite_disclosure_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite'
            state = RuntimeState(path)
            state.data['disclosure_records'] = {'document': 'large original'}
            state.save()
            state.data['disclosure_records']['not-ready-to-save'] = object()
            state.data['observations']['order'] = {'revision': 2}
            state.save(('observations',))
            state.db.close()
            restored = RuntimeState(path)
            self.assertEqual(restored.data['disclosure_records'], {'document': 'large original'})
            self.assertEqual(restored.data['observations']['order']['revision'], 2)
            restored.db.close()


if __name__ == '__main__':
    unittest.main()
