"""KIS paging contracts, using synthetic responses without network or credentials."""
import json
import unittest
from datetime import date
from urllib.parse import parse_qs, urlsplit

from danta.adapters import HttpResponse
from danta.adapters.kis import KisAdapter, KisCredentials


class KisPaginationContracts(unittest.TestCase):
    def adapter(self, pages, **options):
        calls = []

        def transport(method, url, headers, body, timeout):
            self.assertEqual(method, "GET")
            calls.append((parse_qs(urlsplit(url).query, keep_blank_values=True), headers))
            page = pages[len(calls) - 1]
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


if __name__ == "__main__":
    unittest.main()
