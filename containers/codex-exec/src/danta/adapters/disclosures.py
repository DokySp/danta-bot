"""OpenDART list, original documents and exact-domain official IR reads."""

import hashlib
import io
import re
import zipfile
from datetime import date
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree

from . import AdapterError, FetchResult, http_transport, require_http_ok, utcnow

ORIGIN = "https://opendart.fss.or.kr"


class DartAdapter:
    def __init__(self, *, api_key=None, transport=None, mode="offline", authorize=None, official_ir_domains=()):
        self._api_key = api_key
        self.domains = frozenset(official_ir_domains)
        self.transport = transport or http_transport(allowed_origins={ORIGIN, *("https://" + host for host in self.domains)})
        self.mode, self.authorize = mode, authorize

    def _get(self, url):
        if self.mode == "offline" and not getattr(self.transport, "fixture_only", False):
            raise AdapterError("OFFLINE_NETWORK_BLOCKED")
        if self.mode != "offline":
            if self.authorize is None:
                raise AdapterError("AUTHORIZATION_REQUIRED")
            self.authorize("disclosure_read")
        response = self.transport("GET", url, {}, None, 15)
        require_http_ok(response)
        return response

    def _api(self, endpoint, params):
        key = self._api_key
        if key is None and self.mode == "offline" and getattr(self.transport, "fixture_only", False):
            key = "FIXTURE_ONLY"
        if not key:
            raise AdapterError("DART_AUTH_REQUIRED")
        return self._get(ORIGIN + "/api/" + endpoint + "?" + urlencode({"crtfc_key": key, **params}))

    def list_disclosures(self, start: date, end: date, *, corp_code=None, cursor=1, max_pages=100):
        if start > end or type(cursor) is not int or cursor < 1:
            raise AdapterError("INVALID_DISCLOSURE_RANGE")
        if corp_code is not None and not re.fullmatch(r"\d{8}", corp_code):
            raise AdapterError("INVALID_CORP_CODE")
        # Split all-company requests conservatively below DART's 3-month ceiling.
        if corp_code is None and (end - start).days > 89:
            raise AdapterError("DART_RANGE_REQUIRES_SPLIT")
        records, page, expected_total, seen = [], cursor, None, set()
        try:
            for _ in range(max_pages):
                data = self._api("list.json", {"bgn_de": start.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
                    "corp_code": corp_code or "", "last_reprt_at": "N", "sort": "date", "sort_mth": "asc", "page_no": page, "page_count": 100}).json()
                status = data.get("status")
                if status == "013" and not records and page == 1:
                    return FetchResult((), "COMPLETE_NO_EVENT", utcnow())
                if status != "000":
                    raise AdapterError({"010": "AUTH_FAILED", "011": "AUTH_FAILED", "012": "AUTH_FAILED", "020": "RATE_LIMITED"}.get(status, "DART_FETCH_FAILED"))
                if type(data.get("page_no")) is not int or data["page_no"] != page or not isinstance(data.get("list"), list):
                    raise AdapterError("MALFORMED_RESPONSE")
                if expected_total is not None and expected_total != data.get("total_count"):
                    raise AdapterError("DISCLOSURE_PAGINATION_CHANGED")
                expected_total = data.get("total_count")
                for row in data["list"]:
                    if not re.fullmatch(r"\d{14}", row.get("rcept_no", "")) or not re.fullmatch(r"\d{8}", row.get("corp_code", "")):
                        raise AdapterError("MALFORMED_DISCLOSURE")
                    if corp_code and row["corp_code"] != corp_code:
                        raise AdapterError("CORPORATION_MISMATCH")
                    if row["rcept_no"] in seen:
                        raise AdapterError("DISCLOSURE_DUPLICATE_PAGE")
                    seen.add(row["rcept_no"])
                    records.append(dict(row, source_id="dart:" + row["rcept_no"], document_verified=False,
                                        original_public_at=None, available_time_quality="DATE_ONLY", correction_parent_id=None))
                if page >= data["total_page"]:
                    if cursor == 1 and len(records) != expected_total:
                        raise AdapterError("DISCLOSURE_COUNT_MISMATCH")
                    return FetchResult(tuple(records), "COMPLETE", utcnow(), metadata={"next_success_cursor": {"end_date": end.isoformat(), "receipt_ids": [r["rcept_no"] for r in records]}, "intraday_time_verified": False})
                page += 1
        except (AdapterError, KeyError, TypeError) as exc:
            return FetchResult(tuple(records), "PARTIAL" if records else "FETCH_FAILED", utcnow(), page, {"error": getattr(exc, "code", "MALFORMED_RESPONSE")})
        return FetchResult(tuple(records), "PARTIAL", utcnow(), page, {"error": "PAGE_LIMIT"})

    def read_disclosure(self, receipt_no):
        if not isinstance(receipt_no, str) or not re.fullmatch(r"\d{14}", receipt_no):
            raise AdapterError("INVALID_RECEIPT_NO")
        content = self._api("document.xml", {"rcept_no": receipt_no}).body
        documents = unpack_documents(content)
        return FetchResult(tuple({"receipt_no": receipt_no, "filename": name, "content": body,
                                 "sha256": hashlib.sha256(body).hexdigest(), "original_public_at": None} for name, body in documents),
                           "COMPLETE", utcnow(), metadata={"original_public_at_verified": False})

    def read_corporations(self):
        documents = unpack_documents(self._api("corpCode.xml", {}).body)
        result = {}
        for _, body in documents:
            if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
                raise AdapterError("UNSAFE_XML")
            try:
                root = ElementTree.fromstring(body)
                for row in root.findall("list"):
                    code, stock = row.findtext("corp_code", "").strip(), row.findtext("stock_code", "").strip()
                    if not stock:
                        continue
                    if not re.fullmatch(r"\d{8}", code) or not re.fullmatch(r"[A-Z0-9]{6}", stock) or stock in result:
                        raise AdapterError("CORPORATION_MAPPING_CONFLICT")
                    result[stock] = {"symbol": stock, "corp_code": code, "corp_name": row.findtext("corp_name"), "modified_at": row.findtext("modify_date")}
            except ElementTree.ParseError:
                raise AdapterError("MALFORMED_CORPORATION_XML") from None
        return FetchResult(tuple(result.values()), "COMPLETE" if result else "FETCH_FAILED", utcnow())

    def read_official_ir(self, url, *, published_at=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in self.domains or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.fragment:
            raise AdapterError("OFFICIAL_IR_URL_NOT_ALLOWED")
        body = self._get(url).body
        return FetchResult(({"url": url, "content": body, "sha256": hashlib.sha256(body).hexdigest(), "published_at": published_at},),
                           "COMPLETE" if body else "FETCH_FAILED", utcnow(), metadata={"published_at_verified": False})


def unpack_documents(content):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 100 or sum(x.file_size for x in entries) > 32 * 1024 * 1024:
                raise AdapterError("DOCUMENT_SIZE_LIMIT")
            # Never extract provider-supplied paths into the filesystem.
            result = [(item.filename, archive.read(item)) for item in entries if not item.is_dir()]
            if not result:
                raise AdapterError("DOCUMENT_FETCH_FAILED")
            return result
    except (zipfile.BadZipFile, RuntimeError):
        raise AdapterError("DOCUMENT_FETCH_FAILED") from None
