from __future__ import annotations

import queue
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ..commands.session import handle_session
from ..worker import TelegramTask


class SessionCommandTest(unittest.TestCase):
    def test_reports_default_session_and_active_runtime_without_codex_call(self) -> None:
        worker = SimpleNamespace(
            runner=Mock(),
            state=Mock(),
            gateway=Mock(),
            queue=queue.Queue(),
            config=SimpleNamespace(workspace_dir="/workspace", mcp_trading_env="paper"),
        )
        worker.state.get_default_session.return_value = "session-123"
        worker.runner.has_active_telegram_run.return_value = True
        worker.runner.runtime_defaults.return_value = SimpleNamespace(
            model="model-a",
            model_reasoning_effort="high",
        )

        handle_session(
            worker,
            TelegramTask(chat_id="chat", route="route", text="/session"),
            "",
        )

        message = worker.gateway.send_message.call_args.args[0]
        self.assertIn("session-123", message)
        self.assertIn("🏃‍♂️ 실행 중", message)
        self.assertIn("model-a", message)
        self.assertIn("high", message)
        worker.runner.run_resume.assert_not_called()
        worker.runner.run_new_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
