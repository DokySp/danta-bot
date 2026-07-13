from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from ..commands.dispatcher import handle_telegram_command
from ..commands.reasoning_effort import handle_reasoning_effort


def write_runtime_config(path: Path, effort: str = "medium") -> None:
    path.write_text(
        "\n".join(
            [
                "defaults:",
                "  model: model-a",
                f"  model_reasoning_effort: {effort}",
                "  new_session_prompt: hello",
                "daily_trading: {}",
                "",
            ]
        ),
        encoding="utf-8",
    )


class ReasoningEffortCommandTest(unittest.TestCase):
    def test_dispatcher_routes_reasoning_effort_command(self) -> None:
        worker = SimpleNamespace()
        task = SimpleNamespace()

        with patch(
            "service.telegram.commands.dispatcher.handle_reasoning_effort"
        ) as handler:
            handle_telegram_command(worker, task, "reasoning_effort", "high")

        handler.assert_called_once_with(worker, task, "high")

    def test_without_value_shows_current_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-runtime.yaml"
            write_runtime_config(path, "medium")
            worker = SimpleNamespace(
                config=SimpleNamespace(codex_runtime_config_file=path),
                gateway=SimpleNamespace(send_message=Mock()),
            )
            task = SimpleNamespace(chat_id="chat", route="v1")

            handle_reasoning_effort(worker, task, "")

            message, chat_id, route = worker.gateway.send_message.call_args.args
            self.assertIn("현재: <code>medium</code>", message)
            self.assertIn("max, ultra", message)
            self.assertIn("지원 여부는 선택된 모델이 판단", message)
            self.assertEqual(chat_id, "chat")
            self.assertEqual(route, "v1")

    def test_with_model_defined_value_updates_defaults_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-runtime.yaml"
            write_runtime_config(path, "medium")
            worker = SimpleNamespace(
                config=SimpleNamespace(codex_runtime_config_file=path),
                gateway=SimpleNamespace(send_message=Mock()),
            )
            task = SimpleNamespace(chat_id="chat", route="v1")

            handle_reasoning_effort(worker, task, "ultra")

            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["defaults"]["model_reasoning_effort"],
                "ultra",
            )
            message = worker.gateway.send_message.call_args.args[0]
            self.assertIn("기본 reasoning effort 변경", message)
            self.assertIn("범위: <code>defaults.model_reasoning_effort</code>", message)

    def test_multiple_values_do_not_update_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-runtime.yaml"
            write_runtime_config(path, "medium")
            worker = SimpleNamespace(
                config=SimpleNamespace(codex_runtime_config_file=path),
                gateway=SimpleNamespace(send_message=Mock()),
            )
            task = SimpleNamespace(chat_id="chat", route="v1")

            handle_reasoning_effort(worker, task, "max ultra")

            self.assertIn("model_reasoning_effort: medium", path.read_text(encoding="utf-8"))
            self.assertIn("사용법:", worker.gateway.send_message.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
