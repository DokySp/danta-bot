from __future__ import annotations

import runpy
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ..scheduler import Scheduler
from ..config import parse_yaml_schedule
from ...pipelines.daily_trading.launcher import validate_daily_trading_config
from ...trading.daily_trading_direct import DailyTradingDirectResult


class SchedulerDailyTradingFailureClassificationTest(unittest.TestCase):
    def scheduler(self) -> Scheduler:
        scheduler = Scheduler.__new__(Scheduler)
        scheduler.config = SimpleNamespace(telegram_typing_interval_seconds=1.0)
        scheduler.runner = Mock()
        scheduler.daily_trading_direct_runner = Mock()
        scheduler.market_news_direct_runner = Mock()
        scheduler.gateway = Mock()
        return scheduler

    @patch("service.scheduler.scheduler.TypingIndicator", return_value=nullcontext())
    def test_delivery_failure_is_not_reported_as_runner_failure(self, _typing: Mock) -> None:
        scheduler = self.scheduler()
        scheduler.daily_trading_direct_runner.run.return_value = DailyTradingDirectResult(
            output="completed output",
            html_report_path=Path("/tmp/daily-trading-report.html"),
        )
        scheduler.gateway.send_message.side_effect = [RuntimeError("gateway unavailable"), None]

        scheduler._run_job("daily-1", "", None, None, None, None, {})

        self.assertEqual(scheduler.gateway.send_message.call_count, 2)
        fallback = scheduler.gateway.send_message.call_args_list[1].args[0]
        self.assertIn("거래 실행 성공 / Telegram 전송 실패", fallback)
        self.assertNotIn("direct runner failed", fallback)

    @patch("service.scheduler.scheduler.TypingIndicator", return_value=nullcontext())
    def test_success_sends_short_text_then_html_report(self, _typing: Mock) -> None:
        scheduler = self.scheduler()
        report_path = Path("/tmp/reports/runs/run-20260715/daily-trading-report.html")
        scheduler.daily_trading_direct_runner.run.return_value = DailyTradingDirectResult(
            output="completed output",
            html_report_path=report_path,
        )

        scheduler._run_job("daily-1", "", "chat", "route", None, None, {})

        scheduler.gateway.send_message.assert_called_once_with("completed output", "chat", "route")
        scheduler.gateway.send_document.assert_called_once_with(
            report_path,
            "<b>daily-trading 당일 누적 상세 리포트</b>",
            "chat",
            "route",
            filename="daily-trading-report-run-20260715.html",
        )

    @patch("service.scheduler.scheduler.TypingIndicator", return_value=nullcontext())
    def test_html_delivery_failure_is_reported_after_successful_trade(self, _typing: Mock) -> None:
        scheduler = self.scheduler()
        scheduler.daily_trading_direct_runner.run.return_value = DailyTradingDirectResult(
            output="completed output",
            html_report_path=Path("/tmp/daily-trading-report.html"),
        )
        scheduler.gateway.send_document.side_effect = RuntimeError("document unavailable")

        scheduler._run_job("daily-1", "", None, None, None, None, {})

        self.assertEqual(scheduler.gateway.send_message.call_count, 2)
        fallback = scheduler.gateway.send_message.call_args_list[1].args[0]
        self.assertIn("거래 실행 성공 / Telegram 전송 실패", fallback)
        self.assertNotIn("direct runner failed", fallback)

    @patch("service.scheduler.scheduler.TypingIndicator", return_value=nullcontext())
    def test_runner_failure_keeps_runner_failure_message(self, _typing: Mock) -> None:
        scheduler = self.scheduler()
        scheduler.daily_trading_direct_runner.run.side_effect = RuntimeError("pipeline failed")

        scheduler._run_job("daily-1", "", None, None, None, None, {})

        scheduler.gateway.send_message.assert_called_once()
        fallback = scheduler.gateway.send_message.call_args.args[0]
        self.assertIn("daily-trading direct runner failed", fallback)
        self.assertNotIn("거래 실행 성공", fallback)

    def test_market_news_success_is_log_only(self) -> None:
        scheduler = self.scheduler()

        scheduler._run_job("market-news", "", None, None, None, None, None, {})

        scheduler.market_news_direct_runner.run.assert_called_once_with({})
        scheduler.runner.run_once.assert_not_called()
        scheduler.gateway.send_message.assert_not_called()

    def test_market_news_failure_sends_collection_error(self) -> None:
        scheduler = self.scheduler()
        scheduler.market_news_direct_runner.run.side_effect = RuntimeError("domestic source failed")

        scheduler._run_job("market-news", "", "chat", "route", None, None, None, {})

        scheduler.gateway.send_message.assert_called_once()
        message = scheduler.gateway.send_message.call_args.args[0]
        self.assertIn("market-news direct runner failed", message)
        self.assertIn("domestic source failed", message)

    def test_base_schedule_declares_deterministic_market_news_job(self) -> None:
        codex_exec_root = Path(__file__).resolve().parents[3]
        schedules = parse_yaml_schedule(codex_exec_root / "profiles" / "base" / "config" / "schedules.yaml")
        market_job = next(item for item in schedules if item.get("id") == "market-news")

        self.assertEqual(market_job.get("cron"), "*/15 * * * *")
        self.assertEqual(market_job.get("market_news"), {})
        self.assertFalse(str(market_job.get("message") or "").strip())

    def test_base_daily_trading_schedules_use_valid_runtime_config(self) -> None:
        codex_exec_root = Path(__file__).resolve().parents[3]
        schedules = parse_yaml_schedule(codex_exec_root / "profiles" / "base" / "config" / "schedules.yaml")
        daily_jobs = [item for item in schedules if isinstance(item.get("daily_trading"), dict)]

        self.assertTrue(daily_jobs)
        for job in daily_jobs:
            with self.subTest(schedule_id=job.get("id")):
                validate_daily_trading_config(job["daily_trading"])

    def test_base_daily_trading_schedules_use_intended_order_path_policy(self) -> None:
        codex_exec_root = Path(__file__).resolve().parents[3]
        schedules = parse_yaml_schedule(codex_exec_root / "profiles" / "base" / "config" / "schedules.yaml")
        paths = {
            str(item.get("id")): str((item.get("daily_trading") or {}).get("order_path"))
            for item in schedules
            if isinstance(item.get("daily_trading"), dict)
        }

        for schedule_id in {"daily-01", "daily-02", "daily-10", "daily-20", "daily-21", "daily-30", "daily-41", "daily-42", "daily-43"}:
            with self.subTest(schedule_id=schedule_id):
                self.assertEqual(paths.get(schedule_id), "auto")
        self.assertEqual(paths.get("daily-00"), "reservation")
        self.assertEqual(paths.get("weekend-01"), "reservation")

    def test_schedule_toggle_never_reenables_unmanaged_daily_run(self) -> None:
        codex_exec_root = Path(__file__).resolve().parents[3]
        schedule_path = codex_exec_root / "profiles" / "base" / "config" / "schedules.yaml"
        script_path = codex_exec_root / "shared-skills" / "trading-schedule-toggle" / "scripts" / "toggle_daily_schedules.py"
        module = runpy.run_path(str(script_path))
        toggle = module["toggle"]
        lines = schedule_path.read_text(encoding="utf-8").splitlines(keepends=True)

        toggle(lines, False, None)
        enabled_result = toggle(lines, True, None)
        daily_00 = next(block for block in module["find_blocks"](lines) if block.schedule_id == "daily-00")

        self.assertFalse(module["current_enabled"](lines, daily_00)[1])
        self.assertIn("daily-00", enabled_result["unchanged"])
        self.assertIn("daily-01", enabled_result["changed"])


if __name__ == "__main__":
    unittest.main()
