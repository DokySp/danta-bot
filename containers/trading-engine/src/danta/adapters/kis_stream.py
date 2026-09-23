"""One authorized KIS quote session; cached data never survives disconnection."""

import json
import re
import socket
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from . import AdapterError, FetchResult, utcnow


URLS = {"real": "ws://ops.koreainvestment.com:21000", "demo": "ws://ops.koreainvestment.com:31000"}
SEOUL = ZoneInfo("Asia/Seoul")
# KIS portal H0STCNT0, updated 2026-09-11. GitHub's 46-field sample predates MARKET_CLS_CODE.
# https://apiportal.koreainvestment.com/apiservice-apiservice?/tryitout/H0STCNT0
COLUMNS = tuple("""MKSC_SHRN_ISCD STCK_CNTG_HOUR STCK_PRPR PRDY_VRSS_SIGN PRDY_VRSS PRDY_CTRT
WGHN_AVRG_STCK_PRC STCK_OPRC STCK_HGPR STCK_LWPR ASKP1 BIDP1 CNTG_VOL ACML_VOL ACML_TR_PBMN
SELN_CNTG_CSNU SHNU_CNTG_CSNU NTBY_CNTG_CSNU CTTR SELN_CNTG_SMTN SHNU_CNTG_SMTN CCLD_DVSN SHNU_RATE
PRDY_VOL_VRSS_ACML_VOL_RATE OPRC_HOUR OPRC_VRSS_PRPR_SIGN OPRC_VRSS_PRPR HGPR_HOUR HGPR_VRSS_PRPR_SIGN
HGPR_VRSS_PRPR LWPR_HOUR LWPR_VRSS_PRPR_SIGN LWPR_VRSS_PRPR BSOP_DATE NEW_MKOP_CLS_CODE TRHT_YN
ASKP_RSQN1 BIDP_RSQN1 TOTAL_ASKP_RSQN TOTAL_BIDP_RSQN VOL_TNRT PRDY_SMNS_HOUR_ACML_VOL
PRDY_SMNS_HOUR_ACML_VOL_RATE HOUR_CLS_CODE MRKT_TRTM_CLS_CODE VI_STND_PRC MARKET_CLS_CODE""".split())
# KIS official examples_llm/domestic_stock/asking_price_krx/asking_price_krx.py
BOOK_COLUMNS = (('MKSC_SHRN_ISCD','BSOP_HOUR','HOUR_CLS_CODE') +
                tuple(f'{prefix}{i}' for prefix in ('ASKP','BIDP','ASKP_RSQN','BIDP_RSQN') for i in range(1,11)) +
                tuple('TOTAL_ASKP_RSQN TOTAL_BIDP_RSQN OVTM_TOTAL_ASKP_RSQN OVTM_TOTAL_BIDP_RSQN ANTC_CNPR ANTC_CNQN ANTC_VOL ANTC_CNTG_VRSS ANTC_CNTG_VRSS_SIGN ANTC_CNTG_PRDY_CTRT ACML_VOL TOTAL_ASKP_RSQN_ICDC TOTAL_BIDP_RSQN_ICDC OVTM_TOTAL_ASKP_ICDC OVTM_TOTAL_BIDP_ICDC STCK_DEAL_CLS_CODE'.split()))


def connect(url):
    """Direct socket avoids environment proxies; no redirect can receive an approval key."""
    import websocket

    if url not in URLS.values():
        raise AdapterError("STREAM_ORIGIN_NOT_ALLOWED")
    parsed = urlsplit(url)
    raw = socket.create_connection((parsed.hostname, parsed.port), timeout=2)
    ws = websocket.WebSocket(enable_multithread=True)
    try:
        ws.connect(url, socket=raw, timeout=2, redirect_limit=0)
        if ws.getstatus() != 101:
            raise AdapterError("STREAM_HANDSHAKE_FAILED")
        ws.settimeout(0.5)
        return ws
    except Exception:
        raw.close()
        raise AdapterError("STREAM_CONNECT_FAILED") from None


