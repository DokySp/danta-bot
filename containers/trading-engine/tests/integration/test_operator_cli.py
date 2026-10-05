"""CLI entrypoints use the same account owner and report evidence as the service."""
from contextlib import redirect_stdout
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import socket
import struct
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from danta.application import Application
from danta.cli import main, parser
from danta.config import HumanRequired, utcnow
from danta.operator import OperatorLease, OperatorServer, _check_peer, connect_or_claim, operator_request
from tests.integration import test_engine as engine_helpers


class OperatorCliTests(unittest.TestCase):
    def setUp(self):
        engine_helpers.EngineCase.setUp(self)
        self.operator_cleanups = []

    def tearDown(self):
        try:
            for close in reversed(self.operator_cleanups):
                close()
        finally:
            engine_helpers.EngineCase.tearDown(self)

    def running_operator(self):
        lease = OperatorLease(self.config)
        app = Application(self.config, self.bundle)
        server = OperatorServer(app, lease=lease).start()
        def close():
            server.close()
            app.close()
            lease.close()
        self.operator_cleanups.append(close)
        return app, server

    def invoke(self, arguments):
        output = io.StringIO()
        with patch('danta.cli.make_application', side_effect=AssertionError('second Application forbidden')), redirect_stdout(output):
            code = main(['--config-dir', str(self.config_dir), *arguments])
        return code, json.loads(output.getvalue())

    def request(self, arguments):
        return operator_request(parser().parse_args(arguments), self.config)

    def test_cli_uses_existing_application_and_ledger_for_commands(self):
        app, server = self.running_operator()
        code, result = self.invoke(['run', '--request-key', 'operator-review'])
        self.assertEqual(code, 0, result)
        self.assertTrue(app.store.holdings())
        code, result = self.invoke(['reconcile'])
        self.assertEqual((code, result['status']), (0, 'RECONCILED'))
        code, result = self.invoke(['candidates', 'exclude', 'AAA'])
        self.assertEqual(code, 0, result)
        self.assertIn('TEST:AAA', app.store.get('candidate_controls')['excluded'])
        code, result = self.invoke(['pause'])
        self.assertEqual((code, result['status']), (0, 'PAUSED'))
        code, result = self.invoke(['status'])
        self.assertEqual(code, 0, result)
        self.assertTrue(result['paused'])
        code, paths = self.invoke(['report'])
        self.assertEqual(code, 0, paths)
        report = json.loads(Path(paths['json']).read_text())
        self.assertTrue(report['fills'])
        self.assertEqual([(row['instrument_id'], row['quantity']) for row in report['status']['holdings']],
                         [(row['instrument_id'], row['quantity']) for row in app.status()['holdings']])
        self.assertEqual(server.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(server.path.parent.stat().st_mode & 0o777, 0o700)
        with self.assertRaisesRegex(HumanRequired, 'SECOND_WRITER_REJECTED'):
            Application(self.config, self.bundle)

    def test_resume_and_activate_use_only_the_servers_trusted_approval(self):
        app, _server = self.running_operator()
        for command in (['resume', '--approval', 'client-only'],
                        ['activate', '--approval', 'client-only', '--expected-config', self.config.config_hash]):
            code, result = self.invoke(command)
            self.assertEqual(code, 2, result)
            self.assertIn('Matching trusted operator approval', result['reason'])
        app.approval = {'id': 'server-approved'}
        # The dispatch must call the existing authority checks; offline mode still rejects them.
        for name, arguments in (('resume', ['resume', '--approval', 'server-approved']),
                                ('activate', ['activate', '--approval', 'server-approved',
                                              '--expected-config', self.config.config_hash])):
            with self.subTest(command=name), patch.object(app, name, wraps=getattr(app, name)) as call:
                code, result = self.invoke(arguments)
                self.assertEqual(code, 2, result)
                call.assert_called_once()
        self.assertFalse(app.store.get('activation'))

    def test_cli_never_constructs_application_during_startup_or_shutdown_gap(self):
        lease = OperatorLease(self.config)
        app = server = None
        try:
            code, result = self.invoke(['status'])
            self.assertEqual(code, 2, result)
            self.assertIn('LOCAL_OPERATOR_UNAVAILABLE', result['reason'])
            app = Application(self.config, self.bundle)
            server = OperatorServer(app, lease=lease).start()
            server.close()
            code, result = self.invoke(['pause'])
            self.assertEqual(code, 2, result)
            self.assertIn('no standalone fallback', result['reason'])
            self.assertFalse(app.store.get('paused'))
        finally:
            if server:
                server.close()
            if app:
                app.close()
            lease.close()

    def test_stale_socket_does_not_trigger_standalone_fallback(self):
        lease = OperatorLease(self.config)
        path = lease.directory / 'control.sock'
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(path))
        path.chmod(0o600)
        lease.close()
        code, result = self.invoke(['pause'])
        self.assertEqual(code, 2, result)
        self.assertIn('LOCAL_OPERATOR_UNAVAILABLE', result['reason'])
        app, server = self.running_operator()
        self.assertEqual(self.invoke(['pause'])[0], 0)
        self.assertTrue(app.store.get('paused'))

    def test_active_socket_is_not_unlinked_by_another_server(self):
        app, server = self.running_operator()
        inode = server.path.stat().st_ino
        other = OperatorServer(app, lease=server.lease)
        with self.assertRaisesRegex(HumanRequired, 'already active'):
            other.start()
        self.assertEqual(server.path.stat().st_ino, inode)
        self.assertEqual(self.invoke(['status'])[0], 0)

    def test_socket_and_peer_identity_are_verified(self):
        _app, server = self.running_operator()
        server.path.chmod(0o666)
        try:
            code, result = self.invoke(['pause'])
            self.assertEqual(code, 2, result)
            self.assertIn('socket is not private', result['reason'])
        finally:
            server.path.chmod(0o600)
        from unittest.mock import Mock
        connection = Mock()
        connection.getsockopt.return_value = struct.pack('3i', 1234, os.getuid() + 1, os.getgid())
        with self.assertRaisesRegex(HumanRequired, 'peer UID'):
            _check_peer(connection)
        connection.getsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))

    def test_cli_preserves_safe_server_failure_diagnostics(self):
        app, _server = self.running_operator()
        from danta.adapters import AdapterError
        failure = AdapterError('PROVIDER_REJECTED', diagnostic={'endpoint': '/synthetic', 'http_status': 429,
            'provider_code': 'SYNTHETIC_LIMIT', 'app_secret': 'private-synthetic-value'})
        with patch.object(app, 'reconcile', side_effect=failure):
            code, result = self.invoke(['reconcile'])
        self.assertEqual(code, 1, result)
        self.assertEqual(result['reason'], 'PROVIDER_REJECTED')
        self.assertNotIn('private-synthetic-value', json.dumps(result))
        self.assertEqual(result['diagnostic']['http_status'], 429)

    def test_cli_accepts_the_source_hash_of_resolved_automatic_configuration(self):
        from dataclasses import replace
        app, _server = self.running_operator()
        app.config = replace(app.config, config_hash='resolved-runtime-config', source_config_hash=self.config.config_hash)
        code, result = self.invoke(['status'])
        self.assertEqual(code, 0, result)
        request = self.request(['pause'])
        request['config_hash'] = 'different-source-config'
        from danta.operator import RemoteOperatorError
        with self.assertRaises(RemoteOperatorError) as failure:
            connect_or_claim(self.config, request)
        self.assertIn('configuration differs', str(failure.exception))
        self.assertFalse(app.store.get('paused'))

    def test_cli_keeps_working_after_allowed_model_reload_but_rejects_policy_change(self):
        from dataclasses import replace
        import yaml
        from danta.config import model_reload_hash
        app, _server = self.running_operator()
        app.config = replace(app.config, source_config_hash=self.config.config_hash,
                             model_reload_baseline=model_reload_hash(self.config.data))
        path = self.config_dir / 'app.yaml'
        data = yaml.safe_load(path.read_text())
        for field, value in (('timeout_seconds', data['model']['timeout_seconds'] + 1),
                             ('reasoning_effort', 'high'), ('model_id', 'gpt-6-sol')):
            data['model'][field] = value
            path.write_text(yaml.safe_dump(data))
            app.config.assert_current()
            code, result = self.invoke(['status'])
            self.assertEqual(code, 0, (field, result))
        data['monitoring']['order_poll_active_seconds'] += 1
        path.write_text(yaml.safe_dump(data))
        code, result = self.invoke(['pause'])
        self.assertEqual(code, 2, result)
        self.assertIn('POLICY_CHANGED', result['reason'])
        self.assertFalse(app.store.get('paused'))

    def test_remote_report_matches_standalone_report_for_protective_fills(self):
        app, server = self.running_operator()
        app.review()
        with app.store.transaction():
            thesis = app.theses()[0]
            app._save_thesis(thesis.model_copy(update={'current_stop': self.bundle.quotes['TEST:AAA'].bid}))
        app.protect()
        sell = next(row for row in app.store.working() if row['side'] == 'SELL')
        app.broker.fill(sell['broker_id'], sell['quantity'], self.bundle.quotes['TEST:AAA'].bid, Decimal(0), self.bundle.now)
        app.reconcile()
        code, paths = self.invoke(['report'])
        self.assertEqual(code, 0, paths)
        remote = json.loads(Path(paths['json']).read_text())
        from danta.operator import execute_operator
        standalone_paths = execute_operator(app, self.request(['report']))
        standalone = json.loads(Path(standalone_paths['json']).read_text())
        for key in ('orders', 'fills', 'runs', 'status', 'nav', 'theses'):
            self.assertEqual(remote[key], standalone[key], key)
        self.assertEqual([row['quantity'] for row in remote['fills'] if row['side'] == 'SELL'], [sell['quantity']])

    def test_timeout_does_not_fallback_retry_or_release_writer_during_work(self):
        app, server = self.running_operator()
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        pause = app.pause
        def delayed_pause():
            entered.set()
            release.wait(5)
            try:
                return pause()
            finally:
                completed.set()
        try:
            with patch.object(app, 'pause', side_effect=delayed_pause) as called, \
                    patch('danta.cli.connect_or_claim', side_effect=lambda config, request: connect_or_claim(config, request, timeout=.1)):
                code, result = self.invoke(['pause'])
                self.assertEqual(code, 2, result)
                self.assertIn('LOCAL_OPERATOR_OUTCOME_UNKNOWN', result['reason'])
                self.assertTrue(entered.is_set())
                # Reads stay available while the separate request thread is busy.
                self.assertEqual(self.invoke(['status'])[0], 0)
                with self.assertRaisesRegex(HumanRequired, 'retain writer'):
                    server.close(timeout=.01)
                with self.assertRaisesRegex(HumanRequired, 'SECOND_WRITER_REJECTED'):
                    Application(self.config, self.bundle)
                with self.assertRaisesRegex(HumanRequired, 'LOCAL_OPERATOR_UNAVAILABLE'):
                    OperatorLease(self.config)
                release.set()
                self.assertTrue(completed.wait(2))
                server.close()
                called.assert_called_once()
                self.assertTrue(app.store.get('paused'))
        finally:
            release.set()

    def test_cli_report_includes_standalone_protection_fills(self):
        app = Application(self.config, self.bundle)
        try:
            app.review()
            with app.store.transaction():
                thesis = app.theses()[0]
                app._save_thesis(thesis.model_copy(update={'current_stop': self.bundle.quotes['TEST:AAA'].bid}))
            app.protect()
            sell = next(row for row in app.store.working() if row['side'] == 'SELL')
            app.broker.fill(sell['broker_id'], sell['quantity'], self.bundle.quotes['TEST:AAA'].bid, Decimal(0), self.bundle.now)
            app.reconcile()
            output = io.StringIO()
            day = utcnow().astimezone(ZoneInfo('Asia/Seoul')).date().isoformat()
            with patch('danta.cli.make_application', return_value=app), patch.object(app, 'close'), redirect_stdout(output):
                code = main(['--config-dir', str(self.config_dir), 'report', '--date', day])
            self.assertEqual(code, 0, output.getvalue())
            paths = json.loads(output.getvalue())
            report = json.loads(Path(paths['json']).read_text())
            sells = [row for row in report.get('fills', []) if row['side'] == 'SELL']
            self.assertEqual([row['quantity'] for row in sells], [sell['quantity']])
            self.assertEqual([row['reason'] for row in report['orders'] if row['side'] == 'SELL'], ['EXIT_PROTECTION'])
        finally:
            app.close()
