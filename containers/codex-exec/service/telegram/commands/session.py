import html
from typing import Any


def handle_session(worker: Any, task: Any, args: str) -> None:
    if args:
        worker.gateway.send_message(
            "사용법: <code>/session</code>",
            task.chat_id,
            task.route,
        )
        return

    defaults = worker.runner.runtime_defaults()
    session_id = worker.state.get_default_session()
    status = "🏃‍♂️ 실행 중" if worker.runner.has_active_telegram_run() else "대기 중"
    queued = worker.queue.qsize()
    session_text = html.escape(session_id) if session_id else "없음"
    worker.gateway.send_message(
        (
            "<b>현재 Codex 세션</b>\n"
            f"상태: <code>{status}</code>\n"
            f"세션: <code>{session_text}</code>\n"
            f"모델: <code>{html.escape(defaults.model)}</code>\n"
            f"effort: <code>{html.escape(defaults.model_reasoning_effort)}</code>\n"
            f"작업 경로: <code>{html.escape(str(worker.config.workspace_dir))}</code>\n"
            f"거래 환경: <code>{html.escape(worker.config.mcp_trading_env)}</code>\n"
            f"대기 요청: <code>{queued}</code>"
        ),
        task.chat_id,
        task.route,
    )
