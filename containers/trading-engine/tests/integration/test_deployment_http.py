"""Real loopback HTTP, with no broker, model or Telegram network access."""
from contextlib import contextmanager, redirect_stderr, nullcontext
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from danta.config import ROOT, load_config
from danta.adapters.telegram import MAX_REQUEST_BYTES
from danta.service import serve


class DeploymentHTTPTest(unittest.TestCase):
    @contextmanager
    def running(self, factory, *, skip_requirements=False, mode='offline'):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in ('app', 'strategy', 'schedules'):
                shutil.copyfile(ROOT / 'tests/fixtures/config' / (name + '.yaml'), directory / (name + '.yaml'))
            app_path = directory/'app.yaml'
            app_path.write_text(app_path.read_text().replace('mode: offline', 'mode: ' + mode))
            config = load_config(directory)
            stop, bound = threading.Event(), threading.Event()
            self.stop_event = stop
            server = None
            errors = []
            def bind(_address, handler):
                nonlocal server
                server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
                bound.set()
                return server
            def run():
                try:
                    serve(config, application_factory=factory, stop_event=stop)
                except Exception as error:
                    errors.append(error)
            stderr = io.StringIO()
            with redirect_stderr(stderr), patch('danta.service.ThreadingHTTPServer', side_effect=bind):
                with patch('danta.service.RuntimeHost.requirements', return_value=[]) if skip_requirements else nullcontext():
                    thread = threading.Thread(target=run)
                    thread.start()
                    try:
                        self.assertTrue(bound.wait(5))
                        self.origin = f'http://127.0.0.1:{server.server_port}'
                        yield stderr
                    finally:
                        stop.set()
                        thread.join(10)
                        self.assertFalse(thread.is_alive())
                        self.assertEqual(errors, [])

    def request(self, path, body=None, content_type='application/json'):
        request = Request(self.origin + path, data=body,
                          headers={'Content-Type': content_type} if body is not None else {})
        try:
            response = build_opener(ProxyHandler({})).open(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_default_deployment_serves_version_and_explains_inactive_runtime(self):
        factory = Mock(side_effect=AssertionError('must not start fixture trading'))
        with self.running(factory) as logs:
            code, version = self.request('/version')
            self.assertEqual(code, 200)
            self.assertTrue(version['version'])
            self.assertEqual(len(version['code_id']), 64)
            code, health = self.request('/readyz')
            self.assertEqual(code, 503)
            self.assertFalse(health['ready'])
            self.assertEqual(self.request('/healthz')[0], 200)
            self.assertEqual(self.request('/unknown')[0], 404)
            code, reply = self.request('/telegram', json.dumps({'text': '/review'}).encode())
            self.assertEqual(code, 503)
            self.assertFalse(reply['accepted'])
            self.assertIn('준비되지', reply['reply_text'])
            self.assertIn('HTTP_LISTENING', logs.getvalue())
        self.assertIn('WAITING_FOR_CONFIGURATION', logs.getvalue())
        self.assertIn('runtime.json', logs.getvalue())
        factory.assert_not_called()

    def test_slow_then_failed_initialization_keeps_diagnostics_and_redacts_error(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def factory(*_):
            entered.set()
            if not release.wait(5):
                raise AssertionError('release timeout')
            finished.set()
            raise ValueError('SYNTHETIC_PRIVATE_VALUE_DO_NOT_PRINT')
        with self.running(factory, skip_requirements=True) as logs:
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.request('/version')[1]['runtime_status'], 'STARTING')
            self.assertFalse(self.request('/readyz')[1]['ready'])
            release.set()
            self.assertTrue(finished.wait(3))
            # The version endpoint stays available after initialization fails.
            self.assertEqual(self.request('/version')[0], 200)
        self.assertIn('INITIALIZATION_FAILED', logs.getvalue())
        self.assertNotIn('SYNTHETIC_PRIVATE_VALUE_DO_NOT_PRINT', logs.getvalue())

    def test_ingress_limits_apply_even_before_runtime_is_ready(self):
        with self.running(Mock()) as logs:
            self.assertEqual(self.request('/telegram', b'{}', 'text/plain')[0], 403)
            self.assertEqual(self.request('/telegram', b'x' * (MAX_REQUEST_BYTES + 1))[0], 403)
        self.assertIn('INGRESS_REJECTED', logs.getvalue())

    def test_stop_during_initialization_never_starts_trading_workers(self):
        entered, release = threading.Event(), threading.Event()
        app = Mock()
        def factory(*_):
            entered.set()
            self.assertTrue(release.wait(5))
            return app
        with patch('danta.service.Service') as service:
            with self.running(factory, skip_requirements=True, mode='live'):
                self.assertTrue(entered.wait(3))
                self.stop_event.set()
                release.set()
            service.assert_not_called()
        app.activate.assert_not_called()
        app.close.assert_called_once()

    def test_live_activation_precedes_workers_and_failure_keeps_diagnostics(self):
        app = Mock()
        app.activate.side_effect = ValueError('synthetic activation refusal')
        with patch('danta.service.Service') as service:
            with self.running(Mock(return_value=app), skip_requirements=True, mode='live') as logs:
                self.assertEqual(self.request('/version')[0], 200)
            service.assert_not_called()
        app.activate.assert_called_once()
        app.close.assert_called_once()
        self.assertIn('INITIALIZATION_FAILED', logs.getvalue())


if __name__ == '__main__':
    unittest.main()
