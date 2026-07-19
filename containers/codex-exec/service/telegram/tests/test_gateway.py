from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ..gateway import DraftProgressReporter, TelegramGateway


class TelegramGatewayDocumentTest(unittest.TestCase):
    @patch("service.telegram.gateway.urlopen")
    def test_send_document_uses_attachment_filename_override(self, urlopen: Mock) -> None:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        urlopen.return_value = response
        gateway = TelegramGateway(
            SimpleNamespace(
                telegram_gateway_url="http://telegram-gateway/sendMessage",
                telegram_route="default-route",
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily-trading-report.html"
            path.write_text("report", encoding="utf-8")

            gateway.send_document(path, filename="daily-trading-report-run-20260715.html")

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["filename"], "daily-trading-report-run-20260715.html")
        self.assertEqual(request.full_url, "http://telegram-gateway/sendDocument")

    @patch("service.telegram.gateway.urlopen")
    def test_send_message_draft_uses_draft_endpoint_and_plain_text(self, urlopen: Mock) -> None:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        urlopen.return_value = response
        gateway = TelegramGateway(
            SimpleNamespace(
                telegram_gateway_url="http://telegram-gateway/sendMessage",
                telegram_route="default-route",
            )
        )

        gateway.send_message_draft("progress", 7, "chat", "route")

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "http://telegram-gateway/sendMessageDraft")
        self.assertEqual(
            payload,
            {"draft_id": 7, "text": "progress", "chat_id": "chat", "route": "route"},
        )


class DraftProgressReporterTest(unittest.TestCase):
    def test_keeps_only_latest_pending_update_and_joins_before_exit(self) -> None:
        gateway = Mock()
        first_started = threading.Event()
        release_first = threading.Event()
        latest_sent = threading.Event()

        def send(text: str, *_args: object) -> None:
            if text == "first":
                first_started.set()
                self.assertTrue(release_first.wait(timeout=5))
            if text == "latest":
                latest_sent.set()

        gateway.send_message_draft.side_effect = send
        reporter = DraftProgressReporter(gateway, "chat", "route", 9)

        with reporter:
            reporter.update("first")
            self.assertTrue(first_started.wait(timeout=5))
            reporter.update("superseded")
            reporter.update("latest")
            release_first.set()
            self.assertTrue(latest_sent.wait(timeout=5))

        self.assertEqual(
            [call.args for call in gateway.send_message_draft.call_args_list],
            [("first", 9, "chat", "route"), ("latest", 9, "chat", "route")],
        )

    def test_discards_unsent_progress_during_exit(self) -> None:
        gateway = Mock()
        sending = threading.Event()
        release = threading.Event()

        def send(_text: str, *_args: object) -> None:
            sending.set()
            self.assertTrue(release.wait(timeout=5))

        gateway.send_message_draft.side_effect = send
        reporter = DraftProgressReporter(gateway, "chat", "route", 10)
        reporter.__enter__()
        reporter.update("in flight")
        self.assertTrue(sending.wait(timeout=5))
        reporter.update("must be discarded")

        exited = threading.Event()

        def close() -> None:
            reporter.__exit__(None, None, None)
            exited.set()

        close_thread = threading.Thread(target=close)
        close_thread.start()
        self.assertFalse(exited.wait(timeout=0.05))
        release.set()
        self.assertTrue(exited.wait(timeout=5))
        close_thread.join(timeout=1)

        gateway.send_message_draft.assert_called_once_with(
            "in flight", 10, "chat", "route"
        )


if __name__ == "__main__":
    unittest.main()
