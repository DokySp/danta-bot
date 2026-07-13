import html
import logging
from typing import Any

from ...codex.runtime_config import (
    load_codex_runtime_defaults,
    update_codex_runtime_reasoning_effort,
)


def handle_reasoning_effort(worker: Any, task: Any, args: str) -> None:
    if not args:
        try:
            current = load_codex_runtime_defaults(
                worker.config.codex_runtime_config_file
            ).model_reasoning_effort
        except (OSError, ValueError) as exc:
            _send_error(worker, task, exc)
            return
        worker.gateway.send_message(
            (
                "<b>기본 reasoning effort</b>\n"
                f"현재: <code>{html.escape(current)}</code>\n"
                "변경: <code>/reasoning_effort &lt;값&gt;</code>\n"
                "예: <code>low, xhigh, max, ultra</code>\n"
                "값은 그대로 저장되며 지원 여부는 선택된 모델이 판단합니다."
            ),
            task.chat_id,
            task.route,
        )
        return

    values = args.split()
    if len(values) != 1:
        worker.gateway.send_message(
            "사용법: <code>/reasoning_effort &lt;값&gt;</code>",
            task.chat_id,
            task.route,
        )
        return

    try:
        result = update_codex_runtime_reasoning_effort(
            worker.config.codex_runtime_config_file,
            values[0],
        )
    except (OSError, ValueError) as exc:
        _send_error(worker, task, exc)
        return

    title = "기본 reasoning effort 변경" if result.changed else "기본 reasoning effort 유지"
    worker.gateway.send_message(
        (
            f"<b>{title}</b>\n"
            f"이전: <code>{html.escape(result.previous_value)}</code>\n"
            f"현재: <code>{html.escape(result.current_value)}</code>\n"
            "범위: <code>defaults.model_reasoning_effort</code>\n"
            "적용: 다음 일반 Codex 실행부터"
        ),
        task.chat_id,
        task.route,
    )


def _send_error(worker: Any, task: Any, exc: Exception) -> None:
    logging.warning("reasoning effort command failed: %s", exc)
    worker.gateway.send_message(
        f"reasoning effort 처리에 실패했습니다.\n<pre>{html.escape(str(exc))}</pre>",
        task.chat_id,
        task.route,
    )
