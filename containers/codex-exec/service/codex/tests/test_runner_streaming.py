from __future__ import annotations

import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ..runner import CodexRunCancelled, CodexRunner, _ActiveCodexRun


def write_runtime_config(path: Path) -> None:
    path.write_text(
        "defaults:\n"
        "  model: test-model\n"
        "  model_reasoning_effort: medium\n"
        "  new_session_prompt: new session\n"
        "daily_trading: {}\n",
        encoding="utf-8",
    )


def write_executable(path: Path, source: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def make_runner(tmp_path: Path, codex_bin: Path, timeout: float = 5) -> CodexRunner:
    runtime_path = tmp_path / "codex-runtime.yaml"
    write_runtime_config(runtime_path)
    runner = object.__new__(CodexRunner)
    runner.config = SimpleNamespace(
        codex_runtime_config_file=runtime_path,
        codex_bin=str(codex_bin),
        codex_home=tmp_path / "codex-home",
        mcp_trading_env="paper",
        workspace_dir=tmp_path,
        bypass_sandbox=False,
        codex_timeout_seconds=timeout,
    )
    runner.tmp_dir = tmp_path / "tmp"
    runner.tmp_dir.mkdir()
    runner._active_telegram_lock = threading.Lock()
    runner._active_telegram_run = None
    runner._read_usage_snapshot = lambda: None
    runner._append_token_usage_summary = lambda output, *_args: output
    runner._daily_trading_artifact_exists = lambda _context: False
    return runner


class CodexRunnerStreamingTest(unittest.TestCase):
    def test_stop_during_launch_is_applied_when_process_attaches(self) -> None:
        active_run = _ActiveCodexRun()
        process = Mock()
        process.poll.return_value = None

        self.assertTrue(active_run.cancel())
        with patch("service.codex.runner._signal_process_group") as signal_process:
            with patch("service.codex.runner._schedule_forced_kill") as force_kill:
                active_run.attach_process(process)

        signal_process.assert_called_once()
        force_kill.assert_called_once_with(process)

    def test_stop_does_not_reclassify_run_after_stdout_closed(self) -> None:
        process = Mock()
        process.poll.return_value = None
        active_run = _ActiveCodexRun(process)

        active_run.mark_stdout_closed()

        self.assertFalse(active_run.cancel())
        self.assertEqual(active_run.state(), "completed")
        process.send_signal.assert_not_called()

    def test_streams_intermediate_message_and_returns_final_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            executable = tmp_path / "fake-codex"
            write_executable(
                executable,
                """import json
import pathlib
import sys

output_path = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'progress'}}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '<b>final</b>'}}), flush=True)
output_path.write_text('<b>final</b>', encoding='utf-8')
""",
            )
            runner = make_runner(tmp_path, executable)
            updates: list[str] = []

            output = runner.run_resume("session-id", "hello", on_progress=updates.append)

            self.assertEqual(output, "<b>final</b>")
            self.assertEqual(updates, ["progress"])
            self.assertFalse(runner.has_active_telegram_run())

    def test_cancel_stops_only_registered_streaming_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            executable = tmp_path / "fake-codex"
            write_executable(
                executable,
                """import json
import time

print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'started'}}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'still working'}}), flush=True)
time.sleep(60)
""",
            )
            runner = make_runner(tmp_path, executable)
            progress_seen = threading.Event()
            outcome: list[BaseException | str] = []

            def run() -> None:
                try:
                    outcome.append(
                        runner.run_resume(
                            "session-id",
                            "hello",
                            on_progress=lambda _text: progress_seen.set(),
                        )
                    )
                except BaseException as exc:  # test captures the worker outcome
                    outcome.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(progress_seen.wait(timeout=5))
            self.assertTrue(runner.has_active_telegram_run())

            self.assertTrue(runner.cancel_active_telegram_run())
            thread.join(timeout=10)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], CodexRunCancelled)
            self.assertFalse(runner.has_active_telegram_run())
            self.assertFalse(runner.cancel_active_telegram_run())

    def test_streaming_timeout_preserves_timeout_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            executable = tmp_path / "fake-codex"
            write_executable(executable, "import time\ntime.sleep(60)\n")
            runner = make_runner(tmp_path, executable, timeout=0.1)

            with self.assertRaises(subprocess.TimeoutExpired):
                runner.run_resume("session-id", "hello", on_progress=lambda _text: None)


if __name__ == "__main__":
    unittest.main()
