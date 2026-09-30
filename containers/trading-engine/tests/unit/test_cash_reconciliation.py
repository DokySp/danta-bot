"""Explicit cash evidence resolves uncertainty without rewriting cash or loss history."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from danta.config import digest
from danta.store import Store


class CashReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state.sqlite', mode='offline',
                           account_identity=temporary.name, initial_cash=Decimal(1000))
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 9, 21, 1, tzinfo=timezone.utc)
        self.observe(1000)

    def observe(self, cash):
        self.store.reconcile_account_cash({'cash_krw': str(cash), 'observed_at': self.now.isoformat(),
            'source': 'KIS:inquire-balance:prvs_rcdl_excc_amt', 'cost_quality': 'BROKER_REPORTED', 'daily_costs': {}})
        self.now += timedelta(seconds=1)

    def evidence(self, classification, internal='0', flows=None):
        return {'schema_version': 1, 'account_scope': self.store.get('scope'), 'resolutions': [{
            'id': 'statement-1', 'adjustment_ids': [digest(row) for row in self.store.get('unclassified_cash_adjustments')],
            'classification': classification, 'source': 'SYNTHETIC_STATEMENT_ONLY',
            'internal_pnl': internal, 'external_flows': flows or []}]}

    def test_balanced_corrections_require_evidence_and_are_idempotent(self):
        self.observe(800)
        self.observe(1000)
        self.assertTrue(self.store.get('performance_uncertain'))
        evidence = self.evidence('BROKER_CORRECTION')
        result = self.store.resolve_cash_adjustments(evidence, now=self.now)
        self.assertEqual(result['resolved_count'], 2)
        version = self.store.get('account_version')
        self.assertEqual(self.store.resolve_cash_adjustments(evidence, now=self.now)['resolved_count'], 0)
        self.assertEqual(self.store.get('account_version'), version)
        self.assertEqual(self.store.get('cash_krw'), '1000')
        self.assertFalse(self.store.get('performance_uncertain'))
        self.assertEqual(len(self.store.read("SELECT * FROM journal WHERE kind='CASH_ADJUSTMENTS_CLASSIFIED'")), 1)
        evidence['resolutions'][0]['source'] = 'CHANGED_EVIDENCE'
        with self.assertRaisesRegex(ValueError, 'ID_REUSED'):
            self.store.resolve_cash_adjustments(evidence, now=self.now)

    def test_withdrawal_keeps_internal_cost_and_requires_flow_valuations(self):
        self.observe(790)
        flow = {'at': (self.now - timedelta(seconds=1)).isoformat(), 'amount': '-200',
                'before_nav': '1000', 'after_nav': '800'}
        evidence = self.evidence('CASH_TRANSFER', '-10', [flow])
        invalid = copy.deepcopy(evidence)
        invalid['resolutions'][0]['external_flows'][0]['after_nav'] = '790'
        with self.assertRaisesRegex(ValueError, 'VALUATIONS_INVALID'):
            self.store.resolve_cash_adjustments(invalid, now=self.now)
        self.assertTrue(self.store.get('performance_uncertain'))
        self.store.resolve_cash_adjustments(evidence, now=self.now)
        self.assertEqual(self.store.get('cash_krw'), '790')
        self.assertEqual(self.store.get('external_flows')[0]['amount'], '-200')
        self.assertEqual(self.store.get('cash_adjustment_resolutions')['statement-1']['evidence']['internal_pnl'], '-10')

    def test_invalid_scope_reference_or_later_resolution_rolls_back_the_whole_file(self):
        self.observe(800)
        evidence = self.evidence('INTERNAL_PNL', '-200')
        for invalid in (dict(evidence, account_scope='OTHER_ACCOUNT'),
                        dict(evidence, resolutions=[dict(evidence['resolutions'][0], adjustment_ids=['unknown'])]),
                        dict(evidence, resolutions=evidence['resolutions'] + [dict(evidence['resolutions'][0], id='duplicate')])):
            with self.subTest(evidence=invalid), self.assertRaises(ValueError):
                self.store.resolve_cash_adjustments(invalid, now=self.now)
            self.assertTrue(self.store.get('performance_uncertain'))
            self.assertEqual(self.store.get('cash_adjustment_resolutions', {}), {})
            self.assertEqual(self.store.read("SELECT * FROM journal WHERE kind='CASH_ADJUSTMENTS_CLASSIFIED'"), [])

    def test_later_withdrawal_cannot_explain_an_earlier_cash_difference(self):
        self.observe(900)
        observed = self.now - timedelta(seconds=1)
        self.now += timedelta(minutes=10)
        evidence = self.evidence('CASH_TRANSFER', flows=[{'at': (observed + timedelta(minutes=5)).isoformat(),
            'amount': '-100', 'before_nav': '1000', 'after_nav': '900'}])
        with self.assertRaisesRegex(ValueError, 'VALUATIONS_INVALID'):
            self.store.resolve_cash_adjustments(evidence, now=self.now)
        self.assertTrue(self.store.get('performance_uncertain'))
        self.assertEqual(len(self.store.get('unclassified_cash_adjustments')), 1)
        self.assertEqual(self.store.get('external_flows', []), [])


if __name__ == '__main__':
    unittest.main()
