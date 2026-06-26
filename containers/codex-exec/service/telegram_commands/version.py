import html
import os
from typing import Any


def app_version() -> str:
    return os.getenv("APP_VERSION", "").strip() or "unknown"


def handle_version(worker: Any, task: Any, args: str) -> None:
    if args:
        worker.gateway.send_message(
            "사용법: <code>/version</code>\n버전 확인 명령은 메시지를 함께 받지 않습니다.",
            task.chat_id,
            task.route,
        )
        return

    worker.gateway.send_message(
        f"<b>codex-exec</b>\nversion: <code>{html.escape(app_version())}</code>",
        task.chat_id,
        task.route,
    )
