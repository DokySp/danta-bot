from __future__ import annotations

import json
import hashlib
from copy import deepcopy
import tempfile
import sqlite3
import unittest
import zipfile
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from ..scripts import dart_history as dart


def filing(receipt="20240516001421", received="20240516", name="분기보고서 (2024.03)"):
    return {"corp_code": "00126380", "stock_code": "005930", "corp_name": "test",
            "rcept_no": receipt, "rcept_dt": received, "report_nm": name, "rm": ""}


def xbrl(*, member="ConsolidatedMember", extra_member="", currency="KRW", duplicate="", custom=False):
    contexts, facts = [], []
    namespace = "urn:custom" if custom else "http://xbrl.ifrs.org/taxonomy/2023-01-01/ifrs-full"
    for year, revenue, income in ((2024, 1200, 120), (2023, 1000, 100)):
        contexts.append(f'''<xbrli:context id="c{year}"><xbrli:entity>
          <xbrli:identifier>00126380</xbrli:identifier><xbrli:segment>
          <xbrldi:explicitMember dimension="ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis">ifrs-full:{member}</xbrldi:explicitMember>
          {extra_member}</xbrli:segment></xbrli:entity><xbrli:period>
          <xbrli:startDate>{year}-01-01</xbrli:startDate><xbrli:endDate>{year}-03-31</xbrli:endDate>
          </xbrli:period></xbrli:context>''')
        facts.extend([f'<ifrs-full:Revenue contextRef="c{year}" unitRef="u">{revenue}</ifrs-full:Revenue>',
                      f'<dart:OperatingIncomeLoss contextRef="c{year}" unitRef="u">{income}</dart:OperatingIncomeLoss>'])
    return f'''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:ifrs-full="{namespace}"
      xmlns:dart="http://dart.fss.or.kr/taxonomy/2023-01-01/ifrs/dart"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
      {''.join(contexts)}<xbrli:unit id="u"><xbrli:measure>iso4217:{currency}</xbrli:measure></xbrli:unit>
      {''.join(facts)}{duplicate}</xbrli:xbrl>'''.encode()


def archive(xml):
    result = BytesIO()
    with zipfile.ZipFile(result, "w") as target:
        target.writestr("instance.xbrl", xml)
    return result.getvalue()


def income_record(year, month, received, current=(1200, 120), prior=(1000, 100), *, correction=False):
    name = ("사업" if month == 12 else "분기") + f"보고서 ({year}.{month:02})"
    if correction:
        name = "[기재정정]" + name
    row = dart.filing_metadata(filing(received + "000001", received, name))
    row.update(source="fnlttXbrl.xml", sha256="a" * 64, facts=[])
    for period_year, values in ((year, current), (year - 1, prior)):
        for concept, amount in zip(("Revenue", "OperatingIncomeLoss"), values):
            row["facts"].append({"start": f"{period_year}-01-01",
                                 "end": f"{period_year}-{month:02}-{'31' if month == 12 else '30'}",
                                 "concept": concept, "amount_krw": amount, "reported_decimals": ["-3"]})
    return row


