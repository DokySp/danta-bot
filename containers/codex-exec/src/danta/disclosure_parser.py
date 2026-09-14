"""Conservative extraction of explicitly labelled official disclosure tables.

Only supported, unambiguous table layouts become numeric facts. Unknown units,
periods, consolidation bases or correction links produce incomplete evidence.
This parser never infers a consensus surprise or economic materiality.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser

from .models import EventRecord, MarketFact


class Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.table, self.row, self.cell = [], None, None, None
        self.text, self.skip = [], 0
        self.spans = {}
        self.row_index = 0

    def handle_starttag(self, tag, attrs):
        tag, attrs = tag.lower(), dict(attrs)
        if tag in {"script", "style"}:
            self.skip += 1
        if tag == "table":
            if self.table is not None:
                raise ValueError("NESTED_TABLE_UNSUPPORTED")
            self.table, self.spans, self.row_index = [], {}, 0
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
            self.cell_span = (int(attrs.get("rowspan", "1")), int(attrs.get("colspan", "1")))
            if any(span < 1 or span > 100 for span in self.cell_span):
                raise ValueError("INVALID_TABLE_SPAN")

    def handle_data(self, text):
        if self.skip:
            return
        self.text.append(text)
        if self.cell is not None:
            self.cell.append(text)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"td", "th"} and self.cell is not None:
            value = " ".join(" ".join(self.cell).split())
            while (self.row_index, len(self.row)) in self.spans:
                self.row.append(self.spans[(self.row_index, len(self.row))])
            rowspan, colspan = self.cell_span
            column = len(self.row)
            self.row.extend([value] * colspan)
            for future in range(self.row_index + 1, self.row_index + rowspan):
                for col in range(column, column + colspan):
                    self.spans[(future, col)] = value
            self.cell = None
        elif tag == "tr" and self.row is not None:
            while (self.row_index, len(self.row)) in self.spans:
                self.row.append(self.spans[(self.row_index, len(self.row))])
            self.table.append(self.row)
            self.row, self.row_index = None, self.row_index + 1
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def _label(value):
    return re.sub(r"[\s\d.ㆍ·:：()（）]", "", value)


def _amount(value):
    value = re.sub(r"\s", "", value)
    if not re.fullmatch(r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", value):
        raise ValueError("AMBIGUOUS_NUMERIC_CELL")
    return Decimal(value.replace(",", ""))


def _period(value):
    matches = re.findall(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})", value)
    if len(matches) != 2:
        raise ValueError("EXPLICIT_PERIOD_REQUIRED")
    start, end = [date(*map(int, match)) for match in matches]
    if start > end:
        raise ValueError("INVALID_COMPARISON_PERIOD")
    return start, end


def _units(text):
    matches = set(re.findall(r"단위\s*[:：]\s*(억원|백만원|천원|원)", text))
    if len(matches) != 1:
        raise ValueError("EXPLICIT_UNAMBIGUOUS_UNIT_REQUIRED")
    unit = matches.pop()
    return unit, {"원": Decimal(1), "천원": Decimal(1000), "백만원": Decimal(1000000), "억원": Decimal(100000000)}[unit]


def _basis(text):
    connected = "연결" in text
    separate = "별도" in text or "개별재무" in text
    if connected == separate:
        raise ValueError("EXPLICIT_CONSOLIDATION_BASIS_REQUIRED")
    return "CONSOLIDATED" if connected else "SEPARATE"


def _matrix(table, left_labels, right_labels):
    for header_index, row in enumerate(table):
        left = [index for index, cell in enumerate(row) if any(label in cell for label in left_labels)]
        right = [index for index, cell in enumerate(row) if any(label in cell for label in right_labels)]
        if len(left) == len(right) == 1 and left[0] != right[0]:
            return header_index, left[0], right[0]
    raise ValueError("COMPARISON_COLUMNS_UNRECOGNIZED")


def _contract_pairs(table):
    pairs, notes_next = {}, False
    paths = {
        ("계약내역", "계약금액원"): "계약금액원",
        ("계약내역", "최근매출액원"): "최근매출액원",
        ("계약상대", "계약상대"): "계약상대",
        ("계약기간", "시작일"): "계약기간시작일",
        ("계약기간", "종료일"): "계약기간종료일",
        ("주요계약조건", "계약금선급금유무"): "계약금선급금유무",
        ("주요계약조건", "대금지급조건등"): "대금지급조건등",
    }
    for row in table:
        if notes_next:
            if len(row) != 3 or len(set(row)) != 1 or not row[0].strip():
                raise ValueError("CONTRACT_NOTES_INCOMPLETE")
            label, value, notes_next = "기타투자판단과관련한중요사항", row[0], False
        elif len(row) == 3 and len(set(row)) == 1 and _label(row[0]) == "기타투자판단과관련한중요사항":
            notes_next = True
            continue
        elif len(row) == 2:
            label, value = _label(row[0]), row[1]
        elif len(row) == 3:
            label, value = paths.get(tuple(_label(cell) for cell in row[:2])), row[2]
            if label is None:
                continue
        else:
            continue
        if label in pairs:
            raise ValueError("DUPLICATE_CONTRACT_LABEL")
        pairs[label] = value
    if notes_next:
        raise ValueError("CONTRACT_NOTES_INCOMPLETE")
    terms = ("계약금선급금유무", "대금지급조건등")
    if any(label in pairs for label in terms):
        if "계약조건" in pairs or not all(pairs.get(label, "").strip() not in {"", "-", "미정", "미공개", "공시유보"} for label in terms):
            raise ValueError("CONTRACT_TERMS_INCOMPLETE")
        pairs["계약조건"] = "; ".join(label + ": " + pairs[label] for label in terms)
    return pairs


def parse_official_event(receipt: dict, documents: list[dict], instrument_id: str,
                         available_at: datetime) -> tuple[EventRecord | None, list[MarketFact], str]:
    title, receipt_id = receipt.get("report_nm", ""), receipt.get("rcept_no", "")
    if not re.fullmatch(r"\d{14}", receipt_id):
        return None, [], "INVALID_OFFICIAL_RECEIPT"
    correction = receipt.get("correction_parent_id")
    if "정정" in title and not correction:
        return None, [], "CORRECTION_RELATION_UNRESOLVED"
    family = "material_contract" if "계약" in title and any(word in title for word in ("체결", "해지", "취소")) else (
        "official_guidance" if any(word in title for word in ("전망", "계획")) else
        "earnings_quality" if any(word in title for word in ("영업실적", "실적공시", "사업보고서", "분기보고서", "반기보고서")) else None)
    if family is None:
        return None, [], "OBSERVATION_ONLY_EVENT_FAMILY"
    successes, reasons = [], []
    for document in documents:
        content = document.get("content")
        if not isinstance(content, bytes) or not content or len(content) > 10_000_000:
            reasons.append("DOCUMENT_SIZE_OR_TYPE_INVALID")
            continue
        source_hash = hashlib.sha256(content).hexdigest()
        if document.get("sha256") != source_hash:
            reasons.append("DOCUMENT_HASH_MISMATCH")
            continue
        try:
            text = content.decode("utf-8-sig")
            if "<!ENTITY" in text.upper():
                raise ValueError("EXTERNAL_ENTITY_FORBIDDEN")
            parser = Tables()
            parser.feed(text)
            full_text = " ".join(parser.text)
            for table in parser.tables:
                try:
                    values, source_cells = {}, {}
                    if family in {"earnings_quality", "official_guidance"}:
                        basis = _basis(title + " " + full_text)
                        unit, multiplier = _units(full_text)
                        if family == "earnings_quality":
                            header, current_col, prior_col = _matrix(table, ["당기실적", "당해실적"], ["전년동기실적"])
                            current_period, prior_period = _period(table[header][current_col]), _period(table[header][prior_col])
                            if any(a.year != b.year + 1 or (a.month, a.day) != (b.month, b.day) for a, b in zip(current_period, prior_period)):
                                raise ValueError("PERIODS_ARE_NOT_YEAR_OVER_YEAR")
                        else:
                            header, current_col, prior_col = _matrix(table, ["변경전망", "변경 전망", "변경계획", "변경 계획"], ["이전전망", "이전 전망", "이전계획", "이전 계획"])
                            current_period, prior_period = _period(table[header][current_col]), _period(table[header][prior_col])
                            if current_period != prior_period:
                                raise ValueError("GUIDANCE_PERIOD_MISMATCH")
                        if current_period[1] > available_at.date() and family == "earnings_quality":
                            raise ValueError("FUTURE_EARNINGS_PERIOD")
                        for row in table[header + 1:]:
                            names = {_label(cell) for cell in row[:min(current_col, prior_col)]}
                            metric = "revenue" if "매출액" in names else "operating_profit" if "영업이익" in names else None
                            if metric is None:
                                continue
                            if metric + "_current" in values:
                                raise ValueError("DUPLICATE_FINANCIAL_METRIC")
                            values[metric + "_current"] = str(_amount(row[current_col]) * multiplier)
                            values[metric + "_prior"] = str(_amount(row[prior_col]) * multiplier)
                            source_cells[metric] = canonical_cell = " | ".join(row)
                        if not all(key in values for key in ("revenue_current", "revenue_prior", "operating_profit_current", "operating_profit_prior")):
                            raise ValueError("REVENUE_AND_OPERATING_PROFIT_REQUIRED")
                        values.update(current_period="/".join(day.isoformat() for day in current_period), prior_period="/".join(day.isoformat() for day in prior_period), consolidation=basis, source_unit=unit)
                        polarity = "POSITIVE" if all(Decimal(values[key + "_current"]) > Decimal(values[key + "_prior"]) for key in ("revenue", "operating_profit")) else "UNKNOWN"
                        comparison = f"{basis}; KRW; {values['current_period']} vs {values['prior_period']}; {'reported' if family == 'earnings_quality' else 'company forecast, not realized results'}"
                    else:
                        pairs = _contract_pairs(table)
                        fields = {"contract_amount": "계약금액원", "previous_revenue": "최근매출액원", "counterparty": "계약상대", "conditions": "계약조건", "start_date": "계약기간시작일", "end_date": "계약기간종료일"}
                        if not all(label in pairs for label in fields.values()):
                            raise ValueError("COMPLETE_CONTRACT_FIELDS_REQUIRED")
                        values = {key: pairs[label] for key, label in fields.items()}
                        if "기타투자판단과관련한중요사항" in pairs:
                            values["additional_terms"] = pairs["기타투자판단과관련한중요사항"]
                        for key in ("contract_amount", "previous_revenue"):
                            values[key] = str(_amount(values[key]))
                            if Decimal(values[key]) <= 0:
                                raise ValueError("POSITIVE_CONTRACT_AND_REVENUE_REQUIRED")
                        for key in ("start_date", "end_date"):
                            value = values[key].replace(".", "-").replace("/", "-")
                            values[key] = date.fromisoformat(value).isoformat()
                        if values["start_date"] > values["end_date"] or any(values[key].strip() in {"", "-", "미정", "미공개", "공시유보"} for key in ("counterparty", "conditions")):
                            raise ValueError("CONTRACT_TERMS_INCOMPLETE")
                        if any(word in title for word in ("MOU", "업무협약", "검토")):
                            raise ValueError("NOT_A_CONFIRMED_CONTRACT")
                        polarity = "NEGATIVE" if any(word in title for word in ("해지", "취소")) else "POSITIVE"
                        comparison = "KRW; confirmed contract amount vs disclosed previous revenue; conditions and dates remain explicit"
                        source_cells = pairs
                    event_id = "dart:" + receipt_id
                    source = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + receipt_id
                    facts = [MarketFact(fact_id=event_id + ":" + key, instrument_id=instrument_id, value=value,
                        unit="KRW" if key.endswith(("_current", "_prior")) or key in {"contract_amount", "previous_revenue"} else "source_text",
                        source=source, content_hash=source_hash, published_at=None, observed_at=available_at, available_at=available_at, quality="VERIFIED")
                        for key, value in values.items()]
                    facts.append(MarketFact(fact_id=event_id + ":source_cells", instrument_id=instrument_id, value=str(source_cells), unit="verbatim table cells",
                        source=source, content_hash=source_hash, published_at=None, observed_at=available_at, available_at=available_at, quality="VERIFIED"))
                    event = EventRecord(event_id=event_id, instrument_id=instrument_id, official_id=receipt_id, normalized_key=event_id,
                        family=family, source_uri=source, source_hash=source_hash, fact_ids=[fact.fact_id for fact in facts], facts=values,
                        comparison_basis=comparison, available_at=available_at, observed_at=available_at, official=True,
                        primary_source_complete=True, timing_quality="FIRST_COLLECTED", polarity=polarity,
                        correction_of="dart:" + correction if correction and not correction.startswith("dart:") else correction,
                        interpretation="Numeric direction only; materiality, one-offs, priced-in risk and economic path require the portfolio reviewer.")
                    successes.append((event, facts))
                except (ValueError, IndexError) as error:
                    reasons.append(str(error))
        except (UnicodeDecodeError, ValueError) as error:
            reasons.append(str(error))
    if len(successes) == 1:
        return *successes[0], "SUPPORTED_OFFICIAL_TABLE_PARSED"
    if len(successes) > 1:
        return None, [], "MULTIPLE_POSSIBLE_ECONOMIC_TABLES"
    return None, [], "PRIMARY_TEMPLATE_INCOMPLETE:" + ",".join(sorted(set(reasons)))
