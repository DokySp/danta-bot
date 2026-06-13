import html
import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .daily_trading import error_message_with_run_context
from .errors import UserFacingError
from .holding_history import parse_show_holding_history_command, render_holding_history
from .runner import CodexRunner
from .state import StateStore
from .telegram_commands.core import parse_telegram_command
from .telegram_commands.dispatcher import handle_telegram_command
from .telegram_gateway import TelegramGateway, TypingIndicator


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
