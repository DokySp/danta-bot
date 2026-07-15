from __future__ import annotations

import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ...trading.daily_trading_direct import DailyTradingDirectResult
from ..worker import TelegramTask, TelegramWorker


class TelegramWorkerDailyTradingAttachmentTest(unittest.TestCase):
    def worker(self) -> TelegramWorker:
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.config = SimpleNamespace(telegram_typing_interval_seconds=1.0)
        worker.daily_trading_direct_runner = Mock()
        worker.gateway = Mock()
        return worker

    @patch("service.telegram.worker.load_execute_trade_config", return_value={})
    @patch("service.telegram.worker.TypingIndicator", return_value=nullcontext())
    def test_execute_trade_sends_summary_then_html(self, _typing: Mock, _load_config: Mock) -> None:
        worker = self.worker()
        report_path = Path("/tmp/daily-trading-report.html")
        worker.daily_trading_direct_runner.run.return_value = DailyTradingDirectResult(
            output="short summary",
            html_report_path=report_path,
        )

        worker._handle(TelegramTask(chat_id="chat", route="route", text="$execute-trade"))

        worker.gateway.send_message.assert_called_once_with("short summary", "chat", "route")
        worker.gateway.send_document.assert_called_once_with(
            report_path,
            "<b>daily-trading 당일 누적 상세 리포트</b>",
            "chat",
            "route",
        )

    @patch("service.telegram.worker.load_execute_trade_config", return_value={})
    @patch("service.telegram.worker.TypingIndicator", return_value=nullcontext())
    def test_document_failure_is_classified_as_delivery_failure(self, _typing: Mock, _load_config: Mock) -> None:
        worker = self.worker()
        worker.daily_trading_direct_runner.run.return_value = DailyTradingDirectResult(
            output="short summary",
            html_report_path=Path("/tmp/daily-trading-report.html"),
        )
        worker.gateway.send_document.side_effect = RuntimeError("document unavailable")

        worker._handle(TelegramTask(chat_id="chat", route="route", text="$execute-trade"))

        self.assertEqual(worker.gateway.send_message.call_count, 2)
        fallback = worker.gateway.send_message.call_args_list[1].args[0]
        self.assertIn("거래 실행 성공 / Telegram 전송 실패", fallback)
        self.assertNotIn("알 수 없는 에러", fallback)


if __name__ == "__main__":
    unittest.main()
