from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MODULE_PATH = Path(__file__).resolve().parents[1] / "telegram_gateway.py"
SPEC = importlib.util.spec_from_file_location("telegram_gateway_under_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load {MODULE_PATH}")
telegram_gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(telegram_gateway)


class BalancedHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(tag)
            return
        self.stack.pop()


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def assert_balanced(test: unittest.TestCase, value: str) -> None:
    parser = BalancedHtmlParser()
    parser.feed(value)
    parser.close()
    test.assertEqual(parser.errors, [])
    test.assertEqual(parser.stack, [])


def visible_text(value: str) -> str:
    parser = VisibleTextParser()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


class TelegramGatewayHtmlSplitTest(unittest.TestCase):
    def test_config_version_prefers_image_file_then_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            version_file = Path(tmp) / 'VERSION'
            with patch.object(telegram_gateway, 'VERSION_FILE', version_file), patch.dict(os.environ, APP_VERSION=' stale-env '):
                version_file.write_text('v20260923-002\n')
                self.assertEqual(telegram_gateway.Config.from_env().version, 'v20260923-002')
                for content in (b'', b'\xff', None):
                    if content is None:
                        version_file.unlink()
                    else:
                        version_file.write_bytes(content)
                    self.assertEqual(telegram_gateway.Config.from_env().version, 'stale-env')
                with patch.dict(os.environ, APP_VERSION=' '):
                    self.assertEqual(telegram_gateway.Config.from_env().version, '1.0.0')

    def test_split_reopens_tags_without_breaking_closing_tag(self) -> None:
        text = telegram_gateway.sanitize_telegram_html(f"<code>{'한' * 70}</code>")

        chunks = telegram_gateway.split_telegram_html(text, limit=40)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 40 for chunk in chunks))
        for chunk in chunks:
            assert_balanced(self, chunk)
        self.assertEqual("".join(visible_text(chunk) for chunk in chunks), "한" * 70)

    def test_split_preserves_nested_tags_links_and_entities(self) -> None:
        text = telegram_gateway.sanitize_telegram_html(
            '<b>prefix <a href="https://example.test/?a=1&amp;b=2">link &amp; text</a> suffix</b>'
        )

        chunks = telegram_gateway.split_telegram_html(text, limit=60)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 60 for chunk in chunks))
        for chunk in chunks:
            assert_balanced(self, chunk)
        self.assertEqual("".join(visible_text(chunk) for chunk in chunks), visible_text(text))

    def test_explicit_break_is_applied_before_sanitizing(self) -> None:
        text = "<b>first</b><!--telegram-message-break--><i>second</i>"

        chunks = telegram_gateway.telegram_html_chunks(text)

        self.assertEqual(chunks, ["<b>first</b>", "<i>second</i>"])

    def test_july_14_closing_tag_boundary_shape_is_safe(self) -> None:
        text = telegram_gateway.sanitize_telegram_html("A" * 4085 + "<code>001450</code> tail")

        chunks = telegram_gateway.split_telegram_html(text)

        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))
        for chunk in chunks:
            assert_balanced(self, chunk)


class GatewayMenuContractTest(unittest.TestCase):
    def test_example_menu_matches_receiver_commands_without_aliases(self) -> None:
        receiver_source = MODULE_PATH.parents[1] / "trading-engine" / "src"
        with patch.object(sys, "path", [str(receiver_source), *sys.path]):
            from danta.adapters.telegram import COMMANDS

        example = MODULE_PATH.parent / "config" / "routes.example.yaml"
        routes = telegram_gateway.yaml.safe_load(example.read_text())["routes"]
        self.assertEqual(set(routes), {"trading-engine"})
        route = routes["trading-engine"]
        self.assertEqual(route["url"], "http://trading-engine:8080/telegram")
        self.assertEqual(route["env_file"], "/app/config/telegram.env")
        commands = telegram_gateway.route_bot_commands(route, "trading-engine")
        self.assertEqual([item.command for item in commands], ['status', 'report', 'usage', 'new', 'stop'])
        self.assertTrue({item.command for item in commands} <= COMMANDS)
        self.assertTrue({'review', 'pause', 'resume', 'version', 'session'} <= COMMANDS)
        for item in commands:
            self.assertEqual(item.instruction, f"/{item.command}")
            text = f"/{item.command} argument"
            self.assertEqual(
                telegram_gateway.apply_bot_command_alias(SimpleNamespace(bot_commands=commands), text),
                text,
            )


