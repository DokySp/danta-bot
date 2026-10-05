"""Frozen original-document authority from collection through the review gate."""
from copy import deepcopy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from danta.adapters import AdapterError, HttpResponse
from danta.adapters.market_tools import MarketTools
from danta.application import Application, fixture_decision
from danta.config import canonical, digest
from danta.decision import freeze_input
from tests.contract import test_official_ir_runtime as ir_fixture


class FrozenDocumentTests(unittest.TestCase):
    setUp = ir_fixture.OfficialIRRuntimeTests.setUp
    application = ir_fixture.OfficialIRRuntimeTests.application

    def frozen(self, app, runtime, documents=None):
        bundle = runtime.latest_bundle
        return freeze_input(run_id='FROZEN_DOCUMENTS', config_hash=app.config.config_hash,
            strategy_hash=app.config.strategy_hash, code_id=app.code_id, now=self.fixture.now,
            session_id=self.fixture.now.date().isoformat(), profile=app.profile, portfolio=app.portfolio(),
            candidates=bundle.candidates, events=bundle.events, facts=bundle.facts, theses=[],
            raw_documents=bundle.data['raw_documents'] if documents is None else documents)

    def add_document(self, runtime, *, now=None):
        receipt = self.fixture.now.strftime('%Y%m%d') + '000001'
        document = dict(next(value for value in runtime.state.disclosure_documents(receipt).values()
                             if value['source'] == ir_fixture.IR_URL))
        document.update(fact_id='official-ir:later', source='https://issuer.invalid/ir/later.html',
                        content='New official IR collected after freezing.')
        document['sha256'] = hashlib.sha256(document['content'].encode()).hexdigest()
        document['content_sha256'] = document['sha256']
        if now:
            document.update(available_at=now.isoformat(), observed_at=now.isoformat())
        record = runtime.state.data['disclosure_records'][receipt]
        documents = {**runtime.state.disclosure_documents(receipt), document['fact_id']: document}
        runtime.state.data['disclosure_records'][receipt] = runtime.state.save_disclosure(
            receipt, {**record, 'documents': documents})
        runtime.latest_bundle.data['raw_documents'].update(
            runtime.state.data['disclosure_records'][receipt]['documents'])
        return document

    def run_model(self, runtime, frozen, inspect):
        def runner(command, *, cwd, **kwargs):
            snapshot = json.loads(Path(cwd, 'input.json').read_text())
            inspect(MarketTools(snapshot), snapshot)
            Path(command[command.index('--output-last-message') + 1]).write_text(
                json.dumps(fixture_decision(frozen)))
            return 0, '{"type":"turn.completed"}', ''
        runtime.codex.runner = runner
        with patch.object(runtime, 'clock', lambda: datetime.now(timezone.utc)):
            return runtime.decide(frozen)

    def test_original_manifest_changes_both_hashes_without_copying_bodies(self):
        app, runtime = self.application()
        frozen = self.frozen(app, runtime)
        self.assertTrue(frozen['document_manifest'])
        self.assertTrue(all('content' not in item for item in frozen['document_manifest'].values()))
        self.assertEqual(frozen['document_manifest'], runtime.latest_bundle.data['raw_documents'])
        for field, changed in (('sha256', 'a' * 64), ('content_sha256', 'b' * 64),
                               ('source', 'https://issuer.invalid/ir/revised'),
                               ('available_at', (self.fixture.now - timedelta(seconds=1)).isoformat())):
            with self.subTest(field=field):
                documents = deepcopy(runtime.latest_bundle.data['raw_documents'])
                next(iter(documents.values()))[field] = changed
                modified = self.frozen(app, runtime, documents)
                self.assertNotEqual(frozen['input_snapshot_id'], modified['input_snapshot_id'])
                self.assertNotEqual(frozen['material_hash'], modified['material_hash'])
        unrelated = deepcopy(runtime.latest_bundle.data['raw_documents'])
        unrelated['foreign'] = dict(next(iter(unrelated.values())), fact_id='foreign', instrument_id='KRX:999999')
        self.assertEqual(frozen, self.frozen(app, runtime, unrelated))

    def test_document_added_after_freeze_is_not_visible_to_actual_model_tools(self):
        app, runtime = self.application()
        frozen = self.frozen(app, runtime)
        extra = self.add_document(runtime)
        def inspect(tools, snapshot):
            documents = {key for key, value in snapshot['tool_records']['facts'].items() if 'content' in value}
            self.assertEqual(documents, set(frozen['document_manifest']))
            with self.assertRaisesRegex(AdapterError, 'EVIDENCE_NOT_FOUND'):
                tools.call('get_fact', {'fact_id': extra['fact_id']})
        self.run_model(runtime, frozen, inspect)

    def test_actual_market_tools_rejects_injected_or_changed_document_records(self):
        app, runtime = self.application()
        frozen = self.frozen(app, runtime)
        def inspect(_tools, snapshot):
            key = next(iter(frozen['document_manifest']))
            for change in ('extra', 'missing', 'body'):
                with self.subTest(change=change):
                    modified = deepcopy(snapshot)
                    records = modified['tool_records']['facts']
                    if change == 'extra':
                        records['extra'] = dict(records[key], fact_id='extra')
                    elif change == 'missing':
                        records.pop(key)
                    else:
                        records[key]['content'] = 'Changed after tool input was created.'
                    with self.assertRaisesRegex(AdapterError, 'FROZEN_DOCUMENT_'):
                        MarketTools(modified)
        self.run_model(runtime, frozen, inspect)

    def test_original_byte_hash_and_decoded_text_hash_both_survive_non_utf8_ir(self):
        content = '<p>공식 IR 원문 자료</p>'.encode('euc-kr')
        self.transport.ir_response = HttpResponse(200, content, {'content-type': 'text/html; charset=euc-kr'})
        app, runtime = self.application()
        frozen = self.frozen(app, runtime)
        def inspect(tools, _snapshot):
            key = next(key for key in frozen['document_manifest'] if key.startswith('official-ir:'))
            document = tools.call('get_fact', {'fact_id': key})['data']
            self.assertEqual(document['sha256'], hashlib.sha256(content).hexdigest())
            self.assertEqual(document['content_sha256'], hashlib.sha256(document['content'].encode()).hexdigest())
            self.assertNotEqual(document['sha256'], document['content_sha256'])
            self.assertEqual(document['content'], content.decode('euc-kr'))
        self.run_model(runtime, frozen, inspect)

    def test_legacy_document_metadata_gets_text_hash_without_retaining_bodies_in_memory(self):
        app, runtime = self.application()
        receipt = next(iter(runtime.state.data['disclosure_records']))
        record = deepcopy(runtime.state.data['disclosure_records'][receipt])
        originals = runtime.state.disclosure_documents(receipt)
        for document in [*record['documents'].values(), *originals.values()]:
            document.pop('content_sha256')
        runtime.state.data['disclosure_records'][receipt] = record
        runtime.state.cache_db.execute('UPDATE disclosure_records SET metadata=?,documents=? WHERE receipt=?',
            (canonical(record), canonical(originals), receipt))
        with patch.object(runtime.state, 'disclosure_documents', wraps=runtime.state.disclosure_documents) as read:
            bundle = runtime.collect_disclosures()
        read.assert_called_once_with(receipt)
        frozen = self.frozen(app, runtime)
        for key, metadata in bundle.data['raw_documents'].items():
            self.assertNotIn('content', metadata)
            self.assertEqual(metadata['content_sha256'], hashlib.sha256(originals[key]['content'].encode()).hexdigest())
            self.assertEqual(metadata['sha256'], originals[key]['sha256'])
        self.run_model(runtime, frozen, lambda tools, snapshot: self.assertTrue(snapshot['document_manifest']))

    def test_decision_refresh_removes_originals_no_longer_in_the_scoped_collection(self):
        app, runtime = self.application()
        frozen = self.frozen(app, runtime)
        receipt = next(iter(frozen['document_manifest'].values()))['receipt_id']
        key = next(key for key in frozen['document_manifest'] if key.startswith('official-ir:'))
        originals = runtime.state.disclosure_documents(receipt)
        originals.pop(key)
        record = runtime.state.data['disclosure_records'][receipt]
        runtime.state.data['disclosure_records'][receipt] = runtime.state.save_disclosure(
            receipt, {**record, 'documents': originals})
        with patch('danta.adapters.kis.utcnow', return_value=self.fixture.now), \
                patch('danta.adapters.disclosures.utcnow', return_value=self.fixture.now):
            refreshed = runtime.refresh_decision(frozen)
        self.assertNotIn(key, refreshed.data['raw_documents'])
        self.assertEqual(digest([refreshed.events, refreshed.facts]), digest([frozen['events'], frozen['facts']]))

    def test_missing_or_changed_original_cache_fails_before_model_execution(self):
        app, runtime = self.application()
        frozen = self.frozen(app, runtime)
        receipt = next(iter(frozen['document_manifest'].values()))['receipt_id']
        original = runtime.state.disclosure_documents(receipt)
        key = next(iter(original))
        for change in ('missing', 'content', 'source', 'available_at'):
            with self.subTest(change=change):
                documents = deepcopy(original)
                if change == 'missing':
                    documents.pop(key)
                else:
                    documents[key][change] = {'content': 'altered cache body', 'source': 'https://issuer.invalid/changed',
                        'available_at': (self.fixture.now + timedelta(seconds=1)).isoformat()}[change]
                runtime.state.cache_db.execute('UPDATE disclosure_records SET documents=? WHERE receipt=?',
                    (canonical(documents), receipt))
                with patch.object(runtime.codex, 'run', side_effect=AssertionError('changed original reached model')):
                    with self.assertRaisesRegex(AdapterError, 'FROZEN_DOCUMENT_'):
                        runtime.decide(frozen)

    def test_future_document_is_rejected_by_the_real_freeze_producer(self):
        app, runtime = self.application()
        documents = deepcopy(runtime.latest_bundle.data['raw_documents'])
        next(iter(documents.values()))['available_at'] = (self.fixture.now + timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError, 'FUTURE_EVIDENCE'):
            self.frozen(app, runtime, documents)

    def test_actual_review_rejects_ir_added_while_model_runs_with_unchanged_events_and_facts(self):
        fallback = self.transport.fallback
        def entry_quote(method, url, headers=None, body=None, timeout=15):
            response = fallback(method, url, headers, body, timeout)
            if 'inquire-asking-price-exp-ccn' in url:
                value = response.json()
                value['output1'].update(bidp1='10599', askp1='10600')
                return HttpResponse(200, json.dumps(value).encode())
            return response
        self.transport.fallback = entry_quote
        with patch('danta.adapters.kis.utcnow', return_value=self.fixture.now):
            app, runtime = self.application()
        before = digest([runtime.latest_bundle.events, runtime.latest_bundle.facts])
        app.decision_refresh = runtime.refresh_decision
        seen = []
        def runner(command, *, cwd, **kwargs):
            snapshot = json.loads(Path(cwd, 'input.json').read_text())
            MarketTools(snapshot)
            seen.append(snapshot)
            self.add_document(runtime)
            self.assertEqual(before, digest([runtime.latest_bundle.events, runtime.latest_bundle.facts]))
            Path(command[command.index('--output-last-message') + 1]).write_text(
                json.dumps(fixture_decision(snapshot)))
            return 0, '{"type":"turn.completed"}', ''
        runtime.codex.runner = runner
        def decide(frozen):
            with patch.object(runtime, 'clock', lambda: datetime.now(timezone.utc)):
                return runtime.decide(frozen)
        app.decide = decide
        with patch('danta.adapters.kis.utcnow', return_value=self.fixture.now), \
                patch('danta.adapters.disclosures.utcnow', return_value=self.fixture.now):
            with self.assertRaisesRegex(ValueError, '^STALE_DECISION$'):
                app.review(request_key='IR_CHANGED_DURING_MODEL')
        self.assertEqual(len(seen), 1)
        self.assertEqual(list(app.store.read('SELECT * FROM intents')), [])
        result = json.loads(app.store.read('SELECT result FROM requests')[0]['result'])
        self.assertEqual((result['reason'], result['decision_status']), ('STALE_DECISION', 'REVALIDATION_FAILED'))
        self.assertEqual(before, digest([runtime.latest_bundle.events, runtime.latest_bundle.facts]))


