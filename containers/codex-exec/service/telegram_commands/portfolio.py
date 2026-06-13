from typing import Any

from ..portfolio_commands import PortfolioCommandError, add_portfolio_ticker, remove_portfolio_ticker


def handle_add_portfolio_ticker(worker: Any, task: Any, args: str) -> None:
    try:
        result = add_portfolio_ticker(worker.config.portfolio_file, args)
    except PortfolioCommandError as exc:
        worker.gateway.send_message(exc.html_message, task.chat_id, task.route)
        return
    worker.gateway.send_message(result.html_message, task.chat_id, task.route)


def handle_remove_portfolio_ticker(worker: Any, task: Any, args: str) -> None:
    try:
        result = remove_portfolio_ticker(worker.config.portfolio_file, args)
    except PortfolioCommandError as exc:
        worker.gateway.send_message(exc.html_message, task.chat_id, task.route)
        return
    worker.gateway.send_message(result.html_message, task.chat_id, task.route)
