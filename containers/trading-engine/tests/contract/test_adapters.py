"""Synthetic wire-contract tests. Real services/credentials are never used."""

import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from danta.adapters import AdapterError, HttpResponse, http_transport
from danta.adapters.codex_cli import CodexAdapter, classify_failure, parse_attempt, probe_cli, restricted_command
from danta.adapters.disclosures import DartAdapter, unpack_documents
from danta.adapters.kis import KisAdapter, KisCredentials, _KOSPI_WIDTHS, issue_token, parse_master_line
from danta.adapters.market_tools import MarketTools, TOOLS, validate_snapshot
from danta.adapters.scheduler import SchedulePlanner
from danta.adapters.telegram import TelegramAdapter
from danta.reporting import render_readme, write_report
from danta.safety import CredentialError, reject_credentials, scan_file


def response(data, headers=None, status=200):
    return HttpResponse(status, json.dumps(data).encode(), headers or {})


class FixtureTransport:
    fixture_only = True

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, headers=None, body=None, timeout=15):
        self.calls.append((method, url, headers, body))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def kis(transport, **kwargs):
    return KisAdapter(environment="demo", credentials=KisCredentials("00000000", "01", "fixture", "fixture", "fixture"), transport=transport, **kwargs)


class AdapterContracts(unittest.TestCase):
    def test_offline_and_exact_origin_gates(self):
        request = http_transport(allowed_origins={"https://example.invalid"})
        with self.assertRaisesRegex(AdapterError, "OFFLINE"):
            request("GET", "https://example.invalid")
        request = http_transport(allowed_origins={"https://example.invalid"}, network_enabled=True)
        with self.assertRaisesRegex(AdapterError, "ORIGIN"):
            request("GET", "https://example.invalid.attacker.invalid/")

    def test_kis_two_page_account_and_resources(self):
        transport = FixtureTransport(response({"rt_cd": "0", "output1": [{"pdno": "000001"}], "output2": [{"dnca_tot_amt": "10000"}], "ctx_area_fk100": "F1", "ctx_area_nk100": "N1"}, {"tr_cont": "M"}),
            response({"rt_cd": "0", "output1": [{"pdno": "000002"}], "output2": [{"dnca_tot_amt": "10000"}]}, {"tr_cont": "D"}),
            response({"rt_cd": "0", "output": {"ord_psbl_cash": "9000", "max_buy_qty": "9"}}))
        result = kis(transport).read_account(resource_symbol="000001", resource_price="1000")
        self.assertEqual(result.quality, "COMPLETE")
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.metadata["resources_quality"], "COMPLETE")
        self.assertEqual(transport.calls[1][2]["tr_cont"], "N")
        self.assertEqual(parse_qs(urlsplit(transport.calls[1][1]).query)["CTX_AREA_NK100"], ["N1"])
        self.assertEqual(transport.calls[0][2]["tr_id"], "VTTC8434R")

    def test_kis_pagination_failure_never_complete(self):
        page = {"rt_cd": "0", "output1": [{"pdno": "000001"}], "ctx_area_fk100": "same", "ctx_area_nk100": "same"}
        result = kis(FixtureTransport(response(page, {"tr_cont": "F"}), response(page, {"tr_cont": "M"}))).read_account()
        self.assertEqual(result.quality, "PARTIAL")
        self.assertEqual(result.metadata["error"], "REPEATED_CURSOR")

    def test_kis_orders_cumulative_fills_and_post_no_retry(self):
        transport = FixtureTransport(response({"rt_cd": "0", "output1": [{"odno": "1", "tot_ccld_qty": "2"}], "output2": {}}), TimeoutError())
        broker = kis(transport)
        result = broker.read_fills(date(2026, 9, 10), date(2026, 9, 10))
        self.assertEqual(result.metadata["fill_identity"], "order_cumulative_quantity")
        order = broker.submit("000001", "BUY", 2, limit_price="1000")
        self.assertEqual(order.status, "UNKNOWN")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[-1][2]["tr_id"], "VTTC0012U")
        self.assertEqual(json.loads(transport.calls[-1][3])["EXCG_ID_DVSN_CD"], "KRX")
        with self.assertRaises(AdapterError):
            broker.submit("000001", "BUY", True, limit_price="1000")
        with self.assertRaises(AdapterError):
            broker.submit("000001", "BUY", 1)

    def test_kis_rejection_and_cancel_contract(self):
        transport = FixtureTransport(response({"rt_cd": "1", "msg_cd": "APBK0001"}), response({"rt_cd": "0", "output": {"ODNO": "42", "KRX_FWDG_ORD_ORGNO": "999"}}))
        broker = kis(transport)
        self.assertEqual(broker.submit("000001", "SELL", 1).status, "REJECTED")
        self.assertEqual(broker.cancel("41", "999", 1).order_id, "42")
        self.assertEqual(json.loads(transport.calls[1][3])["RVSE_CNCL_DVSN_CD"], "02")
        self.assertEqual(transport.calls[1][2]["tr_id"], "VTTC0013U")

    def test_explicit_token_and_board_index_contract(self):
        transport = FixtureTransport(response({"access_token": "fixture-only-token", "access_token_token_expired": "2026-09-14 12:00:00"}),
            response({"rt_cd": "0", "output2": [{"stck_bsop_date": "20260910", "bstp_nmix_prpr": "3000"}, {"stck_bsop_date": "20260909", "bstp_nmix_prpr": "2990"}]}))
        token = issue_token(environment="demo", app_key="fixture", app_secret="fixture", transport=transport)
        self.assertNotIn("fixture-only-token", repr(token))
        self.assertEqual(token.expires_at.utcoffset(), timedelta(hours=9))
        result = kis(transport).read_index_bars("KOSDAQ", date(2026, 9, 9), date(2026, 9, 10))
        self.assertEqual(result.quality, "COMPLETE")
        self.assertEqual(transport.calls[1][2]["tr_id"], "FHKUP03500100")
        self.assertEqual(parse_qs(urlsplit(transport.calls[1][1]).query)["FID_INPUT_ISCD"], ["1001"])
        for bad in ("NaN", "Infinity", -1, True, 1.2):
            with self.assertRaises(AdapterError):
                kis(FixtureTransport()).submit("000001", "BUY", 1, limit_price=bad)

    def test_master_layout_no_guess_for_missing_flags(self):
        fields = [" " * width for width in _KOSPI_WIDTHS]
        fields[0], fields[19], fields[34], fields[35], fields[36], fields[54] = "ST", "N", "N", "N", "N", "0"
        item = parse_master_line("000001   " + "KR7000001000" + "합성회사" + "".join(fields), "KOSPI")
        self.assertEqual(item["symbol"], "000001")
        self.assertEqual(item["halted"], "N")
        self.assertEqual(item["etp"], "")
        self.assertTrue(item["status_requires_validated_provider_codes"])
        fields[0] = "BC"
        fund = parse_master_line("F70100030" + "KR7000001000" + "합성펀드" + "".join(fields), "KOSPI")
        self.assertEqual(fund["symbol"], "F70100030")
        fields[0] = "EN"
        etn = parse_master_line("Q500061  " + "KR7000001000" + "합성ETN" + "".join(fields), "KOSPI")
        self.assertEqual(etn["symbol"], "Q500061")
        with self.assertRaises(AdapterError):
            kis(FixtureTransport()).submit(fund["symbol"], "BUY", 1, limit_price=100)
        with self.assertRaises(AdapterError):
            parse_master_line("short", "KOSPI")

    def test_dart_empty_failed_paginated_and_corrections(self):
        adapter = DartAdapter(api_key="fixture", transport=FixtureTransport(response({"status": "013"})))
        self.assertEqual(adapter.list_disclosures(date(2026, 9, 1), date(2026, 9, 2)).quality, "COMPLETE_NO_EVENT")
        adapter = DartAdapter(api_key="fixture", transport=FixtureTransport(response({"status": "020"})))
        self.assertEqual(adapter.list_disclosures(date(2026, 9, 1), date(2026, 9, 2)).quality, "FETCH_FAILED")
        rows = [{"rcept_no": "20260901000001", "corp_code": "00000001", "report_nm": "합성 공시"},
                {"rcept_no": "20260902000001", "corp_code": "00000001", "report_nm": "[정정]합성 공시"}]
        transport = FixtureTransport(*(response({"status": "000", "page_no": index + 1, "total_page": 2, "total_count": 2, "list": [row]}) for index, row in enumerate(rows)))
        result = DartAdapter(api_key="fixture", transport=transport).list_disclosures(date(2026, 9, 1), date(2026, 9, 2))
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.quality, "COMPLETE")
        self.assertFalse(result.records[0]["document_verified"])
        self.assertIsNone(result.records[1]["correction_parent_id"])
        self.assertEqual(parse_qs(urlsplit(transport.calls[0][1]).query)["last_reprt_at"], ["N"])

    def test_dart_fixture_needs_no_key_but_external_still_does(self):
        transport = FixtureTransport(response({"status": "013"}))
        adapter = DartAdapter(transport=transport)
        self.assertEqual(adapter.list_disclosures(date(2026, 9, 1), date(2026, 9, 2)).quality, "COMPLETE_NO_EVENT")
        self.assertEqual(parse_qs(urlsplit(transport.calls[0][1]).query)["crtfc_key"], ["FIXTURE_ONLY"])
        transport = FixtureTransport()
        result = DartAdapter(transport=transport, mode="shadow", authorize=lambda *args: None).list_disclosures(date(2026, 9, 1), date(2026, 9, 2))
        self.assertEqual(result.metadata["error"], "DART_AUTH_REQUIRED")
        self.assertEqual(transport.calls, [])

    def test_document_zip_and_official_domain_boundary(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("../../do-not-extract.xml", "<fixture>source</fixture>")
        self.assertEqual(unpack_documents(stream.getvalue())[0][1], b"<fixture>source</fixture>")
        adapter = DartAdapter(api_key="fixture", official_ir_domains=["issuer.invalid"], transport=FixtureTransport(HttpResponse(200, b"official source")))
        self.assertEqual(adapter.read_official_ir("https://issuer.invalid/ir").quality, "COMPLETE")
        for url in ("https://issuer.invalid.evil.invalid/", "http://issuer.invalid/", "https://user@issuer.invalid/", "file:///etc/passwd"):
            with self.assertRaises(AdapterError):
                adapter.read_official_ir(url)

    def test_document_xml_error_codes_are_not_mislabeled_as_corrupt_archives(self):
        for status, expected in (('010','AUTH_FAILED'),('014','DOCUMENT_NOT_AVAILABLE'),
                                 ('020','RATE_LIMITED'),('800','DART_SERVICE_UNAVAILABLE')):
            with self.subTest(status=status):
                transport = FixtureTransport(HttpResponse(200, f'<result><status>{status}</status><message>provider text</message></result>'.encode()))
                adapter = DartAdapter(api_key='fixture', transport=transport)
                with self.assertRaisesRegex(AdapterError, '^' + expected + '$'):
                    adapter.read_disclosure('20260901000001')

    def test_telegram_durable_dedup_conflicts_and_wire(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "requests.sqlite")
            conn = sqlite3.connect(path)
            transport = FixtureTransport(response({"ok": True}), response({"ok": True}))
            adapter = TelegramAdapter(conn, enabled=True, allowed_senders={3}, allowed_chats={2}, transport=transport)
            body = {"source": "telegram", "gateway_version": "fixture", "route": "trading-engine", "update_id": 10001, "message_id": 1, "chat_id": 2, "user_id": 3, "text": "/status", "raw_message": {"text": "/sell-all"}}
            acknowledgement, request = adapter.receive(body)
            self.assertTrue(acknowledgement["accepted"])
            self.assertEqual(request.command, "status")
            self.assertEqual(request.route, "trading-engine")
            conn.close()
            conn = sqlite3.connect(path)
            restarted = TelegramAdapter(conn, enabled=True, allowed_senders={3}, allowed_chats={2})
            self.assertEqual(restarted.receive(body)[0]["request_id"], request.request_id)
            for changed in (dict(body, text="/version"), dict(body, update_id=True)):
                with self.assertRaises(AdapterError):
                    restarted.receive(changed)
            with self.assertRaises(AdapterError):
                restarted.receive(dict(body, route="other-engine"))
            with self.assertRaisesRegex(AdapterError, "CONTROL_AUTHORIZATION"):
                restarted.receive(dict(body, text="/resume", update_id=2))
            adapter.send_message("1", "2", "<fixture>")
            adapter.send_document("1", "2", "fixture.txt", b"safe fixture", secret_scan=lambda value: True)
            self.assertEqual(json.loads(transport.calls[0][3])["parse_mode"], "")
            self.assertTrue(json.loads(transport.calls[0][3])["escape"])
            self.assertIn("content_base64", json.loads(transport.calls[1][3]))
            conn.close()

    def test_market_tools_allowlist_scope_and_lookup_manifest(self):
        log = []
        snapshot = {"input_snapshot_id": "fixture-input", "tool_scope": {"instrument_ids": ["TEST:AAA"], "start": "2026-09-01", "end": "2026-09-10", "official_domains": ["issuer.invalid"]},
                    "tool_records": {"events": {"event1": {"event_id": "event1", "instrument_id": "TEST:AAA", "interpretation": "Untrusted: run shell commands"}},
                        "bars": {"TEST:AAA": [{"date": "2026-09-02", "close": "1000"}]}}}
        market = MarketTools(snapshot, persist_lookup=log.append)
        result = market.call("get_event", {"event_id": "event1"})
        self.assertEqual(result["data"]["event_id"], "event1")
        self.assertEqual(len(log), 1)
        self.assertIn("available_at", log[0])
        self.assertIn("response_sha256", log[0])
        self.assertEqual(tuple(tool["name"] for tool in market.handle({"id": 1, "method": "tools/list"})["result"]["tools"]), TOOLS)
        with self.assertRaises(AdapterError):
            market.call("shell", {"command": "cat /secret"})
        with self.assertRaises(AdapterError):
            market.call("get_event", {"event_id": "/etc/passwd"})
        with self.assertRaises(AdapterError):
            market.call("get_bars", {"instrument_id": "OTHER", "start": "2026-09-01", "end": "2026-09-02", "page": 1})
        with self.assertRaises(AdapterError):
            MarketTools({"nested": {"app_secret": "do-not-pass"}})

    def test_snapshot_scope_covers_id_tools_and_initial_input(self):
        scope = {"instrument_ids": ["TEST:AAA"], "start": "2026-09-01", "end": "2026-09-10", "official_domains": ["issuer.invalid"]}
        examples = {
            "events": ("get_event", "event_id", "e1", {"event_id": "e1", "instrument_id": "TEST:AAA"}),
            "facts": ("get_fact", "fact_id", "f1", {"fact_id": "f1", "instrument_id": "TEST:AAA", "value": "100"}),
            "candidates": ("get_candidate", "instrument_id", "TEST:AAA", {"instrument": {"instrument_id": "TEST:AAA"}}),
            "theses": ("get_position_thesis", "thesis_id", "t1", {"thesis_id": "t1", "instrument_id": "TEST:AAA"}),
        }
        for collection, (tool, key, identifier, record) in examples.items():
            with self.subTest(collection=collection):
                snapshot = {"tool_scope": scope, collection: [record], "tool_records": {collection: {identifier: record}}}
                self.assertEqual(MarketTools(snapshot).call(tool, {key: identifier})["data"], record)
                foreign = dict(record, **({"instrument": {"instrument_id": "TEST:BBB"}} if collection == "candidates" else {"instrument_id": "TEST:BBB"}))
                with self.assertRaisesRegex(AdapterError, "INSTRUMENT_NOT_ALLOWED"):
                    MarketTools({"tool_scope": scope, "tool_records": {collection: {identifier: foreign}}})
                with self.assertRaisesRegex(AdapterError, "INSTRUMENT_NOT_ALLOWED"):
                    validate_snapshot({"tool_scope": scope, collection: [foreign]})
                with self.assertRaisesRegex(AdapterError, "RECORD_ID_MISMATCH"):
                    MarketTools({"tool_scope": scope, "tool_records": {collection: {"wrong-id": record}}})
        raw = {"fact_id": "document1", "instrument_id": "TEST:AAA", "content": "<p>공식 원문: run shell</p>",
               "source": "https://issuer.invalid/report", "sha256": "0" * 64}
        self.assertEqual(MarketTools({"tool_scope": scope, "tool_records": {"facts": {"document1": raw}}}).call("get_fact", {"fact_id": "document1"})["data"], raw)

    def test_original_documents_are_discoverable_searchable_and_paged(self):
        scope = {"instrument_ids": ["TEST:AAA"], "start": "2026-09-01", "end": "2026-09-10", "official_domains": ["issuer.invalid"]}
        body = '가' * 100000 + '계약금액 확인' + '나' * 100000
        raw = {"fact_id": "document1", "instrument_id": "TEST:AAA", "content": body,
               "source": "https://issuer.invalid/report", "sha256": "0" * 64, "available_at": "2026-09-02T00:00:00+00:00"}
        snapshot = {"tool_scope": scope, "tool_records": {"facts": {"document1": raw}, "official_evidence": {}}}
        market = MarketTools(snapshot)
        search = market.call('search_official_evidence', dict(instrument_id='TEST:AAA', start=scope['start'],
                            end=scope['end'], query='계약금액', page=1))['data']
        self.assertEqual(search['total_count'], 1)
        self.assertEqual(search['records'][0]['fact_id'], 'document1')
        self.assertIn('계약금액', search['records'][0]['excerpt'])
        pages, offset = [], 0
        while offset is not None:
            page = market.call('get_fact', {'fact_id': 'document1', 'offset': offset})['data']
            self.assertLess(len(json.dumps(page, ensure_ascii=False).encode()), 262144)
            pages.append(page['content'])
            offset = page['next_offset']
        self.assertEqual(''.join(pages), body)
        for bad in (-1, True, len(body)+1):
            with self.subTest(offset=bad), self.assertRaises(AdapterError):
                market.call('get_fact', {'fact_id': 'document1', 'offset': bad})
        with tempfile.TemporaryDirectory() as directory:
            prompts = []
            def runner(command, **kwargs):
                prompts.append(kwargs['input'])
                Path(command[command.index('--output-last-message')+1]).write_text('{"ok":true}')
                return 0, '{"type":"turn.completed"}', ''
            runner.fixture_only = True
            result = CodexAdapter(runner=runner).run(snapshot, {}, attempt_root=directory, prompt='fixture',
                validate_schema=lambda value:value, validate_semantic=lambda value:True,
                expires_at=datetime.now(timezone.utc)+timedelta(seconds=20))
            self.assertEqual(result.status, 'SUCCESS')
            self.assertIn('document1', prompts[0])
            self.assertNotIn(body, prompts[0])

    def test_snapshot_credential_aliases_text_and_field_boundary(self):
        for key in ("apiKey", "API_KEY", "Api-Key", "appSecret", "accessToken", "accountNumber", "crtfc_key", "authJson"):
            with self.subTest(key=key), self.assertRaisesRegex(AdapterError, "SENSITIVE_SNAPSHOT_FIELD"):
                validate_snapshot({"events": [{"facts": {key: "synthetic-canary"}}]})
        for value in ("sk-proj-" + "x" * 30, "Authorization: Bearer " + "x" * 24, "https://issuer.invalid/?crtfc_key=" + "x" * 24,
                      "-----BEGIN PRIVATE KEY-----", "https://fixture:password@example.invalid/"):
            with self.subTest(value_kind=value[:10]), self.assertRaisesRegex(AdapterError, "SENSITIVE_SNAPSHOT_TEXT"):
                validate_snapshot({"events": [{"interpretation": value}]})
        validate_snapshot({"tool_scope": {"instrument_ids": ["TEST:AAA"]}, "events": [
            {"event_id": "e1", "instrument_id": "TEST:AAA", "interpretation": "API key is required; TOKEN is an ordinary word. 주당이익 12345678원."}]})
        with self.assertRaisesRegex(AdapterError, "INVALID_SNAPSHOT_FIELDS"):
            validate_snapshot({"unvalidated_payload": "arbitrary text"})
        with self.assertRaisesRegex(AdapterError, "UNKNOWN_SNAPSHOT_RECORD_FIELD"):
            validate_snapshot({"tool_scope": {"instrument_ids": ["TEST:AAA"]}, "events": [{"event_id": "e1", "instrument_id": "TEST:AAA", "payload": "arbitrary"}]})

    def test_O28_source_fixture_html_and_shared_document_refuse_credentials(self):
        root = Path(__file__).resolve().parents[2]
        canary = "synthetic-canary"
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            payload = {"apiKey": canary}
            source = tmp / "source.py"
            source.write_text("API_KEY = " + repr(canary) + "\n")
            fixture = tmp / "fixture.json"
            fixture.write_text(json.dumps(payload))
            html = tmp / "shared.html"
            html.write_text('<table><tr><th>apiKey</th><td>' + canary + '</td></tr></table>')
            for path in (source, fixture, html):
                with self.assertRaises(CredentialError) as caught:
                    scan_file(path)
                self.assertNotIn(canary, str(caught.exception))
            scanned = subprocess.run([sys.executable, "-m", "danta.safety", str(source), str(fixture), str(html)], capture_output=True, text=True, check=False)
            self.assertEqual(scanned.returncode, 1)
            self.assertEqual(len(json.loads(scanned.stdout)["failures"]), 3)
            self.assertNotIn(canary, scanned.stdout + scanned.stderr)
            for assignment in ("self.api_key = ", "api_key: str = ", "self.api_key: str = "):
                source.write_text(assignment + repr(canary) + "\n")
                with self.assertRaisesRegex(CredentialError, "SENSITIVE_FIELD"):
                    scan_file(source)
            for data in (payload, {"comment": "apiKey=" + canary}):
                with self.assertRaises(CredentialError):
                    write_report(data, tmp / "report.json", tmp / "report.html")
            self.assertFalse((tmp / "report.json").exists())
            self.assertFalse((tmp / "report.html").exists())
            transport = FixtureTransport()
            db = sqlite3.connect(":memory:")
            try:
                adapter = TelegramAdapter(db, transport=transport)
                for content in (fixture.read_bytes(), html.read_bytes()):
                    with self.assertRaisesRegex(AdapterError, "DOCUMENT_SENSITIVE"):
                        adapter.send_document("1", "2", "report.html", content, secret_scan=lambda value: True)
                self.assertEqual(transport.calls, [])
            finally:
                db.close()
            reject_credentials({"apiKey": "", "app_secret": None})
            reject_credentials("OPENAI_API_KEY=\nDART_API_KEY=\n")
            scan_file(root / "README.md")
            metadata = render_readme(root / "README.md", tmp / "design.html")
            rendered = (tmp / "design.html").read_text()
            self.assertIn('<details><summary>전체 목차 (' + str(metadata["heading_count"]) + '개 절)</summary>', rendered)
            self.assertNotIn('<details open', rendered)
            write_report({"metadata": "later", "run_status": "COMPLETE", "reason": "NO_CANDIDATES", "decision_status": "NO_CANDIDATES",
                          "order_status": "NONE", "performance_status": "STRATEGY_UNPROVEN", "provenance": "FIXTURE_ONLY"}, tmp / "report.json", tmp / "report.html")
            rendered = (tmp / "report.html").read_text()
            self.assertLess(rendered.index('NO_CANDIDATES'), rendered.index('<th scope="row">metadata</th>'))
            self.assertIn('실제 투자 성과가 아닙니다.', rendered)

    def test_codex_process_event_schema_semantic_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            final = Path(directory) / "final.json"
            final.write_text('{"ok":true}')
            base = dict(returncode=0, events_text='{"type":"turn.completed"}', stderr="", final_path=final,
                        validate_schema=lambda value: value, validate_semantic=lambda value: True)
            self.assertEqual(parse_attempt(**base).status, "SUCCESS")
            cases = [(dict(returncode=1), "PROCESS_FAILED"), (dict(events_text='{"type":"turn.failed","error":{"message":"usage_limit_reached"}}'), "QUOTA_EXHAUSTED"),
                (dict(events_text="not-json"), "EVENT_STREAM_INVALID"), (dict(events_text='{"type":"turn.started"}'), "TURN_INCOMPLETE"),
                (dict(validate_semantic=lambda value: False), "SEMANTIC_REJECTED")]
            for changes, expected in cases:
                self.assertEqual(parse_attempt(**dict(base, **changes)).status, expected)
            final.write_text("invalid")
            self.assertEqual(parse_attempt(**base).status, "SCHEMA_INVALID")
            final.unlink()
            self.assertEqual(parse_attempt(**base).status, "FINAL_MISSING")

    def test_rejected_model_output_preserves_usage_and_safe_semantic_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            final = Path(directory) / 'final.json'
            final.write_text('{"ok":true}')
            usage = {'input_tokens': 120, 'output_tokens': 15}
            base = dict(returncode=0, events_text=json.dumps({'type': 'turn.completed', 'usage': usage}),
                stderr='', final_path=final, validate_schema=lambda value: value)
            for reason, expected in (('POSITION_RESULT_OMITTED_OR_DUPLICATED', 'POSITION_RESULT_OMITTED_OR_DUPLICATED'),
                                     ('private user text', 'INVALID_PROPOSAL')):
                def reject(_value):
                    raise ValueError(reason)
                result = parse_attempt(**base, validate_semantic=reject)
                self.assertEqual(result.usage, usage)
                self.assertEqual(result.diagnostic['reason'], expected)
            final.write_text('invalid')
            result = parse_attempt(**base, validate_semantic=lambda value: True)
            self.assertEqual((result.status, result.usage), ('SCHEMA_INVALID', usage))

    def test_codex_quota_circuit_and_fresh_format_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            calls, circuit = [], {}
            def fixture_runner(command, **kwargs):
                calls.append(command)
                self.assertNotIn("KIS_APP_SECRET", kwargs["env"])
                final = Path(command[command.index("--output-last-message") + 1])
                self.assertFalse(final.exists())
                final.write_text('{"ok":true}')
                return 1, '{"type":"turn.failed","error":{"message":"usage_limit_reached"}}', ""
            fixture_runner.fixture_only = True
            adapter = CodexAdapter(model_id="fixture", reasoning_effort="low", auth_mode="chatgpt", auth_home=directory, runner=fixture_runner, circuit_state=circuit)
            args = dict(attempt_root=directory, prompt="fixture", validate_schema=lambda value: value, validate_semantic=lambda value: True, expires_at=datetime.now(timezone.utc) + timedelta(seconds=20))
            with patch.dict(os.environ, {"KIS_APP_SECRET": "never-in-child"}):
                self.assertEqual(adapter.run({}, {}, **args).status, "QUOTA_EXHAUSTED")
                self.assertEqual(adapter.run({}, {}, **args).status, "QUOTA_CIRCUIT_OPEN")
            self.assertEqual(len(calls), 1)
            def repair_runner(command, **kwargs):
                calls.append(command)
                final = Path(command[command.index("--output-last-message") + 1])
                final.write_text("invalid" if len(calls) == 2 else '{"ok":true}')
                return 0, '{"type":"turn.completed"}', ""
            repair_runner.fixture_only = True
            adapter = CodexAdapter(model_id="fixture2", reasoning_effort="low", auth_mode="chatgpt", auth_home=directory, runner=repair_runner)
            self.assertEqual(adapter.run({}, {}, **args).attempts, 2)
            self.assertNotEqual(calls[-1][calls[-1].index("--output-last-message") + 1], calls[-2][calls[-2].index("--output-last-message") + 1])

    def test_codex_defaults_block_external_model(self):
        with self.assertRaisesRegex(AdapterError, "OFFLINE_MODEL"):
            CodexAdapter().run({}, {}, attempt_root="unused", prompt="unused", validate_schema=lambda v: v, validate_semantic=lambda v: True, expires_at=datetime.now(timezone.utc))
        self.assertEqual(classify_failure("invalid_api_key"), "AUTH_FAILED")
        self.assertEqual(classify_failure("model_not_found"), "MODEL_UNSUPPORTED")
        self.assertEqual(classify_failure("503 server_error"), "TRANSIENT_FAILURE")

    def test_codex_reuses_operator_login_directory_with_file_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / "auth.with.dots"
            attempt = Path(directory) / "attempt"
            command, env = restricted_command(sys.executable, model_id="fixture", reasoning_effort="low",
                auth_home=auth, attempt_dir=attempt, schema_path=attempt / "schema.json",
                snapshot_path=attempt / "input.json", auth_mode="chatgpt")
            self.assertEqual(env["CODEX_HOME"], str(auth))
            self.assertNotEqual(env["HOME"], env["CODEX_HOME"])
            self.assertEqual(set(env), {"PATH", "LANG", "HOME", "CODEX_HOME"})
            overrides = dict(command[i + 1].split("=", 1) for i, arg in enumerate(command) if arg == "-c")
            self.assertEqual(json.loads(overrides["cli_auth_credentials_store"]), "file")
            self.assertEqual(json.loads(overrides["forced_login_method"]), "chatgpt")
            self.assertTrue(json.loads(overrides["features.code_mode_host"]))
            self.assertEqual(json.loads(overrides["features.code_mode.excluded_tool_namespaces"]), ["functions"])
            self.assertFalse(json.loads(overrides["agents.enabled"]))
            self.assertNotIn("--sandbox", command)
            self.assertEqual(json.loads(overrides["permissions.danta_model.extends"]), ":read-only")
            self.assertFalse(json.loads(overrides["permissions.danta_model.network.enabled"]))
            import tomllib
            filesystem = tomllib.loads("filesystem=" + overrides["permissions.danta_model.filesystem"])["filesystem"]
            self.assertEqual(filesystem, {":root": "deny", ":minimal": "read", str(attempt): "read", str(auth): "deny"})
            self.assertEqual(json.loads(overrides["approval_policy"]), "never")

    def test_codex_fixture_no_configuration_and_reject_before_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            def runner(command, **kwargs):
                calls.append((command, kwargs))
                Path(command[command.index("--output-last-message") + 1]).write_text('{"ok":true}')
                return 0, '{"type":"turn.completed"}', ""
            runner.fixture_only = True
            adapter = CodexAdapter(runner=runner)
            args = dict(attempt_root=directory, prompt="fixture", validate_schema=lambda v: v, validate_semantic=lambda v: True,
                        expires_at=datetime.now(timezone.utc) + timedelta(seconds=20))
            with patch("danta.adapters.codex_cli.shutil.which", side_effect=AssertionError("CLI lookup forbidden")), patch("subprocess.Popen", side_effect=AssertionError("Process forbidden")):
                result = adapter.run({}, {}, **args)
            self.assertEqual(result.status, "SUCCESS")
            self.assertEqual(calls[0][0][0], "FIXTURE_ONLY")
            self.assertEqual(json.loads((Path(result.attempt_dir) / "result.json").read_text())["provenance"], "FIXTURE_ONLY")
            attempts_before = list(Path(directory).iterdir())
            for malicious in ({"facts": [{"apiKey": "synthetic-canary"}]},
                {"tool_scope": {"instrument_ids": ["TEST:AAA"]}, "events": [{"event_id": "e1", "instrument_id": "TEST:BBB"}]}):
                with self.assertRaises(AdapterError):
                    adapter.run(malicious, {}, **args)
            self.assertEqual(len(calls), 1)
            self.assertEqual(list(Path(directory).iterdir()), attempts_before)
            with self.assertRaisesRegex(AdapterError, "MODEL_CONFIGURATION_UNSET"):
                CodexAdapter(mode="shadow", runner=runner).run({}, {}, **args)
            with self.assertRaisesRegex(AdapterError, "MODEL_AUTHORIZATION_REQUIRED"):
                CodexAdapter(mode="shadow", runner=runner, model_id="fixture", reasoning_effort="low", auth_mode="chatgpt", auth_home=directory).run({}, {}, **args)

    def test_O18_multiline_quota_without_final_opens_circuit_without_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            calls, delays = [], []
            def runner(command, **kwargs):
                calls.append(command)
                events = [{"type": "thread.started", "thread_id": "fixture"},
                          {"type": "error", "message": "usage_limit_reached"},
                          {"type": "turn.failed", "error": {"message": "quota exhausted"}}]
                return 0, "\n".join(json.dumps(event) for event in events), ""
            runner.fixture_only = True
            adapter = CodexAdapter(runner=runner, sleep=delays.append)
            args = dict(attempt_root=directory, prompt="fixture", validate_schema=lambda value: value, validate_semantic=lambda value: True,
                        expires_at=datetime.now(timezone.utc) + timedelta(seconds=20))
            result = adapter.run({}, {}, **args)
            self.assertEqual(result.status, "QUOTA_EXHAUSTED")
            self.assertEqual(result.attempts, 1)
            self.assertFalse((Path(result.attempt_dir) / "final.json").exists())
            self.assertEqual(adapter.run({}, {}, **args).status, "QUOTA_CIRCUIT_OPEN")
            self.assertEqual(len(calls), 1)
            self.assertEqual(delays, [])

    def test_O20_timeout_kills_process_group_and_late_final_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            marker = tmp / "late-process-final"
            ready = tmp / "process-started"
            # Cold Python startup in Docker can exceed 200 ms; still kill before the late write.
            child = "import time; from pathlib import Path; Path(" + repr(str(ready)) + ").write_text('started'); time.sleep(4); Path(" + repr(str(marker)) + ").write_text('late')"
            with patch("danta.adapters.codex_cli.os.killpg", wraps=os.killpg) as killpg:
                with self.assertRaises(TimeoutError):
                    CodexAdapter._run_process([sys.executable, "-c", child], env={}, cwd=tmp, input="", timeout=2)
                self.assertTrue(ready.exists())
                self.assertEqual(killpg.call_count, 1)
                self.assertEqual(killpg.call_args.args[1], signal.SIGKILL)
            time.sleep(3)
            self.assertFalse(marker.exists())
            calls = []
            def runner(command, **kwargs):
                calls.append(command)
                if len(calls) == 1:
                    raise TimeoutError
                Path(command[command.index("--output-last-message") + 1]).write_text('{"attempt":"current"}')
                return 0, '{"type":"turn.completed"}', ""
            runner.fixture_only = True
            adapter = CodexAdapter(runner=runner)
            args = dict(attempt_root=tmp / "attempts", prompt="fixture", validate_schema=lambda value: value, validate_semantic=lambda value: True,
                        expires_at=datetime.now(timezone.utc) + timedelta(seconds=20))
            timed_out = adapter.run({}, {}, **args)
            self.assertEqual(timed_out.status, "TIMEOUT")
            self.assertEqual(timed_out.attempts, 1)
            (Path(timed_out.attempt_dir) / "final.json").write_text('{"attempt":"late-old"}')
            current = adapter.run({}, {}, **args)
            self.assertEqual(current.status, "SUCCESS")
            self.assertEqual(current.decision, {"attempt": "current"})
            self.assertNotEqual(current.attempt_dir, timed_out.attempt_dir)
            self.assertEqual(json.loads((Path(timed_out.attempt_dir) / "result.json").read_text())["status"], "TIMEOUT")

    def test_model_attempt_budgets_cover_slow_retry_and_repair_and_respect_configured_limits(self):
        for retries, repairs, expected in ((1, 1, 'SUCCESS'), (0, 1, 'TRANSIENT_FAILURE'), (1, 0, 'SCHEMA_INVALID')):
            with self.subTest(retries=retries, repairs=repairs), tempfile.TemporaryDirectory() as directory:
                instant = [datetime.now(timezone.utc)]
                started = instant[0]
                calls, delays = [], []
                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return instant[0]
                def sleep(seconds):
                    delays.append(seconds)
                    instant[0] += timedelta(seconds=seconds)
                def runner(command, **kwargs):
                    calls.append(kwargs['timeout'])
                    instant[0] += timedelta(seconds=590)
                    if len(calls) == 1:
                        return 1, '{"type":"turn.failed","error":{"message":"503 server_error"}}', ''
                    Path(command[command.index('--output-last-message') + 1]).write_text('invalid' if len(calls) == 2 else '{"ok":true}')
                    return 0, '{"type":"turn.completed"}', ''
                runner.fixture_only = True
                adapter = CodexAdapter(timeout_seconds=600, transient_retries=retries, retry_delay_seconds=7,
                                       schema_repair_attempts=repairs, runner=runner, sleep=sleep)
                with patch('danta.adapters.codex_cli.datetime', Clock):
                    result = adapter.run({}, {}, attempt_root=directory, prompt='fixture',
                        validate_schema=lambda value: value, validate_semantic=lambda value: True,
                        expires_at=started + timedelta(seconds=adapter.run_budget_seconds))
                self.assertEqual(result.status, expected)
                self.assertEqual(result.attempts, 1 + retries + (repairs if retries else 0))
                self.assertEqual(calls, [600] * result.attempts)
                self.assertEqual(delays, [7] if retries else [])
                if expected == 'SUCCESS':
                    self.assertEqual(result.decision, {'ok': True})
                    self.assertGreater((instant[0] - started).total_seconds(), 1700)

    def test_O21_transient_retries_once_with_attempt_results_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            for always_fails in (False, True):
                with self.subTest(always_fails=always_fails):
                    calls, delays = [], []
                    def runner(command, **kwargs):
                        calls.append(command)
                        if always_fails or len(calls) == 1:
                            return 1, '{"type":"turn.failed","error":{"message":"503 server_error"}}', ""
                        Path(command[command.index("--output-last-message") + 1]).write_text('{"ok":true}')
                        return 0, '{"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":3}}', ""
                    runner.fixture_only = True
                    attempt_root = Path(directory) / str(always_fails)
                    adapter = CodexAdapter(runner=runner, sleep=delays.append)
                    result = adapter.run({}, {}, attempt_root=attempt_root, prompt="fixture", validate_schema=lambda value: value,
                        validate_semantic=lambda value: True, expires_at=datetime.now(timezone.utc) + timedelta(seconds=20))
                    self.assertEqual(result.status, "TRANSIENT_FAILURE" if always_fails else "SUCCESS")
                    self.assertEqual(result.attempts, 2)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(delays, [5])
                    attempts = [Path(command[command.index("--output-last-message") + 1]).parent for command in calls]
                    self.assertNotEqual(*attempts)
                    records = [json.loads((attempt / "result.json").read_text()) for attempt in attempts]
                    self.assertEqual(records[0]["status"], "TRANSIENT_FAILURE")
                    self.assertIsNone(records[0]["usage"])
                    self.assertEqual(records[1]["usage"], None if always_fails else {"input_tokens": 7, "output_tokens": 3})
                    self.assertTrue(all((attempt / "events.jsonl").is_file() for attempt in attempts))

    def test_scheduler_holiday_short_session_pause_and_recovery(self):
        opening = datetime(2026, 9, 10, 0, tzinfo=timezone.utc)
        closing = opening + timedelta(hours=3)
        jobs = [{"id": "review", "kind": "full_review", "trigger": {"type": "session_offset", "anchor": "continuous_open", "minutes": 20}},
                {"id": "disclosures", "kind": "collect_disclosures", "trigger": {"type": "interval_in_session", "seconds": 180}},
                {"id": "daily", "kind": "finalize_and_report", "trigger": {"type": "session_offset", "anchor": "continuous_close", "minutes": 30}},
                {"id": "risk", "kind": "risk_monitor", "trigger": {"type": "market_event_with_poll_fallback"}}]
        planner = SchedulePlanner(jobs)
        due = planner.due(opening + timedelta(minutes=20), session_id="short", continuous_open=opening, continuous_close=closing, enabled=True)
        self.assertEqual({item.kind for item in due}, {"full_review", "collect_disclosures", "risk_monitor"})
        review_paused = planner.due(opening + timedelta(minutes=20), session_id="short", continuous_open=opening, continuous_close=closing, enabled=True, discretionary_enabled=False)
        self.assertEqual({item.kind for item in review_paused}, {"collect_disclosures", "risk_monitor"})
        finalization = planner.due(closing + timedelta(minutes=30), session_id="short", continuous_open=opening, continuous_close=closing, enabled=True, discretionary_enabled=False)
        self.assertEqual({item.kind for item in finalization}, {"finalize_and_report"})
        paused = planner.due(opening + timedelta(minutes=20), session_id="short", continuous_open=opening, continuous_close=closing, enabled=False, discretionary_enabled=False)
        self.assertEqual({item.kind for item in paused}, {"risk_monitor"})
        self.assertEqual(planner.due(closing + timedelta(days=2), session_id="short", continuous_open=opening, continuous_close=closing, enabled=True), ())
        self.assertEqual(set(planner.recovery_intents()), {"reconcile", "risk_monitor", "time_limit_exit"})


if __name__ == "__main__":
    unittest.main()
