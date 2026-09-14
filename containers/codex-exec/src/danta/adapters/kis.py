"""KIS direct REST contract; mutations have no implicit retry or token refresh."""

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from . import AdapterError, FetchResult, http_transport, require_http_ok, utcnow

BASE_URLS = {"real": "https://openapi.koreainvestment.com:9443", "demo": "https://openapivts.koreainvestment.com:29443"}
MASTER_ORIGIN = "https://new.real.download.dws.co.kr"
TRADING = "/uapi/domestic-stock/v1/trading/"
QUOTATIONS = "/uapi/domestic-stock/v1/quotations/"


@dataclass(frozen=True)
class KisCredentials:
    account: str = field(repr=False)
    product: str = field(repr=False)
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    token: str = field(repr=False)

    def __post_init__(self):
        if not re.fullmatch(r"\d{8}", self.account) or not re.fullmatch(r"\d{2}", self.product):
            raise AdapterError("INVALID_ACCOUNT_REFERENCE")


@dataclass(frozen=True)
class BrokerResult:
    status: str
    order_id: str | None = None
    organization: str | None = None
    code: str | None = None


@dataclass(frozen=True)
class KisToken:
    access_token: str = field(repr=False)
    expires_at: datetime


def issue_token(*, environment, app_key, app_secret, transport=None, mode="offline", authorize=None):
    """Explicit authorized authentication. Never called by an order retry."""
    if environment not in BASE_URLS:
        raise AdapterError("BROKER_ENVIRONMENT_UNSET")
    request = transport or http_transport(allowed_origins={BASE_URLS[environment]})
    if mode == "offline" and not getattr(request, "fixture_only", False):
        raise AdapterError("OFFLINE_NETWORK_BLOCKED")
    if mode != "offline":
        if authorize is None:
            raise AdapterError("AUTHORIZATION_REQUIRED")
        authorize("broker_auth", environment)
    response = request("POST", BASE_URLS[environment] + "/oauth2/tokenP", {"content-type": "application/json"},
                       json.dumps({"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}).encode(), 15)
    require_http_ok(response)
    body = response.json()
    try:
        token = body["access_token"]
        expiry = datetime.strptime(body["access_token_token_expired"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Seoul"))
        if not isinstance(token, str) or not token:
            raise ValueError
        return KisToken(token, expiry)
    except (ValueError, TypeError, KeyError):
        raise AdapterError("AUTH_FAILED") from None


def order_price(value):
    try:
        if isinstance(value, (bool, float)):
            raise ValueError
        parsed = Decimal(value)
        if not parsed.is_finite() or parsed <= 0 or parsed % 1:
            raise ValueError
        return parsed
    except (TypeError, ValueError, InvalidOperation):
        raise AdapterError("INVALID_PRICE") from None


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{6}", value):
        raise AdapterError("INVALID_SYMBOL")
    return value


class KisAdapter:
    def __init__(self, *, environment, credentials, transport=None, mode="offline", authorize=None, max_pages=100):
        if environment not in BASE_URLS:
            raise AdapterError("BROKER_ENVIRONMENT_UNSET")
        self.environment, self.credentials, self.mode = environment, credentials, mode
        self.base_url = BASE_URLS[environment]
        self.transport = transport or http_transport(allowed_origins={self.base_url, MASTER_ORIGIN})
        self.authorize = authorize
        self.max_pages = max_pages

    def _permit(self, operation):
        if self.mode == "offline" and not getattr(self.transport, "fixture_only", False):
            raise AdapterError("OFFLINE_NETWORK_BLOCKED")
        if self.mode != "offline":
            if self.authorize is None:
                raise AdapterError("AUTHORIZATION_REQUIRED")
            self.authorize(operation, self.environment)
        if operation == "broker_write" and self.mode not in {"live", "broker_demo", "offline"}:
            raise AdapterError("BROKER_WRITE_NOT_ALLOWED")
        if self.mode == "live" and self.environment != "real" or self.mode == "broker_demo" and self.environment != "demo":
            raise AdapterError("BROKER_ENVIRONMENT_MISMATCH")

    def _request(self, path, tr_id, params, *, post=False, continuation=""):
        self._permit("broker_write" if post else "broker_read" if path.startswith(TRADING) else "market_read")
        c = self.credentials
        headers = {"content-type": "application/json; charset=utf-8", "authorization": "Bearer " + c.token,
                   "appkey": c.app_key, "appsecret": c.app_secret, "custtype": "P", "tr_id": tr_id, "tr_cont": continuation}
        response = self.transport("POST" if post else "GET", self.base_url + path + ("" if post else "?" + urlencode(params)),
                                  headers, json.dumps(params).encode() if post else None, 15)
        require_http_ok(response)
        data = response.json()
        if not isinstance(data, dict) or "rt_cd" not in data:
            raise AdapterError("MALFORMED_RESPONSE")
        if data["rt_cd"] != "0":
            # A well-formed broker rejection is different from a lost POST response.
            raise AdapterError("BROKER_REJECTED:" + str(data.get("msg_cd", "UNKNOWN")))
        return data, {k.lower(): v for k, v in response.headers.items()}

    def _tr(self, suffix):
        return ("T" if self.environment == "real" else "V") + suffix

    def _account_params(self):
        return {"CANO": self.credentials.account, "ACNT_PRDT_CD": self.credentials.product}

    def _pages(self, path, tr, params, cursor=None):
        rows, summaries, seen = [], [], set()
        cursor = cursor or ("", "")
        try:
            for _ in range(self.max_pages):
                if cursor in seen:
                    raise AdapterError("REPEATED_CURSOR")
                seen.add(cursor)
                data, headers = self._request(path, tr, {**params, "CTX_AREA_FK100": cursor[0], "CTX_AREA_NK100": cursor[1]}, continuation="N" if any(cursor) else "")
                if not isinstance(data.get("output1"), list):
                    raise AdapterError("MALFORMED_RESPONSE")
                rows.extend(data["output1"])
                summaries.append(data.get("output2"))
                if headers.get("tr_cont") not in {"M", "F"}:
                    return FetchResult(tuple(rows), "COMPLETE", utcnow(), metadata={"summaries": summaries})
                cursor = (data.get("ctx_area_fk100", "").strip(), data.get("ctx_area_nk100", "").strip())
                if not any(cursor):
                    raise AdapterError("MISSING_CURSOR")
        except AdapterError as exc:
            return FetchResult(tuple(rows), "PARTIAL" if rows else "FETCH_FAILED", utcnow(), cursor, {"error": exc.code, "summaries": summaries})
        return FetchResult(tuple(rows), "PARTIAL", utcnow(), cursor, {"error": "PAGE_LIMIT", "summaries": summaries})

    def read_account(self, *, resource_symbol=None, resource_price=None):
        result = self._pages(TRADING + "inquire-balance", self._tr("TTC8434R"), {**self._account_params(),
            "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00"})
        metadata = dict(result.metadata, orderable_resources=None, resources_quality="NOT_QUERIED")
        quality = result.quality
        if resource_symbol is not None:
            resource_price = order_price(resource_price)
            try:
                data, _ = self._request(TRADING + "inquire-psbl-order", self._tr("TTC8908R"), {**self._account_params(),
                    "PDNO": symbol(resource_symbol), "ORD_UNPR": str(resource_price), "ORD_DVSN": "00", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"})
                metadata.update(orderable_resources=data["output"], resources_quality="COMPLETE")
            except (AdapterError, KeyError):
                quality = "PARTIAL"
                metadata["resources_quality"] = "FETCH_FAILED"
        return FetchResult(result.records, quality, result.retrieved_at, result.next_cursor, metadata)

    def read_orders(self, start: date, end: date, *, cursor=None, older_than_three_months=False):
        if start > end:
            raise AdapterError("INVALID_DATE_RANGE")
        tr = self._tr("TSC9215R" if older_than_three_months else "TTC0081R")
        if older_than_three_months and self.environment == "real":
            tr = "CTSC9215R"
        return self._pages(TRADING + "inquire-daily-ccld", tr, {**self._account_params(), "INQR_STRT_DT": start.strftime("%Y%m%d"),
            "INQR_END_DT": end.strftime("%Y%m%d"), "SLL_BUY_DVSN_CD": "00", "PDNO": "", "CCLD_DVSN": "00", "INQR_DVSN": "00",
            "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_1": "", "EXCG_ID_DVSN_CD": "KRX"}, cursor)

    def read_fills(self, start, end, **kwargs):
        result = self.read_orders(start, end, **kwargs)
        # This provider endpoint exposes order-cumulative fills, not invented fill ids.
        return FetchResult(result.records, result.quality, result.retrieved_at, result.next_cursor,
                           dict(result.metadata, fill_identity="order_cumulative_quantity"))

    def quote(self, ticker):
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol(ticker)}
        quote, _ = self._request(QUOTATIONS + "inquire-asking-price-exp-ccn", "FHKST01010200", params)
        price, _ = self._request(QUOTATIONS + "inquire-price", "FHKST01010100", params)
        if not isinstance(quote.get("output1"), dict) or not isinstance(price.get("output"), dict):
            raise AdapterError("MALFORMED_RESPONSE")
        return FetchResult(({"symbol": ticker, "asking": quote["output1"], "price": price["output"]},), "COMPLETE", utcnow(),
                           metadata={"venue": "KRX", "provider_time_field": "aspr_acpt_hour", "tick_field": "aspr_unit", "exchange_session_date_required": True})

    def read_bars(self, ticker, start: date, end: date, *, adjusted=True):
        return self._historical_bars(symbol(ticker), start, end, adjusted=adjusted)

    def read_index_bars(self, board, start: date, end: date):
        if board not in {"KOSPI", "KOSDAQ"}:
            raise AdapterError("INVALID_BOARD")
        return self._historical_bars({"KOSPI": "0001", "KOSDAQ": "1001"}[board], start, end, index=True)

    def _historical_bars(self, ticker, start, end, *, adjusted=True, index=False):
        if start > end:
            raise AdapterError("INVALID_DATE_RANGE")
        rows, upper = {}, end
        try:
            for _ in range(self.max_pages):
                params = {"FID_COND_MRKT_DIV_CODE": "U" if index else "J", "FID_INPUT_ISCD": ticker,
                          "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": upper.strftime("%Y%m%d"), "FID_PERIOD_DIV_CODE": "D"}
                if not index:
                    params["FID_ORG_ADJ_PRC"] = "0" if adjusted else "1"
                data, _ = self._request(QUOTATIONS + ("inquire-daily-indexchartprice" if index else "inquire-daily-itemchartprice"),
                                        "FHKUP03500100" if index else "FHKST03010100", params)
                page = data.get("output2")
                if not isinstance(page, list) or not page:
                    break
                oldest = upper
                for row in page:
                    day = date.fromisoformat(row["stck_bsop_date"])
                    if day > upper or day < start:
                        continue
                    oldest = min(oldest, day)
                    rows[day] = row
                if oldest <= start:
                    return FetchResult(tuple(rows[d] for d in sorted(rows)), "COMPLETE", utcnow(), metadata={"adjustment": "provider_adjusted" if adjusted else "raw", "point_in_time_adjustment_verified": False})
                if oldest == upper and upper not in rows:
                    break
                upper = oldest - timedelta(days=1)
        except (AdapterError, ValueError, KeyError):
            pass
        # Completeness needs the exchange calendar: a weekend start is not proof of a gap.
        return FetchResult(tuple(rows[d] for d in sorted(rows)), "PARTIAL" if rows else "FETCH_FAILED", utcnow(), upper,
                           {"adjustment": "provider_adjusted" if adjusted else "raw", "coverage_requires_calendar": True, "point_in_time_adjustment_verified": False})

    def read_instruments(self, board):
        if board not in {"KOSPI", "KOSDAQ"}:
            raise AdapterError("INVALID_BOARD")
        self._permit("market_read")
        filename = board.lower() + "_code.mst"
        response = self.transport("GET", MASTER_ORIGIN + "/common/master/" + filename + ".zip", {}, None, 15)
        require_http_ok(response)
        try:
            with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
                info = archive.getinfo(filename)
                if info.file_size > 32 * 1024 * 1024:
                    raise AdapterError("RESPONSE_TOO_LARGE")
                content = archive.read(filename).decode("cp949")
            rows = tuple(parse_master_line(line, board) for line in content.splitlines())
        except (ValueError, KeyError, UnicodeError, zipfile.BadZipFile):
            raise AdapterError("MASTER_CONTRACT_MISMATCH") from None
        return FetchResult(rows, "COMPLETE" if rows else "FETCH_FAILED", utcnow(), metadata={"board": board, "point_in_time": False})

    def _mutation(self, path, tr, params):
        try:
            data, _ = self._request(path, tr, params, post=True)
            output = data.get("output", {})
            order_id = output.get("ODNO") or output.get("odno")
            if not order_id:
                return BrokerResult("UNKNOWN", code="ACK_WITHOUT_ORDER_ID")
            return BrokerResult("ACKNOWLEDGED", str(order_id), output.get("KRX_FWDG_ORD_ORGNO") or output.get("krx_fwdg_ord_orgno"))
        except AdapterError as exc:
            if exc.code.startswith("BROKER_REJECTED:"):
                return BrokerResult("REJECTED", code=exc.code.split(":", 1)[1])
            if exc.code in {"TRANSPORT_FAILED", "TRANSIENT_FAILURE", "MALFORMED_RESPONSE", "HTTP_FAILURE"}:
                return BrokerResult("UNKNOWN", code=exc.code)
            raise
        except (TimeoutError, OSError):
            return BrokerResult("UNKNOWN", code="TRANSPORT_FAILED")

    def submit(self, ticker, side, quantity, *, limit_price=None):
        if type(quantity) is not int or quantity <= 0 or side not in {"BUY", "SELL"}:
            raise AdapterError("INVALID_ORDER")
        if side == "BUY" and limit_price is None:
            raise AdapterError("BUY_REQUIRES_LIMIT")
        if limit_price is not None:
            limit_price = order_price(limit_price)
        return self._mutation(TRADING + "order-cash", self._tr("TTC0012U" if side == "BUY" else "TTC0011U"), {
            **self._account_params(), "PDNO": symbol(ticker), "ORD_DVSN": "00" if limit_price is not None else "01", "ORD_QTY": str(quantity),
            "ORD_UNPR": str(limit_price or 0), "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if side == "SELL" else "", "CNDT_PRIC": ""})

    def cancel(self, order_id, organization, quantity, *, order_type="00"):
        return self._revise(order_id, organization, quantity, "02", "0", order_type)

    def replace(self, order_id, organization, quantity, *, limit_price):
        limit_price = order_price(limit_price)
        return self._revise(order_id, organization, quantity, "01", str(limit_price), "00")

    def _revise(self, order_id, organization, quantity, kind, price, order_type):
        if type(quantity) is not int or quantity <= 0 or not str(order_id).isdigit() or not str(organization).isdigit() or order_type not in {"00", "01"}:
            raise AdapterError("INVALID_ORDER")
        return self._mutation(TRADING + "order-rvsecncl", self._tr("TTC0013U"), {**self._account_params(),
            "KRX_FWDG_ORD_ORGNO": organization, "ORGN_ODNO": order_id, "ORD_DVSN": order_type, "RVSE_CNCL_DVSN_CD": kind,
            "ORD_QTY": str(quantity), "ORD_UNPR": price, "QTY_ALL_ORD_YN": "N", "EXCG_ID_DVSN_CD": "KRX"})

    def subscribe_quotes(self, tickers):
        # Polling is the implemented fallback; no fake stream capability or native stop.
        raise AdapterError("STREAM_CAPABILITY_UNVERIFIED_USE_APPROVED_POLLING")


_KOSPI_WIDTHS = [2,1,4,4,4,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,9,5,5,1,1,1,2,1,1,1,2,2,2,3,1,3,12,12,8,15,21,2,7,1,1,1,1,1,9,9,9,5,9,8,9,3,1,1,1]
_KOSDAQ_WIDTHS = [2,1,4,4,4,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,9,5,5,1,1,1,2,1,1,1,2,2,2,3,1,3,12,12,8,15,21,2,7,1,1,1,1,9,9,9,5,9,8,9,3,1,1,1]


def parse_master_line(line, board):
    widths = _KOSPI_WIDTHS if board == "KOSPI" else _KOSDAQ_WIDTHS
    size = sum(widths)
    if len(line) < size + 21:
        raise AdapterError("MASTER_CONTRACT_MISMATCH")
    head, tail = line[:-size], line[-size:]
    fields, pos = [], 0
    for width in widths:
        fields.append(tail[pos:pos + width].strip())
        pos += width
    indexes = {"group": 0, "industry": 2, "etp": 12, "spac": 19, "halted": 34, "liquidation": 35, "managed": 36, "preferred": 54} if board == "KOSPI" else {"group": 0, "industry": 2, "etp": 8, "spac": 14, "halted": 29, "liquidation": 30, "managed": 31, "preferred": 49}
    return {"symbol": symbol(head[:9].strip()), "isin": head[9:21].strip(), "name": head[21:].strip(), "board": board,
            **{name: fields[index] for name, index in indexes.items()}, "status_requires_validated_provider_codes": True}
