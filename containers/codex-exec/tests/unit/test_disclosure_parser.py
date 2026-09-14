import hashlib
import unittest
from datetime import datetime, timezone

from danta.disclosure_parser import parse_official_event


class DisclosureParserTest(unittest.TestCase):
    """Synthetic labelled tables; official production template coverage unverified."""
    def parse(self, text, title="연결 영업실적 공시", **receipt):
        content = text.encode()
        return parse_official_event({"rcept_no": "20260914000001", "report_nm": title, **receipt},
            [{"content": content, "sha256": hashlib.sha256(content).hexdigest()}], "TEST:AAA", datetime(2026, 9, 14, tzinfo=timezone.utc))

    def earnings(self):
        return '<p>연결 기준 (단위: 백만원)</p><table><tr><th>구분</th><th>당기실적 2026.04.01~2026.06.30</th><th>전년동기실적 2025.04.01~2025.06.30</th></tr><tr><td>매출액</td><td>1,200</td><td>1,000</td></tr><tr><td>영업이익</td><td>150</td><td>100</td></tr></table>'

    def test_source_labelled_earnings_and_units(self):
        event, facts, reason = self.parse(self.earnings())
        self.assertEqual(event.facts["revenue_current"], "1200000000")
        self.assertEqual(event.family, "earnings_quality")
        self.assertEqual(event.polarity, "POSITIVE")
        self.assertTrue(all(fact.content_hash == event.source_hash for fact in facts))

    def test_missing_basis_period_unit_and_metric_never_inferred(self):
        text = self.earnings()
        for bad in (text.replace("연결", ""), text.replace("백만원", "USD"), text.replace("2025.04.01", "2025.01.01"), text.replace("영업이익", "순이익")):
            self.assertIsNone(self.parse(bad, title="영업실적 공시")[0])

    def test_correction_requires_parent(self):
        self.assertEqual(self.parse(self.earnings(), title="[정정] 연결 영업실적")[2], "CORRECTION_RELATION_UNRESOLVED")
        event, _, _ = self.parse(self.earnings(), title="[정정] 연결 영업실적", correction_parent_id="20260913000001")
        self.assertEqual(event.correction_of, "dart:20260913000001")

    def test_guidance_is_forecast_with_same_period(self):
        text = self.earnings().replace("당기실적", "변경 전망").replace("전년동기실적", "이전 전망").replace("2025.", "2026.")
        event, _, _ = self.parse(text, title="연결 영업실적 전망")
        self.assertEqual(event.family, "official_guidance")
        self.assertIn("not realized", event.comparison_basis)

    def test_confirmed_contract_has_scale_and_terms(self):
        rows = [("계약금액(원)", "2000000000"), ("최근매출액(원)", "8000000000"), ("계약상대", "합성회사"), ("계약조건", "검수 후 대금 지급"), ("계약기간 시작일", "2026-09-14"), ("계약기간 종료일", "2027-09-13")]
        text = "<table>" + "".join(f"<tr><td>{key}</td><td>{value}</td></tr>" for key, value in rows) + "</table>"
        event, _, _ = self.parse(text, title="단일판매 공급계약 체결")
        self.assertEqual(event.family, "material_contract")
        self.assertEqual(event.facts["contract_amount"], "2000000000")
        self.assertIsNone(self.parse(text, title="업무협약 MOU 계약체결")[0])


if __name__ == "__main__":
    unittest.main()