def parse_records(raw):
    if not isinstance(raw, str) or len(raw) > 1024 * 1024:
        raise AdapterError("STREAM_FRAME_INVALID")
    parts = raw.split("|")
    if len(parts) != 4 or parts[0] != '0' or parts[1] not in {'H0STCNT0','H0STASP0'} or not re.fullmatch(r"[0-9]{3}", parts[2]):
        raise AdapterError("STREAM_FRAME_INVALID")
    columns = COLUMNS if parts[1] == 'H0STCNT0' else BOOK_COLUMNS
    count, values = int(parts[2]), parts[3].split("^")
    if count < 1 or len(values) != count * len(columns):
        raise AdapterError("STREAM_FIELD_COUNT_INVALID")
    return tuple(dict(zip(columns, values[index:index + len(columns)])) for index in range(0, len(values), len(columns)))


def provider_time(record, now):
    # A delayed halt/non-regular packet must never authorize REST fallback.
    if record["MARKET_CLS_CODE"] != "2" or record["HOUR_CLS_CODE"] != "0" or record["TRHT_YN"] != "N":
        raise AdapterError("STREAM_SESSION_INVALID")
    day, hour = record["BSOP_DATE"], record["STCK_CNTG_HOUR"]
    if not re.fullmatch(r"[0-9]{8}", day) or not re.fullmatch(r"[0-9]{6}", hour):
        raise AdapterError("STREAM_TIMESTAMP_INVALID")
    try:
        stamp = datetime.strptime(day + hour, "%Y%m%d%H%M%S").replace(tzinfo=SEOUL)
    except ValueError:
        raise AdapterError("STREAM_TIMESTAMP_INVALID") from None
    if stamp.date() != now.astimezone(SEOUL).date() or not 0 <= (now - stamp).total_seconds() <= 5:
        raise AdapterError("STREAM_QUOTE_STALE")
    return stamp


