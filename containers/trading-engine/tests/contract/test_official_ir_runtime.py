"""Official IR collection through real runtime, frozen input and read tools."""
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import zipfile

import yaml

from danta.adapters import AdapterError, HttpResponse
from danta.adapters.disclosures import DartAdapter, OFFICIAL_IR_MAX_BYTES
from danta.adapters.market_tools import MarketTools
from danta.application import Application, fixture_decision
from danta.config import load_config
from danta.decision import freeze_input
from danta.deployment import prepare_application
from danta.runtime import RuntimeState
from tests.contract import test_runtime as runtime_fixture


IR_URL = 'https://issuer.invalid/ir/results.html'
IR_BODY = ('<html><body>공식 IR 보충 설명. 공개시각은 확인되지 않았습니다.'
           '<a href="https://issuer.invalid/ir/followup.html">하위 링크</a></body></html>').encode()
TABLE = ('<p>연결 기준 (단위: 백만원)</p><table><tr><th>구분</th>'
         '<th>당기실적 2026.01.01~2026.03.31</th><th>전년동기실적 2025.01.01~2025.03.31</th></tr>'
         '<tr><td>매출액</td><td>1,200</td><td>1,000</td></tr>'
         '<tr><td>영업이익</td><td>150</td><td>100</td></tr></table>')


class OfficialIRTransport:
    fixture_only = True

    def __init__(self, fallback, now, *, links=(IR_URL,)):
        self.fallback, self.now = fallback, now
        self.links = links
        self.ir_calls = []
        self.corp_code = '00000001'
        self.ir_response = HttpResponse(200, IR_BODY, {'content-type': 'text/html; charset=utf-8'})

    def __call__(self, method, url, headers=None, body=None, timeout=15):
        parsed = urlsplit(url)
        if parsed.hostname == 'opendart.fss.or.kr' and parsed.path.endswith('/list.json'):
            row = {'rcept_no': self.now.strftime('%Y%m%d') + '000001',
                   'rcept_dt': self.now.strftime('%Y%m%d'), 'corp_code': self.corp_code,
                   'report_nm': '연결 영업실적 공시'}
            return HttpResponse(200, json.dumps({'status': '000', 'page_no': 1,
                'total_page': 1, 'total_count': 1, 'list': [row]}).encode())
        if parsed.hostname == 'opendart.fss.or.kr' and parsed.path.endswith('/document.xml'):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr('official.xml', TABLE + ''.join(f'<a href="{link}">IR</a>' for link in self.links))
            return HttpResponse(200, stream.getvalue())
        if parsed.hostname == 'issuer.invalid':
            self.ir_calls.append(url)
            return self.ir_response
        return self.fallback(method, url, headers, body, timeout)


class OfficialIRRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_fixture.ExternalRuntimeContracts()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        path = self.fixture.config_dir / 'app.yaml'
        data = yaml.safe_load(path.read_text())
        data['market']['official_ir_domains'] = ['issuer.invalid']
        path.write_text(yaml.safe_dump(data))
        self.fixture.config = load_config(self.fixture.config_dir)
        self.fixture.approval['config_hash'] = self.fixture.config.config_hash
        self.fixture.manifest['disclosures']['instrument_by_corp_code'] = {'00000001': 'KRX:000001'}
        self.fixture._save_manifest()
        self.fixture.approval['operational_evidence']['runtime_manifest_sha256'] = hashlib.sha256(
            (self.fixture.base / 'manifest.json').read_bytes()).hexdigest()
        self.transport = OfficialIRTransport(self.fixture.transport, self.fixture.now)
        self.fixture.transport = self.transport

    def application(self):
        bundle, broker, decide, refresh = self.fixture._factory()
        app = Application(self.fixture.config, bundle, broker=broker, decide=decide,
                          refresh=refresh, approval=self.fixture.approval)
        self.addCleanup(app.close)
        app.reconcile()
        return app, decide.__self__

    def test_collected_ir_reaches_actual_frozen_market_tools_without_invented_facts(self):
        app, runtime = self.application()
        bundle = runtime.collect_disclosures()
        self.assertEqual(self.transport.ir_calls, [IR_URL])
        self.assertTrue(all(fact.source != IR_URL for fact in bundle.facts))
        frozen = freeze_input(run_id='OFFICIAL_IR_FIXTURE', config_hash=app.config.config_hash,
            strategy_hash=app.config.strategy_hash, code_id=app.code_id, now=self.fixture.now,
            session_id=self.fixture.now.date().isoformat(), profile=app.profile, portfolio=app.portfolio(),
            candidates=bundle.candidates, events=bundle.events, facts=bundle.facts, theses=[],
            raw_documents=bundle.data['raw_documents'])
        inspected = []
        def runner(command, *, env, cwd, input, timeout):
            snapshot = json.loads(Path(cwd, 'input.json').read_text())
            tools = MarketTools(snapshot, persist_lookup=inspected.append)
            search = tools.call('search_official_evidence', {'instrument_id': 'KRX:000001',
                'start': self.fixture.now.date().isoformat(), 'end': self.fixture.now.date().isoformat(),
                'query': '보충 설명', 'page': 1})['data']
            self.assertEqual(search['total_count'], 1)
            document = tools.call('get_fact', {'fact_id': search['records'][0]['fact_id']})['data']
            self.assertEqual(document['content'], IR_BODY.decode())
            self.assertEqual(document['sha256'], hashlib.sha256(IR_BODY).hexdigest())
            self.assertEqual(document['corp_code'], '00000001')
            self.assertEqual(document['instrument_id'], 'KRX:000001')
            self.assertIsNone(document['published_at'])
            self.assertEqual(document['observed_at'], self.fixture.now.astimezone(timezone.utc).isoformat())
            self.assertEqual(document['available_at'], self.fixture.now.astimezone(timezone.utc).isoformat())
            self.assertEqual(document['timing_quality'], 'UNCERTAIN')
            Path(command[command.index('--output-last-message') + 1]).write_text(json.dumps(fixture_decision(frozen)))
            return 0, '{"type":"turn.completed"}', ''
        runtime.codex.runner = runner
        runtime.clock = lambda: datetime.now(timezone.utc)
        runtime.decide(frozen)
        self.assertEqual([item['tool'] for item in inspected], ['search_official_evidence', 'get_fact'])
        self.assertEqual(self.transport.ir_calls, [IR_URL])

    def test_unapproved_links_are_not_fetched(self):
        self.transport.links = ('https://issuer.invalid.evil.invalid/ir', 'http://issuer.invalid/ir',
            'https://user@issuer.invalid/ir', 'https://issuer.invalid:444/ir', 'https://issuer.invalid/ir#fragment')
        app, runtime = self.application()
        bundle = runtime.collect_disclosures()
        self.assertEqual(self.transport.ir_calls, [])
        self.assertTrue(any(row['reason'] == 'OFFICIAL_IR_LINK_OUTSIDE_ALLOWED_SCOPE'
                            for row in bundle.data['runtime_diagnostics']))
        self.assertFalse(any(key.startswith('official-ir:') for key in bundle.data['raw_documents']))
    def test_unknown_corporation_cannot_supply_official_ir_for_a_scoped_company(self):
        self.transport.corp_code = '99999999'
        app, runtime = self.application()
        bundle = runtime.collect_disclosures()
        self.assertEqual(self.transport.ir_calls, [])
        self.assertEqual(bundle.data['raw_documents'], {})

    def test_empty_domain_configuration_keeps_ir_collection_disabled(self):
        path = self.fixture.config_dir / 'app.yaml'
        data = yaml.safe_load(path.read_text())
        data['market']['official_ir_domains'] = []
        path.write_text(yaml.safe_dump(data))
        self.fixture.config = load_config(self.fixture.config_dir)
        self.fixture.approval['config_hash'] = self.fixture.config.config_hash
        app, runtime = self.application()
        bundle = runtime.collect_disclosures()
        self.assertEqual(self.transport.ir_calls, [])
        self.assertFalse(any(key.startswith('official-ir:') for key in bundle.data['raw_documents']))

    def test_binary_ir_is_reported_as_incomplete_without_inventing_text_or_facts(self):
        self.transport.ir_response = HttpResponse(200, b'%PDF-1.7 unsupported binary',
                                                 {'content-type': 'application/pdf'})
        app, runtime = self.application()
        bundle = runtime.collect_disclosures()
        self.assertEqual(self.transport.ir_calls, [IR_URL])
        self.assertEqual(bundle.data['coverage']['KRX:000001'], 'PARTIAL')
        self.assertTrue(any(row['reason'] == 'OFFICIAL_IR_UNSUPPORTED_FORMAT'
                            for row in bundle.data['runtime_diagnostics']))
        self.assertFalse(any(key.startswith('official-ir:') for key in bundle.data['raw_documents']))

    def test_transient_ir_failure_retries_after_poll_gap_and_clears_stale_diagnostics(self):
        self.transport.ir_response = HttpResponse(503, b'provider error body')
        app, runtime = self.application()
        first = runtime.collect_disclosures()
        self.assertEqual(len(self.transport.ir_calls), 1)
        issue = next(row for row in first.data['runtime_diagnostics'] if row.get('source') == 'OFFICIAL_IR')
        self.assertEqual((issue['reason'], issue['http_status']), ('TRANSIENT_FAILURE', 503))
        original_available = first.events[0].available_at
        self.transport.ir_response = HttpResponse(200, IR_BODY, {'content-type': 'text/html'})
        later = self.fixture.now + timedelta(seconds=181)
        runtime.clock = lambda: later
        with patch('danta.adapters.disclosures.utcnow', return_value=later):
            recovered = runtime.collect_disclosures()
        self.assertEqual(len(self.transport.ir_calls), 2)
        self.assertEqual(recovered.events[0].available_at, original_available)
        self.assertFalse(any(row.get('source') == 'OFFICIAL_IR' for row in recovered.data['runtime_diagnostics']))
        document = next(value for key, value in recovered.data['raw_documents'].items() if key.startswith('official-ir:'))
        self.assertEqual(document['available_at'], later.astimezone(timezone.utc).isoformat())

    def test_cached_ir_keeps_first_version_and_available_time_across_runtime_state_reload(self):
        app, runtime = self.application()
        receipt = self.fixture.now.strftime('%Y%m%d') + '000001'
        first = runtime.state.disclosure_documents(receipt)
        self.transport.ir_response = HttpResponse(200, b'changed after the original observation')
        runtime.clock = lambda: self.fixture.now + timedelta(seconds=181)
        runtime.collect_disclosures()
        self.assertEqual(self.transport.ir_calls, [IR_URL])
        restored = RuntimeState(app.config.state_dir / 'state.sqlite')
        self.addCleanup(restored.close)
        self.assertEqual(restored.disclosure_documents(receipt), first)
        self.assertTrue(all('content' not in document for document in restored.data['disclosure_records'][receipt]['documents'].values()))

    def test_ir_response_size_and_redirect_boundaries_remain_enforced(self):
        adapter = DartAdapter(api_key='fixture', transport=self.transport, official_ir_domains=['issuer.invalid'])
        self.transport.ir_response = HttpResponse(302, b'', {'location': 'https://other.invalid/secret'})
        with self.assertRaisesRegex(AdapterError, 'HTTP_FAILURE'):
            adapter.read_official_ir(IR_URL)
        self.assertEqual(self.transport.ir_calls, [IR_URL])
        self.transport.ir_response = HttpResponse(200, b'x' * (OFFICIAL_IR_MAX_BYTES + 1))
        with self.assertRaisesRegex(AdapterError, 'OFFICIAL_IR_DOCUMENT_SIZE_LIMIT'):
            adapter.read_official_ir(IR_URL)

    def test_automatic_deployment_transport_includes_only_configured_ir_origins(self):
        class StopAfterAdapterConstruction(Exception):
            pass
        origins = []
        def transport(**kwargs):
            origins.append(kwargs['allowed_origins'])
            return self.transport
        with patch('danta.deployment.require_operator_config'), \
                patch('danta.deployment.load_secrets', return_value=self.fixture.env), \
                patch('danta.deployment.model_evidence', return_value={}), \
                patch('danta.adapters.http_transport', side_effect=transport), \
                patch('danta.runtime.KisBrokerPort.census', side_effect=StopAfterAdapterConstruction):
            with self.assertRaises(StopAfterAdapterConstruction):
                prepare_application(self.fixture.config, clock=lambda: self.fixture.now)
        self.assertIn({'https://opendart.fss.or.kr', 'https://issuer.invalid'}, origins)


if __name__ == '__main__':
    unittest.main()
