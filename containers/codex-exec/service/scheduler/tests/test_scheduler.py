from __future__ import annotations

import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ..scheduler import Scheduler
from ..config import parse_yaml_schedule
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

        scheduler._run_job("market-news", "", None, None, None, None, None, {"config_file": "/tmp/market-news.yaml"})

        scheduler.market_news_direct_runner.run.assert_called_once_with({"config_file": "/tmp/market-news.yaml"})
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

    def test_market_news_rate_limited_alert_sends_one_warning_message(self) -> None:
        scheduler = self.scheduler()
        scheduler.market_news_direct_runner.run.return_value = {
            "status": "skipped_rate_limited",
            "alert": True,
            "provider": "gdelt_doc_2",
            "rate_limited_until": "2026-07-19T13:00:00+00:00",
        }

        scheduler._run_job("market-news", "", "chat", "route", None, None, None, {})

        scheduler.gateway.send_message.assert_called_once()
        message = scheduler.gateway.send_message.call_args.args[0]
        self.assertIn("rate limited", message)
        self.assertNotIn("direct runner failed", message)

    def test_market_news_rate_limited_alert_delivery_failure_is_not_reported_as_runner_failure(self) -> None:
        scheduler = self.scheduler()
        scheduler.market_news_direct_runner.run.return_value = {
            "status": "skipped_rate_limited",
            "alert": True,
            "provider": "gdelt_doc_2",
            "rate_limited_until": "2026-07-19T13:00:00+00:00",
        }
        scheduler.gateway.send_message.side_effect = [RuntimeError("gateway unavailable"), None]

        scheduler._run_job("market-news", "", "chat", "route", None, None, None, {})

        self.assertEqual(scheduler.gateway.send_message.call_count, 2)
        fallback = scheduler.gateway.send_message.call_args_list[1].args[0]
        self.assertNotIn("direct runner failed", fallback)

    def test_market_news_rate_limited_double_delivery_failure_is_not_reported_as_runner_failure(self) -> None:
        scheduler = self.scheduler()
        scheduler.market_news_direct_runner.run.return_value = {
            "status": "skipped_rate_limited",
            "alert": True,
            "provider": "gdelt_doc_2",
            "rate_limited_until": "2026-07-19T13:00:00+00:00",
        }
        scheduler.gateway.send_message.side_effect = [
            RuntimeError("warning unavailable"),
            RuntimeError("fallback unavailable"),
            None,
        ]

        scheduler._run_job("market-news", "", "chat", "route", None, None, None, {})

        self.assertEqual(scheduler.gateway.send_message.call_count, 2)
        for call in scheduler.gateway.send_message.call_args_list:
            self.assertNotIn("direct runner failed", call.args[0])

    def test_market_news_rate_limited_skip_without_alert_is_silent(self) -> None:
        scheduler = self.scheduler()
        scheduler.market_news_direct_runner.run.return_value = {
            "status": "skipped_rate_limited",
            "alert": False,
            "provider": "gdelt_doc_2",
            "rate_limited_until": "2026-07-19T13:00:00+00:00",
        }

        scheduler._run_job("market-news", "", "chat", "route", None, None, None, {})

        scheduler.gateway.send_message.assert_not_called()

    def test_base_schedule_declares_deterministic_market_news_job(self) -> None:
        codex_exec_root = Path(__file__).resolve().parents[3]
        schedules = parse_yaml_schedule(codex_exec_root / "profiles" / "base" / "config" / "schedules.yaml")
        market_job = next(item for item in schedules if item.get("id") == "market-news")

        self.assertEqual(market_job.get("cron"), "*/15 * * * *")
        self.assertEqual((market_job.get("market_news") or {}).get("config_file"), "/app/config/market-news.yaml")
        self.assertFalse(str(market_job.get("message") or "").strip())


if __name__ == "__main__":
    unittest.main()