class DartHistoryTest(unittest.TestCase):
    def test_universe_batch_refuses_incomplete_source_and_preserves_partial_status(self):
        from ..scripts import historical_universe as universe_tools
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / 'universe.sqlite3'
            client = Mock(request_count=0)
            with self.assertRaisesRegex(dart.DartError, 'universe must be complete'):
                dart.collect_universe(client, universe, date(2019, 1, 1), date(2025, 12, 31), root)
            client.request.assert_not_called()
            days = [(date(2020, 1, 2) + timedelta(days=i)).isoformat() for i in range(1472)] + ['2025-12-30']
            dart.save_json(root / 'calendar.json', {'trading_dates': days})
            universe_tools.initialize(universe, days, hashlib.sha256((root / 'calendar.json').read_bytes()).hexdigest())
            with sqlite3.connect(universe) as db:
                for market, code in [('STK', '005930'), ('KSQ', '035420')]:
                    db.executemany('INSERT INTO universe_membership VALUES(?,?,?)', [(day, market, code) for day in days])
                    db.execute("UPDATE universe_snapshots SET status='complete',expected_count=1,actual_count=1,membership_sha256=? WHERE market=?", (universe_tools.code_digest([code]), market))
            self.assertEqual(universe_tools.validated_complete_codes(universe, root / 'calendar.json'), ['005930', '035420'])
            # Aggregate complete counts alone must not accept a wrong dated snapshot.
            with sqlite3.connect(universe) as db:
                db.execute("UPDATE universe_snapshots SET snapshot_date='1900-01-01' WHERE snapshot_date=? AND market='STK'", (days[0],))
            with self.assertRaises(dart.DartError):
                dart.collect_universe(client, universe, date(2019, 1, 1), date(2025, 12, 31), root)
            client.request.assert_not_called()
            with sqlite3.connect(universe) as db:
                db.execute("UPDATE universe_snapshots SET snapshot_date=? WHERE snapshot_date='1900-01-01'", (days[0],))
            data = BytesIO()
            with zipfile.ZipFile(data, 'w') as zipped:
                zipped.writestr('CORPCODE.xml', '<result><list><corp_code>00126380</corp_code><stock_code>005930</stock_code></list><list><corp_code>00266961</corp_code><stock_code>035420</stock_code></list></result>')
            client.request.return_value = data.getvalue()
            calls = []
            def complete_company(client, corp, start, end, folder, **kwargs):
                calls.append(corp)
                folder.mkdir(parents=True, exist_ok=True)
                result = {'status': 'complete', 'receipts': 0, 'records': []}
                dart.save_json(folder / f'result-{corp}-{start}-{end}.json', result)
                return result
            def stopped_company(client, corp, start, end, folder, **kwargs):
                if corp == '00266961':
                    raise OSError('fixture disk I/O failure must not leak')
                return complete_company(client, corp, start, end, folder, **kwargs)
            with patch.object(dart, 'collect', side_effect=stopped_company):
                with self.assertRaises(dart.DartError):
                    dart.collect_universe(client, universe, date(2019, 1, 1), date(2025, 12, 31), root)
            status = json.loads((root / 'dart-full-collection-status.json').read_text())
            self.assertEqual(status['status'], 'partial')
            self.assertEqual(status['stopped_code'], '035420')
            self.assertEqual(status['error'], 'financial batch failed (OSError)')
            self.assertFalse(status['strategy_backtest_ready'])
            self.assertEqual([r['code'] for r in status['completed']], ['005930'])
            calls.clear()
            client.request.reset_mock()
            with patch.object(dart, 'collect', side_effect=complete_company):
                result = dart.collect_universe(client, universe, date(2019, 1, 1), date(2025, 12, 31), root)
            self.assertEqual(calls, ['00266961'])
            self.assertEqual(result['status'], 'collection_complete_not_backtest_ready')
            client.request.assert_not_called()  # Mapping and completed company reused without API budget.
            with sqlite3.connect(universe) as db:
                db.execute("UPDATE universe_snapshots SET expected_count=2 WHERE snapshot_date=? AND market='STK'", (days[0],))
            with self.assertRaises(ValueError):
                universe_tools.validated_complete_codes(universe, root / 'calendar.json')

    def test_receipt_next_day_and_corrections_are_point_in_time(self):
        original = dart.filing_metadata(filing())
        correction = dart.filing_metadata(filing("20240601000001", "20240601", "[기재정정]분기보고서 (2024.03)"))
        self.assertEqual(original["available_on"], "2024-05-17")
        self.assertIsNone(dart.latest_asof([original, correction], date(2024, 5, 16)))
        self.assertEqual(dart.latest_asof([original, correction], date(2024, 6, 1)), original)
        self.assertEqual(dart.latest_asof([original, correction], date(2024, 6, 2)), correction)
        self.assertTrue(correction["is_correction"])

    def test_late_old_correction_does_not_replace_latest_quarter(self):
        recent = dart.filing_metadata(filing("20240816000001", "20240816", "반기보고서 (2024.06)"))
        old_correction = dart.filing_metadata(filing("20240901000001", "20240901", "[기재정정]분기보고서 (2024.03)"))
        self.assertEqual(dart.latest_asof([recent, old_correction], date(2024, 9, 2)), recent)

    def test_unsupported_latest_report_does_not_resurrect_older_signal(self):
        prior = dart.filing_metadata(filing())
        latest = dart.filing_metadata(filing("20240816000001", "20240816", "반기보고서 (2024.06)"))
        latest["quarterly_growth"] = {"status": "unavailable"}
        self.assertEqual(dart.latest_asof([prior, latest], date(2024, 8, 17)), latest)

    def test_reject_wrong_identifiers_future_period_and_unsupported_calendar(self):
        with self.assertRaises(dart.DartError):
            dart.filing_metadata(filing(receipt="../../x"))
        with self.assertRaises(dart.DartError):
            dart.filing_metadata(filing(received="20240330"))
        self.assertIsNone(dart.filing_metadata(filing(name="사업보고서 (2024.03)")))

    def test_parse_consolidated_quarter_without_scaling_decimals(self):
        facts = dart.consolidated_facts(archive(xbrl().replace(b'unitRef="u"', b'unitRef="u" decimals="-3"')), "00126380")
        growth = dart.quarterly_growth(facts, "2024-03-31")
        self.assertEqual(growth["revenue_current_krw"], 1200)
        self.assertEqual(growth["operating_income_prior_year_krw"], 100)
        self.assertTrue(growth["earnings_improved"])
        self.assertTrue(all(row["reported_decimals"] == ["-3"] for row in facts))
        self.assertEqual(dart.quarterly_growth(facts, "2024-12-31")["status"], "unavailable")

    def test_separate_segment_currency_custom_and_wrong_company_not_substituted(self):
        segment = '<xbrldi:explicitMember dimension="ifrs-full:SegmentAxis">ifrs-full:SegmentMember</xbrldi:explicitMember>'
        for kwargs in ({"member": "SeparateMember"}, {"extra_member": segment}, {"currency": "USD"}, {"custom": True}):
            with self.subTest(kwargs=kwargs):
                facts = dart.consolidated_facts(archive(xbrl(**kwargs)), "00126380")
                self.assertEqual(dart.quarterly_growth(facts, "2024-03-31")["status"], "unavailable")
        self.assertEqual(dart.consolidated_facts(archive(xbrl()), "99999999"), [])

    def test_conflicting_duplicates_fail_and_identical_duplicates_deduplicate(self):
        for amount in (1200, 1300):
            duplicate = f'<ifrs-full:Revenue contextRef="c2024" unitRef="u">{amount}</ifrs-full:Revenue>'
            if amount == 1300:
                with self.assertRaises(dart.ConflictingFacts):
                    dart.consolidated_facts(archive(xbrl(duplicate=duplicate)), "00126380")
            else:
                self.assertEqual(len(dart.consolidated_facts(archive(xbrl(duplicate=duplicate)), "00126380")), 4)

    def test_research_quarantines_conflicting_receipt_without_stopping_later_receipts(self):
        duplicate = '<ifrs-full:Revenue contextRef="c2024" unitRef="u">1300</ifrs-full:Revenue>'
        bad = archive(xbrl(duplicate=duplicate))
        rows = [dart.filing_metadata(filing()), dart.filing_metadata(filing(
            '20240601000001', '20240601', '[기재정정]분기보고서 (2024.03)'))]
        with tempfile.TemporaryDirectory() as directory, patch('builtins.print'):
            output = Path(directory)
            client = Mock()
            client.request.return_value = bad
            with patch.object(dart, 'list_filings', return_value=deepcopy(rows)):
                with self.assertRaises(dart.ConflictingFacts):
                    dart.collect(client, '00126380', date(2024, 1, 1), date(2024, 12, 31), output)
            self.assertFalse(list(output.glob('*.zip')))
            client.request.return_value = archive(xbrl().replace(b'>1200<', b'>NaN<'))
            with patch.object(dart, 'list_filings', return_value=deepcopy(rows)):
                with self.assertRaisesRegex(dart.DartError, 'finite whole KRW'):
                    dart.collect(client, '00126380', date(2024, 1, 1), date(2024, 12, 31), output,
                                 compact=True, research=True)
            self.assertFalse(list(output.glob('*.zip')))
            client.request.side_effect = [bad, archive(xbrl())]
            with patch.object(dart, 'list_filings', return_value=deepcopy(rows)):
                result = dart.collect(client, '00126380', date(2024, 1, 1), date(2024, 12, 31), output,
                                      compact=True, research=True)
            self.assertEqual(result['quarantined_receipts'], 1)
            self.assertEqual(result['comparable_research_reports'], 1)
            self.assertFalse(result['strategy_backtest_ready'])
            first = result['records'][0]
            self.assertEqual(first['facts'], [])
            self.assertEqual(first['research_growth']['status'], 'unavailable')
            self.assertIn('conflicting facts', first['financial_data_issue'])
            self.assertEqual(dart.xbrl_instance((output / first['archive']).read_bytes())[1], xbrl(duplicate=duplicate))
            self.assertEqual(dart.latest_asof(result['records'], date(2024, 6, 1), research=True)['research_growth']['status'], 'unavailable')
            self.assertTrue(dart.latest_asof(result['records'], date(2024, 6, 2), research=True)['research_growth']['earnings_improved'])
            client.request.reset_mock()
            with patch.object(dart, 'list_filings', return_value=deepcopy(rows)):
                resumed = dart.collect(client, '00126380', date(2024, 1, 1), date(2024, 12, 31), output,
                                       compact=True, research=True)
            client.request.assert_not_called()
            self.assertEqual(resumed['quarantined_receipts'], 1)

    def test_nil_nonfinite_xml_entities_and_api_error(self):
        for payload in (b'<!DOCTYPE x [<!ENTITY e "secret">]><x/>', '<x/>'.encode("utf-16")):
            with self.assertRaises(dart.DartError):
                dart.xml_root(payload)
        with self.assertRaises(dart.DartError):
            dart.consolidated_facts(archive(xbrl().replace(b">1200<", b">NaN<")), "00126380")
        with self.assertRaisesRegex(dart.DartError, "status 020"):
            dart.consolidated_facts(b'<result><status>020</status><message>not printed</message></result>', "00126380")

    def test_list_paginates_and_requests_originals_and_corrections(self):
        class Client:
            def request(self, endpoint, params):
                self.params.append(params)
                row = filing() if params["page_no"] == "1" else filing("20240601000001", "20240601", "[기재정정]분기보고서 (2024.03)")
                return json.dumps({"status": "000", "total_page": 2, "list": [row]}).encode()
        client = Client()
        client.params = []
        rows = dart.list_filings(client, "00126380", date(2024, 1, 1), date(2024, 12, 31))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(params["last_reprt_at"] == "N" for params in client.params))

    def test_auth_errors_and_redirects_never_echo_sensitive_details(self):
        client = dart.DartClient("0" * 40, request_limit=1)
        with patch.object(client._opener, "open", side_effect=URLError("sensitive-query")), patch.object(dart.time, "sleep"):
            with self.assertRaisesRegex(dart.DartError, "^DART transport failed$"):
                client.request("list.json", {})
        with self.assertRaisesRegex(dart.DartError, "budget exhausted"):
            client.request("list.json", {})
        with self.assertRaises(dart.DartError):
            dart.NoRedirect().redirect_request(None, None, 302, None, None, "https://elsewhere.invalid")

    def test_collect_reuses_archives_and_rejects_changed_cached_provenance(self):
        class Client:
            request_count = 0
            def request(self, endpoint, params):
                self.request_count += 1
                if endpoint == "list.json":
                    return json.dumps({"status": "000", "total_page": 1, "list": [filing()]}).encode()
                return archive(xbrl())
        with tempfile.TemporaryDirectory() as name, patch("builtins.print"):
            output, client = Path(name), Client()
            result = dart.collect(client, "00126380", date(2024, 1, 1), date(2024, 12, 31), output)
            self.assertEqual(result["comparable_quarters"], 1)
            self.assertFalse(result["strategy_backtest_ready"])
            self.assertEqual(client.request_count, 2)
            dart.collect(client, "00126380", date(2024, 1, 1), date(2024, 12, 31), output)
            self.assertEqual(client.request_count, 3)
            (output / "20240516001421.zip").write_bytes(archive(xbrl(member="SeparateMember")))
            with self.assertRaisesRegex(dart.DartError, "provenance mismatch"):
                dart.collect(client, "00126380", date(2024, 1, 1), date(2024, 12, 31), output)

    def test_orphan_archive_is_not_assigned_to_receipt_by_filename(self):
        with tempfile.TemporaryDirectory() as name:
            output = Path(name)
            (output / "20240516001421.zip").write_bytes(archive(xbrl()))
            client = Mock()
            with patch.object(dart, "list_filings", return_value=[dart.filing_metadata(filing())]):
                with self.assertRaisesRegex(dart.DartError, "metadata missing"):
                    dart.collect(client, "00126380", date(2024, 1, 1), date(2024, 12, 31), output)
            client.request.assert_not_called()

    def test_corrupt_xbrl_fails_but_explicit_missing_file_remains_unavailable(self):
        for payload, allowed in ((archive(b"not xml"), False),
                                 (b"<result><status>014</status></result>", True)):
            with tempfile.TemporaryDirectory() as name, patch("builtins.print"):
                client = Mock()
                client.request.return_value = payload
                with patch.object(dart, "list_filings", return_value=[dart.filing_metadata(filing())]):
                    if allowed:
                        result = dart.collect(client, "00126380", date(2024, 1, 1), date(2024, 12, 31), Path(name))
                        self.assertEqual(result["comparable_quarters"], 0)
                    else:
                        with self.assertRaisesRegex(dart.DartError, "invalid DART XML"):
                            dart.collect(client, "00126380", date(2024, 1, 1), date(2024, 12, 31), Path(name))

    def test_compressed_reflected_key_is_rejected_before_archive_write(self):
        # Synthetic test credential; not an account key.
        key = "1" * 40
        packed = BytesIO()
        with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED) as target:
            target.writestr("instance.xbrl", b"<x>" + key.encode() + b"</x>")
        payload = packed.getvalue()
        self.assertNotIn(key.encode(), payload)
        client = dart.DartClient(key)
        with patch.object(client._opener, "open", return_value=BytesIO(payload)), patch.object(dart.time, "sleep"):
            with self.assertRaisesRegex(dart.DartError, "unsafe DART archive"):
                client.request("fnlttXbrl.xml", {"rcept_no": "20240516001421", "reprt_code": "11013"})

    def test_q4_difference_keeps_provenance_precision_and_unverified_status(self):
        annual = income_record(2024, 12, "20250311")
        ytd = income_record(2024, 9, "20241114", (900, 70), (800, 70))
        result = dart.q4_reconciliation(annual, [annual, ytd], date(2025, 3, 12))
        self.assertEqual(result["status"], "derived_unverified")
        self.assertEqual(result["candidate"], {"revenue_current_krw": 300, "revenue_prior_year_krw": 200,
                                            "operating_income_current_krw": 50, "operating_income_prior_year_krw": 30,
                                            "earnings_improved": True})
        self.assertNotIn("earnings_improved", result)
        self.assertEqual(result["available_on"], "2025-03-12")
        self.assertEqual([row["rcept_no"] for row in result["sources"]], [annual["rcept_no"], ytd["rcept_no"]])
        self.assertEqual(result["calculations"]["revenue_current_krw"]["annual_decimals"], ["-3"])

    def test_q4_future_receipts_and_other_company_do_not_fill_missing_ytd(self):
        annual = income_record(2024, 12, "20250311")
        future = income_record(2024, 9, "20250320")
        other = income_record(2024, 9, "20241114")
        other["corp_code"] = "99999999"
        self.assertEqual(dart.q4_reconciliation(annual, [annual, future, other], date(2025, 3, 12))["status"], "unavailable")
        self.assertEqual(dart.q4_reconciliation(annual, [annual], date(2025, 3, 11))["status"], "unavailable")

    def test_q4_later_ytd_correction_recomputes_only_after_publication(self):
        annual = income_record(2024, 12, "20250311")
        ytd = income_record(2024, 9, "20241114", (900, 70), (800, 70))
        correction = income_record(2024, 9, "20250320", (950, 90), (800, 70), correction=True)
        records = [annual, ytd, correction]
        frozen = deepcopy(records)
        before = dart.latest_asof(records, date(2025, 3, 20))["quarterly_growth"]
        after = dart.latest_asof(records, date(2025, 3, 21))["quarterly_growth"]
        self.assertEqual(before["candidate"]["revenue_current_krw"], 300)
        self.assertEqual(after["candidate"]["revenue_current_krw"], 250)
        self.assertEqual(after["available_on"], "2025-03-21")
        self.assertEqual(records, frozen)

    def test_q4_missing_latest_correction_does_not_reuse_older_ytd(self):
        annual = income_record(2024, 12, "20250311")
        ytd = income_record(2024, 9, "20241114")
        correction = income_record(2024, 9, "20250320", correction=True)
        correction.pop("facts")
        result = dart.latest_asof([annual, ytd, correction], date(2025, 3, 21))
        self.assertEqual(result["quarterly_growth"]["status"], "unavailable")

    def test_q4_changed_comparative_annual_or_ytd_amounts_block_subtraction(self):
        annual = income_record(2024, 12, "20250311")
        ytd = income_record(2024, 9, "20241114", (900, 70), (800, 70))
        for month, received, changed in ((12, "20240311", (999, 100)), (9, "20231114", (799, 70))):
            with self.subTest(month=month):
                older = income_record(2023, month, received, changed)
                result = dart.q4_reconciliation(annual, [annual, ytd, older], date(2025, 3, 12))
                self.assertEqual(result["status"], "unavailable")
                self.assertIn("restatement", result["reason"])
                self.assertNotIn("candidate", result)

    def test_q4_matching_overlap_does_not_certify_consolidation_scope(self):
        annual = income_record(2024, 12, "20250311")
        ytd = income_record(2024, 9, "20241114", (900, 70), (800, 70))
        old_annual = income_record(2023, 12, "20240311", (1000, 100))
        old_ytd = income_record(2023, 9, "20231114", (800, 70))
        result = dart.q4_reconciliation(annual, [annual, ytd, old_annual, old_ytd], date(2025, 3, 12))
        self.assertEqual(result["status"], "derived_unverified")
        self.assertEqual(len(result["overlap_checks"]), 4)
        self.assertTrue(all(row["matches"] for row in result["overlap_checks"]))

    def test_q4_requires_exact_ytd_duration_and_source_provenance(self):
        annual = income_record(2024, 12, "20250311")
        ytd = income_record(2024, 9, "20241114")
        for kind in ("missing", "quarter_only", "no_hash", "duplicate"):
            changed = deepcopy(ytd)
            if kind == "missing": changed["facts"].pop()
            elif kind == "quarter_only": changed["facts"][0]["start"] = "2024-07-01"
            elif kind == "no_hash": changed.pop("sha256")
            else: changed["facts"].append(deepcopy(changed["facts"][0]))
            with self.subTest(kind=kind):
                self.assertEqual(dart.q4_reconciliation(annual, [annual, changed], date(2025, 3, 12))["status"], "unavailable")

    def test_q4_negative_difference_not_clamped_and_direct_quarter_supported(self):
        annual = income_record(2024, 12, "20250311")
        ytd = income_record(2024, 9, "20241114", (900, 150), (800, 70))
        result = dart.q4_reconciliation(annual, [annual, ytd], date(2025, 3, 12))
        self.assertEqual(result["candidate"]["operating_income_current_krw"], -30)
        self.assertFalse(result["candidate"]["earnings_improved"])
        for row in annual["facts"]:
            row["start"] = row["start"].replace("-01-01", "-10-01")
        direct = dart.q4_reconciliation(annual, [annual], date(2025, 3, 12))
        self.assertEqual(direct["status"], "available")
        self.assertEqual(direct["method"], "direct_single_receipt")

    def test_collect_reconciles_q4_after_all_receipts_without_certifying_candidate(self):
        annual = dart.filing_metadata(filing("20250311000001", "20250311", "사업보고서 (2024.12)"))
        ytd = dart.filing_metadata(filing("20250311000002", "20250311", "분기보고서 (2024.09)"))
        annual_xml = xbrl().replace(b"-03-31", b"-12-31")
        ytd_xml = (xbrl().replace(b"-03-31", b"-09-30").replace(b">1200<", b">900<")
                   .replace(b">120<", b">70<").replace(b">1000<", b">800<").replace(b">100<", b">70<"))
        client = Mock()
        client.request.side_effect = lambda endpoint, params: archive(annual_xml if params["rcept_no"] == annual["rcept_no"] else ytd_xml)
        with tempfile.TemporaryDirectory() as name, patch("builtins.print"), patch.object(dart, "list_filings", return_value=[annual, ytd]):
            result = dart.collect(client, "00126380", date(2024, 1, 1), date(2025, 12, 31), Path(name))
            self.assertEqual(result["derived_unverified_q4"], 1)
            self.assertEqual(result["comparable_quarters"], 0)
            self.assertFalse(result["strategy_backtest_ready"])
            saved = json.loads((Path(name) / (annual["rcept_no"] + ".json")).read_text())
            self.assertEqual(saved["quarterly_growth"]["candidate"]["revenue_current_krw"], 300)

    def test_research_annual_uses_same_receipt_full_year_and_quarter_remains_yoy(self):
        annual = income_record(2024, 12, "20250311")
        result = dart.research_growth(annual["facts"], "2024-12-31")
        self.assertEqual(result["period_kind"], "annual")
        self.assertEqual(result["method"], "same_receipt")
        self.assertEqual(result["comparison"], "year_over_year")
        self.assertEqual(result["revenue_current_krw"], 1200)
        self.assertTrue(result["earnings_improved"])
        facts = dart.consolidated_facts(archive(xbrl()), "00126380")
        quarter = dart.research_growth(facts, "2024-03-31")
        self.assertEqual(quarter["period_kind"], "quarter")
        self.assertEqual(quarter["revenue_prior_year_krw"], 1000)
        self.assertEqual(dart.research_growth(facts, "2024-12-31")["status"], "unavailable")
        annual["facts"].pop()
        self.assertEqual(dart.research_growth(annual["facts"], "2024-12-31")["status"], "unavailable")

    def test_research_latest_asof_keeps_missing_correction_and_never_derives_q4(self):
        annual = income_record(2024, 12, "20250311")
        correction = income_record(2024, 12, "20250320", correction=True)
        correction.pop("facts")
        rows = [annual, correction]
        frozen = deepcopy(rows)
        with patch.object(dart, "q4_reconciliation", side_effect=AssertionError("Q4 not research")):
            self.assertIsNone(dart.latest_asof(rows, date(2025, 3, 11), research=True))
            before = dart.latest_asof(rows, date(2025, 3, 20), research=True)
            self.assertEqual(before["research_growth"]["revenue_current_krw"], 1200)
            after = dart.latest_asof(rows, date(2025, 3, 21), research=True)
            self.assertEqual(after["rcept_no"], correction["rcept_no"])
            self.assertEqual(after["research_growth"]["status"], "unavailable")
        self.assertEqual(rows, frozen)

    def test_compact_collection_preserves_source_and_instance_hashes_and_resumes(self):
        packed = BytesIO()
        instance = xbrl().replace(b"-03-31", b"-12-31")
        with zipfile.ZipFile(packed, "w") as bundle:
            bundle.writestr("nested/instance.xbrl", instance)
            bundle.writestr("labels.xml", b"<labels>not retained</labels>")
        original = packed.getvalue()
        annual = filing("20250311000001", "20250311", "사업보고서 (2024.12)")
        client = Mock()
        client.request.return_value = original
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"), \
                patch.object(dart, "list_filings", side_effect=lambda *args: [dart.filing_metadata(annual)]), \
                patch.object(dart, "q4_reconciliation", side_effect=AssertionError("Q4 not research")):
            output = Path(directory)
            result = dart.collect(client, "00126380", date(2025, 1, 1), date(2025, 12, 31), output,
                                  compact=True, research=True)
            row = result["records"][0]
            stored = output / "20250311000001.instance.zip"
            self.assertEqual(row["archive"], stored.name)
            self.assertEqual(row["sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(row["stored_sha256"], hashlib.sha256(stored.read_bytes()).hexdigest())
            self.assertEqual(row["instance_sha256"], hashlib.sha256(instance).hexdigest())
            self.assertEqual(row["instance_name"], "nested/instance.xbrl")
            self.assertEqual(row["storage_limitations"], [dart.COMPACT_LIMITATION])
            self.assertFalse((output / "20250311000001.zip").exists())
            self.assertFalse((output / "20250311000001.json").exists())
            with zipfile.ZipFile(stored) as bundle:
                self.assertEqual(bundle.namelist(), ["nested/instance.xbrl"])
                self.assertEqual(bundle.read(row["instance_name"]), instance)
            self.assertEqual(result["comparable_research_reports"], 1)
            self.assertEqual(result["comparable_quarters"], 0)
            self.assertEqual(result["derived_unverified_q4"], 0)
            before = stored.read_bytes()
            resumed = dart.collect(client, "00126380", date(2025, 1, 1), date(2025, 12, 31), output,
                                   compact=True, research=True)
            self.assertEqual(client.request.call_count, 1)
            self.assertEqual(stored.read_bytes(), before)
            self.assertEqual(resumed["records"][0]["sha256"], row["sha256"])
            self.assertEqual(resumed["records"][0]["research_growth"], row["research_growth"])

    def test_compact_resume_rejects_missing_metadata_and_changed_hash_or_member(self):
        client = Mock()
        client.request.return_value = archive(xbrl())
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"), \
                patch.object(dart, "list_filings", side_effect=lambda *args: [dart.filing_metadata(filing())]):
            output = Path(directory)
            dart.collect(client, "00126380", date(2024, 1, 1), date(2024, 12, 31), output, compact=True)
            metadata = output / "20240516001421.instance.json"
            original = json.loads(metadata.read_text())
            for field in ("sha256", "stored_sha256", "instance_sha256", "instance_name", "archive", "corp_code", "storage_mode"):
                changed = {**original, field: "invalid"}
                metadata.write_text(json.dumps(changed))
                with self.subTest(field=field), self.assertRaisesRegex(dart.DartError, "provenance mismatch"):
                    dart.collect(client, "00126380", date(2024, 1, 1), date(2024, 12, 31), output, compact=True)
            metadata.unlink()
            with self.assertRaisesRegex(dart.DartError, "metadata missing"):
                dart.collect(client, "00126380", date(2024, 1, 1), date(2024, 12, 31), output, compact=True)
            self.assertEqual(client.request.call_count, 1)

    def test_compact_rejects_unsafe_instance_and_multiple_instances(self):
        with self.assertRaisesRegex(dart.DartError, "unsupported or oversized XML"):
            dart.compact_archive(archive(b'<!DOCTYPE x [<!ENTITY e "secret">]><x/>'))
        packed = BytesIO()
        with zipfile.ZipFile(packed, "w") as bundle:
            bundle.writestr("a.xbrl", xbrl())
            bundle.writestr("b.xbrl", xbrl())
        with self.assertRaisesRegex(dart.DartError, "expected one bounded XBRL"):
            dart.compact_archive(packed.getvalue())

    def test_corp_codes_mapping_preserves_empty_and_alphanumeric_associations(self):
        rows = ("<list><corp_code>00126380</corp_code><stock_code>005930</stock_code>"
                "<corp_name>test</corp_name><modify_date>20260906</modify_date></list>"
                "<list><corp_code>99999999</corp_code><stock_code> </stock_code></list>"
                "<list><corp_code>00000001</corp_code><stock_code>0123A0</stock_code></list>")
        def packed(xml):
            result = BytesIO()
            with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("CORPCODE.xml", xml)
            return result.getvalue()
        payload = packed(("<result>" + rows + "</result>").encode())
        result = dart.parse_corp_codes(payload)
        self.assertEqual(result["by_stock_code"]["005930"]["corp_code"], "00126380")
        self.assertIn("0123A0", result["by_stock_code"])
        self.assertEqual(result["unmapped_corp_codes"], ["99999999"])
        self.assertFalse(result["historical_membership"])
        self.assertEqual(result["source_sha256"], hashlib.sha256(payload).hexdigest())
        conflict = rows + "<list><corp_code>00000002</corp_code><stock_code>005930</stock_code></list>"
        with self.assertRaisesRegex(dart.DartError, "ambiguous stock"):
            dart.parse_corp_codes(packed(("<result>" + conflict + "</result>").encode()))
        with self.assertRaisesRegex(dart.DartError, "unsupported or oversized XML"):
            dart.parse_corp_codes(packed(b'<!DOCTYPE x [<!ENTITY e "secret">]><result/>'))
        with self.assertRaisesRegex(dart.DartError, "status 020"):
            dart.parse_corp_codes(b"<result><status>020</status></result>")

    def test_corp_codes_endpoint_rejects_compressed_credential(self):
        key = "2" * 40
        packed = BytesIO()
        with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("CORPCODE.xml", b"<result>" + key.encode() + b"</result>")
        client = dart.DartClient(key)
        with patch.object(client._opener, "open", return_value=BytesIO(packed.getvalue())), patch.object(dart.time, "sleep"):
            with self.assertRaisesRegex(dart.DartError, "unsafe DART archive"):
                client.request("corpCode.xml", {})

    def test_persisted_budget_counts_failed_attempt_and_blocks_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            first = dart.DartClient("0" * 40, request_limit=1, budget_file=path)
            with patch.object(first._opener, "open", side_effect=URLError("secret")), patch.object(dart.time, "sleep"):
                with self.assertRaisesRegex(dart.DartError, "DART transport failed"):
                    first.request("list.json", {})
            self.assertEqual(json.loads(path.read_text()), {"request_count": 1})
            second = dart.DartClient("0" * 40, request_limit=1, budget_file=path)
            with patch.object(second._opener, "open") as request:
                with self.assertRaisesRegex(dart.DartError, "budget exhausted"):
                    second.request("corpCode.xml", {})
                request.assert_not_called()
            path.write_text('{"request_count": true}')
            with self.assertRaisesRegex(dart.DartError, "invalid persisted"):
                second.request("list.json", {})

    def test_budget_exhaustion_saves_partial_records_and_compact_resume_finishes(self):
        rows = [filing(), filing("20240601000001", "20240601", "[기재정정]분기보고서 (2024.03)")]
        listing = json.dumps({"status": "000", "total_page": 1, "list": rows}).encode()
        original = archive(xbrl())
        def response(request, **kwargs):
            return BytesIO(listing if "list.json" in request.full_url else original)
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"), patch.object(dart.time, "sleep"):
            output = Path(directory)
            budget = output / "budget.json"
            first = dart.DartClient("0" * 40, request_limit=2, budget_file=budget)
            with patch.object(first._opener, "open", side_effect=response):
                with self.assertRaisesRegex(dart.DartError, "budget exhausted"):
                    dart.collect(first, "00126380", date(2024, 1, 1), date(2024, 12, 31), output,
                                 compact=True, research=True)
            partial = json.loads((output / "result-00126380-2024-01-01-2024-12-31.json").read_text())
            self.assertEqual(partial["status"], "partial")
            self.assertEqual(partial["completed_receipts"], 1)
            self.assertEqual([row["rcept_no"] for row in partial["records"]], [rows[0]["rcept_no"]])
            self.assertEqual(partial["pending_receipts"], [rows[1]["rcept_no"]])
            self.assertFalse(partial["strategy_backtest_ready"])
            second = dart.DartClient("0" * 40, request_limit=4, budget_file=budget)
            with patch.object(second._opener, "open", side_effect=response) as request:
                result = dart.collect(second, "00126380", date(2024, 1, 1), date(2024, 12, 31), output,
                                      compact=True, research=True)
                self.assertEqual(request.call_count, 2)  # One list and only the pending XBRL.
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["receipts"], 2)
            self.assertEqual(result["comparable_research_reports"], 2)
            self.assertEqual(json.loads(budget.read_text())["request_count"], 4)
            self.assertTrue(all(row["sha256"] == hashlib.sha256(original).hexdigest() for row in result["records"]))


if __name__ == "__main__":
    unittest.main()