class GatewayEngineClientTest(unittest.TestCase):
    def test_forwards_payload_without_shared_secret_and_restricts_target(self) -> None:
        client = telegram_gateway.TradingEngineClient(2)
        client._opener = Mock()
        client._opener.open.return_value = io.BytesIO(b'{"accepted":true}')
        payload = {"text": "/status 한글", "chat_id": "synthetic"}
        self.assertEqual(client.post_message("http://receiver/telegram", payload), {"accepted": True})
        request = client._opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(dict(request.header_items()), {"Content-type": "application/json"})
        for url in ("file:///telegram", "http://user:password@receiver/telegram", "http://receiver/other",
                    "http://receiver/telegram?key=value", "http://receiver/telegram#fragment"):
            for operation in (lambda: client.post_message(url, payload), lambda: client.get_version(url),
                              lambda: client.get_readiness(url)):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    operation()
        self.assertEqual(client._opener.open.call_count, 1)

    def test_maximum_korean_attachment_fits_engine_wire_limit(self) -> None:
        client = telegram_gateway.TradingEngineClient(2)
        client._opener = Mock()
        client._opener.open.return_value = io.BytesIO(b'{"accepted":true}')
        payload = {'text': '첨부를 요약해줘', 'chat_id': 'synthetic',
                   'attachments': [{'file_name': '메모.txt', 'content': '가' * 10922}]}
        client.post_message('http://receiver/telegram', payload)
        body = client._opener.open.call_args.args[0].data
        self.assertLessEqual(len(body), 65536)
        self.assertEqual(json.loads(body), payload)

    def test_version_uses_management_get_when_runtime_is_not_ready(self) -> None:
        client = telegram_gateway.TradingEngineClient(2)
        client._opener = Mock()
        client._opener.open.return_value = io.BytesIO(b'{"version":"v-test","runtime_status":"NOT_READY"}')
        result = client.get_version("http://receiver:8080/telegram")
        self.assertEqual(result["version"], "v-test")
        request = client._opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "http://receiver:8080/version")
        self.assertIsNone(request.data)

    def test_connection_failures_preserve_safe_cause_and_timing_without_retry(self) -> None:
        for cause, expected in ((ConnectionRefusedError(111, 'private-value'), 'CONNECTION_REFUSED'),
                                (socket.gaierror(-2, 'private-host'), 'DNS_FAILURE'),
                                (TimeoutError('private-url'), 'TIMEOUT')):
            client = telegram_gateway.TradingEngineClient(2)
            client._opener = Mock()
            client._opener.open.side_effect = URLError(cause)
            with self.assertLogs(level='WARNING') as logs, self.assertRaises(telegram_gateway.EngineRequestError) as raised:
                client.post_message('http://receiver/telegram', {})
            detail = raised.exception.diagnostic
            self.assertEqual((detail['reason'], detail['method'], detail['endpoint']), (expected, 'POST', '/telegram'))
            self.assertIn('occurred_at', detail)
            self.assertGreaterEqual(detail['elapsed_seconds'], 0)
            self.assertEqual(client._opener.open.call_count, 1)
            rendered = telegram_gateway.engine_failure_text(raised.exception)
            self.assertIn(expected, rendered)
            self.assertNotIn('private', rendered + str(detail) + str(logs.output))

    def test_invalid_success_response_is_not_echoed_as_private_text(self) -> None:
        client = telegram_gateway.TradingEngineClient(2)
        client._opener = Mock()
        for raw in (b'private-provider-body', b'"private-provider-body"', b''):
            client._opener.open.return_value = io.BytesIO(raw)
            with self.assertRaises(telegram_gateway.EngineRequestError) as raised:
                client.post_message('http://receiver/telegram', {})
            self.assertEqual(raised.exception.diagnostic['reason'], 'INVALID_RESPONSE')
            self.assertNotIn('private', str(raised.exception.diagnostic))

    def test_failures_do_not_expose_response_body_or_connection_details(self) -> None:
        client = telegram_gateway.TradingEngineClient(2)
        client._opener = Mock()
        for error in (HTTPError("http://receiver/telegram", 503, "unavailable", {}, io.BytesIO(b"private-body")),
                      URLError("private-connection-details")):
            client._opener.open.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(RuntimeError) as raised:
                client.post_message("http://receiver/telegram", {})
            self.assertNotIn("private", str(raised.exception))
        self.assertEqual(client._opener.open.call_count, 2)

    def test_readiness_accepts_structured_http_503_only(self) -> None:
        client = telegram_gateway.TradingEngineClient(2)
        client._opener = Mock()
        readiness = {"ready": False, "status": "WAITING_FOR_CONFIGURATION", "issues": ["config/runtime.json: 운영 승인 파일 없음"]}
        for body, valid in ((json.dumps(readiness).encode(), True), (b"private-body", False),
                            (b'{"ready":false,"status":"private-status","issues":[]}', False),
                            (b'{"ready":false,"status":"FAILED","issues":"private-body"}', False)):
            client._opener.open.side_effect = HTTPError("http://receiver/readyz", 503, "unavailable", {}, io.BytesIO(body))
            with self.subTest(valid=valid, body_type=type(body).__name__):
                if valid:
                    self.assertEqual(client.get_readiness("http://receiver/telegram"), readiness)
                else:
                    with self.assertRaisesRegex(RuntimeError, "Invalid engine readiness response"):
                        client.get_readiness("http://receiver/telegram")
        request = client._opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "http://receiver/readyz")
        self.assertIsNone(request.data)
        client._opener.open.side_effect = HTTPError("http://receiver/readyz", 500, "private-error", {}, io.BytesIO(b"private-body"))
        with self.assertRaisesRegex(RuntimeError, "^trading-engine route failed: HTTP 500$"):
            client.get_readiness("http://receiver/telegram")

    def test_rejected_response_is_failure_even_with_http_success(self) -> None:
        client = telegram_gateway.TradingEngineClient(2)
        client._opener = Mock()
        client._opener.open.return_value = io.BytesIO(b'{"accepted":false,"reply_text":"private-body"}')
        with self.assertRaisesRegex(RuntimeError, "Engine request rejected"):
            client.post_message("http://receiver/telegram", {})
        client._opener.open.assert_called_once()

    def test_structured_http_rejection_preserves_only_user_reply(self) -> None:
        client = telegram_gateway.TradingEngineClient(2)
        client._opener = Mock()
        reply = 'Codex 실행 준비가 끝나지 않았습니다. /status에서 확인해 주세요.'
        body = json.dumps({'accepted': False, 'reply_text': reply, 'internal': 'private details'}).encode()
        client._opener.open.side_effect = HTTPError('http://receiver/telegram', 503, 'unavailable', {}, io.BytesIO(body))
        with self.assertRaises(telegram_gateway.EngineRequestRejected) as raised:
            client.post_message('http://receiver/telegram', {})
        self.assertEqual(raised.exception.reply_text, reply)
        self.assertNotIn('private', str(raised.exception))

    def test_preserves_structured_text_reply(self) -> None:
        client = telegram_gateway.TradingEngineClient(2)
        client._opener = Mock()
        client._opener.open.return_value = io.BytesIO(b'{"reply_text":"plain text reply"}')
        self.assertEqual(client.post_message("http://receiver/telegram", {}), {"reply_text": "plain text reply"})

    def test_http_does_not_follow_redirect_or_environment_proxy(self) -> None:
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.end_headers()

            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1"}, clear=True):
                client = telegram_gateway.TradingEngineClient(2)
                with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                    client.post_message(f"http://127.0.0.1:{server.server_port}/telegram", {})
            self.assertEqual(received, ["/telegram"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


class TelegramClientTest(unittest.TestCase):
    def test_get_updates_requests_messages_and_callback_queries(self) -> None:
        route = SimpleNamespace(
            telegram_bot_token="test-token",
            poll_timeout=25,
            http_timeout=10,
        )
        client = telegram_gateway.TelegramClient(route)
        client.post_form = Mock(return_value={"result": []})

        client.get_updates(12)

        payload = client.post_form.call_args.args[1]
        self.assertEqual(
            json.loads(payload["allowed_updates"]),
            ["message", "callback_query"],
        )
        self.assertEqual(payload["offset"], 12)

    def test_send_message_serializes_copy_text_reply_markup(self) -> None:
        route = SimpleNamespace(
            telegram_bot_token="test-token",
            parse_mode=None,
            http_timeout=10,
        )
        client = telegram_gateway.TelegramClient(route)
        client.post_form = Mock()
        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "/resume 019f6681",
                        "copy_text": {"text": "/resume 019f6681"},
                    }
                ]
            ]
        }

        client.send_message("1", "sessions", reply_markup=reply_markup)

        payload = client.post_form.call_args.args[1]
        self.assertEqual(json.loads(payload["reply_markup"]), reply_markup)

    def test_send_message_draft_posts_plain_text_with_draft_id(self) -> None:
        route = SimpleNamespace(
            telegram_bot_token="test-token",
            parse_mode=None,
            http_timeout=10,
        )
        client = telegram_gateway.TelegramClient(route)
        client.post_form = Mock()

        client.send_message_draft("1", 123, "진행 문구 *원문*")

        client.post_form.assert_called_once_with(
            "sendMessageDraft",
            {
                "chat_id": "1",
                "draft_id": 123,
                "text": "진행 문구 *원문*",
            },
        )

    def test_answer_callback_query_posts_callback_id(self) -> None:
        route = SimpleNamespace(
            telegram_bot_token="test-token",
            parse_mode=None,
            http_timeout=10,
        )
        client = telegram_gateway.TelegramClient(route)
        client.post_form = Mock()

        client.answer_callback_query("callback-1")

        client.post_form.assert_called_once_with(
            "answerCallbackQuery",
            {"callback_query_id": "callback-1"},
        )

    def test_download_file_uses_get_file_and_enforces_limit(self) -> None:
        route = SimpleNamespace(
            telegram_bot_token="test-token",
            http_timeout=10,
        )
        client = telegram_gateway.TelegramClient(route)
        client.post_form = Mock(return_value={"result": {"file_path": "documents/report 1.pdf"}})
        response = Mock()
        response.headers = {"Content-Length": "3"}
        response.read.return_value = b"pdf"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with patch.object(telegram_gateway, "urlopen", return_value=response) as urlopen:
            content = client.download_file("file-1", 10)

        self.assertEqual(content, b"pdf")
        client.post_form.assert_called_once_with("getFile", {"file_id": "file-1"})
        self.assertIn("documents/report%201.pdf", urlopen.call_args.args[0].full_url)

    def test_download_file_rejects_oversized_content_length(self) -> None:
        route = SimpleNamespace(
            telegram_bot_token="test-token",
            http_timeout=10,
        )
        client = telegram_gateway.TelegramClient(route)
        client.post_form = Mock(return_value={"result": {"file_path": "documents/large.bin"}})
        response = Mock()
        response.headers = {"Content-Length": "11"}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with patch.object(telegram_gateway, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "20MiB"):
                client.download_file("file-1", 10)


