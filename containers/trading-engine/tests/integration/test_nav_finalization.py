"""Daily-close accounting contracts with synthetic data and no network access."""
import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import PropertyMock, patch
from zoneinfo import ZoneInfo

from danta.application import Application, MarketBundle
from danta.config import ROOT, load_config
from danta.market import SessionCalendar
from danta.service import Service


class NavFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket', side_effect=AssertionError('No network in NAV tests'))
        self.network.start()
        self.tmp = tempfile.TemporaryDirectory()
        directory = Path(self.tmp.name) / 'config'
        shutil.copytree(ROOT / 'config', directory, ignore=shutil.ignore_patterns('secrets.yaml'))
        settings = directory / 'app.yaml'
        settings.write_text(settings.read_text().replace('state_dir: ./var/offline/research',
                                                       f'state_dir: {self.tmp.name}/state'))
        self.config = load_config(directory)
        data = json.loads((ROOT / 'tests/fixtures/offline-e2e.json').read_text())
        self.bundle = MarketBundle(data, self.config.research, mode='offline')
        self.session = self.bundle.calendar.active(self.bundle.now)
        self.app = Application(self.config, self.bundle)
        self.app.review(request_key='synthetic-nav-position')
        self.bundle.now = self.session.closes_at + timedelta(minutes=30)
        self.symbol = 'TEST:AAA'
        bar = self.bundle.bars[self.symbol][-1]
        self.close_bar = bar.model_copy(update={'session_id': self.session.session_id,
            'opens_at': self.session.opens_at, 'closes_at': self.session.closes_at,
            'available_at': self.bundle.now, 'high': Decimal('12000'), 'low': Decimal('10000'),
            'close': Decimal('11000'), 'complete': True})
        self.bundle.bars[self.symbol].append(self.close_bar)
        self.bundle.data['account_snapshot'] = self.app.broker.snapshot()
        self.bundle.data['account_snapshot']['strategy_quantities'] = {
            self.symbol: self.app.store.quantity(self.symbol)}

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()
        self.network.stop()

    def completed(self):
        return [point for point in self.app.store.get('nav_points', []) if point['completed']]

    def test_actual_close_works_with_stale_quotes_and_is_idempotent_after_restart(self):
        quantity = self.app.store.quantity(self.symbol)
        cash = Decimal(self.app.store.get('cash_krw'))
        result = self.app.finalize_nav()
        self.assertEqual(result['status'], 'NAV_FINALIZED')
        point = self.completed()[0]
        self.assertEqual(Decimal(point['nav']), cash + quantity * self.close_bar.close)
        self.assertEqual(point['at'], self.session.closes_at.isoformat())
        self.assertEqual(point['observed_at'], self.bundle.now.isoformat())
        self.assertEqual(point['provenance'], 'FIXTURE_ONLY')
        self.assertEqual(point['price_sources'][self.symbol]['available_at'], self.close_bar.available_at.isoformat())
        self.assertFalse(self.app.portfolio().complete)  # Old intraday quotes remain stale.
        broker = self.app.broker
        self.app.close()
        self.app = Application(self.config, self.bundle, broker=broker)
        self.bundle.now += timedelta(minutes=1)
        self.assertEqual(self.app.finalize_nav()['status'], 'ALREADY_FINALIZED')
        self.assertEqual(self.completed(), [point])
        self.bundle.now = self.session.closes_at
        self.app.record_nav()
        self.assertEqual(self.completed(), [point])

    def test_unconfirmed_actual_fees_block_until_settled(self):
        order = next(iter(self.app.broker.orders.values()))
        fees = order['cumulative_fees']
        order.update(cumulative_fees=None, revision=order['revision'] + 1)
        result = self.app.finalize_nav()
        self.assertIn('SETTLEMENT_COSTS_UNCONFIRMED', result['issues'])
        self.assertEqual(self.completed(), [])
        order.update(cumulative_fees=fees, revision=order['revision'] + 1)
        self.assertEqual(self.app.finalize_nav()['status'], 'NAV_FINALIZED')

    def test_after_midnight_finalizes_previous_close_within_catchup_window(self):
        self.bundle.now = self.session.closes_at + timedelta(hours=10)
        self.assertGreater(self.bundle.now.astimezone(ZoneInfo('Asia/Seoul')).date(),
                           self.session.closes_at.astimezone(ZoneInfo('Asia/Seoul')).date())
        result = self.app.finalize_nav()
        self.assertEqual(result['status'], 'NAV_FINALIZED')
        self.assertEqual(result['point']['session_id'], self.session.session_id)
        self.assertEqual(result['point']['at'], self.session.closes_at.isoformat())
        self.bundle.now = self.session.closes_at + timedelta(hours=12)
        self.assertIn('COMPLETED_SESSION_UNAVAILABLE', self.app.finalize_nav()['issues'])

    def test_incomplete_or_unallocated_account_never_finalizes(self):
        account = self.bundle.data['account_snapshot']
        for changed in ({'complete': False}, {'ownership_complete': False},
                        {'strategy_quantities': {self.symbol: 1}}, {'errors': ['UNALLOCATED_BROKER_ORDER']}):
            with self.subTest(changed=changed):
                self.bundle.data['account_snapshot'] = {**account, **changed}
                result = self.app.finalize_nav()
                self.assertEqual(result['status'], 'NAV_NOT_FINALIZED')
                self.assertEqual(self.completed(), [])

    def test_missing_incomplete_future_or_wrong_session_close_is_not_a_zero_return(self):
        for bars in ([], [self.close_bar.model_copy(update={'complete': False})],
                     [self.close_bar.model_copy(update={'available_at': self.bundle.now + timedelta(seconds=1)})],
                     [self.close_bar.model_copy(update={'closes_at': self.session.closes_at - timedelta(minutes=1)})],
                     [self.close_bar, self.close_bar]):
            with self.subTest(bars=len(bars)):
                self.bundle.bars[self.symbol] = bars
                result = self.app.finalize_nav()
                self.assertIn('CLOSING_PRICE_UNVERIFIED:' + self.symbol, result['issues'])
                self.assertEqual(self.completed(), [])

    def test_open_session_and_weekend_do_not_create_completed_samples(self):
        days_to_saturday = (5 - self.session.closes_at.weekday()) % 7
        for when in (self.session.closes_at - timedelta(seconds=1),
                     self.session.closes_at + timedelta(days=days_to_saturday)):
            self.bundle.now = when
            result = self.app.finalize_nav()
            self.assertIn('COMPLETED_SESSION_UNAVAILABLE', result['issues'])
            self.assertEqual(self.completed(), [])

    def test_shortened_session_uses_verified_close_instead_of_regular_hours(self):
        shortened = self.session.model_copy(update={'closes_at': self.session.closes_at - timedelta(hours=3)})
        self.bundle.calendar = SessionCalendar([
            shortened if item.session_id == shortened.session_id else item for item in self.bundle.sessions],
            provenance='SYNTHETIC_SHORTENED_SESSION', verified=True, synthetic=True)
        self.bundle.now = shortened.closes_at + timedelta(minutes=30)
        self.bundle.bars[self.symbol][-1] = self.close_bar.model_copy(update={
            'closes_at': shortened.closes_at, 'available_at': self.bundle.now})
        self.assertEqual(self.app.finalize_nav()['point']['at'], shortened.closes_at.isoformat())

    def test_synthetic_data_cannot_finalize_an_external_mode(self):
        with patch.object(type(self.config), 'mode', new_callable=PropertyMock, return_value='paper'):
            result = self.app.finalize_nav()
        self.assertIn('SYNTHETIC_INPUT_CANNOT_FINALIZE_EXTERNAL_SESSION', result['issues'])
        self.assertEqual(self.completed(), [])

    def test_first_recorded_close_has_no_invented_external_starting_value(self):
        self.bundle.synthetic = False
        self.bundle.data['provenance'] = 'RECORDED_SNAPSHOT'
        with self.app.store.transaction():
            self.app.store.set('nav_points', [])
        result = self.app.finalize_nav()
        self.assertEqual(result['status'], 'NAV_FINALIZED')
        self.assertEqual(result['performance']['coverage'], 'INSUFFICIENT_COVERAGE')
        self.assertEqual(len(self.app.store.get('nav_points')), 1)

    def test_external_observation_does_not_invent_inception_or_accept_missing_account(self):
        self.bundle.synthetic = False
        self.bundle.data['provenance'] = 'RECORDED_SNAPSHOT'
        with self.app.store.transaction():
            self.app.store.set('nav_points', [])
        self.assertEqual(self.app.record_nav()['coverage'], 'INSUFFICIENT_COVERAGE')
        self.assertEqual(len(self.app.store.get('nav_points')), 1)
        self.bundle.data.pop('account_snapshot')
        self.assertIn('ACCOUNT_INCOMPLETE', self.app.finalize_nav()['issues'])
        self.assertEqual(self.completed(), [])

    def test_service_refresh_reconcile_finalize_then_reports_same_persisted_result(self):
        calls = []
        self.app.refresh = lambda: calls.append('refresh') or self.bundle
        service = Service(self.app, clock=lambda: self.bundle.now)
        try:
            result = service._dispatch({'source': 'scheduler', 'kind': 'finalize_and_report'}, 'synthetic-daily')
            self.assertEqual(calls, ['refresh'])
            self.assertEqual(result['finalization']['status'], 'NAV_FINALIZED')
            self.assertEqual(result['report']['status']['nav_finalization'], result['finalization'])
            data = json.loads(Path(result['paths']['json']).read_text())
            self.assertEqual(data['status']['nav_finalization'], result['finalization'])
        finally:
            service.close()

    def test_restart_after_nav_commit_recovers_one_report_without_repeating_nav(self):
        service = Service(self.app, clock=lambda: self.bundle.now)
        request_id, _ = self.app.store.accept_request('service:schedule:crash-close',
            {'source': 'scheduler', 'kind': 'finalize_and_report'})
        with self.app.store.transaction():
            self.app.store.set('service_deadline:' + request_id, (self.session.closes_at+timedelta(hours=12)).isoformat())
        try:
            with patch.object(service, '_report', side_effect=SystemExit('synthetic crash after NAV commit')):
                with self.assertRaises(SystemExit):
                    service.run_once(review=True)
            self.assertEqual(len(self.completed()), 1)
            self.assertEqual(self.app.store.db.execute("SELECT COUNT(*) FROM outbox WHERE event_key=?",
                ('report:' + request_id,)).fetchone()[0], 0)
            service.close()
            service = Service(self.app, clock=lambda: self.bundle.now)
            self.assertTrue(service.run_once(review=True))
            self.assertEqual(len(self.completed()), 1)
            self.assertEqual(self.app.store.db.execute("SELECT COUNT(*) FROM outbox WHERE event_key=?",
                ('report:' + request_id,)).fetchone()[0], 1)
            service.close()
            service = Service(self.app, clock=lambda: self.bundle.now)
            self.assertFalse(service.run_once(review=True))
        finally:
            service.close()


if __name__ == '__main__':
    unittest.main()
