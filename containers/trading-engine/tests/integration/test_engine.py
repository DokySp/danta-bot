"""Synthetic ledger and workflow regression checks; no account or network access."""
from __future__ import annotations

import json
import io
import shutil
import socket
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
import yaml

from danta.adapters import AdapterError
from danta.application import Application, MarketBundle, fixture_decision
from danta.config import ConfigurationError, HumanRequired, ROOT, canonical, digest, load_config, utcnow
from danta.decision import freeze_input, validate_proposal
from danta.market import SessionCalendar
from danta.execution import Executor, FixtureBroker, OrderIntent, needed_quantity
from danta.reporting import render_notification
from danta.store import Store


class EngineCase(unittest.TestCase):
    def test_partial_review_contract_excludes_context_positions(self):
        app = Application(self.config, self.bundle)
        try:
            app.review()
            frozen = freeze_input(run_id='partial', config_hash=self.config.config_hash,
                strategy_hash=self.config.strategy_hash, code_id=app.code_id, now=self.bundle.now,
                session_id=self.bundle.calendar.active(self.bundle.now).session_id, profile=app.profile,
                portfolio=app.portfolio(), candidates=[], events=self.bundle.events, facts=self.bundle.facts,
                theses=app.theses(), scope='PARTIAL', reviewed_positions=[])
            self.assertTrue(frozen['theses'])
            self.assertEqual(frozen['review_targets']['position_ids'], [])
            self.assertEqual(frozen['output_contract']['properties']['position_reviews']['maxItems'], 0)
            proposal = fixture_decision(frozen)
            args = dict(current_account_version=app.store.get('account_version'),
                current_facts_hash=digest([frozen['events'], frozen['facts']]),
                completed_at=self.bundle.now, now=self.bundle.now)
            self.assertEqual(validate_proposal(proposal, frozen, **args).position_reviews, [])
            proposal['position_reviews'] = [dict(instrument_id='TEST:AAA', thesis_id=app.theses()[0].thesis_id,
                action='KEEP', changed_event_ids=[], reason='context only')]
            with self.assertRaisesRegex(ValueError, 'POSITION_RESULT_OMITTED_OR_DUPLICATED'):
                validate_proposal(proposal, frozen, **args)
        finally:
            app.close()

    def test_new_evidence_at_buy_preflight_invalidates_the_approved_decision(self):
        app = Application(self.config, self.bundle)
        try:
            preflight = app.executor.preflight
            def correction_before_dispatch(intent, now):
                event = self.bundle.events[0]
                self.bundle.events.append(event.model_copy(update={
                    'event_id': 'later-correction', 'correction_of': event.event_id}))
                preflight(intent, now)
            app.executor.preflight = correction_before_dispatch
            with self.assertRaisesRegex(ValueError, 'STALE_DECISION_EVIDENCE'):
                app.review()
            self.assertEqual(app.broker.submissions, 0)
        finally:
            app.close()

    def test_ai_invalidation_preserves_monitor_updates_and_survives_quote_retry(self):
        app = Application(self.config, self.bundle)
        try:
            app.review()
            symbol = 'TEST:AAA'
            original = app.theses()[0]
            correction = self.bundle.events[0].model_copy(update={
                'event_id': 'official-neutral-correction', 'polarity': 'UNKNOWN',
                'correction_of': original.event_ids[0]})
            self.bundle.events.append(correction)
            quote = self.bundle.quotes[symbol]
            observed_high = quote.bid + 1000
            def decide(frozen):
                # A monitor observation arrives while the model is processing its older snapshot.
                self.bundle.quotes[symbol] = quote.model_copy(update={'bid': observed_high, 'ask': observed_high + 1})
                app.protect()
                self.bundle.quotes[symbol] = quote.model_copy(update={
                    'observed_at': self.bundle.now - timedelta(seconds=90)})
                proposal = fixture_decision(frozen)
                proposal['position_reviews'][0].update(action='EXIT_THESIS_INVALID',
                    changed_event_ids=[correction.event_id], reason=original.invalidation_case)
                return proposal
            app.decide = decide
            result = app.review()
            self.assertEqual(result['decision_status'], 'VALID')
            self.assertEqual(app.theses()[0].mfe_price, observed_high)
            self.assertEqual(app.theses()[0].invalidating_event_ids, [correction.event_id])
            self.assertFalse(any(row['side'] == 'SELL' for row in app.store.working()))
            # Retry even if the rolling provider snapshot no longer contains the approved event.
            self.bundle.events = []
            self.bundle.quotes[symbol] = quote
            result = app.protect()
            self.assertIn('EXIT_THESIS_INVALID', result[0]['reasons'])
            self.assertTrue(any(row['side'] == 'SELL' for row in app.store.working()))
        finally:
            app.close()

    def test_protection_thesis_update_is_atomic_against_concurrent_invalidation(self):
        app = Application(self.config, self.bundle)
        entered, release = threading.Event(), threading.Event()
        from danta.risk import update_trailing_stop
        try:
            app.review()
            def slow_update(*args, **kwargs):
                entered.set()
                if not release.wait(3):
                    raise AssertionError('trailing update was not released')
                return update_trailing_stop(*args, **kwargs)
            def save_invalidation():
                with app.store.transaction():
                    latest = app.theses()[0]
                    app._save_thesis(latest.model_copy(update={'invalidating_event_ids': ['approved-event']}))
            with patch('danta.application.update_trailing_stop', side_effect=slow_update), ThreadPoolExecutor(2) as pool:
                monitor = pool.submit(app.protect)
                self.assertTrue(entered.wait(3))
                writer = pool.submit(save_invalidation)
                self.assertFalse(writer.done())
                release.set()
                monitor.result(timeout=3)
                writer.result(timeout=3)
            self.assertEqual(app.theses()[0].invalidating_event_ids, ['approved-event'])
        finally:
            release.set()
            app.close()

    def test_missing_nav_mark_recovers_risk_checks_without_certifying_history(self):
        app = Application(self.config, self.bundle)
        try:
            for offset, nav, quality in ((0, '10000000', 'EXACT'), (1, '9900000', 'INSUFFICIENT_COVERAGE'),
                                         (2, '9900000', 'EXACT'), (3, '8000000', 'EXACT')):
                result = app._record_nav_point(dict(at=(self.bundle.now + timedelta(seconds=offset)).isoformat(),
                    nav=nav, session_id=None, completed=False, quality=quality), ownership_verified=True)
                if offset == 1:
                    self.assertTrue(app.portfolio().new_risk_paused)
                if offset == 2:
                    self.assertFalse(app.portfolio().new_risk_paused)
            self.assertEqual(result['coverage'], 'INSUFFICIENT_COVERAGE')
            self.assertIsNone(result['max_drawdown'])
            self.assertTrue(app.store.get('drawdown_paused'))
            self.assertTrue(app.portfolio().new_risk_paused)
        finally:
            app.close()

    def test_closed_thesis_clock_survives_rolling_calendar_and_review_reports_protection_order(self):
        app = Application(self.config, self.bundle)
        try:
            app.review()
            symbol = 'TEST:AAA'
            with app.store.transaction():
                thesis = app.theses()[0]
                app._save_thesis(thesis.model_copy(update={'current_stop': self.bundle.quotes[symbol].bid}))
            result = app.review()
            self.assertEqual(result['order_status'], 'ACKNOWLEDGED')
            sells = [order for order in app.store.working() if order['side'] == 'SELL']
            self.assertEqual(len(sells), 1)
            self.assertEqual(json.loads(sells[0]['payload'])['run_id'], result['run_id'])
            app.broker.fill(sells[0]['broker_id'], sells[0]['quantity'], self.bundle.quotes[symbol].bid, D(0), self.bundle.now)
            app.reconcile()
            self.assertIsNotNone(app.theses()[0].exited_at)
            old_clock = app.theses()[0].first_fill_session
            sessions = [session for session in self.bundle.calendar.sessions if session.opens_at > self.bundle.now]
            self.bundle.calendar = SessionCalendar(sessions, provenance='FIXTURE_ONLY', verified=True, synthetic=True)
            app.reconcile()
            self.assertEqual(app.theses()[0].first_fill_session, old_clock)
        finally:
            app.close()

    def test_new_thesis_uses_configured_holding_limit(self):
        path = self.config_dir / 'strategy.yaml'
        path.write_text(path.read_text().replace('max_holding_sessions: 20', 'max_holding_sessions: 10'))
        app = Application(load_config(self.config_dir), self.bundle)
        try:
            app.review()
            self.assertEqual(app.theses()[0].max_holding_sessions, 10)
        finally:
            app.close()

    def test_reentry_resolution_requires_selected_new_official_evidence(self):
        from danta.strategy import assess_entry, reentry_eligibility
        app = Application(self.config, self.bundle)
        try:
            app.review()
            now = self.bundle.now
            previous = app.theses()[0].model_copy(update={'exit_reason': 'EXIT_THESIS_INVALID',
                'exited_at': now - timedelta(minutes=1), 'invalidating_event_ids': ['negative-official']})
            original = self.bundle.events[0]
            positive = original.model_copy(update={'event_id': 'new-positive', 'available_at': now,
                'observed_at': now, 'published_at': now})
            negative = original.model_copy(update={'event_id': 'negative-official', 'polarity': 'NEGATIVE'})
            candidate = self.bundle.candidates[0].model_copy(update={
                'event_ids': [original.event_id, positive.event_id], 'counterevidence_event_ids': [negative.event_id]})
            frozen = freeze_input(run_id='reentry', config_hash=self.config.config_hash,
                strategy_hash=self.config.strategy_hash, code_id=app.code_id, now=now,
                session_id=self.bundle.calendar.active(now).session_id, profile=app.profile,
                portfolio=app.portfolio(), candidates=[candidate], events=[original, positive, negative],
                facts=self.bundle.facts, theses=[], scope='PARTIAL', reviewed_positions=[], previous_theses=[previous])
            proposal = fixture_decision(frozen)
            review = proposal['candidate_reviews'][0]
            review.update(event_ids=[positive.event_id], resolved_invalidation_event_ids=[negative.event_id],
                resolution_case=previous.invalidation_case + ' 새 공식 원문이 해당 무효화 조건을 해소했음을 확인한 합성 검증입니다.')
            args = dict(current_account_version=app.store.get('account_version'),
                current_facts_hash=digest([frozen['events'], frozen['facts']]), completed_at=now, now=now)
            decision = validate_proposal(proposal, frozen, **args).candidate_reviews[0]
            selected = candidate.model_copy(update={'event_ids': decision.event_ids})
            gate_args = (selected, self.bundle.quotes['TEST:AAA'], [original, positive, negative],
                         self.bundle.calendar, self.bundle.ticks, now, app.profile)
            self.assertIn('NEGATIVE_EVENT_REVIEW_REQUIRED', assess_entry(*gate_args, synthetic=True).reasons)
            self.assertTrue(assess_entry(*gate_args, synthetic=True,
                resolved_event_ids=decision.resolved_invalidation_event_ids).allowed)
            self.assertTrue(reentry_eligibility(previous, [positive], self.bundle.bars['TEST:AAA'],
                self.bundle.calendar, now, app.profile, resolved_event_ids=decision.resolved_invalidation_event_ids).allowed)
            self.assertFalse(reentry_eligibility(previous, [original], self.bundle.bars['TEST:AAA'],
                self.bundle.calendar, now, app.profile, resolved_event_ids=decision.resolved_invalidation_event_ids).allowed)
            for changes in ({'event_ids': [original.event_id]}, {'resolved_invalidation_event_ids': ['foreign-event']},
                            {'resolution_case': 'unsupported'}):
                with self.subTest(changes=changes):
                    bad = {**proposal, 'candidate_reviews': [{**review, **changes}]}
                    with self.assertRaisesRegex(ValueError, 'REENTRY_RESOLUTION_UNVERIFIED'):
                        validate_proposal(bad, frozen, **args)
        finally:
            app.close()

    def test_unsupported_policy_knobs_and_ambiguous_report_destination_fail_at_load(self):
        strategy_path, schedule_path, app_path = [self.config_dir / (name + '.yaml') for name in ('strategy', 'schedules', 'app')]
        original = strategy_path.read_text()
        for before, after in (('trend_exit_consecutive_closes: 2', 'trend_exit_consecutive_closes: 3'),
                              ('overnight: true', 'overnight: false'),
                              ('watchlist: []', 'watchlist: [TEST:AAA]'),
                              ('block_bootstrap_resamples: 2000', 'block_bootstrap_resamples: 100')):
            with self.subTest(setting=before):
                strategy_path.write_text(original.replace(before, after))
                with self.assertRaises(ConfigurationError):
                    load_config(self.config_dir)
        strategy_path.write_text(original)
        original = schedule_path.read_text()
        schedule_path.write_text(original.replace('start_minutes_after_continuous_open: 20', 'start_minutes_after_continuous_open: 40'))
        with self.assertRaisesRegex(ConfigurationError, 'entry window'):
            load_config(self.config_dir)
        schedule_path.write_text(original)
        settings = yaml.safe_load(app_path.read_text())
        settings['telegram'].update(enabled=True, allowed_chat_ids=['one', 'two'])
        for default in (None, 'foreign', 1):
            settings['telegram']['default_chat_id'] = default
            app_path.write_text(yaml.safe_dump(settings))
            with self.assertRaises(ConfigurationError):
                load_config(self.config_dir)
        settings['telegram']['default_chat_id'] = 'two'
        app_path.write_text(yaml.safe_dump(settings))
        self.assertEqual(load_config(self.config_dir).app['telegram']['default_chat_id'], 'two')


    def test_review_failure_keeps_provider_details_in_result_and_notification(self):
        app = Application(self.config, self.bundle)
        try:
            error = HumanRequired('External account observations incomplete: NO_MARGIN_BUYING_POWER_UNVERIFIED')
            error.diagnostics = [{'endpoint':'inquire-psbl-order', 'reason':'TRANSIENT_FAILURE',
                                  'http_status':500, 'provider_code':'EGW00215', 'provider_message':'합성 오류 원문'}]
            with patch.object(app, 'reconcile', side_effect=error), self.assertRaises(HumanRequired):
                app.review(request_key='diagnostic-review')
            result = json.loads(app.store.read("SELECT result FROM requests WHERE request_key='diagnostic-review'")[0][0])
            self.assertEqual(result['diagnostics'], error.diagnostics)
            text = render_notification(result)
            self.assertIn('HTTP 500', text)
            self.assertIn('EGW00215', text)
            self.assertIn('합성 오류 원문', text)
        finally:
            app.close()

    def test_monitor_retains_each_failed_check_after_recovery_without_extra_alerts(self):
        app = Application(self.config, self.bundle)
        try:
            output = io.StringIO()
            errors = [AdapterError('TRANSIENT_FAILURE', diagnostic={'endpoint':'inquire-psbl-order',
                'http_status':code, 'provider_message':'합성 조회 오류'}) for code in (500, 503)]
            with patch.object(app.monitor_stop, 'wait', side_effect=[False, False, True]), \
                 patch.object(app, 'protect', side_effect=errors), redirect_stderr(output):
                app.start_monitor(0.001)
                app.monitor_thread.join(3)
                self.assertFalse(app.monitor_thread.is_alive())
            events = [json.loads(row[0]) for row in app.store.read("SELECT payload FROM journal WHERE kind='MONITOR_DEGRADED'")]
            self.assertEqual([row['diagnostics'][0]['http_status'] for row in events], [500, 503])
            notices = [json.loads(row[0]) for row in app.store.read('SELECT payload FROM outbox')]
            self.assertEqual(sum(row.get('kind') == 'MONITOR_DEGRADED' for row in notices), 1)
            self.assertEqual([json.loads(line)['diagnostics'][0]['http_status'] for line in output.getvalue().splitlines()], [500, 503])
            app.protect()
            app.bundle.now += timedelta(seconds=61)
            app.protect()
            self.assertFalse(app.store.get('monitor_degraded'))
            self.assertIsNone(app.store.get('monitor_diagnostic'))
            self.assertEqual(app.store.read("SELECT COUNT(*) FROM journal WHERE kind='MONITOR_DEGRADED'")[0][0], 2)
        finally:
            app.close()

    def test_review_reports_every_prefilter(self):
        app = Application(self.config,self.bundle)
        try:
            for symbol,quote in app.bundle.quotes.items():
                app.bundle.quotes[symbol] = quote.model_copy(update={'observed_at':app.bundle.now-timedelta(seconds=6)})
            blocked = app.review()
            self.assertEqual(blocked['reason'],'NO_ELIGIBLE_CANDIDATES')
            self.assertTrue(blocked['review_details'])
            self.assertIn('STALE_OR_INVALID_QUOTE',blocked['review_details'][0]['filter_reasons'])
            self.assertEqual(blocked['review_details'][0]['quote_age_seconds'],6)
            self.assertIsNone(blocked['review_details'][0]['ai'])
        finally:
            app.close()

    def test_review_refreshes_after_slow_protection(self):
        app = Application(self.config,self.bundle)
        try:
            calls = []
            def slow_protection(**kwargs):
                app.bundle.now += timedelta(seconds=6)
                return app.bundle
            def fresh_quotes(symbols):
                calls.append(list(symbols))
                for symbol in symbols:
                    app.bundle.quotes[symbol] = app.bundle.quotes[symbol].model_copy(update={
                        'observed_at':app.bundle.now,'received_at':app.bundle.now})
                return app.bundle
            app.protection_refresh,app.quote_refresh = slow_protection,fresh_quotes
            result = app.review()
            self.assertGreaterEqual(len(calls),2)
            self.assertEqual(result['model_status'],'FIXTURE_RECORDED_RESPONSE')
            row = result['review_details'][0]
            self.assertEqual(row['stage'],'AI_REVIEWED')
            self.assertTrue(row['ai'])
            self.assertEqual(row['quote_age_seconds'],0)
            self.assertTrue(row['evidence'])
        finally:
            app.close()

    def test_review_removes_exclusion_recovered_during_candidate_quote_refresh(self):
        app = Application(self.config, self.bundle)
        try:
            symbol = "TEST:AAA"
            quote = app.bundle.quotes[symbol]
            app.bundle.quotes[symbol] = quote.model_copy(update={"observed_at": app.bundle.now - timedelta(seconds=6)})
            app.bundle.exclusions = [{"instrument_id": symbol, "reason": "STREAM_NOT_READY"}]
            app.bundle.data['runtime_diagnostics'] = [{'scope':'ENTRY', 'instrument_id':symbol, 'reason':'STREAM_NOT_READY'}]
            calls = []
            def refresh_quotes(symbols):
                calls.append(symbols)
                if len(calls) > 1:
                    app.bundle.quotes[symbol] = quote
                    app.bundle.exclusions = []
                    app.bundle.data['runtime_diagnostics'] = []
                return app.bundle
            app.quote_refresh = refresh_quotes
            result = app.review()
            self.assertEqual(result["feature_exclusions"], [])
            candidates = json.loads(next(self.config.state_dir.glob("runs/*/*/candidates.json")).read_text())["data"]
            self.assertEqual(candidates["feature_exclusions"], [])
            self.assertEqual(result["order_status"], "FIXTURE_FILLED")
            self.assertEqual(result['review_details'][0]['filter_reasons'], [])
        finally:
            app.close()

    def test_failed_review_reports_an_already_persisted_order(self):
        app = Application(self.config,self.bundle)
        submit = app.executor.submit
        def fail_after_submit(*args, **kwargs):
            submit(*args, **kwargs)
            raise RuntimeError('fixture after accepted order')
        try:
            with patch.object(app.executor, 'submit', side_effect=fail_after_submit):
                with self.assertRaisesRegex(RuntimeError, 'fixture after accepted order'):
                    app.review()
            result = json.loads(next(self.config.state_dir.glob('runs/*/*/result.json')).read_text())
            self.assertEqual(result['run_status'],'FAILED')
            self.assertEqual(result['order_status'],'ACKNOWLEDGED')
            self.assertTrue(any(row['orders'] for row in result['review_details']))
            self.assertTrue(list(self.config.state_dir.glob('runs/*/*/execution.json')))
        finally:
            app.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_dir = self.root / "config"
        shutil.copytree(ROOT / "tests/fixtures/config", self.config_dir, ignore=shutil.ignore_patterns("secrets.yaml"))
        app = self.config_dir / "app.yaml"
        app.write_text(app.read_text().replace("state_dir: ./var/offline/research", f"state_dir: {self.root / 'state'}"))
        self.config = load_config(self.config_dir)
        self.data = json.loads((ROOT / "tests/fixtures/offline-e2e.json").read_text())
        self.bundle = MarketBundle(self.data, self.config.research, mode="offline")
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.tmp.cleanup()

    def store(self, *, name="db", identity="test-account"):
        store = Store(self.root / name / "state.sqlite", mode="offline", account_identity=self.tmp.name + identity, initial_cash=D("10000000"))
        self.stores.append(store)
        return store

    def executor(self, broker=None):
        store, broker = self.store(), broker or FixtureBroker()
        return Executor(store, broker, mode="offline", authorize=lambda *_: None, preflight=lambda *_: None)

    def intent(self, executor, *, side="BUY", quantity=10, plan="plan-1"):
        return OrderIntent(run_id="test-run", plan_id=plan, thesis_id="thesis-1", instrument_id="TEST:AAA", side=side,
            quantity=quantity, limit_price=D("100") if side == "BUY" else None,
            expires_at=self.bundle.now + timedelta(seconds=120) if side == "BUY" else None,
            reason="TEST", account_version=executor.store.get("account_version"), policy_hash="synthetic-policy",
            reserve_cash=D(quantity * 100) if side == "BUY" else D(0), reserve_risk=D(quantity * 10) if side == "BUY" else D(0))

    def test_status_preserves_legacy_model_success_only_for_its_recorded_purpose(self):
        app = Application(self.config, self.bundle)
        try:
            health = {'purpose': 'chat', 'status': 'SUCCESS', 'checked_at': '2026-09-21T10:00:00Z'}
            app.store.set('model_health', health)
            status = app.status()
            self.assertEqual(status['chat_model'], health)
            self.assertEqual(status['review_model'], {})
            app.store.set('model_health:chat', {**health, 'status': 'PROCESS_FAILED'})
            self.assertEqual(app.status()['chat_model']['status'], 'PROCESS_FAILED')
        finally:
            app.close()

    def test_successful_reconciliation_clears_previous_account_failure(self):
        ex = self.executor()
        ex.reconcile({'complete': False, 'diagnostics': [{'endpoint': 'balance', 'reason': 'TRANSIENT_FAILURE', 'http_status': 503}]})
        self.assertFalse(ex.store.get('reconciled'))
        self.assertTrue(ex.store.get('account_diagnostics'))
        ex.reconcile({'complete': True, 'ownership_complete': True, 'orders': []})
        self.assertTrue(ex.store.get('reconciled'))
        self.assertEqual(ex.store.get('account_diagnostics'), [])
        self.assertIsNotNone(ex.store.get('account_succeeded_at'))

    def test_review_progress_reaches_model_and_validation_stages(self):
        app = Application(self.config, self.bundle)
        progress = []
        def model(frozen, *, on_progress=None):
            self.assertIsNotNone(on_progress)
            on_progress('공개 모델 진행 안내')
            return fixture_decision(frozen)
        from danta.application import fixture_decision
        app.decide = model
        try:
            result = app.review(on_progress=progress.append)
            self.assertEqual(result['run_status'], 'COMPLETE')
            self.assertIn('공개 모델 진행 안내', progress)
            self.assertIn('대조', progress[-1])
        finally:
            app.close()

    def test_monitor_expires_working_entries_before_a_protection_failure(self):
        app = Application(self.config, self.bundle)
        calls = []
        def fail_protection():
            calls.append('protect')
            app.monitor_stop.set()
            raise HumanRequired('synthetic protection refresh failed')
        try:
            with patch.object(app.store, 'working', return_value=[{}]), \
                 patch.object(app, 'reconcile', side_effect=lambda: calls.append('reconcile')), \
                 patch.object(app.executor, 'expire_entries', side_effect=lambda now: calls.append('expire')), \
                 patch.object(app, 'protect', side_effect=fail_protection):
                app.start_monitor(interval=0.001)
                app.monitor_thread.join(2)
            self.assertEqual(calls, ['reconcile', 'expire', 'protect'])
        finally:
            app.close()

    def test_review_progress_preserves_one_argument_deciders(self):
        from danta.application import fixture_decision
        app = Application(self.config, self.bundle, decide=lambda frozen: fixture_decision(frozen))
        try:
            self.assertEqual(app.review(on_progress=lambda text: None)['run_status'], 'COMPLETE')
        finally:
            app.close()

    def test_failed_ownership_check_has_fresh_time_without_changing_last_success(self):
        ex = self.executor()
        ex.reconcile({'complete': True, 'ownership_complete': True, 'orders': []})
        success = ex.store.get('account_succeeded_at')
        with self.assertRaises(HumanRequired):
            ex.reconcile({'complete': True, 'ownership_complete': False, 'orders': []})
        self.assertGreater(ex.store.get('account_checked_at'), success)
        self.assertEqual(ex.store.get('account_succeeded_at'), success)
        self.assertEqual(ex.store.get('account_diagnostics'), ['OWNERSHIP_RECONCILIATION_REQUIRED'])

    def test_O01_I01_offline_end_to_end_without_network(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            app = Application(self.config, self.bundle)
            try:
                result = app.review(request_key="full-review-1")
                self.assertEqual(result["run_status"], "COMPLETE")
                self.assertEqual(result["order_status"], "FIXTURE_FILLED")
                self.assertEqual(result["provenance"], "FIXTURE_ONLY")
                self.assertEqual(result["performance_status"], "STRATEGY_UNPROVEN")
                self.assertGreater(app.store.quantity("TEST:AAA"), 0)
                held = app.store.quantity("TEST:AAA")
                document_rows = app.store.db.execute("SELECT payload FROM outbox WHERE event_key=?",
                    ("report:" + result["run_id"],)).fetchall()
                self.assertEqual(len(document_rows), 1)
                document = json.loads(document_rows[0][0])["document"]
                date = self.bundle.now.date().isoformat()
                summary = self.config.state_dir / "runs" / date / result["run_id"] / "summary.html"
                self.assertEqual(document["filename"], f"summary-{date}-{result['run_id']}.html")
                self.assertEqual(document["content"].encode(), summary.read_bytes())
                self.assertIn("FIXTURE_ONLY", document["content"])
                repeat = app.review(request_key="full-review-1")
                self.assertEqual(repeat["run_id"], result["run_id"])
                self.assertEqual(app.store.db.execute("SELECT COUNT(*) FROM outbox WHERE event_key=?",
                    ("report:" + result["run_id"],)).fetchone()[0], 1)
                app.review(request_key="full-review-2")
                self.assertEqual(app.store.quantity("TEST:AAA"), held)
                self.assertEqual(app.broker.submissions, 1)
                self.assertEqual(len(app.store.holdings()), 1)
            finally:
                app.close()

    def test_O02_strict_config_rejects_duplicate_unknown_bool_and_ratio(self):
        app_file = self.config_dir / "app.yaml"
        original = app_file.read_text()
        for bad in [original + "schema_version: 1\n", original + "unknown: value\n", original.replace("enabled: false", 'enabled: "false"', 1)]:
            app_file.write_text(bad)
            with self.assertRaises(ConfigurationError):
                load_config(self.config_dir)
        app_file.write_text(original)
        strategy_file = self.config_dir / "strategy.yaml"
        strategy_file.write_text(strategy_file.read_text().replace('entry_risk_fraction: "0.0025"', 'entry_risk_fraction: "1.01"'))
        with self.assertRaises(ConfigurationError):
            load_config(self.config_dir)

    def test_startup_activation_never_records_success_after_failed_reconciliation(self):
        app = Application(self.config, self.bundle)
        app.approval = {'id': 'synthetic-startup-approval'}
        try:
            with patch('danta.application.validate_activation') as validation:
                with patch.object(app, 'reconcile', side_effect=HumanRequired('synthetic broker mismatch')):
                    with self.assertRaises(HumanRequired):
                        app.activate(self.config.config_hash)
                self.assertIsNone(app.store.get('activation'))
                app.activate(self.config.config_hash)
                app.activate(self.config.config_hash)
                self.assertEqual(app.store.get('activation'), {'config_hash': self.config.config_hash,
                    'code_id': app.code_id, 'approval_id': app.approval['id']})
                self.assertEqual(app.store.db.execute("SELECT COUNT(*) FROM journal WHERE kind='ACTIVATED'").fetchone()[0], 1)
                self.assertEqual(validation.call_count, 3)
                with app.store.transaction():
                    app.store.set('cash_krw', '0')
                with self.assertRaisesRegex(HumanRequired, 'ACCOUNT_ALLOCATION_EMPTY'):
                    app.activate(self.config.config_hash)
            # Actual validation still rejects the offline profile even with an existing record.
            with self.assertRaises(HumanRequired):
                app.activate(self.config.config_hash)
        finally:
            app.close()

    def test_final_dispatch_rechecks_pause_account_cash_and_quote(self):
        for change, reason in [("pause", "NEW_RISK_PAUSED"), ("version", "STALE_ACCOUNT_VERSION"),
                               ("cash", "CURRENT_PORTFOLIO_LIMIT"), ("quote", "MONITOR_DEGRADED")]:
            with self.subTest(change=change):
                app = Application(self.config, MarketBundle(json.loads(canonical(self.data)), self.config.research, mode="offline"))
                try:
                    original, calls = app.executor.authorize, []
                    def authorize(intent, operation):
                        calls.append(operation)
                        if len(calls) == 2:
                            if change == "pause":
                                app.pause()
                            elif change == "version":
                                with app.store.transaction():
                                    app.store.bump_version()
                            elif change == "cash":
                                app.bundle.data["broker_available_cash"] = "0"
                            else:
                                app.bundle.now += timedelta(seconds=app.profile["orders"]["quote_max_age_seconds"] + 1)
                        original(intent, operation)
                    app.executor.authorize = authorize
                    with self.assertRaisesRegex(ValueError, reason):
                        app.review()
                    self.assertEqual(app.broker.submissions, 0)
                    self.assertEqual(app.store.reservation(), 0)
                    self.assertEqual(app.store.db.execute("SELECT state FROM intents").fetchone()[0], "INVALIDATED")
                finally:
                    app.close()
                    shutil.rmtree(self.config.state_dir)

    def test_protection_retries_known_rejection_with_new_revision_and_fast_refresh(self):
        app = Application(self.config, self.bundle)
        try:
            app.review()
            held = app.store.quantity("TEST:AAA")
            quote = app.bundle.quotes["TEST:AAA"]
            app.bundle.quotes["TEST:AAA"] = quote.model_copy(update={"bid": app.theses()[0].current_stop - 1})
            app.refresh = lambda: self.fail("Protection must not wait for DART/full collection")
            fast_calls = []
            def refresh_protection():
                fast_calls.append(True)
                return app.bundle
            app.protection_refresh = refresh_protection
            original, attempts = app.broker.submit, []
            def submit(intent):
                attempts.append(intent)
                return {"status": "REJECTED"} if len(attempts) == 1 else original(intent)
            app.broker.submit = submit
            app.protect()
            app.protect()
            app.protect()
            self.assertEqual([item["plan_revision"] for item in attempts], [1, 2])
            self.assertEqual([item["quantity"] for item in attempts], [held, held])
            self.assertGreaterEqual(len(fast_calls), 5)  # monitor plus SELL preflight
            pending = app.store.working("TEST:AAA")
            self.assertEqual(len(pending), 1)
            app.broker.fill(pending[0]["broker_id"], held, app.bundle.quotes["TEST:AAA"].bid, D(1), app.bundle.now)
            app.reconcile()
            self.assertEqual(app.store.quantity("TEST:AAA"), 0)
        finally:
            app.close()

    def test_protection_never_retries_unknown_submission(self):
        app = Application(self.config, self.bundle)
        try:
            app.review()
            app.bundle.quotes["TEST:AAA"] = app.bundle.quotes["TEST:AAA"].model_copy(update={"bid": app.theses()[0].current_stop - 1})
            with patch.object(app.broker, "submit", return_value={"status": "UNKNOWN"}) as submit:
                app.protect()
                app.protect()
                self.assertEqual(submit.call_count, 1)
                self.assertEqual(app.store.working("TEST:AAA")[0]["state"], "UNKNOWN")
        finally:
            app.close()

    def test_slow_entry_preflight_does_not_block_reconciliation_or_pause(self):
        ex = self.executor()
        intent = self.intent(ex)
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def preflight(*_):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test preflight was not released")
        ex.preflight = preflight
        def submit():
            try:
                ex.submit(intent, self.bundle.now)
            except Exception as error:
                errors.append(str(error))
        def protect():
            ex.reconcile()
            with ex.store.transaction():
                ex.store.set("paused", True)
                ex.store.bump_version()
            done.set()
        sender = threading.Thread(target=submit)
        monitor = threading.Thread(target=protect)
        try:
            sender.start()
            self.assertTrue(entered.wait(2))
            monitor.start()
            self.assertTrue(done.wait(2), "Protection/reconciliation waited for entry collection")
        finally:
            release.set()
            sender.join(5)
            if monitor.ident:
                monitor.join(5)
        self.assertFalse(sender.is_alive() or monitor.is_alive())
        self.assertEqual(errors, ["NEW_RISK_PAUSED"])
        self.assertEqual(ex.broker.submissions, 0)
        self.assertEqual(ex.store.reservation(), 0)

    def test_review_summary_uses_persisted_rejected_unknown_and_not_sent_states(self):
        for response, state in [("REJECTED", "REJECTED"), ("UNKNOWN", "UNKNOWN"), ("NOT_SENT", "INVALIDATED")]:
            with self.subTest(response=response):
                app = Application(self.config, self.bundle)
                try:
                    with patch.object(app.broker, "submit", return_value={"status": response}):
                        result = app.review()
                    self.assertEqual(result["order_status"], state)
                    self.assertEqual(result["order_states"], {state: 1})
                    self.assertEqual(app.store.quantity("TEST:AAA"), 0)
                    self.assertEqual(app.store.db.execute("SELECT state FROM intents").fetchone()[0], state)
                    report = next(self.config.state_dir.glob("runs/*/*/summary.json"))
                    self.assertEqual(json.loads(report.read_text())["order_status"], state)
                finally:
                    app.close()
                    shutil.rmtree(self.config.state_dir)

    def test_new_review_can_enter_after_unfilled_failure_without_replaying_same_request(self):
        for failure in ("REJECTED", "NOT_SENT", "CANCELED", "PREFLIGHT_FAILURE"):
            with self.subTest(failure=failure):
                app = Application(self.config, self.bundle)
                try:
                    if failure == "PREFLIGHT_FAILURE":
                        with patch.object(app.executor, "preflight", side_effect=ValueError("TEMPORARY_PREFLIGHT")):
                            with self.assertRaisesRegex(ValueError, "TEMPORARY_PREFLIGHT"):
                                app.review(request_key="first-review")
                    elif failure == "CANCELED":
                        with patch.object(app.broker, "fill"):
                            app.review(request_key="first-review")
                        app.executor.cancel(app.store.working()[0]["id"], app.bundle.now)
                        app.reconcile()
                    else:
                        with patch.object(app.broker, "submit", return_value={"status": failure}):
                            app.review(request_key="first-review")
                    self.assertEqual(app.store.quantity("TEST:AAA"), 0)
                    self.assertEqual(len(app.theses()), 1)
                    self.assertIsNone(app.theses()[0].protection_started_at)
                    before = app.broker.submissions
                    app.review(request_key="first-review")
                    self.assertEqual(app.broker.submissions, before)
                    result = app.review(request_key="new-review")
                    self.assertEqual(result["order_status"], "FIXTURE_FILLED")
                    self.assertEqual(app.broker.submissions, before + 1)
                    self.assertGreater(app.store.quantity("TEST:AAA"), 0)
                    # The abandoned thesis stays auditable, but must not duplicate
                    # the actual holding's review or enable an additional buy.
                    held = app.review(request_key="held-review")
                    self.assertEqual(held["decision_status"], "VALID")
                    self.assertEqual(app.broker.submissions, before + 1)
                    frozen = json.loads((self.config.state_dir / "runs" / app.bundle.now.date().isoformat() /
                                         held["run_id"] / "input.snapshot.json").read_text())["data"]
                    self.assertEqual(len(frozen["theses"]), 1)
                    self.assertIsNotNone(frozen["theses"][0]["first_fill_at"])
                finally:
                    app.close()
                    shutil.rmtree(self.config.state_dir)

    def test_new_review_keeps_working_unknown_and_filled_entry_restrictions(self):
        for state in ("ACKNOWLEDGED", "UNKNOWN", "PARTIAL_CANCELED", "EXIT_PROTECTION"):
            with self.subTest(state=state):
                app = Application(self.config, self.bundle)
                try:
                    if state == "UNKNOWN":
                        with patch.object(app.broker, "submit", return_value={"status": "UNKNOWN"}):
                            app.review()
                        with patch.object(app.broker, "submit") as submit, self.assertRaises(HumanRequired):
                            app.review()
                        submit.assert_not_called()
                        continue
                    with patch.object(app.broker, "fill"):
                        app.review()
                    order = app.store.working()[0]
                    if state != "ACKNOWLEDGED":
                        app.broker.fill(order["broker_id"], 1, D(order["limit_price"]), D(0), app.bundle.now)
                        app.reconcile()
                        app.executor.cancel(order["id"], app.bundle.now)
                        app.reconcile()
                    if state == "EXIT_PROTECTION":
                        quote = app.bundle.quotes["TEST:AAA"]
                        app.bundle.quotes["TEST:AAA"] = quote.model_copy(update={"bid": app.theses()[0].current_stop - 1})
                        app.protect()
                        exit_order = app.store.working()[0]
                        app.broker.fill(exit_order["broker_id"], 1, app.bundle.quotes["TEST:AAA"].bid, D(0), app.bundle.now)
                        app.reconcile()
                        app.bundle.quotes["TEST:AAA"] = quote
                    before = app.broker.submissions
                    result = app.review()
                    self.assertEqual(app.broker.submissions, before)
                    if state == "EXIT_PROTECTION":
                        self.assertEqual(result["reason"], "REENTRY_COMPLETED_SESSION_REQUIRED")
                finally:
                    app.close()
                    shutil.rmtree(self.config.state_dir)

    def test_unsubmitted_invalidation_changes_version_only_when_it_changes_orders(self):
        ex = self.executor()
        ex.reconcile()
        version = ex.store.get("account_version")
        ex.invalidate_unsubmitted_entries("protection", "EXIT_OVERDUE")
        self.assertEqual(ex.store.get("account_version"), version)
        order = ex.submit(self.intent(ex), self.bundle.now)
        version = ex.store.get("account_version")
        ex.invalidate_unsubmitted_entries("protection", "EXIT_OVERDUE")
        self.assertEqual(ex.store.get("account_version"), version)
        self.assertEqual(ex.store.order(order["id"])["state"], "ACKNOWLEDGED")
        ex.store.db.execute("UPDATE intents SET state='VALIDATED' WHERE id=?", (order["id"],))
        ex.invalidate_unsubmitted_entries("protection", "EXIT_OVERDUE")
        self.assertEqual(ex.store.get("account_version"), version + 1)
        self.assertEqual(ex.store.order(order["id"])["state"], "INVALIDATED")
        self.assertEqual(ex.store.reservation(), 0)
        ex.invalidate_unsubmitted_entries("protection", "EXIT_OVERDUE")
        self.assertEqual(ex.store.get("account_version"), version + 1)
        self.assertEqual(ex.store.read("SELECT COUNT(*) FROM journal WHERE kind='UNSUBMITTED_ENTRIES_INVALIDATED'")[0][0], 1)

    def test_portfolio_matches_only_reservations_in_the_same_complete_broker_snapshot(self):
        app = Application(self.config, self.bundle)
        try:
            with patch.object(app.broker, "fill"):
                app.review()
            order = app.store.working()[0]
            account = json.loads(canonical(app.broker.snapshot()))
            app.bundle.data["account_snapshot"] = account
            self.assertEqual(app.portfolio().broker_reflected_reserve_cash, 0)
            account["broker_cash_reserves_orders"] = True
            reserved = D(order["reserve_cash"])
            principal = order["quantity"] * D(order["limit_price"])
            self.assertEqual(app.portfolio().broker_reflected_reserve_cash, principal)
            self.assertEqual(app.portfolio(exclude_intent_id=order["id"]).broker_reflected_reserve_cash, 0)
            app.store.db.execute("UPDATE intents SET reserve_cash=? WHERE id=?", (str(reserved + D(100)), order["id"]))
            self.assertEqual(app.portfolio().broker_reflected_reserve_cash, principal)
            self.assertEqual(app.portfolio().pending_entries[0].reserved_cash, reserved + D(100))
            app.store.db.execute("UPDATE intents SET reserve_cash=? WHERE id=?", (str(reserved), order["id"]))
            for update in ({"complete": False}, {"ownership_complete": False}, {"orders": []},
                           {"orders": account["orders"] * 2},
                           {"orders": [{**account["orders"][0], "namespace": "different-account"}]},
                           {"orders": [{**account["orders"][0], "state": "REJECTED"}]}):
                app.bundle.data["account_snapshot"] = {**account, **update}
                self.assertEqual(app.portfolio().broker_reflected_reserve_cash, 0)
            app.bundle.data["account_snapshot"] = account
            for state in ("PLANNED", "VALIDATED", "SUBMITTING", "UNKNOWN"):
                app.store.db.execute("UPDATE intents SET state=? WHERE id=?", (state, order["id"]))
                self.assertEqual(app.portfolio().broker_reflected_reserve_cash, 0)
            app.store.db.execute("UPDATE intents SET state='ACKNOWLEDGED' WHERE id=?", (order["id"],))
            app.store.set("reconciled", False)
            self.assertEqual(app.portfolio().broker_reflected_reserve_cash, 0)
            app.reconcile()
            # A subsequent partial fill cannot be matched to the earlier quantity.
            app.broker.fill(order["broker_id"], 1, D(order["limit_price"]), D(0), app.bundle.now)
            app.reconcile()
            self.assertEqual(app.portfolio().broker_reflected_reserve_cash, 0)
            account = {**json.loads(canonical(app.broker.snapshot())), "broker_cash_reserves_orders": True}
            app.bundle.data["account_snapshot"] = account
            remaining = D(app.store.order(order["id"])["reserve_cash"])
            self.assertLess(remaining, reserved)
            remaining_principal = (order["quantity"] - 1) * D(order["limit_price"])
            self.assertEqual(app.portfolio().broker_reflected_reserve_cash, remaining_principal)
            app.executor.cancel(order["id"], app.bundle.now)
            self.assertEqual(app.portfolio().broker_reflected_reserve_cash, remaining_principal)
            app.reconcile()
            self.assertEqual(app.portfolio().broker_reflected_reserve_cash, 0)
            self.assertEqual(app.portfolio().pending_entries, [])
        finally:
            app.close()

    def test_concurrent_preflights_reserve_and_submit_same_intent_once(self):
        ex = self.executor()
        intent = self.intent(ex)
        gate = threading.Barrier(2)
        ex.preflight = lambda *_: gate.wait(timeout=3)
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(ex.submit, intent, self.bundle.now) for _ in range(2)]
            orders = [future.result(timeout=5) for future in futures]
        self.assertEqual([order["id"] for order in orders], [intent.id, intent.id])
        self.assertEqual(ex.broker.submissions, 1)
        self.assertEqual(ex.store.reservation(), intent.reserve_cash)

    def test_monitor_tolerates_one_minute_but_dispatch_still_requires_fresh_quotes(self):
        path = self.config_dir / 'strategy.yaml'
        settings = yaml.safe_load(path.read_text())
        settings['strategy']['research_profile']['orders']['monitor_quote_max_age_seconds'] = 60
        path.write_text(yaml.safe_dump(settings))
        config = load_config(self.config_dir)
        app = Application(config, MarketBundle(self.data, config.research, mode='offline'))
        try:
            app.review()
            original = app.bundle.now
            for age in (6, 30, 60):
                with self.subTest(age=age):
                    app.bundle.now = original + timedelta(seconds=age)
                    self.assertTrue(app.portfolio().complete)
                    self.assertEqual(app.protect()[0]['action'], 'KEEP_QUANTITY')
                    if age == 30:
                        def strict_quotes(symbols):
                            for symbol in symbols:
                                app.bundle.quotes.pop(symbol, None)
                            return app.bundle
                        with patch.object(app, 'quote_refresh', side_effect=strict_quotes):
                            app.review()
                        self.assertTrue(app.portfolio().complete)
                    for side in ('BUY', 'SELL'):
                        with self.assertRaisesRegex(ValueError, 'MONITOR_DEGRADED'):
                            app._validate_order(self.intent(app.executor, side=side), app.bundle.now)
            self.assertFalse(app.store.get('monitor_degraded', False))
            app.bundle.now = original + timedelta(seconds=61)
            self.assertFalse(app.portfolio().complete)
            self.assertEqual(app.protect()[0]['action'], 'MONITOR_DEGRADED')
            # Recovery still needs 60 seconds of valid observations, independent of quote age.
            for elapsed in (0, 59, 60):
                app.bundle.now = original + timedelta(seconds=62 + elapsed)
                app.bundle.quotes = {symbol: quote.model_copy(update={
                    'observed_at':app.bundle.now, 'received_at':app.bundle.now})
                    for symbol,quote in app.bundle.quotes.items()}
                app.protect()
                self.assertEqual(app.store.get('monitor_degraded'), elapsed < 60)
            self.assertEqual(app.store.db.execute("SELECT COUNT(*) FROM journal WHERE kind='MONITOR_RECOVERED'").fetchone()[0], 1)
        finally:
            app.close()

    def test_monitor_quote_config_preserves_legacy_limit_and_validates_new_value(self):
        self.assertEqual(self.config.research['orders']['monitor_quote_max_age_seconds'], 5)
        path = self.config_dir / 'strategy.yaml'
        original = path.read_text()
        for value in ('60', '0', 'true'):
            path.write_text(original.replace('      quote_max_age_seconds: 5',
                '      quote_max_age_seconds: 5\n      monitor_quote_max_age_seconds: ' + value))
            if value == '60':
                config = load_config(self.config_dir)
                self.assertEqual(config.research['orders']['monitor_quote_max_age_seconds'], 60)
                self.assertEqual(config.research['orders']['quote_max_age_seconds'], 5)
            else:
                with self.assertRaises(ConfigurationError):
                    load_config(self.config_dir)

    def test_stale_and_missing_holding_quote_never_certify_nav_or_disable_clock(self):
        app = Application(self.config, self.bundle)
        try:
            app.review(request_key="buy-before-missing-data")
            symbol = "TEST:AAA"
            app.bundle.now += timedelta(seconds=60)
            self.assertFalse(app.portfolio().complete)
            self.assertEqual(app.record_nav()["coverage"], "INSUFFICIENT_COVERAGE")
            self.assertEqual(app.protect()[0]["action"], "MONITOR_DEGRADED")
            del app.bundle.quotes[symbol]
            self.assertEqual(app.portfolio().holdings[0].valuation_quality, "MISSING")
            self.assertEqual(app.protect()[0]["action"], "MONITOR_DEGRADED")
        finally:
            app.close()

    def test_future_received_quote_cannot_pass_dispatch_preflight(self):
        app = Application(self.config, self.bundle)
        try:
            self.bundle.quotes["TEST:AAA"] = self.bundle.quotes["TEST:AAA"].model_copy(update={"received_at": self.bundle.now + timedelta(seconds=1)})
            with self.assertRaisesRegex(ValueError, "MONITOR_DEGRADED"):
                app._preflight(self.intent(app.executor), self.bundle.now)
            self.assertEqual(app.broker.submissions, 0)
        finally:
            app.close()

    def test_O03_I04_mode_flip_is_not_authority(self):
        app_file = self.config_dir / "app.yaml"
        app_file.write_text(app_file.read_text().replace("mode: offline", "mode: live"))
        live = load_config(self.config_dir)
        broker = FixtureBroker()
        with self.assertRaises(HumanRequired):
            Application(live, self.bundle, broker=broker)
        self.assertEqual(broker.submissions, 0)
        self.assertIsNone(live.data["strategy"]["strategy"]["live_mandate"]["capital_krw"])

    def test_O04_policy_frozen_and_changed_config_rejected(self):
        original = self.config.config_hash
        self.config.app["execution"]["enabled"] = True
        self.assertFalse(self.config.app["execution"]["enabled"])
        app_file = self.config_dir / "app.yaml"
        app_file.write_text(app_file.read_text().replace("timeout_seconds: 180", "timeout_seconds: 181"))
        with self.assertRaises(HumanRequired):
            self.config.assert_current()
        self.assertEqual(self.config.config_hash, original)

    def test_O05_request_dedup_different_payload_rejected(self):
        store = self.store()
        first = store.accept_request("trusted:route:100", {"text": "/review"})
        repeat = store.accept_request("trusted:route:100", {"text": "/review"})
        self.assertEqual(first[0], repeat[0])
        self.assertFalse(repeat[1])
        with self.assertRaises(ValueError):
            store.accept_request("trusted:route:100", {"text": "/pause"})

    def test_O06_second_profile_cannot_be_second_writer(self):
        self.store(name="profile-a")
        with self.assertRaises(HumanRequired):
            self.store(name="profile-b")

    def test_O07_O08_fixed_target_and_reserved_idempotency(self):
        self.assertEqual(needed_quantity(10, 3, 15), 2)
        ex = self.executor()
        intent = self.intent(ex)
        first = ex.submit(intent, self.bundle.now)
        repeat = ex.submit(intent, self.bundle.now)
        self.assertEqual(first["id"], repeat["id"])
        self.assertEqual(ex.broker.submissions, 1)
        self.assertEqual(ex.store.reservation(), D(1000))

    def test_O10_O11_timeout_is_unknown_and_never_resent(self):
        class LostResponse(FixtureBroker):
            def submit(self, intent):
                super().submit(intent)
                raise TimeoutError("synthetic lost response")
        ex = self.executor(LostResponse())
        intent = self.intent(ex)
        self.assertEqual(ex.submit(intent, self.bundle.now)["state"], "UNKNOWN")
        self.assertEqual(ex.submit(intent, self.bundle.now)["state"], "UNKNOWN")
        self.assertEqual(ex.broker.submissions, 1)
        with self.assertRaises(HumanRequired):
            ex.reconcile()

    def test_O12_O13_cumulative_partial_cancel_race_and_correction(self):
        ex = self.executor()
        order = ex.submit(self.intent(ex), self.bundle.now)
        ex.broker.fill(order["broker_id"], 3, D(99), D(1), self.bundle.now)
        ex.reconcile()
        self.assertEqual(ex.store.quantity("TEST:AAA"), 3)
        ex.reconcile()
        self.assertEqual(ex.store.quantity("TEST:AAA"), 3)
        ex.cancel(order["id"], self.bundle.now)
        self.assertEqual(ex.store.order(order["id"])["state"], "CANCEL_REQUESTED")
        ex.broker.fill(order["broker_id"], 5, D(99), D(2), self.bundle.now)
        ex.broker.orders[order["broker_id"]]["state"] = "CANCELED"
        ex.reconcile()
        self.assertEqual(ex.store.quantity("TEST:AAA"), 5)
        self.assertEqual(ex.store.order(order["id"])["state"], "PARTIAL_CANCELED")
        self.assertEqual(ex.store.reservation(), 0)
        before = D(ex.store.get("cash_krw"))
        self.assertFalse(ex.store.apply_cumulative_fill(order["id"], quantity=3, notional=D(297), fees=D(1), revision=1, observed_at=self.bundle.now.isoformat()))
        ex.store.apply_cumulative_fill(order["id"], quantity=5, notional=D(490), fees=D(2), revision=100, observed_at=self.bundle.now.isoformat(), correction=True)
        self.assertEqual(D(ex.store.get("cash_krw")), before + 5)

    def test_O14_O15_incomplete_account_and_external_ownership(self):
        ex = self.executor()
        self.assertEqual(ex.reconcile({"complete": False})["status"], "DATA_INCOMPLETE")
        with self.assertRaises(HumanRequired):
            ex.submit(self.intent(ex), self.bundle.now)
        with ex.store.transaction():
            ex.store.set("reconciled", True)
            ex.store.db.execute("INSERT INTO holdings VALUES ('TEST:AAA','manual','external',100,'10000')")
        with self.assertRaises(ValueError):
            ex.submit(self.intent(ex, side="SELL", quantity=1), self.bundle.now)

    def test_cancel_rejection_and_lost_response_can_reconcile_and_retry(self):
        from unittest.mock import patch
        ex = self.executor()
        order = ex.submit(self.intent(ex), self.bundle.now)
        with patch.object(ex.broker, "cancel", return_value={"status":"REJECTED"}) as cancel:
            ex.cancel(order["id"], self.bundle.now)
            self.assertEqual(ex.store.order(order["id"])["state"], "ACKNOWLEDGED")
            ex.cancel(order["id"], self.bundle.now)
            self.assertEqual(cancel.call_count, 2)
        with patch.object(ex.broker, "cancel", side_effect=TimeoutError):
            ex.cancel(order["id"], self.bundle.now)
        self.assertEqual(ex.store.order(order["id"])["state"], "UNKNOWN")
        self.assertFalse(ex.store.get("reconciled"))
        ex.reconcile()
        self.assertEqual(ex.store.order(order["id"])["state"], "ACKNOWLEDGED")

    def test_O17_expiry_releases_only_after_cancellation_reconciliation(self):
        ex = self.executor()
        order = ex.submit(self.intent(ex), self.bundle.now)
        ex.broker.fill(order["broker_id"], 2, D(100), D(0), self.bundle.now)
        ex.reconcile()
        ex.expire_entries(self.bundle.now + timedelta(seconds=121))
        self.assertGreater(ex.store.reservation(), 0)
        ex.reconcile()
        self.assertEqual(ex.store.quantity("TEST:AAA"), 2)
        self.assertEqual(ex.store.reservation(), 0)
        self.assertEqual(ex.broker.submissions, 1)

    def test_O09_O22_protection_executes_while_model_waits(self):
        app = Application(self.config, self.bundle)
        try:
            app.review()
            thesis = app.theses()[0]
            entered, release = threading.Event(), threading.Event()
            def blocked_model(frozen):
                entered.set()
                self.assertTrue(release.wait(3))
                return fixture_decision(frozen)
            app.decide = blocked_model
            errors = []
            def review():
                try:
                    app.review()
                except ValueError as error:
                    errors.append(str(error))
            thread = threading.Thread(target=review)
            thread.start()
            self.assertTrue(entered.wait(3))
            quote = app.bundle.quotes[thesis.instrument_id]
            app.bundle.quotes[thesis.instrument_id] = quote.model_copy(update={"bid": thesis.current_stop - D(1), "ask": thesis.current_stop})
            plans = app.protect()
            self.assertTrue(any(item["action"] == "EXIT_PROTECTION" for item in plans))
            self.assertEqual(app.broker.submissions, 2)
            release.set()
            thread.join(3)
            self.assertTrue(any("STALE_ACCOUNT_VERSION" in item for item in errors))
        finally:
            app.close()

    def test_O26_outbox_is_not_a_trade_retry(self):
        ex = self.executor()
        ex.submit(self.intent(ex), self.bundle.now)
        with ex.store.transaction():
            ex.store.db.execute("UPDATE outbox SET attempts=attempts+1")
            ex.store.db.execute("UPDATE outbox SET attempts=attempts+1")
        self.assertEqual(ex.broker.submissions, 1)
        self.assertGreater(ex.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    def test_O27_backup_restore_preserves_unknown_reservations(self):
        store = self.store()
        ex = Executor(store, FixtureBroker(), mode="offline", authorize=lambda *_: None, preflight=lambda *_: None)
        order = ex.submit(self.intent(ex), self.bundle.now)
        with store.transaction():
            store.db.execute("UPDATE intents SET state='SUBMITTING' WHERE id=?", (order["id"],))
        backup, restored = self.root / "backup.sqlite", self.root / "restored.sqlite"
        store.backup(backup)
        Store.restore(backup, restored)
        self.stores.remove(store)
        store.close()
        reopened = Store(restored, mode="offline", account_identity=self.tmp.name + "test-account")
        self.stores.append(reopened)
        self.assertEqual(reopened.order(order["id"])["state"], "UNKNOWN")
        self.assertFalse(reopened.get("reconciled"))
        self.assertEqual(reopened.reservation(), D(1000))

    def external_app(self, mode, *, enabled=False):
        data = self.config.data
        data["app"]["app"]["mode"] = mode
        data["app"]["execution"]["enabled"] = enabled
        (self.config_dir / "app.yaml").write_text(yaml.safe_dump(data["app"]))
        config = load_config(self.config_dir)
        recorded = {**self.data, "provenance": "RECORDED_SNAPSHOT",
                    "costs": {**self.data["costs"], "synthetic": False}}
        bundle = MarketBundle(recorded, config.research, mode=mode)
        broker = FixtureBroker()
        broker.environment = None if mode == "shadow" else "demo"
        approval = {"id": "synthetic-review-approval", "config_hash": config.config_hash,
                    "expires_at": (utcnow() + timedelta(hours=1)).isoformat(),
                    "capabilities": ["account_read", "demo_orders", "resume", "candidate_control"]}
        return Application(config, bundle, broker=broker, decide=fixture_decision,
                           refresh=lambda: bundle, approval=approval, clock=lambda: bundle.now)

    def test_execution_disabled_and_changed_activation_block_dispatch(self):
        for enabled in (False, True):
            app = self.external_app("broker_demo", enabled=enabled)
            try:
                with app.store.transaction():
                    app.store.set("activated", True)  # Legacy bool never creates current authority.
                intent = OrderIntent(run_id="test", plan_id="test", thesis_id="test", instrument_id="TEST:AAA",
                    side="BUY", quantity=1, limit_price=D(100), expires_at=app.bundle.now + timedelta(seconds=120),
                    reason="TEST", account_version=app.store.get("account_version"), policy_hash=app.config.config_hash)
                with self.assertRaisesRegex(HumanRequired, "activation" if enabled else "EXECUTION_DISABLED"):
                    app.executor.submit(intent, app.bundle.now)
                if enabled:
                    with app.store.transaction():
                        app.store.set("activation", {"config_hash": app.config.config_hash, "code_id": "old-code",
                                                     "approval_id": app.approval["id"]})
                    with self.assertRaisesRegex(HumanRequired, "activation"):
                        app.executor.submit(intent, app.bundle.now)
                self.assertEqual(app.broker.submissions, 0)
            finally:
                app.close()

    def test_shadow_records_accepted_plan_without_intents_theses_or_fills(self):
        app = self.external_app("shadow")
        try:
            result = app.review(request_key="shadow-review")
            self.assertEqual(result["run_status"], "COMPLETE")
            self.assertEqual(result["order_status"], "SHADOW_PLAN_ONLY")
            plans = json.loads((app.config.state_dir / "runs" / app.bundle.now.date().isoformat() /
                                result["run_id"] / "plan.json").read_text())["data"]["plans"]
            self.assertGreater(plans[0]["quantity"], 0)
            self.assertEqual(app.broker.submissions, 0)
            self.assertEqual(app.store.db.execute("SELECT count(*) FROM intents").fetchone()[0], 0)
            self.assertEqual(app.theses(), [])
            self.assertEqual(app.store.holdings(), [])
        finally:
            app.close()

    def test_decision_age_starts_at_completion_and_includes_post_model_refresh(self):
        for refresh_delay in (0, 121):
            with self.subTest(refresh_delay=refresh_delay):
                clock = [self.bundle.now]
                finished = [False]
                app = Application(self.config, self.bundle, clock=lambda: clock[0])
                def refresh():
                    if finished[0]:
                        clock[0] += timedelta(seconds=refresh_delay)
                        finished[0] = False
                    data = json.loads(json.dumps(self.data))
                    data["as_of"] = clock[0].isoformat()
                    for quote in data["quotes"]:
                        for field in ("observed_at", "received_at", "last_observed_at"):
                            quote[field] = clock[0].isoformat()
                    return MarketBundle(data, self.config.research, mode="offline")
                def model(frozen):
                    clock[0] += timedelta(seconds=180)
                    finished[0] = True
                    return fixture_decision(frozen)
                app.refresh, app.decide = refresh, model
                try:
                    if refresh_delay:
                        with self.assertRaisesRegex(ValueError, "STALE_DECISION"):
                            app.review()
                        self.assertEqual(app.broker.submissions, 0)
                        recorded = json.loads(next(self.config.state_dir.glob('runs/*/*/result.json')).read_text())
                        self.assertEqual(recorded['model_status'], 'FIXTURE_RECORDED_RESPONSE')
                        self.assertEqual(recorded['decision_status'], 'REVALIDATION_FAILED')
                        self.assertTrue(recorded['review_details'][0]['ai'])
                        self.assertTrue(list(self.config.state_dir.glob('runs/*/*/proposal.json')))
                    else:
                        self.assertEqual(app.review()["order_status"], "FIXTURE_FILLED")
                finally:
                    app.close()
                    shutil.rmtree(self.config.state_dir)

    def test_post_model_validation_uses_scoped_refresh(self):
        app = Application(self.config, self.bundle)
        try:
            scoped = []
            def refresh_decision(frozen):
                scoped.append(frozen['run_id'])
                return app.bundle
            app.decision_refresh = refresh_decision
            result = app.review()
            self.assertEqual(scoped, [result['run_id']])
            self.assertEqual(result['decision_status'], 'VALID')
        finally:
            app.close()

    def test_uncollected_history_does_not_duplicate_excluded_instruments(self):
        data = json.loads(json.dumps(self.data))
        instrument = data['instruments'][0]
        instrument.update(kind='excluded_instrument')
        data['bars'] = {}
        data['history_requested'] = []
        excluded = MarketBundle(data, self.config.research, mode='offline')
        reasons = [row['reason'] for row in excluded.exclusions if row['instrument_id'] == instrument['instrument_id']]
        self.assertEqual(reasons, ['UNIVERSE_STATUS_OR_CLASSIFICATION_EXCLUDED'])
        instrument.update(kind='common_stock')
        outside_scope = MarketBundle(data, self.config.research, mode='offline')
        self.assertEqual(outside_scope.exclusions[0]['reason'], 'NO_RECENT_EVENT_TO_REVIEW')
        data['history_requested'] = [instrument['instrument_id']]
        missing = MarketBundle(data, self.config.research, mode='offline')
        self.assertEqual(missing.exclusions[0]['reason'], 'DAILY_HISTORY_NOT_COLLECTED')

    def test_candidate_controls_are_durable_and_do_not_change_held_quantity(self):
        app = Application(self.config, self.bundle)
        try:
            app.update_candidate_list("remove_portfolio_ticker", "AAA")
            self.assertEqual(app.review()["decision_status"], "NO_CANDIDATES")
            app.update_candidate_list("add_portfolio_ticker", "TEST:AAA")
            self.assertEqual(app.review()["order_status"], "FIXTURE_FILLED")
            held = app.store.quantity("TEST:AAA")
            app.update_candidate_list("add_portfolio_except_ticker", "AAA")
            self.assertEqual(app.store.quantity("TEST:AAA"), held)
            self.assertEqual(app.broker.submissions, 1)
            with self.assertRaisesRegex(ValueError, "UNKNOWN_OR_AMBIGUOUS"):
                app.update_candidate_list("add_portfolio_ticker", "MISSING")
        finally:
            app.close()
        reopened = Application(self.config, self.bundle)
        try:
            self.assertEqual(reopened.store.get("candidate_controls")["excluded"], ["TEST:AAA"])
            self.assertEqual(reopened.store.quantity("TEST:AAA"), held)
        finally:
            reopened.close()

    def test_preflight_checks_expiry_after_refresh_before_broker_dispatch(self):
        app = Application(self.config, self.bundle)
        try:
            intent = OrderIntent(run_id="expiry", plan_id="expiry", thesis_id="expiry", instrument_id="TEST:AAA",
                side="BUY", quantity=1, limit_price=D(100), expires_at=self.bundle.now + timedelta(seconds=120),
                reason="TEST", account_version=app.store.get("account_version"), policy_hash=app.config.config_hash)
            def refresh():
                app.bundle.now += timedelta(seconds=121)
                return app.bundle
            app.refresh = refresh
            with self.assertRaisesRegex(ValueError, "ENTRY_EXPIRED"):
                app.executor.submit(intent, self.bundle.now)
            self.assertEqual(app.broker.submissions, 0)
        finally:
            app.close()

    def test_resume_and_candidate_changes_cannot_bypass_missing_authority(self):
        app = self.external_app("shadow")
        try:
            app.approval["capabilities"].remove("candidate_control")
            with self.assertRaisesRegex(HumanRequired, "candidate_control"):
                app.update_candidate_list("remove_portfolio_ticker", "AAA")
            app.pause()
            with app.store.transaction():
                app.store.set("drawdown_paused", True)
            with self.assertRaisesRegex(HumanRequired, "Drawdown"):
                app.resume()
            self.assertTrue(app.store.get("paused"))
            app.approval["capabilities"].append("resume_drawdown")
            with patch.object(app.broker, "snapshot", return_value={"complete": False}):
                with self.assertRaisesRegex(HumanRequired, "reconciliation"):
                    app.resume()
            self.assertEqual(app.resume()["status"], "RESUMED")
            self.assertFalse(app.store.get("paused"))
        finally:
            app.close()

    def test_authorization_change_during_preflight_never_reaches_broker(self):
        app = self.external_app("broker_demo", enabled=True)
        try:
            with app.store.transaction():
                app.store.set("activation", {"config_hash": app.config.config_hash, "code_id": app.code_id,
                                             "approval_id": app.approval["id"]})
            original = app.executor.preflight
            def preflight(intent, now):
                original(intent, now)
                path = self.config_dir / "app.yaml"
                data = yaml.safe_load(path.read_text())
                data["execution"]["enabled"] = False
                path.write_text(yaml.safe_dump(data))
            app.executor.preflight = preflight
            with self.assertRaisesRegex(HumanRequired, "POLICY_CHANGED"):
                app.review()
            self.assertEqual(app.broker.submissions, 0)
            self.assertEqual(app.store.reservation(), 0)
            self.assertEqual(app.store.db.execute("SELECT state FROM intents").fetchone()[0], "INVALIDATED")
            self.assertEqual(app.store.db.execute("SELECT count(*) FROM journal WHERE kind='ORDER_NOT_SENT'").fetchone()[0], 1)
        finally:
            app.close()

    def test_cancel_rechecks_authority_and_preserves_reservation_when_not_sent(self):
        executor = self.executor()
        order = executor.submit(self.intent(executor), self.bundle.now)
        calls = []
        def authorize(_intent, operation):
            calls.append(operation)
            if len(calls) == 2:
                raise HumanRequired("Expired approval")
        executor.authorize = authorize
        with self.assertRaisesRegex(HumanRequired, "Expired approval"):
            executor.cancel(order["id"])
        self.assertEqual(executor.broker.cancel_requests, 0)
        self.assertEqual(executor.store.order(order["id"])["state"], "ACKNOWLEDGED")
        self.assertEqual(executor.store.reservation(), D(1000))


if __name__ == "__main__":
    unittest.main()