class TelegramDraftEndpointTest(unittest.TestCase):
    def test_forwards_valid_draft_request_to_selected_route(self) -> None:
        app = telegram_gateway.GatewayApp.__new__(telegram_gateway.GatewayApp)
        app.config = SimpleNamespace(
            version="test",
            gateway_host="127.0.0.1",
            gateway_port=0,
        )
        route = SimpleNamespace(
            default_chat_id=None,
            telegram_bot_token="test-token",
            http_timeout=10,
        )
        app.resolve_send_route = Mock(return_value=route)
        client = Mock()

        with patch.object(telegram_gateway, "TelegramClient", return_value=client):
            server = app.serve_http()
            try:
                payload = json.dumps(
                    {
                        "route": "bridge-server",
                        "chat_id": "1",
                        "draft_id": 123,
                        "text": "진행 중",
                    }
                ).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/sendMessageDraft",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=2) as response:
                    result = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()

        self.assertEqual(result, {"ok": True})
        app.resolve_send_route.assert_called_once_with(
            {
                "route": "bridge-server",
                "chat_id": "1",
                "draft_id": 123,
                "text": "진행 중",
            }
        )
        client.send_message_draft.assert_called_once_with("1", 123, "진행 중")

    def test_rejects_non_integer_draft_id_and_non_string_text(self) -> None:
        app = telegram_gateway.GatewayApp.__new__(telegram_gateway.GatewayApp)
        app.config = SimpleNamespace(
            version="test",
            gateway_host="127.0.0.1",
            gateway_port=0,
        )
        route = SimpleNamespace(
            default_chat_id=None,
            telegram_bot_token="test-token",
            http_timeout=10,
        )
        app.resolve_send_route = Mock(return_value=route)
        client = Mock()

        with patch.object(telegram_gateway, "TelegramClient", return_value=client):
            server = app.serve_http()
            try:
                for invalid_fields in (
                    {"draft_id": 1.5, "text": "진행 중"},
                    {"draft_id": True, "text": "진행 중"},
                    {"draft_id": 0, "text": "진행 중"},
                    {"draft_id": 2 ** 31, "text": "진행 중"},
                    {"draft_id": -(2 ** 31) - 1, "text": "진행 중"},
                    {"draft_id": 123, "text": None},
                    {"draft_id": 123, "text": ""},
                    {"draft_id": 123, "text": "a" * 4097},
                ):
                    payload = json.dumps(
                        {
                            "route": "bridge-server",
                            "chat_id": "1",
                            **invalid_fields,
                        }
                    ).encode("utf-8")
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}/sendMessageDraft",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(request, timeout=2)
                    self.assertEqual(caught.exception.code, 400)
                    caught.exception.close()
            finally:
                server.shutdown()
                server.server_close()

        client.send_message_draft.assert_not_called()


