from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ..commands.dispatcher import handle_telegram_command
from ..commands.external_flow import handle_external_flow, handle_external_flow_clear


class ExternalFlowCommandTest(unittest.TestCase):
    def test_records_and_clears_exception_without_deleting_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            worker = SimpleNamespace(
                config=SimpleNamespace(workspace_dir=Path(tmp_name)),
                gateway=SimpleNamespace(send_message=Mock()),
            )
            task = SimpleNamespace(chat_id="chat", route="route")

            handle_external_flow(worker, task, "2026-08-20")
            handle_external_flow_clear(worker, task, "2026-08-20")

            ledger = Path(tmp_name) / "memory/account-performance/external-flows.jsonl"
            rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["action"] for row in rows], ["exclude", "clear"])
            self.assertIn("성과 계산에서 제외", worker.gateway.send_message.call_args_list[0].args[0])
            self.assertIn("정정 기록", worker.gateway.send_message.call_args_list[1].args[0])

    def test_invalid_or_future_date_shows_usage_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            worker = SimpleNamespace(
                config=SimpleNamespace(workspace_dir=Path(tmp_name)),
                gateway=SimpleNamespace(send_message=Mock()),
            )
            task = SimpleNamespace(chat_id="chat", route="route")

            handle_external_flow(worker, task, "2999-01-01")

            self.assertIn("사용법", worker.gateway.send_message.call_args.args[0])
            self.assertFalse((Path(tmp_name) / "memory/account-performance/external-flows.jsonl").exists())

    def test_dispatcher_routes_external_flow(self) -> None:
        worker = SimpleNamespace()
        task = SimpleNamespace()
        with patch("service.telegram.commands.dispatcher.handle_external_flow") as handler:
            handle_telegram_command(worker, task, "external_flow", "2026-08-20")
        handler.assert_called_once_with(worker, task, "2026-08-20")


if __name__ == "__main__":
    unittest.main()
