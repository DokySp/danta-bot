import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any


TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

QUOTA_WINDOW_SPECS = {
    "5h": ("primary", 300),
    "weekly": ("secondary", 10080),
}


def zero_token_usage() -> dict[str, int]:
    return {field: 0 for field in TOKEN_USAGE_FIELDS}


def token_usage_from(raw: Any) -> dict[str, int]:
    usage = zero_token_usage()
    if not isinstance(raw, dict):
        return usage
    for field in TOKEN_USAGE_FIELDS:
        value = raw.get(field)
        if isinstance(value, bool):
            continue
        try:
            usage[field] = int(value)
        except (TypeError, ValueError):
            usage[field] = 0
    if usage["total_tokens"] <= 0:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def add_token_usage(total: dict[str, int], usage: dict[str, int]) -> None:
    for field in TOKEN_USAGE_FIELDS:
        total[field] = int(total.get(field, 0)) + int(usage.get(field, 0))


def read_usage_snapshot(config: Any) -> dict[str, Any] | None:
    if not config.usage_script.exists():
        logging.warning("codex usage script not found path=%s", config.usage_script)
        return None

    cmd = [
        str(config.usage_script),
        "--json",
        "--timeout",
        str(config.usage_timeout_seconds),
    ]
    env = os.environ.copy()
    env["CODEX_HOME"] = str(config.codex_home)
    env["CODEX_BIN"] = config.codex_bin

    try:
        result = subprocess.run(
            cmd,
            cwd=config.workspace_dir,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=config.usage_timeout_seconds + 5,
            check=False,
        )
    except Exception:
        logging.exception("failed to query codex usage snapshot")
        return None
    if result.returncode != 0:
        logging.warning("codex usage snapshot failed stderr=%s", (result.stderr or "").strip()[-1000:])
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        logging.warning("codex usage snapshot returned invalid JSON")
        return None


def append_token_usage_summary(
    output: str,
    workspace_dir: Path,
    context: Any,
    event_summary: dict[str, Any],
    usage_before: dict[str, Any] | None,
    usage_after: dict[str, Any] | None,
) -> str:
    if "총 사용 토큰:" in output:
        if "5h:" in output and "weekly:" in output:
            return output
        summary = "\n".join(
            [
                f"<b>5h: {format_percent_delta(usage_before, usage_after, '5h')}</b>",
                f"<b>weekly: {format_percent_delta(usage_before, usage_after, 'weekly')}</b>",
            ]
        )
        return f"{output.rstrip()}\n\n{summary}"

    main_usage = token_usage_from(event_summary.get("token_usage"))
    subagent_usage, subagent_has_usage = subagent_token_usage(workspace_dir, context)
    total_usage = zero_token_usage()
    add_token_usage(total_usage, main_usage)
    add_token_usage(total_usage, subagent_usage)
    has_usage = bool(event_summary.get("token_usage_event_count")) or subagent_has_usage

    summary = "\n".join(
        [
            f"<b>총 사용 토큰: {format_token_count(total_usage['total_tokens'], has_usage)}</b>",
            f"<b>5h: {format_percent_delta(usage_before, usage_after, '5h')}</b>",
            f"<b>weekly: {format_percent_delta(usage_before, usage_after, 'weekly')}</b>",
        ]
    )
    return f"{output.rstrip()}\n\n{summary}"


def subagent_token_usage(workspace_dir: Path, context: Any) -> tuple[dict[str, int], bool]:
    total = zero_token_usage()
    has_usage = False
    subagent_dir = workspace_dir / "reports" / "runs" / context.run_id / "subagents"
    if not subagent_dir.is_dir():
        return total, False
    for path in sorted(subagent_dir.glob("*.wrapper.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            logging.warning("failed to read subagent token wrapper path=%s", path)
            continue
        if not isinstance(payload, dict):
            continue
        usage = token_usage_from(payload.get("token_usage"))
        if int(usage.get("total_tokens", 0)) > 0 or payload.get("token_usage_event_count"):
            has_usage = True
        add_token_usage(total, usage)
    return total, has_usage


def parse_percent(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().removesuffix("%").strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_window_duration(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def usage_window(snapshot: Any, period: str) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    limits = snapshot.get("rateLimits")
    if not isinstance(limits, dict):
        return None
    spec = QUOTA_WINDOW_SPECS.get(period)
    if spec is None:
        return None
    fallback_key, expected_duration = spec

    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        if parse_window_duration(window.get("windowDurationMins")) == expected_duration:
            return window

    fallback = limits.get(fallback_key)
    if not isinstance(fallback, dict):
        return None
    fallback_duration = parse_window_duration(fallback.get("windowDurationMins"))
    if fallback_duration is None:
        return fallback
    return None


def used_percent(snapshot: Any, period: str) -> float | None:
    window = usage_window(snapshot, period)
    if not window:
        return None
    return parse_percent(window.get("usedPercent"))


def format_percent_delta(before: Any, after: Any, period: str) -> str:
    before_used = used_percent(before, period)
    after_used = used_percent(after, period)
    if before_used is None or after_used is None or after_used < before_used:
        return "n/a"
    delta = max(0.0, after_used - before_used)
    if delta.is_integer():
        return f"{int(delta)}%"
    return f"{delta:.1f}".rstrip("0").rstrip(".") + "%"


def format_token_count(total_tokens: int, has_usage: bool) -> str:
    if not has_usage:
        return "n/a"
    return f"{total_tokens:,}"
