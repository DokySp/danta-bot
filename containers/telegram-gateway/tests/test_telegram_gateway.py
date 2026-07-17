from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


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
            first = cache.store("v2", "chat-1", self.attachment(), b"pdf", now=100)
            cache.store("v2", "chat-2", self.attachment(file_name="other.pdf"), b"two", now=101)

            reloaded = self.make_cache(root)
            pending = reloaded.list_pending("v2", "chat-1", now=110)

            self.assertEqual([item.file_name for item in pending], ["report.pdf"])
            self.assertEqual(pending[0].host_path, root / "host" / "v2" / "chat-1" / first.host_path.name)
            reloaded.mark_consumed(pending, now=111)
            self.assertEqual(reloaded.list_pending("v2", "chat-1", now=112), ())
            self.assertEqual(len(reloaded.list_pending("v2", "chat-2", now=112)), 1)
            self.assertTrue(first.metadata_path.with_suffix(".pdf").exists())

    def test_cleanup_removes_expired_content_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = self.make_cache(Path(tmp), ttl_seconds=10)
            item = cache.store("v2", "1", self.attachment(), b"pdf", now=100)
            data_path = item.metadata_path.with_suffix(".pdf")

            cache.cleanup_expired(now=111)

            self.assertFalse(item.metadata_path.exists())
            self.assertFalse(data_path.exists())

    def test_rejects_pending_and_total_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = self.make_cache(Path(tmp))
            cache.store("v2", "1", self.attachment(file_name="a.pdf"), b"123", now=100)
            cache.store("v2", "1", self.attachment(file_name="b.pdf"), b"456", now=101)

            with self.assertRaisesRegex(ValueError, "최대 2개"):
                cache.store("v2", "1", self.attachment(file_name="c.pdf"), b"789", now=102)

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
            (cache.cache_dir / "v2").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "unsafe attachment cache route path"):
                cache.store("v2", "1", self.attachment(), b"pdf", now=100)

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
                "route_id": "v2",
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
            (cache.cache_dir / "v2").symlink_to(outside, target_is_directory=True)

            pending = cache.list_pending("v2", "1", now=110)

            self.assertEqual(pending, ())
            self.assertEqual(json.loads(metadata_path.read_text())["status"], "pending")

    def test_cleanup_and_size_scan_do_not_read_metadata_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = self.make_cache(root)
            chat_dir = cache.cache_dir / "v2" / "1"
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
            route_id="v2",
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
        app.codex = Mock()
        app.router = Mock()
        app.attachment_cache = Mock()
        app.attachment_cache.max_file_bytes = 20
        app.append_inbound_conversation_event = Mock()
        app.append_outbound_conversation_event = Mock()
        return app

    @staticmethod
    def cached_attachment(
        *,
        attachment_id: str = "10-abc",
        file_name: str = "report.pdf",
        caption: str | None = None,
        created_at: float = 100,
    ) -> object:
        return telegram_gateway.CachedTelegramAttachment(
            attachment_id=attachment_id,
            route_id="v2",
            chat_id="9",
            kind="document",
            file_name=file_name,
            mime_type="application/pdf",
            size=3,
            caption=caption,
            host_path=Path(f"/host/inbox/v2/9/{attachment_id}.pdf"),
            metadata_path=Path(f"/container/inbox/v2/9/{attachment_id}.json"),
            created_at=created_at,
        )

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
                    "file_name": "report.pdf",
                    "file_size": 3,
                },
            },
        }

        with patch.object(telegram_gateway, "TelegramClient", return_value=client):
            app.handle_update(self.route(), update)

        client.download_file.assert_called_once_with("file-1", 20)
        app.attachment_cache.store.assert_called_once()
        app.codex.post_message.assert_not_called()
        app.attachment_cache.list_pending.assert_not_called()
        self.assertIn("저장했습니다", client.send_message.call_args.args[1])

    def test_media_caption_submits_all_pending_attachments_immediately(self) -> None:
        app = self.app()
        previous = self.cached_attachment(
            attachment_id="9-previous",
            file_name="previous.pdf",
            created_at=99,
        )
        current = self.cached_attachment(
            caption="두 파일을 비교해줘",
        )
        app.attachment_cache.store.return_value = current
        app.attachment_cache.list_pending.return_value = (previous, current)
        app.router.resolve.return_value = telegram_gateway.ResolvedRoute(
            route_id="v2",
            url="http://codex.test/telegram",
            text="두 파일을 비교해줘",
        )
        app.codex.post_message.return_value = None
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
                    "file_name": "report.pdf",
                    "file_size": 3,
                },
                "caption": "두 파일을 비교해줘",
            },
        }

        with patch.object(telegram_gateway, "TelegramClient", return_value=client):
            app.handle_update(self.route(), update)

        payload = app.codex.post_message.call_args.args[1]
        self.assertIn("/host/inbox/v2/9/9-previous.pdf", payload["text"])
        self.assertIn("/host/inbox/v2/9/10-abc.pdf", payload["text"])
        self.assertIn("두 파일을 비교해줘", payload["text"])
        self.assertEqual(payload["raw_message"], update["message"])
        app.attachment_cache.mark_consumed.assert_called_once_with((previous, current))
        client.send_message.assert_not_called()

    def test_failed_media_caption_submission_keeps_all_attachments_pending(self) -> None:
        app = self.app()
        previous = self.cached_attachment(
            attachment_id="9-previous",
            file_name="previous.pdf",
            created_at=99,
        )
        current = self.cached_attachment(caption="두 파일을 비교해줘")
        app.attachment_cache.store.return_value = current
        app.attachment_cache.list_pending.return_value = (previous, current)
        app.router.resolve.return_value = telegram_gateway.ResolvedRoute(
            route_id="v2",
            url="http://codex.test/telegram",
            text="두 파일을 비교해줘",
        )
        app.codex.post_message.side_effect = RuntimeError("bridge unavailable")
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
                    "file_name": "report.pdf",
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
            route_id="v2",
            url="http://codex.test/telegram",
            text="이 보고서를 요약해줘",
        )
        app.codex.post_message.return_value = None
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

        payload = app.codex.post_message.call_args.args[1]
        self.assertIn("<telegram_attachments>", payload["text"])
        self.assertIn("/host/inbox/v2/9/10-abc.pdf", payload["text"])
        self.assertIn("이 보고서를 요약해줘", payload["text"])
        app.attachment_cache.mark_consumed.assert_called_once_with((cached,))

    def test_command_does_not_consume_pending_attachment(self) -> None:
        app = self.app()
        app.router.resolve.return_value = telegram_gateway.ResolvedRoute(
            route_id="v2",
            url="http://codex.test/telegram",
            text="/session",
        )
        app.codex.post_message.return_value = None
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
        payload = app.codex.post_message.call_args.args[1]
        self.assertNotIn("<telegram_attachments>", payload["text"])

    def test_failed_codex_submission_keeps_attachment_pending(self) -> None:
        app = self.app()
        cached = self.cached_attachment()
        app.attachment_cache.list_pending.return_value = (cached,)
        app.router.resolve.return_value = telegram_gateway.ResolvedRoute(
            route_id="v2",
            url="http://codex.test/telegram",
            text="분석해줘",
        )
        app.codex.post_message.side_effect = RuntimeError("bridge unavailable")
        update = {
            "message": {
                "message_id": 13,
                "chat": {"id": 9, "type": "private"},
                "from": {"id": 9},
                "text": "분석해줘",
            }
        }

        with patch.object(telegram_gateway, "TelegramClient"):
            with self.assertRaisesRegex(RuntimeError, "bridge unavailable"):
                app.handle_update(self.route(), update)

        app.attachment_cache.mark_consumed.assert_not_called()

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
