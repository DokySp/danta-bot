import json
from io import StringIO
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, Mock, patch

from service.pipelines.daily_trading.scripts import kis_history as history


def bar(day, **extra):
    return dict(stck_bsop_date=day, stck_oprc='100', stck_hgpr='110', stck_lwpr='90',
                stck_clpr='105', acml_vol='10', **extra)


def direct_bridge():
    # The bridge's third-party imports exist only inside the container at runtime.
    namespace = {'__name__': 'test_direct_bridge'}
    requests = Mock()
    requests.exceptions.Timeout = TimeoutError
    requests.exceptions.ConnectionError = ConnectionError
    requests.exceptions.SSLError = type('SSLError', (ConnectionError,), {})
    requests.exceptions.ChunkedEncodingError = type('ChunkedEncodingError', (Exception,), {})
    with patch.dict(sys.modules, {'requests': requests, 'yaml': Mock()}):
        exec(history.DIRECT_BRIDGE, namespace)
    return namespace


def direct_args(stock=True):
    params = {'env_dv': 'real', 'fid_cond_mrkt_div_code': 'J' if stock else 'U',
              'fid_input_iscd': '0013V0' if stock else '0001', 'fid_input_date_1': '20200102',
              'fid_input_date_2': '20200103', 'fid_period_div_code': 'D'}
    if stock:
        params['fid_org_adj_prc'] = '1'
    return {'api_type': 'inquire_daily_itemchartprice' if stock else 'inquire_daily_indexchartprice', 'params': params}


def direct_response(payload, status=200):
    response = MagicMock(status_code=status)
    response.headers = {}
    response.__enter__.return_value = response
    response.iter_content.return_value = [payload]
    return response


