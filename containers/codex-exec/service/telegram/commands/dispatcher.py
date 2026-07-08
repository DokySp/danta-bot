import html
from collections.abc import Callable
from typing import Any

from .new_usage import handle_new, handle_usage
from .portfolio import (
    handle_add_portfolio_except_ticker,
    handle_add_portfolio_ticker,
    handle_remove_portfolio_except_ticker,
    handle_remove_portfolio_ticker,
)
from .schedule import handle_schedule_off, handle_schedule_on
from .touch_point import handle_show_touch_point
from .version import handle_version

TelegramCommandHandler = Callable[[Any, Any, str], None]


def handle_telegram_command(worker: Any, task: Any, command: str, args: str) -> None:
    handlers: dict[str, TelegramCommandHandler] = {
        "new": handle_new,
        "usage": handle_usage,
        "version": handle_version,
        "show-touch-point": handle_show_touch_point,
        "show_touch_point": handle_show_touch_point,
        "add_portfolio_ticker": handle_add_portfolio_ticker,
        "remove_portfolio_ticker": handle_remove_portfolio_ticker,
        "add_portfolio_except_ticker": handle_add_portfolio_except_ticker,
        "remove_portfolio_except_ticker": handle_remove_portfolio_except_ticker,
        "schedule_on": handle_schedule_on,
        "schedule_off": handle_schedule_off,
    }
    handler = handlers.get(command)
    if handler is None:
        worker.gateway.send_message(
            f"알 수 없는 명령입니다: <code>/{html.escape(command)}</code>",
            task.chat_id,
            task.route,
        )
        return
    handler(worker, task, args)
