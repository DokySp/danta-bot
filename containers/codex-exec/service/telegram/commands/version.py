import html
import os
from pathlib import Path
from typing import Any


VERSION_FILE = Path("/app/VERSION")


def app_version() -> str:
    try:
        image_version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        image_version = ""
    return image_version or os.getenv("APP_VERSION", "").strip() or "unknown"


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
