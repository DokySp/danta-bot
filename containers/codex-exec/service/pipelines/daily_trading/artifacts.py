import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ...codex.events import token_count_payload, turn_completed_usage
from ...codex.usage import zero_token_usage


def daily_trading_artifact_exists(workspace_dir: Path, context: Any) -> bool:
    return daily_trading_run_dir(workspace_dir, context).joinpath("run.json").is_file()


def daily_trading_run_dir(workspace_dir: Path, context: Any) -> Path:
    return workspace_dir / "reports" / "runs" / context.run_id


def daily_trading_telegram_summary(workspace_dir: Path, context: Any) -> str | None:
    path = daily_trading_run_dir(workspace_dir, context) / "telegram-summary.txt"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        logging.exception("failed to read daily-trading telegram summary run_id=%s", context.run_id)
        return None
    return text or None


def refresh_daily_trading_token_artifacts(workspace_dir: Path, context: Any, stdout: str) -> None:
    run_dir = daily_trading_run_dir(workspace_dir, context)
    if not run_dir.is_dir():
        return
    artifact_script = daily_trading_artifact_script(workspace_dir)
    if artifact_script is None:
        logging.warning("daily-trading artifact helper not found; cannot refresh token-summary run_id=%s", context.run_id)
        return
    main_events = write_daily_trading_main_events(run_dir, stdout)
    if main_events is None:
        return

    cmd = [
        sys.executable,
        str(artifact_script),
        "token-summary",
        "--run-dir",
        str(run_dir),
        "--main-events",
        str(main_events),
    ]
    result = subprocess.run(
        cmd,
        cwd=workspace_dir,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        logging.warning("daily-trading token-summary refresh failed stderr=%s", (result.stderr or "").strip()[-1000:])
        return
    token_summary_path = run_dir / "token-summary.json"
    pipeline_summary_path = run_dir / "pipeline-summary.json"
    try:
        token_summary = json.loads(token_summary_path.read_text(encoding="utf-8"))
        pipeline_summary = json.loads(pipeline_summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.warning("daily-trading token artifact refresh could not read summaries run_id=%s", context.run_id)
        return
    pipeline_summary["token_usage"] = {
        "main": (token_summary.get("main") or {}).get("token_usage", zero_token_usage()),
        "subagents": (token_summary.get("subagents") or {}).get("token_usage", zero_token_usage()),
        "total": (token_summary.get("total") or {}).get("token_usage", zero_token_usage()),
    }
    tmp = pipeline_summary_path.with_suffix(pipeline_summary_path.suffix + ".tmp")
    tmp.write_text(json.dumps(pipeline_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(pipeline_summary_path)
    renderer = daily_trading_telegram_renderer(workspace_dir)
    if renderer is None:
        logging.warning("daily-trading telegram renderer not found; cannot refresh telegram summary run_id=%s", context.run_id)
        return
    result = subprocess.run(
        [
            sys.executable,
            str(renderer),
            "--summary",
            str(pipeline_summary_path),
            "--output",
            str(run_dir / "telegram-summary.txt"),
        ],
        cwd=workspace_dir,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        logging.warning("daily-trading telegram summary refresh failed stderr=%s", (result.stderr or "").strip()[-1000:])


def daily_trading_artifact_script(workspace_dir: Path) -> Path | None:
    candidates = [
        workspace_dir / "containers/codex-exec/service/pipelines/daily_trading/scripts/build_run_artifacts.py",
        Path("/app/service/pipelines/daily_trading/scripts/build_run_artifacts.py"),
    ]
    return first_existing(candidates)


def daily_trading_telegram_renderer(workspace_dir: Path) -> Path | None:
    candidates = [
        workspace_dir / "containers/codex-exec/service/pipelines/daily_trading/scripts/render_telegram_summary.py",
        Path("/app/service/pipelines/daily_trading/scripts/render_telegram_summary.py"),
    ]
    return first_existing(candidates)


def first_existing(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def write_daily_trading_main_events(run_dir: Path, stdout: str) -> Path | None:
    lines: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if token_count_payload(item) is not None or turn_completed_usage(item) is not None:
            lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    if not lines:
        return None
    path = run_dir / "main-events.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

