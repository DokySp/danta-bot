"""Synthetic protocol responses only; no credentials, network, or broker orders."""
import io
import unittest
import zipfile
from datetime import date, datetime
from zoneinfo import ZoneInfo

from danta.adapters import AdapterError, FetchResult, HttpResponse
from danta.config import canonical
from danta.deployment_sources import (completed_sessions, normalize_master_flags, prepare_market_sources,
    read_historical_days, read_open_days, read_sector_map, validate_bar_observations)
from danta.market import SessionCalendar
from danta.models import Session

SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 9, 21, 16, 30, tzinfo=SEOUL)


class Sources:
    max_pages = 10

    def __init__(self):
        self.calls = []
        self.different_board = False
        self.holidays = [{"bass_dt": "20260921", "opnd_yn": "Y"},
                         {"bass_dt": "20260922", "opnd_yn": "Y"},
                         {"bass_dt": "20260923", "opnd_yn": "N"}]
        self.histories = [{"stck_bsop_date": "202609" + day, "bstp_nmix_hgpr": "101",
            "bstp_nmix_lwpr": "99", "bstp_nmix_prpr": "100", "acml_tr_pbmn": "10000"} for day in ("16", "17", "18")]
        content = (b"00025" + "반도체".encode("cp949").ljust(40, b" ") + b"\n"
                   + b"99999" + b" " * 40 + b"\n" + b"EE199" + b"ETN".ljust(40, b" ") + b"\n")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("idxcode.mst", content)
        self.body = archive.getvalue()

    def _request(self, path, tr, params, *, continuation=""):
        self.calls.append((path, tr, params, continuation))
        if path.endswith("chk-holiday"):
            if params["CTX_AREA_NK"]:
                return {"output": self.holidays[2:]}, {}
            return {"output": self.holidays[:2], "ctx_area_fk": "fixture", "ctx_area_nk": "next"}, {"tr_cont": "M"}
        self.assert_index_request(path, tr, params)
        rows = [row for row in self.histories if params["FID_INPUT_DATE_1"] <= row["stck_bsop_date"] <= params["FID_INPUT_DATE_2"]]
        if self.different_board and params["FID_INPUT_ISCD"] == "1001":
            rows = [row for row in rows if row["stck_bsop_date"] != "20260917"]
        return {"output2": rows[-2:]}, {}

    @staticmethod
    def assert_index_request(path, tr, params):
        assert path.endswith("inquire-daily-indexchartprice") and tr == "FHKUP03500100"
        assert params["FID_COND_MRKT_DIV_CODE"] == "U" and params["FID_PERIOD_DIV_CODE"] == "D"

    def _permit(self, operation):
        assert operation == "market_read"

    def transport(self, method, url, *_args):
        assert method == "GET" and url.endswith("/common/master/idxcode.mst.zip")
        return HttpResponse(200, self.body)

    def read_corporations(self):
        return FetchResult(({"symbol": "005930", "corp_code": "00126380"},), "COMPLETE", NOW)


