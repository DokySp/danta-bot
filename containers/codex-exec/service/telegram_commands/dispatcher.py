import html
from collections.abc import Callable
from typing import Any

from .new_usage import handle_new, handle_usage
from .kospi_touch_status import handle_kospi_touch_status
from .portfolio import handle_add_portfolio_ticker, handle_remove_portfolio_ticker

TelegramCommandHandler = Callable[[Any, Any, str], None]


def handle_telegram_command(worker: Any, task: Any, command: str, args: str) -> None:
    handlers: dict[str, TelegramCommandHandler] = {
        "new": handle_new,
        "usage": handle_usage,
        "kospi_touch_status": handle_kospi_touch_status,
        "add_portfolio_ticker": handle_add_portfolio_ticker,
        "remove_portfolio_ticker": handle_remove_portfolio_ticker,
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
