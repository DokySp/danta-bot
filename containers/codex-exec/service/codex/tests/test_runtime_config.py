from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ..runner import CodexRunner
from ..runtime_config import load_codex_runtime_defaults


def write_runtime_config(path: Path, *, model: str, effort: str, prompt: str = "new session") -> None:
    path.write_text(
        "\n".join(
            [
                "defaults:",
                f"  model: {model}",
                f"  model_reasoning_effort: {effort}",
                f"  new_session_prompt: {prompt}",
                "daily_trading:",
                "  collection:",
                "    model: collection-model",
                "    model_reasoning_effort: low",
                "",
            ]
        ),
        encoding="utf-8",
    )


class RuntimeConfigTest(unittest.TestCase):
    def test_loads_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-runtime.yaml"
            write_runtime_config(path, model="model-a", effort="high", prompt="hello")

            defaults = load_codex_runtime_defaults(path)

            self.assertEqual(defaults.model, "model-a")
            self.assertEqual(defaults.model_reasoning_effort, "high")
            self.assertEqual(defaults.new_session_prompt, "hello")

    def test_rejects_missing_required_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-runtime.yaml"
            path.write_text(
                "defaults:\n  model: model-a\n  model_reasoning_effort: high\ndaily_trading: {}\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "defaults.new_session_prompt must not be empty"):
                load_codex_runtime_defaults(path)

    def test_uses_baked_default_when_bind_mounted_config_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            primary_path = tmp_path / "config" / "codex-runtime.yaml"
            baked_path = tmp_path / "default-config" / "codex-runtime.yaml"
            baked_path.parent.mkdir()
            write_runtime_config(baked_path, model="baked-model", effort="medium")

            with (
                patch("service.codex.runtime_config.DEFAULT_RUNTIME_CONFIG_PATH", primary_path),
                patch("service.codex.runtime_config.BAKED_RUNTIME_CONFIG_PATH", baked_path),
            ):
                defaults = load_codex_runtime_defaults(primary_path)

            self.assertEqual(defaults.model, "baked-model")

    def test_runner_reloads_defaults_before_each_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runtime_path = tmp_path / "codex-runtime.yaml"
            commands: list[list[str]] = []
            runner = object.__new__(CodexRunner)
            runner.config = SimpleNamespace(
                codex_runtime_config_file=runtime_path,
                codex_bin="codex",
                codex_home=tmp_path / "codex-home",
                mcp_trading_env="paper",
                workspace_dir=tmp_path,
                bypass_sandbox=False,
                codex_timeout_seconds=10,
            )
            runner.tmp_dir = tmp_path / "tmp"
            runner.tmp_dir.mkdir()
            runner._read_usage_snapshot = lambda: None
            runner._append_token_usage_summary = lambda output, *_args: output
            runner._daily_trading_artifact_exists = lambda _context: False
            runner._session_ids = lambda: []
            runner._detect_new_session_id = lambda _before: "new-session-id"

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                commands.append(cmd)
                output_path = Path(cmd[cmd.index("-o") + 1])
                output_path.write_text("ok", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("service.codex.runner.subprocess.run", side_effect=fake_run):
                write_runtime_config(runtime_path, model="model-a", effort="low")
                runner.run_resume("session-id", "first")
                write_runtime_config(runtime_path, model="model-b", effort="xhigh")
                runner.run_resume("session-id", "second")
                write_runtime_config(runtime_path, model="model-c", effort="medium", prompt="updated prompt")
                runner.run_new_session()

            self.assertEqual(commands[0][commands[0].index("-m") + 1], "model-a")
            self.assertIn('model_reasoning_effort="low"', commands[0])
            self.assertEqual(commands[1][commands[1].index("-m") + 1], "model-b")
            self.assertIn('model_reasoning_effort="xhigh"', commands[1])
            self.assertEqual(commands[2][commands[2].index("-m") + 1], "model-c")
            self.assertIn('model_reasoning_effort="medium"', commands[2])
            self.assertTrue(commands[2][-1].startswith("updated prompt"))


if __name__ == "__main__":
    unittest.main()