class DeploymentSourcesTests(unittest.TestCase):
    def test_source_preparation_pages_and_keeps_special_hours_unverified(self):
        source = Sources()
        result = prepare_market_sources(source, source, now=NOW, start=date(2026, 9, 16), end=date(2026, 9, 23))
        calendar = result["calendar"]
        sessions = [Session.model_validate_json(canonical(row)) for row in calendar["sessions"]]
        parsed = SessionCalendar(sessions, provenance=calendar["source"], verified=calendar["verified"], synthetic=False)
        self.assertEqual([s.session_id for s in completed_sessions(parsed, NOW)], ["2026-09-16", "2026-09-17", "2026-09-18"])
        self.assertIsNone(parsed.active(NOW))
        self.assertFalse(calendar["special_hours_verified"])
        self.assertTrue(all(not row.hours_verified for row in sessions))
        self.assertEqual(result["normalization"]["instruments"]["sector_by_industry"], {"0025": "반도체"})
        self.assertEqual(result["disclosures"]["instrument_by_corp_code"], {"00126380": "KRX:005930"})
        self.assertFalse(result["normalization"]["bars"]["consistent_ohlc_verified"])
        self.assertEqual(len(source.calls), 6)
        self.assertEqual(source.calls[-1][-1], "N")
        self.assertEqual(source.calls[-2][2]["BASS_DT"], "20260921")
        self.assertEqual(completed_sessions(parsed, datetime(2026, 9, 22, tzinfo=SEOUL))[-1].session_id, "2026-09-21")

    def test_missing_conflicting_or_incomplete_calendar_fails(self):
        for days in ([{"bass_dt": "20260921", "opnd_yn": "Y"}],
                     [{"bass_dt": "20260921", "opnd_yn": "Y"}, {"bass_dt": "20260921", "opnd_yn": "N"}]):
            source = Sources()
            source.holidays = days
            with self.assertRaises(AdapterError):
                read_open_days(source, date(2026, 9, 21), date(2026, 9, 23))
        source = Sources()
        source.different_board = True
        with self.assertRaisesRegex(AdapterError, "INDEX_CALENDAR_BOARDS_DIFFER"):
            read_historical_days(source, date(2026, 9, 16), date(2026, 9, 20))
        source = Sources()
        source.max_pages = 1
        with self.assertRaisesRegex(AdapterError, "PAGE_LIMIT"):
            read_historical_days(source, date(2026, 9, 16), date(2026, 9, 20))

    def test_holiday_stops_at_requested_coverage_despite_future_pages(self):
        source = Sources()
        days, _ = read_open_days(source, date(2026, 9, 21), date(2026, 9, 22))
        self.assertEqual(days, [date(2026, 9, 21), date(2026, 9, 22)])
        self.assertEqual(len(source.calls), 1)

    def test_master_categorical_codes_are_not_generic_booleans(self):
        row = {"group": "ST", "etp": "0", "preferred": "0", "spac": "N",
               "halted": "N", "liquidation": "N", "managed": "N"}
        self.assertEqual(normalize_master_flags(row), {"kind": "common_stock", "status": "NORMAL", "codes_known": True})
        self.assertEqual(normalize_master_flags({**row, "etp": ""}), normalize_master_flags(row))
        self.assertFalse(normalize_master_flags({**row, "group": "EF", "etp": ""})["codes_known"])
        for field, value in (("etp", "5"), ("preferred", "2"), ("spac", "Y"), ("group", "EF")):
            self.assertEqual(normalize_master_flags({**row, field: value})["kind"], "excluded_instrument")
        for field, value in (("etp", "N"), ("preferred", "N"), ("managed", "0")):
            self.assertFalse(normalize_master_flags({**row, field: value})["codes_known"])
        self.assertEqual(normalize_master_flags({**row, "halted": "Y"})["status"], "HALTED")

    def test_domestic_sector_unknown_code_or_missing_name_is_rejected(self):
        for row in (b"0E199" + b"ETN".ljust(40, b" "), b"00025" + b" " * 40):
            source = Sources()
            content = io.BytesIO()
            with zipfile.ZipFile(content, "w") as archive:
                archive.writestr("idxcode.mst", row + b"\n")
            source.body = content.getvalue()
            with self.assertRaisesRegex(AdapterError, "SECTOR_MASTER_CONTRACT_MISMATCH"):
                read_sector_map(source)

    def test_adjusted_bar_snapshot_validation_does_not_claim_historical_pit(self):
        rows = ({"stck_bsop_date": "20260918", "stck_hgpr": "101", "stck_lwpr": "99",
                 "stck_clpr": "100", "acml_tr_pbmn": "5000"},)
        result = FetchResult(rows, "PARTIAL", NOW, metadata={"adjustment": "provider_adjusted", "coverage_requires_calendar": True})
        evidence = validate_bar_observations(result, ["2026-09-18"])
        self.assertTrue(evidence["consistent_ohlc_verified"])
        self.assertFalse(evidence["point_in_time_adjustment_verified"])
        for bad in ({**rows[0], "stck_clpr": "102"}, {**rows[0], "stck_clpr": "NaN"}, {**rows[0], "stck_lwpr": True}):
            with self.assertRaises(AdapterError):
                validate_bar_observations(FetchResult((bad,), "COMPLETE", NOW, metadata=result.metadata), ["2026-09-18"])
        with self.assertRaises(AdapterError):
            validate_bar_observations(result, ["2026-09-17", "2026-09-18"])


if __name__ == "__main__":
    unittest.main()
