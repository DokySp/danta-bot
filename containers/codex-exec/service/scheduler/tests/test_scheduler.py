from __future__ import annotations

import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ..scheduler import Scheduler


class SchedulerDailyTradingFailureClassificationTest(unittest.TestCase):
    def scheduler(self) -> Scheduler:
        scheduler = Scheduler.__new__(Scheduler)
        scheduler.config = SimpleNamespace(telegram_typing_interval_seconds=1.0)
        scheduler.runner = Mock()
        scheduler.daily_trading_direct_runner = Mock()
        scheduler.gateway = Mock()
        return scheduler

    @patch("service.scheduler.scheduler.TypingIndicator", return_value=nullcontext())
    def test_delivery_failure_is_not_reported_as_runner_failure(self, _typing: Mock) -> None:
        scheduler = self.scheduler()
        scheduler.daily_trading_direct_runner.run.return_value = "completed output"
        scheduler.gateway.send_message.side_effect = [RuntimeError("gateway unavailable"), None]

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


if __name__ == "__main__":
    unittest.main()
