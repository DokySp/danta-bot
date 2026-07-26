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
        report_path = Path("/tmp/reports/runs/run-20260715/daily-trading-report.html")
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
            filename="daily-trading-report-run-20260715.html",
        )

    @patch("service.telegram.worker.load_execute_trade_config", return_value={"env": "acct"})
    @patch("service.telegram.worker.TypingIndicator", return_value=nullcontext())
    def test_execute_trade_is_routed_as_manual_invocation(self, _typing: Mock, _load_config: Mock) -> None:
        """$execute-trade must stay force-full: it always routes as manual,
        never scheduled, so the broker-preflight gate can never skip it.
        """
        worker = self.worker()
        worker.daily_trading_direct_runner.run.return_value = DailyTradingDirectResult(
            output="short summary", html_report_path=None
        )

        worker._handle(TelegramTask(chat_id="chat", route="route", text="$execute-trade"))

        worker.daily_trading_direct_runner.run.assert_called_once_with(
            {"env": "acct"}, invocation_type="manual"
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


class TelegramWorkerCodexProgressTest(unittest.TestCase):
    @patch("service.telegram.worker.TypingIndicator", return_value=nullcontext())
    def test_regular_message_passes_progress_callback_and_sends_final_normally(
        self,
        _typing: Mock,
    ) -> None:
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.config = SimpleNamespace(telegram_typing_interval_seconds=1.0)
        worker.state = Mock()
        worker.state.get_default_session.return_value = "session-id"
        worker.runner = Mock()
        worker.gateway = Mock()
        reporter = Mock()
        reporter_context = Mock()
        reporter_context.__enter__ = Mock(return_value=reporter)
        reporter_context.__exit__ = Mock(return_value=False)
        worker.progress_reporter = Mock(return_value=reporter_context)

        def run_resume(_session: str, _text: str, *, on_progress: object) -> str:
            self.assertIs(on_progress, reporter.update)
            return "final result"

        worker.runner.run_resume.side_effect = run_resume

        worker._handle(TelegramTask(chat_id="chat", route="route", text="hello"))

        worker.gateway.send_message.assert_called_once_with("final result", "chat", "route")
        worker.gateway.send_message_draft.assert_not_called()

    def test_immediate_stop_is_dispatched_without_queue(self) -> None:
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.runner = Mock()
        worker.runner.cancel_active_telegram_run.return_value = True
        worker.gateway = Mock()

        handled = worker.handle_immediate(
            TelegramTask(chat_id="chat", route="route", text="/stop")
        )

        self.assertTrue(handled)
        worker.runner.cancel_active_telegram_run.assert_called_once_with()
        worker.gateway.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
