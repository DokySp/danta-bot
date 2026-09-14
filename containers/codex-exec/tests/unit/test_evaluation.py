"""E01–E12 and I05: synthetic engine correctness, never real profitability."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest

from danta.accounting import ExternalFlow, NavPoint, completed_session_returns, performance, strategy_nav, usage_totals
from danta.evaluation import (ARMS, EvaluationManifest, PaperLedger, PaperOrder, TimelineEvent,
                              _protect, _run, content_hash, evaluate_manifest, evaluate_metrics,
                              paired_bootstrap, validate_candidate_pool)
from danta.market import SessionCalendar, TickTable
from danta.models import EventRecord, InvestmentThesis, Quote
from danta.reporting import render_readme, write_report


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests/fixtures/evaluation-synthetic.json'
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def fixture():
    return EvaluationManifest.model_validate_json(FIXTURE.read_text())


def ledger_order(quantity=3):
    manifest = fixture()
    calendar = SessionCalendar(manifest.sessions, provenance=manifest.calendar_source, verified=True, synthetic=True)
    ticks = TickTable([(D(0), D(1))], provenance='synthetic', verified=True, synthetic=True)
    ledger = PaperLedger('full_strategy', D('10000000'), manifest.costs, calendar, ticks)
    at = manifest.timeline[0].at
    thesis = InvestmentThesis(thesis_id='synthetic-thesis', instrument_id='TEST:AAA',
        event_ids=['event-AAA'], source_uris=['https://example.invalid/official'],
        economic_path='synthetic', horizon_case='synthetic 3-20 sessions', counterevidence='synthetic',
        invalidation_case='synthetic', initial_stop=D(10000), current_stop=D(10000),
        initial_r_price=D(600), average_entry=D(10600), planned_quantity=quantity,
        risk_budget=D(25000), strategy_hash='synthetic', policy_hash='synthetic', created_at=at)
    order = PaperOrder('test-order', 'TEST:AAA', 'BUY', quantity, D(10600), at,
        at + timedelta(seconds=1), at + timedelta(seconds=120), thesis, 'issuer-AAA',
        'synthetic-sector', D(300), D(750))
    return manifest, ledger, order


def quote(at, bid='10499', ask='10500'):
    return Quote(instrument_id='TEST:AAA', venue='KRX', observed_at=at, received_at=at,
                 bid=D(bid), ask=D(ask), source='synthetic')


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = ''
    def handle_data(self, data):
        self.text += data


class AccountingEvaluationTests(unittest.TestCase):
    def test_e01_overnight_loss_is_in_continuous_return(self):
        points = [NavPoint(T0 + timedelta(days=i), value) for i, value in enumerate((100, 90, 91))]
        result = performance(points)
        self.assertEqual(D(result['twr']), D('-.09'))
        self.assertEqual(D(result['max_drawdown']), D('.1'))
        self.assertNotEqual(D(performance(points[1:])['twr']), D('-.09'))

    def test_e02_cash_asset_flows_are_external_but_dividends_are_internal(self):
        flow = ExternalFlow(T0 + timedelta(days=1), 100, 90, 190)
        result = performance([NavPoint(T0, 100), NavPoint(T0 + timedelta(days=2), 209)], [flow])
        self.assertEqual(D(result['twr']), D('-.01'))
        self.assertEqual(D(result['max_drawdown']), D('.1'))
        self.assertEqual(D(result['pnl']), D(9))
        asset = ExternalFlow(flow.at, 100, 90, 190, 'ASSET_TRANSFER')
        self.assertEqual(performance([NavPoint(T0, 100), NavPoint(T0 + timedelta(days=2), 209)], [asset])['twr'], result['twr'])
        with self.assertRaisesRegex(ValueError, 'internal'):
            ExternalFlow(flow.at, 10, 100, 110, 'DIVIDEND')

    def test_e03_managed_quantity_excludes_manual_and_foreign_assets(self):
        managed = {'TEST:AAA': 3}
        self.assertEqual(strategy_nav(100, managed, {'TEST:AAA': 10, 'FOREIGN': 1000}), D(130))
        self.assertEqual(strategy_nav(100, managed, {'TEST:AAA': 10, 'FOREIGN': 5000}), D(130))
        self.assertFalse(performance([NavPoint(T0, 100), NavPoint(T0 + timedelta(days=1), 100)], unallocated=True)['new_risk_allowed'])
        with self.assertRaises(ValueError):
            strategy_nav(100, {'TEST:AAA': True}, {'TEST:AAA': 10})

    def test_e04_fill_prices_and_cash_already_include_costs(self):
        _, ledger, order = ledger_order()
        ledger.submit(order)
        reserved_nav = ledger.nav
        ledger.process_quote(quote(order.eligible_at), ask_quantity=1, bid_quantity=0, event_id='a')
        ledger.process_quote(quote(order.eligible_at + timedelta(seconds=1)), ask_quantity=2, bid_quantity=0, event_id='b')
        self.assertEqual(reserved_nav, D('10000000'))
        fills = [event for event in ledger.journal if event['type'] == 'FILL']
        self.assertEqual(order.commission_charged, sum(D(fill['commission']) for fill in fills))
        self.assertEqual(ledger.cash, D('10000000') - sum(D(fill['quantity']) * D(fill['price']) + D(fill['commission']) for fill in fills))
        # No accounting fee/slippage arguments can deduct those events a second time.
        result = performance([NavPoint(T0, 100), NavPoint(T0 + timedelta(days=1), 99)])
        self.assertEqual(D(result['pnl']), D(-1))
        usage = usage_totals([{'input_tokens': 100, 'cached_input_tokens': 60, 'output_tokens': 40, 'reasoning_tokens': 20}])
        self.assertEqual(usage['total_tokens'], 140)

    def test_e05_only_completed_calendar_sessions_and_no_zero_fill(self):
        day = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)
        points, sessions = [], []
        while len(sessions) < 21:
            if day.weekday() < 5:
                session = day.date().isoformat()
                sessions.append(session)
                points.append(NavPoint(day, 100 + len(sessions), session, True))
            else:
                points.append(NavPoint(day, 9999, None, False))
            day += timedelta(days=1)
        result = completed_session_returns(points, sessions, 20)
        self.assertEqual(len(result['returns']), 20)
        self.assertEqual(result['coverage'], 'EXACT')
        missing = completed_session_returns([p for p in points if p.session_id != sessions[5]], sessions, 20)
        self.assertEqual(missing['coverage'], 'INSUFFICIENT_COVERAGE')
        self.assertNotIn(sessions[5], missing['returns'])

    def test_e06_missing_flow_valuations_forbid_exact_twr(self):
        result = performance([NavPoint(T0, 100), NavPoint(T0 + timedelta(days=2), 300)],
                              [ExternalFlow(T0 + timedelta(days=1), 100)])
        self.assertIsNone(result['twr'])
        self.assertFalse(result['new_risk_allowed'])
        self.assertIn('FLOW_VALUATION_MISSING', result['issues'])

    def test_e07_split_dividend_halt_and_delisting_never_create_fake_fills(self):
        _, ledger, order = ledger_order(2)
        ledger.submit(order)
        ledger.process_quote(quote(order.eligible_at), ask_quantity=2, bid_quantity=0, event_id='entry')
        before = ledger.nav
        ledger.corporate_action('TEST:AAA', 'SPLIT', order.eligible_at, ratio='2', confirmed=True)
        self.assertEqual(ledger.nav, before)
        self.assertEqual(ledger.holdings['TEST:AAA'].quantity, 4)
        ledger.corporate_action('TEST:AAA', 'DIVIDEND', order.eligible_at, net_per_share='10', confirmed=True)
        self.assertEqual(ledger.nav, before + 40)
        ledger.corporate_action('TEST:AAA', 'HALT', order.eligible_at)
        count = len([item for item in ledger.journal if item['type'] == 'FILL'])
        ledger.process_quote(quote(order.eligible_at + timedelta(seconds=2)), ask_quantity=100, bid_quantity=100, event_id='halt')
        self.assertEqual(len([item for item in ledger.journal if item['type'] == 'FILL']), count)
        ledger.corporate_action('TEST:AAA', 'DELIST', order.eligible_at)
        self.assertIn('UNRESOLVED_CORPORATE_ACTION_DELIST', ledger.quality_issues)
        self.assertEqual(ledger.holdings['TEST:AAA'].quantity, 4)

    def test_e08_comparison_pool_must_precede_ai_acceptance(self):
        manifest = fixture()
        data = copy.deepcopy(manifest.timeline[0].data)
        data['pool_stage'] = 'AI_ACCEPTED'
        with self.assertRaisesRegex(ValueError, 'AI_CANDIDATE_CONTAMINATION'):
            validate_candidate_pool(data, manifest.timeline[0].at)
        data['pool_stage'] = 'UNIVERSE_PRE_AI'
        data['candidates'] = data['candidates'][:1]
        with self.assertRaisesRegex(ValueError, 'incomplete original'):
            validate_candidate_pool(data, manifest.timeline[0].at)
        replay = _run(manifest)
        self.assertEqual(set(replay['arms']), set(ARMS))
        self.assertNotEqual(replay['arms']['full_strategy']['cash_krw'], replay['arms']['technical_only']['cash_krw'])
        self.assertEqual(replay['arms']['cash']['nav_krw'], '10000000')
        self.assertEqual(replay['arms']['technical_only']['order_statuses']['EXPIRED'], 1)

    def test_e09_future_revisions_and_survivor_universe_are_visible(self):
        manifest = fixture()
        data = copy.deepcopy(manifest.timeline[0].data)
        data['events'][0]['available_at'] = (manifest.timeline[0].at + timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, 'FUTURE_DISCLOSURE'):
            validate_candidate_pool(data, manifest.timeline[0].at)
        manifest.population_point_in_time = False
        replay = _run(manifest)
        self.assertIn('SURVIVOR_UNIVERSE', replay['arms']['full_strategy']['issues'])
        self.assertIsNone(replay['arms']['full_strategy']['twr'])

    def test_e10_daily_bars_cannot_certify_intraday_order(self):
        manifest = fixture()
        manifest.precision = 'DAILY_BARS'
        replay = _run(manifest)
        self.assertIsNone(replay['arms']['full_strategy']['twr'])
        self.assertTrue(any('DAILY_BAR' in issue for issue in replay['arms']['full_strategy']['issues']))

    def test_e11_sample_counts_are_sessions_and_closed_theses(self):
        manifest = fixture()
        replay = _run(manifest)
        replay['arms']['full_strategy']['model_call_count'] = 10000
        evaluation = evaluate_metrics(manifest, replay, _run(manifest, 2))
        self.assertEqual(evaluation['closed_discovery_theses'], 1)
        self.assertFalse(evaluation['live_review_eligible'])
        self.assertIn('MINIMUM_60_COMPLETED_SESSIONS_AND_30_CLOSED_THESES_NOT_MET', evaluation['hold_reasons'])

    def test_e12_loss_unknown_operating_cost_selection_bias_no_live_promotion(self):
        result = evaluate_manifest(FIXTURE)
        self.assertEqual(result['verdict'], 'INCONCLUSIVE')
        self.assertIn('NONPOSITIVE_NET_PNL_OR_PRIMARY_BASELINE_EXCESS', result['failures'])
        self.assertEqual(result['evidence_status'], 'FIXTURE_ONLY')
        self.assertFalse(result['automatic_live_promotion'])
        self.assertIn('OPERATING_COST_ALLOCATION_UNCONFIRMED', result['hold_reasons'])
        manifest = fixture()
        manifest.selected_best_experiment = True
        manifest.experiment_attempt_count = 20
        selected = evaluate_metrics(manifest, _run(manifest), _run(manifest, 2))
        self.assertIn('BEST_OF_MULTIPLE_EXPERIMENTS_SELECTION_BIAS', selected['hold_reasons'])
        self.assertEqual(selected['experiment_attempt_count'], 20)
        loss = _run(manifest)
        loss['sessions'] = [str(i) for i in range(80)]
        for arm in loss['arms'].values():
            arm['daily_returns'] = {session: '-.001' for session in loss['sessions']}
        loss['arms']['full_strategy']['closed_trades'] = [
            {'thesis_id': str(i), 'exit_session': str(i)} for i in range(30)]
        failed = evaluate_metrics(manifest, loss, loss)
        self.assertEqual(failed['verdict'], 'FAIL')

    def test_paper_after_latency_partial_expiry_and_cancel_race(self):
        _, ledger, order = ledger_order()
        ledger.submit(order)
        ledger.process_quote(quote(order.submitted_at), ask_quantity=3, bid_quantity=0, event_id='too-early')
        self.assertEqual(order.filled, 0)
        ledger.process_quote(quote(order.eligible_at), ask_quantity=1, bid_quantity=0, event_id='first')
        self.assertEqual(order.status, 'PARTIAL')
        ledger.request_cancel(order.order_id, order.eligible_at, order.eligible_at + timedelta(seconds=2))
        ledger.process_quote(quote(order.eligible_at + timedelta(seconds=1)), ask_quantity=1, bid_quantity=0, event_id='raced')
        self.assertEqual(order.filled, 2)
        ledger.process_quote(quote(order.eligible_at + timedelta(seconds=2)), ask_quantity=1, bid_quantity=0, event_id='after-ack')
        self.assertEqual(order.status, 'CANCELLED')
        self.assertEqual(order.filled, 2)
        _, other, expires = ledger_order()
        other.submit(expires)
        other.process_quote(quote(expires.expires_at), ask_quantity=3, bid_quantity=0, event_id='at-expiry')
        self.assertEqual(expires.status, 'EXPIRED')
        self.assertEqual(expires.filled, 0)

    def test_semantic_invalidation_belongs_only_to_full_strategy(self):
        manifest, full, order = ledger_order()
        full.submit(order)
        current_quote = quote(order.eligible_at)
        full.process_quote(current_quote, ask_quantity=3, bid_quantity=0, event_id='entry')
        baseline = copy.deepcopy(full)
        baseline.arm = 'technical_only'
        event = EventRecord.model_validate_json(json.dumps(manifest.timeline[0].data['events'][0]))
        event.event_id, event.polarity = 'new-negative-official-event', 'NEGATIVE'
        _protect(full, manifest, current_quote, order.eligible_at, invalidating_events=[event])
        _protect(baseline, manifest, current_quote, order.eligible_at, invalidating_events=[event])
        self.assertEqual(len(full.orders), 2)
        self.assertEqual(len(baseline.orders), 1)
        self.assertEqual(list(full.orders.values())[-1].reasons, ['EXIT_THESIS_INVALID'])

    def test_session_deadline_runs_without_quotes_and_waits_for_actual_liquidity(self):
        manifest = fixture()
        first = next(index for index, session in enumerate(manifest.sessions)
                     if session.opens_at <= manifest.timeline[0].at < session.closes_at)
        held_sessions = manifest.sessions[first:first + 21]
        # Retain entry/partial-fill quotes only. No market observation is supplied for 20 sessions.
        timeline = list(manifest.timeline[:4])
        for session in held_sessions:
            timeline.append(TimelineEvent(at=session.closes_at, kind='session_close', data={
                'session_id': session.session_id, 'marks_verified': True,
                'marks': {'TEST:AAA': '10590', 'TEST:BBB': '10600'}}))
        resumed_at = held_sessions[20].opens_at + timedelta(minutes=30)
        for offset in (0, 1):
            at = resumed_at + timedelta(seconds=offset)
            timeline.append(TimelineEvent(at=at, kind='quote', data={
                'event_id': f'resumed-{offset}', 'quote': quote(at, '10590', '10591').model_dump(mode='json'),
                'ask_quantity': 0, 'bid_quantity': 1000}))
        data = manifest.model_dump(mode='json')
        data['timeline'] = [event.model_dump(mode='json') for event in timeline]
        data['data_hash'] = content_hash(data['timeline'])
        replay = _run(EvaluationManifest.model_validate_json(json.dumps(data)))
        full = replay['arms']['full_strategy']
        observations = [event for event in full['journal'] if event['type'] == 'PROTECTION_OBSERVATION']
        due_at = held_sessions[19].closes_at - timedelta(minutes=10)
        deadline = next(event for event in observations if datetime.fromisoformat(event['at']) == due_at)
        self.assertEqual(deadline['action'], 'MONITOR_DEGRADED')
        self.assertIn('EXIT_TIME_LIMIT', deadline['reasons'])
        closed = next(event for event in observations if datetime.fromisoformat(event['at']) == held_sessions[19].closes_at)
        self.assertEqual(closed['action'], 'EXIT_OVERDUE')
        sells = [event for event in full['journal'] if event['type'] == 'FILL' and event['side'] == 'SELL']
        self.assertEqual(len(sells), 1)
        self.assertEqual(datetime.fromisoformat(sells[0]['at']), resumed_at + timedelta(seconds=1))
        self.assertEqual(full['closed_trades'][0]['holding_sessions'], 21)
        self.assertIn('MONITOR_DEGRADED_OBSERVATIONS', full['issues'])
        self.assertTrue(all(datetime.fromisoformat(event['at']) <= held_sessions[-1].closes_at
                            for event in full['journal'] if 'at' in event))

    def test_unexecutable_protection_cancels_entry_before_zero_quantity_return(self):
        manifest, ledger, order = ledger_order()
        ledger.submit(order)
        at = order.eligible_at
        ledger.process_quote(quote(at), ask_quantity=1, bid_quantity=0, event_id='partial-entry')
        ledger.halted.add(order.instrument_id)
        _protect(ledger, manifest, quote(at, '9900', '9901'), at)
        self.assertEqual(order.status, 'CANCEL_REQUESTED')
        self.assertEqual(len(ledger.orders), 1)
        ledger.advance(at + timedelta(milliseconds=manifest.cancel_ack_delay_ms))
        self.assertEqual(order.status, 'CANCELLED')
        self.assertEqual(ledger.holdings[order.instrument_id].quantity, 1)

    def test_bootstrap_is_reproducible_paired_non_circular_and_missing_invalidates(self):
        sessions = [str(index) for index in range(60)]
        full = {session: '.002' for session in sessions}
        base = {session: '.001' for session in sessions}
        result = paired_bootstrap(full, base, sessions, 123)
        self.assertEqual(result, paired_bootstrap(full, base, sessions, 123))
        self.assertEqual(list(map(D, result['ci95'])), [D('.001'), D('.001')])
        self.assertFalse(result['circular'])
        self.assertEqual(result['resamples'], 2000)
        self.assertEqual(paired_bootstrap({**full, '5': None}, base, sessions, 123)['status'], 'INSUFFICIENT_COVERAGE')

    def test_i05_report_preserves_readme_hash_tables_code_and_escapes_raw_html(self):
        source = ROOT / 'README.md'
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'report.html'
            metadata = render_readme(source, target, generated_at=T0)
            rendered = target.read_text()
            self.assertEqual(metadata['source_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertIn(metadata['source_sha256'], rendered)
            self.assertIn('@media print', rendered)
            self.assertIn('@media(max-width:650px)', rendered)
            self.assertNotIn('<script', rendered)
            text = Text()
            text.feed(rendered)
            self.assertIn('block_bootstrap_resamples: 2000', text.text)
            self.assertIn('100→90→91', text.text)
            self.assertIn('E12', text.text)
            self.assertIn('I05', text.text)
            self.assertGreater(metadata['heading_count'], 50)
            malicious = Path(tmp) / 'sample.md'
            malicious.write_text('# test\n\n<script>alert(1)</script>\n\n| A | B |\n|---|---|\n|1|2|\n')
            render_readme(malicious, target)
            self.assertNotIn('<script>', target.read_text())
            self.assertIn('&lt;script&gt;', target.read_text())
            self.assertIn('<table>', target.read_text())
            data = {'evidence_status': 'FIXTURE_ONLY', 'pnl': None, 'reason': '<script>bad</script>'}
            write_report(data, Path(tmp) / 'daily.json', target)
            self.assertEqual(json.loads((Path(tmp) / 'daily.json').read_text()), data)
            self.assertNotIn('<script>', target.read_text())


if __name__ == '__main__':
    unittest.main()
