"""Synthetic protocol checks only: no credentials, provider session, or live quote proof."""

import json
import queue
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from danta.adapters import AdapterError, HttpResponse
from danta.adapters.kis import KisAdapter, KisCredentials
from danta.adapters.kis_stream import COLUMNS, connect, parse_records


NOW = datetime(2026, 9, 18, 1, 0, 0, tzinfo=timezone.utc)


def record(ticker="005930", **changes):
    values = {field: "0" for field in COLUMNS}
    values.update(MKSC_SHRN_ISCD=ticker, STCK_CNTG_HOUR="100000", STCK_PRPR="70000", ASKP1="70100", BIDP1="70000",
                  ASKP_RSQN1="100", BIDP_RSQN1="200", BSOP_DATE="20260918", NEW_MKOP_CLS_CODE="20",
                  TRHT_YN="N", HOUR_CLS_CODE="0", MARKET_CLS_CODE="2")
    values.update(changes)
    return values


def frame(*records):
    return "0|H0STCNT0|" + f"{len(records):03}" + "|" + "^".join(row[field] for row in records for field in COLUMNS)


def ack(operation, ticker, success=True):
    return json.dumps({"header": {"tr_id": "H0STCNT0", "tr_key": ticker},
                       "body": {"rt_cd": "0" if success else "1",
                                "msg1": "SUBSCRIBE SUCCESS" if operation == "1" else "UNSUBSCRIBE SUCCESS"}})


class Socket:
    def __init__(self):
        self.incoming = queue.Queue()
        self.sent, self.pongs = [], []
        self.closed = False
        self.auto_unsubscribe = True

    def send(self, raw):
        data = json.loads(raw)
        self.sent.append(data)
        operation, ticker = data["header"]["tr_type"], data["body"]["input"]["tr_key"]
        if operation == "1" or self.auto_unsubscribe:
            self.incoming.put(ack(operation, ticker))

    def recv(self):
        try:
            value = self.incoming.get(timeout=0.02)
        except queue.Empty:
            raise TimeoutError from None
        if isinstance(value, Exception):
            raise value
        return value

    def pong(self, raw):
        self.pongs.append(raw)

    def shutdown(self):
        self.closed = True
        self.incoming.put(ConnectionError("synthetic-private-text"))


class Connector:
    fixture_only = True

    def __init__(self):
        self.sockets = []

    def __call__(self, url):
        self.sockets.append(Socket())
        return self.sockets[-1]


class Transport:
    fixture_only = True

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, json.loads(body), timeout))
        return HttpResponse(200, json.dumps({"approval_key": "synthetic-approval"}).encode())


class KisStreamTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.connector, self.transport = Connector(), Transport()
        self.adapter = KisAdapter(environment="demo", credentials=KisCredentials("00000000", "01", "fixture", "fixture", ""),
                                  transport=self.transport, ws_connector=self.connector, clock=lambda: self.now)
        self.addCleanup(self.adapter.close)

    def until(self, predicate, seconds=2):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Synthetic worker did not reach expected state")

    def start(self, targets=("005930",)):
        self.adapter.subscribe_quotes(targets)
        self.until(lambda: self.adapter._stream.active == set(targets))
        return self.connector.sockets[-1]

    def quote_ready(self, ticker="005930"):
        try:
            return bool(self.adapter.stream_quote(ticker).records)
        except AdapterError:
            return False

    def test_offline_and_constructor_do_not_authenticate(self):
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.connector.sockets, [])
        with self.assertRaisesRegex(AdapterError, "STREAM_NOT_READY"):
            self.adapter.stream_quote("005930")
        adapter = KisAdapter(environment="demo", credentials=self.adapter.credentials, transport=self.transport)
        with self.assertRaisesRegex(AdapterError, "OFFLINE_NETWORK_BLOCKED"):
            adapter.subscribe_quotes(["005930"])
        self.assertEqual(self.transport.calls, [])
        self.start()
        method, url, body, timeout = self.transport.calls[0]
        self.assertEqual((method, url, timeout), ("POST", "https://openapivts.koreainvestment.com:29443/oauth2/Approval", 3))
        self.assertEqual(set(body), {"grant_type", "appkey", "secretkey"})

    def test_multirecord_field_mapping_received_time_and_ping(self):
        ws = self.start(("005930", "000660"))
        payload = frame(record(), record("000660", ASKP1="200100", BIDP_RSQN1="77"))
        self.assertEqual(len(COLUMNS), 47)
        self.assertEqual(parse_records(payload)[1]["BIDP_RSQN1"], "77")
        ws.incoming.put(payload)
        self.until(lambda: self.quote_ready("000660"))
        self.now += timedelta(seconds=1)
        result = self.adapter.stream_quote("000660")
        self.assertEqual(result.retrieved_at, NOW)
        self.assertEqual(result.records[0]["ASKP1"], "200100")
        ping = json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "synthetic"}})
        ws.incoming.put(ping)
        self.until(lambda: ws.pongs == [ping])

    def test_old_field_count_bad_count_encryption_and_unknown_symbol_fail(self):
        for raw in (frame(record()).rsplit("^", 1)[0], frame(record()).replace("|001|", "|002|"),
                    frame(record()).replace("0|", "1|", 1)):
            with self.subTest(shape=raw[:15]), self.assertRaises(AdapterError):
                parse_records(raw)
        ws = self.start()
        ws.incoming.put(frame(record("000660")))
        self.until(lambda: self.adapter._stream.error == "STREAM_SYMBOL_UNEXPECTED")
        self.assertFalse(self.adapter._stream.cache)

    def test_reject_nonregular_missing_invalid_future_and_other_date(self):
        ws = self.start()
        for changes in ({"MARKET_CLS_CODE": "3"}, {"MARKET_CLS_CODE": ""}, {"TRHT_YN": "Y"}, {"HOUR_CLS_CODE": "C"},
                        {"BSOP_DATE": ""}, {"STCK_CNTG_HOUR": "100099"}, {"STCK_CNTG_HOUR": "100001"}, {"BSOP_DATE": "20260917"}):
            with self.subTest(changes=changes):
                ws.incoming.put(frame(record()))
                self.until(self.quote_ready)
                ws.incoming.put(frame(record(**changes)))
                self.until(lambda: "005930" in self.adapter._stream.errors)
                with self.assertRaises(AdapterError):
                    self.adapter.stream_quote("005930")

    def test_out_of_order_and_cache_expiry(self):
        ws = self.start()
        ws.incoming.put(frame(record()))
        self.until(self.quote_ready)
        self.now += timedelta(seconds=1)
        ws.incoming.put(frame(record(STCK_CNTG_HOUR="095959")))
        self.until(lambda: self.adapter._stream.errors.get("005930") == "STREAM_QUOTE_OUT_OF_ORDER")
        self.assertFalse(self.quote_ready())
        ws.incoming.put(frame(record(STCK_CNTG_HOUR="100001")))
        self.until(self.quote_ready)
        self.now += timedelta(seconds=6)
        with self.assertRaisesRegex(AdapterError, "STREAM_QUOTE_STALE"):
            self.adapter.stream_quote("005930")

    def test_delta_keeps_other_cache_and_waits_for_unsubscribe_ack(self):
        ws = self.start(("005930", "000660"))
        ws.incoming.put(frame(record(), record("000660")))
        self.until(self.quote_ready)
        ws.auto_unsubscribe = False
        self.adapter.subscribe_quotes(["005930", "035420"])
        self.until(lambda: any(data["header"]["tr_type"] == "2" for data in ws.sent))
        self.assertTrue(self.quote_ready())
        self.assertEqual(len(self.connector.sockets), 1)
        self.assertFalse(any(data["body"]["input"]["tr_key"] == "035420" for data in ws.sent))
        with self.assertRaisesRegex(AdapterError, "STREAM_SYMBOL_NOT_SUBSCRIBED"):
            self.adapter.stream_quote("000660")
        ws.incoming.put(ack("2", "000660"))
        self.until(lambda: self.adapter._stream.active == {"005930", "035420"})
        self.assertTrue(self.quote_ready())
        self.assertFalse(self.quote_ready("035420"))

    def test_disconnect_and_reconnect_require_new_packet_and_ack_error_clears(self):
        ws = self.start()
        ws.incoming.put(frame(record()))
        self.until(self.quote_ready)
        ws.incoming.put(ConnectionError("synthetic-private-text"))
        self.until(lambda: ws.closed)
        with self.assertRaises(AdapterError) as error:
            self.adapter.stream_quote("005930")
        self.assertNotIn("synthetic", str(error.exception))
        self.until(lambda: len(self.connector.sockets) == 2 and self.adapter._stream.active == {"005930"})
        self.assertFalse(self.quote_ready())
        ws2 = self.connector.sockets[-1]
        ws2.incoming.put(frame(record()))
        self.until(self.quote_ready)
        ws2.incoming.put(ack("1", "005930", success=False))
        self.until(lambda: ws2.closed)
        self.assertFalse(self.quote_ready())

    def test_scope_limit_authorization_and_interruptible_close(self):
        with self.assertRaisesRegex(AdapterError, "STREAM_SUBSCRIPTION_LIMIT"):
            self.adapter.subscribe_quotes([f"{i:06}" for i in range(42)])
        self.assertEqual(self.transport.calls, [])
        permissions = []
        self.adapter.mode = "shadow"
        self.adapter.authorize = lambda operation, environment: permissions.append(operation)
        ws = self.start()
        self.assertIn("broker_auth", permissions)
        self.assertIn("market_read", permissions)
        self.assertNotIn("broker_write", permissions)
        self.adapter.close()
        self.assertTrue(ws.closed)
        self.assertFalse(self.adapter._stream.thread.is_alive())
        with self.assertRaisesRegex(AdapterError, "STREAM_CLOSED"):
            self.adapter.subscribe_quotes(["005930"])

    def test_authorization_revocation_stops_session_and_close_before_start_is_terminal(self):
        self.adapter.mode = "shadow"
        self.adapter.authorize = lambda operation, environment: None
        ws = self.start()
        def revoked(operation, environment):
            raise AdapterError("STREAM_synthetic-private-text")
        self.adapter.authorize = revoked
        self.until(lambda: ws.closed)
        self.assertEqual(self.adapter._stream.error, "STREAM_CONNECTION_FAILED")
        unused = KisAdapter(environment="demo", credentials=self.adapter.credentials, transport=self.transport, ws_connector=self.connector)
        unused.close()
        with self.assertRaisesRegex(AdapterError, "STREAM_CLOSED"):
            unused.subscribe_quotes(["005930"])

    def test_auth_permission_is_required_before_auth_transport_or_socket(self):
        self.adapter.mode = "shadow"
        def authorize(operation, environment):
            if operation == "broker_auth":
                raise AdapterError("AUTHORIZATION_REQUIRED")
        self.adapter.authorize = authorize
        self.adapter.subscribe_quotes(["005930"])
        self.until(lambda: self.adapter._stream.error == "STREAM_CONNECTION_FAILED")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.connector.sockets, [])

    def test_direct_socket_and_redirect_limit_without_network(self):
        class Raw:
            def close(self):
                pass
        with patch("danta.adapters.kis_stream.socket.create_connection", return_value=Raw()) as direct, patch("websocket.WebSocket") as ws:
            ws.return_value.getstatus.return_value = 101
            result = connect("ws://ops.koreainvestment.com:31000")
            direct.assert_called_once_with(("ops.koreainvestment.com", 31000), timeout=2)
            self.assertIs(result, ws.return_value)
            self.assertEqual(ws.return_value.connect.call_args.kwargs["redirect_limit"], 0)
            self.assertIsInstance(ws.return_value.connect.call_args.kwargs["socket"], Raw)
            ws.return_value.getstatus.return_value = 302
            with self.assertRaisesRegex(AdapterError, "STREAM_CONNECT_FAILED"):
                connect("ws://ops.koreainvestment.com:31000")


if __name__ == "__main__":
    unittest.main()
