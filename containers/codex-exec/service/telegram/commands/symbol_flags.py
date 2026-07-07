from typing import Any

from ...trading.symbol_flags import SymbolFlagsCommandError, render_symbol_flags


def handle_show_symbol_flags(worker: Any, task: Any, args: str) -> None:
    try:
        result = render_symbol_flags(worker.config.workspace_dir, args)
    except SymbolFlagsCommandError as exc:
        worker.gateway.send_message(exc.html_message, task.chat_id, task.route)
        return
    worker.gateway.send_message(result.html_message, task.chat_id, task.route)
