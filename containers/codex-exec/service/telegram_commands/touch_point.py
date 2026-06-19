from pathlib import Path
from typing import Any

from ..telegram_gateway import TypingIndicator
from ..touch_point import (
    TouchPointCommandError,
    parse_show_touch_point_args,
    render_touch_point,
    touch_point_caption,
)


def handle_show_touch_point(worker: Any, task: Any, args: str) -> None:
    try:
        request = parse_show_touch_point_args(args)
    except TouchPointCommandError as exc:
        worker.gateway.send_message(exc.html_message, task.chat_id, task.route)
        return

    with TypingIndicator(
        worker.gateway,
        task.chat_id,
        task.route,
        worker.config.telegram_typing_interval_seconds,
    ):
        summary = render_touch_point(worker.config, request)

    image_path = Path(str(summary["image_path"]))
    worker.gateway.send_photo(image_path, touch_point_caption(summary), task.chat_id, task.route)
