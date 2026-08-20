from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ...pipelines.daily_trading.scripts.account_performance import (
    parse_session_date,
    record_external_flow,
)


KST = ZoneInfo("Asia/Seoul")


def _session_date(args: str):
    values = args.split()
    if len(values) > 1:
        raise ValueError
    session_date = parse_session_date(values[0]) if values else datetime.now(KST).date()
    if session_date > datetime.now(KST).date():
        raise ValueError
    return session_date


def _handle(worker: Any, task: Any, args: str, action: str) -> None:
    try:
        session_date = _session_date(args)
    except ValueError:
        command = "external_flow" if action == "exclude" else "external_flow_clear"
        worker.gateway.send_message(
            f"사용법: <code>/{command} [YYYY-MM-DD]</code>\n오늘 또는 과거 날짜 한 개만 입력해주세요.",
            task.chat_id,
            task.route,
        )
        return
    record_external_flow(worker.config.workspace_dir, session_date, action)
    if action == "exclude":
        message = (
            f"<b>{session_date.isoformat()} 외부 입출금 예외를 기록했습니다.</b>\n"
            "해당 날짜는 국내 매매계좌 성과 계산에서 제외됩니다."
        )
    else:
        message = (
            f"<b>{session_date.isoformat()} 외부 입출금 예외를 해제했습니다.</b>\n"
            "기존 장부는 보존하고 정정 기록을 추가했습니다."
        )
    worker.gateway.send_message(message, task.chat_id, task.route)


def handle_external_flow(worker: Any, task: Any, args: str) -> None:
    _handle(worker, task, args, "exclude")


def handle_external_flow_clear(worker: Any, task: Any, args: str) -> None:
    _handle(worker, task, args, "clear")
