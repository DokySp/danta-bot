#!/usr/bin/env python3
"""Run daily-trading as one compact deterministic pipeline command.

The pipeline keeps orchestration and large helper stdout out of the Main agent
prompt path. It writes canonical run artifacts, captures verbose command output
to a local command log, and prints only a compact summary pointer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
ANALYST_REVIEW_ROLES = (
    "analyst-quality-risk",
    "analyst-momentum-news",
)
COMMAND_OUTPUT_LIMIT = 2000
ORDER_PATH_AUTO = "auto"
REGULAR_ORDER_START_MINUTE = 9 * 60
REGULAR_ORDER_END_MINUTE = 15 * 60 + 30
RESERVATION_ORDER_START_MINUTE = 15 * 60 + 40
RESERVATION_ORDER_END_MINUTE = 7 * 60 + 30
STRATEGY_POLICY_CONFIG_ENV = "DAILY_TRADING_STRATEGY_POLICY_CONFIG"
STRATEGY_POLICY_CONFIG_FILENAME = "daily-trading-strategy-policy.yaml"
LIFECYCLE_ONLY_ORDER_REASONS = {
    "active_order_cancel_submitted",
    "active_order_correction_submitted",
    "active_order_cancel_and_replacement_submitted",
}
def now_kst() -> datetime:
    return datetime.now(KST)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_kst_datetime(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        return now_kst()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def resolve_order_path(order_path: str, started_at: str) -> tuple[str, str]:
    requested = str(order_path or ORDER_PATH_AUTO)
    if requested in {"reservation", "immediate"}:
        return requested, "explicit"
    if requested != ORDER_PATH_AUTO:
        raise ValueError(f"unsupported order_path: {requested}")

    started = parse_kst_datetime(started_at)
    if started.weekday() >= 5:
        return "reservation", "auto_closed_weekend"
    minute = started.hour * 60 + started.minute
    if REGULAR_ORDER_START_MINUTE <= minute < REGULAR_ORDER_END_MINUTE:
        return "immediate", "auto_regular_session"
    if minute >= RESERVATION_ORDER_START_MINUTE or minute < RESERVATION_ORDER_END_MINUTE:
        return "reservation", "auto_reservation_session"
    raise ValueError(
        "auto order path cannot select a supported KIS order API for "
        f"{started.isoformat(timespec='minutes')}; regular order_cash window is "
        "09:00-15:30 KST and order_resv window is 15:40-07:30 KST"
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def write_compact_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json_if_exists(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def load_yaml_if_exists(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        import yaml  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except Exception:
        return None


def resolve_workspace_path(workspace_dir: Path, path_text: str | Path) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path
    return workspace_dir / path


def strategy_policy_config_candidates(workspace_dir: Path, workspace_repo_root: Path) -> list[Path]:
    code_repo_root = repo_root_from(script_dir())
    return [
        Path("/app/config") / STRATEGY_POLICY_CONFIG_FILENAME,
        code_repo_root / "containers/codex-exec/profiles/base/config" / STRATEGY_POLICY_CONFIG_FILENAME,
        workspace_repo_root / "containers/codex-exec/profiles/base/config" / STRATEGY_POLICY_CONFIG_FILENAME,
        workspace_dir / "containers/codex-exec/profiles/base/config" / STRATEGY_POLICY_CONFIG_FILENAME,
    ]


def resolve_strategy_policy_config_path(
    workspace_dir: Path,
    workspace_repo_root: Path,
    configured: str = "",
) -> Path:
    explicit = str(configured or os.getenv(STRATEGY_POLICY_CONFIG_ENV, "")).strip()
    if explicit:
        path = resolve_workspace_path(workspace_dir, explicit)
        if not path.exists():
            raise FileNotFoundError(f"strategy policy config not found: {path}")
        return path.resolve()
    for path in strategy_policy_config_candidates(workspace_dir, workspace_repo_root):
        if path.exists():
            return path.resolve()
    searched = ", ".join(str(path) for path in strategy_policy_config_candidates(workspace_dir, workspace_repo_root))
    raise FileNotFoundError(f"default strategy policy config not found; searched: {searched}")


def repo_root_from(path: Path) -> Path:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def default_output_dir(run_id: str) -> str:
    return f"reports/runs/{run_id}"


def normalize_symbol_ids(raw: Any) -> list[str]:
    if isinstance(raw, dict):
        raw = raw.get("universe") or raw.get("symbols") or raw.get("symbol_ids") or []
    if raw is None:
        items: list[Any] = []
    elif isinstance(raw, str):
        items = raw.replace("\n", ",").split(",")
    elif isinstance(raw, list):
        items = raw
    else:
        items = [raw]
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            value = item.get("symbol_id") or item.get("symbol") or item.get("code")
        else:
            value = item
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def normalize_symbol_key(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits and digits == text:
        return digits.zfill(6)
    return text


def zero_usage() -> dict[str, int]:
    return {field: 0 for field in TOKEN_USAGE_FIELDS}


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def non_negative_int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            parsed = int(text)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def decimal_value(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def non_negative_decimal_value(value: Any) -> Decimal | None:
    parsed = decimal_value(value)
    if parsed is None or parsed < 0:
        return None
    return parsed


def positive_decimal_value(value: Any) -> Decimal | None:
    parsed = decimal_value(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def decimal_json_value(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def round_half_up_quantity(target_value: Decimal, price: Decimal) -> int:
    return int((target_value / price).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def derive_action(quantity: int, baseline_qty: int) -> str:
    """Mechanical action label from rounded quantity versus baseline: increase|hold|reduce|exit.

    Uses rounded whole-share quantities (not raw KRW values) so the action
    matches what is actually orderable.
    """
    if quantity > baseline_qty:
        return "increase"
    if quantity == baseline_qty:
        return "hold"
    if quantity <= 0:
        return "exit"
    return "reduce"


def format_number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return ""


def format_signed_number(value: Any) -> str:
    number = as_int(value)
    sign = "+" if number > 0 else ""
    return f"{sign}{number:,}"


def md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "/").replace("\n", " ").strip()


def bool_status(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def report_date_from(started_at: str) -> str:
    text = str(started_at or "").strip()
    if not text:
        return now_kst().strftime("%Y-%m-%d")
    try:
        return datetime.fromisoformat(text).astimezone(KST).strftime("%Y-%m-%d")
    except ValueError:
        match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        return match.group(0) if match else now_kst().strftime("%Y-%m-%d")


def token_usage_from(raw: Any) -> dict[str, int]:
    usage = zero_usage()
    if not isinstance(raw, dict):
        return usage
    for field in TOKEN_USAGE_FIELDS:
        usage[field] = as_int(raw.get(field))
    if usage["total_tokens"] <= 0:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def compact_text(value: str, limit: int = COMMAND_OUTPUT_LIMIT) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "unknown"


def symbol_key(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("symbol_id") or item.get("symbol") or item.get("code") or "").strip()


THESIS_CONDITION_ID_MAX_LENGTH = 64


def normalize_thesis_condition_id(value: Any) -> str:
    """Normalize a thesis invalidation condition_id.

    Unlike the generic filename safe_name(), this never falls back to a
    placeholder like "unknown": non-string input, or input with nothing left
    after normalization, returns "". Runs of characters other than Unicode
    alphanumerics, '.', '_', '-' (including whitespace) collapse to a single
    '-'; the result is lowercased, trimmed of leading/trailing separators, and
    capped at THESIS_CONDITION_ID_MAX_LENGTH characters. Korean/non-ASCII
    identifiers are preserved, while separator-only values remain unusable.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    if not text:
        return ""
    collapsed = re.sub(r"[^\w.\-]+", "-", text, flags=re.UNICODE)
    normalized = collapsed[:THESIS_CONDITION_ID_MAX_LENGTH].strip("._-")
    return normalized if any(character.isalnum() for character in normalized) else ""


def thesis_definition_is_valid(thesis: Any) -> bool:
    """True when thesis has a non-empty core_rationale and >=1 usable invalidation condition.

    An empty/malformed thesis_definition (missing rationale, no conditions, or
    conditions missing condition_id/description) never counts as a valid prior.
    core_rationale, condition_id, and description must be actual strings, not
    other JSON types coerced with str().
    """
    if not isinstance(thesis, dict):
        return False
    core_rationale = thesis.get("core_rationale")
    if not isinstance(core_rationale, str) or not core_rationale.strip():
        return False
    conditions = thesis.get("invalidation_conditions")
    if not isinstance(conditions, list) or not conditions:
        return False
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        condition_id = condition.get("condition_id")
        description = condition.get("description")
        if not isinstance(condition_id, str) or not isinstance(description, str):
            continue
        if normalize_thesis_condition_id(condition_id) and description.strip():
            return True
    return False


def cache_symbol_has_content(value: Any) -> bool:
    if isinstance(value, dict):
        if not value:
            return False
        if value.get("errors") and len(value) <= 1:
            return False
        candidates = [
            item
            for key, item in value.items()
            if key not in {"symbol_name", "errors", "sentiment", "article_date", "date"}
        ]
        return any(cache_symbol_has_content(item) for item in candidates)
    if isinstance(value, list):
        return any(cache_symbol_has_content(item) for item in value)
    text = str(value or "").strip()
    if not text:
        return False
    if "수집된 뉴스가 없습니다" in text:
        return False
    return True


def cache_symbol_keys(path: Path) -> set[str]:
    payload = load_json_if_exists(path) if path.suffix.lower() == ".json" else load_yaml_if_exists(path)
    if not isinstance(payload, dict):
        return set()
    symbols = payload.get("symbols")
    if isinstance(symbols, dict):
        return {normalize_symbol_key(key) for key, value in symbols.items() if normalize_symbol_key(key) and cache_symbol_has_content(value)}
    if isinstance(symbols, list):
        return set(normalize_symbol_ids(symbols))
    return set()


def normalized_calendar_date(value: Any) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())[:8]
    if len(digits) != 8:
        return ""
    try:
        return datetime.strptime(digits, "%Y%m%d").date().isoformat()
    except ValueError:
        return ""


def symbol_news_cache_symbol_keys(path: Path, expected_date: str) -> set[str]:
    payload = load_json_if_exists(path) if path.suffix.lower() == ".json" else load_yaml_if_exists(path)
    if not isinstance(payload, dict):
        return set()
    cache_date = normalized_calendar_date(payload.get("date"))
    if not cache_date or cache_date != normalized_calendar_date(expected_date):
        return set()
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict):
        return set()
    usable: set[str] = set()
    for key, value in symbols.items():
        symbol_id = normalize_symbol_key(key)
        if not symbol_id:
            continue
        entries = value
        if isinstance(entries, dict):
            entries = entries.get("items") or entries.get("articles") or []
        if not isinstance(entries, list):
            continue
        if any(
            isinstance(item, dict)
            and normalized_calendar_date(item.get("article_date") or item.get("date")) == cache_date
            and cache_symbol_has_content(item)
            for item in entries
        ):
            usable.add(symbol_id)
    return usable


def cache_symbol_all_keys(path: Path) -> set[str]:
    payload = load_json_if_exists(path) if path.suffix.lower() == ".json" else load_yaml_if_exists(path)
    if not isinstance(payload, dict):
        return set()
    symbols = payload.get("symbols")
    if isinstance(symbols, dict):
        return {normalize_symbol_key(key) for key in symbols if normalize_symbol_key(key)}
    if isinstance(symbols, list):
        return set(normalize_symbol_ids(symbols))
    return set()


def cache_coverage(path: Path, symbols: list[str]) -> tuple[bool, list[str]]:
    wanted = {normalize_symbol_key(symbol) for symbol in symbols if normalize_symbol_key(symbol)}
    available = cache_symbol_keys(path)
    missing = sorted(wanted - available)
    return bool(wanted) and not missing, missing


def symbol_news_cache_coverage(path: Path, symbols: list[str], expected_date: str) -> tuple[bool, list[str]]:
    wanted = {normalize_symbol_key(symbol) for symbol in symbols if normalize_symbol_key(symbol)}
    available = symbol_news_cache_symbol_keys(path, expected_date)
    missing = sorted(wanted - available)
    return bool(wanted) and not missing, missing


def cache_evidence_counts(path: Path, symbols: list[str]) -> dict[str, Any]:
    wanted = {normalize_symbol_key(symbol) for symbol in symbols if normalize_symbol_key(symbol)}
    available = cache_symbol_keys(path)
    present = cache_symbol_all_keys(path)
    usable = wanted & available
    present_wanted = wanted & present
    return {
        "wanted_symbol_count": len(wanted),
        "cache_symbol_count": len(present),
        "present_symbol_count": len(present_wanted),
        "usable_symbol_count": len(usable),
        "missing_usable_symbol_count": len(wanted - available),
        "missing_usable_symbols_sample": sorted(wanted - available)[:20],
    }


def symbol_news_cache_evidence_counts(path: Path, symbols: list[str], expected_date: str) -> dict[str, Any]:
    wanted = {normalize_symbol_key(symbol) for symbol in symbols if normalize_symbol_key(symbol)}
    available = symbol_news_cache_symbol_keys(path, expected_date)
    present = cache_symbol_all_keys(path)
    usable = wanted & available
    present_wanted = wanted & present
    return {
        "wanted_symbol_count": len(wanted),
        "cache_symbol_count": len(present),
        "present_symbol_count": len(present_wanted),
        "usable_symbol_count": len(usable),
        "missing_usable_symbol_count": len(wanted - available),
        "missing_usable_symbols_sample": sorted(wanted - available)[:20],
    }


def count_symbol_errors(payload: Any) -> int:
    symbols = payload.get("symbols") if isinstance(payload, dict) else []
    if not isinstance(symbols, list):
        return 0
    count = 0
    for item in symbols:
        if not isinstance(item, dict):
            continue
        errors = item.get("errors")
        if isinstance(errors, list) and errors:
            count += 1
    return count


def safe_stage(name: str, status: str, *, detail: str = "", required: bool = True, path: Path | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "stage": name,
        "status": status,
        "required": required,
        "detail": detail,
    }
    if path is not None:
        payload["path"] = str(path)
    return payload


def requested_execution_completed(stages: list[dict[str, Any]], execution: dict[str, Any]) -> bool:
    if execution.get("request_type") not in {"demo-submit", "real-submit"}:
        return False
    if execution.get("status") != "success":
        return False
    return any(item.get("stage") == "order-execution" and item.get("status") == "success" for item in stages)


def summarized_status(stages: list[dict[str, Any]], execution: dict[str, Any]) -> str:
    if any(item.get("required") and item.get("status") == "failed" for item in stages):
        return "failed"
    if (
        execution.get("request_type") in {"demo-submit", "real-submit"}
        and execution.get("requires_main_agent_order_execution")
        and not requested_execution_completed(stages, execution)
    ):
        return "partial"
    partial_required = [
        item
        for item in stages
        if item.get("required") and (item.get("status") == "partial" or item.get("status") == "skipped")
    ]
    if requested_execution_completed(stages, execution):
        partial_required = [item for item in partial_required if item.get("stage") != "execution-plan"]
    if partial_required:
        return "partial"
    return "success"


