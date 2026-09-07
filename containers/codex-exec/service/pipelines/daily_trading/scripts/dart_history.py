#!/usr/bin/env python3
"""Archive receipt-specific DART XBRL; never backdate today's financial API rows."""

from __future__ import annotations

import argparse
import calendar
import fcntl
import getpass
import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"
MAX_BYTES = 32 * 1024 * 1024
REPORT_CODES = {3: "11013", 6: "11012", 9: "11014", 12: "11011"}
COMPACT_LIMITATION = "Instance only; original ZIP, XSD schemas and XML linkbases are not retained"


class DartError(RuntimeError):
    """Safe diagnostic: no URL, request headers, response text, or credentials."""


class ConflictingFacts(DartError):
    """Well-formed financial facts disagree; never choose an amount automatically."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DartError("DART redirect rejected")


class DartClient:
    def __init__(self, key: str, *, request_limit: int = 200, budget_file: Path | None = None):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", key):
            raise DartError("DART key must be a 40-character hexadecimal value")
        if request_limit <= 0:
            raise DartError("request_limit must be positive")
        self._key = key
        self.request_limit = request_limit
        self.request_count = 0
        self.budget_file = Path(budget_file) if budget_file is not None else None
        self._opener = build_opener(NoRedirect())

    def request(self, endpoint: str, params: dict) -> bytes:
        if endpoint not in {"list.json", "fnlttXbrl.xml", "corpCode.xml"} or "crtfc_key" in params:
            raise DartError("unsupported DART request")
        if self.request_count >= self.request_limit:
            raise DartError("DART request budget exhausted; resume from saved archives")
        if self.budget_file is not None:
            # This is one cumulative collection budget, not an assumed daily API quota.
            self.budget_file.parent.mkdir(parents=True, exist_ok=True)
            with self.budget_file.with_suffix(self.budget_file.suffix + ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    count = json.loads(self.budget_file.read_text())["request_count"] if self.budget_file.exists() else 0
                except (ValueError, KeyError, TypeError):
                    raise DartError("invalid persisted DART request budget") from None
                if type(count) is not int or count < 0:
                    raise DartError("invalid persisted DART request budget")
                if count >= self.request_limit:
                    raise DartError("DART request budget exhausted; resume from saved archives")
                # Reserve before sending: a failed/uncertain request still consumes budget.
                save_json(self.budget_file, {"request_count": count + 1})
        self.request_count += 1
        time.sleep(0.15)
        query = urlencode(dict(params, crtfc_key=self._key))
        request = Request("https://opendart.fss.or.kr/api/" + endpoint + "?" + query,
                          headers={"User-Agent": "danta-dart-history/1"})
        try:
            with self._opener.open(request, timeout=30) as response:
                payload = response.read(MAX_BYTES + 1)
        except HTTPError as exc:
            raise DartError(f"DART HTTP status {exc.code}") from None
        except (URLError, OSError, TimeoutError):
            raise DartError("DART transport failed") from None
        if len(payload) > MAX_BYTES:
            raise DartError("DART response too large")
        if self._key.encode() in payload:
            raise DartError("DART response unexpectedly contained a credential")
        if zipfile.is_zipfile(BytesIO(payload)):
            with zipfile.ZipFile(BytesIO(payload)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 4 * MAX_BYTES:
                    raise DartError("expanded DART archive too large")
                try:
                    for item in archive.infolist():
                        member_limit = 4 * MAX_BYTES if endpoint == "corpCode.xml" else MAX_BYTES
                        if item.file_size > member_limit or self._key.encode() in archive.read(item):
                            raise DartError("unsafe DART archive member")
                except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
                    raise DartError("unsafe DART archive") from None
        return payload


def xml_root(payload: bytes, *, max_bytes: int = MAX_BYTES) -> ET.Element:
    if (len(payload) > max_bytes or b"\x00" in payload
            or b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper()):
        raise DartError("unsupported or oversized XML")
    try:
        return ET.fromstring(payload)
    except ET.ParseError:
        raise DartError("invalid DART XML") from None


def api_status(status: object) -> str:
    value = str(status or "")
    return value if re.fullmatch(r"\d{3}", value) else "invalid"


def parse_corp_codes(payload: bytes) -> dict:
    """Current identifier associations only; never a historical listing universe."""
    if not zipfile.is_zipfile(BytesIO(payload)):
        root = xml_root(payload)
        raise DartError(f"DART corporation-code status {api_status(root.findtext('status'))}")
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        members = archive.infolist()
        if (len(payload) > MAX_BYTES or len(members) != 1
                or members[0].filename.upper() != "CORPCODE.XML" or members[0].file_size > 4 * MAX_BYTES):
            raise DartError("expected one bounded corporation-code XML")
        try:
            document = archive.read(members[0])
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
            raise DartError("invalid corporation-code archive") from None
        root = xml_root(document, max_bytes=4 * MAX_BYTES)
    if root.tag != "result" or not root.findall("list"):
        raise DartError("invalid corporation-code document")
    by_stock_code, unmapped = {}, set()
    for item in root.findall("list"):
        corp = (item.findtext("corp_code") or "").strip()
        stock = (item.findtext("stock_code") or "").strip()
        if not re.fullmatch(r"\d{8}", corp) or (stock and not re.fullmatch(r"[0-9A-Z]{6}", stock)):
            raise DartError("invalid corporation-code identifiers")
        if not stock:
            unmapped.add(corp)
            continue
        if stock in by_stock_code and by_stock_code[stock]["corp_code"] != corp:
            raise DartError("ambiguous stock-to-corporation association")
        by_stock_code[stock] = {"corp_code": corp, "corp_name": item.findtext("corp_name", ""),
                               "modify_date": item.findtext("modify_date", "")}
    return {"by_stock_code": by_stock_code, "unmapped_corp_codes": sorted(unmapped),
            "source_sha256": hashlib.sha256(payload).hexdigest(), "historical_membership": False,
            "limitations": ["Current identifier snapshot; historical stock-code and delisted coverage require validation",
                            "Empty stock-code associations are excluded from by_stock_code"]}


def filing_metadata(row: dict) -> dict | None:
    name = str(row.get("report_nm", ""))
    match = re.search(r"(사업|반기|분기)보고서\s*\((\d{4})\.(\d{2})\)", name)
    if not match:
        return None
    year, month = int(match[2]), int(match[3])
    # ponytail: December fiscal year only; other fiscal calendars need an explicit mapping.
    if month not in REPORT_CODES or match[1] != ({3: "분기", 6: "반기", 9: "분기", 12: "사업"}[month]):
        return None
    receipt = str(row.get("rcept_no", ""))
    corp_code = str(row.get("corp_code", ""))
    if not re.fullmatch(r"\d{14}", receipt) or not re.fullmatch(r"\d{8}", corp_code):
        raise DartError("invalid filing identifiers")
    try:
        received = datetime.strptime(str(row["rcept_dt"]), "%Y%m%d").date()
        end = date(year, month, calendar.monthrange(year, month)[1])
    except (KeyError, ValueError):
        raise DartError("invalid filing dates") from None
    if end >= received:
        raise DartError("filing period is not before receipt")
    return {
        "corp_code": corp_code, "stock_code": row.get("stock_code", ""),
        "corp_name": row.get("corp_name", ""), "rcept_no": receipt,
        "report_name": name, "report_code": REPORT_CODES[month],
        "period_end": end.isoformat(), "received_on": received.isoformat(),
        "available_on": (received + timedelta(days=1)).isoformat(),
        "is_correction": "정정" in name, "remarks": row.get("rm", ""),
    }


def list_filings(client: DartClient, corp_code: str, start: date, end: date) -> list[dict]:
    if not re.fullmatch(r"\d{8}", corp_code) or start > end:
        raise DartError("invalid company or date range")
    rows = []
    page = 1
    while True:
        raw = client.request("list.json", {
            "corp_code": corp_code, "bgn_de": start.strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"), "pblntf_ty": "A", "last_reprt_at": "N",
            "sort": "date", "sort_mth": "asc", "page_count": "100", "page_no": str(page),
        })
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise DartError("invalid DART JSON") from None
        status = api_status(body.get("status"))
        if status == "013" and page == 1:
            return []
        if status != "000":
            raise DartError(f"DART API status {status}")
        for row in body.get("list", []):
            filing = filing_metadata(row)
            if filing:
                if filing["corp_code"] != corp_code or not start.isoformat() <= filing["received_on"] <= end.isoformat():
                    raise DartError("filing outside requested company/date bounds")
                rows.append(filing)
        if page >= int(body["total_page"]):
            break
        page += 1
    by_id = {row["rcept_no"]: row for row in rows}
    return sorted(by_id.values(), key=lambda row: (row["received_on"], row["rcept_no"]))


def xbrl_instance(payload: bytes) -> tuple[str, bytes]:
    if not zipfile.is_zipfile(BytesIO(payload)):
        root = xml_root(payload)
        raise DartError(f"DART XBRL status {api_status(root.findtext('status'))}")
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        instances = [item for item in archive.infolist() if item.filename.lower().endswith(".xbrl")]
        if len(payload) > MAX_BYTES or len(instances) != 1 or instances[0].file_size > MAX_BYTES:
            raise DartError("expected one bounded XBRL instance")
        try:
            return instances[0].filename, archive.read(instances[0])
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
            raise DartError("invalid XBRL archive") from None


def consolidated_facts(payload: bytes, corp_code: str) -> list[dict]:
    _, instance = xbrl_instance(payload)
    root = xml_root(instance)
    contexts = {}
    for context in root.findall(XBRLI + "context"):
        members = list(context.iter(XBRLDI + "explicitMember"))
        if (len(members) != 1 or list(context.iter(XBRLDI + "typedMember"))
                or members[0].get("dimension") != "ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis"
                or members[0].text != "ifrs-full:ConsolidatedMember"
                or context.findtext(XBRLI + "entity/" + XBRLI + "identifier") != corp_code):
            continue
        start = context.findtext(XBRLI + "period/" + XBRLI + "startDate")
        end = context.findtext(XBRLI + "period/" + XBRLI + "endDate")
        if start and end:
            try:
                if date.fromisoformat(start) > date.fromisoformat(end):
                    raise ValueError
            except ValueError:
                raise DartError("invalid XBRL duration") from None
            contexts[context.get("id")] = (start, end)
    units = {unit.get("id") for unit in root.findall(XBRLI + "unit")
             if len(unit) == 1 and unit[0].tag == XBRLI + "measure" and unit[0].text == "iso4217:KRW"}
    grouped = {}
    precision = {}
    for fact in root:
        context = contexts.get(fact.get("contextRef"))
        concept = fact.tag.rsplit("}", 1)[-1]
        namespace = fact.tag.split("}", 1)[0].lstrip("{")
        standard = ((concept == "Revenue" and re.fullmatch(r"https?://xbrl\.ifrs\.org/taxonomy/\d{4}-\d{2}-\d{2}/ifrs-full", namespace))
                    or (concept == "OperatingIncomeLoss" and re.fullmatch(r"https?://dart\.fss\.or\.kr/taxonomy/\d{4}-\d{2}-\d{2}/ifrs/dart", namespace)))
        if not context or not standard or fact.get("unitRef") not in units or fact.get("scale") is not None:
            continue
        if fact.get("{http://www.w3.org/2001/XMLSchema-instance}nil") in {"true", "1"}:
            continue
        try:
            amount = Decimal(fact.text or "")
        except InvalidOperation:
            raise DartError("invalid financial amount") from None
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise DartError("financial amount must be finite whole KRW")
        key = (*context, concept)
        grouped.setdefault(key, set()).add(int(amount))
        precision.setdefault(key, set()).add(fact.get("decimals", "unspecified"))
    if any(len(values) != 1 for values in grouped.values()):
        raise ConflictingFacts("conflicting facts for the same period/concept")
    return [{"start": start, "end": end, "concept": concept, "amount_krw": next(iter(values)),
             "reported_decimals": sorted(precision[(start, end, concept)])}
            for (start, end, concept), values in sorted(grouped.items())]


def quarterly_growth(facts: list[dict], period_end: str) -> dict:
    end = date.fromisoformat(period_end)
    if end.month not in REPORT_CODES:
        return {"status": "unavailable", "reason": "unsupported quarter end"}
    periods = [(date(year, end.month - 2, 1).isoformat(),
                date(year, end.month, calendar.monthrange(year, end.month)[1]).isoformat())
               for year in (end.year, end.year - 1)]
    return _period_growth(facts, periods, "quarterly")


def _period_growth(facts: list[dict], periods: list[tuple[str, str]], kind: str) -> dict:
    lookup = {(row["start"], row["end"], row["concept"]): row["amount_krw"] for row in facts}
    amounts = {}
    for label, concept in (("revenue", "Revenue"), ("operating_income", "OperatingIncomeLoss")):
        values = [lookup.get((*period, concept)) for period in periods]
        if any(value is None for value in values):
            return {"status": "unavailable", "reason": f"missing comparable consolidated {kind} facts"}
        amounts[label + "_current_krw"], amounts[label + "_prior_year_krw"] = values
    return {"status": "available", **amounts,
            "earnings_improved": (amounts["revenue_current_krw"] > amounts["revenue_prior_year_krw"]
                                  and amounts["operating_income_current_krw"] > max(0, amounts["operating_income_prior_year_krw"]))}


def research_growth(facts: list[dict], period_end: str) -> dict:
    """Same-receipt YoY comparison: full year for annual reports, otherwise quarter."""
    end = date.fromisoformat(period_end)
    annual = period_end.endswith("-12-31")
    if annual:
        periods = [(f"{year}-01-01", f"{year}-12-31") for year in (end.year, end.year - 1)]
        result = _period_growth(facts, periods, "annual")
    else:
        result = quarterly_growth(facts, period_end)
    return {**result, "period_kind": "annual" if annual else "quarter",
            "comparison": "year_over_year", "method": "same_receipt"}


def q4_reconciliation(annual: dict, records: list[dict], decision_date: date) -> dict:
    """Derive Q4 as known on a date, without certifying cross-filing accounting scope."""
    cutoff = decision_date.isoformat()
    eligible = [row for row in records if row["corp_code"] == annual["corp_code"]
                and row["received_on"] < cutoff and row["available_on"] <= cutoff]
    if (annual["received_on"] >= cutoff or annual["available_on"] > cutoff
            or not annual["period_end"].endswith("-12-31")):
        return {"status": "unavailable", "reason": "annual report not available at decision time"}

    def source(row: dict) -> dict:
        return {key: row[key] for key in ("corp_code", "rcept_no", "period_end", "received_on", "available_on", "sha256")}

    def latest(period: str) -> dict | None:
        rows = [row for row in eligible if row["period_end"] == period]
        return max(rows, key=lambda row: (row["received_on"], row["rcept_no"])) if rows else None

    def fact(row: dict, year: int, month: int, concept: str) -> dict | None:
        end = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
        found = [item for item in row.get("facts", []) if item["start"] == f"{year}-01-01"
                 and item["end"] == end and item["concept"] == concept]
        return found[0] if len(found) == 1 else None

    if not annual.get("facts") or not annual.get("sha256"):
        return {"status": "unavailable", "reason": "annual source facts/provenance missing"}
    direct = quarterly_growth(annual["facts"], annual["period_end"])
    if direct["status"] == "available":
        return {**direct, "method": "direct_single_receipt", "sources": [source(annual)]}

    year = date.fromisoformat(annual["period_end"]).year
    nine_months = latest(f"{year}-09-30")
    if not nine_months or not nine_months.get("facts") or not nine_months.get("sha256"):
        return {"status": "unavailable", "reason": "latest nine-month report facts/provenance missing"}
    sources = [source(annual), source(nine_months)]
    candidate, calculations, comparisons = {}, {}, []
    for label, concept in (("revenue", "Revenue"), ("operating_income", "OperatingIncomeLoss")):
        for suffix, period_year in (("current_krw", year), ("prior_year_krw", year - 1)):
            full = fact(annual, period_year, 12, concept)
            ytd = fact(nine_months, period_year, 9, concept)
            if full is None or ytd is None:
                return {"status": "unavailable", "reason": "missing comparable annual/nine-month facts", "sources": sources}
            key = label + "_" + suffix
            candidate[key] = full["amount_krw"] - ytd["amount_krw"]
            calculations[key] = {
                "annual_krw": full["amount_krw"], "nine_months_krw": ytd["amount_krw"],
                "difference_krw": candidate[key],
                "annual_decimals": full.get("reported_decimals", ["unspecified"]),
                "nine_months_decimals": ytd.get("reported_decimals", ["unspecified"]),
            }
        # An observed comparative restatement needs reconciliation, not a silent subtraction.
        for current, month in ((annual, 12), (nine_months, 9)):
            prior = latest(date(year - 1, month, calendar.monthrange(year - 1, month)[1]).isoformat())
            older = fact(prior, year - 1, month, concept) if prior else None
            newer = fact(current, year - 1, month, concept)
            if older is not None:
                comparisons.append({"source_receipt": prior["rcept_no"], "concept": concept,
                                    "period_end": prior["period_end"],
                                    "matches": older["amount_krw"] == newer["amount_krw"]})
    if any(not row["matches"] for row in comparisons):
        return {"status": "unavailable", "reason": "comparative amounts changed across filings; reconcile restatement",
                "sources": sources, "overlap_checks": comparisons}
    candidate["earnings_improved"] = (candidate["revenue_current_krw"] > candidate["revenue_prior_year_krw"]
                                      and candidate["operating_income_current_krw"] > max(0, candidate["operating_income_prior_year_krw"]))
    # CFS/KRW/account/period checks cannot establish an unchanged consolidation or accounting basis.
    return {"status": "derived_unverified", "method": "annual_minus_nine_months",
            "reason": "cross-filing consolidation/accounting comparability requires review",
            "candidate": candidate, "calculations": calculations, "sources": sources,
            "overlap_checks": comparisons,
            "available_on": max(row["available_on"] for row in sources)}


def latest_asof(records: list[dict], decision_date: date, *, research: bool = False) -> dict | None:
    eligible = [row for row in records if row["received_on"] < decision_date.isoformat()
                and row["available_on"] <= decision_date.isoformat()]
    # Latest missing/unsupported report remains missing, rather than reviving an older positive signal.
    latest = max(eligible, key=lambda row: (row["period_end"], row["received_on"], row["rcept_no"])) if eligible else None
    if latest and research:
        return {**latest, "research_growth": research_growth(latest.get("facts", []), latest["period_end"])}
    if latest and latest["period_end"].endswith("-12-31"):
        # A later Q3 correction can change Q4, but must never rewrite an earlier decision.
        return {**latest, "quarterly_growth": q4_reconciliation(latest, eligible, decision_date)}
    return latest


def save_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def compact_archive(payload: bytes) -> tuple[bytes, dict]:
    name, instance = xbrl_instance(payload)
    xml_root(instance)
    packed = BytesIO()
    with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        target.writestr(name, instance)
    stored = packed.getvalue()
    return stored, {"storage_mode": "instance_only", "sha256": hashlib.sha256(payload).hexdigest(),
                    "stored_sha256": hashlib.sha256(stored).hexdigest(), "instance_name": name,
                    "instance_sha256": hashlib.sha256(instance).hexdigest(),
                    "storage_limitations": [COMPACT_LIMITATION]}


def collect(client: DartClient, corp_code: str, start: date, end: date, output: Path, *,
            compact: bool = False, research: bool = False) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    result_path = output / f"result-{corp_code}-{start}-{end}.json"
    records = list_filings(client, corp_code, start, end)
    save_json(output / f"filings-{corp_code}-{start}-{end}.json", records)
    suffix = ".instance" if compact else ""
    for index, row in enumerate(records, 1):
        target = output / (row["rcept_no"] + suffix + ".zip")
        metadata_path = target.with_suffix(".json")
        storage = {}
        if target.exists():
            payload = target.read_bytes()
            if not metadata_path.exists():
                raise DartError("cached XBRL metadata missing; use a fresh output directory")
            try:
                previous = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                raise DartError("invalid cached XBRL metadata") from None
            if not isinstance(previous, dict):
                raise DartError("invalid cached XBRL metadata")
            if (previous.get("corp_code") != corp_code or previous.get("rcept_no") != row["rcept_no"]
                    or previous.get("archive") != target.name):
                raise DartError("cached XBRL provenance mismatch")
            if compact:
                name, instance = xbrl_instance(payload)
                with zipfile.ZipFile(BytesIO(payload)) as archive:
                    member_count = len(archive.infolist())
                if (member_count != 1 or previous.get("storage_mode") != "instance_only"
                        or not re.fullmatch(r"[0-9a-f]{64}", str(previous.get("sha256", "")))
                        or previous.get("stored_sha256") != hashlib.sha256(payload).hexdigest()
                        or previous.get("instance_name") != name
                        or previous.get("instance_sha256") != hashlib.sha256(instance).hexdigest()):
                    raise DartError("cached XBRL provenance mismatch")
                storage = {key: previous[key] for key in
                           ("storage_mode", "sha256", "stored_sha256", "instance_name", "instance_sha256")}
                storage["storage_limitations"] = [COMPACT_LIMITATION]
            elif previous.get("sha256") != hashlib.sha256(payload).hexdigest():
                raise DartError("cached XBRL provenance mismatch")
        else:
            try:
                payload = client.request("fnlttXbrl.xml", {"rcept_no": row["rcept_no"], "reprt_code": row["report_code"]})
            except DartError as exc:
                save_json(result_path, {"status": "partial", "corp_code": corp_code,
                                       "start": start.isoformat(), "end": end.isoformat(),
                                       "receipts": len(records), "completed_receipts": index - 1,
                                       "records": records[:index - 1],
                                       "pending_receipts": [item["rcept_no"] for item in records[index - 1:]],
                                       "error": str(exc), "strategy_backtest_ready": False})
                raise
        facts = None
        try:
            facts = consolidated_facts(payload, corp_code)
        except ConflictingFacts as exc:
            if not research:
                raise
            # Preserve the receipt, but quarantine every amount in it. Other XML,
            # amount, archive, credential and provenance failures still abort.
            facts = []
            row["financial_data_issue"] = str(exc)
        except DartError as exc:
            row["quarterly_growth"] = {"status": "unavailable", "reason": str(exc)}
            # Only explicit no-data/no-file responses are missing-company data.
            if str(exc) not in {"DART XBRL status 013", "DART XBRL status 014"}:
                raise
        if facts is not None:
            if not target.exists():
                if compact:
                    payload, storage = compact_archive(payload)
                temporary = target.with_suffix(".zip.tmp")
                temporary.write_bytes(payload)
                temporary.replace(target)
            row.update({"source": "fnlttXbrl.xml", "archive": target.name,
                        "sha256": hashlib.sha256(payload).hexdigest(), "facts": facts,
                        "quarterly_growth": quarterly_growth(facts, row["period_end"]), **storage})
        if research:
            row["research_growth"] = research_growth(row.get("facts", []), row["period_end"])
        row["collected_at"] = datetime.now(timezone.utc).isoformat()
        save_json(metadata_path, row)
        print(f"DART {corp_code} {index}/{len(records)}: {'archived' if row.get('facts') else 'unavailable'}", flush=True)
    for row in records:
        if not research and row["period_end"].endswith("-12-31"):
            row["quarterly_growth"] = q4_reconciliation(row, records, date.fromisoformat(row["available_on"]))
            save_json(output / (row["rcept_no"] + suffix + ".json"), row)
    result = {"status": "complete", "corp_code": corp_code, "start": start.isoformat(), "end": end.isoformat(),
              "receipts": len(records), "corrections": sum(row["is_correction"] for row in records),
              "comparable_quarters": sum(row["quarterly_growth"]["status"] == "available" for row in records),
              "derived_unverified_q4": sum(row["quarterly_growth"]["status"] == "derived_unverified" for row in records),
              "records": records, "strategy_backtest_ready": False,
              "limitations": ["explicit CFS dimensions, standard Revenue/OperatingIncomeLoss and KRW only",
                              "reported amounts retain XBRL decimals precision; not exact unrounded accounting values",
                              "December fiscal year; cross-filing Q4 differences are candidates, not verified signals",
                              "annual/nine-month accounting and consolidation comparability require review even when overlapping amounts match",
                              "receipt date plus one calendar day, not verified intraday publication time",
                              "company sample is not a historical investable universe",
                              "sector, membership, delisting, corporate-action and price coverage still required"]}
    if compact:
        result["storage_mode"] = "instance_only"
        result["limitations"].append(COMPACT_LIMITATION)
    if research:
        result["research_mode"] = "same_receipt_annual_or_quarter_yoy"
        result["comparable_research_reports"] = sum(row["research_growth"]["status"] == "available" for row in records)
        result["quarantined_receipts"] = sum(bool(row.get("financial_data_issue")) for row in records)
        result["limitations"].append("Annual growth is full-year YoY, not Q4; research mode does not derive Q4")
        result["limitations"].append("Conflicting source facts are archived but quarantined, not repaired or accepted as signals")
    save_json(result_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--corp-code")
    target.add_argument("--universe-db", type=Path, help="Collect all codes in a completed historical universe")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--request-limit", type=int, default=200)
    parser.add_argument("--budget-file", type=Path, help="Cumulative request counter across restarts; no automatic reset")
    parser.add_argument("--compact", action="store_true", help="Keep only the XBRL instance, not the entire original ZIP")
    parser.add_argument("--research", action="store_true", help="Same-receipt annual/quarter YoY research signals")
    args = parser.parse_args()
    key = os.environ.get("DART_API_KEY") or getpass.getpass("OpenDART key (not saved): ")
    try:
        client = DartClient(key, request_limit=args.request_limit, budget_file=args.budget_file)
        if args.universe_db:
            result = collect_universe(client, args.universe_db, args.start, args.end, args.output_dir,
                                      compact=args.compact, research=args.research)
        else:
            result = collect(client, args.corp_code, args.start, args.end, args.output_dir,
                             compact=args.compact, research=args.research)
    except DartError as exc:
        print(str(exc))
        return 1
    print(json.dumps({k: v for k, v in result.items() if k != "records"}, ensure_ascii=False))
    return 0


def collect_universe(client: DartClient, universe: Path, start: date, end: date, output: Path,
                     *, compact: bool = False, research: bool = False) -> dict:
    """One bounded, resumable batch; no quota reset or scheduled retries."""
    if start > end:
        raise DartError("invalid financial collection date range")
    try:
        from .historical_universe import validated_complete_codes
    except ImportError:
        from historical_universe import validated_complete_codes
    try:
        codes = validated_complete_codes(universe, universe.with_name('calendar.json'))
    except (ValueError, OSError):
        raise DartError("full historical universe must be complete and integrity-verified before financial batch") from None
    output.mkdir(parents=True, exist_ok=True)
    mapping_archive = output / 'corpCode.zip'
    if mapping_archive.exists():
        payload = mapping_archive.read_bytes()
        mapping = parse_corp_codes(payload)
        recorded = json.loads((output / 'corp-code-mapping.json').read_text(encoding='utf-8'))
        if recorded.get('source_sha256') != mapping['source_sha256']:
            raise DartError("corporation mapping provenance mismatch")
    else:
        payload = client.request('corpCode.xml', {})
        mapping = parse_corp_codes(payload)
        temporary = mapping_archive.with_suffix('.zip.tmp')
        temporary.write_bytes(payload)
        temporary.replace(mapping_archive)
        save_json(output / 'corp-code-mapping.json', mapping)
    result = {'status': 'running', 'start': start.isoformat(), 'end': end.isoformat(),
              'target_codes': codes, 'completed': [], 'unmapped_codes': [],
              'mapping_source_sha256': mapping['source_sha256'], 'compact': compact, 'research': research,
              'strategy_backtest_ready': False}
    status_path = output / 'dart-full-collection-status.json'
    if status_path.exists():
        previous = json.loads(status_path.read_text(encoding='utf-8'))
        if any(previous.get(k) != result[k] for k in ('start', 'end', 'target_codes', 'mapping_source_sha256', 'compact', 'research')):
            raise DartError('financial batch resume scope mismatch')
        result['completed'] = previous['completed']
    completed = {row['code'] for row in result['completed']}
    if len(completed) != len(result['completed']) or not completed <= set(codes):
        raise DartError('financial batch resume identifiers invalid')
    # Verify completed archives locally; do not spend the shared API budget on those companies again.
    for row in result['completed']:
        corp = mapping['by_stock_code'].get(row['code'], {}).get('corp_code')
        folder = output / 'dart' / row['code']
        saved = folder / f'result-{corp}-{start}-{end}.json'
        if corp != row['corp_code'] or hashlib.sha256(saved.read_bytes()).hexdigest() != row.get('result_sha256'):
            raise DartError('completed financial company provenance mismatch')
        for record in json.loads(saved.read_text(encoding='utf-8'))['records']:
            if record.get('archive'):
                name = record['archive']
                if Path(name).name != name or hashlib.sha256((folder / name).read_bytes()).hexdigest() != record.get('stored_sha256', record.get('sha256')):
                    raise DartError('completed financial archive provenance mismatch')
    save_json(status_path, result)
    try:
        for code in codes:
            if code in completed:
                continue
            if code not in mapping['by_stock_code']:
                result['unmapped_codes'].append(code)
                continue
            if shutil.disk_usage(output).free < 3 * 1024 ** 3:
                raise DartError("financial collection stopped at 3 GiB free-space guard")
            corp = mapping['by_stock_code'][code]['corp_code']
            item = collect(client, corp, start, end, output / 'dart' / code,
                           compact=compact, research=research)
            result['completed'].append({'code': code, 'corp_code': corp, 'receipts': item['receipts'],
                                        'comparable': item.get('comparable_research_reports'),
                                        'quarantined_receipts': item.get('quarantined_receipts', 0),
                                        'result_sha256': hashlib.sha256((output / 'dart' / code / f'result-{corp}-{start}-{end}.json').read_bytes()).hexdigest()})
            result['requests_this_process'] = client.request_count
            save_json(status_path, result)
    except Exception as exc:
        error = str(exc) if isinstance(exc, DartError) else f'financial batch failed ({type(exc).__name__})'
        result.update(status='partial', error=error, stopped_code=code,
                      requests_this_process=client.request_count)
        try:
            save_json(status_path, result)
        except OSError:
            pass  # A full/unwritable disk cannot guarantee a status write; the process still fails.
        raise DartError(error) from None
    result['status'] = 'partial' if result['unmapped_codes'] else 'collection_complete_not_backtest_ready'
    save_json(status_path, result)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
