import html
import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..codex.runner import CodexRunner
from ..config import Config
from ..errors import UserFacingError
from ..state import StateStore
from ..trading.daily_trading import error_message_with_run_context
from ..trading.daily_trading_direct import (
    DailyTradingDirectRunner,
    format_direct_delivery_error,
    is_execute_trade_direct_request,
    load_execute_trade_config,
)
from ..trading.holding_history import parse_show_holding_history_command, render_holding_history
from .commands.core import parse_telegram_command
from .commands.dispatcher import handle_telegram_command
from .gateway import TelegramGateway, TypingIndicator


@dataclass(frozen=True)
class TelegramTask:
    chat_id: str | None
    text: str
    route: str | None = None
    message_id: Any = None


class TelegramWorker:
    def __init__(self, config: Config, state: StateStore, runner: CodexRunner, gateway: TelegramGateway) -> None:
        self.config = config
        self.state = state
        self.runner = runner
        self.daily_trading_direct_runner = DailyTradingDirectRunner(config, runner)
        self.gateway = gateway
        self.queue: queue.Queue[TelegramTask] = queue.Queue()
        self.thread = threading.Thread(target=self._work, name="telegram-worker", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def submit(self, task: TelegramTask) -> None:
        self.queue.put(task)

    def _work(self) -> None:
        while True:
            task = self.queue.get()
            try:
                self._handle(task)
            except Exception as exc:  # noqa: BLE001 - report task failures to Telegram
                if isinstance(exc, UserFacingError):
                    logging.warning("telegram task failed: %s", exc)
                else:
                    logging.exception("telegram task failed")
                self.gateway.send_message(self._error_message(exc), task.chat_id, task.route)
            finally:
                self.queue.task_done()

    def _handle(self, task: TelegramTask) -> None:
        text = task.text.strip()
        logging.info("handling telegram task message_id=%s text=%r", task.message_id, text)

        if text == "$daily-trading":
            self.gateway.send_message(
                (
                    "<b>$daily-trading은 더 이상 Codex skill entrypoint로 실행되지 않습니다.</b>\n"
                    "<code>$execute-trade</code> 또는 스케줄의 <code>daily_trading</code> 설정을 사용해주세요."
                ),
                task.chat_id,
                task.route,
            )
            return

        if is_execute_trade_direct_request(text):
            with TypingIndicator(
                self.gateway,
                task.chat_id,
                task.route,
                self.config.telegram_typing_interval_seconds,
            ):
                result = self.daily_trading_direct_runner.run(load_execute_trade_config(self.config))
            try:
                self.gateway.send_message(result.output, task.chat_id, task.route)
                if result.html_report_path is not None:
                    self.gateway.send_document(
                        result.html_report_path,
                        "<b>daily-trading 당일 누적 상세 리포트</b>",
                        task.chat_id,
                        task.route,
                    )
            except Exception as exc:  # noqa: BLE001 - trade succeeded; classify delivery separately
                logging.exception("daily-trading Telegram delivery failed command=$execute-trade")
                self.gateway.send_message(
                    format_direct_delivery_error("$execute-trade", exc),
                    task.chat_id,
                    task.route,
                )
            return

        holding_history_days = parse_show_holding_history_command(text)
        if holding_history_days is not None:
            summary = render_holding_history(self.config, holding_history_days)
            row_count = int(summary.get("row_count", 0))
            caption = (
                f"<b>보유수량 변경 이력</b>\n"
                f"<code>{holding_history_days}일</code> / <code>{row_count}건</code>"
            )
            image_paths = [Path(str(path)) for path in summary.get("image_paths", [])]
            if not image_paths:
                image_paths = [Path(str(summary["image_path"]))]
            csv_path = Path(str(summary["csv_path"]))
            for index, image_path in enumerate(image_paths, start=1):
                photo_caption = caption
                if len(image_paths) > 1:
                    photo_caption = f"{caption}\n<code>{index}/{len(image_paths)}</code>"
                self.gateway.send_photo(image_path, photo_caption, task.chat_id, task.route)
            if csv_path.exists():
                self.gateway.send_document(csv_path, "원본 CSV", task.chat_id, task.route)
            return

        command = parse_telegram_command(text)
        if command is not None:
            with TypingIndicator(
                self.gateway,
                task.chat_id,
                task.route,
                self.config.telegram_typing_interval_seconds,
            ):
                handle_telegram_command(self, task, *command)
            return

        session_id = self.state.get_default_session()
        if not session_id:
            self.gateway.send_message(
                "기본 Codex 세션이 없습니다.\n먼저 <code>/new</code>로 새 세션을 시작해주세요.",
                task.chat_id,
                task.route,
            )
            return

        with TypingIndicator(
            self.gateway,
            task.chat_id,
            task.route,
            self.config.telegram_typing_interval_seconds,
        ):
            output = self.runner.run_resume(session_id, text)
        self.gateway.send_message(output, task.chat_id, task.route)

    @staticmethod
    def _error_message(exc: Exception) -> str:
        fallback = f"<b>알 수 없는 에러가 발생했습니다.</b>\n<pre>{html.escape(str(exc))}</pre>"
        return error_message_with_run_context(exc, fallback)
