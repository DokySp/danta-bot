"""Risk accounting regressions through the frozen-manifest public entry points."""
import copy
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from danta.evaluation import TimelineEvent, content_hash, evaluate_manifest, replay_manifest


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/evaluation-synthetic.json'


class EvaluationRiskReplayTests(unittest.TestCase):
    def manifest(self, *, two_positions=False):
        data = json.loads(FIXTURE.read_text())
        data['timeline'] = data['timeline'][:1]
        if two_positions:
            review = data['timeline'][0]['data']
            decisions = review['decisions']
            decisions['TEST:BBB'] = {**copy.deepcopy(decisions['TEST:AAA']), 'rank': 2}
            review['events'].append({**copy.deepcopy(review['events'][0]),
                                     'event_id': 'event-BBB', 'instrument_id': 'TEST:BBB'})
            review['candidates'][1]['event_ids'] = ['event-BBB']
            review['candidates'][1]['coverage'] = 'COMPLETE'
        return data

    def quote(self, data, seconds, symbol, price, *, ask_quantity=0, bid_quantity=0):
        start = datetime.fromisoformat(data['timeline'][0]['at'].replace('Z', '+00:00'))
        at = (start + timedelta(seconds=seconds)).isoformat()
        data['timeline'].append({'at': at, 'kind': 'quote', 'data': {
            'event_id': f'{symbol}:{seconds}:{len(data["timeline"])}',
            'quote': {'instrument_id': symbol, 'venue': 'KRX', 'observed_at': at,
                      'received_at': at, 'bid': str(price), 'ask': str(price + 1),
                      'source': 'synthetic'},
            'ask_quantity': ask_quantity, 'bid_quantity': bid_quantity}})
        return at

    def replay(self, data, *, evaluate=False):
        data['timeline'] = [TimelineEvent.model_validate_json(json.dumps(event)).model_dump(mode='json')
                            for event in data['timeline']]
        data['data_hash'] = content_hash(data['timeline'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            path.write_text(json.dumps(data))
            return (evaluate_manifest if evaluate else replay_manifest)(path)

    def close(self, data, *, mark):
        session_id = data['timeline'][0]['at'][:10]
        session = next(item for item in data['sessions'] if item['session_id'] == session_id)
        data['timeline'].append({'at': session['closes_at'], 'kind': 'session_close',
            'data': {'session_id': session_id, 'marks_verified': True,
                     'marks': {'TEST:AAA': str(mark), 'TEST:BBB': str(mark)}}})
        return session_id

    def test_intraday_drawdown_survives_recovery_in_evaluation_metrics(self):
        data = self.manifest()
        self.quote(data, 3, 'TEST:AAA', 10559, ask_quantity=1000)
        self.quote(data, 10, 'TEST:AAA', 80000)
        trough_at = self.quote(data, 11, 'TEST:AAA', 40000)
        self.quote(data, 12, 'TEST:AAA', 80000)
        session_id = self.close(data, mark=80000)

        result = self.replay(data, evaluate=True)
        self.assertIn('RESEARCH_DRAWDOWN_BOUNDARY_REACHED', result['failures'])
        self.assertIn('DATA_OR_OPERATIONAL_COVERAGE_INCOMPLETE', result['hold_reasons'])
        for replay in (result['replay'], result['stress_replay']):
            full = replay['arms']['full_strategy']
            self.assertTrue(full['new_risk_paused'])
            self.assertGreaterEqual(Decimal(full['max_drawdown']), Decimal('.10'))
            self.assertFalse(full['new_risk_allowed'])
            observed = next(point for point in full['performance_index'] if point['at'] == trough_at)
            self.assertGreaterEqual(Decimal(observed['drawdown']), Decimal('.10'))
            self.assertEqual(set(full['daily_returns']), {session_id})
            self.assertEqual(Decimal(full['daily_returns'][session_id]), Decimal(full['twr']))

    def test_asynchronous_fresh_quotes_record_concentration_without_reductions(self):
        data = self.manifest(two_positions=True)
        self.quote(data, 3, 'TEST:AAA', 10559, ask_quantity=1000)
        self.quote(data, 4, 'TEST:BBB', 10559, ask_quantity=1000)
        self.quote(data, 10, 'TEST:AAA', 80000)
        self.quote(data, 11, 'TEST:BBB', 80000)
        self.quote(data, 15, 'TEST:AAA', 80000)
        self.quote(data, 16, 'TEST:BBB', 80000)
        self.quote(data, 17, 'TEST:AAA', 80000, bid_quantity=1000)
        self.quote(data, 18, 'TEST:BBB', 80000, bid_quantity=1000)

        full = self.replay(data)['arms']['full_strategy']
        reductions = [item for item in full['journal'] if item['type'] == 'PROTECTION_OBSERVATION'
                      and item['action'] == 'REDUCE_TO_LIMIT']
        self.assertEqual(reductions, [])
        sells = [item for item in full['journal'] if item['type'] == 'FILL' and item['side'] == 'SELL']
        self.assertEqual(sells, [])
        reference = full['concentration_state']['reference']
        self.assertEqual(set(reference['position_weights']), {'issuer-AAA', 'issuer-BBB'})
        self.assertTrue(reference['above_reference'])
        self.assertFalse(full['concentration_state']['active_plan'])

    def test_stale_other_position_does_not_count_as_valid_concentration_observation(self):
        data = self.manifest(two_positions=True)
        self.quote(data, 3, 'TEST:AAA', 10559, ask_quantity=1000)
        self.quote(data, 4, 'TEST:BBB', 10559, ask_quantity=1000)
        self.quote(data, 10, 'TEST:AAA', 80000)
        self.quote(data, 20, 'TEST:AAA', 80000)
        full = self.replay(data)['arms']['full_strategy']
        self.assertEqual(full['concentration_state']['observations'], {})
        self.assertEqual(full['coverage'], 'INSUFFICIENT_COVERAGE')
        self.assertFalse(full['new_risk_allowed'])
        self.assertFalse(any(item['type'] == 'FILL' and item['side'] == 'SELL'
                             for item in full['journal']))

    def test_same_timestamp_quotes_share_one_nav_sample_and_advisory_state(self):
        data = self.manifest(two_positions=True)
        self.quote(data, 3, 'TEST:AAA', 10559, ask_quantity=1000)
        self.quote(data, 4, 'TEST:BBB', 10559, ask_quantity=1000)
        self.quote(data, 10, 'TEST:AAA', 80000)
        self.quote(data, 10, 'TEST:BBB', 80000)
        self.quote(data, 10, 'TEST:AAA', 80000)
        full = self.replay(data)['arms']['full_strategy']
        self.assertTrue(full['performance_index'])
        self.assertEqual(len(full['performance_index']),
                         len({point['at'] for point in full['performance_index']}))
        self.assertEqual(full['concentration_state']['observations'], {})
        self.assertEqual(full['concentration_state']['mode'], 'advisory')
        self.assertTrue(full['concentration_state']['reference'])

    def test_unrelated_quote_does_not_confirm_the_same_position_valuation_twice(self):
        data = self.manifest()
        self.quote(data, 3, 'TEST:AAA', 10559, ask_quantity=1000)
        self.quote(data, 10, 'TEST:AAA', 80000)
        self.quote(data, 15, 'TEST:BBB', 10559)
        full = self.replay(data)['arms']['full_strategy']
        self.assertFalse(full['concentration_state']['active_plan'])
        self.assertTrue(all(count == 1 for count, _ in full['concentration_state']['observations'].values()))

    def test_missing_session_mark_is_not_folded_into_the_next_daily_return(self):
        data = self.manifest()
        self.quote(data, 3, 'TEST:AAA', 10559, ask_quantity=1000)
        first = self.close(data, mark=11000)
        index = next(i for i, item in enumerate(data['sessions']) if item['session_id'] == first)
        missing, later = data['sessions'][index + 1:index + 3]
        close = copy.deepcopy(data['timeline'][-1])
        close['at'] = later['closes_at']
        close['data']['session_id'] = later['session_id']
        close['data']['marks']['TEST:AAA'] = '11500'
        data['timeline'].append(close)
        replay = self.replay(data)
        self.assertEqual(replay['sessions'], [first, missing['session_id'], later['session_id']])
        returns = replay['arms']['full_strategy']['daily_returns']
        self.assertNotIn(missing['session_id'], returns)
        self.assertIsNone(returns[later['session_id']])


if __name__ == '__main__':
    unittest.main()
