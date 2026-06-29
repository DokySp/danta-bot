import json
import subprocess
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import UserFacingError


def parse_show_holding_history_command(text: str) -> int | None:
    parts = text.strip().split()
    if not parts or parts[0] != "$show-holding-history":
        return None
    if len(parts) == 1:
        return 7
    if len(parts) != 2:
        raise UserFacingError(
            "invalid show-holding-history arguments",
            "사용법: <code>$show-holding-history</code> 또는 <code>$show-holding-history 7</code>",
        )
    try:
        days = int(parts[1])
    except ValueError as exc:
        raise UserFacingError(
            "invalid show-holding-history days",
            "일수는 숫자로 입력해주세요. 예: <code>$show-holding-history 7</code>",
        ) from exc
    if days <= 0:
        raise UserFacingError(
            "invalid show-holding-history days",
            "일수는 1 이상의 숫자로 입력해주세요.",
        )
    return days


def render_holding_history(config: Config, days: int) -> dict[str, Any]:
    script = (
        config.workspace_dir
        / "containers"
        / "codex-exec"
        / "profiles"
        / "base"
        / "skills"
        / "show-holding-history"
        / "scripts"
        / "render_holding_history.py"
    )
    if not script.exists():
        script = config.codex_home / "skills" / "show-holding-history" / "scripts" / "render_holding_history.py"
    if not script.exists():
        raise RuntimeError(f"show-holding-history renderer not found: {script}")
    result = subprocess.run(
        ["python3", str(script), "--days", str(days)],
        cwd=config.workspace_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"show-holding-history failed: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout)
