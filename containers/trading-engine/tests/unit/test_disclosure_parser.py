import hashlib
import unittest
from datetime import datetime, timezone

from danta.disclosure_parser import correction_reference, parse_official_event, resolve_correction_parent


class DisclosureParserTest(unittest.TestCase):
    """Synthetic values, including the layout observed in DART 20260911800002."""
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

    def test_nonfinancial_plans_and_treasury_contracts_are_observations(self):
        for title in ("투자판단관련주요경영사항(임상시험계획승인신청등결정)",
                      "주요사항보고서(자기주식취득신탁계약체결결정)",
                      "[기재정정]장래사업ㆍ경영계획"):
            with self.subTest(title=title):
                self.assertEqual(self.parse(self.earnings(), title=title)[2], "OBSERVATION_ONLY_EVENT_FAMILY")

    def test_confirmed_contract_has_scale_and_terms(self):
        rows = [("계약금액(원)", "2000000000"), ("최근매출액(원)", "8000000000"), ("계약상대", "합성회사"), ("계약조건", "검수 후 대금 지급"), ("계약기간 시작일", "2026-09-14"), ("계약기간 종료일", "2027-09-13")]
        text = "<table>" + "".join(f"<tr><td>{key}</td><td>{value}</td></tr>" for key, value in rows) + "</table>"
        event, _, _ = self.parse(text, title="단일판매 공급계약 체결")
        self.assertEqual(event.family, "material_contract")
        self.assertEqual(event.facts["contract_amount"], "2000000000")
        self.assertIsNone(self.parse(text, title="업무협약 MOU 계약체결")[0])

    def contract_with_spans(self):
        return '''<table>
<tr><td rowspan="2">2. 계약내역</td><td>계약금액(원)</td><td>2,000,000,000</td></tr>
<tr><td>최근매출액(원)</td><td>8,000,000,000</td></tr>
<tr><td colspan="2">3. 계약상대</td><td>합성회사</td></tr>
<tr><td rowspan="2">5. 계약기간</td><td>시작일</td><td>2026-10-12</td></tr>
<tr><td>종료일</td><td>2030-12-11</td></tr>
<tr><td rowspan="2">6. 주요 계약조건</td><td>계약금ㆍ선급금 유무</td><td>유</td></tr>
<tr><td>대금지급 조건 등</td><td>진행에 따라 청구 및 지급</td></tr>
<tr><td colspan="3">9. 기타 투자판단과 관련한 중요사항</td></tr>
<tr><td colspan="3">본사 계약분이며 금액과 기간은 변동될 수 있습니다.</td></tr>
</table>'''

    def test_observed_contract_layout_keeps_amount_scope_and_terms(self):
        event, facts, reason = self.parse(self.contract_with_spans(), title="단일판매ㆍ공급계약체결")
        self.assertEqual(reason, "SUPPORTED_OFFICIAL_TABLE_PARSED")
        self.assertEqual(event.facts["contract_amount"], "2000000000")
        self.assertEqual(event.facts["previous_revenue"], "8000000000")
        self.assertEqual(event.facts["start_date"], "2026-10-12")
        self.assertEqual(event.facts["end_date"], "2030-12-11")
        self.assertIn("계약금선급금유무: 유", event.facts["conditions"])
        self.assertIn("진행에 따라 청구 및 지급", event.facts["conditions"])
        self.assertIn("본사 계약분", event.facts["additional_terms"])
        self.assertTrue(all(fact.content_hash == event.source_hash for fact in facts))
        self.assertIn("변동될 수", facts[-1].value)

    def test_observed_contract_layout_does_not_infer_missing_or_conflicting_fields(self):
        text = self.contract_with_spans()
        for bad in (
            text.replace("계약금액(원)", "계약금액(USD)"),
            text.replace("2. 계약내역", "2. 기타내역"),
            text.replace("대금지급 조건 등", "확인되지 않은 항목"),
            text.replace("<td>유</td>", "<td>-</td>"),
            text.replace("합성회사", "공시유보"),
            text.replace("본사 계약분이며 금액과 기간은 변동될 수 있습니다.", ""),
            text.replace("</table>", '<tr><td colspan="2">계약상대</td><td>다른회사</td></tr></table>'),
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(self.parse(bad, title="단일판매ㆍ공급계약체결")[0])

    def test_undisclosed_payment_terms_preserve_contract_and_explicit_uncertainty(self):
        for value in ("-", "미정", "미공개", "공시유보", ""):
            with self.subTest(value=value):
                text = self.contract_with_spans().replace("진행에 따라 청구 및 지급", value)
                event, facts, reason = self.parse(text, title="단일판매ㆍ공급계약체결")
                self.assertEqual(reason, "SUPPORTED_OFFICIAL_TABLE_PARSED")
                self.assertEqual(event.facts["contract_amount"], "2000000000")
                self.assertIn("구체적인 지급조건이 공개되지 않음", event.facts["undisclosed_terms"])
                self.assertTrue(any(fact.fact_id.endswith(":undisclosed_terms") for fact in facts))

    def test_correction_requires_unique_issuer_title_date_match_from_hashed_cover_table(self):
        content = ('<table><tr><td>1. 정정관련 공시서류</td><td>단일판매ㆍ공급계약 체결</td></tr>'
                   '<tr><td>2. 정정관련 공시서류제출일</td><td>2026-09-11</td></tr></table>').encode()
        documents = [{"content": content, "sha256": hashlib.sha256(content).hexdigest()}]
        correction = {"rcept_no": "20260914000001", "corp_code": "00000001", "report_nm": "[기재정정]단일판매ㆍ공급계약체결"}
        original = {"rcept_no": "20260911000001", "corp_code": "00000001", "report_nm": "단일판매·공급계약체결", "rcept_dt": "20260911"}
        self.assertEqual(correction_reference(documents)[1].isoformat(), "2026-09-11")
        self.assertEqual(resolve_correction_parent(correction, documents, [original]), original["rcept_no"])
        for rows in ([], [dict(original, corp_code="00000002")], [dict(original, rcept_dt="20260910")],
                     [dict(original, report_nm="연결 영업실적 공시")],
                     [original, dict(original, rcept_no="20260911000002")], [correction]):
            with self.subTest(rows=rows):
                self.assertIsNone(resolve_correction_parent(correction, documents, rows))
        self.assertIsNone(resolve_correction_parent(correction, [dict(documents[0], sha256="wrong")], [original]))
        misleading = b'<p>20260911000001</p><a href="https://kind.krx.co.kr/?rcpno=20260911000001">related</a>'
        self.assertIsNone(resolve_correction_parent(correction,
            [{"content": misleading, "sha256": hashlib.sha256(misleading).hexdigest()}], [original]))

    def test_correction_cover_accepts_explicit_original_filing_date_without_inventing_period(self):
        content = ('<table><tr><td>1. 정정대상 공시서류 :</td><td>분기보고서</td></tr>'
                   '<tr><td>2. 정정대상 공시서류의 최초제출일 :</td><td>2026년 08월 14일</td></tr></table>').encode()
        documents = [{"content": content, "sha256": hashlib.sha256(content).hexdigest()}]
        receipt = {"rcept_no": "20260914000001", "corp_code": "00000001", "report_nm": "[기재정정]분기보고서 (2026.06)"}
        original = {"rcept_no": "20260814000001", "corp_code": "00000001", "report_nm": "분기보고서 (2026.06)", "rcept_dt": "20260814"}
        self.assertEqual(resolve_correction_parent(receipt, documents, [original]), original["rcept_no"])
        self.assertIsNone(resolve_correction_parent(receipt, documents,
            [original, dict(original, rcept_no="20260814000002", report_nm="분기보고서 (2026.03)")]))


if __name__ == "__main__":
    unittest.main()