class Pipeline:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.workspace_dir = Path(args.workspace_dir).expanduser().resolve()
        self.repo_root = repo_root_from(self.workspace_dir)
        self.run_id = args.run_id or now_kst().strftime("daily-trading-%Y%m%d-%H%M%S-kst")
        self.started_at = args.started_at or now_kst().isoformat(timespec="seconds")
        output_text = args.output_dir or default_output_dir(self.run_id)
        self.output_dir_text = output_text
        self.output_dir = resolve_workspace_path(self.workspace_dir, output_text)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.command_log_path = self.output_dir / "pipeline-command-log.json"
        self.summary_path = self.output_dir / "pipeline-summary.json"
        self.run_path = self.output_dir / "run.json"
        self.model_usage_path = self.output_dir / "model-usage.jsonl"
        self.review_extra_instructions_path = self.resolve_optional_path(
            getattr(args, "review_extra_instructions_file", "")
        )
        self.strategy_policy_config_path = resolve_strategy_policy_config_path(
            self.workspace_dir,
            self.repo_root,
            str(getattr(args, "strategy_policy_config", "") or ""),
        )
        self.order_path_requested = str(getattr(args, "order_path", ORDER_PATH_AUTO) or ORDER_PATH_AUTO)
        try:
            self.order_path, self.order_path_reason = resolve_order_path(self.order_path_requested, self.started_at)
        except ValueError:
            if self.order_path_requested == ORDER_PATH_AUTO and (
                getattr(args, "command", "") == "summarize" or getattr(args, "request_type", "") not in {"prepare", "demo-submit", "real-submit"}
            ):
                self.order_path, self.order_path_reason = "reservation", "auto_unresolved_non_submit"
            else:
                raise
        self.logs: list[dict[str, Any]] = []
        self.stages: list[dict[str, Any]] = []

    def resolve_optional_path(self, value: str) -> Path | None:
        text = str(value or "").strip()
        if not text:
            return None
        return resolve_workspace_path(self.workspace_dir, text)

    def daily_trading_config_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        if self.review_extra_instructions_path is not None:
            summary["review_extra_instructions_path"] = str(self.review_extra_instructions_path)
            if self.review_extra_instructions_path.exists():
                summary["review_extra_instructions_sha256"] = file_sha256(self.review_extra_instructions_path)
        summary["strategy_policy_config_path"] = str(self.strategy_policy_config_path)
        summary["strategy_policy_config_sha256"] = file_sha256(self.strategy_policy_config_path)
        return summary

    def add_stage(self, name: str, status: str, *, detail: str = "", required: bool = True, path: Path | None = None) -> None:
        self.stages.append(safe_stage(name, status, detail=detail, required=required, path=path))
        self.write_run_json(status=self.pipeline_status())

    def pipeline_status(self) -> str:
        required_failed = [item for item in self.stages if item.get("required") and item.get("status") == "failed"]
        if required_failed:
            return "failed"
        partial = [
            item
            for item in self.stages
            if item.get("status") == "partial" or (item.get("required") and item.get("status") == "skipped")
        ]
        return "partial" if partial else "success"

    def write_run_json(self, *, status: str | None = None) -> None:
        write_json(
            self.run_path,
            {
                "schema_version": "1",
                "run_id": self.run_id,
                "started_at": self.started_at,
                "updated_at": now_iso(),
                "status": status or self.pipeline_status(),
                "pipeline_summary": str(self.summary_path),
                "model_usage": str(self.model_usage_path),
                "order_path_selection": {
                    "requested": self.order_path_requested,
                    "resolved": self.order_path,
                    "reason": self.order_path_reason,
                },
                "daily_trading_config": self.daily_trading_config_summary(),
                "stages": self.stages,
            },
        )

    def command_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.args.env:
            env["CODEX_MCP_TRADING_ENV"] = "paper" if self.args.env in {"paper", "demo"} else "acct"
        return env

    def run_cmd(self, stage: str, cmd: list[str], *, required: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            cmd,
            cwd=self.workspace_dir,
            env=env or self.command_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        entry = {
            "stage": stage,
            "command": cmd,
            "returncode": result.returncode,
            "stdout_tail": compact_text(result.stdout),
            "stderr_tail": compact_text(result.stderr),
            "required": required,
            "recorded_at": now_iso(),
        }
        self.logs.append(entry)
        write_json(self.command_log_path, {"commands": self.logs})
        return result

    def artifact_script(self) -> str:
        return str(script_dir() / "build_run_artifacts.py")

    def subagent_script(self) -> str:
        return str(script_dir() / "run_subagent.py")

    def market_index_snapshot_script(self) -> Path:
        configured = os.getenv("DAILY_TRADING_MARKET_INDEX_SNAPSHOT_SCRIPT", "").strip()
        if configured:
            return Path(configured)
        return script_dir().parent.parent / "market_index_snapshot" / "cli.py"

    def news_context_script(self) -> Path:
        configured = os.getenv("DAILY_TRADING_NEWS_CONTEXT_SCRIPT", "").strip()
        if configured:
            return Path(configured)
        return script_dir().parent.parent / "news_context" / "cli.py"

    def main_evidence_script(self) -> str:
        return str(script_dir() / "collect_main_evidence.py")

    def order_execution_script(self) -> str:
        return str(script_dir() / "execute_orders.py")

    def telegram_summary_script(self) -> str:
        return str(script_dir() / "render_telegram_summary.py")

    def html_report_script(self) -> str:
        return str(script_dir() / "render_html_report.py")

    def portfolio_script_candidates(self) -> list[Path]:
        return [
            self.repo_root / "containers/codex-exec/profiles/base/skills/check-portfolio/scripts/read_portfolio.sh",
            self.repo_root / "containers/codex-exec/shared-skills/check-portfolio/scripts/read_portfolio.sh",
            Path("/app/skills/check-portfolio/scripts/read_portfolio.sh"),
        ]

    def optional_cache_date(self) -> str:
        return str(getattr(self.args, "date", "") or report_date_from(self.started_at))

    def optional_cache_filename(self, domain: str) -> str:
        date = self.optional_cache_date()
        if domain == "financial":
            return f"financial-{date}.yaml"
        return f"symbol-news-{date}.yaml"

    def optional_cache_stage_name(self, domain: str) -> str:
        return "financial-cache" if domain == "financial" else "symbol-news-cache"

    def expected_cache_path(self, domain: str) -> Path:
        return self.optional_cache_candidate_paths(domain)[0]

    def optional_cache_candidate_paths(self, domain: str) -> list[Path]:
        filename = self.optional_cache_filename(domain)
        paths: list[Path] = []
        if domain == "financial":
            configured = os.environ.get("COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR")
            subdir = "collect-financial-information"
        else:
            configured = os.environ.get("SYMBOL_NEWS_CACHE_MEMORY_DIR")
            subdir = "symbol-news-cache"
        if configured:
            paths.append(Path(configured).expanduser() / filename)
        memory_root = os.environ.get("DAILY_TRADING_MEMORY_DIR")
        if memory_root:
            paths.append(Path(memory_root).expanduser() / subdir / filename)
        paths.append(self.workspace_dir / "memory" / subdir / filename)
        return paths

    def default_cache_path(self, domain: str) -> str:
        for path in self.optional_cache_candidate_paths(domain):
            if path.exists():
                return str(path)
        return ""

    def optional_cache_script_candidates(self, domain: str) -> list[Path]:
        if domain == "financial":
            skill_name = "collect-financial-information"
            script_name = "financial_cache.py"
            candidates = [
                Path("/app/skills") / skill_name / "scripts" / script_name,
                Path("/codex-home/skills") / skill_name / "scripts" / script_name,
            ]
            codex_home = os.environ.get("CODEX_HOME")
            if codex_home:
                candidates.insert(1, Path(codex_home).expanduser() / "skills" / skill_name / "scripts" / script_name)
            return candidates

        configured = os.environ.get("DAILY_TRADING_SYMBOL_NEWS_SCRIPT", "").strip()
        candidates = [
            script_dir().parent.parent / "symbol_news" / "cli.py",
            Path("/app/service/pipelines/symbol_news/cli.py"),
            self.repo_root / "containers/codex-exec/service/pipelines/symbol_news/cli.py",
        ]
        if configured:
            candidates.insert(0, Path(configured).expanduser())
        return candidates

    def optional_cache_script(self, domain: str) -> Path | None:
        for path in self.optional_cache_script_candidates(domain):
            if path.exists():
                return path
        return None

    def etf_or_etn_symbol_ids(self) -> list[str]:
        price = load_json_if_exists(self.output_dir / "price-chart.json")
        if not isinstance(price, dict):
            return []
        result: list[str] = []
        for item in price.get("symbols", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("product_type") or "").lower() not in {"etf", "etn"}:
                continue
            symbol_id = str(item.get("symbol_id") or "").strip()
            if symbol_id:
                result.append(symbol_id)
        return result

    def cache_has_etf_nav_evidence(self, path: Path, etf_symbols: list[str]) -> bool:
        payload = load_yaml_if_exists(path)
        if not isinstance(payload, dict):
            return False
        symbols = payload.get("symbols")
        if not isinstance(symbols, dict):
            return False

        def contains_key(value: Any, wanted: str) -> bool:
            if isinstance(value, dict):
                if wanted in value:
                    return True
                return any(contains_key(child, wanted) for child in value.values())
            if isinstance(value, list):
                return any(contains_key(child, wanted) for child in value)
            return False

        for symbol_id in etf_symbols:
            symbol_payload = symbols.get(symbol_id)
            if not contains_key(symbol_payload, "ETF/ETN 현재가") or not contains_key(symbol_payload, "NAV 비교추이(종목)"):
                return False
        return True

    def covered_cache_path(self, domain: str, path_text: str, symbols: list[str], *, detail: str) -> str:
        path = resolve_workspace_path(self.workspace_dir, path_text)
        stage_name = self.optional_cache_stage_name(domain)
        if domain == "symbol_news":
            expected_date = self.optional_cache_date()
            covered, missing = symbol_news_cache_coverage(path, symbols, expected_date)
        else:
            covered, missing = cache_coverage(path, symbols)
        if covered:
            etf_symbols = self.etf_or_etn_symbol_ids() if domain == "financial" else []
            if etf_symbols and not self.cache_has_etf_nav_evidence(path, etf_symbols):
                self.logs.append(
                    {
                        "stage": f"{stage_name}-coverage",
                        "path": str(path),
                        "missing_etf_nav_symbol_count": len(etf_symbols),
                        "missing_etf_nav_symbols_sample": etf_symbols[:20],
                        "recorded_at": now_iso(),
                    }
                )
                write_json(self.command_log_path, {"commands": self.logs})
                return ""
            self.add_stage(stage_name, "success", detail=detail, required=False, path=path)
            return str(path)
        self.logs.append(
            {
                "stage": f"{stage_name}-coverage",
                "path": str(path),
                "missing_symbol_count": len(missing),
                "missing_symbols_sample": missing[:20],
                "recorded_at": now_iso(),
            }
        )
        write_json(self.command_log_path, {"commands": self.logs})
        return ""

    def parse_cache_collect_path(self, stdout: str) -> str:
        text = stdout.strip()
        if not text:
            return ""
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                path_text = str(payload.get("path") or payload.get("cache_path") or "").strip()
                if path_text:
                    return path_text
        return text.splitlines()[-1].strip()

    def parse_existing_cache_path(self, stdout: str) -> str:
        path_text = self.parse_cache_collect_path(stdout)
        if not path_text:
            return ""
        path = resolve_workspace_path(self.workspace_dir, path_text)
        return str(path) if path.exists() else ""

    def first_existing_cache_path(self, paths: list[Path], symbols: list[str], domain: str = "") -> Path | None:
        wanted = {normalize_symbol_key(symbol) for symbol in symbols if normalize_symbol_key(symbol)}
        seen: set[Path] = set()
        for path in paths:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if not resolved.exists():
                continue
            if domain == "symbol_news":
                expected_date = self.optional_cache_date()
                available = symbol_news_cache_symbol_keys(resolved, expected_date)
            else:
                available = cache_symbol_keys(resolved)
            if available & wanted:
                return resolved
        return None

    def first_existing_cache_file_path(self, paths: list[Path]) -> Path | None:
        seen: set[Path] = set()
        for path in paths:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.exists():
                return resolved
        return None

    def zero_usable_symbol_news_cache_path(self, paths: list[Path], symbols: list[str]) -> Path | None:
        path = self.first_existing_cache_file_path(paths)
        if path is None:
            return None
        expected_date = self.optional_cache_date()
        counts = symbol_news_cache_evidence_counts(path, symbols, expected_date)
        if counts["wanted_symbol_count"] > 0 and counts["usable_symbol_count"] == 0:
            return path
        return None

    def resolve_portfolio(self) -> tuple[dict[str, Any], Path]:
        output_path = self.output_dir / "check-portfolio.json"
        if self.args.portfolio_json:
            source = resolve_workspace_path(self.workspace_dir, self.args.portfolio_json)
            portfolio = load_json(source)
            write_json(output_path, portfolio)
            self.add_stage("check-portfolio", "success", detail="loaded provided JSON", path=output_path)
            return portfolio, output_path

        for script in self.portfolio_script_candidates():
            if not script.exists():
                continue
            result = self.run_cmd("check-portfolio", [str(script)])
            if result.returncode != 0:
                self.add_stage("check-portfolio", "failed", detail="portfolio command failed", path=self.command_log_path)
                raise RuntimeError("check-portfolio command failed")
            try:
                portfolio = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                self.add_stage("check-portfolio", "failed", detail=f"invalid portfolio JSON: {exc}", path=self.command_log_path)
                raise
            write_json(output_path, portfolio)
            self.add_stage("check-portfolio", "success", detail=f"loaded via {script}", path=output_path)
            return portfolio, output_path

        self.add_stage("check-portfolio", "failed", detail="no check-portfolio script found")
        raise RuntimeError("no check-portfolio script found; pass --portfolio-json")

    def collect_main_evidence(self, symbols: list[str]) -> None:
        price_path = self.output_dir / "price-chart.json"
        account_path = self.output_dir / "account-before-order.json"
        if self.args.reuse_existing_artifacts and price_path.exists() and account_path.exists():
            self.add_stage("main-evidence", "success", detail="reused existing price/account artifacts", path=self.output_dir)
            return

        cmd = [
            sys.executable,
            self.main_evidence_script(),
            "collect",
            "--run-id",
            self.run_id,
            "--started-at",
            self.started_at,
            "--symbols",
            ",".join(symbols),
            "--output-dir",
            str(self.output_dir),
            "--env",
            self.args.env,
            "--request-type",
            self.args.request_type,
        ]
        if self.args.skip_account:
            cmd.append("--skip-account")
        result = self.run_cmd("main-evidence", cmd)
        price = load_json_if_exists(price_path)
        if result.returncode == 0 and isinstance(price, dict) and price.get("status") != "failed":
            self.add_stage("main-evidence", "success", detail="collected price/account artifacts", path=self.output_dir)
            return
        self.add_stage("main-evidence", "failed", detail="required price/account collection failed", path=self.command_log_path)
        raise RuntimeError("main evidence collection failed")

    def run_order_lifecycle_preflight(self) -> dict[str, Any]:
        lifecycle_path = self.output_dir / "order-lifecycle.json"
        result = self.run_cmd(
            "order-lifecycle-preflight",
            [
                sys.executable,
                self.order_execution_script(),
                "preflight",
                "--output-dir",
                str(self.output_dir),
                "--env",
                self.args.env,
            ],
        )
        lifecycle = load_json_if_exists(lifecycle_path) or {}
        status = str(lifecycle.get("status") or "failed")
        if result.returncode != 0 or status == "failed":
            self.add_stage(
                "order-lifecycle-preflight",
                "failed",
                detail=compact_text(result.stderr or result.stdout) or f"status={status}",
                path=lifecycle_path,
            )
            raise RuntimeError("order lifecycle preflight failed")
        stage_status = "partial" if status == "partial" else "success"
        self.add_stage(
            "order-lifecycle-preflight",
            stage_status,
            detail=(
                f"active={lifecycle.get('active_order_count', 0)}, "
                f"prior_submitted={lifecycle.get('previous_submitted_cash_order_count', 0)}, "
                f"holding_issues={lifecycle.get('holding_state_issue_count', 0)}"
            ),
            path=lifecycle_path,
        )
        return lifecycle

    def collect_optional_cache(self, domain: str, symbols: list[str]) -> str:
        configured = self.args.financial_cache_path if domain == "financial" else self.args.symbol_news_cache_path
        stage_name = self.optional_cache_stage_name(domain)
        candidate_paths: list[Path] = []
        if configured:
            configured_path = resolve_workspace_path(self.workspace_dir, configured)
            candidate_paths.append(configured_path)
            covered = self.covered_cache_path(domain, configured, symbols, detail="using provided full-universe cache path")
            if covered:
                return covered

        for path in self.optional_cache_candidate_paths(domain):
            if path not in candidate_paths:
                candidate_paths.append(path)
        default_path = self.default_cache_path(domain)
        if default_path:
            covered = self.covered_cache_path(domain, default_path, symbols, detail="using existing same-date full-universe memory cache")
            if covered:
                return covered

        cache_script = self.optional_cache_script(domain)
        if cache_script is None:
            partial = self.first_existing_cache_path(candidate_paths, symbols, domain)
            if partial:
                self.add_stage(stage_name, "partial", detail="optional cache script not found; using existing incomplete cache", required=False, path=partial)
                return str(partial)
            zero_news = self.zero_usable_symbol_news_cache_path(candidate_paths, symbols) if domain == "symbol_news" else None
            if zero_news:
                self.add_stage(
                    "symbol-news-cache",
                    "partial",
                    detail="optional news cache script not found; cache exists but zero usable articles",
                    required=False,
                    path=zero_news,
                )
                return str(zero_news)
            self.add_stage(stage_name, "skipped", detail="optional cache script not found", required=False)
            return ""
        date = self.optional_cache_date()
        get_cmd = [sys.executable, str(cache_script), "get", "--date", date]
        get_result = self.run_cmd(f"{stage_name}-get", get_cmd, required=False)
        get_path = self.parse_existing_cache_path(get_result.stdout)
        if get_path:
            get_cache_path = resolve_workspace_path(self.workspace_dir, get_path)
            if get_cache_path not in candidate_paths:
                candidate_paths.insert(0, get_cache_path)
            covered = self.covered_cache_path(domain, get_path, symbols, detail="using get-returned same-date full-universe cache")
            if covered:
                return covered
        cmd = [
            sys.executable,
            str(cache_script),
            "collect",
            "--date",
            date,
            "--symbols",
            ",".join(symbols),
        ]
        if domain == "financial" and self.has_etf_or_etn_price_rows():
            cmd.append("--include-etf")
        result = self.run_cmd(f"{stage_name}-collect", cmd, required=False)
        if result.returncode == 0:
            path_text = self.parse_cache_collect_path(result.stdout)
            if path_text:
                collected_path = resolve_workspace_path(self.workspace_dir, path_text)
                candidate_paths.insert(0, collected_path)
            else:
                collected_path = None
            second_get_result = self.run_cmd(f"{stage_name}-get", get_cmd, required=False)
            second_get_path = self.parse_existing_cache_path(second_get_result.stdout)
            if second_get_path:
                second_path = resolve_workspace_path(self.workspace_dir, second_get_path)
                candidate_paths.insert(0, second_path)
            covered = self.covered_cache_path(domain, second_get_path, symbols, detail="optional cache collected once and get-returned cache covers full universe") if second_get_path else ""
            if not covered and collected_path:
                covered = self.covered_cache_path(domain, str(collected_path), symbols, detail="optional cache collected once and covers full universe")
            if covered:
                return covered
            partial = self.first_existing_cache_path(candidate_paths, symbols, domain)
            if partial:
                self.add_stage(stage_name, "partial", detail="optional cache collected once but still missing universe symbols; using partial cache", required=False, path=partial)
                return str(partial)
            zero_news = self.zero_usable_symbol_news_cache_path(candidate_paths, symbols) if domain == "symbol_news" else None
            if zero_news:
                self.add_stage(
                    "symbol-news-cache",
                    "partial",
                    detail="optional news cache collected once; cache exists but zero usable articles",
                    required=False,
                    path=zero_news,
                )
                return str(zero_news)
            self.add_stage(stage_name, "partial", detail="optional cache collected once but no cache file was produced", required=False)
            return ""
        partial = self.first_existing_cache_path(candidate_paths, symbols, domain)
        if partial:
            self.add_stage(stage_name, "partial", detail="optional cache collection failed once; using existing incomplete cache", required=False, path=partial)
            return str(partial)
        zero_news = self.zero_usable_symbol_news_cache_path(candidate_paths, symbols) if domain == "symbol_news" else None
        if zero_news:
            self.add_stage(
                "symbol-news-cache",
                "partial",
                detail="optional news cache collection failed once; cache exists but zero usable articles",
                required=False,
                path=zero_news,
            )
            return str(zero_news)
        self.add_stage(stage_name, "partial", detail="optional cache collection failed once", required=False, path=self.command_log_path)
        return ""

    def has_etf_or_etn_price_rows(self) -> bool:
        return bool(self.etf_or_etn_symbol_ids())

    def collect_market_index_snapshot(self) -> str:
        script = self.market_index_snapshot_script()
        output = self.output_dir / "market-index-snapshot.json"
        if not script.exists():
            self.add_stage("market-index-snapshot", "skipped", detail="optional market index snapshot script not found", required=False)
            return ""
        result = self.run_cmd(
            "market-index-snapshot",
            [
                sys.executable,
                str(script),
                "collect",
                "--run-id",
                self.run_id,
                "--started-at",
                self.started_at,
                "--output",
                str(output),
            ],
            required=False,
        )
        payload = load_json_if_exists(output)
        if result.returncode != 0 or not isinstance(payload, dict):
            self.add_stage("market-index-snapshot", "partial", detail="optional market index snapshot collection failed", required=False, path=self.command_log_path)
            return ""
        status = str(payload.get("status") or "")
        if status == "success":
            self.add_stage("market-index-snapshot", "success", detail="collected five market indexes", required=False, path=output)
            return str(output)
        self.add_stage("market-index-snapshot", "partial", detail=f"optional market index snapshot status={status or 'unknown'}", required=False, path=output)
        return str(output)

    def build_news_context(self, symbol_news_cache: str) -> str:
        script = self.news_context_script()
        output = self.output_dir / "news-context.json"
        if not script.exists():
            self.add_stage("news-context", "skipped", detail="optional news context script not found", required=False)
            return ""
        command = [
            sys.executable,
            str(script),
            "build",
            "--workspace-dir",
            str(self.workspace_dir),
            "--run-id",
            self.run_id,
            "--started-at",
            self.started_at,
            "--output",
            str(output),
        ]
        if symbol_news_cache:
            command.extend(["--symbol-news-cache-path", symbol_news_cache])
        result = self.run_cmd("news-context", command, required=False)
        payload = load_json_if_exists(output)
        if result.returncode != 0 or not isinstance(payload, dict):
            self.add_stage(
                "news-context",
                "partial",
                detail="optional news context construction failed",
                required=False,
                path=self.command_log_path,
            )
            return ""
        status = str(payload.get("status") or "partial")
        market_news = payload.get("market_news") if isinstance(payload.get("market_news"), dict) else {}
        selected_count = as_int(market_news.get("selected_count"))
        self.add_stage(
            "news-context",
            "success" if status == "success" else "partial",
            detail=f"status={status}, market_news_items={selected_count}",
            required=False,
            path=output,
        )
        return str(output)

    def run_artifact_command(self, stage: str, args: list[str], *, required: bool = True) -> dict[str, Any] | None:
        result = self.run_cmd(stage, [sys.executable, self.artifact_script(), *args], required=required)
        if result.returncode != 0:
            self.add_stage(stage, "failed" if required else "partial", detail="artifact helper failed", required=required, path=self.command_log_path)
            if required:
                raise RuntimeError(f"{stage} failed")
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    def run_analyst_reviews(self) -> None:
        specs_path = self.output_dir / "analyst-review-specs.json"
        result = self.run_cmd(
            "analyst-review",
            [sys.executable, self.subagent_script(), "run-group", "--spec", str(specs_path), "--max-workers", str(self.args.max_workers)],
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {}
        if result.returncode == 0 and payload.get("status") == "success":
            self.add_stage("analyst-review", "success", detail="all analyst-review wrappers succeeded", path=self.output_dir / "subagents")
            return
        # Missing/failed analyst rows are explicit non-directional evidence
        # gaps. Continue so every eligible held symbol still reaches Judge;
        # merge-first records the missing scores, and unheld selection remains
        # limited to symbols with a valid continuous score.
        self.add_stage(
            "analyst-review",
            "partial",
            detail="one or more analyst-review wrappers failed; held-symbol review continues without missing scores",
            path=self.command_log_path,
        )

    def run_judge_review(self) -> None:
        spec_path = self.output_dir / "judge-review-spec.json"
        spec = load_json(spec_path)
        if not normalize_symbol_ids(spec.get("symbol_ids")):
            write_json(
                self.output_dir / "judge-review.json",
                {
                    "schema_version": "4",
                    "run_id": self.run_id,
                    "started_at": self.started_at,
                    "generated_at": now_iso(),
                    "stage": "judge-review",
                    "status": "success",
                    "skipped": True,
                    "skip_reason": "no selected symbols",
                    "errors": [],
                    "symbols": [],
                },
            )
            self.add_stage("judge-review", "skipped", detail="no selected symbols", required=False, path=self.output_dir / "judge-review.json")
            return
        last_detail = "required judge-review wrapper failed"
        for attempt in range(1, 4):
            result = self.run_cmd("judge-review", [sys.executable, self.subagent_script(), "run-one", "--spec", str(spec_path)])
            try:
                wrapper = json.loads(result.stdout)
            except json.JSONDecodeError:
                wrapper = {}
            if result.returncode == 0 and wrapper.get("status") == "success":
                artifact = self.write_judge_review(wrapper)
                artifact_status = str(artifact.get("status") or "partial")
                detail = "judge-review wrapper merged" if attempt == 1 else f"judge-review wrapper merged after retry {attempt - 1}"
                self.add_stage(
                    "judge-review",
                    "success" if artifact_status == "success" else "partial",
                    detail=f"{detail}; artifact status={artifact_status}",
                    path=self.output_dir / "judge-review.json",
                )
                return
            last_detail = f"required judge-review wrapper failed on attempt {attempt}"
        self.add_stage("judge-review", "failed", detail=last_detail, path=self.command_log_path)
        raise RuntimeError("judge-review failed")

    def run_order_execution(self) -> dict[str, Any]:
        result = self.run_cmd(
            "order-execution",
            [
                sys.executable,
                self.order_execution_script(),
                "run",
                "--output-dir",
                str(self.output_dir),
                "--env",
                self.args.env,
                "--submit",
            ],
        )
        if result.returncode != 0:
            self.add_stage("order-execution", "failed", detail=compact_text(result.stderr or result.stdout), path=self.output_dir / "execution.json")
            raise RuntimeError("order-execution failed")
        execution = load_json(self.output_dir / "execution.json")
        status = str(execution.get("status") or "")
        stage_status = "success" if status == "success" else "partial" if status == "partial" else "failed"
        self.add_stage("order-execution", stage_status, detail=f"status={status}", path=self.output_dir / "execution.json")
        if stage_status == "failed":
            raise RuntimeError("order-execution failed")
        return execution

    def run_terminal_order_reconciliation(self) -> None:
        if not getattr(self.args, "submit_orders", False) or self.args.request_type not in {"demo-submit", "real-submit"}:
            return
        result = self.run_cmd(
            "order-execution-terminal-reconcile",
            [
                sys.executable,
                self.order_execution_script(),
                "terminal-reconcile",
                "--output-dir",
                str(self.output_dir),
                "--env",
                self.args.env,
            ],
            required=False,
        )
        execution = load_json_if_exists(self.output_dir / "execution.json") or {}
        status = str(execution.get("status") or "partial")
        stage_status = "success" if result.returncode == 0 and status == "success" else "partial"
        for stage in reversed(self.stages):
            if stage.get("stage") == "order-execution":
                stage["status"] = stage_status
                stage["detail"] = f"status={status}; terminal reconciliation {'checked' if result.returncode == 0 else 'failed'}"
                self.write_run_json(status=self.pipeline_status())
                break

    def finalize_account_order_gate_status(self, execution: dict[str, Any]) -> str:
        account_path = self.output_dir / "account-before-order.json"
        account = load_json_if_exists(account_path) or {}
        current = str(account.get("order_gate_status") or "").strip()
        if current in {"success", "failed"}:
            return current
        requires_gate = (
            str(execution.get("request_type") or self.args.request_type) in {"demo-submit", "real-submit"}
            and bool(execution.get("requires_main_agent_order_execution"))
        )
        status = "not_run" if requires_gate else "not_required"
        account["order_gate_status"] = status
        write_json(account_path, account)
        return status

    def select_prior_thesis_source(self, symbol_id: str) -> dict[str, Any] | None:
        """Return the most recent strictly-earlier successful run's valid thesis_definition.

        Eligible runs must have judge-review.json status == "success" and a
        started_at strictly earlier than this run's started_at; the current run,
        future/equal-time runs, and partial/failed/malformed artifacts are
        ignored. An empty or malformed thesis_definition never counts as valid
        prior context. The current Judge may assess it or return a successor
        definition, but neither one mechanically authorizes the current target.

        Single source of truth for load_prior_thesis() and prior_thesis_context():
        both derive from this same selection so Judge context and the
        persisted/rendered provenance cannot drift apart.
        """
        if not symbol_id:
            return None
        runs_dir = self.output_dir.parent
        if not runs_dir.is_dir():
            return None
        current_started = parse_kst_datetime(self.started_at)
        current_dir = self.output_dir.resolve()
        # Secondary sort key (source run_id) breaks ties when two eligible runs
        # share the exact same started_at, so this and run_subagent.prior_thesis_context
        # deterministically agree instead of depending on filesystem iteration order.
        best: tuple[datetime, str, str, dict[str, Any]] | None = None
        for run_dir in (path for path in runs_dir.iterdir() if path.is_dir()):
            if run_dir.resolve() == current_dir:
                continue
            payload = load_json_if_exists(run_dir / "judge-review.json")
            if not isinstance(payload, dict) or payload.get("status") != "success":
                continue
            started_text = str(payload.get("started_at") or "").strip()
            if not started_text:
                continue
            try:
                candidate_started = parse_kst_datetime(started_text)
            except ValueError:
                continue
            if not candidate_started < current_started:
                continue
            source_run_id = str(payload.get("run_id") or run_dir.name)
            symbols = payload.get("symbols") if isinstance(payload.get("symbols"), list) else []
            for item in symbols:
                if not isinstance(item, dict) or symbol_key(item) != symbol_id:
                    continue
                thesis = item.get("thesis_definition")
                if not thesis_definition_is_valid(thesis):
                    continue
                candidate_key = (candidate_started, source_run_id)
                if best is None or candidate_key > (best[0], best[1]):
                    best = (candidate_started, source_run_id, started_text, thesis)
        if best is None:
            return None
        return {
            "source_run_id": best[1],
            "source_started_at": best[2],
            "thesis_definition": best[3],
        }

    def load_prior_thesis(self, symbol_id: str) -> dict[str, Any] | None:
        """Return only the prior thesis_definition; unchanged public shape for existing callers."""
        selected = self.select_prior_thesis_source(symbol_id)
        return selected["thesis_definition"] if selected else None

    def prior_thesis_context_from_selection(self, selected: dict[str, Any] | None) -> dict[str, Any]:
        """Format an already-selected prior source into bounded, HTML-safe provenance.

        Pure formatting, no filesystem access: callers that already hold a
        select_prior_thesis_source() result pass that exact object here instead
        of triggering a second scan. Never includes filesystem paths;
        source_artifact is a stable relative label.
        """
        if selected is None:
            return {"available": False}
        thesis = selected["thesis_definition"]
        raw_conditions = thesis.get("invalidation_conditions") if isinstance(thesis, dict) else None
        conditions: list[dict[str, Any]] = []
        for condition in raw_conditions if isinstance(raw_conditions, list) else []:
            if not isinstance(condition, dict):
                continue
            condition_id = normalize_thesis_condition_id(condition.get("condition_id"))
            description = condition.get("description")
            if not condition_id or not isinstance(description, str) or not description.strip():
                continue
            conditions.append({"condition_id": condition_id, "description": description.strip()[:200]})
        return {
            "available": True,
            "source_run_id": selected["source_run_id"],
            "source_started_at": selected["source_started_at"],
            "source_artifact": "judge-review.json",
            "core_rationale": str(thesis.get("core_rationale") or "").strip()[:300],
            "invalidation_conditions": conditions[:8],
        }

    def prior_thesis_context(self, symbol_id: str) -> dict[str, Any]:
        """Bounded, HTML-safe prior-thesis provenance from a fresh selection.

        Use when the caller does not already hold a selected prior source.
        """
        return self.prior_thesis_context_from_selection(self.select_prior_thesis_source(symbol_id))

    def validate_thesis_definition(self, raw: Any) -> dict[str, Any] | None:
        """Sanitize a judge-supplied thesis_definition, returning None when invalid.

        A valid definition requires a non-empty core_rationale and at least one
        unique invalidation condition with a non-empty condition_id and
        description. core_rationale, condition_id, and description must be
        actual strings (not numbers/bools/objects/lists coerced with str()).
        Nothing is ever synthesized here: an invalid/missing input simply
        yields None so callers never persist an empty placeholder.
        """
        if not isinstance(raw, dict):
            return None
        core_rationale = raw.get("core_rationale")
        if not isinstance(core_rationale, str) or not core_rationale.strip():
            return None
        conditions: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for condition in raw.get("invalidation_conditions") or []:
            if not isinstance(condition, dict):
                continue
            raw_condition_id = condition.get("condition_id")
            description = condition.get("description")
            if not isinstance(raw_condition_id, str) or not isinstance(description, str):
                continue
            description = description.strip()
            if not description:
                continue
            condition_id = normalize_thesis_condition_id(raw_condition_id)
            if not condition_id or condition_id in seen_ids:
                continue
            seen_ids.add(condition_id)
            conditions.append({"condition_id": condition_id, "description": description[:200]})
        if not conditions:
            return None
        return {
            "defined_at_run_id": self.run_id,
            "core_rationale": core_rationale.strip()[:300],
            "invalidation_conditions": conditions[:8],
        }

    def sanitize_thesis_assessment(self, raw: dict[str, Any]) -> dict[str, Any]:
        status = str(raw.get("status") or "").strip().lower()
        if status not in {"intact", "damaged", "uncertain"}:
            status = "uncertain"
        raw_matched = raw.get("matched_invalidation_condition_ids")
        matched_ids: list[str] = []
        if isinstance(raw_matched, list):
            for value in raw_matched:
                normalized = normalize_thesis_condition_id(value)
                if normalized and normalized not in matched_ids:
                    matched_ids.append(normalized)
        return {
            "status": status,
            "matched_invalidation_condition_ids": matched_ids[:8],
        }

    def sanitize_opposing_view_case(self, raw: Any) -> dict[str, Any]:
        source = raw if isinstance(raw, dict) else {}
        raw_refs = source.get("evidence_refs")
        return {
            "summary": str(source.get("summary") or "")[:300],
            "evidence_refs": (
                [str(value).strip() for value in raw_refs if str(value).strip()][:10]
                if isinstance(raw_refs, list)
                else []
            ),
        }

    def sanitize_opposing_view(self, raw: Any) -> dict[str, Any] | None:
        """Keep the Judge's compact increase/reduce comparison for audit."""
        if not isinstance(raw, dict):
            return None
        return {
            "increase_case": self.sanitize_opposing_view_case(raw.get("increase_case")),
            "reduce_case": self.sanitize_opposing_view_case(raw.get("reduce_case")),
        }

    def judge_review_context_by_symbol(self, wrapper: dict[str, Any]) -> dict[str, dict[str, Any]]:
        paths = wrapper.get("review_input_paths") if isinstance(wrapper.get("review_input_paths"), dict) else {}
        review_core_path = paths.get("review_core")
        if not review_core_path:
            return {}
        payload = load_json_if_exists(Path(str(review_core_path)))
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(symbols, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for item in symbols:
            if isinstance(item, dict):
                key = symbol_key(item)
                if key:
                    result[key] = item
        return result

    def derive_judge_final_quantity(
        self,
        item: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        symbol_id = symbol_key(item)
        errors: list[dict[str, Any]] = []
        if context:
            merged_context = dict(item)
            merged_context.update(context)
            context = merged_context
        else:
            context = item
        requested_target_value = non_negative_decimal_value(item.get("target_position_value_krw"))
        if requested_target_value is None:
            errors.append(
                {
                    "stage": "judge-review",
                    "source": "pipeline",
                    "code": "invalid_target_position_value_krw",
                    "message": f"{symbol_id}: target_position_value_krw must be a non-negative number",
                    "required": True,
                }
            )
            return None, errors
        price = positive_decimal_value((context.get("price") or {}).get("current_or_last") if isinstance(context.get("price"), dict) else None)
        if price is None:
            price = positive_decimal_value((item.get("price") or {}).get("current_or_last") if isinstance(item.get("price"), dict) else None)
        if price is None:
            errors.append(
                {
                    "stage": "judge-review",
                    "source": "pipeline",
                    "code": "invalid_price_current_or_last",
                    "message": f"{symbol_id}: price.current_or_last must be a positive number to derive final_holding_quantity",
                    "required": True,
                }
            )
            return None, errors

        holding_context = context.get("holding_quantity_context") if isinstance(context.get("holding_quantity_context"), dict) else {}
        if not holding_context and isinstance(item.get("holding_quantity_context"), dict):
            holding_context = item.get("holding_quantity_context") or {}
        expected_qty = as_int(holding_context.get("expected_holding_quantity"))
        baseline_value = Decimal(expected_qty) * price
        requested_qty = round_half_up_quantity(requested_target_value, price)
        # Canonical exposure is always the value of the whole-share quantity the
        # executor can actually submit. The raw judge KRW target remains in
        # requested_target_position_value_krw for audit.
        target_value = Decimal(requested_qty) * price

        decision_basis = str(item.get("decision_basis") or "none").strip().lower()
        if decision_basis not in {"none", "thesis", "profit_protection", "concentration_rebalance"}:
            decision_basis = "none"

        additional_buy_reason = str(item.get("additional_buy_reason") or "").strip()
        thesis_definition_out = self.validate_thesis_definition(item.get("thesis_definition"))

        normalized = self._assemble_judge_symbol(
            symbol_id=symbol_id,
            item=item,
            context=context,
            requested_target_value=requested_target_value,
            target_value=target_value,
            baseline_value=baseline_value,
            price=price,
            expected_qty=expected_qty,
            decision_basis=decision_basis,
            thesis_definition_out=thesis_definition_out,
            additional_buy_reason=additional_buy_reason,
        )
        return normalized, errors

    def _assemble_judge_symbol(
        self,
        *,
        symbol_id: str,
        item: dict[str, Any],
        context: dict[str, Any],
        requested_target_value: Decimal,
        target_value: Decimal,
        baseline_value: Decimal,
        price: Decimal,
        expected_qty: int,
        decision_basis: str,
        thesis_definition_out: dict[str, Any] | None,
        additional_buy_reason: str = "",
    ) -> dict[str, Any]:
        final_qty = round_half_up_quantity(target_value, price)
        requested_qty = round_half_up_quantity(requested_target_value, price)
        requested_action = derive_action(requested_qty, expected_qty)
        canonical_action = derive_action(final_qty, expected_qty)
        normalized = {
            "symbol_id": symbol_id,
            "symbol_name": item.get("symbol_name") or context.get("symbol_name") or symbol_id,
            "requested_target_position_value_krw": decimal_json_value(requested_target_value),
            "target_position_value_krw": decimal_json_value(target_value),
            "baseline_position_value_krw": decimal_json_value(baseline_value),
            "final_holding_quantity": final_qty,
            "requested_action": requested_action,
            "canonical_action": canonical_action,
            "relative_attractiveness_rank": as_int(item.get("relative_attractiveness_rank")),
            "decision_basis": decision_basis,
            "reason_code": safe_name(str(item.get("reason_code") or "hold_neutral")).lower(),
            "one_line_reason": str(item.get("one_line_reason") or "")[:300],
        }
        if additional_buy_reason:
            normalized["additional_buy_reason"] = additional_buy_reason[:300]
        if thesis_definition_out is not None:
            normalized["thesis_definition"] = thesis_definition_out
        assessment_echo = item.get("thesis_assessment") if isinstance(item.get("thesis_assessment"), dict) else None
        if assessment_echo is not None:
            normalized["thesis_assessment"] = self.sanitize_thesis_assessment(assessment_echo)
        opposing_view_echo = self.sanitize_opposing_view(item.get("opposing_view"))
        if opposing_view_echo is not None:
            normalized["opposing_view"] = opposing_view_echo
        if assessment_echo is not None or thesis_definition_out is not None:
            normalized["prior_thesis_context"] = self.prior_thesis_context(symbol_id)
        return normalized

    def write_judge_review(self, wrapper: dict[str, Any]) -> dict[str, Any]:
        parsed = wrapper.get("parsed_json") if isinstance(wrapper.get("parsed_json"), dict) else {}
        symbols: list[dict[str, Any]] = []
        errors = wrapper.get("errors") if isinstance(wrapper.get("errors"), list) else []
        context_by_symbol = self.judge_review_context_by_symbol(wrapper)
        spec = load_json_if_exists(self.output_dir / "judge-review-spec.json") or {}
        allowed_symbols = set(normalize_symbol_ids(spec.get("symbol_ids"))) if isinstance(spec.get("symbol_ids"), list) else set()
        ordered_items = sorted(
            (item for item in parsed.get("symbols", []) if isinstance(item, dict) and symbol_key(item)),
            key=symbol_key,
        )
        symbol_counts: dict[str, int] = {}
        for item in ordered_items:
            item_symbol_id = symbol_key(item)
            symbol_counts[item_symbol_id] = symbol_counts.get(item_symbol_id, 0) + 1
        duplicate_symbol_ids = {
            symbol_id for symbol_id, count in symbol_counts.items() if count > 1
        }
        for symbol_id in sorted(duplicate_symbol_ids):
            errors.append(
                {
                    "stage": "judge-review",
                    "source": "pipeline",
                    "code": "duplicate_judge_symbol",
                    "message": f"{symbol_id}: judge returned duplicate decisions; all duplicate rows were dropped",
                    "required": True,
                }
            )
        for item in ordered_items:
            symbol_id = symbol_key(item)
            if symbol_id in duplicate_symbol_ids:
                continue
            if allowed_symbols and symbol_id not in allowed_symbols:
                errors.append(
                    {
                        "stage": "judge-review",
                        "source": "pipeline",
                        "code": "judge_symbol_outside_candidate_set",
                        "message": f"{symbol_id}: judge returned a decision for a symbol outside the supplied candidate set",
                        "required": True,
                    }
                )
                continue
            normalized, item_errors = self.derive_judge_final_quantity(
                item,
                context_by_symbol.get(symbol_id, {}),
            )
            errors.extend(item_errors)
            if normalized is None:
                continue
            symbols.append(normalized)
        resolved_symbol_ids = {symbol_key(item) for item in symbols}
        missing_symbol_ids = sorted(allowed_symbols - resolved_symbol_ids)
        for symbol_id in missing_symbol_ids:
            errors.append(
                {
                    "stage": "judge-review",
                    "source": "pipeline",
                    "code": "missing_judge_symbol",
                    "message": f"{symbol_id}: judge returned no valid decision for a required review-scope symbol",
                    "required": True,
                }
            )
        membership_complete = not duplicate_symbol_ids and not missing_symbol_ids
        artifact = {
            # Version 4 accepts the Judge target directly after whole-share
            # normalization; strategy labels and thesis fields are audit only.
            "schema_version": "4",
            "run_id": self.run_id,
            "started_at": self.started_at,
            "generated_at": now_iso(),
            "stage": "judge-review",
            "status": "success" if symbols and membership_complete else "partial",
            "skipped": False,
            "skip_reason": "",
            "errors": errors,
            "symbols": symbols,
        }
        write_json(self.output_dir / "judge-review.json", artifact)
        self.write_second_sidecar(str(wrapper.get("agent_role") or "judge"), str(wrapper.get("task_name") or "second-judge"), symbols)
        return artifact

    def write_second_sidecar(self, role: str, task_name: str, symbols: list[dict[str, Any]]) -> None:
        path = self.output_dir / "reviews" / f"judge-review--{safe_name(role)}--{safe_name(task_name)}.md"
        lines = [
            "| 종목 | 목표금액 | 최종수량 | 상대매력도 | 판단코드 | 의견(판단) |",
            "|---|---:|---:|---:|---|---|",
        ]
        for item in symbols:
            symbol_name = f"{item.get('symbol_id', '')} {item.get('symbol_name', '')}".strip()
            lines.append(
                f"| {symbol_name} | {format_number(item.get('target_position_value_krw'))} | {as_int(item.get('final_holding_quantity'))} | {as_int(item.get('relative_attractiveness_rank'))} | {item.get('reason_code', '')} | {item.get('one_line_reason', '')} |"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def report_path(self) -> Path:
        return self.workspace_dir / "reports" / f"{report_date_from(self.started_at)}_포트폴리오.md"

    def load_summary_stages(self) -> list[dict[str, Any]]:
        if self.stages:
            return self.stages
        run = load_json_if_exists(self.run_path) or {}
        stages = run.get("stages") if isinstance(run, dict) else []
        return stages if isinstance(stages, list) else []

    def load_portfolio_for_summary(self) -> dict[str, Any]:
        if self.args.portfolio_json:
            path = resolve_workspace_path(self.workspace_dir, self.args.portfolio_json)
            payload = load_json_if_exists(path)
            if isinstance(payload, dict):
                return payload
        payload = load_json_if_exists(self.output_dir / "check-portfolio.json")
        return payload if isinstance(payload, dict) else {}

    def build_account_display_summary(self, account_summary: dict[str, Any]) -> dict[str, Any]:
        return {
            "cash_amount": account_summary.get("cash_amount"),
            "orderable_cash_amount": account_summary.get("orderable_cash_amount"),
            "securities_valuation_amount": account_summary.get("securities_valuation_amount"),
            "total_evaluation_amount": account_summary.get("total_evaluation_amount"),
            "total_pnl_amount": account_summary.get("total_pnl_amount"),
            "today_trade_amounts": {
                "buy_amount": account_summary.get("today_buy_amount"),
                "sell_amount": account_summary.get("today_sell_amount"),
                "display_policy": "Do not mix these cumulative same-day amounts into the main account state; show only if explicitly useful as 당일 거래 누계. These are the this-run pre-order snapshot captured from account-before-order.json, not values current at Telegram delivery time.",
            },
        }

    def build_account_asset_summary(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        if snapshot.get("skipped") or snapshot.get("status") not in {"success", "partial"}:
            return {}
        embedded = snapshot.get("account_asset_summary")
        if isinstance(embedded, dict) and embedded.get("total_asset_amount") is not None:
            return embedded
        total_asset = as_int(snapshot.get("tot_asst_amt"))
        if total_asset is None:
            return {}
        purchase = as_int(snapshot.get("pchs_amt_smtl"))
        pnl = as_int(snapshot.get("evlu_pfls_amt_smtl"))
        return {
            "source_api": snapshot.get("source_api") or "inquire_account_balance",
            "observed_at": snapshot.get("observed_at"),
            "total_asset_amount": total_asset,
            "cash_deposit_amount": as_int(snapshot.get("tot_dncl_amt")),
            "evaluated_asset_amount": as_int(snapshot.get("evlu_amt_smtl")),
            "purchase_amount": purchase,
            "evaluation_pnl_amount": pnl,
            "evaluation_pnl_rate": (pnl / purchase) if purchase and pnl is not None else None,
            "overseas_stock_evaluation_amount": as_int(snapshot.get("ovrs_stck_evlu_amt1")),
        }

    def build_today_trade_summary(self, decision_brief: dict[str, Any]) -> dict[str, Any]:
        symbols = decision_brief.get("symbols") if isinstance(decision_brief.get("symbols"), list) else []
        traded: list[dict[str, Any]] = []
        collection_statuses: list[str] = []
        confirmed_no_trade_count = 0
        unknown_count = 0
        for item in symbols:
            if not isinstance(item, dict):
                continue
            context = item.get("today_trade_timeline_context") if isinstance(item.get("today_trade_timeline_context"), dict) else {}
            collection_status = str(context.get("collection_status") or "unavailable").lower()
            collection_statuses.append(collection_status)
            if context.get("has_same_day_trade") is False:
                confirmed_no_trade_count += 1
            elif context.get("has_same_day_trade") is None:
                unknown_count += 1
            if not context.get("has_same_day_trade"):
                continue
            traded.append(
                {
                    "symbol_id": item.get("symbol_id"),
                    "symbol_name": item.get("symbol_name"),
                    "fill_count": context.get("fill_count"),
                    "net_quantity": context.get("net_quantity"),
                    "bot_net_quantity": context.get("bot_net_quantity"),
                    "manual_net_quantity": context.get("manual_net_quantity"),
                    "manual_fill_count": context.get("manual_fill_count"),
                    "last_direction": context.get("last_direction"),
                    "last_fill_at": context.get("last_fill_at"),
                    "last_fill_price": context.get("last_fill_price"),
                    "move_since_last_fill_pct": context.get("move_since_last_fill_pct"),
                    "has_intraday_reversal": bool(context.get("has_intraday_reversal")),
                }
            )
        if collection_statuses and all(status == "complete" for status in collection_statuses):
            collection_status = "complete"
        elif any(status in {"complete", "partial"} for status in collection_statuses):
            collection_status = "partial"
        else:
            collection_status = "unavailable"
        return {
            "collection_status": collection_status,
            "symbol_count_with_fills": len(traded),
            "confirmed_no_trade_symbol_count": confirmed_no_trade_count,
            "unknown_symbol_count": unknown_count,
            "symbols": traded[:10],
            "display_policy": "Show as same-day trade timeline context, not as trades newly caused by this command. Empty history is confirmed only when collection_status is complete.",
        }

    def build_evidence_summary(self, decision_brief: dict[str, Any], stages: list[dict[str, Any]], symbols: list[str]) -> dict[str, Any]:
        stage_by_name = {item.get("stage"): item for item in stages if isinstance(item, dict)}
        decision_symbols = decision_brief.get("symbols") if isinstance(decision_brief.get("symbols"), list) else []
        market_news_context = (
            decision_brief.get("market_news_context")
            if isinstance(decision_brief.get("market_news_context"), dict)
            else {}
        )
        financial_supplied = 0
        symbol_news_with_articles = 0
        investor_flow_usable = 0
        price_only = 0
        for item in decision_symbols:
            if not isinstance(item, dict):
                continue
            financial_summary = item.get("financial_summary") if isinstance(item.get("financial_summary"), dict) else {}
            if financial_summary.get("cache_status") == "supplied":
                financial_supplied += 1
            symbol_news_summary = item.get("symbol_news_summary") if isinstance(item.get("symbol_news_summary"), list) else []
            if symbol_news_summary:
                symbol_news_with_articles += 1
            investor_flow = item.get("investor_flow_summary") if isinstance(item.get("investor_flow_summary"), dict) else {}
            if any(
                investor_flow.get(key) is not None
                for key in (
                    "foreign_net_buy_quantity",
                    "institution_net_buy_quantity",
                    "combined_net_buy_quantity",
                )
            ):
                investor_flow_usable += 1
            if item.get("evidence_mode") == "price-only":
                price_only += 1

        summary: dict[str, Any] = {
            "symbol_count": len(symbols),
            "price_only_symbol_count": price_only,
            "financial": {
                "status": "supplied" if financial_supplied else "not_supplied",
                "symbol_count_with_summary": financial_supplied,
                "display_text": f"재무: {financial_supplied}개 종목 반영" if financial_supplied else "재무: 반영된 요약 없음",
            },
            "symbol_news": {
                "status": "supplied" if symbol_news_with_articles else "not_supplied",
                "symbol_count_with_articles": symbol_news_with_articles,
                "display_text": (
                    f"종목뉴스: {symbol_news_with_articles}개 종목 기사 반영"
                    if symbol_news_with_articles
                    else "종목뉴스: 반영된 기사 없음"
                ),
            },
            "market_news": {
                "status": market_news_context.get("status") or "missing",
                "article_count": as_int(market_news_context.get("selected_count")),
                "window_start": market_news_context.get("window_start") or "",
                "window_end": market_news_context.get("window_end") or "",
                "window_source": market_news_context.get("window_source") or "",
                "source_statuses": market_news_context.get("source_statuses")
                if isinstance(market_news_context.get("source_statuses"), dict)
                else {},
                "display_text": (
                    f"시장뉴스: {as_int(market_news_context.get('selected_count'))}건 반영"
                    if as_int(market_news_context.get("selected_count"))
                    else "시장뉴스: 반영된 기사 없음"
                ),
            },
            "investor_flow": {
                "status": (
                    "supplied"
                    if investor_flow_usable == len(symbols) and symbols
                    else "partial"
                    if investor_flow_usable
                    else "not_supplied"
                ),
                "usable_symbol_count": investor_flow_usable,
                "missing_usable_symbol_count": max(0, len(symbols) - investor_flow_usable),
                "display_text": (
                    f"장중 수급: {investor_flow_usable}개 종목 추정치 반영"
                    if investor_flow_usable == len(symbols) and symbols
                    else f"장중 수급: {investor_flow_usable}개 종목 추정치 반영, 일부 종목 수급 없음"
                    if investor_flow_usable
                    else "장중 수급: 반영된 추정치 없음"
                ),
            },
        }
        for domain, stage_name in (("financial", "financial-cache"), ("symbol_news", "symbol-news-cache")):
            stage = stage_by_name.get(stage_name)
            if not isinstance(stage, dict):
                continue
            path_text = str(stage.get("path") or "").strip()
            domain_summary = summary[domain]
            domain_summary["cache_stage_status"] = stage.get("status")
            domain_summary["cache_stage_detail"] = stage.get("detail")
            if path_text:
                domain_summary["cache_path"] = path_text
                path = resolve_workspace_path(self.workspace_dir, path_text)
                if path.exists():
                    if domain == "symbol_news":
                        expected_date = self.optional_cache_date()
                        domain_summary["cache_counts"] = symbol_news_cache_evidence_counts(path, symbols, expected_date)
                    else:
                        domain_summary["cache_counts"] = cache_evidence_counts(path, symbols)
            if domain == "symbol_news":
                counts = domain_summary.get("cache_counts") if isinstance(domain_summary.get("cache_counts"), dict) else {}
                if counts and as_int(counts.get("usable_symbol_count")) == 0:
                    domain_summary["status"] = "cache_exists_zero_usable_articles"
                    domain_summary["display_text"] = "종목뉴스: 캐시 파일은 있으나 사용 가능한 기사 0건"
                elif not path_text:
                    domain_summary["status"] = "cache_missing"
                    domain_summary["display_text"] = "종목뉴스: 캐시 파일 없음"
                elif symbol_news_with_articles and symbol_news_with_articles < len(symbols):
                    domain_summary["status"] = "partial"
                    domain_summary["display_text"] = f"종목뉴스: {symbol_news_with_articles}개 종목 기사 반영, 일부 종목 기사 없음"
            elif domain == "financial":
                if not path_text:
                    domain_summary["status"] = "cache_missing"
                    domain_summary["display_text"] = "재무: 캐시 파일 없음"
                elif financial_supplied and financial_supplied < len(symbols):
                    domain_summary["status"] = "partial"
                    domain_summary["display_text"] = f"재무: {financial_supplied}개 종목 반영, 일부 종목 요약 없음"
        market_stage = stage_by_name.get("news-context")
        if isinstance(market_stage, dict):
            summary["market_news"]["context_stage_status"] = market_stage.get("status")
            summary["market_news"]["context_stage_detail"] = market_stage.get("detail")
            if market_stage.get("path"):
                summary["market_news"]["context_path"] = market_stage.get("path")
        market_article_count = as_int(summary["market_news"].get("article_count"))
        if market_article_count and summary["market_news"]["status"] not in {"supplied", "partial"}:
            summary["market_news"]["status"] = "partial"
        if summary["market_news"]["status"] == "partial" and market_article_count:
            summary["market_news"]["display_text"] = f"시장뉴스: {market_article_count}건 반영, 일부 수집원 실패"
        elif summary["market_news"]["status"] == "missing":
            summary["market_news"]["display_text"] = "시장뉴스: 저장 DB 없음"
        elif summary["market_news"]["status"] == "failed":
            summary["market_news"]["display_text"] = "시장뉴스: 최근 수집 실패"
        elif summary["market_news"]["status"] == "empty":
            summary["market_news"]["display_text"] = "시장뉴스: 조회 구간 내 기사 없음"
        return summary

    def build_reporting_view(
        self,
        *,
        account_display_summary: dict[str, Any],
        account_asset_summary: dict[str, Any],
        account: dict[str, Any],
        order_lifecycle_view: dict[str, Any],
        current_run_orders: list[dict[str, Any]],
        evidence_summary: dict[str, Any],
        account_collection_status: str,
        order_gate_status: str,
        execution_status: str,
        pipeline_status: str,
    ) -> dict[str, Any]:
        """Normalize account/order/evidence scopes into one shared rendering contract.

        Markdown/Telegram/HTML all read this instead of re-deriving scope from raw
        artifacts, so full-account assets, domestic valuation, and lifecycle-derived
        active order counts cannot silently drift apart across renderers again.
        """
        raw_active_orders = account.get("active_orders")
        raw_active_order_row_count = len(raw_active_orders) if isinstance(raw_active_orders, list) else 0
        lifecycle_lookup_complete = bool(order_lifecycle_view.get("lookup_complete"))
        legacy_lookup_performed = account.get("active_order_lookup_performed") is True
        if lifecycle_lookup_complete:
            # Only a lifecycle-confirmed lookup may populate a count; the raw
            # active_orders row length is history/reservation evidence, not a
            # confirmed active-order count, and must never be guessed as 0.
            active_count = as_int(order_lifecycle_view.get("active_order_count"))
            active_lookup_status = "complete"
        elif legacy_lookup_performed:
            active_count = None
            active_lookup_status = "legacy_lookup_without_lifecycle_confirmation"
        else:
            active_count = None
            active_lookup_status = "not_looked_up"
        current_run_submitted_count = sum(1 for item in current_run_orders if isinstance(item, dict) and item.get("result") == "submitted")

        def evidence_domain_view(name: str) -> dict[str, Any]:
            domain = evidence_summary.get(name) if isinstance(evidence_summary.get(name), dict) else {}
            if name == "market_news":
                return {
                    "scope": "market_news_context_quality",
                    "status": domain.get("status") or "not_supplied",
                    "coverage_text": domain.get("display_text") or "",
                    "usable_item_count": domain.get("article_count"),
                    "window_start": domain.get("window_start") or "",
                    "window_end": domain.get("window_end") or "",
                    "blocks_trading": False,
                }
            counts = domain.get("cache_counts") if isinstance(domain.get("cache_counts"), dict) else {}
            usable = counts.get("usable_symbol_count") if counts else domain.get("usable_symbol_count")
            wanted = counts.get("wanted_symbol_count") if counts else domain.get("wanted_symbol_count") or evidence_summary.get("symbol_count")
            missing = counts.get("missing_usable_symbol_count") if counts else domain.get("missing_usable_symbol_count")
            if missing is None and usable is not None and wanted is not None:
                missing = max(0, as_int(wanted) - as_int(usable))
            return {
                "scope": "collection_data_quality",
                "status": domain.get("status") or "not_supplied",
                "coverage_text": domain.get("display_text") or "",
                "usable_symbol_count": usable,
                "wanted_symbol_count": wanted,
                "missing_usable_symbol_count": missing,
                "blocks_trading": False,
            }

        financial_view = evidence_domain_view("financial")
        symbol_news_view = evidence_domain_view("symbol_news")
        market_news_view = evidence_domain_view("market_news")
        investor_flow_view = evidence_domain_view("investor_flow")
        supplied_like = {"supplied", "success", "complete"}
        partial_like = {"partial", "cache_exists_zero_usable_articles"}
        domain_statuses = {
            financial_view["status"],
            symbol_news_view["status"],
            market_news_view["status"],
            investor_flow_view["status"],
        }
        if domain_statuses <= supplied_like:
            evidence_collection_status = "success"
        elif domain_statuses & (supplied_like | partial_like):
            evidence_collection_status = "partial"
        else:
            evidence_collection_status = "not_supplied"

        return {
            "account": {
                "full_account": {
                    "scope": "full_account_total_asset",
                    "label": "전체 계좌 총자산(KIS 잔고, 국내외 전 상품 합산)",
                    "total_asset_amount": account_asset_summary.get("total_asset_amount"),
                    "available": account_asset_summary.get("total_asset_amount") is not None,
                    "source_field": "account_asset_summary.total_asset_amount",
                    "source_api": account_asset_summary.get("source_api"),
                    "observed_at": account_asset_summary.get("observed_at"),
                },
                "domestic_trading_account": {
                    "scope": "domestic_trading_account_valuation",
                    "label": "국내 주식 매매계좌 총평가금액",
                    "total_evaluation_amount": account_display_summary.get("total_evaluation_amount"),
                    "securities_valuation_amount": account_display_summary.get("securities_valuation_amount"),
                    "orderable_cash_amount": account_display_summary.get("orderable_cash_amount"),
                    "total_pnl_amount": account_display_summary.get("total_pnl_amount"),
                    "available": account_display_summary.get("total_evaluation_amount") is not None,
                    "source_field": "account_display_summary.total_evaluation_amount",
                    "snapshot_generated_at": account.get("generated_at") or account.get("started_at"),
                    "source_artifact": "account-before-order.json",
                },
            },
            "orders": {
                "active": {
                    "scope": "currently_active_orders",
                    "label": "현재 활성 미체결/예약 주문(lifecycle 확인 필요)",
                    "count": active_count,
                    "lookup_status": active_lookup_status,
                    "source_field": "order_lifecycle.active_order_count (only when lookup_complete is true)",
                },
                "current_run_submitted": {
                    "scope": "current_run_submitted_orders",
                    "label": "이번 run에서 제출 확정된 주문(당일 전체 제출 이력이 아님)",
                    "count": current_run_submitted_count,
                    "source_field": "execution.orders[].result==submitted",
                },
                "history_or_reservation_rows": {
                    "scope": "history_reservation_raw_rows",
                    "label": "계좌 조회에 포함된 미체결/예약 이력 원본 행 수(참고용, 활성 주문 수와 다를 수 있음)",
                    "raw_row_count": raw_active_order_row_count,
                    "source_field": "account-before-order.json.active_orders (raw, compatibility reference only)",
                },
            },
            "evidence_domains": {
                "financial": financial_view,
                "symbol_news": symbol_news_view,
                "market_news": market_news_view,
                "investor_flow": investor_flow_view,
            },
            "run_status": {
                "execution": execution_status or "unknown",
                "account_collection": account_collection_status or "unknown",
                "evidence_collection": evidence_collection_status,
                "pipeline_summary": pipeline_status or "unknown",
                "delivery": "not_observed_at_summary_build_time",
                "order_gate": order_gate_status or "unknown",
            },
        }

    def adopt_existing_run_identity(self) -> None:
        run = load_json_if_exists(self.run_path) or {}
        if not isinstance(run, dict):
            return
        self.run_id = str(run.get("run_id") or self.run_id)
        self.started_at = str(run.get("started_at") or self.started_at)

    def build_review_summary(self, account: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
        judge_review = load_json_if_exists(self.output_dir / "judge-review.json") or {}
        account_by_symbol = {symbol_key(item): item for item in account.get("symbols", []) if isinstance(item, dict)}
        execution_by_symbol = {symbol_key(item): item for item in execution.get("orders", []) if isinstance(item, dict)}
        rows: list[dict[str, Any]] = []
        for item in judge_review.get("symbols", []) if isinstance(judge_review, dict) else []:
            if not isinstance(item, dict):
                continue
            symbol_id = symbol_key(item)
            if not symbol_id:
                continue
            account_item = account_by_symbol.get(symbol_id, {})
            execution_item = execution_by_symbol.get(symbol_id, {})
            current_qty = as_int(account_item.get("current_live_holding_quantity"))
            final_qty = non_negative_int_value(item.get("final_holding_quantity"))
            if final_qty is None:
                continue
            delta = final_qty - current_qty
            # expected_holding_quantity = current + pending buy - pending sell (the baseline canonical_action
            # was actually decided against); it can differ from current_live_holding_quantity when active
            # orders are already in flight, so delta_quantity (current->final) is a position-change summary,
            # not the same axis canonical_action was computed on.
            # Prefer the execution row's own expected_holding_quantity (computed against the same
            # active-order snapshot execute_orders actually decided against). When execution has no
            # row for this symbol yet (or an incomplete one), fall back to account current + pending
            # buy - pending sell so every resolved Judge row still gets a baseline value.
            expected_holding_quantity = execution_item.get("expected_holding_quantity")
            if expected_holding_quantity is None:
                expected_holding_quantity = (
                    current_qty
                    + as_int(account_item.get("pending_and_reserved_buy_quantity"))
                    - as_int(account_item.get("pending_and_reserved_sell_quantity"))
                )
            order_reason = execution_item.get("reason") or ""
            rows.append(
                {
                    "symbol_id": symbol_id,
                    "symbol_name": item.get("symbol_name") or account_item.get("symbol_name") or symbol_id,
                    "current_live_holding_quantity": current_qty,
                    "expected_holding_quantity": as_int(expected_holding_quantity) if expected_holding_quantity is not None else None,
                    "requested_target_position_value_krw": item.get("requested_target_position_value_krw"),
                    "target_position_value_krw": item.get("target_position_value_krw"),
                    "final_holding_quantity": final_qty,
                    "delta_quantity": delta,
                    "position_change_quantity": delta,
                    "relative_attractiveness_rank": as_int(item.get("relative_attractiveness_rank")),
                    "requested_action": item.get("requested_action") or "hold",
                    "canonical_action": item.get("canonical_action") or "hold",
                    "decision_basis": item.get("decision_basis") or "none",
                    "reason_code": item.get("reason_code") or "",
                    "one_line_reason": item.get("one_line_reason") or "",
                    "order_result": execution_item.get("result") or "",
                    "order_direction": execution_item.get("direction") or "none",
                    "order_reason": order_reason,
                    "is_lifecycle_only_order": order_reason in LIFECYCLE_ONLY_ORDER_REASONS,
                    "order_quantity": as_int(execution_item.get("validated_order_quantity")),
                    "requested_order_quantity": as_int(execution_item.get("requested_order_quantity")),
                    "quantity_adjustment": execution_item.get("quantity_adjustment") if isinstance(execution_item.get("quantity_adjustment"), dict) else {},
                    "order_or_reservation_id": execution_item.get("order_or_reservation_id") or "",
                    "broker_reconciliation": execution_item.get("broker_reconciliation")
                    if isinstance(execution_item.get("broker_reconciliation"), dict)
                    else {},
                }
            )
        submitted = [item for item in rows if item.get("order_result") == "submitted"]
        new_order_submitted = [item for item in submitted if not item.get("is_lifecycle_only_order")]
        lifecycle_only_submitted = [item for item in submitted if item.get("is_lifecycle_only_order")]
        spec = load_json_if_exists(self.output_dir / "judge-review-spec.json") or {}
        review_scope_reasons = spec.get("review_scope_reasons") if isinstance(spec.get("review_scope_reasons"), dict) else {}
        # final_*_count below is a current->final POSITION CHANGE summary (existing/back-compat field
        # names), never fold unresolved/invalid Judge decisions into 유지(hold).
        final_sell_count = sum(1 for row in rows if row["delta_quantity"] < 0)
        final_buy_count = sum(1 for row in rows if row["delta_quantity"] > 0)
        final_hold_count = sum(1 for row in rows if row["delta_quantity"] == 0)
        # canonical_*_count is the separate axis: the new-order DECISION Judge made against
        # expected_holding_quantity (current + pending buy - pending sell), which can differ from the
        # current->final position-change counts above when active orders are already in flight.
        # canonical_action (derive_action) is increase|hold|reduce|exit, NOT buy/sell/hold — counting
        # against buy/sell silently zeroes every row (reduce/exit never equal "sell").
        canonical_increase_count = sum(1 for row in rows if row.get("canonical_action") == "increase")
        canonical_reduce_count = sum(1 for row in rows if row.get("canonical_action") == "reduce")
        canonical_exit_count = sum(1 for row in rows if row.get("canonical_action") == "exit")
        canonical_hold_count = sum(1 for row in rows if row.get("canonical_action") == "hold")
        # Deliberate combined view for callers that only care about "any reduction direction".
        canonical_reduce_or_exit_count = canonical_reduce_count + canonical_exit_count
        resolved_symbol_ids = {str(row.get("symbol_id") or "") for row in rows}
        # judge-review.json errors carry "{symbol_id}: message" text with a machine code (e.g.
        # missing_judge_symbol, duplicate_judge_symbol); surface that code per unresolved symbol so
        # an in-scope missing/duplicate/invalid Judge result is never described as "not shortlisted".
        judge_review_errors = judge_review.get("errors") if isinstance(judge_review.get("errors"), list) else []
        error_code_by_symbol: dict[str, str] = {}
        for error_item in judge_review_errors:
            if not isinstance(error_item, dict):
                continue
            message = str(error_item.get("message") or "")
            candidate_symbol_id = message.split(":", 1)[0].strip()
            if candidate_symbol_id and candidate_symbol_id not in error_code_by_symbol:
                error_code_by_symbol[candidate_symbol_id] = str(error_item.get("code") or "")
        analyst_review = load_json_if_exists(self.output_dir / "analyst-review.json") or {}
        analyst_name_by_symbol = {
            symbol_key(item): item.get("symbol_name")
            for item in (analyst_review.get("symbols") if isinstance(analyst_review.get("symbols"), list) else [])
            if isinstance(item, dict) and symbol_key(item) and item.get("symbol_name")
        }
        unresolved_review_scope: list[dict[str, Any]] = []
        for symbol_id, scope_reason in review_scope_reasons.items():
            stripped_symbol_id = str(symbol_id).strip()
            if not stripped_symbol_id or stripped_symbol_id in resolved_symbol_ids:
                continue
            # An unheld (unheld_score_rank) unresolved symbol commonly has no account holding
            # row at all, so account_by_symbol has nothing to name it with; fall back to the
            # Analyst review's symbol_name so it isn't rendered blank.
            symbol_name = str((account_by_symbol.get(stripped_symbol_id) or {}).get("symbol_name") or "")
            if not symbol_name:
                symbol_name = str(analyst_name_by_symbol.get(stripped_symbol_id) or "")
            unresolved_review_scope.append(
                {
                    "symbol_id": stripped_symbol_id,
                    "symbol_name": symbol_name,
                    "scope_reason": str(scope_reason or ""),
                    "judge_error_code": error_code_by_symbol.get(stripped_symbol_id, ""),
                }
            )
        unresolved_review_scope_count = len(unresolved_review_scope)
        scored_symbol_ids = {
            symbol_key(item)
            for item in (analyst_review.get("symbols") if isinstance(analyst_review.get("symbols"), list) else [])
            if isinstance(item, dict)
            and symbol_key(item)
            and (score := as_float(item.get("final_first_score"))) is not None
            and 0 <= score <= 10
        }
        return {
            "status": judge_review.get("status"),
            "symbol_count": len(rows),
            "submitted_order_count": len(submitted),
            "new_order_submitted_count": len(new_order_submitted),
            "lifecycle_only_submitted_count": len(lifecycle_only_submitted),
            "final_sell_count": final_sell_count,
            "final_buy_count": final_buy_count,
            "final_hold_count": final_hold_count,
            "canonical_increase_count": canonical_increase_count,
            "canonical_reduce_count": canonical_reduce_count,
            "canonical_exit_count": canonical_exit_count,
            "canonical_reduce_or_exit_count": canonical_reduce_or_exit_count,
            "canonical_hold_count": canonical_hold_count,
            "unresolved_review_scope_count": unresolved_review_scope_count,
            "unresolved_review_scope": unresolved_review_scope,
            "held_review_scope_count": sum(1 for reason in review_scope_reasons.values() if reason == "held_position"),
            "active_order_review_scope_count": sum(1 for reason in review_scope_reasons.values() if reason == "active_order"),
            "unheld_review_scope_count": sum(
                1 for reason in review_scope_reasons.values() if reason in {"symbol_news", "unheld_score_rank"}
            ),
            "hold_symbol_count": len(scored_symbol_ids - set(review_scope_reasons)),
            "symbols": rows,
        }

    def evidence_domain_or_fallback(self, reporting_domains: dict[str, Any], evidence_summary: dict[str, Any], key: str) -> dict[str, Any]:
        view = reporting_domains.get(key) if isinstance(reporting_domains, dict) else None
        if isinstance(view, dict):
            return {
                "status": view.get("status"),
                "display_text": view.get("coverage_text"),
                "missing_usable_symbol_count": view.get("missing_usable_symbol_count"),
            }
        raw = evidence_summary.get(key) if isinstance(evidence_summary.get(key), dict) else {}
        counts = raw.get("cache_counts") if isinstance(raw.get("cache_counts"), dict) else {}
        return {
            "status": raw.get("status"),
            "display_text": raw.get("display_text"),
            "missing_usable_symbol_count": counts.get("missing_usable_symbol_count"),
        }

    def write_portfolio_report(self, summary: dict[str, Any]) -> Path:
        path = self.report_path()
        execution = summary.get("execution") if isinstance(summary.get("execution"), dict) else {}
        review = summary.get("review_summary") if isinstance(summary.get("review_summary"), dict) else {}
        decision = summary.get("decision_brief") if isinstance(summary.get("decision_brief"), dict) else {}
        token_total = (((summary.get("token_usage") or {}).get("total") or {}).get("total_tokens")) if isinstance(summary.get("token_usage"), dict) else 0
        decision_brief = load_json_if_exists(self.output_dir / "decision-brief.json") or {}
        analyst_review = load_json_if_exists(self.output_dir / "analyst-review.json") or {}
        execution_full = load_json_if_exists(self.output_dir / "execution.json") or {}
        account_full = load_json_if_exists(self.output_dir / "account-before-order.json") or {}
        price_chart = load_json_if_exists(self.output_dir / "price-chart.json") or {}
        evidence_summary = summary.get("evidence_summary") if isinstance(summary.get("evidence_summary"), dict) else {}
        reporting_view = summary.get("reporting_view") if isinstance(summary.get("reporting_view"), dict) else {}
        reporting_account = reporting_view.get("account") if isinstance(reporting_view.get("account"), dict) else {}
        reporting_orders = reporting_view.get("orders") if isinstance(reporting_view.get("orders"), dict) else {}
        reporting_domains = reporting_view.get("evidence_domains") if isinstance(reporting_view.get("evidence_domains"), dict) else {}
        full_account_view = reporting_account.get("full_account") if isinstance(reporting_account.get("full_account"), dict) else {}
        domestic_account_view = (
            reporting_account.get("domestic_trading_account") if isinstance(reporting_account.get("domestic_trading_account"), dict) else {}
        )
        active_order_view = reporting_orders.get("active") if isinstance(reporting_orders.get("active"), dict) else {}
        # Fall back to legacy raw account_summary only when the normalized domestic view is
        # absent entirely (older pipeline-summary.json artifacts). When the normalized view is
        # present but explicitly unavailable, keep its (unknown) values instead of masking that
        # state with conflicting legacy numbers.
        legacy_account = summary.get("account_summary") if isinstance(summary.get("account_summary"), dict) else {}
        account = domestic_account_view if domestic_account_view else legacy_account
        legacy_asset = summary.get("account_asset_summary") if isinstance(summary.get("account_asset_summary"), dict) else {}
        full_account_amount = full_account_view.get("total_asset_amount") if full_account_view else legacy_asset.get("total_asset_amount")
        stages = summary.get("stages") if isinstance(summary.get("stages"), list) else []
        stage_by_name = {item.get("stage"): item for item in stages if isinstance(item, dict)}
        active_order_lookup_performed = account_full.get("active_order_lookup_performed")
        order_available_lookup_performed = account_full.get("order_available_lookup_performed")
        if active_order_view:
            active_order_count_text = (
                f"{as_int(active_order_view.get('count'))}건"
                if active_order_view.get("lookup_status") == "complete"
                else "미조회"
            )
        else:
            active_order_count_text = (
                f"{len(account_full.get('active_orders', [])) if isinstance(account_full.get('active_orders'), list) else 0}건"
                if active_order_lookup_performed is True
                else "미조회"
            )
        order_reservation_check = (
            account_full.get("active_order_checks", {}).get("order_resv_ccnl", "")
            if isinstance(account_full.get("active_order_checks"), dict)
            else ""
        )
        if active_order_lookup_performed is not True and not order_reservation_check:
            order_reservation_check = "미조회"
        today_trade_summary = summary.get("today_trade_summary") if isinstance(summary.get("today_trade_summary"), dict) else {}

        lines = [
            f"# 포트폴리오 평결문 - {report_date_from(self.started_at)}",
            "",
            "## 실행 정보",
            f"- run_id: {summary.get('run_id', '')}",
            f"- 작업 시작: {summary.get('started_at', '')}",
            f"- 환경: {account_full.get('execution_environment') or self.args.env}",
            f"- 최종 상태: {summary.get('status', '')}",
            f"- 실행 디렉터리: {summary.get('run_dir', '')}",
            "",
            "## 1. 수집 상태",
            "| 도메인 | 상태 | 대상/기사 수 | 누락/오류 수 | 핵심 오류 |",
            "|---|---|---:|---:|---|",
        ]
        for domain, stage_name, evidence_key in (
            ("시장", "main-evidence", None),
            ("재무", "financial-cache", "financial"),
            ("종목뉴스", "symbol-news-cache", "symbol_news"),
            ("시장뉴스", "news-context", "market_news"),
            ("수급", "", "investor_flow"),
        ):
            stage = stage_by_name.get(stage_name, {}) if stage_name else {}
            detail = stage.get("detail", "")
            status_text = stage.get("status", "")
            error_count = 0
            if domain == "시장":
                error_count = count_symbol_errors(price_chart)
            elif evidence_key:
                domain_view = self.evidence_domain_or_fallback(reporting_domains, evidence_summary, evidence_key)
                error_count = as_int(domain_view.get("missing_usable_symbol_count"))
                detail = domain_view.get("display_text") or detail
                if not status_text:
                    status_text = domain_view.get("status") or ""
            total_count = (summary.get("portfolio_counts") or {}).get("universe", 0)
            if evidence_key == "market_news":
                market_summary = evidence_summary.get("market_news") if isinstance(evidence_summary.get("market_news"), dict) else {}
                total_count = as_int(market_summary.get("article_count"))
            lines.append(f"| {domain} | {status_text} | {total_count} | {error_count} | {md_cell(detail)} |")

        lines.extend(
            [
                "",
                "## 2. 평결 제외 종목",
                "| 종목식별자 | 종목명 | 제외 사유 | 누락 필수 정보 |",
                "|---|---|---|---|",
            ]
        )
        excluded_count = 0
        for item in decision_brief.get("symbols", []) if isinstance(decision_brief, dict) else []:
            if not isinstance(item, dict) or item.get("eligible_for_review", True):
                continue
            excluded_count += 1
            lines.append(
                f"| {md_cell(item.get('symbol_id'))} | {md_cell(item.get('symbol_name'))} | "
                f"{md_cell(', '.join(item.get('exclusion_reasons', [])) if isinstance(item.get('exclusion_reasons'), list) else item.get('exclusion_reasons'))} | "
                f"{md_cell(', '.join(item.get('required_missing', [])) if isinstance(item.get('required_missing'), list) else item.get('required_missing'))} |"
            )
        if excluded_count == 0:
            lines.append("| - | - | 없음 | - |")

        today_rows = today_trade_summary.get("symbols") if isinstance(today_trade_summary.get("symbols"), list) else []
        lines.extend(
            [
                "",
                "## 2-1. 당일 거래 타임라인 요약",
                f"- 수집 상태: {today_trade_summary.get('collection_status') or 'unavailable'} · 체결 없음 확인 {as_int(today_trade_summary.get('confirmed_no_trade_symbol_count'))}종목 · 미확인 {as_int(today_trade_summary.get('unknown_symbol_count'))}종목",
                f"- 당일 체결 타임라인 확인 종목 수: {as_int(today_trade_summary.get('symbol_count_with_fills'))}",
                "- 누계 금액은 이번 명령 발생 거래로 해석하지 않고, 체결 타임라인은 당일 거래 맥락으로만 사용",
                "| 종목식별자 | 종목명 | 마지막 방향 | 마지막 체결시각 | 마지막 체결가 | 순수량 | 반대거래 발생 |",
                "|---|---|---|---|---:|---:|---|",
            ]
        )
        if today_rows:
            for item in today_rows[:10]:
                if isinstance(item, dict):
                    lines.append(
                        f"| {md_cell(item.get('symbol_id'))} | {md_cell(item.get('symbol_name'))} | {md_cell(item.get('last_direction'))} | "
                        f"{md_cell(item.get('last_fill_at'))} | {as_int(item.get('last_fill_price'))} | {as_int(item.get('net_quantity'))} | "
                        f"{'yes' if item.get('has_intraday_reversal') else 'no'} |"
                    )
        else:
            lines.append("| - | - | - | - | 0 | 0 | no |")

        price_only_count = 0
        for item in decision_brief.get("symbols", []) if isinstance(decision_brief, dict) else []:
            if isinstance(item, dict) and item.get("evidence_mode") == "price-only":
                price_only_count += 1
        lines.extend(
            [
                "",
                "## 3. `decision-brief.json` 요약",
                f"- `decision-brief.json` 생성 여부: {'yes' if decision else 'no'}",
                f"- 포함된 eligible 종목 수: {sum(1 for item in decision_brief.get('symbols', []) if isinstance(item, dict) and item.get('eligible_for_review', False)) if isinstance(decision_brief, dict) else 0}",
                f"- price-only eligible 종목 수: {price_only_count}",
                "- 제외된 raw payload / 기사 원문 / 민감정보: yes",
                f"- 핵심 누락 또는 오류: {decision.get('error_count', 0)}건",
                "",
                "## 4. `analyst-review` 독립 평결",
                "| 종목식별자 | 종목명 | 최종점수(원점수 평균, 0-10) | 유효 응답 수 | role별 점수 | 핵심 근거 | 핵심 리스크 |",
                "|---|---|---:|---:|---|---|---|",
            ]
        )
        for item in analyst_review.get("symbols", []) if isinstance(analyst_review, dict) else []:
            if not isinstance(item, dict):
                continue
            agent_scores = item.get("agent_scores") if isinstance(item.get("agent_scores"), list) else []
            aggregation_scores = [
                score
                for score in agent_scores
                if isinstance(score, dict) and not score.get("excluded_from_aggregation")
            ]
            role_details = []
            for score in agent_scores:
                if not isinstance(score, dict):
                    continue
                detail = f"{score.get('agent_role', '')}: {score.get('score', '')}"
                if score.get("excluded_from_aggregation"):
                    detail = f"{detail}(평균 제외)"
                role_details.append(detail)
            reasons = [str(score.get("one_line_reason", "")) for score in agent_scores if isinstance(score, dict) and score.get("one_line_reason")]
            lines.append(
                f"| {md_cell(item.get('symbol_id'))} | {md_cell(item.get('symbol_name'))} | {item.get('final_first_score', '')} | {len(aggregation_scores)} | "
                f"{md_cell('; '.join(role_details))} | {md_cell('; '.join(reasons[:2]))} | - |"
            )

        lines.extend(
            [
                "",
                "## 5. `judge-review` 포트폴리오 평결",
                "- 최종 포트폴리오 판단: `judge` 목표 보유금액을 기준으로 Main/pipeline이 최종 보유수량 산출",
                "- 잔여 현금 처리: 목표현금을 별도 판단값으로 만들지 않고 최종 보유수량 산출 후 남는 금액으로만 기록",
                f"- Main agent 검증 결과: {execution.get('status', '')}",
                "",
                "| 종목식별자 | 종목명 | 현재 보유수량 | 목표 보유금액 | 최종 보유수량 | 상대매력도 | 판단 코드 | 한 줄 판단 |",
                "|---|---|---:|---:|---:|---:|---|---|",
            ]
        )
        for item in review.get("symbols", []) if isinstance(review.get("symbols"), list) else []:
            lines.append(
                f"| {md_cell(item.get('symbol_id'))} | {md_cell(item.get('symbol_name'))} | {as_int(item.get('current_live_holding_quantity'))} | "
                f"{format_number(item.get('target_position_value_krw'))} | {as_int(item.get('final_holding_quantity'))} | {as_int(item.get('relative_attractiveness_rank'))} | "
                f"{md_cell(item.get('reason_code'))} | {md_cell(item.get('one_line_reason'))} |"
            )

        submitted_orders = [item for item in execution.get("orders", []) if isinstance(item, dict) and item.get("result") == "submitted"]
        result_orders = [
            item
            for item in execution.get("orders", [])
            if isinstance(item, dict)
            and (
                item.get("result") == "submitted"
                or isinstance(item.get("quantity_adjustment"), dict)
                or str(item.get("reason") or "").startswith(("buy_quantity_", "sell_quantity_", "buy_cash_gate_"))
            )
        ]
        lines.extend(
            [
                "",
                "## 6. 최신 계좌 검증",
                f"- 전체 계좌 총자산(KIS 잔고, 국내외 전 상품 합산): {format_number(full_account_amount)}원"
                if full_account_amount is not None
                else "- 전체 계좌 총자산(KIS 잔고, 국내외 전 상품 합산): 조회 안됨",
                f"- 국내 매매계좌 총평가금액: {format_number(account.get('total_evaluation_amount'))}원",
                f"- 주문가능금액: {format_number(account.get('orderable_cash_amount'))}원",
                f"- 주식평가: {format_number(account.get('securities_valuation_amount'))}원",
                f"- 평가손익: {format_signed_number(account.get('total_pnl_amount'))}원",
                f"- 주문 전 기존 미체결/예약 주문 조회: {bool_status(active_order_lookup_performed)}",
                f"- 주문가능 조회: {bool_status(order_available_lookup_performed)}",
                f"- 주문 전 기존 미체결/예약 주문: {active_order_count_text}",
                f"- 예약 주문 확인: {order_reservation_check}",
                "- 당일 체결: 계좌 요약에 반영된 스냅샷 기준",
                "",
                "## 7. 주문 전 기존 미체결/예약 주문 조정",
                "| 종목식별자 | 종목명 | 기존 주문번호 | 구분 | 방향 | 잔여수량 | 가격 | 주문 API | 경로 | 조치 | 사유 | 결과 | 확인 상태 | 대체 주문번호 |",
                "|---|---|---|---|---|---:|---:|---|---|---|---|---|---|---|",
            ]
        )
        adjustments = execution_full.get("order_adjustments") if isinstance(execution_full.get("order_adjustments"), list) else []
        if adjustments:
            for item in adjustments:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    f"| {md_cell(item.get('symbol_id'))} | {md_cell(item.get('symbol_name'))} | {md_cell(item.get('existing_order_id'))} | "
                    f"{md_cell(item.get('existing_order_kind'))} | {md_cell(item.get('direction'))} | {as_int(item.get('remaining_quantity'))} | "
                    f"{as_int(item.get('order_price'))} | {md_cell(item.get('order_api'))} | {md_cell(item.get('order_path'))} | "
                    f"{md_cell(item.get('action'))} | {md_cell(item.get('reason'))} | {md_cell(item.get('result'))} | "
                    f"{md_cell(item.get('confirmed_status'))} | {md_cell(item.get('replacement_order_id'))} |"
                )
        else:
            if active_order_lookup_performed is True:
                lines.append("| - | - | - | - | - | 0 | 0 | - | - | none | 기존 조정 대상 없음 | skipped | - | - |")
            else:
                lines.append("| - | - | - | - | - | 0 | 0 | - | - | refresh_required | 주문 전 기존 미체결/예약 주문 미조회 | blocked_until_refreshed | 미조회 | - |")

        lines.extend(
            [
                "",
                "## 8. 최종 주문 목록",
                "| 종목식별자 | 종목명 | 방향 | 현재 실시간 보유수량 | 미체결·예약 매수 | 미체결·예약 매도 | 예상 보유수량 | 최종 보유수량 | 요청수량 | 검증수량 | 추가 필요수량 | 수량조정 | 결과 |",
                "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
            ]
        )
        for item in execution_full.get("orders", []) if isinstance(execution_full, dict) else []:
            if not isinstance(item, dict):
                continue
            adjustment = item.get("quantity_adjustment") if isinstance(item.get("quantity_adjustment"), dict) else {}
            requested_qty = as_int(item.get("requested_order_quantity")) or as_int(item.get("validated_order_quantity"))
            adjustment_text = ""
            if adjustment:
                adjustment_text = f"{as_int(adjustment.get('from'))}->{as_int(adjustment.get('to'))} ({adjustment.get('reason', '')})"
            lines.append(
                f"| {md_cell(item.get('symbol_id'))} | {md_cell(item.get('symbol_name'))} | {md_cell(item.get('direction'))} | "
                f"{as_int(item.get('current_live_holding_quantity'))} | {as_int(item.get('pending_and_reserved_buy_quantity'))} | "
                f"{as_int(item.get('pending_and_reserved_sell_quantity'))} | {as_int(item.get('expected_holding_quantity'))} | "
                f"{as_int(item.get('final_holding_quantity'))} | {requested_qty} | {as_int(item.get('validated_order_quantity'))} | "
                f"{as_int(item.get('additional_required_quantity'))} | {md_cell(adjustment_text or '-')} | {md_cell(item.get('result'))} |"
            )

        lines.extend(
            [
                "",
                "## 9. 실행 결과",
                f"- 요청 유형: {execution.get('request_type', '')}",
                f"- 실제 제출 여부: {'yes' if submitted_orders else 'no'}",
                f"- 제출된 주문번호 또는 예약번호: {', '.join(str(item.get('order_or_reservation_id') or '') for item in submitted_orders if item.get('order_or_reservation_id')) or '-'}",
                "- 취소/정정 요청번호: -",
                "- 취소/정정 확인 상태: -",
                f"- 실패 또는 보류 사유: {md_cell('; '.join(error.get('message', '') for error in execution.get('errors', []) if isinstance(error, dict))) if isinstance(execution.get('errors'), list) else '-'}",
                "| 종목 | 방향 | 요청수량 | 제출수량 | 결과 | 사유 | 수량조정 | 예약/주문번호 |",
                "|---|---|---:|---:|---|---|---|---|",
            ]
        )
        for item in result_orders:
            symbol_name = md_cell(f"{item.get('symbol_id', '')} {item.get('symbol_name', '')}".strip())
            adjustment = item.get("quantity_adjustment") if isinstance(item.get("quantity_adjustment"), dict) else {}
            requested_qty = as_int(item.get("requested_quantity")) or as_int(item.get("quantity"))
            adjustment_text = ""
            if adjustment:
                adjustment_text = f"{as_int(adjustment.get('from'))}->{as_int(adjustment.get('to'))} ({adjustment.get('reason', '')})"
            lines.append(
                f"| {symbol_name} | {md_cell(item.get('direction'))} | {requested_qty} | {as_int(item.get('quantity'))} | {md_cell(item.get('result'))} | "
                f"{md_cell(item.get('reason'))} | {md_cell(adjustment_text or '-')} | {md_cell(item.get('order_or_reservation_id') or '-')} |"
            )
        lines.extend(
            [
                "",
                "## 10. 아티팩트",
                f"- 실행 디렉터리: {summary.get('run_dir', '')}",
                f"- 보존된 partial / failed 아티팩트: {sum(1 for item in stages if isinstance(item, dict) and item.get('status') in {'partial', 'failed'})}",
                f"- pipeline-summary.json: {summary.get('summary_path', '')}",
                f"- decision-brief.json: {(summary.get('artifacts') or {}).get('decision_brief', '')}",
                f"- judge-review.json: {(summary.get('artifacts') or {}).get('judge_review', '')}",
                f"- execution.json: {(summary.get('artifacts') or {}).get('execution', '')}",
                f"- 총 사용 토큰: {format_number(token_total)}",
                "",
                "## 11. 메모",
                "- 당일 체결수량은 현재 보유수량에 이미 반영된 값으로 보고 다시 차감하지 않음",
                "- `judge-review`는 단일 `judge` 목표 보유금액을 사용하며 Main/pipeline이 가격 기준 반올림 수량을 산출함",
                "- 목표금액 초과분만을 이유로 매수 수량을 줄이지 않으며 주문가능금액, active 주문, same-day, account-order 검증은 기존 실행 단계에서 처리함",
                "- 투자 권유가 아니라 의사결정 보조 분석입니다.",
            ]
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def write_telegram_summary(self) -> Path:
        path = self.output_dir / "telegram-summary.txt"
        result = self.run_cmd(
            "telegram-summary",
            [
                sys.executable,
                self.telegram_summary_script(),
                "--summary",
                str(self.summary_path),
                "--output",
                str(path),
            ],
            required=False,
        )
        self.stages = [item for item in self.stages if item.get("stage") != "telegram-summary"]
        if result.returncode != 0:
            self.add_stage("telegram-summary", "partial", required=False, detail=compact_text(result.stderr or result.stdout), path=path)
        else:
            self.add_stage("telegram-summary", "success", required=False, detail="rendered telegram-summary.txt", path=path)
        return path

    def write_html_report(self) -> Path:
        path = self.output_dir / "daily-trading-report.html"
        result = self.run_cmd(
            "html-report",
            [
                sys.executable,
                self.html_report_script(),
                "--runs-root",
                str(self.output_dir.parent),
                "--target-run",
                self.output_dir.name,
                "--output",
                str(path),
            ],
            required=False,
        )
        self.stages = [item for item in self.stages if item.get("stage") != "html-report"]
        if result.returncode != 0:
            path.unlink(missing_ok=True)
            self.add_stage("html-report", "partial", required=False, detail=compact_text(result.stderr or result.stdout), path=path)
        else:
            self.add_stage("html-report", "success", required=False, detail="rendered daily-trading-report.html", path=path)
        return path

    def build_summary(self, portfolio: dict[str, Any]) -> dict[str, Any]:
        token_summary = load_json_if_exists(self.output_dir / "token-summary.json") or {}
        execution = load_json_if_exists(self.output_dir / "execution.json") or {}
        account = load_json_if_exists(self.output_dir / "account-before-order.json") or {}
        account_asset_snapshot = load_json_if_exists(self.output_dir / "account-asset-snapshot.json") or {}
        today_fills = load_json_if_exists(self.output_dir / "today-fills.json") or {}
        order_lifecycle = load_json_if_exists(self.output_dir / "order-lifecycle.json") or {}
        decision_brief = load_json_if_exists(self.output_dir / "decision-brief.json") or {}
        orders = []
        for item in execution.get("orders", []) if isinstance(execution, dict) else []:
            if not isinstance(item, dict):
                continue
            orders.append(
                {
                    "symbol_id": item.get("symbol_id"),
                    "symbol_name": item.get("symbol_name"),
                    "direction": item.get("direction"),
                    "quantity": item.get("validated_order_quantity"),
                    "requested_quantity": item.get("requested_order_quantity"),
                    "quantity_adjustment": item.get("quantity_adjustment") if isinstance(item.get("quantity_adjustment"), dict) else {},
                    "result": item.get("result"),
                    "reason": item.get("reason"),
                    "order_or_reservation_id": item.get("order_or_reservation_id"),
                    "broker_reconciliation": item.get("broker_reconciliation")
                    if isinstance(item.get("broker_reconciliation"), dict)
                    else {},
                }
            )
        stages = self.load_summary_stages()
        self.stages = stages
        review_summary = self.build_review_summary(account, execution)
        symbols = normalize_symbol_ids(portfolio.get("universe"))
        account_summary = account.get("account_summary") if isinstance(account.get("account_summary"), dict) else {}
        account_display_summary = self.build_account_display_summary(account_summary)
        account_collection_status = str(account.get("status") or "unavailable")
        order_gate_status = str(account.get("order_gate_status") or "").strip()
        if order_gate_status not in {"not_run", "success", "failed", "not_required"}:
            if account.get("active_order_lookup_performed") is True and account.get("order_available_lookup_performed") is True:
                order_gate_status = "success"
            elif str(execution.get("request_type") or "") in {"demo-submit", "real-submit"} and execution.get("requires_main_agent_order_execution"):
                order_gate_status = "not_run"
            else:
                order_gate_status = "not_required"
        account_asset_summary = self.build_account_asset_summary(account_asset_snapshot if isinstance(account_asset_snapshot, dict) else {})
        today_trade_summary = self.build_today_trade_summary(decision_brief if isinstance(decision_brief, dict) else {})
        evidence_summary = self.build_evidence_summary(decision_brief, stages, symbols)
        pipeline_status = summarized_status(stages, execution)
        order_lifecycle_view = {
            "status": order_lifecycle.get("status") or "not_run",
            "lookup_complete": bool(order_lifecycle.get("lookup_complete")),
            "active_order_count": int(order_lifecycle.get("active_order_count") or 0),
            "previous_submitted_cash_order_count": int(
                order_lifecycle.get("previous_submitted_cash_order_count") or 0
            ),
            "holding_state_issue_count": int(order_lifecycle.get("holding_state_issue_count") or 0),
            "holding_state_issues": order_lifecycle.get("holding_state_issues", [])[:5]
            if isinstance(order_lifecycle.get("holding_state_issues"), list)
            else [],
        }
        reporting_view = self.build_reporting_view(
            account_display_summary=account_display_summary,
            account_asset_summary=account_asset_summary,
            account=account,
            order_lifecycle_view=order_lifecycle_view,
            current_run_orders=orders,
            evidence_summary=evidence_summary,
            account_collection_status=account_collection_status,
            order_gate_status=order_gate_status,
            execution_status=str(execution.get("status") or ""),
            pipeline_status=pipeline_status,
        )
        report_path = self.report_path()
        telegram_summary_path = self.output_dir / "telegram-summary.txt"
        html_report_path = self.output_dir / "daily-trading-report.html"
        summary = {
            "schema_version": "1",
            "run_id": self.run_id,
            "started_at": self.started_at,
            "status": pipeline_status,
            "run_dir": str(self.output_dir),
            "summary_path": str(self.summary_path),
            "command_log_path": str(self.command_log_path),
            "report_path": str(report_path),
            "telegram_summary_path": str(telegram_summary_path),
            "html_report_path": str(html_report_path),
            "daily_trading_config": self.daily_trading_config_summary(),
            "stages": stages,
            "portfolio_except": normalize_symbol_ids(portfolio.get("portfolio_except")),
            "portfolio_counts": {
                "recommanded": len(normalize_symbol_ids(portfolio.get("recommanded"))),
                "recommended": len(normalize_symbol_ids(portfolio.get("recommended"))),
                "specified": len(normalize_symbol_ids(portfolio.get("specified"))),
                "holding": len(normalize_symbol_ids(portfolio.get("holding"))),
                "universe": len(normalize_symbol_ids(portfolio.get("universe"))),
            },
            "decision_brief": {
                "status": decision_brief.get("status"),
                "symbol_count": len(decision_brief.get("symbols", [])) if isinstance(decision_brief.get("symbols"), list) else 0,
                "error_count": len(decision_brief.get("errors", [])) if isinstance(decision_brief.get("errors"), list) else 0,
            },
            "review_summary": review_summary,
            "account_collection_status": account_collection_status,
            "order_gate_status": order_gate_status,
            "account_summary": account_summary,
            "account_display_summary": account_display_summary,
            "account_asset_summary": account_asset_summary,
            "today_fills_summary": {
                "status": today_fills.get("status"),
                "skipped": bool(today_fills.get("skipped")),
                "fill_scope": today_fills.get("fill_scope") or "universe",
                "fill_count": len(today_fills.get("fills", [])) if isinstance(today_fills.get("fills"), list) else 0,
            },
            "today_trade_summary": today_trade_summary,
            "order_lifecycle": order_lifecycle_view,
            "evidence_summary": evidence_summary,
            "reporting_view": reporting_view,
            "execution": {
                "status": execution.get("status"),
                "request_type": execution.get("request_type"),
                "order_path_selection": {
                    "requested": self.order_path_requested,
                    "resolved": self.order_path,
                    "reason": self.order_path_reason,
                },
                "order_count": len(orders),
                "orders": orders,
                "errors": execution.get("errors", [])[:5] if isinstance(execution.get("errors"), list) else [],
                "requires_main_agent_order_execution": bool(execution.get("requires_main_agent_order_execution")),
                "required_main_agent_actions": execution.get("required_main_agent_actions", [])
                if isinstance(execution.get("required_main_agent_actions"), list)
                else [],
                "broker_reconciliation": execution.get("broker_reconciliation")
                if isinstance(execution.get("broker_reconciliation"), dict)
                else {},
            },
            "token_usage": {
                "main": (token_summary.get("main") or {}).get("token_usage", zero_usage()),
                "subagents": (token_summary.get("subagents") or {}).get("token_usage", zero_usage()),
                "total": (token_summary.get("total") or {}).get("token_usage", zero_usage()),
            },
            "telegram_response_policy": {
                "source": "Send telegram-summary.txt as the short Telegram message and attach daily-trading-report.html when html_report_available is true.",
                "account_state_fields": [
                    "cash_amount",
                    "securities_valuation_amount",
                    "total_evaluation_amount",
                    "total_pnl_amount",
                ],
                "today_trade_amount_policy": "Show today_buy_amount/today_sell_amount and today_fills_summary.fill_count only under a separate 당일 거래 누계 label when relevant, explicitly marked as this run's pre-order snapshot (account-before-order.json / collection-time today-fills.json), never as values current at Telegram delivery time; never present them as newly caused by this command unless execution.json confirms submitted orders.",
                "gate_label": "주문 전 기존 미체결/예약 주문",
                "evidence_policy": "Report evidence_summary.financial.display_text, evidence_summary.symbol_news.display_text, evidence_summary.market_news.display_text, and evidence_summary.investor_flow.display_text; keep per-symbol KIS news distinct from market-wide stored news and distinguish missing cache from cache_exists_zero_usable_articles.",
                "review_policy": "Mention judge/judge-review outcome using review_summary.final_sell_count/final_buy_count/final_hold_count (final decisions derived from current->final holding quantity direction) and unresolved_review_scope_count; never present held_review_scope_count/unheld_review_scope_count/hold_symbol_count as final verdicts. Highlight submitted or final-quantity-changed symbols, including final holding quantity, requested_action/canonical_action, and one_line_reason when available.",
            },
            "artifacts": {
                "check_portfolio": str(self.output_dir / "check-portfolio.json"),
                "price_chart": str(self.output_dir / "price-chart.json"),
                "account_before_order": str(self.output_dir / "account-before-order.json"),
                "account_asset_snapshot": str(self.output_dir / "account-asset-snapshot.json"),
                "today_fills": str(self.output_dir / "today-fills.json"),
                "order_lifecycle": str(self.output_dir / "order-lifecycle.json"),
                "news_context": str(self.output_dir / "news-context.json"),
                "decision_brief": str(self.output_dir / "decision-brief.json"),
                "analyst_review": str(self.output_dir / "analyst-review.json"),
                "judge_review": str(self.output_dir / "judge-review.json"),
                "execution": str(self.output_dir / "execution.json"),
                "token_summary": str(self.output_dir / "token-summary.json"),
                "model_usage": str(self.model_usage_path),
                "portfolio_report": str(report_path),
                "telegram_summary": str(telegram_summary_path),
                "html_report": str(html_report_path),
            },
            "main_agent_read_policy": (
                "Read pipeline-summary.json first. For explicit demo-submit or real-submit runs, pass --submit-orders so execute_orders.py refreshes "
                "read-only account/order gates, reconciles active pending/reserved orders, and submits, adjusts, or blocks immediate/reservation orders before summary generation. "
                "When --order-path auto is used, the pipeline resolves weekday 09:00 <= t < 15:30 KST runs to immediate/order_cash, and 15:40 <= t or t < 07:30 plus weekends to reservation/order_resv before execution-plan. "
                "For explicit limit requests, treat execution-plan order_price values as the default limit price candidates unless a current API gate rejects them. "
                "Open command_log_path or other intermediate artifacts only when a stage failed and the summary is insufficient."
            ),
        }
        self.write_portfolio_report(summary)
        write_json(self.summary_path, summary)
        self.write_html_report()
        summary["stages"] = self.load_summary_stages()
        summary["html_report_available"] = html_report_path.is_file()
        write_json(self.summary_path, summary)
        self.write_telegram_summary()
        summary["stages"] = self.load_summary_stages()
        write_json(self.summary_path, summary)
        self.write_run_json(status=summary["status"])
        return summary

    def missing_summarize_artifacts(self) -> list[str]:
        required = [
            self.run_path,
            self.output_dir / "check-portfolio.json",
            self.output_dir / "decision-brief.json",
            self.output_dir / "analyst-review.json",
            self.output_dir / "judge-review.json",
            self.output_dir / "account-before-order.json",
            self.output_dir / "execution.json",
        ]
        missing = [str(path) for path in required if not path.exists()]
        for path in required:
            if not path.exists():
                continue
            payload = load_json_if_exists(path)
            if not isinstance(payload, dict):
                missing.append(f"{path}: invalid JSON object")
                continue
            if path == self.run_path:
                stages = payload.get("stages")
                if not isinstance(stages, list):
                    missing.append(f"{self.run_path}: missing stages")
                elif not stages:
                    missing.append(f"{self.run_path}: empty stages")
        return missing

    def run(self) -> dict[str, Any]:
        self.write_run_json(status="running")
        portfolio, portfolio_path = self.resolve_portfolio()
        symbols = normalize_symbol_ids(portfolio.get("universe"))
        if not symbols:
            self.add_stage("portfolio-universe", "failed", detail="check-portfolio universe is empty", path=portfolio_path)
            raise RuntimeError("check-portfolio universe is empty")
        self.add_stage("portfolio-universe", "success", detail=f"{len(symbols)} symbols", path=portfolio_path)

        self.collect_main_evidence(symbols)
        if getattr(self.args, "submit_orders", False) and self.args.request_type in {"demo-submit", "real-submit"}:
            self.run_order_lifecycle_preflight()
        financial_cache = self.collect_optional_cache("financial", symbols)
        symbol_news_cache = self.collect_optional_cache("symbol_news", symbols)
        news_context = self.build_news_context(symbol_news_cache)
        market_index_snapshot = self.collect_market_index_snapshot()

        decision_args = [
            "decision-brief",
            "--output-dir",
            str(self.output_dir),
            "--portfolio-json",
            str(portfolio_path),
            "--strategy-policy-config",
            str(self.strategy_policy_config_path),
            "--expected-symbol-news-date",
            self.optional_cache_date(),
        ]
        if financial_cache:
            decision_args.extend(["--financial-cache-path", financial_cache])
        if symbol_news_cache:
            decision_args.extend(["--symbol-news-cache-path", symbol_news_cache])
        if news_context:
            decision_args.extend(["--news-context-json", news_context])
        if market_index_snapshot:
            decision_args.extend(["--market-index-snapshot-json", market_index_snapshot])
        decision = self.run_artifact_command("decision-brief", decision_args)
        decision_status = str((decision or {}).get("status") or "")
        self.add_stage("decision-brief", "success" if decision_status in {"success", "partial"} else "failed", detail=f"status={decision_status}", path=self.output_dir / "decision-brief.json")
        if decision_status not in {"success", "partial"}:
            raise RuntimeError("decision-brief failed")

        self.run_artifact_command(
            "first-specs",
            [
                "first-specs",
                "--output-dir",
                str(self.output_dir),
                "--workspace-dir",
                str(self.workspace_dir),
                "--pipeline-dir",
                str(script_dir().parent),
                *(
                    ["--review-extra-instructions-file", str(self.review_extra_instructions_path)]
                    if self.review_extra_instructions_path
                    else []
                ),
            ],
        )
        self.add_stage("first-specs", "success", detail="built analyst-review specs", path=self.output_dir / "analyst-review-specs.json")
        self.run_analyst_reviews()

        first = self.run_artifact_command("merge-first", ["merge-first", "--output-dir", str(self.output_dir)])
        first_status = str((first or {}).get("status") or "")
        self.add_stage(
            "merge-first",
            "success" if first_status == "success" else "partial" if first_status == "partial" else "failed",
            detail=f"status={first_status}",
            path=self.output_dir / "analyst-review.json",
        )
        if first_status not in {"success", "partial"}:
            raise RuntimeError("merge-first failed")

        self.run_artifact_command(
            "second-spec",
            [
                "second-spec",
                "--output-dir",
                str(self.output_dir),
                "--portfolio-json",
                str(portfolio_path),
                "--workspace-dir",
                str(self.workspace_dir),
                "--pipeline-dir",
                str(script_dir().parent),
                "--strategy-policy-config",
                str(self.strategy_policy_config_path),
                *(
                    ["--review-extra-instructions-file", str(self.review_extra_instructions_path)]
                    if self.review_extra_instructions_path
                    else []
                ),
            ],
        )
        self.add_stage("second-spec", "success", detail="built judge-review spec", path=self.output_dir / "judge-review-spec.json")
        self.run_judge_review()

        execution = self.run_artifact_command(
            "execution-plan",
            [
                "execution-plan",
                "--output-dir",
                str(self.output_dir),
                "--request-type",
                self.args.request_type,
                "--order-path",
                self.order_path,
            ],
        )
        execution_status = str((execution or {}).get("status") or "success")
        self.add_stage("execution-plan", "partial" if execution_status == "partial" else "success", detail=f"status={execution_status}", path=self.output_dir / "execution.json")

        if (
            getattr(self.args, "submit_orders", False)
            and self.args.request_type in {"demo-submit", "real-submit"}
            and bool((execution or {}).get("requires_main_agent_order_execution"))
        ):
            execution = self.run_order_execution()

        if isinstance(execution, dict):
            self.finalize_account_order_gate_status(execution)

        token_args = ["token-summary", "--run-dir", str(self.output_dir)]
        if self.args.main_events:
            token_args.extend(["--main-events", str(resolve_workspace_path(self.workspace_dir, self.args.main_events))])
        token_summary = self.run_artifact_command("token-summary", token_args, required=False)
        if token_summary is not None:
            detail = "main/sub-agent token summary built" if self.args.main_events else "sub-agent token summary built"
            self.add_stage("token-summary", "success", detail=detail, required=False, path=self.output_dir / "token-summary.json")
        self.run_terminal_order_reconciliation()
        return self.build_summary(portfolio)


def run_self_test() -> int:
    """Run the extracted test suite through the legacy CLI contract."""
    codex_exec_root = Path(__file__).resolve().parents[4]
    codex_exec_root_text = str(codex_exec_root)
    if codex_exec_root_text not in sys.path:
        sys.path.insert(0, codex_exec_root_text)

    from service.pipelines.daily_trading.tests.test_run_daily_trading_pipeline import (
        run_self_test as run_external_self_test,
    )

    return run_external_self_test()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the daily-trading pipeline with compact Main-agent output.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run the deterministic daily-trading orchestration pipeline.")
    run.add_argument("--workspace-dir", default=".")
    run.add_argument("--output-dir", default="")
    run.add_argument("--run-id", default="")
    run.add_argument("--started-at", default="")
    run.add_argument("--env", default=os.environ.get("CODEX_MCP_TRADING_ENV", "acct"), choices=["acct", "real", "paper", "demo"])
    run.add_argument("--request-type", default="analysis", choices=["analysis", "prepare", "demo-submit", "real-submit"])
    run.add_argument("--portfolio-json", default="")
    run.add_argument("--financial-cache-path", default="")
    run.add_argument("--symbol-news-cache-path", default="")
    run.add_argument("--main-events", default="", help="Optional Codex JSONL events path for Main-agent token accounting.")
    run.add_argument("--submit-orders", action="store_true", help="For explicit demo-submit/real-submit runs, execute immediate or reservation orders through execute_orders.py.")
    run.add_argument("--review-extra-instructions-file", default="", help="Optional JSON file with analyst_review/judge_review supplemental instructions.")
    run.add_argument("--strategy-policy-config", default="", help="Optional YAML file for computed judge strategy context.")
    run.add_argument(
        "--order-path",
        choices=[ORDER_PATH_AUTO, "reservation", "immediate"],
        default=ORDER_PATH_AUTO,
        help="Order API path. auto uses KST order-session time: order_cash for weekday 09:00 <= t < 15:30, order_resv for 15:40 <= t or t < 07:30 and weekends.",
    )
    run.add_argument("--date", default="")
    run.add_argument("--reuse-existing-artifacts", action="store_true")
    run.add_argument("--skip-account", action="store_true")
    run.add_argument("--max-workers", type=int, default=2)
    summarize = subparsers.add_parser("summarize", help="Rebuild pipeline-summary.json and the portfolio Markdown report from existing run artifacts.")
    summarize.add_argument("--workspace-dir", default=".")
    summarize.add_argument("--output-dir", required=True)
    summarize.add_argument("--run-id", default="")
    summarize.add_argument("--started-at", default="")
    summarize.add_argument("--env", default=os.environ.get("CODEX_MCP_TRADING_ENV", "acct"), choices=["acct", "real", "paper", "demo"])
    summarize.add_argument("--request-type", default="analysis", choices=["analysis", "prepare", "demo-submit", "real-submit"])
    summarize.add_argument("--order-path", choices=[ORDER_PATH_AUTO, "reservation", "immediate"], default=ORDER_PATH_AUTO)
    summarize.add_argument("--portfolio-json", default="")
    summarize.add_argument("--review-extra-instructions-file", default="")
    summarize.add_argument("--strategy-policy-config", default="")

    subparsers.add_parser("self-test", help="Run an offline pipeline smoke test with a fake codex binary.")
    return parser


def command_run(args: argparse.Namespace) -> int:
    pipeline = Pipeline(args)
    try:
        summary = pipeline.run()
    except Exception as exc:  # noqa: BLE001 - write compact failed summary
        pipeline.add_stage("pipeline", "failed", detail=str(exc)[:300])
        summary = pipeline.build_summary(load_json_if_exists(pipeline.output_dir / "check-portfolio.json") or {})
        summary["error"] = str(exc)[:500]
        write_json(pipeline.summary_path, summary)
        print(json.dumps({"status": "failed", "run_dir": str(pipeline.output_dir), "summary_path": str(pipeline.summary_path)}, ensure_ascii=False, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "status": summary["status"],
                "run_dir": summary["run_dir"],
                "summary_path": summary["summary_path"],
                "subagent_total_tokens": summary["token_usage"]["subagents"]["total_tokens"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if summary["status"] in {"success", "partial"} else 1


def command_summarize(args: argparse.Namespace) -> int:
    pipeline = Pipeline(args)
    missing = pipeline.missing_summarize_artifacts()
    if missing:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "run_dir": str(pipeline.output_dir),
                    "missing_artifacts": missing,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    pipeline.adopt_existing_run_identity()
    summary = pipeline.build_summary(pipeline.load_portfolio_for_summary())
    print(
        json.dumps(
            {
                "status": summary["status"],
                "run_dir": summary["run_dir"],
                "summary_path": summary["summary_path"],
                "report_path": summary["report_path"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if summary["status"] in {"success", "partial"} else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "self-test":
        return run_self_test()
    if args.command == "run":
        return command_run(args)
    if args.command == "summarize":
        return command_summarize(args)
    raise SystemExit("a subcommand is required")


if __name__ == "__main__":
    raise SystemExit(main())