class KisQuoteStream:
    def __init__(self, *, environment, approval, permit, connector=None, clock=utcnow):
        self.url, self.approval, self.permit = URLS[environment], approval, permit
        self.connector, self.clock = connector or connect, clock
        self.lock = threading.Lock()
        self.stop, self.wake = threading.Event(), threading.Event()
        self.targets, self.active = set(), set()
        self.book_active, self.books = set(), {}
        self.cache, self.latest, self.errors = {}, {}, {}
        self.thread, self.ws = None, None
        self.connected, self.closed = False, False
        self.error = "STREAM_NOT_READY"

    def replace(self, tickers):
        with self.lock:
            if self.closed:
                raise AdapterError("STREAM_CLOSED")
            self.targets = set(tickers)
            for ticker in self.cache.keys() - self.targets:
                self.cache.pop(ticker, None)
            self.latest = {ticker: stamp for ticker, stamp in self.latest.items() if ticker in self.targets}
            self.errors = {ticker: error for ticker, error in self.errors.items() if ticker in self.targets}
            self.books = {ticker: value for ticker,value in self.books.items() if ticker in self.targets}
            if self.targets and self.thread is None:
                self.thread = threading.Thread(target=self._run, name="kis-quotes", daemon=True)
                self.thread.start()
        self.wake.set()

    def quote(self, ticker):
        with self.lock:
            if self.closed:
                raise AdapterError("STREAM_CLOSED")
            if ticker not in self.targets:
                raise AdapterError("STREAM_SYMBOL_NOT_SUBSCRIBED")
            if not self.connected:
                raise AdapterError(self.error)
            if ticker not in self.active:
                raise AdapterError(self.errors.get(ticker, "STREAM_NOT_READY"))
            now = self.clock()
            if (ticker in self.book_active and ticker in self.books and ticker in self.latest and
                    self.latest[ticker].date() == now.astimezone(SEOUL).date() and
                    self.errors.get(ticker) in {None,'STREAM_QUOTE_STALE'}):
                book, received = self.books[ticker]
                try:
                    stamp = datetime.strptime(self.latest[ticker].strftime('%Y%m%d')+book['BSOP_HOUR'],'%Y%m%d%H%M%S').replace(tzinfo=SEOUL)
                    if book['HOUR_CLS_CODE'] != '0':
                        raise AdapterError('STREAM_SESSION_INVALID')
                    if 0 <= (now-stamp).total_seconds() <= 5 and 0 <= (now-received).total_seconds() <= 5:
                        return FetchResult(({**book,'BSOP_DATE':stamp.strftime('%Y%m%d'),'MARKET_CLS_CODE':'2'},),
                                           'COMPLETE',received,metadata={'tr_id':'H0STASP0','source':'KIS:H0STASP0'})
                except ValueError:
                    raise AdapterError('STREAM_TIMESTAMP_INVALID') from None
            if ticker not in self.cache:
                raise AdapterError(self.errors.get(ticker, "STREAM_NOT_READY"))
            record, received = self.cache[ticker]
            try:
                provider_time(record, now)
                if not 0 <= (now - received).total_seconds() <= 5:
                    raise AdapterError("STREAM_QUOTE_STALE")
            except AdapterError as error:
                self.cache.pop(ticker, None)
                self.errors[ticker] = error.code
                raise
            except (TypeError, ValueError, AttributeError):
                self.cache.pop(ticker, None)
                raise AdapterError("STREAM_TIMESTAMP_INVALID") from None
            return FetchResult((dict(record),), "COMPLETE", received,
                               metadata={"venue": "KRX", "transport": "websocket", "tr_id": "H0STCNT0"})

    def _reset(self, error):
        with self.lock:
            self.connected = False
            self.active.clear()
            self.book_active.clear()
            self.books.clear()
            self.cache.clear()
            self.latest.clear()
            self.errors.clear()
            self.error = error

    def session_date(self, ticker):
        """A REST book may borrow only a verified date from this live session."""
        with self.lock:
            stamp = self.latest.get(ticker)
            if (self.closed or not self.connected or ticker not in self.active or ticker not in self.targets
                    or self.errors.get(ticker) != "STREAM_QUOTE_STALE" or stamp is None
                    or stamp.date() != self.clock().astimezone(SEOUL).date()):
                raise AdapterError("STREAM_SESSION_UNVERIFIED")
            return stamp.date().isoformat()

    def _receive(self, ws, pending=None):
        import websocket

        self.permit()
        try:
            raw = ws.recv()
        except (TimeoutError, websocket.WebSocketTimeoutException):
            return False
        self.permit()
        if not isinstance(raw, str) or not raw or len(raw) > 1024 * 1024:
            raise AdapterError("STREAM_FRAME_INVALID")
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                header = data["header"]
                if header["tr_id"] == "PINGPONG":
                    ws.pong(raw)
                    return False
                body = data["body"]
                if body["rt_cd"] != "0":
                    raise AdapterError("STREAM_SUBSCRIPTION_REJECTED")
                expected = "SUBSCRIBE SUCCESS" if pending and pending[0] == "1" else "UNSUBSCRIBE SUCCESS"
                if pending is None or header["tr_id"] != pending[2] or header["tr_key"] != pending[1] or body["msg1"] != expected:
                    raise AdapterError("STREAM_ACK_INVALID")
                with self.lock:
                    active = self.active if pending[2] == 'H0STCNT0' else self.book_active
                    if pending[0] == "1":
                        active.add(pending[1])
                    else:
                        active.discard(pending[1])
                        if pending[2] == 'H0STCNT0':
                            self.cache.pop(pending[1], None)
                            self.latest.pop(pending[1], None)
                        else:
                            self.books.pop(pending[1],None)
                return True
            except (ValueError, TypeError, KeyError):
                raise AdapterError("STREAM_ACK_INVALID") from None
        records, received = parse_records(raw), self.clock()
        tr_id = raw.split('|',2)[1]
        with self.lock:
            for record in records:
                ticker = record["MKSC_SHRN_ISCD"]
                # Removed subscriptions can still deliver until their unsubscribe ACK.
                active = self.active if tr_id == 'H0STCNT0' else self.book_active
                if ticker not in active:
                    if pending == ("1", ticker, tr_id):
                        continue
                    raise AdapterError("STREAM_SYMBOL_UNEXPECTED")
                if ticker not in self.targets:
                    continue
                if tr_id == 'H0STASP0':
                    if not re.fullmatch(r'\d{6}',record['BSOP_HOUR']):
                        raise AdapterError('STREAM_TIMESTAMP_INVALID')
                    previous = self.books.get(ticker)
                    if previous and record['BSOP_HOUR'] < previous[0]['BSOP_HOUR']:
                        continue
                    self.books[ticker] = (record,received)
                    continue
                try:
                    stamp = provider_time(record, received)
                    if ticker in self.latest and stamp < self.latest[ticker]:
                        raise AdapterError("STREAM_QUOTE_OUT_OF_ORDER")
                except AdapterError as error:
                    self.cache.pop(ticker, None)
                    self.errors[ticker] = error.code
                    continue
                self.latest[ticker] = stamp
                self.cache[ticker] = (record, received)
                self.errors.pop(ticker, None)
        return False

    def _change(self, ws, key, operation, ticker, tr_id='H0STCNT0'):
        self.permit()
        ws.send(json.dumps({"header": {"approval_key": key, "custtype": "P", "tr_type": operation, "content-type": "utf-8"},
                            "body": {"input": {"tr_id": tr_id, "tr_key": ticker}}}))
        deadline = time.monotonic() + 5
        while not self.stop.is_set() and time.monotonic() < deadline:
            if self._receive(ws, (operation, ticker, tr_id)):
                self.stop.wait(0.1)
                return
        raise AdapterError("STREAM_ACK_TIMEOUT")

    def _run(self):
        delay = 1
        while not self.stop.is_set():
            with self.lock:
                targets = set(self.targets)
            if not targets:
                self.wake.wait(0.5)
                self.wake.clear()
                continue
            ws = None
            try:
                self.permit()
                key = self.approval()
                if self.stop.is_set():
                    break
                self.permit()
                ws = self.connector(self.url)
                with self.lock:
                    self.ws, self.connected = ws, True
                while not self.stop.is_set():
                    with self.lock:
                        removed = self.active - self.targets
                        added = self.targets - self.active
                        # Preserve all 41 trade registrations; remaining slots carry
                        # independent books. Uncovered books retain the REST fallback.
                        book_targets = set(sorted(self.targets)[:max(0,41-len(self.targets))])
                        removed_books = self.book_active-book_targets
                        added_books = book_targets-self.book_active
                    if removed_books:
                        self._change(ws,key,'2',sorted(removed_books)[0],'H0STASP0')
                    elif removed:
                        self._change(ws, key, "2", sorted(removed)[0])
                    elif added:
                        self._change(ws, key, "1", sorted(added)[0])
                    elif added_books:
                        self._change(ws,key,'1',sorted(added_books)[0],'H0STASP0')
                    else:
                        delay = 1
                        self._receive(ws)
            except Exception as error:
                safe_codes = {"STREAM_FRAME_INVALID", "STREAM_FIELD_COUNT_INVALID", "STREAM_ACK_TIMEOUT", "STREAM_ACK_INVALID",
                              "STREAM_SUBSCRIPTION_REJECTED", "STREAM_SYMBOL_UNEXPECTED", "STREAM_AUTH_FAILED", "STREAM_CONNECT_FAILED"}
                code = error.code if isinstance(error, AdapterError) and error.code in safe_codes else "STREAM_CONNECTION_FAILED"
                self._reset(code)
            finally:
                self._reset("STREAM_CLOSED" if self.stop.is_set() else self.error)
                if ws is not None:
                    try:
                        ws.shutdown()
                    except Exception:
                        pass
                with self.lock:
                    self.ws = None
            if self.stop.wait(delay):
                break
            delay = min(delay * 2, 30)

    def close(self):
        with self.lock:
            self.closed = True
            ws, thread = self.ws, self.thread
        self.stop.set()
        self.wake.set()
        self._reset("STREAM_CLOSED")
        if ws is not None:
            try:
                ws.shutdown()
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise AdapterError("STREAM_CLOSE_TIMEOUT")
