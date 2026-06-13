import logging
from typing import Any

from ..telegram_gateway import TypingIndicator


def handle_new(worker: Any, task: Any, args: str) -> None:
    if args:
        worker.gateway.send_message(
            "사용법: <code>/new</code>\n새 세션 생성 명령은 메시지를 함께 받지 않습니다.",
            task.chat_id,
            task.route,
        )
        return
    with TypingIndicator(
        worker.gateway,
        task.chat_id,
        task.route,
        worker.config.telegram_typing_interval_seconds,
    ):
        session_id, output = worker.runner.run_new_session(worker.config.new_session_prompt)
    worker.state.set_default_session(session_id)
    logging.info("new default session_id=%s", session_id)
    worker.gateway.send_message(output, task.chat_id, task.route)


def handle_usage(worker: Any, task: Any, args: str) -> None:
    if args:
        worker.gateway.send_message(
            "사용법: <code>/usage</code>\n사용량 확인 명령은 메시지를 함께 받지 않습니다.",
            task.chat_id,
            task.route,
        )
        return
    with TypingIndicator(
        worker.gateway,
        task.chat_id,
        task.route,
        worker.config.telegram_typing_interval_seconds,
    ):
        output = worker.runner.run_usage()
    worker.gateway.send_message(output, task.chat_id, task.route)
