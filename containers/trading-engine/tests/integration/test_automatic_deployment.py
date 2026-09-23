"""Actual startup pipeline with only provider boundaries replaced by fixtures."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import yaml

from danta.config import ROOT, HumanRequired, load_config
from danta.deployment import prepare_application, require_operator_config

spec = importlib.util.spec_from_file_location('deployment_probe', ROOT/'tests/fixtures/deployment_probe.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class AutomaticDeploymentTests(unittest.TestCase):
    def test_live_startup_adopts_once_and_never_requires_handwritten_runtime_files(self):
        import danta.adapters as adapters
        import danta.adapters.kis as kis
        import danta.adapters.disclosures as dart
        import danta.deployment as deployment
        import danta.runtime as runtime
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            config_dir = directory/'config'
            config_dir.mkdir()
            for name in ('app.yaml','strategy.yaml','schedules.yaml'):
                shutil.copyfile(ROOT/'config'/name, config_dir/name)
            data = yaml.safe_load((config_dir/'app.yaml').read_text())
            data['app']['state_dir'] = str(directory/'state')
            data['model']['executable'] = '/usr/bin/true'
            (config_dir/'app.yaml').write_text(yaml.safe_dump(data))
            (config_dir/'secrets.yaml').write_text(yaml.safe_dump({'KIS_ACCOUNT_REF':'00000000-00',
                'KIS_APP_KEY':'FAKE_KEY_NOT_A_CREDENTIAL','KIS_APP_SECRET':'FAKE_SECRET_NOT_A_CREDENTIAL',
                'DART_API_KEY':'FAKE_DART_NOT_A_CREDENTIAL','DANTA_CODEX_AUTH_HOME':str(directory/'auth'),
                'TELEGRAM_GATEWAY_URL':'http://telegram-gateway:8080',
                'TELEGRAM_ALLOWED_CHAT_IDS':'12345','TELEGRAM_ALLOWED_SENDER_IDS':'12345'}))
            (config_dir/'secrets.yaml').chmod(0o600)
            source = load_config(config_dir)
            self.assertEqual(source.mode, 'live')
            with self.assertRaises(HumanRequired):
                require_operator_config(source)
            # Patch only ownership for this nonroot temp directory; Docker smoke
            # exercises the real root-owned read-only mount and initializer.
            with patch.object(adapters,'http_transport'), patch.object(runtime,'http_transport'), \
                    patch.object(kis,'utcnow'), patch.object(dart,'utcnow'), \
                    patch.object(kis.KisAdapter,'read_instruments'), patch.object(kis.KisAdapter,'subscribe_quotes'), \
                    patch.object(kis.KisAdapter,'stream_quote'), patch.object(deployment,'model_evidence'), \
                    patch.object(deployment,'prepare_application'), patch.object(deployment,'require_operator_config'):
                fixture.install()
                fixture.CALLS.clear()
                app = prepare_application(source, clock=lambda:fixture.NOW)
                try:
                    app.activate(app.config.config_hash)
                    self.assertEqual(app.store.quantity('KRX:005930'),10)
                    self.assertEqual(app.store.get('cash_krw'),'1000000')
                    self.assertTrue(app.store.get('account_cash_reconciled'))
                    self.assertEqual(app.profile['capital_krw'],'1200000')
                    baseline = app.store.get('account_adoption')
                    self.assertEqual(app.config.app['telegram']['allowed_sender_ids'], ['12345'])
                    app.store.set('monitor_degraded', True)
                    app.protect()
                    self.assertTrue(app.store.get('monitor_degraded'))
                    from datetime import timedelta
                    app.store.set('monitor_healthy_since', (app.bundle.now - timedelta(seconds=61)).isoformat())
                    app.protect()
                    self.assertFalse(app.store.get('monitor_degraded'))
                    changed = dict(data, model={**data['model'], 'model_id': 'gpt-5.6-luna', 'reasoning_effort': 'medium'})
                    (config_dir/'app.yaml').write_text(yaml.safe_dump(changed))
                    app.config.assert_current()
                    self.assertEqual(app.status()['model_id'], 'gpt-5.6-luna')
                    app.protect()
                    self.assertFalse(app.store.get('monitor_degraded'))
                    (config_dir/'app.yaml').write_text(yaml.safe_dump(data))
                finally:
                    app.close()
                app = prepare_application(source, clock=lambda:fixture.NOW)
                try:
                    app.activate(app.config.config_hash)
                    self.assertEqual(app.store.get('account_adoption'),baseline)
                    self.assertEqual(app.store.quantity('KRX:005930'),10)
                finally:
                    app.close()
                self.assertFalse(any(method=='POST' and path.startswith('/uapi/') for method,path in fixture.CALLS))
            self.assertFalse((config_dir/'runtime.json').exists())
            self.assertFalse((config_dir/'runtime-manifest.json').exists())


if __name__ == '__main__':
    unittest.main()
