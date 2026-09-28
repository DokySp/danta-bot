"""KIS paging contracts, using synthetic responses without network or credentials."""
import json
import io
from contextlib import redirect_stderr
import unittest
from unittest.mock import patch
from datetime import date
from urllib.parse import parse_qs, urlsplit

from danta.adapters import AdapterError, HttpResponse, require_http_ok
from danta.adapters.kis import KisAdapter, KisCredentials


class KisPaginationContracts(unittest.TestCase):
    def test_success_status_with_missing_response_field_identifies_failed_endpoint(self):
        for endpoint, field, call in (
            ('inquire-balance', 'output1', lambda adapter: adapter.read_account()),
            ('inquire-psbl-order', 'output', lambda adapter: adapter.read_account(resource_symbol='005930', resource_price='100')),
            ('inquire-asking-price-exp-ccn', 'output1', lambda adapter: adapter.quote('005930')),
            ('inquire-price', 'output', lambda adapter: adapter.quote('005930')),
        ):
            with self.subTest(endpoint=endpoint):
                def transport(method, url, *args):
                    data = {'rt_cd':'0'}
                    if not urlsplit(url).path.endswith(endpoint):
                        data.update(output={}, output1=[] if 'inquire-balance' in url else {})
                    return HttpResponse(200, json.dumps(data).encode())
                transport.fixture_only = True
                adapter = KisAdapter(environment='real', credentials=KisCredentials('00000000','00','FAKE','FAKE','FAKE'), transport=transport)
                try:
                    result = call(adapter)
                except AdapterError as error:
                    detail = error.diagnostic
                else:
                    self.assertNotEqual(result.quality, 'COMPLETE')
                    detail = result.metadata
                self.assertEqual((detail['endpoint'], detail['field'], detail['http_status']), (endpoint, field, 200))

    def test_request_failures_keep_safe_context_for_http_business_and_parse_errors(self):
        private = 'private-' + 'z' * 32
        for status, body, expected in (
            (500, {'msg_cd':'EGW00215', 'msg1':'처리 오류 계좌 12345678 ' + private}, 'TRANSIENT_FAILURE'),
            (200, {'rt_cd':'1', 'msg_cd':'APBK0918', 'msg1':'조회 불가 계좌 1234-5678 12/345/678 12.345.678 '
                'https://user:pass@example.com/private?CANO=12345678 ' + private}, 'BROKER_REJECTED:APBK0918'),
            (200, b'not-json', 'MALFORMED_RESPONSE'),
        ):
            with self.subTest(status=status, expected=expected):
                def transport(*args):
                    return HttpResponse(status, body if isinstance(body, bytes) else json.dumps(body).encode())
                transport.fixture_only = True
                adapter = KisAdapter(environment='real',
                    credentials=KisCredentials('12345678', '00', private, private, private), transport=transport)
                output = io.StringIO()
                with redirect_stderr(output), self.assertRaises(AdapterError) as raised:
                    adapter.read_buying_power('005930', '100')
                detail = raised.exception.diagnostic
                self.assertEqual(raised.exception.code, expected)
                self.assertEqual(detail['endpoint'], 'inquire-psbl-order')
                self.assertEqual(detail['http_status'], status)
                self.assertEqual(detail['tr_id'], 'TTTC8908R')
                self.assertEqual(detail['method'], 'GET')
                self.assertEqual(detail['request_stage'], 'BROKER_REQUEST')
                self.assertIn('requested_at', detail)
                if isinstance(body, dict):
                    self.assertEqual(detail['provider_code'], body['msg_cd'])
                    self.assertIn('[비공개]', detail['provider_message'])
                record = json.loads(output.getvalue())
                self.assertEqual(record['event'], 'KIS_REQUEST_FAILED')
                for secret in (private, '12345678', '1234-5678', '12/345/678', '12.345.678', 'example.com', 'CANO='):
                    self.assertNotIn(secret, output.getvalue() + str(detail))

    @patch('danta.adapters.kis.time.sleep')
    def test_server_error_preserves_redacted_message_timing_and_bounded_retries(self, sleep):
        calls = []
        private = 'private-' + 'z'*32
        def transport(*args):
            calls.append(args[0])
            return HttpResponse(500,json.dumps({'msg_cd':'EGW00215','msg1':'처리 오류 계좌 12345678 '+private}).encode())
        transport.fixture_only = True
        adapter = KisAdapter(environment='real',credentials=KisCredentials('12345678','00',private,private,private),transport=transport)
        result = adapter.read_account()
        self.assertEqual(calls,['GET']*3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],[0.5,1.0])
        self.assertEqual(result.metadata['provider_code'],'EGW00215')
        self.assertEqual(result.metadata['attempt_count'],3)
        self.assertIn('처리 오류',result.metadata['provider_message'])
        self.assertNotIn(private,str(result.metadata))
        self.assertNotIn('12345678',str(result.metadata))
        self.assertIn('requested_at',result.metadata)
        self.assertIn('elapsed_seconds',result.metadata)

    def adapter(self, pages, **options):
        calls = []

        def transport(method, url, headers, body, timeout):
            self.assertEqual(method, "GET")
            calls.append((parse_qs(urlsplit(url).query, keep_blank_values=True), headers))
            page = pages[min(len(calls) - 1, len(pages) - 1)]
            if isinstance(page, Exception):
                raise page
            data, continuation = page
            return HttpResponse(200, json.dumps(data).encode(), {"Tr_Cont": continuation})

        transport.fixture_only = True
        return KisAdapter(environment="real", credentials=KisCredentials("00000000", "00", "FAKE", "FAKE", "FAKE"),
                          transport=transport, **options), calls

    def test_cursor_is_preserved_for_all_account_queries(self):
        today = date(2026, 9, 21)
        for method, args, rows_key, width, endpoint in (
            ("read_account", (), "output1", 100, "inquire-balance"),
            ("read_orders", (today, today), "output1", 100, "inquire-daily-ccld"),
            ("read_cancelable_orders", (), "output", 100, "inquire-psbl-rvsecncl"),
            ("read_reservations", (today, today), "output", 200, "order-resv-ccnl"),
        ):
            with self.subTest(method=method):
                first = {"rt_cd": "0", rows_key: [{"fixture": "first"}],
                         f"ctx_area_fk{width}": "  opaque search  ", f"ctx_area_nk{width}": " opaque key "}
                adapter, calls = self.adapter([(first, " F "), ({"rt_cd": "0", rows_key: [{"fixture": "last"}]}, "D")])
                result = getattr(adapter, method)(*args)
                self.assertEqual(result.quality, "COMPLETE")
                self.assertEqual(result.records, ({"fixture": "first"}, {"fixture": "last"}))
                self.assertEqual(result.metadata["endpoint"], endpoint)
                self.assertEqual(calls[1][0][f"CTX_AREA_FK{width}"], [first[f"ctx_area_fk{width}"]])
                self.assertEqual(calls[1][0][f"CTX_AREA_NK{width}"], [first[f"ctx_area_nk{width}"]])
                self.assertEqual(calls[1][1]["tr_cont"], "N")

    def test_incomplete_pages_never_become_complete(self):
        page = {"rt_cd": "0", "output1": [{"fixture": "first"}], "ctx_area_fk100": "next", "ctx_area_nk100": "key"}
        cases = [
            ([(page, "M"), (page, "F")], {}, "PARTIAL", "REPEATED_CURSOR"),
            ([(page, "M")], {"max_pages": 1}, "PARTIAL", "PAGE_LIMIT"),
            ([(dict(page, ctx_area_fk100=" ", ctx_area_nk100=""), "M")], {}, "PARTIAL", "MISSING_CURSOR"),
            ([(dict(page, ctx_area_nk100=None), "M")], {}, "PARTIAL", "MALFORMED_CURSOR"),
            ([(page, "unknown")], {}, "PARTIAL", "INVALID_CONTINUATION"),
            ([(dict(page, output1=[None]), "D")], {}, "FETCH_FAILED", "MALFORMED_RESPONSE"),
            ([(page, "M"), TimeoutError()], {}, "PARTIAL", "TRANSPORT_FAILED"),
            ([TimeoutError()], {}, "FETCH_FAILED", "TRANSPORT_FAILED"),
            ([(page, "M"), ({"rt_cd": "1", "msg_cd": "EGW00201"}, "")], {}, "PARTIAL", "BROKER_REJECTED:EGW00201"),
        ]
        for pages, options, quality, error in cases:
            with self.subTest(error=error, quality=quality):
                adapter, _ = self.adapter(pages, **options)
                result = adapter.read_account()
                self.assertEqual(result.quality, quality)
                self.assertEqual(result.metadata["error"], error)
                self.assertEqual(result.metadata["endpoint"], "inquire-balance")

    @patch('danta.adapters.kis.time.sleep')
    def test_transient_read_retries_same_page_without_duplicate_rows(self, sleep):
        first = {'rt_cd': '0', 'output1': [{'fixture': 'first'}], 'ctx_area_fk100': 'next', 'ctx_area_nk100': 'key'}
        adapter, calls = self.adapter([(first, 'M'), AdapterError('TRANSIENT_FAILURE', diagnostic={'http_status': 503}),
                                      ({'rt_cd': '0', 'output1': [{'fixture': 'last'}]}, 'D')])
        result = adapter.read_account()
        self.assertEqual(result.quality, 'COMPLETE')
        self.assertEqual(len(result.records), 2)
        self.assertEqual(calls[1], calls[2])
        sleep.assert_called_once_with(0.5)

    @patch('danta.adapters.kis.time.sleep')
    def test_exhausted_read_preserves_safe_diagnostics_and_auth_is_not_retried(self, sleep):
        for code, status, count in [('TRANSIENT_FAILURE', 503, 3), ('AUTH_FAILED', 401, 1)]:
            adapter, calls = self.adapter([AdapterError(code, diagnostic={'http_status': status, 'provider_code': 'EGW00201'})])
            result = adapter.read_account()
            self.assertEqual(len(calls), count)
            self.assertEqual(result.quality, 'FETCH_FAILED')
            self.assertEqual(result.metadata['http_status'], status)
            self.assertEqual(result.metadata['failed_page'], 1)
        with self.assertRaises(AdapterError) as failure:
            require_http_ok(HttpResponse(503, b'{"msg_cd":"EGW00201","msg1":"private provider details"}'))
        self.assertEqual(failure.exception.diagnostic, {'http_status': 503, 'provider_code': 'EGW00201'})


if __name__ == "__main__":
    unittest.main()
