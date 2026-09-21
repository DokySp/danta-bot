"""Operator views preserve trade facts without inventing missing performance."""
import json
from pathlib import Path
import tempfile
import unittest

from danta.reporting import render_notification, write_report
from danta.safety import CredentialError


class OperatorReportingTests(unittest.TestCase):
    def test_operational_failure_has_cause_impact_and_truthful_action(self):
        text = render_notification({'kind': 'MONITOR_DEGRADED', 'error_type': 'HumanRequired',
            'diagnostics': [{'endpoint': 'balance', 'quality': 'FETCH_FAILED', 'reason': 'TRANSIENT_FAILURE', 'http_status': 503}]})
        self.assertIn('잔고', text)
        self.assertIn('503', text)
        self.assertIn('자동으로 다시 확인', text)
        self.assertNotIn('HumanRequired', text)
        self.assertNotIn('FETCH_FAILED', text)
        self.assertNotIn('재개하면', text)
        recovered = render_notification({'kind': 'MONITOR_RECOVERED', 'checked_at': '2026-09-21T10:00:00+00:00'})
        self.assertIn('60초', recovered)
        self.assertNotIn('상세 자료가 없습니다', recovered)

    def test_candidate_view_is_not_misrepresented_as_a_mutation_or_candidate_list(self):
        text = render_notification({'status': 'CANDIDATE_CONTROLS', 'controls': {'removed': [], 'excluded': []}})
        self.assertIn('조회만 수행', text)
        self.assertIn('실제 투자 후보 목록이 아닙니다', text)
        self.assertNotIn('처리 결과', text)

    def test_status_separates_chat_success_from_uncalled_investment_model(self):
        text = render_notification({'mode': 'live', 'holdings': [], 'model_id': 'gpt-5.6-sol',
            'chat_model': {'status': 'SUCCESS', 'checked_at': '2026-09-21T10:00:00+00:00'},
            'review_model': {}, 'review_status': 'ACCOUNT_INCOMPLETE'})
        self.assertIn('일반 대화 AI: 성공', text)
        self.assertIn('투자 판단 AI: 호출하지 않음', text)
        self.assertIn('투자 검토: 계좌 조회 불완전', text)
        self.assertIn('계좌 재조회·복구를 실행하지 않습니다', text)

    def test_daily_report_keeps_all_protection_fills_and_unverified_values(self):
        at = '2026-09-21T00:20:00+00:00'
        orders = [{'instrument_id': f'KRX:00000{i}', 'name': f'보호종목{i}', 'side': 'SELL',
                   'quantity': i, 'state': 'FILLED', 'cumulative_quantity': i,
                   'cumulative_notional': str(i * 12000), 'reason': 'EXIT_PROTECTION',
                   'created_at': at} for i in range(1, 6)]
        fills = [{'at': at, 'instrument_id': order['instrument_id'], 'name': order['name'],
                  'side': order['side'], 'quantity': order['quantity'],
                  'amount_krw': order['cumulative_notional'], 'fee_krw': None,
                  'reason': order['reason'], 'correction': False} for order in orders]
        data = {'date': '2026-09-21', 'created_at': at,
                'status': {'mode': 'live', 'cash_krw': '1234567', 'holdings': [],
                           'performance': {'coverage': 'INSUFFICIENT_COVERAGE', 'pnl': None, 'twr': None}},
                'runs': [{'created_at': at, 'run_status': 'BLOCKED', 'model_status': 'NOT_CALLED',
                          'order_status': 'NONE', 'reason': 'BROKER_PAGINATION_INCOMPLETE'}],
                'orders': orders, 'fills': fills, 'nav': [], 'theses': [],
                'diagnostics': [{'at': at, 'kind': 'MONITOR_DEGRADED', 'reason': '<script>unsafe</script>'}]}
        with tempfile.TemporaryDirectory() as tmp:
            json_path, html_path = Path(tmp) / 'daily.json', Path(tmp) / 'daily.html'
            write_report(data, json_path, html_path, '오늘 <리포트>')
            self.assertEqual(json.loads(json_path.read_text()), data)
            rendered = html_path.read_text()
        for order in orders:
            self.assertEqual(rendered.count(order['name']), 2, 'each order and its fill must remain visible')
        for text in ('체결·정정 기록', '5건', '1,234,567원', '60,000원', '5주',
                     '2026-09-21 09:20:00 KST', '보호 기준가 도달', '누적 손익: 미확인 / 자료 없음',
                     '이번 검토의 주문', '전체 거래', '&lt;script&gt;unsafe&lt;/script&gt;'):
            self.assertIn(text, rendered)
        self.assertNotIn('누적 손익: 0원', rendered)
        self.assertNotIn('<script', rendered)
        self.assertNotIn('<svg', rendered)

    def test_chart_uses_only_supplied_points_and_marks_uncertain_coverage(self):
        data = {'date': '2026-09-21', 'status': {}, 'runs': [], 'orders': [], 'fills': [],
                'nav': [{'at': '2026-09-21T00:00:00Z', 'nav': '1000000', 'quality': 'EXACT'},
                        {'at': '2026-09-21T01:00:00Z', 'nav': '1012345', 'quality': 'INSUFFICIENT_COVERAGE'}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'daily.html'
            write_report(data, Path(tmp) / 'daily.json', path)
            rendered = path.read_text()
        self.assertIn('<svg', rendered)
        self.assertIn('1,012,345원', rendered)
        self.assertIn('품질 미확인/불완전 1개', rendered)
        self.assertIn('입출금이 포함될 수 있으며 수익률을 뜻하지 않습니다', rendered)
        self.assertIn('누적 시간가중 수익률: 미확인 / 자료 없음', rendered)

    def test_trade_notifications_and_generic_fallback_keep_human_facts(self):
        payload = {'kind': 'CUMULATIVE_FILL', 'instrument_id': 'KRX:005930', 'side': 'SELL',
                   'intent_id': 'internal-id', 'quantity_delta': 3, 'notional_delta_krw': '210000',
                   'fee_delta_krw': '420', 'cumulative_quantity': 5, 'reason': 'EXIT_TIME_LIMIT',
                   'observed_at': '2026-09-21T01:00:00Z'}
        text = render_notification(payload, symbols={'KRX:005930': '삼성전자'})
        for phrase in ('체결 확인 · 삼성전자 (005930) · 매도', '추가 체결 수량: 3주',
                       '210,000원', '누적 체결 수량: 5주', '최대 보유 기간 도달', '10:00:00 KST'):
            self.assertIn(phrase, text)
        self.assertNotIn('internal-id', text)
        for confirmed, expected in ((False, '미확인 / 자료 없음'), (True, '0원')):
            fees = render_notification({**payload, 'fee_delta_krw': '0', 'fees_confirmed': confirmed})
            self.assertIn('추가 수수료·세금: ' + expected, fees)
        self.assertIn('추가 수수료·세금: 미확인 / 자료 없음',
                      render_notification({**payload, 'fee_delta_krw': '0'}))
        fallback = render_notification({'status': 'FAILED', 'error_type': 'InterfaceError',
            'reason': '조회 재시도 필요', 'new_measurement': 17, 'config_hash': 'a' * 64,
            'paths': {'html': '/app/private/report.html'}})
        for phrase in ('실패', 'InterfaceError', '조회 재시도 필요', 'new measurement: 17'):
            self.assertIn(phrase, fallback)
        self.assertNotIn('/app/', fallback)
        self.assertNotIn('a' * 64, fallback)
        self.assertNotIn('{', fallback)

    def test_usage_controls_safety_and_full_length(self):
        usage = render_notification({'status': 'USAGE', 'rate_limits': {
            'primary': {'usedPercent': 18, 'windowDurationMins': 300, 'resetsAt': 1790006400},
            'secondary': {'used_percent': 77, 'window_minutes': 10080}}, 'attempts': [{'status': 'PROCESS_FAILED'}]})
        self.assertIn('5시간 남은 한도: 82%', usage)
        self.assertIn('주간 남은 한도: 23%', usage)
        self.assertIn('KST', usage)
        self.assertIn('Codex 실행 실패', usage)
        self.assertIn('보호 감시·매도는 계속 작동', render_notification({'status': 'PAUSED', 'protection': 'CONTINUES'}))
        long_text = '계좌 조회 실패 원인입니다. ' * 400
        self.assertIn(long_text, render_notification({'reason': long_text}))
        with self.assertRaises(CredentialError):
            render_notification({'apiKey': 'not-a-real-secret'})


if __name__ == '__main__':
    unittest.main()
