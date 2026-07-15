from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ..gateway import TelegramGateway


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


if __name__ == "__main__":
    unittest.main()
