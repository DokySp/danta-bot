from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ..runner import CodexRunner
from ..runtime_config import load_codex_runtime_defaults, update_codex_runtime_reasoning_effort


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

    def test_updates_only_defaults_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-runtime.yaml"
            path.write_text(
                "\n".join(
                    [
                        "defaults:",
                        "  model: model-a",
                        "  model_reasoning_effort: medium  # general default",
                        "  new_session_prompt: hello",
                        "daily_trading:",
                        "  collection:",
                        "    model: collection-model",
                        "    model_reasoning_effort: low",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = update_codex_runtime_reasoning_effort(path, "max")

            self.assertTrue(result.changed)
            self.assertEqual(result.previous_value, "medium")
            self.assertEqual(result.current_value, "max")
            updated = path.read_text(encoding="utf-8")
            self.assertIn('  model_reasoning_effort: "max"  # general default', updated)
            self.assertIn("    model_reasoning_effort: low", updated)

    def test_accepts_model_defined_reasoning_effort_and_serializes_it_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-runtime.yaml"
            write_runtime_config(path, model="model-a", effort="medium")

            ultra_result = update_codex_runtime_reasoning_effort(path, "ultra")
            future_result = update_codex_runtime_reasoning_effort(path, 'future"effort')

            self.assertEqual(ultra_result.current_value, "ultra")
            self.assertEqual(future_result.current_value, 'future"effort')
            self.assertEqual(
                load_codex_runtime_defaults(path).model_reasoning_effort,
                'future"effort',
            )

    def test_rejects_empty_reasoning_effort_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex-runtime.yaml"
            write_runtime_config(path, model="model-a", effort="medium")
            original = path.read_text(encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must not be empty"):
                update_codex_runtime_reasoning_effort(path, "  ")

            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_update_materializes_baked_default_when_primary_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            primary_path = tmp_path / "config" / "codex-runtime.yaml"
            primary_path.parent.mkdir()
            baked_path = tmp_path / "default-config" / "codex-runtime.yaml"
            baked_path.parent.mkdir()
            write_runtime_config(baked_path, model="baked-model", effort="medium")

            with (
                patch("service.codex.runtime_config.DEFAULT_RUNTIME_CONFIG_PATH", primary_path),
                patch("service.codex.runtime_config.BAKED_RUNTIME_CONFIG_PATH", baked_path),
            ):
                result = update_codex_runtime_reasoning_effort(primary_path, "high")

            self.assertTrue(result.changed)
            self.assertTrue(primary_path.exists())
            self.assertEqual(load_codex_runtime_defaults(primary_path).model_reasoning_effort, "high")

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
                update_codex_runtime_reasoning_effort(runtime_path, 'future"effort')
                runner.run_resume("session-id", "third")
                write_runtime_config(runtime_path, model="model-c", effort="medium", prompt="updated prompt")
                runner.run_new_session()

            self.assertEqual(commands[0][commands[0].index("-m") + 1], "model-a")
            self.assertIn('model_reasoning_effort="low"', commands[0])
            self.assertEqual(commands[1][commands[1].index("-m") + 1], "model-b")
            self.assertIn('model_reasoning_effort="xhigh"', commands[1])
            self.assertEqual(commands[2][commands[2].index("-m") + 1], "model-b")
            self.assertIn('model_reasoning_effort="future\\"effort"', commands[2])
            self.assertEqual(commands[3][commands[3].index("-m") + 1], "model-c")
            self.assertIn('model_reasoning_effort="medium"', commands[3])
            self.assertTrue(commands[3][-1].startswith("updated prompt"))

    def test_runner_terminates_options_before_dash_prefixed_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runtime_path = tmp_path / "codex-runtime.yaml"
            write_runtime_config(runtime_path, model="model-a", effort="medium")
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

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                commands.append(cmd)
                output_path = Path(cmd[cmd.index("-o") + 1])
                output_path.write_text("ok", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("service.codex.runner.subprocess.run", side_effect=fake_run):
                runner.run_resume("session-id", "- list item")
                runner.run_resume("session-id", "--looks-like-an-option")

            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[0][-2], "--")
            self.assertTrue(commands[0][-1].startswith("- list item"))
            self.assertEqual(commands[1][-2], "--")
            self.assertTrue(commands[1][-1].startswith("--looks-like-an-option"))

    def test_daily_trading_main_records_model_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runtime_path = tmp_path / "codex-runtime.yaml"
            write_runtime_config(runtime_path, model="default-model", effort="medium")
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
            runner._daily_trading_artifact_exists = lambda candidate: candidate.run_id == "main-fallback-test"
            runner._append_holding_history_if_available = lambda _context: None

            context = SimpleNamespace(
                run_id="main-model-test",
                started_at="2026-07-14T09:00:00+09:00",
                started_at_display="2026-07-14 09:00:00 KST",
            )
            fallback_context = SimpleNamespace(
                run_id="main-fallback-test",
                started_at="2026-07-14T09:05:00+09:00",
                started_at_display="2026-07-14 09:05:00 KST",
            )

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                output_path = Path(cmd[cmd.index("-o") + 1])
                output_path.write_text("ok", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("service.codex.runner.new_codex_run_context", side_effect=[context, fallback_context]),
                patch("service.codex.runner.subprocess.run", side_effect=fake_run),
                patch("service.codex.runner.refresh_daily_trading_token_artifacts"),
                patch("service.codex.runner.daily_trading_telegram_summary", return_value=None),
            ):
                runner.run_once(
                    "daily trading",
                    daily_trading_hint=True,
                    model="main-model",
                    reasoning_effort="high",
                )
                runner.run_once(
                    "fallback daily trading",
                    model="fallback-model",
                    reasoning_effort="medium",
                )

            model_usage_path = tmp_path / "reports" / "runs" / context.run_id / "model-usage.jsonl"
            entries = [json.loads(line) for line in model_usage_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["source"], "daily-trading-main")
            self.assertEqual(entries[0]["stage"], "main")
            self.assertEqual(entries[0]["model"], "main-model")
            self.assertEqual(entries[0]["model_reasoning_effort"], "high")
            fallback_model_usage_path = (
                tmp_path / "reports" / "runs" / fallback_context.run_id / "model-usage.jsonl"
            )
            fallback_entries = [
                json.loads(line) for line in fallback_model_usage_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(fallback_entries), 1)
            self.assertEqual(fallback_entries[0]["source"], "daily-trading-main")
            self.assertEqual(fallback_entries[0]["model"], "fallback-model")
            self.assertEqual(fallback_entries[0]["model_reasoning_effort"], "medium")


if __name__ == "__main__":
    unittest.main()
