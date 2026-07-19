from typing import Any


def handle_stop(worker: Any, task: Any, args: str) -> None:
    if args:
        worker.gateway.send_message(
            "사용법: <code>/stop</code>",
            task.chat_id,
            task.route,
        )
        return

    if worker.runner.cancel_active_telegram_run():
        message = "<b>현재 Codex 작업을 중지했습니다.</b>"
    else:
        message = "<i>현재 중지할 Telegram Codex 작업이 없습니다.</i>"
    worker.gateway.send_message(message, task.chat_id, task.route)