class TelegramAttachmentCacheTest(unittest.TestCase):
    @staticmethod
    def attachment(
        *,
        file_name: str = "report.pdf",
        file_size: int | None = 3,
    ) -> object:
        return telegram_gateway.IncomingTelegramAttachment(
            kind="document",
            file_id="file-1",
            file_unique_id="unique-1",
            file_name=file_name,
            mime_type="application/pdf",
            file_size=file_size,
            caption="분석 대상",
            message_id=10,
        )

    def make_cache(self, root: Path, *, ttl_seconds: int = 60):
        return telegram_gateway.TelegramAttachmentCache(
            root / "container",
            root / "host",
            ttl_seconds=ttl_seconds,
            max_file_bytes=10,
            max_total_bytes=30,
            max_pending=2,
        )

    def test_store_survives_restart_and_consumes_only_selected_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = self.make_cache(root)
            first = cache.store("trading-engine", "chat-1", self.attachment(), b"pdf", now=100, user_id="9")
            cache.store("trading-engine", "chat-2", self.attachment(file_name="other.pdf"), b"two", now=101, user_id="9")

            reloaded = self.make_cache(root)
            pending = reloaded.list_pending("trading-engine", "chat-1", now=110, user_id="9")

            self.assertEqual([item.file_name for item in pending], ["report.pdf"])
            self.assertEqual(pending[0].host_path, root / "host" / "trading-engine" / "chat-1" / first.host_path.name)
            reloaded.mark_consumed(pending, now=111)
            self.assertEqual(reloaded.list_pending("trading-engine", "chat-1", now=112, user_id="9"), ())
            self.assertEqual(len(reloaded.list_pending("trading-engine", "chat-2", now=112, user_id="9")), 1)
            self.assertTrue(first.metadata_path.with_suffix(".pdf").exists())

    def test_text_payload_reads_container_files_and_preserves_pending_on_invalid_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = telegram_gateway.TelegramAttachmentCache(root / 'container', root / 'different-host',
                ttl_seconds=60, max_file_bytes=65536, max_total_bytes=262144, max_pending=10)
            item = cache.store('trading-engine', '1', self.attachment(file_name='notes.txt', file_size=None),
                               '한글 메모'.encode(), now=100, user_id="9")
            self.assertFalse(item.host_path.exists())
            self.assertEqual(cache.text_payload((item,)), [{'file_name': 'notes.txt', 'content': '한글 메모'}])
            self.assertEqual(json.loads(item.metadata_path.read_text())['status'], 'pending')
            with self.assertRaisesRegex(ValueError, '최대 5개'):
                cache.text_payload((item,) * 6)
            item.metadata_path.with_suffix('.txt').write_bytes(b'a' * 32769)
            with self.assertRaisesRegex(ValueError, '32KiB'):
                cache.text_payload((item,))
            item.metadata_path.with_suffix('.txt').write_bytes(b'\xff\x00')
            with self.assertRaisesRegex(ValueError, 'UTF-8'):
                cache.text_payload((item,))
            self.assertEqual(json.loads(item.metadata_path.read_text())['status'], 'pending')

    def test_text_payload_checks_aggregate_bytes_and_rejects_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = telegram_gateway.TelegramAttachmentCache(root / 'container', root / 'host',
                ttl_seconds=60, max_file_bytes=65536, max_total_bytes=262144, max_pending=10)
            items = [cache.store('trading-engine', '1', self.attachment(file_name=f'{i}.txt', file_size=None),
                                 b'a' * 10000, now=100, user_id="9") for i in range(2)]
            with self.assertRaisesRegex(ValueError, '32KiB'):
                cache.store('trading-engine', '1', self.attachment(file_name='too-large.txt', file_size=None),
                            b'a' * 20000, now=100, user_id="9")
            items[1].metadata_path.with_suffix('.txt').write_bytes(b'a' * 25000)
            with self.assertRaisesRegex(ValueError, '32KiB'):
                cache.text_payload(tuple(items))
            binary = cache.store('trading-engine', '1', self.attachment(), b'pdf', now=100, user_id="9")
            with self.assertRaisesRegex(ValueError, 'PDF'):
                cache.text_payload((binary,))
            self.assertEqual(len(cache.list_pending('trading-engine', '1', now=101, user_id="9")), 3)

    def test_sixth_pending_file_is_rejected_without_poisoning_first_five(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = telegram_gateway.TelegramAttachmentCache(root / 'container', root / 'host',
                ttl_seconds=60, max_file_bytes=65536, max_total_bytes=262144, max_pending=10)
            for i in range(5):
                cache.store('trading-engine', '1', self.attachment(file_name=f'{i}.txt'), b'txt', now=100, user_id="9")
            with self.assertRaisesRegex(ValueError, '/new'):
                cache.store('trading-engine', '1', self.attachment(file_name='sixth.txt'), b'txt', now=100, user_id="9")
            self.assertEqual(len(cache.text_payload(cache.list_pending('trading-engine', '1', now=101, user_id="9"))), 5)

    def test_cleanup_removes_expired_content_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = self.make_cache(Path(tmp), ttl_seconds=10)
            item = cache.store("trading-engine", "1", self.attachment(), b"pdf", now=100, user_id="9")
            data_path = item.metadata_path.with_suffix(".pdf")

            cache.cleanup_expired(now=111)

            self.assertFalse(item.metadata_path.exists())
            self.assertFalse(data_path.exists())

    def test_rejects_pending_and_total_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = self.make_cache(Path(tmp))
            cache.store("trading-engine", "1", self.attachment(file_name="a.pdf"), b"123", now=100, user_id="9")
            cache.store("trading-engine", "1", self.attachment(file_name="b.pdf"), b"456", now=101, user_id="9")

            with self.assertRaisesRegex(ValueError, "최대 2개"):
                cache.store("trading-engine", "1", self.attachment(file_name="c.pdf"), b"789", now=102, user_id="9")

    def test_sanitizes_untrusted_document_filename(self) -> None:
        message = {
            "message_id": 3,
            "document": {
                "file_id": "file-1",
                "file_name": "../../secret.txt",
                "file_size": 4,
            },
        }

        attachment = telegram_gateway.extract_telegram_attachment(message)

        self.assertIsNotNone(attachment)
        self.assertEqual(attachment.file_name, "secret.txt")

    def test_rejects_symlinked_route_directory_outside_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = self.make_cache(root)
            outside = root / "outside"
            outside.mkdir()
            (cache.cache_dir / "trading-engine").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "unsafe attachment cache route path"):
                cache.store("trading-engine", "1", self.attachment(), b"pdf", now=100, user_id="9")

            self.assertEqual(list(outside.iterdir()), [])

    def test_ignores_pending_metadata_through_symlinked_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = self.make_cache(root)
            safe_data = cache.cache_dir / "safe.pdf"
            safe_data.write_bytes(b"pdf")
            outside = root / "outside"
            outside.mkdir()
            metadata_path = outside / "x.json"
            metadata = {
                "attachment_id": "x",
                "route_id": "trading-engine",
                "chat_id": "1",
                "kind": "document",
                "file_name": "safe.pdf",
                "mime_type": "application/pdf",
                "size": 3,
                "caption": None,
                "relative_path": "safe.pdf",
                "created_at": 100,
                "status": "pending",
            }
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            (cache.cache_dir / "trading-engine").symlink_to(outside, target_is_directory=True)

            pending = cache.list_pending("trading-engine", "1", now=110, user_id="9")

            self.assertEqual(pending, ())
            self.assertEqual(json.loads(metadata_path.read_text())["status"], "pending")

    def test_cleanup_and_size_scan_do_not_read_metadata_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = self.make_cache(root)
            chat_dir = cache.cache_dir / "trading-engine" / "1"
            chat_dir.mkdir(parents=True)
            outside = root / "outside.json"
            outside.write_text('{"created_at":0,"size":3}', encoding="utf-8")
            metadata_link = chat_dir / "x.json"
            metadata_link.symlink_to(outside)

            with patch.object(cache, "_read_metadata", wraps=cache._read_metadata) as read:
                cache.cleanup_expired(now=100)
                total = cache._total_size_locked()

            self.assertEqual(total, 0)
            self.assertEqual(read.call_args_list, [])
            self.assertTrue(metadata_link.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), '{"created_at":0,"size":3}')

    def test_selects_largest_photo_variant(self) -> None:
        message = {
            "message_id": 4,
            "photo": [
                {"file_id": "small", "width": 90, "height": 90, "file_size": 100},
                {"file_id": "large", "width": 900, "height": 900, "file_size": 1000},
            ],
        }

        attachment = telegram_gateway.extract_telegram_attachment(message)

        self.assertIsNotNone(attachment)
        self.assertEqual(attachment.file_id, "large")
        self.assertEqual(attachment.file_name, "photo-4.jpg")


