import html
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class ScheduleCommandError(RuntimeError):
    def __init__(self, log_message: str, html_message: str) -> None:
        super().__init__(log_message)
        self.html_message = html_message


@dataclass(frozen=True)
class ScheduleCommandResult:
    html_message: str


def toggle_daily_schedules(
    skills_dir: Path,
    schedule_file: Path,
    state: str,
    args: str,
) -> ScheduleCommandResult:
    if args.strip():
        raise ScheduleCommandError(
            "invalid schedule toggle arguments",
            f"사용법: <code>/schedule_{html.escape(state)}</code>",
        )

    script = skills_dir / "trading-schedule-toggle" / "scripts" / "toggle_daily_schedules.py"
    if not script.exists():
        raise ScheduleCommandError(
            f"schedule toggle script not found: {script}",
            f"스케줄 토글 스크립트를 찾을 수 없습니다.\n<code>{html.escape(str(script))}</code>",
        )

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--file",
            str(schedule_file),
            "--state",
            state,
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    payload = parse_toggle_output(result.stdout)
    if result.returncode != 0:
        message = str(payload.get("error") or result.stderr or result.stdout or "unknown error").strip()
        raise ScheduleCommandError(
            f"schedule toggle failed returncode={result.returncode}: {message}",
            f"스케줄 변경에 실패했습니다.\n<pre>{html.escape(message)}</pre>",
        )

    return ScheduleCommandResult(format_toggle_result(payload))


def parse_toggle_output(stdout: str) -> dict[str, object]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ScheduleCommandError(
            "schedule toggle returned invalid JSON",
            f"스케줄 토글 결과를 해석할 수 없습니다.\n<pre>{html.escape(stdout.strip())}</pre>",
        ) from exc
    if not isinstance(payload, dict):
        raise ScheduleCommandError(
            "schedule toggle returned non-object JSON",
            "스케줄 토글 결과 형식이 올바르지 않습니다.",
        )
    return payload


def format_toggle_result(payload: dict[str, object]) -> str:
    state = str(payload.get("state", ""))
    title = "일일 거래 스케줄 활성화" if state == "on" else "일일 거래 스케줄 비활성화"
    return (
        f"<b>{title}</b>\n"
        f"파일: <code>{html.escape(str(payload.get('file', '')))}</code>\n"
        f"변경: <code>{format_list(payload.get('changed'))}</code>\n"
        f"유지: <code>{format_list(payload.get('unchanged'))}</code>\n"
        f"누락: <code>{format_list(payload.get('missing'))}</code>"
    )


def format_list(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "-"
    return html.escape(", ".join(str(item) for item in value))
