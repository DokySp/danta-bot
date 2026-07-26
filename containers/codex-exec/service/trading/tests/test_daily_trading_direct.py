"""Behavior tests for DailyTradingDirectRunner's subprocess/artifact/retry/error boundary.

The only faked seam is `subprocess.run` (the actual external process
invocation); command construction, artifact reading, holding-history
bookkeeping, and error-context attachment all run for real against a
temporary workspace so these tests catch regressions in the runner's own
orchestration logic, not just its mocks.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from ..daily_trading_direct import DailyTradingDirectRunner, _DIRECT_RUN_LOCK_RELATIVE_PATH


def make_config(workspace_dir: Path) -> Mock:
    config = Mock()
    config.workspace_dir = workspace_dir
    config.codex_timeout_seconds = 30
    config.usage_script = workspace_dir / "no-such-usage-script"
    config.usage_timeout_seconds = 5
    config.codex_home = workspace_dir / "codex-home"
    config.codex_bin = "codex"
    config.deferred_buy_retry_config_file = workspace_dir / "no-such-deferred-buy-retry.yaml"
    return config


def stub_pipeline_script(workspace_dir: Path) -> None:
    script_path = (
        workspace_dir
        / "containers"
        / "codex-exec"
        / "service"
        / "pipelines"
        / "daily_trading"
        / "scripts"
        / "run_daily_trading_pipeline.py"
    )
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("# stub, never executed: subprocess.run is patched in tests\n")


def output_dir_from_cmd(cmd: list[str]) -> Path:
    return Path(cmd[cmd.index("--output-dir") + 1])


class DailyTradingDirectRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace_dir = Path(self._tmp.name)
        stub_pipeline_script(self.workspace_dir)
        self.config = make_config(self.workspace_dir)
        self.runner = DailyTradingDirectRunner(self.config, codex_runner=Mock())

    def raw_config(self, **overrides) -> dict:
        base = {"env": "acct", "request_type": "analysis", "order_path": "auto"}
        base.update(overrides)
        return base

    def test_successful_run_reads_telegram_summary_and_reports_html_path(self) -> None:
        def fake_subprocess_run(cmd, **kwargs):
            output_dir = output_dir_from_cmd(cmd)
            (output_dir / "telegram-summary.txt").write_text("오늘의 매매 요약")
            (output_dir / "daily-trading-report.html").write_text("<html></html>")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        with patch("service.trading.daily_trading_direct.subprocess.run", side_effect=fake_subprocess_run) as run_mock:
            result = self.runner.run(self.raw_config())

        run_mock.assert_called_once()
        self.assertIn("오늘의 매매 요약", result.output)
        self.assertIsNotNone(result.html_report_path)
        self.assertTrue(result.html_report_path.is_file())
        self.assertIn("작업 시작:", result.output)

    def test_scheduled_invocation_type_is_forwarded_to_the_pipeline_command(self) -> None:
        """The cost reduction is disabled if the scheduler ever forgets to
        pass invocation_type="scheduled" -- assert the exact flag reaches the
        constructed pipeline command, not just that run() succeeds.
        """
        def fake_subprocess_run(cmd, **kwargs):
            output_dir = output_dir_from_cmd(cmd)
            (output_dir / "telegram-summary.txt").write_text("요약")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        with patch("service.trading.daily_trading_direct.subprocess.run", side_effect=fake_subprocess_run) as run_mock:
            self.runner.run(self.raw_config(), invocation_type="scheduled")

        cmd = run_mock.call_args.args[0]
        self.assertIn("--invocation-type", cmd)
        self.assertEqual(cmd[cmd.index("--invocation-type") + 1], "scheduled")

    def test_manual_invocation_type_is_forwarded_to_the_pipeline_command(self) -> None:
        """$execute-trade must stay force-full: assert the exact "manual" flag
        reaches the constructed pipeline command.
        """
        def fake_subprocess_run(cmd, **kwargs):
            output_dir = output_dir_from_cmd(cmd)
            (output_dir / "telegram-summary.txt").write_text("요약")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        with patch("service.trading.daily_trading_direct.subprocess.run", side_effect=fake_subprocess_run) as run_mock:
            self.runner.run(self.raw_config(), invocation_type="manual")

        cmd = run_mock.call_args.args[0]
        self.assertIn("--invocation-type", cmd)
        self.assertEqual(cmd[cmd.index("--invocation-type") + 1], "manual")

    def test_default_invocation_type_is_manual_when_caller_omits_it(self) -> None:
        def fake_subprocess_run(cmd, **kwargs):
            output_dir = output_dir_from_cmd(cmd)
            (output_dir / "telegram-summary.txt").write_text("요약")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        with patch("service.trading.daily_trading_direct.subprocess.run", side_effect=fake_subprocess_run) as run_mock:
            self.runner.run(self.raw_config())

        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--invocation-type") + 1], "manual")

    def test_successful_run_without_html_report_returns_none_path(self) -> None:
        def fake_subprocess_run(cmd, **kwargs):
            output_dir = output_dir_from_cmd(cmd)
            (output_dir / "telegram-summary.txt").write_text("요약")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        with patch("service.trading.daily_trading_direct.subprocess.run", side_effect=fake_subprocess_run):
            result = self.runner.run(self.raw_config())

        self.assertIsNone(result.html_report_path)

    def test_nonzero_returncode_raises_with_stderr_and_attaches_run_context(self) -> None:
        def fake_subprocess_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="pipeline exploded")

        with patch("service.trading.daily_trading_direct.subprocess.run", side_effect=fake_subprocess_run):
            with self.assertRaises(RuntimeError) as ctx:
                self.runner.run(self.raw_config())

        self.assertIn("pipeline exploded", str(ctx.exception))
        self.assertTrue(hasattr(ctx.exception, "daily_trading_run_context"))

    def test_subprocess_timeout_reraises_and_attaches_run_context(self) -> None:
        def fake_subprocess_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

        with patch("service.trading.daily_trading_direct.subprocess.run", side_effect=fake_subprocess_run):
            with self.assertRaises(subprocess.TimeoutExpired) as ctx:
                self.runner.run(self.raw_config())

        self.assertTrue(hasattr(ctx.exception, "daily_trading_run_context"))

    def test_missing_telegram_summary_raises_even_on_success_returncode(self) -> None:
        def fake_subprocess_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        with patch("service.trading.daily_trading_direct.subprocess.run", side_effect=fake_subprocess_run):
            with self.assertRaisesRegex(RuntimeError, "telegram summary not found"):
                self.runner.run(self.raw_config())

    def test_empty_telegram_summary_raises(self) -> None:
        def fake_subprocess_run(cmd, **kwargs):
            output_dir = output_dir_from_cmd(cmd)
            (output_dir / "telegram-summary.txt").write_text("   \n")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        with patch("service.trading.daily_trading_direct.subprocess.run", side_effect=fake_subprocess_run):
            with self.assertRaisesRegex(RuntimeError, "telegram summary is empty"):
                self.runner.run(self.raw_config())

    def test_failed_run_still_appends_holding_history_without_raising_secondary_error(self) -> None:
        def fake_subprocess_run(cmd, **kwargs):
            output_dir = output_dir_from_cmd(cmd)
            (output_dir / "execution.json").write_text('{"data": {"orders": []}}')
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="boom")

        with patch("service.trading.daily_trading_direct.subprocess.run", side_effect=fake_subprocess_run):
            with self.assertRaises(RuntimeError):
                self.runner.run(self.raw_config())

    def test_invalid_raw_config_is_rejected_before_any_subprocess_call(self) -> None:
        with patch("service.trading.daily_trading_direct.subprocess.run") as run_mock:
            with self.assertRaises(ValueError):
                self.runner.run(self.raw_config(env="not-a-real-env"))

        run_mock.assert_not_called()

    def test_enabled_retry_config_enqueues_deferred_buy_and_appends_output_suffix(self) -> None:
        self.config.deferred_buy_retry_config_file.write_text(
            "deferred_buy_retry:\n"
            "  enabled: true\n"
            "  delay_seconds: 120\n"
            "  expires_after_seconds: 300\n"
            "  slippage_bps: 25\n"
        )
        no_such_portfolio_except = self.workspace_dir / "no-such-portfolio-except.txt"

        def fake_subprocess_run(cmd, **kwargs):
            output_dir = output_dir_from_cmd(cmd)
            (output_dir / "telegram-summary.txt").write_text("오늘의 매매 요약")
            (output_dir / "execution.json").write_text(
                json.dumps(
                    {
                        "execution_environment": "acct",
                        "orders": [
                            {
                                "direction": "sell",
                                "result": "submitted",
                                "symbol_id": "000660",
                            },
                            {
                                "direction": "buy",
                                "result": "adjusted",
                                "reason": "buy_quantity_exceeds_order_available_quantity",
                                "symbol_id": "005930",
                                "symbol_name": "삼성전자",
                                "requested_order_quantity": 10,
                                "validated_order_quantity": 4,
                                "final_holding_quantity": 100,
                                "order_price": 70000,
                            },
                        ],
                    }
                )
            )
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        before = datetime.now(timezone.utc)
        with patch("service.trading.daily_trading_direct.subprocess.run", side_effect=fake_subprocess_run), patch.dict(
            "os.environ", {"PORTFOLIO_EXCEPT_FILE": str(no_such_portfolio_except)}
        ):
            result = self.runner.run(self.raw_config(), chat_id="12345", route="v2")
        after = datetime.now(timezone.utc)

        self.assertIn("후속 매수 재시도: 1건 예약됨", result.output)
        self.assertIn("작업 시작:", result.output)
        self.assertLess(
            result.output.index("후속 매수 재시도"),
            result.output.index("작업 시작:"),
        )

        queue_dir = self.workspace_dir / "memory" / "deferred-buy-retry"
        artifacts = list(queue_dir.glob("*--005930.json"))
        self.assertEqual(len(artifacts), 1)
        payload = json.loads(artifacts[0].read_text())

        self.assertEqual(payload["symbol_id"], "005930")
        self.assertEqual(payload["chat_id"], "12345")
        self.assertEqual(payload["route"], "v2")
        self.assertEqual(payload["retry_quantity"], 6)
        self.assertEqual(payload["target_holding_quantity"], 100)
        self.assertEqual(payload["original_order_price"], 70000)
        self.assertEqual(payload["slippage_bps"], 25)
        self.assertEqual(payload["max_acceptable_price"], 70200)

        created_at = datetime.fromisoformat(payload["created_at"])
        due_at = datetime.fromisoformat(payload["due_at"])
        expires_at = datetime.fromisoformat(payload["expires_at"])
        # created_at is truncated to whole seconds, so it may read up to ~1s
        # earlier than `before`.
        self.assertAlmostEqual((created_at - before).total_seconds(), 0, delta=2)
        self.assertLessEqual(created_at, after)
        self.assertAlmostEqual((due_at - created_at).total_seconds(), 120, delta=2)
        self.assertAlmostEqual((expires_at - due_at).total_seconds(), 300, delta=2)


class DailyTradingDirectRunnerSerializationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace_dir = Path(self._tmp.name)
        stub_pipeline_script(self.workspace_dir)
        self.config = make_config(self.workspace_dir)
        self.runner = DailyTradingDirectRunner(self.config, codex_runner=Mock())

    def raw_config(self, **overrides) -> dict:
        base = {"env": "acct", "request_type": "analysis", "order_path": "auto"}
        base.update(overrides)
        return base

    def test_scheduler_and_manual_runs_are_serialized_via_workspace_lock(self) -> None:
        entered_first_run = threading.Event()
        release_first_run = threading.Event()

        def blocking_subprocess_run(cmd, **kwargs):
            output_dir = output_dir_from_cmd(cmd)
            entered_first_run.set()
            release_first_run.wait(timeout=5)
            (output_dir / "telegram-summary.txt").write_text("첫 실행 요약")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        with patch("service.trading.daily_trading_direct.subprocess.run", side_effect=blocking_subprocess_run):
            first_thread = threading.Thread(
                target=self.runner.run, args=(self.raw_config(),), kwargs={"invocation_type": "scheduled"}
            )
            first_thread.start()
            self.assertTrue(entered_first_run.wait(timeout=5), "first run never reached subprocess.run")

            lock_path = self.workspace_dir / _DIRECT_RUN_LOCK_RELATIVE_PATH
            self.assertTrue(lock_path.exists())
            probe_handle = open(lock_path, "a", encoding="utf-8")
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                probe_handle.close()

            release_first_run.set()
            first_thread.join(timeout=5)
            self.assertFalse(first_thread.is_alive())

            # Once the first (scheduled) run releases the lock, a second (manual)
            # run can acquire it immediately.
            def immediate_subprocess_run(cmd, **kwargs):
                output_dir = output_dir_from_cmd(cmd)
                (output_dir / "telegram-summary.txt").write_text("두번째 실행 요약")
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

            with patch("service.trading.daily_trading_direct.subprocess.run", side_effect=immediate_subprocess_run):
                result = self.runner.run(self.raw_config(), invocation_type="manual")
            self.assertIn("두번째 실행 요약", result.output)


if __name__ == "__main__":
    unittest.main()
