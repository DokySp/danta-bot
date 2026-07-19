from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.request import Request, urlopen

from ..app import App


class AppTelegramDispatchTest(unittest.TestCase):
    def app(self) -> App:
        app = App.__new__(App)
        app.config = SimpleNamespace(host="127.0.0.1", port=0)
        app.telegram_worker = Mock()
        return app

    @staticmethod
    def post(server_port: int, text: str) -> tuple[int, dict[str, object]]:
        request = Request(
            f"http://127.0.0.1:{server_port}/telegram",
            data=json.dumps(
                {"text": text, "chat_id": "chat", "route": "route", "message_id": 1}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_immediate_command_bypasses_worker_queue(self) -> None:
        app = self.app()
        app.telegram_worker.handle_immediate.return_value = True
        server = app._serve_http()
        try:
            status, payload = self.post(server.server_port, "/stop")
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "queued": False})
        app.telegram_worker.submit.assert_not_called()
        task = app.telegram_worker.handle_immediate.call_args.args[0]
        self.assertEqual(task.text, "/stop")

    def test_normal_message_remains_queued(self) -> None:
        app = self.app()
        app.telegram_worker.handle_immediate.return_value = False
        server = app._serve_http()
        try:
            status, payload = self.post(server.server_port, "hello")
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(status, 202)
        self.assertEqual(payload, {"ok": True, "queued": True})
        app.telegram_worker.submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
