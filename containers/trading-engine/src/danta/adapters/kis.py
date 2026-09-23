"""KIS direct REST contract; token renewal happens before requests, never on retry."""

import fcntl
import hashlib
import io
import json
import os
import re
import stat
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import AdapterError, FetchResult, http_transport, require_http_ok, utcnow
from ..safety import CREDENTIAL_TEXT, reject_credentials

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


class OrderNotSent(AdapterError):
    """A local failure before the broker transport was invoked."""


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
    try:
        response = request("POST", BASE_URLS[environment] + "/oauth2/tokenP", {"content-type": "application/json"},
                           json.dumps({"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}).encode(), 15)
    except OSError:
        raise AdapterError("AUTH_TRANSPORT_FAILED") from None
    require_http_ok(response)
    body = response.json()
    try:
        token = body["access_token"]
        expiry = datetime.strptime(body["access_token_token_expired"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Seoul"))
        if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,16384}", token):
            raise ValueError
        return KisToken(token, expiry)
    except (ValueError, TypeError, KeyError):
        raise AdapterError("AUTH_FAILED") from None


class KisTokenCache:
    """Private per-account cache; a file lock covers read, renewal and replacement."""

    def __init__(self, environment, app_key, app_secret, path, transport=None, mode="offline", authorize=None, clock=utcnow):
        if environment not in BASE_URLS:
            raise AdapterError("BROKER_ENVIRONMENT_UNSET")
        if not all(isinstance(value, str) and value for value in (app_key, app_secret)):
            raise AdapterError("AUTH_CREDENTIALS_REQUIRED")
        self.environment, self.app_key, self.app_secret = environment, app_key, app_secret
        self.path = Path(os.path.abspath(path))
        self.identity = hashlib.sha256(json.dumps([app_key, app_secret]).encode()).hexdigest()
        self.transport = transport or http_transport(allowed_origins={BASE_URLS[environment]})
        self.mode, self.authorize, self.clock = mode, authorize, clock

    @staticmethod
    def _open(directory, name, flags):
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            os.close(fd)
            raise AdapterError("TOKEN_CACHE_UNSAFE")
        return fd

    def _now(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise AdapterError("TOKEN_CLOCK_INVALID")
        return now

    def _read(self, directory):
        try:
            fd = self._open(directory, self.path.name, os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            try:
                data = json.loads(stream.read(32769))
                if not isinstance(data, dict) or set(data) != {"environment", "identity", "access_token", "expires_at"}:
                    raise ValueError
                token = data["access_token"]
                expiry = datetime.fromisoformat(data["expires_at"])
                if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,16384}", token) or expiry.utcoffset() is None:
                    raise ValueError
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise AdapterError("TOKEN_CACHE_INVALID") from None
        if data["environment"] == self.environment and data["identity"] == self.identity and expiry > self._now() + timedelta(seconds=60):
            return token
        return None

    def _write(self, directory, token):
        name = ".kis-token-" + uuid4().hex + ".tmp"
        fd = self._open(directory, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"environment": self.environment, "identity": self.identity,
                           "access_token": token.access_token, "expires_at": token.expires_at.isoformat()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass

    def __call__(self, *, allow_refresh=True):
        if self.mode == "offline" and not getattr(self.transport, "fixture_only", False):
            raise AdapterError("OFFLINE_NETWORK_BLOCKED")
        if self.mode != "offline":
            if self.authorize is None:
                raise AdapterError("AUTHORIZATION_REQUIRED")
            self.authorize("broker_auth", self.environment)
        directory = None
        try:
            if any(parent.is_symlink() for parent in self.path.parents):
                raise AdapterError("TOKEN_CACHE_UNSAFE")
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise AdapterError("TOKEN_CACHE_UNSAFE")
            with os.fdopen(self._open(directory, self.path.name + ".lock", os.O_RDWR | os.O_CREAT), "r+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | (0 if allow_refresh else fcntl.LOCK_NB))
                except BlockingIOError:
                    raise AdapterError("TOKEN_REFRESH_REQUIRED") from None
                cached = self._read(directory)
                if cached is not None:
                    return cached
                if not allow_refresh:
                    raise AdapterError("TOKEN_REFRESH_REQUIRED")
                token = issue_token(environment=self.environment, app_key=self.app_key, app_secret=self.app_secret,
                                    transport=self.transport, mode=self.mode, authorize=self.authorize)
                if token.expires_at <= self._now() + timedelta(seconds=60):
                    raise AdapterError("TOKEN_EXPIRY_INVALID")
                self._write(directory, token)
                return token.access_token
        except OSError:
            raise AdapterError("TOKEN_CACHE_IO_FAILED") from None
        finally:
            if directory is not None:
                os.close(directory)


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
    def __init__(self, *, environment, credentials, transport=None, mode="offline", authorize=None, max_pages=100, token_provider=None, clock=utcnow, ws_connector=None):
        if environment not in BASE_URLS:
            raise AdapterError("BROKER_ENVIRONMENT_UNSET")
        self.environment, self.credentials, self.mode = environment, credentials, mode
        self.base_url = BASE_URLS[environment]
        self.transport = transport or http_transport(allowed_origins={self.base_url, MASTER_ORIGIN})
        self.authorize = authorize
        self.max_pages = max_pages
        self.token_provider = token_provider
        self.clock = clock
        self.ws_connector, self._stream = ws_connector, None
        self._stream_lock, self._closed = threading.Lock(), False

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

    def _request(self, path, tr_id, params, *, post=False, continuation="", valid_until=None):
        operation = "broker_write" if post else "broker_read" if path.startswith(TRADING) else "market_read"
        self._permit(operation)
        c = self.credentials
        try:
            token = self.token_provider(allow_refresh=not post) if self.token_provider is not None else c.token
        except Exception as error:
            if post:
                raise OrderNotSent(getattr(error, "code", type(error).__name__)) from None
            raise
        headers = {"content-type": "application/json; charset=utf-8", "authorization": "Bearer " + token,
                   "appkey": c.app_key, "appsecret": c.app_secret, "custtype": "P", "tr_id": tr_id, "tr_cont": continuation}
        def before_send():
            try:
                self._permit(operation)
                if valid_until is not None and self.clock() >= valid_until:
                    raise AdapterError("ORDER_VALIDITY_EXPIRED")
            except Exception as error:
                if post:
                    raise OrderNotSent(getattr(error, "code", type(error).__name__)) from None
                raise
        args = ("POST" if post else "GET", self.base_url + path + ("" if post else "?" + urlencode(params)),
                headers, json.dumps(params).encode() if post else None, 15)
        started, requested_at = time.monotonic(), self.clock().isoformat()
        response = None
        try:
            if post and hasattr(self.transport, "request_checked"):
                response = self.transport.request_checked(*args, before_send=before_send)
            else:
                before_send()
                response = self.transport(*args)
            require_http_ok(response)
        except AdapterError as error:
            error.diagnostic.update(endpoint=path.rsplit('/',1)[-1], requested_at=requested_at,
                                    elapsed_seconds=round(time.monotonic()-started,3))
            if response is not None:
                try:
                    message = response.json().get('msg1')
                    if isinstance(message,str):
                        for private in (c.account,c.app_key,c.app_secret,c.token,token):
                            if private:
                                message = message.replace(private,'[비공개]')
                        message = CREDENTIAL_TEXT.sub('[비공개]',message)
                        message = re.sub(r'https?://\S+|[A-Za-z0-9_.~-]{24,}|\d{6,}', '[비공개]', message)
                        message = ' '.join(message.split())[:240]
                        reject_credentials(message)
                        error.diagnostic['provider_message'] = message
                except (AdapterError,AttributeError,ValueError):
                    pass
            raise
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

    def _pages(self, path, tr, params, cursor=None, *, rows_key="output1", cursor_width=100):
        rows, summaries, seen = [], [], set()
        metadata = {"endpoint": path.rsplit("/", 1)[-1], "summaries": summaries}
        cursor = cursor or ("", "")
        try:
            for _ in range(self.max_pages):
                if cursor in seen:
                    raise AdapterError("REPEATED_CURSOR")
                seen.add(cursor)
                for attempt in range(3):
                    try:
                        data, headers = self._request(path, tr, {**params, f"CTX_AREA_FK{cursor_width}": cursor[0], f"CTX_AREA_NK{cursor_width}": cursor[1]}, continuation="N" if any(cursor) else "")
                        break
                    except (AdapterError, OSError) as error:
                        code = error.code if isinstance(error, AdapterError) else 'TRANSPORT_FAILED'
                        details = getattr(error, 'diagnostic', {})
                        delay = details.get('retry_after_seconds', 0.5 * (2 ** attempt))
                        # Replay only the failed read page; submissions are never retried here.
                        if isinstance(error,AdapterError):
                            error.diagnostic['attempt_count'] = attempt + 1
                        if attempt == 2 or code not in {'TRANSIENT_FAILURE', 'TRANSPORT_FAILED', 'RATE_LIMITED', 'BROKER_REJECTED:EGW00201'} or delay > 2:
                            raise
                        time.sleep(delay)
                if not isinstance(data.get(rows_key), list) or any(not isinstance(row, dict) for row in data[rows_key]):
                    raise AdapterError("MALFORMED_RESPONSE")
                rows.extend(data[rows_key])
                summaries.append(data.get("output2"))
                continuation = headers.get("tr_cont", "")
                if not isinstance(continuation, str) or continuation.strip() not in {"", "D", "E", "M", "F"}:
                    raise AdapterError("INVALID_CONTINUATION")
                if continuation.strip() not in {"M", "F"}:
                    return FetchResult(tuple(rows), "COMPLETE", utcnow(), metadata=metadata)
                # KIS cursors are opaque: send the previous response unchanged.
                cursor = (data.get(f"ctx_area_fk{cursor_width}", ""), data.get(f"ctx_area_nk{cursor_width}", ""))
                if any(not isinstance(value, str) for value in cursor):
                    raise AdapterError("MALFORMED_CURSOR")
                if not any(value.strip() for value in cursor):
                    raise AdapterError("MISSING_CURSOR")
        except (AdapterError, OSError) as exc:
            return FetchResult(tuple(rows), "PARTIAL" if rows else "FETCH_FAILED", utcnow(), cursor,
                               dict(metadata, error=exc.code if isinstance(exc, AdapterError) else "TRANSPORT_FAILED",
                                    **getattr(exc, 'diagnostic', {}), failed_page=len(summaries) + 1))
        return FetchResult(tuple(rows), "PARTIAL", utcnow(), cursor, dict(metadata, error="PAGE_LIMIT"))

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

    def read_buying_power(self, ticker, price):
        # KIS requires market-price calculation to include the symbol margin ratio.
        data, _ = self._request(TRADING + "inquire-psbl-order", self._tr("TTC8908R"), {
            **self._account_params(), "PDNO": symbol(ticker), "ORD_UNPR": str(order_price(price)),
            "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"})
        if not isinstance(data.get("output"), dict):
            raise AdapterError("MALFORMED_RESPONSE")
        return data["output"]

    def read_cancelable_orders(self):
        return self._pages(TRADING + "inquire-psbl-rvsecncl", "TTTC0084R", {
            **self._account_params(), "INQR_DVSN_1": "0", "INQR_DVSN_2": "0"}, rows_key="output")

    def read_reservations(self, start: date, end: date):
        if self.environment != "real" or start > end:
            raise AdapterError("RESERVATION_QUERY_UNSUPPORTED")
        return self._pages(TRADING + "order-resv-ccnl", "CTSC0004R", {
            **self._account_params(), "RSVN_ORD_ORD_DT": start.strftime("%Y%m%d"),
            "RSVN_ORD_END_DT": end.strftime("%Y%m%d"), "TMNL_MDIA_KIND_CD": "00",
            "PRCS_DVSN_CD": "0", "CNCL_YN": "Y", "RSVN_ORD_SEQ": "", "PDNO": "",
            "SLL_BUY_DVSN_CD": ""}, rows_key="output", cursor_width=200)

    def read_daily_costs(self, start: date, end: date):
        if self.environment != "real" or start > end:
            raise AdapterError("COST_QUERY_UNSUPPORTED")
        return self._pages(TRADING + "inquire-period-profit", "TTTC8708R", {
            **self._account_params(), "INQR_STRT_DT": start.strftime("%Y%m%d"),
            "INQR_END_DT": end.strftime("%Y%m%d"), "SORT_DVSN": "01", "INQR_DVSN": "00",
            "CBLC_DVSN": "00", "PDNO": ""})

    def read_orders(self, start: date, end: date, *, cursor=None, older_than_three_months=False):
        if start > end:
            raise AdapterError("INVALID_DATE_RANGE")
        tr = self._tr("TSC9215R" if older_than_three_months else "TTC0081R")
        if older_than_three_months and self.environment == "real":
            tr = "CTSC9215R"
        return self._pages(TRADING + "inquire-daily-ccld", tr, {**self._account_params(), "INQR_STRT_DT": start.strftime("%Y%m%d"),
            "INQR_END_DT": end.strftime("%Y%m%d"), "SLL_BUY_DVSN_CD": "00", "PDNO": "", "CCLD_DVSN": "00", "INQR_DVSN": "00",
            "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_1": "", "EXCG_ID_DVSN_CD": "ALL" if self.environment == "real" else "KRX"}, cursor)

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

    def _mutation(self, path, tr, params, *, valid_until=None):
        try:
            data, _ = self._request(path, tr, params, post=True, valid_until=valid_until)
            output = data.get("output", {})
            order_id = output.get("ODNO") or output.get("odno")
            if not order_id:
                return BrokerResult("UNKNOWN", code="ACK_WITHOUT_ORDER_ID")
            return BrokerResult("ACKNOWLEDGED", str(order_id), output.get("KRX_FWDG_ORD_ORGNO") or output.get("krx_fwdg_ord_orgno"))
        except OrderNotSent as exc:
            return BrokerResult("NOT_SENT", code=exc.code)
        except AdapterError as exc:
            if exc.code.startswith("BROKER_REJECTED:"):
                return BrokerResult("REJECTED", code=exc.code.split(":", 1)[1])
            if exc.code in {"TRANSPORT_FAILED", "TRANSIENT_FAILURE", "MALFORMED_RESPONSE", "HTTP_FAILURE"}:
                return BrokerResult("UNKNOWN", code=exc.code)
            raise
        except (TimeoutError, OSError):
            return BrokerResult("UNKNOWN", code="TRANSPORT_FAILED")

    def submit(self, ticker, side, quantity, *, limit_price=None, valid_until=None):
        if type(quantity) is not int or quantity <= 0 or side not in {"BUY", "SELL"}:
            raise AdapterError("INVALID_ORDER")
        if side == "BUY" and limit_price is None:
            raise AdapterError("BUY_REQUIRES_LIMIT")
        if limit_price is not None:
            limit_price = order_price(limit_price)
        return self._mutation(TRADING + "order-cash", self._tr("TTC0012U" if side == "BUY" else "TTC0011U"), {
            **self._account_params(), "PDNO": symbol(ticker), "ORD_DVSN": "00" if limit_price is not None else "01", "ORD_QTY": str(quantity),
            "ORD_UNPR": str(limit_price or 0), "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if side == "SELL" else "", "CNDT_PRIC": ""}, valid_until=valid_until)

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
        from .kis_stream import KisQuoteStream

        if not isinstance(tickers, (list, tuple, set, frozenset)):
            raise AdapterError("STREAM_SYMBOLS_INVALID")
        targets = {symbol(ticker) for ticker in tickers}
        if len(targets) > 41:
            raise AdapterError("STREAM_SUBSCRIPTION_LIMIT")
        self._permit("market_read")
        if self.mode == "offline" and not getattr(self.ws_connector, "fixture_only", False):
            raise AdapterError("OFFLINE_NETWORK_BLOCKED")
        with self._stream_lock:
            if self._closed:
                raise AdapterError("STREAM_CLOSED")
            if self._stream is None:
                self._stream = KisQuoteStream(environment=self.environment, approval=self._approval,
                    permit=lambda: self._permit("market_read"), connector=self.ws_connector, clock=self.clock)
            self._stream.replace(targets)

    def _approval(self):
        def before_send():
            self._permit("broker_auth")
            if self._closed:
                raise AdapterError("STREAM_CLOSED")
        before_send()
        c = self.credentials
        args = ("POST", self.base_url + "/oauth2/Approval", {"content-type": "application/json; charset=utf-8"},
                json.dumps({"grant_type": "client_credentials", "appkey": c.app_key, "secretkey": c.app_secret}).encode(), 3)
        try:
            if hasattr(self.transport, "request_checked"):
                response = self.transport.request_checked(*args, before_send=before_send)
            else:
                before_send()
                response = self.transport(*args)
            require_http_ok(response)
            key = response.json()["approval_key"]
            if not isinstance(key, str) or not re.fullmatch(r"[!-~]{1,16384}", key):
                raise ValueError
            return key
        except Exception:
            raise AdapterError("STREAM_AUTH_FAILED") from None

    def stream_quote(self, ticker):
        self._permit("market_read")
        if self._closed:
            raise AdapterError("STREAM_CLOSED")
        if self._stream is None:
            raise AdapterError("STREAM_NOT_READY")
        return self._stream.quote(symbol(ticker))

    def poll_quote(self, ticker):
        """Refresh an inactive trade feed with an independently timestamped KRX book."""
        ticker = symbol(ticker)
        self._permit("market_read")
        if self._stream is None:
            raise AdapterError("STREAM_NOT_READY")
        day = self._stream.session_date(ticker)
        data, _ = self._request(QUOTATIONS + "inquire-asking-price-exp-ccn", "FHKST01010200",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker})
        asking = data.get("output1")
        if not isinstance(asking, dict):
            raise AdapterError("MALFORMED_RESPONSE")
        if self._stream.session_date(ticker) != day:
            raise AdapterError("STREAM_SESSION_UNVERIFIED")
        return FetchResult(({"session_date": day, "asking": asking},), "COMPLETE", self.clock(),
            metadata={"source": "KIS:FHKST01010200;session_date=H0STCNT0"})

    def close(self):
        with self._stream_lock:
            self._closed, stream = True, self._stream
        if stream is not None:
            stream.close()


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
    code = head[:9].strip()
    # The master also contains longer ETN/fund codes. They remain excluded by
    # their product group; six-character order-symbol validation is unchanged.
    if fields[0] == "ST":
        symbol(code)
    elif not re.fullmatch(r"[A-Z0-9]{6,9}", code):
        raise AdapterError("MASTER_CONTRACT_MISMATCH")
    return {"symbol": code, "isin": head[9:21].strip(), "name": head[21:].strip(), "board": board,
            **{name: fields[index] for name, index in indexes.items()}, "status_requires_validated_provider_codes": True}