class KisHistoryTests(unittest.TestCase):
    def test_transient_status_is_distinct_from_auth_and_unknown_rate_errors(self):
        bridge = direct_bridge()
        credentials = ('fixture-key', 'fixture-secret', 'fixture-token', datetime.now() + timedelta(hours=1))
        for status, body, retryable in [
                (503, {}, True), (200, {'rt_cd': '1', 'msg_cd': 'EGW00201'}, True),
                (401, {}, False), (429, {}, False), (200, {'rt_cd': '1', 'msg_cd': 'EGW00123'}, False)]:
            with self.subTest(status=status, body=body):
                session = Mock()
                session.get.return_value = direct_response(json.dumps(body).encode(), status)
                expected = bridge['TransientHistoryError'] if retryable else ValueError
                with self.assertRaises(expected):
                    bridge['history_rows'](direct_args(), session, credentials, [0.0])
        session.get.return_value = direct_response(b'{}', 503)
        session.get.return_value.headers = {'Retry-After': '120'}
        with self.assertRaises(ValueError):
            bridge['history_rows'](direct_args(), session, credentials, [0.0])

    def test_bridge_transient_error_never_echoes_transport_details(self):
        bridge = direct_bridge()
        bridge['cached_credentials'] = Mock(return_value=(
            'fixture-key', 'fixture-secret', 'fixture-token', datetime.now() + timedelta(hours=1)))
        session = MagicMock()
        session.__enter__.return_value = session
        bridge['requests'].Session.return_value = session
        session.get.side_effect = TimeoutError('fixture-key fixture-secret fixture-token')
        output = StringIO()
        with patch.object(bridge['sys'], 'stdin', StringIO(json.dumps(direct_args()) + '\n')), \
                patch.object(bridge['sys'], 'stdout', output):
            bridge['main']()
        self.assertEqual(json.loads(output.getvalue()), {'error': 'KIS transient history request failed', 'retryable': True})
        session.get.side_effect = bridge['requests'].exceptions.SSLError('fixture-secret')
        output = StringIO()
        with patch.object(bridge['sys'], 'stdin', StringIO(json.dumps(direct_args()) + '\n')), \
                patch.object(bridge['sys'], 'stdout', output):
            bridge['main']()
        self.assertEqual(json.loads(output.getvalue()), {'error': 'KIS read-only history request failed'})

    def test_direct_rest_only_two_paths_and_raw_output2_leave_container(self):
        bridge = direct_bridge()
        credentials = ('fixture-key', 'fixture-secret', 'fixture-token', datetime.now() + timedelta(hours=1))
        rows = [bar('20200103')]
        response = direct_response(json.dumps({'rt_cd': '0', 'output1': {'account': 'not returned'}, 'output2': rows}).encode())
        session = Mock()
        session.get.return_value = response
        for stock, path, tr_id in ((True, 'inquire-daily-itemchartprice', 'FHKST03010100'),
                                   (False, 'inquire-daily-indexchartprice', 'FHKUP03500100')):
            result = bridge['history_rows'](direct_args(stock), session, credentials, [0.0])
            self.assertEqual(result, rows)
            args, kwargs = session.get.call_args
            self.assertEqual(args, ('https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/' + path,))
            self.assertEqual(kwargs['headers']['tr_id'], tr_id)
            self.assertEqual(kwargs['allow_redirects'], False)
            self.assertEqual(kwargs['timeout'], (5, 25))
            self.assertTrue(kwargs['stream'])
            self.assertNotIn('ENV_DV', kwargs['params'])
            self.assertEqual(kwargs['params']['FID_COND_MRKT_DIV_CODE'], 'J' if stock else 'U')
            self.assertEqual(kwargs['params'].get('FID_ORG_ADJ_PRC'), '1' if stock else None)
        self.assertEqual(response.__exit__.call_count, 2)
        session.post.assert_not_called()

    def test_direct_rest_rejects_non_history_account_adjusted_and_invalid_params(self):
        bridge = direct_bridge()
        credentials = ('fixture-key', 'fixture-secret', 'fixture-token', datetime.now() + timedelta(hours=1))
        invalid = [{'api_type': 'order_cash', 'params': {}}]
        for change in ({'CANO': 'fixture-account'}, {'fid_org_adj_prc': '0'},
                       {'fid_cond_mrkt_div_code': 'UN'}, {'env_dv': 'demo'},
                       {'fid_input_iscd': '../005930'}, {'fid_period_div_code': 'M'},
                       {'fid_input_date_1': '20210101'}):
            args = direct_args()
            args['params'].update(change)
            invalid.append(args)
        session = Mock()
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                bridge['history_rows'](args, session, credentials, [0.0])
        session.get.assert_not_called()

    def test_direct_rest_rejects_redirect_oversize_error_and_reflected_secrets(self):
        bridge = direct_bridge()
        credentials = ('fixture-key', 'fixture-secret', 'fixture-token', datetime.now() + timedelta(hours=1))
        ordinary = json.dumps({'rt_cd': '0', 'output2': [bar('20200103')]}).encode()
        reflected = json.dumps({'rt_cd': '0', 'output2': [bar('20200103', extra='fixture-token')]}).encode()
        escaped = reflected.replace(b'fixture-token', b'\\u0066ixture-token')
        cases = [(ordinary, 302), (b'x' * (bridge['MAX_PAYLOAD'] + 1), 200),
                 (b'{"rt_cd":"1","output2":[]}', 200), (b'{"rt_cd":"0","output2":{}}', 200),
                 (reflected, 200), (escaped, 200)]
        for payload, status in cases:
            with self.subTest(status=status, size=len(payload)):
                session = Mock()
                response = direct_response(payload, status)
                session.get.return_value = response
                with self.assertRaises(ValueError):
                    bridge['history_rows'](direct_args(), session, credentials, [0.0])
                response.__exit__.assert_called_once()
        session = Mock()
        expired = (*credentials[:3], datetime.now() - timedelta(seconds=1))
        with self.assertRaises(ValueError):
            bridge['history_rows'](direct_args(), session, expired, [0.0])
        session.get.assert_not_called()

    def test_direct_rest_cached_token_is_read_only_and_expiry_never_issues_token(self):
        bridge = direct_bridge()
        bridge['yaml'].safe_load.side_effect = json.loads
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'KISprod_fixture'
            future = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
            path.write_text(json.dumps({'token': 'fixture-token', 'valid-date': future}))
            before = path.read_bytes()
            bridge['Path'] = Mock(return_value=root)
            with patch.dict(bridge['os'].environ, {'KIS_APP_KEY': 'fixture-key', 'KIS_APP_SECRET': 'fixture-secret'}, clear=True):
                result = bridge['cached_credentials']()
                self.assertEqual(result[:3], ('fixture-key', 'fixture-secret', 'fixture-token'))
                self.assertEqual(path.read_bytes(), before)
                path.write_text(json.dumps({'token': 'fixture-token', 'valid-date': '2000-01-01 00:00:00'}))
                expired = path.read_bytes()
                with self.assertRaises(ValueError):
                    bridge['cached_credentials']()
                self.assertEqual(path.read_bytes(), expired)
                path.unlink()
                with self.assertRaises(ValueError):
                    bridge['cached_credentials']()
                self.assertEqual(list(root.iterdir()), [])
            bridge['requests'].Session.assert_not_called()

    def test_direct_bridge_reuses_closes_session_throttles_and_sanitizes_errors(self):
        bridge = direct_bridge()
        bridge['cached_credentials'] = Mock(return_value=(
            'fixture-key', 'fixture-secret', 'fixture-token', datetime.now() + timedelta(hours=1)))
        session = MagicMock()
        session.__enter__.return_value = session
        bridge['requests'].Session.return_value = session
        payload = json.dumps({'rt_cd': '0', 'output1': {'snapshot': 'never returned'}, 'output2': [bar('20200103')]}).encode()
        session.get.side_effect = [direct_response(payload), RuntimeError('fixture-secret fixture-token')]
        output = StringIO()
        with patch.object(bridge['sys'], 'stdin', StringIO((json.dumps(direct_args()) + '\n') * 2)), \
                patch.object(bridge['sys'], 'stdout', output), \
                patch.object(bridge['time'], 'monotonic', side_effect=[1.0, 1.0, 1.1, 1.5]), \
                patch.object(bridge['time'], 'sleep') as sleep:
            bridge['main']()
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()],
                         [{'rows': [bar('20200103')]}, {'error': 'KIS read-only history request failed'}])
        self.assertFalse(session.trust_env)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.4)
        bridge['requests'].Session.assert_called_once()
        session.__exit__.assert_called_once()
        session.post.assert_not_called()

    def test_full_calendar_preserves_existing_provenance_and_rejects_truncation(self):
        # Synthetic dates exercise only the size/boundary/persistence guard, not calendar truth.
        dates = [(date(2020, 1, 2) + timedelta(days=i)).isoformat() for i in range(1472)] + ['2025-12-30']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'calendar.json'
            history.save_calendar(path, dates, dates)
            first = path.read_bytes()
            history.save_calendar(path, dates, dates)
            self.assertEqual(path.read_bytes(), first)
            with self.assertRaises(ValueError):
                history.save_calendar(path, dates[:-1], dates[:-1])
            with self.assertRaises(ValueError):
                history.save_calendar(path, dates, dates[:-1])
            from service.pipelines.daily_trading.scripts import historical_universe
            self.assertEqual(historical_universe.load_calendar(path, require_full=True)[0], dates)
            path.write_text(json.dumps({'trading_dates': ['2020-01-02']}))
            with self.assertRaises(ValueError):
                historical_universe.load_calendar(path, require_full=True)

    def test_validation_preserves_action_fields_and_rejects_bad_dates_prices(self):
        start, end = date(2020, 1, 2), date(2020, 1, 3)
        row = bar('20200103', prtt_rate='50.0', flng_cls_code='01')
        self.assertEqual(history.validate_page([row], 'stock', start, end)['2020-01-03'], row)
        for rows in ([bar('20200104')], [bar('20201303')], [row, row],
                     [dict(row, stck_clpr='nan')], [dict(row, stck_lwpr='200')],
                     [dict(row, stck_bsop_date='')]):
            with self.assertRaises(ValueError):
                history.validate_page(rows, 'stock', start, end)
        suspended = dict(row, stck_oprc='0', stck_hgpr='0', stck_lwpr='0', acml_vol='0')
        self.assertTrue(history.validate_page([suspended, {'stck_bsop_date': ''}], 'stock', start, end))

    def test_page_failure_resume_and_conflict(self):
        db = sqlite3.connect(':memory:')
        history.initialize(db)
        calls = []

        class Client:
            fail = True

            def request(self, kind, code, start, end):
                calls.append(end.isoformat())
                if end == date(2020, 1, 3):
                    return [bar('20200103')]
                if self.fail:
                    raise RuntimeError('fixture failure')
                return [bar('20200102')]

        client = Client()
        args = (client, db, 'stock', '0013V0', date(2020, 1, 2), date(2020, 1, 3))
        with self.assertRaises(RuntimeError):
            history.collect_series(*args)
        self.assertEqual(db.execute('SELECT status,next_end FROM history_collection').fetchone(), ('failed', '2020-01-02'))
        client.fail = False
        history.collect_series(*args)
        history.collect_series(*args)
        self.assertEqual(calls, ['2020-01-03', '2020-01-02', '2020-01-02'])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM history_bars').fetchone()[0], 2)
        # An overlapping collection cannot silently overwrite a changed source response.
        row = json.dumps(dict(bar('20200103'), stck_clpr='106'))
        db.execute("UPDATE history_bars SET row_json=? WHERE trade_date='2020-01-03'", (row,))
        db.execute('DELETE FROM history_collection')
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            history.collect_series(*args)
        db.close()


if __name__ == '__main__':
    unittest.main()
