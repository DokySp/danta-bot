import html
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TouchPointRequest:
    trigger_id: str


class TouchPointCommandError(RuntimeError):
    def __init__(self, log_message: str, html_message: str) -> None:
        super().__init__(log_message)
        self.html_message = html_message


def parse_show_touch_point_args(args: str, command: str = "show-touch-point") -> TouchPointRequest:
    parts = args.strip().split()
    if len(parts) != 1:
        raise TouchPointCommandError(
            "invalid show-touch-point arguments",
            f"사용법: <code>/{html.escape(command)} kospi-case-1</code>",
        )
    trigger_id = parts[0].strip()
    if not trigger_id:
        raise TouchPointCommandError(
            "missing show-touch-point trigger id",
            f"사용법: <code>/{html.escape(command)} kospi-case-1</code>",
        )
    return TouchPointRequest(trigger_id=trigger_id)


def render_touch_point(config: Any, request: TouchPointRequest) -> dict[str, Any]:
    script = config.codex_home / "skills" / "show-touch-point" / "scripts" / "render_touch_point.py"
    if not script.exists():
        raise RuntimeError(f"show-touch-point renderer not found: {script}")

    cmd = [
        "python3",
        str(script),
        request.trigger_id,
        "--config",
        str(config.price_trigger_file),
        "--state-dir",
        str(config.state_dir),
    ]
    result = subprocess.run(
        cmd,
        cwd=config.workspace_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        message = (result.stderr.strip() or result.stdout.strip() or "unknown error")[-1600:]
        raise RuntimeError(f"show-touch-point failed: {message}")
    return json.loads(result.stdout)


def touch_point_caption(summary: dict[str, Any]) -> str:
    warnings = summary.get("warnings") if isinstance(summary.get("warnings"), list) else []
    warning_text = ""
    if warnings:
        warning_text = "\n주의: <code>" + html.escape(str(warnings[0])[:900]) + "</code>"
    return (
        f"<b>{html.escape(str(summary.get('case_title') or summary.get('trigger_id') or 'show-touch-point'))}</b>\n"
        f"대상: <code>{html.escape(str(summary.get('name') or ''))}</code>\n"
        f"범위: <code>{html.escape(str(summary.get('data_start') or ''))}</code> ~ "
        f"<code>{html.escape(str(summary.get('data_end') or ''))}</code>\n"
        f"터치: <code>{int(summary.get('touch_count') or 0)}건</code> / "
        f"시계열: <code>{int(summary.get('series_count') or 0)}건</code>"
        f"{warning_text}"
    )