class FrozenDocumentDispatchTests(unittest.TestCase):
    from tests.integration.test_engine import EngineCase as _Fixture
    setUp = _Fixture.setUp
    tearDown = _Fixture.tearDown

    def check_dispatch_documents(self, change, *, final_check=False):
        document = {'fact_id': 'official-ir:dispatch', 'instrument_id': 'TEST:AAA',
            'receipt_id': 'synthetic-receipt', 'source': 'https://issuer.invalid/ir/current',
            'sha256': 'a' * 64, 'content_sha256': 'b' * 64,
            'available_at': self.bundle.now.isoformat(), 'interpretation_status': 'RAW_OFFICIAL_IR_DOCUMENT'}
        documents = self.bundle.data['raw_documents'] = {document['fact_id']: document}
        app = Application(self.config, self.bundle)
        try:
            if change is None:
                self.assertTrue(app.review()['orders'])
                self.assertEqual(app.broker.submissions, 1)
                return
            boundary = 'validate' if final_check else 'preflight'
            original = getattr(app.executor, boundary)
            def update_before_dispatch(intent, now):
                if change == 'add':
                    documents['official-ir:later'] = {**document, 'fact_id': 'official-ir:later'}
                elif change == 'remove':
                    documents.clear()
                else:
                    documents[document['fact_id']] = {**document, 'sha256': 'c' * 64,
                                                       'content_sha256': 'd' * 64}
                return original(intent, now)
            setattr(app.executor, boundary, update_before_dispatch)
            with self.assertRaisesRegex(ValueError, '^STALE_DECISION_EVIDENCE$'):
                app.review()
            self.assertEqual(app.broker.submissions, 0)
            if final_check:
                self.assertEqual([row['state'] for row in app.store.read('SELECT state FROM intents')], ['INVALIDATED'])
        finally:
            app.close()

    def test_new_ir_at_preflight_blocks_broker_submission(self):
        self.check_dispatch_documents('add')

    def test_removed_ir_at_preflight_blocks_broker_submission(self):
        self.check_dispatch_documents('remove')

    def test_changed_ir_at_preflight_blocks_broker_submission(self):
        self.check_dispatch_documents('change')

    def test_ir_change_after_reservation_is_rejected_at_final_dispatch_check(self):
        self.check_dispatch_documents('add', final_check=True)

    def test_unchanged_reviewed_ir_allows_the_validated_buy(self):
        self.check_dispatch_documents(None)


if __name__ == '__main__':
    unittest.main()