class GatewayAttachmentFlowTest(unittest.TestCase):
    @staticmethod
    def route() -> object:
        return SimpleNamespace(
            route_id="trading-engine",
            allowed_chat_ids={"9"},
            telegram_bot_token="test-token",
            http_timeout=10,
            parse_mode=None,
            echo_mode=False,
            ack_text=None,
            bot_commands=(),
        )

    @staticmethod
    def app() -> object:
        app = telegram_gateway.GatewayApp.__new__(telegram_gateway.GatewayApp)
        app.config = SimpleNamespace(version="test")
        app.engine = Mock()
        app.router = Mock()
        app.attachment_cache = Mock()
        app.attachment_cache.max_file_bytes = 20
        app.attachment_cache.text_payload.side_effect = lambda files: [
            {'file_name': item.file_name, 'content': '파일 내용'} for item in files]
        def freeze_request(payload, files, *, include_attachments=True):
            body = dict(payload)
            if files and include_attachments:
                body['attachments'] = app.attachment_cache.text_payload(files)
            return telegram_gateway.CachedTelegramRequest(body, files, Path('/request.json'), False)
        app.attachment_cache.freeze_request.side_effect = freeze_request
        app.append_inbound_conversation_event = Mock()
        app.append_outbound_conversation_event = Mock()
        return app

    @staticmethod
    def cached_attachment(
        *,
        attachment_id: str = "10-abc",
        file_name: str = "report.txt",
        caption: str | None = None,
        created_at: float = 100,
    ) -> object:
        return telegram_gateway.CachedTelegramAttachment(
            attachment_id=attachment_id,
            route_id="trading-engine",
            chat_id="9",
            kind="document",
            file_name=file_name,
            mime_type="text/plain",
            size=3,
            caption=caption,
            host_path=Path(f"/host/inbox/trading-engine/9/{attachment_id}.txt"),
            metadata_path=Path(f"/container/inbox/trading-engine/9/{attachment_id}.json"),
            created_at=created_at,
        )

    def test_unauthorized_chat_cannot_forward_messages_or_attachments(self) -> None:
        app = self.app()
        for content in ({"text": "/resume"}, {"text": "/version"}, {"text": "/status"},
                        {"document": {"file_id": "file-1", "file_size": 3}}):
            update = {"message": {"message_id": 10, "chat": {"id": 8}, "from": {"id": 8}, **content}}
            with self.subTest(content=content), patch.object(telegram_gateway, "TelegramClient") as client:
                app.handle_update(self.route(), update)
                client.assert_not_called()
        app.engine.post_message.assert_not_called()
        app.engine.get_version.assert_not_called()
        app.engine.get_readiness.assert_not_called()
        app.attachment_cache.store.assert_not_called()

    def test_version_reports_both_services_without_submitting_an_engine_command(self) -> None:
        for engine_version in ({"version": "engine-test", "runtime_status": "NOT_READY"},
                               RuntimeError("private engine failure")):
            app = self.app()
            app.router.resolve.return_value = telegram_gateway.ResolvedRoute(
                "trading-engine", "http://receiver/telegram", "/version")
            if isinstance(engine_version, Exception):
                app.engine.get_version.side_effect = engine_version
            else:
                app.engine.get_version.return_value = engine_version
            update = {"message": {"chat": {"id": 9}, "from": {"id": 9}, "text": "/version@my_bot"}}
            with self.subTest(engine_version=type(engine_version).__name__), patch.object(telegram_gateway, "TelegramClient") as client:
                app.handle_update(self.route(), update)
                reply = client.return_value.send_message.call_args.args[1]
                self.assertIn("telegram-gateway: test", reply)
                self.assertIn("trading-engine:", reply)
                self.assertNotIn("private", reply)
                if not isinstance(engine_version, Exception):
                    self.assertIn("engine-test", reply)
                    self.assertIn("NOT_READY", reply)
            app.engine.get_version.assert_called_once_with("http://receiver/telegram")
            app.engine.post_message.assert_not_called()
            app.attachment_cache.list_pending.assert_not_called()
            app.attachment_cache.mark_consumed.assert_not_called()

    def test_status_reports_inactive_runtime_and_keeps_ready_account_status_authorized(self) -> None:
        for readiness in ({"ready": False, "status": "WAITING_FOR_CONFIGURATION", "issues": ["config/runtime.json: 운영 승인 파일 없음"]},
                          {"ready": True, "status": "READY", "issues": []}, RuntimeError("private readiness failure")):
            app = self.app()
            app.router.resolve.return_value = telegram_gateway.ResolvedRoute(
                "trading-engine", "http://receiver/telegram", "/status")
            app.engine.post_message.return_value = {"accepted": True, "reply_text": "요청을 접수했습니다."}
            if isinstance(readiness, Exception):
                app.engine.get_readiness.side_effect = readiness
            else:
                app.engine.get_readiness.return_value = readiness
            update = {"message": {"chat": {"id": 9}, "from": {"id": 9}, "text": "/status@my_bot"}}
            with self.subTest(readiness_type=type(readiness).__name__), patch.object(telegram_gateway, "TelegramClient") as client:
                app.handle_update(self.route(), update)
                if isinstance(readiness, Exception):
                    reply = client.return_value.send_message.call_args.args[1]
                    self.assertNotIn('private', reply)
                    self.assertIn("UNCLASSIFIED_FAILURE", reply)
                elif not readiness["ready"]:
                    reply = client.return_value.send_message.call_args.args[1]
                    self.assertIn("WAITING_FOR_CONFIGURATION", reply)
                    self.assertIn("운영 승인 파일 없음", reply)
                else:
                    app.engine.post_message.assert_called_once()
                    self.assertEqual(app.engine.post_message.call_args.args[1]["user_id"], "9")
                    client.return_value.send_message.assert_not_called()
            if isinstance(readiness, Exception) or not readiness["ready"]:
                app.engine.post_message.assert_not_called()
            app.engine.get_readiness.assert_called_once_with("http://receiver/telegram")
            app.attachment_cache.mark_consumed.assert_not_called()

    def test_media_message_is_cached_without_codex_call(self) -> None:
        app = self.app()
        cached = self.cached_attachment()
        app.attachment_cache.store.return_value = cached
        client = Mock()
        client.download_file.return_value = b"pdf"
        update = {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 1_800_000_000,
                "chat": {"id": 9, "type": "private"},
                "from": {"id": 9},
                "document": {
                    "file_id": "file-1",
                    "file_name": "report.txt",
                    "file_size": 3,
                },
            },
        }

        with patch.object(telegram_gateway, "TelegramClient", return_value=client):
            app.handle_update(self.route(), update)

        client.download_file.assert_called_once_with("file-1", 20)
        app.attachment_cache.store.assert_called_once()
        app.engine.post_message.assert_not_called()
        app.attachment_cache.list_pending.assert_not_called()
        self.assertIn("저장했습니다", client.send_message.call_args.args[1])

    def test_media_caption_submits_all_pending_attachments_immediately(self) -> None:
        app = self.app()
        previous = self.cached_attachment(
            attachment_id="9-previous",
            file_name="previous.txt",
            created_at=99,
        )
        current = self.cached_attachment(
            caption="두 파일을 비교해줘",
        )
        app.attachment_cache.store.return_value = current
        app.attachment_cache.list_pending.return_value = (previous, current)
        app.router.resolve.return_value = telegram_gateway.ResolvedRoute(
            route_id="trading-engine",
            url="http://codex.test/telegram",
            text="두 파일을 비교해줘",
        )
        app.engine.post_message.return_value = {"accepted": True}
        client = Mock()
        client.download_file.return_value = b"pdf"
        update = {
            "update_id": 3,
            "message": {
                "message_id": 10,
                "date": 1_800_000_002,
                "chat": {"id": 9, "type": "private"},
                "from": {"id": 9, "username": "tester"},
                "document": {
                    "file_id": "file-1",
                    "file_name": "report.txt",
                    "file_size": 3,
                },
                "caption": "두 파일을 비교해줘",
            },
        }

        with patch.object(telegram_gateway, "TelegramClient", return_value=client):
            app.handle_update(self.route(), update)

        payload = app.engine.post_message.call_args.args[1]
        self.assertEqual(payload['text'], '두 파일을 비교해줘')
        self.assertEqual(payload['attachments'], [{'file_name': 'previous.txt', 'content': '파일 내용'},
                                                  {'file_name': 'report.txt', 'content': '파일 내용'}])
        self.assertEqual(payload["raw_message"], update["message"])
        app.attachment_cache.mark_consumed.assert_called_once_with((previous, current))
        client.send_message.assert_not_called()

    def test_failed_media_caption_submission_keeps_all_attachments_pending(self) -> None:
        app = self.app()
        previous = self.cached_attachment(
            attachment_id="9-previous",
            file_name="previous.txt",
            created_at=99,
        )
        current = self.cached_attachment(caption="두 파일을 비교해줘")
        app.attachment_cache.store.return_value = current
        app.attachment_cache.list_pending.return_value = (previous, current)
        app.router.resolve.return_value = telegram_gateway.ResolvedRoute(
            route_id="trading-engine",
            url="http://codex.test/telegram",
            text="두 파일을 비교해줘",
        )
        app.engine.post_message.side_effect = RuntimeError("private bridge unavailable")
        client = Mock()
        client.download_file.return_value = b"pdf"
        update = {
            "update_id": 4,
            "message": {
                "message_id": 10,
                "date": 1_800_000_003,
                "chat": {"id": 9, "type": "private"},
                "from": {"id": 9},
                "document": {
                    "file_id": "file-1",
                    "file_name": "report.txt",
                    "file_size": 3,
                },
                "caption": "두 파일을 비교해줘",
            },
        }

        with patch.object(telegram_gateway, "TelegramClient", return_value=client):
            app.handle_update(self.route(), update)

        app.attachment_cache.mark_consumed.assert_not_called()
        self.assertIn("파일은 저장했지만", client.send_message.call_args.args[1])

    def test_next_plain_text_injects_and_consumes_pending_attachment(self) -> None:
        app = self.app()
        cached = self.cached_attachment()
        app.attachment_cache.list_pending.return_value = (cached,)
        app.router.resolve.return_value = telegram_gateway.ResolvedRoute(
            route_id="trading-engine",
            url="http://codex.test/telegram",
            text="이 보고서를 요약해줘",
        )
        app.engine.post_message.return_value = {"accepted": True}
        update = {
            "update_id": 2,
            "message": {
                "message_id": 11,
                "date": 1_800_000_001,
                "chat": {"id": 9, "type": "private"},
                "from": {"id": 9},
                "text": "이 보고서를 요약해줘",
            },
        }

        with patch.object(telegram_gateway, "TelegramClient"):
            app.handle_update(self.route(), update)

        payload = app.engine.post_message.call_args.args[1]
        self.assertEqual(payload['text'], '이 보고서를 요약해줘')
        self.assertEqual(payload['attachments'], [{'file_name': 'report.txt', 'content': '파일 내용'}])
        self.assertNotIn('/host/', json.dumps(payload))
        app.attachment_cache.mark_consumed.assert_called_once_with((cached,))

    def test_new_clears_pending_files_only_after_engine_accepts_reset(self) -> None:
        for accepted in (True, False):
            app = self.app()
            pending = (self.cached_attachment(),) * 6
            app.attachment_cache.list_pending.return_value = pending
            app.router.resolve.return_value = telegram_gateway.ResolvedRoute(
                'trading-engine', 'http://receiver/telegram', '/new')
            app.engine.post_message.return_value = {'accepted': accepted}
            update = {'message': {'chat': {'id': 9}, 'from': {'id': 9}, 'text': '/new'}}
            with self.subTest(accepted=accepted), patch.object(telegram_gateway, 'TelegramClient'):
                app.handle_update(self.route(), update)
            if accepted:
                app.attachment_cache.mark_consumed.assert_called_once_with(pending)
            else:
                app.attachment_cache.mark_consumed.assert_not_called()

    def test_command_does_not_consume_pending_attachment(self) -> None:
        app = self.app()
        app.router.resolve.return_value = telegram_gateway.ResolvedRoute(
            route_id="trading-engine",
            url="http://codex.test/telegram",
            text="/session",
        )
        app.engine.post_message.return_value = {"accepted": True}
        update = {
            "message": {
                "message_id": 12,
                "chat": {"id": 9, "type": "private"},
                "from": {"id": 9},
                "text": "/session",
            }
        }

        with patch.object(telegram_gateway, "TelegramClient"):
            app.handle_update(self.route(), update)

        app.attachment_cache.list_pending.assert_not_called()
        app.attachment_cache.mark_consumed.assert_not_called()
        payload = app.engine.post_message.call_args.args[1]
        self.assertNotIn("<telegram_attachments>", payload["text"])

    def test_failed_codex_submission_keeps_attachment_pending(self) -> None:
        app = self.app()
        cached = self.cached_attachment()
        app.attachment_cache.list_pending.return_value = (cached,)
        app.router.resolve.return_value = telegram_gateway.ResolvedRoute(
            route_id="trading-engine",
            url="http://codex.test/telegram",
            text="분석해줘",
        )
        app.engine.post_message.side_effect = RuntimeError("private bridge unavailable")
        update = {
            "message": {
                "message_id": 13,
                "chat": {"id": 9, "type": "private"},
                "from": {"id": 9},
                "text": "분석해줘",
            }
        }

        with patch.object(telegram_gateway, "TelegramClient") as client:
            app.handle_update(self.route(), update)
            reply = client.return_value.send_message.call_args.args[1]
            self.assertIn("자동 재시도하지 않습니다", reply)
            self.assertNotIn("private", reply)

        app.engine.post_message.assert_called_once()
        app.attachment_cache.mark_consumed.assert_not_called()

    def test_structured_rejection_keeps_plain_and_caption_attachments_pending(self) -> None:
        for media in (False, True):
            app = self.app()
            cached = self.cached_attachment(caption='분석해줘')
            app.attachment_cache.store.return_value = cached
            app.attachment_cache.list_pending.return_value = (cached,)
            app.router.resolve.return_value = telegram_gateway.ResolvedRoute('trading-engine', 'http://receiver/telegram', '분석해줘')
            app.engine.post_message.side_effect = telegram_gateway.EngineRequestRejected('계좌 초기화 중입니다. 잠시 후 다시 확인해 주세요.')
            content = {'caption': '분석해줘', 'document': {'file_id': 'file-1', 'file_name': 'report.txt', 'file_size': 3}} if media else {'text': '분석해줘'}
            update = {'update_id': 9, 'message': {'message_id': 13, 'chat': {'id': 9}, 'from': {'id': 9}, **content}}
            with self.subTest(media=media), patch.object(telegram_gateway, 'TelegramClient') as client:
                client.return_value.download_file.return_value = b'txt'
                app.handle_update(self.route(), update)
                self.assertIn('계좌 초기화 중', client.return_value.send_message.call_args.args[1])
            app.attachment_cache.mark_consumed.assert_not_called()

    def test_missing_durable_receipt_keeps_attachments_and_legacy_ack_is_quiet(self) -> None:
        app = self.app()
        cached = self.cached_attachment()
        app.attachment_cache.list_pending.return_value = (cached,)
        app.router.resolve.return_value = telegram_gateway.ResolvedRoute('trading-engine', 'http://receiver/telegram', '분석해줘')
        app.engine.post_message.return_value = None
        route = self.route()
        route.ack_text = '요청이 접수되었습니다'
        update = {'message': {'message_id': 14, 'chat': {'id': 9}, 'from': {'id': 9}, 'text': '분석해줘'}}
        with patch.object(telegram_gateway, 'TelegramClient') as client:
            app.handle_update(route, update)
            client.return_value.send_message.assert_not_called()
        app.attachment_cache.mark_consumed.assert_not_called()

    def test_unsupported_media_is_explicitly_rejected_before_cache_or_model(self) -> None:
        app = self.app()
        update = {'message': {'message_id': 15, 'chat': {'id': 9}, 'from': {'id': 9},
                             'document': {'file_id': 'file-1', 'file_name': 'report.pdf', 'file_size': 3},
                             'caption': '요약해줘'}}
        with patch.object(telegram_gateway, 'TelegramClient') as client:
            client.return_value.download_file.return_value = b'pdf'
            app.handle_update(self.route(), update)
            self.assertIn('PDF·이미지·바이너리 파일은 읽지 않았습니다', client.return_value.send_message.call_args.args[1])
        app.attachment_cache.store.assert_not_called()
        app.engine.post_message.assert_not_called()

    def test_periodic_cleanup_runs_without_new_messages(self) -> None:
        app = self.app()
        app.attachment_cleanup_interval_seconds = 60
        app.next_attachment_cleanup_at = 0

        app.cleanup_attachment_cache_if_due(now=100)
        app.cleanup_attachment_cache_if_due(now=120)
        app.cleanup_attachment_cache_if_due(now=160)

        self.assertEqual(
            app.attachment_cache.cleanup_expired.call_args_list,
            [unittest.mock.call(now=100), unittest.mock.call(now=160)],
        )


class GatewaySenderAttachmentFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = GatewayAttachmentFlowTest.app()
        self.reload_cache()
        self.app.router.resolve.side_effect = lambda route, text: telegram_gateway.ResolvedRoute(
            route, "http://receiver/telegram", text)
        self.app.engine.post_message.return_value = {"accepted": True}
        self.client = Mock()
        self.client.download_file.side_effect = lambda file_id, _limit: file_id.encode()
        self.client_patch = patch.object(telegram_gateway, "TelegramClient", return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def reload_cache(self) -> None:
        self.app.attachment_cache = telegram_gateway.TelegramAttachmentCache(
            self.root / "container", self.root / "host", ttl_seconds=60,
            max_file_bytes=32768, max_total_bytes=262144, max_pending=5)

    def receive(self, update_id: int, user_id: int, *, text: str | None = None,
                file_id: str | None = None, caption: str | None = None,
                chat_id: int = -9, route_id: str = "trading-engine") -> None:
        route = GatewayAttachmentFlowTest.route()
        route.route_id, route.allowed_chat_ids = route_id, {str(chat_id)}
        message = {"message_id": update_id, "chat": {"id": chat_id, "type": "group"},
                   "from": {"id": user_id}}
        if file_id is not None:
            message["document"] = {"file_id": file_id, "file_name": f"{file_id}.txt",
                                   "mime_type": "text/plain"}
            if caption is not None:
                message["caption"] = caption
        else:
            message["text"] = text
        self.app.handle_update(route, {"update_id": update_id, "message": message})

    def metadata(self) -> list[dict]:
        values = [json.loads(path.read_text()) for path in
                  self.app.attachment_cache.cache_dir.rglob("*.json")]
        return [item for item in values if item.get("kind") != "request"]

    def assert_sender_files(self, *, caption: bool) -> None:
        # A may be disallowed by the engine even though the group is allowed.
        self.receive(10, 7, file_id="sender-a")
        self.receive(11, 9, file_id="sender-b")
        self.reload_cache()
        if caption:
            self.receive(12, 9, file_id="sender-b-second", caption="분석해줘")
        else:
            self.receive(12, 9, text="분석해줘")
        payload = self.app.engine.post_message.call_args.args[1]
        expected = [{"file_name": "sender-b.txt", "content": "sender-b"}]
        if caption:
            expected.append({"file_name": "sender-b-second.txt", "content": "sender-b-second"})
        self.assertEqual(payload["user_id"], "9")
        self.assertEqual(payload["attachments"], expected)
        statuses = {item["file_name"]: item["status"] for item in self.metadata()}
        self.assertEqual(statuses["sender-a.txt"], "pending")
        self.assertEqual(statuses["sender-b.txt"], "consumed")
        self.receive(13, 7, text="내 파일을 분석해줘")
        self.assertEqual(self.app.engine.post_message.call_args.args[1]["attachments"],
                         [{"file_name": "sender-a.txt", "content": "sender-a"}])

    def test_group_prompt_only_uses_current_senders_files_after_restart(self) -> None:
        self.assert_sender_files(caption=False)

    def test_group_caption_only_uses_current_senders_files_after_restart(self) -> None:
        self.assert_sender_files(caption=True)

    def test_new_does_not_clear_another_sender_or_chat_or_route(self) -> None:
        self.receive(20, 7, file_id="sender-a")
        self.receive(21, 9, file_id="sender-b")
        self.receive(22, 9, file_id="other-chat", chat_id=-10)
        self.receive(23, 9, file_id="other-route", route_id="other")
        self.receive(24, 9, text="/new")
        statuses = {item["file_name"]: item["status"] for item in self.metadata()}
        self.assertEqual(statuses, {"sender-a.txt": "pending", "sender-b.txt": "consumed",
                                    "other-chat.txt": "pending", "other-route.txt": "pending"})
        for update_id, user_id, name, chat_id, route_id in (
            (25, 7, "sender-a", -9, "trading-engine"),
            (26, 9, "other-chat", -10, "trading-engine"),
            (27, 9, "other-route", -9, "other"),
        ):
            self.receive(update_id, user_id, text="분석해줘", chat_id=chat_id, route_id=route_id)
            self.assertEqual(self.app.engine.post_message.call_args.args[1]["attachments"],
                             [{"file_name": f"{name}.txt", "content": name}])

    def test_legacy_cache_without_sender_is_not_assigned_to_next_user(self) -> None:
        self.receive(30, 9, file_id="legacy")
        path = next(self.app.attachment_cache.cache_dir.rglob("*.json"))
        metadata = json.loads(path.read_text())
        metadata.pop("user_id", None)
        path.write_text(json.dumps(metadata))
        self.reload_cache()
        self.receive(31, 9, text="분석해줘")
        self.assertNotIn("attachments", self.app.engine.post_message.call_args.args[1])
        self.assertEqual(json.loads(path.read_text())["status"], "pending")
        self.app.attachment_cache.cleanup_expired(now=metadata["created_at"] + 61)
        self.assertFalse(path.exists())

    def test_repeated_upload_update_does_not_duplicate_pending_files_after_restart(self) -> None:
        self.receive(40, 9, file_id="once")
        self.reload_cache()
        self.receive(40, 9, file_id="once")
        self.receive(41, 9, text="분석해줘")
        self.assertEqual(self.app.engine.post_message.call_args.args[1]["attachments"],
                         [{"file_name": "once.txt", "content": "once"}])
        self.receive(40, 9, file_id="once")
        self.receive(42, 9, text="다음 메시지")
        self.assertNotIn("attachments", self.app.engine.post_message.call_args.args[1])
        self.assertEqual(len(self.metadata()), 1)

    def test_repeated_caption_update_does_not_resubmit_consumed_attachment(self) -> None:
        self.receive(50, 9, file_id="caption", caption="분석해줘")
        self.reload_cache()
        self.receive(50, 9, file_id="caption", caption="분석해줘")
        self.app.engine.post_message.assert_called_once()
        self.assertEqual(len(self.metadata()), 1)

    def test_conflicting_upload_update_does_not_replace_original_file(self) -> None:
        self.receive(60, 9, file_id="original")
        self.receive(60, 9, file_id="changed")
        self.assertIn("같은 update ID", self.client.send_message.call_args.args[1])
        self.receive(61, 9, text="분석해줘")
        self.assertEqual(self.app.engine.post_message.call_args.args[1]["attachments"],
                         [{"file_name": "original.txt", "content": "original"}])

    def test_accepted_text_replay_does_not_submit_again_or_consume_new_files(self) -> None:
        self.receive(80, 9, file_id="first")
        self.receive(81, 9, text="분석해줘")
        first_payload = self.app.engine.post_message.call_args.args[1]
        self.receive(82, 9, file_id="later")
        self.reload_cache()
        self.app.config.version = "new-gateway-version"
        self.receive(81, 9, text="분석해줘")
        self.app.engine.post_message.assert_called_once()
        self.assertEqual({item["file_name"]: item["status"] for item in self.metadata()},
                         {"first.txt": "consumed", "later.txt": "pending"})
        self.receive(83, 9, text="다음 파일")
        self.assertEqual(self.app.engine.post_message.call_args.args[1]["attachments"],
                         [{"file_name": "later.txt", "content": "later"}])
        self.assertEqual(first_payload["attachments"], [{"file_name": "first.txt", "content": "first"}])

    def test_uncertain_text_retry_reuses_original_payload_and_engine_receipt(self) -> None:
        import sqlite3

        receiver_source = MODULE_PATH.parents[1] / "trading-engine" / "src"
        with patch.object(sys, "path", [str(receiver_source), *sys.path]):
            from danta.adapters.telegram import TelegramAdapter

        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        receiver = TelegramAdapter(db, enabled=True, allowed_senders=("9",), allowed_chats=("-9",))
        responses = []

        def post(_url, payload):
            response, _request = receiver.receive(payload)
            responses.append(response)
            if len(responses) == 1:
                raise RuntimeError("synthetic response lost after durable acceptance")
            return response

        self.app.engine.post_message.side_effect = post
        self.receive(90, 9, file_id="first")
        self.receive(91, 9, text="분석해줘")
        first_payload = self.app.engine.post_message.call_args.args[1]
        self.receive(92, 9, file_id="later")
        self.reload_cache()
        self.receive(91, 9, text="분석해줘")
        self.assertEqual(self.app.engine.post_message.call_args.args[1], first_payload)
        self.assertEqual(responses[0]["request_id"], responses[1]["request_id"])
        self.assertEqual(db.execute("SELECT count(*) FROM telegram_requests").fetchone()[0], 1)
        self.assertEqual({item["file_name"]: item["status"] for item in self.metadata()},
                         {"first.txt": "consumed", "later.txt": "pending"})

    def test_retry_without_original_attachment_does_not_pick_up_new_attachment(self) -> None:
        self.app.engine.post_message.side_effect = [RuntimeError("synthetic timeout"), {"accepted": True}]
        self.receive(100, 9, text="안녕")
        original = self.app.engine.post_message.call_args.args[1]
        self.receive(101, 9, file_id="later")
        self.reload_cache()
        self.receive(100, 9, text="안녕")
        self.assertEqual(self.app.engine.post_message.call_args.args[1], original)
        self.assertNotIn("attachments", original)
        self.assertEqual(self.metadata()[0]["status"], "pending")

    def test_conflicting_text_or_sender_for_update_does_not_reuse_receipt(self) -> None:
        self.receive(110, 9, text="처음 메시지")
        for user_id, text in ((9, "다른 메시지"), (7, "처음 메시지")):
            with self.subTest(user_id=user_id):
                self.receive(110, user_id, text=text)
                self.assertIn("같은 update ID", self.client.send_message.call_args.args[1])
                self.app.engine.post_message.assert_called_once()

    def test_new_replay_keeps_files_uploaded_after_reset(self) -> None:
        self.receive(120, 9, file_id="first")
        self.receive(121, 9, text="/new")
        self.receive(122, 9, file_id="later")
        self.reload_cache()
        self.receive(121, 9, text="/new")
        self.app.engine.post_message.assert_called_once()
        self.assertEqual({item["file_name"]: item["status"] for item in self.metadata()},
                         {"first.txt": "consumed", "later.txt": "pending"})

    def test_frozen_request_files_follow_cache_permissions_and_ttl(self) -> None:
        self.receive(130, 9, text="안녕")
        request_path = next(self.app.attachment_cache.cache_dir.rglob("update-130.json"))
        metadata = json.loads(request_path.read_text())
        body_path = self.app.attachment_cache.cache_dir / metadata["relative_path"]
        self.assertEqual(request_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(body_path.stat().st_mode & 0o777, 0o600)
        self.app.attachment_cache.cleanup_expired(now=metadata["created_at"] + 61)
        self.assertFalse(request_path.exists())
        self.assertFalse(body_path.exists())

    def test_request_receipt_write_failure_does_not_submit_or_leave_orphan_body(self) -> None:
        write = self.app.attachment_cache._write_request_json

        def fail_receipt(path, value):
            if path.suffix == ".json":
                raise OSError("synthetic receipt write failure")
            write(path, value)

        with patch.object(self.app.attachment_cache, "_write_request_json", side_effect=fail_receipt):
            self.receive(140, 9, text="안녕")
        self.app.engine.post_message.assert_not_called()
        self.assertEqual(list(self.app.attachment_cache.cache_dir.rglob("*.wire")), [])
        self.receive(140, 9, text="안녕")
        self.app.engine.post_message.assert_called_once()

    def test_engine_rejects_unallowed_sender_without_lending_their_file_to_allowed_sender(self) -> None:
        import sqlite3

        receiver_source = MODULE_PATH.parents[1] / "trading-engine" / "src"
        with patch.object(sys, "path", [str(receiver_source), *sys.path]):
            from danta.adapters import AdapterError
            from danta.adapters.telegram import TelegramAdapter

        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        receiver = TelegramAdapter(db, enabled=True, allowed_senders=("9",), allowed_chats=("-9",))

        def post(_url, payload):
            try:
                return receiver.receive(payload)[0]
            except AdapterError as error:
                raise telegram_gateway.EngineRequestRejected(error.code) from None

        self.app.engine.post_message.side_effect = post
        self.receive(70, 7, file_id="unallowed", caption="분석해줘")
        self.assertEqual(self.client.send_message.call_args.args[1], "SENDER_NOT_ALLOWED")
        self.receive(71, 9, file_id="allowed")
        self.receive(72, 9, text="분석해줘")
        accepted = [json.loads(row[0]) for row in db.execute("SELECT payload FROM telegram_requests")]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["user_id"], "9")
        self.assertEqual(accepted[0]["attachments"], [{"file_name": "allowed.txt", "content": "allowed"}])
        self.assertEqual({item["file_name"]: item["status"] for item in self.metadata()},
                         {"unallowed.txt": "pending", "allowed.txt": "consumed"})


class TelegramUpdateTest(unittest.TestCase):
    def test_extracts_callback_data_as_synthetic_message(self) -> None:
        update = {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 7, "username": "tester"},
                "message": {
                    "message_id": 11,
                    "date": 1_800_000_000,
                    "chat": {"id": 9, "type": "private"},
                    "text": "old bot message",
                },
                "data": "/resume page=1",
            }
        }

        message, callback_id = telegram_gateway.extract_telegram_update_message(update)

        self.assertEqual(callback_id, "callback-1")
        self.assertEqual(message["text"], "/resume page=1")
        self.assertEqual(message["from"]["id"], 7)
        self.assertEqual(message["chat"]["id"], 9)


if __name__ == "__main__":
    unittest.main()
