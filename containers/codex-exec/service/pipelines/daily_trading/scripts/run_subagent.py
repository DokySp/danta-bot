#!/usr/bin/env python3
"""Run daily-trading sub-agent stages through codex exec."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_SPEC_FIELDS = {
    "run_id",
    "started_at",
    "stage",
    "agent_role",
    "task_name",
    "prompt",
    "workspace_dir",
    "output_dir",
}
SUBAGENT_MODEL_CONFIG_ENV = "DAILY_TRADING_SUBAGENT_MODEL_CONFIG"
SUBAGENT_MODEL_CONFIG_FILENAME = "daily-trading-subagents.yaml"
DEFAULT_SUBAGENT_MODEL_CONFIG = {
    "collection": {"model": "gpt-5.6-luna", "model_reasoning_effort": "low"},
    "analyst_review": {"model": "gpt-5.6-sol", "model_reasoning_effort": "xhigh"},
    "judge_review": {"model": "gpt-5.6-sol", "model_reasoning_effort": "xhigh"},
}
SUBAGENT_MODEL_CONFIG_KEYS = ("collection", "analyst_review", "judge_review")
COLLECTION_STAGES = {"financial-collection", "news-collection"}
FINANCIAL_PATH_OUTPUT_STAGES = {"financial-collection"}
NEWS_PATH_OUTPUT_STAGES = {"news-collection"}
TEXT_OUTPUT_STAGES = FINANCIAL_PATH_OUTPUT_STAGES | NEWS_PATH_OUTPUT_STAGES
OPTIONAL_GROUP_FAILURE_STAGES = TEXT_OUTPUT_STAGES
REVIEW_STAGES = {"analyst-review", "judge-review"}
SELECTED_ANALYST_REVIEW_ROLES = {
    "analyst-quality-risk",
    "analyst-momentum-news",
}
MARKET_INDEX_SNAPSHOT_AGENT_ROLES = {"analyst-quality-risk", "analyst-momentum-news", "judge"}
COMBINED_ANALYST_REVIEW_ROLE_OUTPUTS = {
    "analyst-quality-risk": (
        "analyst-quality-value",
        "analyst-risk-allocation",
    ),
    "analyst-momentum-news": (
        "analyst-momentum-cycle",
        "analyst-news-flow",
    ),
}
ANALYST_REVIEW_VIEW_INPUT_FIELDS = {
    "analyst-quality-value": {
        "price",
        "today_trade_price_context",
        "financial_summary",
        "etf_summary",
        "news_summary",
    },
    "analyst-risk-allocation": {
        "price",
        "today_trade_price_context",
        "account_exposure",
        "orderbook_summary",
        "trade_flow_summary",
        "investor_flow_summary",
        "etf_summary",
    },
    "analyst-momentum-cycle": {
        "price",
        "today_trade_price_context",
        "price_chart_signals",
        "chart_context",
        "orderbook_summary",
        "trade_flow_summary",
        "investor_flow_summary",
    },
    "analyst-news-flow": {
        "price",
        "today_trade_price_context",
        "news_summary",
    },
}
ANALYST_REVIEW_ALWAYS_SYMBOL_FIELDS = {
    "symbol_id",
    "symbol",
    "symbol_name",
    "code",
    "name",
    "market",
    "product_type",
    "eligible_for_review",
    "evidence_mode",
    "warnings",
    "errors",
    "missing_data",
    "exclusion_reasons",
}
MAX_BLANK_LINES = 1
RAW_RETENTION_VALUES = {"always", "failed", "never"}
EVENT_RETENTION_VALUES = {"always", "anomaly", "failed", "never"}
DEFAULT_EVENT_TOKEN_THRESHOLD = 1_000_000
MAX_USAGE_EVENT_SUMMARY_ITEMS = 50
MAX_REPEATED_TOOL_FINGERPRINTS = 20
TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
DISALLOWED_COMPACT_REVIEW_KEYS = {
    "cash_rationale",
    "cash_reason_code",
    "duplicate_exposure_limits",
    "evidence",
    "price_chart_view",
    "portfolio",
    "rationale",
    "risks",
    "target_cash_amount",
    "one_line_portfolio_reason",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def repo_root_from(path: Path) -> Path:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_config_payload(path: Path) -> Any:
    if path.suffix.lower() == ".json":
        return load_json(path)
    try:
        import yaml  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - depends on runtime image
        raise RuntimeError(f"PyYAML is required to read {path}") from exc
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def subagent_model_config_candidates() -> list[Path]:
    configured = os.getenv(SUBAGENT_MODEL_CONFIG_ENV, "").strip()
    if configured:
        return [Path(configured).expanduser()]
    repo_root = repo_root_from(script_dir())
    return [
        Path("/app/config") / SUBAGENT_MODEL_CONFIG_FILENAME,
        repo_root / "containers/codex-exec/profiles/base/config" / SUBAGENT_MODEL_CONFIG_FILENAME,
        Path("containers/codex-exec/profiles/base/config") / SUBAGENT_MODEL_CONFIG_FILENAME,
    ]


def normalize_model_config(payload: Any, source: Path | None) -> dict[str, dict[str, str]]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        where = str(source) if source else "built-in defaults"
        raise ValueError(f"sub-agent model config must be an object: {where}")
    config = json.loads(json.dumps(DEFAULT_SUBAGENT_MODEL_CONFIG))
    for key in SUBAGENT_MODEL_CONFIG_KEYS:
        if key not in payload:
            continue
        entry = payload[key]
        if not isinstance(entry, dict):
            raise ValueError(f"sub-agent model config entry must be an object: {key}")
        for field in ("model", "model_reasoning_effort"):
            if field not in entry:
                continue
            value = str(entry.get(field, "")).strip()
            if not value:
                raise ValueError(f"sub-agent model config {key}.{field} must not be empty")
            config[key][field] = value
    extra_keys = sorted(str(key) for key in payload if str(key) not in SUBAGENT_MODEL_CONFIG_KEYS)
    if extra_keys:
        raise ValueError(f"unsupported sub-agent model config keys: {', '.join(extra_keys)}")
    return config


def load_subagent_model_config() -> dict[str, dict[str, str]]:
    candidates = subagent_model_config_candidates()
    explicit = bool(os.getenv(SUBAGENT_MODEL_CONFIG_ENV, "").strip())
    for path in candidates:
        if path.exists():
            return normalize_model_config(load_config_payload(path), path)
    if explicit:
        raise FileNotFoundError(f"{SUBAGENT_MODEL_CONFIG_ENV} does not exist: {candidates[0]}")
    return normalize_model_config(DEFAULT_SUBAGENT_MODEL_CONFIG, None)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


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


def token_threshold_from_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def token_count_payload(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    if item.get("type") == "token_count":
        return item
    if item.get("type") != "event_msg":
        return None
    payload = item.get("payload")
    if isinstance(payload, dict) and payload.get("type") == "token_count":
        return payload
    return None


def turn_completed_usage(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    if item.get("type") != "turn.completed":
        return None
    usage = item.get("usage")
    return usage if isinstance(usage, dict) else None


def event_label(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "unknown")
    payload = item.get("payload")
    if isinstance(payload, dict):
        payload_type = payload.get("type")
        if payload_type:
            return f"{item_type}:{payload_type}"
    return item_type


def iter_dict_values(value: Any) -> Any:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dict_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dict_values(child)


def compact_for_fingerprint(value: Any) -> str:
    if isinstance(value, list):
        text = shlex.join(str(item) for item in value)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        text = str(value or "")
    return re.sub(r"\s+", " ", text).strip()[:500]


def tool_command_text(item: dict[str, Any]) -> str:
    for key in ("command", "cmd", "argv", "args", "arguments", "input"):
        if key in item:
            text = compact_for_fingerprint(item.get(key))
            if text:
                return text
    for key in ("tool_name", "name", "function"):
        if key in item:
            text = compact_for_fingerprint(item.get(key))
            if text:
                return text
    return ""


def tool_result_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("content", "output", "result", "stdout", "stderr", "message"):
        value = item.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif value is not None and key in {"content", "output", "result"}:
            parts.append(compact_for_fingerprint(value))
    return "\n".join(parts)


def looks_like_tool_call(item: dict[str, Any]) -> bool:
    item_type = str(item.get("type") or "").lower()
    if "function_call_output" in item_type:
        return False
    if "tool" in item_type and "call" in item_type:
        return True
    if "function_call" in item_type:
        return True
    if item.get("tool_name") and any(key in item for key in ("command", "cmd", "args", "arguments", "input")):
        return True
    return False


def looks_like_tool_result(item: dict[str, Any]) -> bool:
    item_type = str(item.get("type") or "").lower()
    if "tool" in item_type and any(word in item_type for word in ("result", "output", "complete")):
        return True
    if "function_call_output" in item_type:
        return True
    if item.get("tool_name") and any(key in item for key in ("content", "output", "result", "stdout", "stderr")):
        return True
    return False


def tool_kind(item: dict[str, Any]) -> str:
    for key in ("tool_name", "name", "function"):
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("name")
        text = str(value or "").strip()
        if text:
            return text[:80]
    return str(item.get("type") or "unknown")[:80]


def fingerprint_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def summarize_codex_event_stream(stdout: str, token_usage: dict[str, int]) -> dict[str, Any]:
    event_type_counts: dict[str, int] = {}
    tool_call_counts: dict[str, int] = {}
    repeated_fingerprints: dict[str, dict[str, Any]] = {}
    usage_events: list[dict[str, Any]] = []
    json_event_count = 0
    parse_error_count = 0
    max_event_bytes = 0
    tool_call_count = 0
    tool_result_count = 0
    total_tool_result_bytes = 0
    max_tool_result_bytes = 0

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line_bytes = len(raw_line.encode("utf-8", errors="replace"))
        max_event_bytes = max(max_event_bytes, line_bytes)
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            parse_error_count += 1
            continue
        if not isinstance(item, dict):
            continue
        json_event_count += 1
        label = event_label(item)
        event_type_counts[label] = event_type_counts.get(label, 0) + 1

        payload = token_count_payload(item)
        if payload is not None:
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            usage = token_usage_from(info.get("last_token_usage"))
            if len(usage_events) < MAX_USAGE_EVENT_SUMMARY_ITEMS:
                usage_events.append({"kind": "token_count", **usage})
        completed_usage = turn_completed_usage(item)
        if completed_usage is not None:
            usage = token_usage_from(completed_usage)
            if len(usage_events) < MAX_USAGE_EVENT_SUMMARY_ITEMS:
                usage_events.append({"kind": "turn.completed", **usage})

        for candidate in iter_dict_values(item):
            if looks_like_tool_call(candidate):
                tool_call_count += 1
                kind = tool_kind(candidate)
                tool_call_counts[kind] = tool_call_counts.get(kind, 0) + 1
                command = tool_command_text(candidate)
                if command:
                    digest = fingerprint_text(f"{kind}\n{command}")
                    entry = repeated_fingerprints.setdefault(
                        digest,
                        {"fingerprint": digest, "kind": kind, "count": 0},
                    )
                    entry["count"] += 1
            if looks_like_tool_result(candidate):
                tool_result_count += 1
                result_text = tool_result_text(candidate)
                result_bytes = len(result_text.encode("utf-8", errors="replace"))
                total_tool_result_bytes += result_bytes
                max_tool_result_bytes = max(max_tool_result_bytes, result_bytes)

    token_threshold = token_threshold_from_env("CODEX_SUBAGENT_EVENT_TOKEN_THRESHOLD", DEFAULT_EVENT_TOKEN_THRESHOLD)
    anomaly_detected = (
        int(token_usage.get("total_tokens", 0)) >= token_threshold
        or int(token_usage.get("input_tokens", 0)) >= token_threshold
    )
    repeated = [
        item
        for item in sorted(
            repeated_fingerprints.values(),
            key=lambda entry: (-int(entry.get("count", 0)), str(entry.get("kind", "")), str(entry.get("fingerprint", ""))),
        )
        if int(item.get("count", 0)) > 1
    ][:MAX_REPEATED_TOOL_FINGERPRINTS]
    return {
        "stdout_bytes": len(stdout.encode("utf-8", errors="replace")),
        "json_event_count": json_event_count,
        "parse_error_count": parse_error_count,
        "event_type_counts": event_type_counts,
        "max_event_bytes": max_event_bytes,
        "usage_event_count": len(usage_events),
        "usage_event_count_truncated": json_event_count > len(usage_events) >= MAX_USAGE_EVENT_SUMMARY_ITEMS,
        "usage_events": usage_events,
        "tool_call_count": tool_call_count,
        "tool_call_counts": tool_call_counts,
        "tool_result_count": tool_result_count,
        "tool_result_bytes": total_tool_result_bytes,
        "max_tool_result_bytes": max_tool_result_bytes,
        "repeated_tool_fingerprints": repeated,
        "anomaly_detected": anomaly_detected,
        "anomaly_token_threshold": token_threshold,
    }


def parse_codex_json_events(stdout: str) -> dict[str, Any]:
    usage = zero_token_usage()
    event_count = 0
    last_rate_limits: Any | None = None
    last_message = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = token_count_payload(item)
        if payload is not None:
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            add_token_usage(usage, token_usage_from(info.get("last_token_usage")))
            event_count += 1
            last_rate_limits = item.get("rate_limits") or payload.get("rate_limits") or last_rate_limits
            continue
        completed_usage = turn_completed_usage(item)
        if completed_usage is not None:
            add_token_usage(usage, token_usage_from(completed_usage))
            event_count += 1
            last_rate_limits = item.get("rate_limits") or last_rate_limits
            continue
        if isinstance(item, dict) and item.get("type") == "event_msg":
            event_payload = item.get("payload")
            if isinstance(event_payload, dict) and event_payload.get("type") == "task_complete":
                message = event_payload.get("last_agent_message")
                if isinstance(message, str):
                    last_message = message
    return {
        "token_usage": usage,
        "token_usage_event_count": event_count,
        "rate_limits": last_rate_limits,
        "last_agent_message": last_message,
    }


def read_json_if_exists(path: Path) -> Any | None:
    if not path.exists():
        return None
    return load_json(path)


def safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    return cleaned.strip(".-") or "subagent"


def compact_prompt(prompt: str) -> str:
    """Remove prompt whitespace that does not carry trading instructions."""
    compacted: list[str] = []
    blank_count = 0
    for raw_line in prompt.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        if line:
            compacted.append(line)
            blank_count = 0
            continue
        blank_count += 1
        if blank_count <= MAX_BLANK_LINES:
            compacted.append("")
    return "\n".join(compacted).strip("\n")


def normalize_artifact_paths(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    paths: dict[str, str] = {}
    for key, value in raw.items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if key_text and value_text:
            paths[key_text] = value_text
    return paths


def normalize_symbol_ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items: list[Any] = raw.replace("\n", ",").split(",")
    elif isinstance(raw, list):
        items = raw
    else:
        items = [raw]

    symbols: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            value = item.get("symbol_id") or item.get("symbol") or item.get("code")
        else:
            value = item
        symbol_id = str(value or "").strip()
        if symbol_id and symbol_id not in seen:
            symbols.append(symbol_id)
            seen.add(symbol_id)
    return symbols


def symbol_key(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("symbol_id") or item.get("symbol") or item.get("code") or "").strip()


def raw_retention_mode() -> str:
    mode = os.getenv("CODEX_SUBAGENT_RAW_RETENTION", "always").strip().lower()
    if mode not in RAW_RETENTION_VALUES:
        return "always"
    return mode


def event_retention_mode() -> str:
    mode = os.getenv("CODEX_SUBAGENT_EVENT_RETENTION", "anomaly").strip().lower()
    if mode not in EVENT_RETENTION_VALUES:
        return "anomaly"
    return mode


def resolve_artifact_path(path_text: str, workspace_dir: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return Path(workspace_dir) / path


def filter_symbol_scoped_errors(errors: Any, wanted: set[str]) -> Any:
    if not isinstance(errors, list):
        return errors
    filtered: list[Any] = []
    for item in errors:
        if not isinstance(item, dict):
            filtered.append(item)
            continue
        symbol_id = item.get("symbol_id")
        if symbol_id is None or str(symbol_id) in wanted:
            filtered.append(item)
    return filtered


def filter_symbols(payload: Any, symbol_ids: list[str]) -> Any:
    if not isinstance(payload, dict):
        return payload
    wanted = set(symbol_ids)
    filtered = dict(payload)
    symbols = payload.get("symbols")
    if isinstance(symbols, list):
        filtered["symbols"] = [
            item
            for item in symbols
            if isinstance(item, dict) and str(item.get("symbol_id") or item.get("symbol") or item.get("code") or "") in wanted
        ]
    elif isinstance(symbols, dict):
        filtered["symbols"] = {key: value for key, value in symbols.items() if str(key) in wanted}
    if "errors" in filtered:
        filtered["errors"] = filter_symbol_scoped_errors(filtered.get("errors"), wanted)
    return filtered


def analyst_review_output_roles(agent_role: str) -> tuple[str, ...]:
    role = safe_name(agent_role).lower()
    return COMBINED_ANALYST_REVIEW_ROLE_OUTPUTS.get(role, (role,))


def analyst_review_symbol_fields(agent_role: str) -> set[str] | None:
    roles = analyst_review_output_roles(agent_role)
    if not roles:
        return None
    fields = set(ANALYST_REVIEW_ALWAYS_SYMBOL_FIELDS)
    matched = False
    for role in roles:
        role_fields = ANALYST_REVIEW_VIEW_INPUT_FIELDS.get(role)
        if role_fields:
            fields.update(role_fields)
            matched = True
    return fields if matched else None


def filter_symbol_fields_for_agent(payload: Any, agent_role: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    fields = analyst_review_symbol_fields(agent_role)
    if not fields:
        return payload
    filtered = dict(payload)
    symbols = filtered.get("symbols")
    if isinstance(symbols, list):
        filtered["symbols"] = [
            {key: value for key, value in item.items() if key in fields}
            if isinstance(item, dict)
            else item
            for item in symbols
        ]
    elif isinstance(symbols, dict):
        filtered["symbols"] = {
            symbol_id: {key: value for key, value in item.items() if key in fields}
            if isinstance(item, dict)
            else item
            for symbol_id, item in symbols.items()
        }
    filtered["slice_agent_role"] = safe_name(agent_role).lower()
    filtered["slice_output_roles"] = list(analyst_review_output_roles(agent_role))
    return filtered


def build_review_core_payload(payload: Any, symbol_ids: list[str], agent_role: str = "") -> Any:
    filtered = filter_symbols(payload, symbol_ids)
    if not isinstance(filtered, dict):
        return filtered
    core = dict(filter_symbol_fields_for_agent(filtered, agent_role) if agent_role else filtered)
    if agent_role and safe_name(agent_role).lower() not in MARKET_INDEX_SNAPSHOT_AGENT_ROLES:
        core.pop("market_index_snapshot", None)
    if agent_role and safe_name(agent_role).lower() != "judge":
        core.pop("strategy_context", None)
    core["slice_type"] = "review-core"
    core["source_brief_type"] = filtered.get("brief_type") or "decision-brief"
    return core


def int_or_zero(raw: Any) -> int:
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        text = raw.strip().replace(",", "")
        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError:
            return 0
    return 0


def non_negative_int_value(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, float):
        if raw.is_integer() and raw >= 0:
            return int(raw)
        return None
    if isinstance(raw, str):
        text = raw.strip().replace(",", "")
        if not text:
            return None
        try:
            value = int(text)
        except ValueError:
            return None
        return value if value >= 0 else None
    return None


def non_negative_number_value(raw: Any) -> int | float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        if raw < 0:
            return None
        return int(raw) if isinstance(raw, float) and raw.is_integer() else raw
    if isinstance(raw, str):
        text = raw.strip().replace(",", "")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
        if value < 0:
            return None
        return int(value) if value.is_integer() else value
    return None


def review_score_value(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if 0 <= raw <= 10 else None


def first_present_int_value(*values: Any) -> int:
    for value in values:
        if value is not None:
            return int_or_zero(value)
    return 0


def build_holding_quantity_context(symbol: dict[str, Any]) -> dict[str, Any]:
    account = symbol.get("account_exposure")
    if not isinstance(account, dict):
        account = {}
    pending_buy = first_present_int_value(
        account.get("pending_and_reserved_buy_quantity"),
        account.get("active_pending_buy_quantity"),
        account.get("reserved_buy_quantity"),
        symbol.get("pending_and_reserved_buy_quantity"),
        symbol.get("active_pending_buy_quantity"),
        symbol.get("reserved_buy_quantity"),
    )
    pending_sell = first_present_int_value(
        account.get("pending_and_reserved_sell_quantity"),
        account.get("active_pending_sell_quantity"),
        account.get("reserved_sell_quantity"),
        symbol.get("pending_and_reserved_sell_quantity"),
        symbol.get("active_pending_sell_quantity"),
        symbol.get("reserved_sell_quantity"),
    )
    current = first_present_int_value(
        account.get("current_live_holding_quantity"),
        account.get("holding_quantity"),
        symbol.get("current_live_holding_quantity"),
        symbol.get("holding_quantity"),
    )
    expected = current + pending_buy - pending_sell
    return {
        "current_live_holding_quantity": current,
        "pending_and_reserved_buy_quantity": pending_buy,
        "pending_and_reserved_sell_quantity": pending_sell,
        "expected_holding_quantity": expected,
        "target_position_value_semantics": "judge decides target_position_value_krw as desired exposure first; expected_holding_quantity is the baseline hold quantity, and pipeline derives final_holding_quantity from target_position_value_krw / price.current_or_last with Decimal ROUND_HALF_UP",
    }


def run_started_sort_key(run_dir: Path, payload: dict[str, Any]) -> tuple[str, str]:
    started_at = str(payload.get("started_at") or "").strip()
    return (started_at or run_dir.name, run_dir.name)


def recent_submitted_trade_context(output_dir: Path, symbol_id: str, run_limit: int = 2) -> dict[str, Any]:
    unavailable = {
        "recent_submitted_trades": [],
        "inspected_run_ids": [],
        "coverage_status": "unavailable",
        "requested_run_count": max(run_limit, 0),
        "inspected_run_count": 0,
        "invalid_execution_count": 0,
        "policy": "An empty recent_submitted_trades list confirms no recent trades only when coverage_status=complete; otherwise history is unknown.",
    }
    if not symbol_id or run_limit <= 0:
        return unavailable
    runs_dir = output_dir.parent
    if not runs_dir.is_dir():
        return unavailable

    previous_runs: list[tuple[tuple[str, str], Path, dict[str, Any]]] = []
    invalid_execution_count = 0
    for run_dir in (path for path in runs_dir.iterdir() if path.is_dir()):
        if run_dir.resolve() == output_dir.resolve():
            continue
        execution_path = run_dir / "execution.json"
        if not execution_path.exists():
            continue
        try:
            payload = read_json_if_exists(execution_path)
        except (OSError, ValueError):
            invalid_execution_count += 1
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("orders"), list):
            invalid_execution_count += 1
            continue
        previous_runs.append((run_started_sort_key(run_dir, payload), run_dir, payload))
    previous_runs.sort(key=lambda item: item[0], reverse=True)

    trades: list[dict[str, Any]] = []
    inspected_runs: list[str] = []
    for _, run_dir, payload in previous_runs[:run_limit]:
        inspected_runs.append(str(payload.get("run_id") or run_dir.name))
        orders = payload.get("orders")
        if not isinstance(orders, list):
            continue
        for order in orders:
            if not isinstance(order, dict) or symbol_key(order) != symbol_id:
                continue
            direction = str(order.get("direction", "")).lower()
            result = str(order.get("result", "")).lower()
            if direction not in {"buy", "sell"} or not result.startswith("submitted"):
                continue
            trades.append(
                {
                    "run_id": payload.get("run_id") or run_dir.name,
                    "started_at": payload.get("started_at") or "",
                    "direction": direction,
                    "result": order.get("result"),
                    "submitted_quantity": int_or_zero(order.get("validated_order_quantity")),
                    "current_live_holding_quantity": int_or_zero(order.get("current_live_holding_quantity")),
                    "expected_holding_quantity": int_or_zero(order.get("expected_holding_quantity")),
                    "final_holding_quantity": int_or_zero(order.get("final_holding_quantity")),
                    "additional_required_quantity": int_or_zero(order.get("additional_required_quantity")),
                    "order_path": order.get("order_path") or "",
                    "order_api": order.get("order_api") or "",
                    "reason": str(order.get("reason") or "")[:120],
                }
            )
    inspected_run_count = len(inspected_runs)
    coverage_status = (
        "complete"
        if inspected_run_count >= run_limit and invalid_execution_count == 0
        else "partial"
        if inspected_run_count
        else "unavailable"
    )
    return {
        "recent_submitted_trades": trades,
        "inspected_run_ids": inspected_runs,
        "coverage_status": coverage_status,
        "requested_run_count": run_limit,
        "inspected_run_count": inspected_run_count,
        "invalid_execution_count": invalid_execution_count,
        "policy": "Recent submitted trades are context for sizing within the allowed candidate direction; an empty list confirms no recent trades only when coverage_status=complete, and score-band direction preconditions still apply.",
    }


def add_judge_review_holding_context(payload: Any, output_dir: Path | None = None) -> Any:
    if not isinstance(payload, dict):
        return payload
    context_output_dir = output_dir or Path("")
    symbols = payload.get("symbols")
    if isinstance(symbols, list):
        enriched: list[Any] = []
        for item in symbols:
            if isinstance(item, dict):
                copied = dict(item)
                copied["holding_quantity_context"] = build_holding_quantity_context(copied)
                copied["recent_trade_context"] = recent_submitted_trade_context(context_output_dir, symbol_key(copied))
                enriched.append(copied)
            else:
                enriched.append(item)
        copied_payload = dict(payload)
        copied_payload["symbols"] = enriched
        return copied_payload
    if isinstance(symbols, dict):
        copied_payload = dict(payload)
        copied_payload["symbols"] = {
            symbol_id: dict(
                item,
                holding_quantity_context=build_holding_quantity_context(item),
                recent_trade_context=recent_submitted_trade_context(context_output_dir, symbol_id),
            )
            if isinstance(item, dict)
            else item
            for symbol_id, item in symbols.items()
        }
        return copied_payload
    return payload


def without_excluded_agent_scores(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    copied = dict(item)
    agent_scores = item.get("agent_scores")
    if isinstance(agent_scores, list):
        copied["agent_scores"] = [
            dict(score) if isinstance(score, dict) else score
            for score in agent_scores
            if not (isinstance(score, dict) and score.get("excluded_from_aggregation"))
        ]
    return copied


def build_analyst_review_slice_payload(payload: Any, symbol_ids: list[str]) -> Any:
    filtered = filter_symbols(payload, symbol_ids)
    if not isinstance(filtered, dict):
        return filtered
    sliced = dict(filtered)
    symbols = filtered.get("symbols")
    if isinstance(symbols, list):
        sliced["symbols"] = [without_excluded_agent_scores(item) for item in symbols]
    elif isinstance(symbols, dict):
        sliced["symbols"] = {
            symbol_id: without_excluded_agent_scores(item)
            for symbol_id, item in symbols.items()
        }
    sliced["slice_type"] = "analyst-review-slice"
    sliced["source_stage"] = filtered.get("stage") or "analyst-review"
    return sliced


def write_review_input_slices(spec: dict[str, Any]) -> dict[str, str]:
    if not is_compact_review_spec(spec):
        return {}
    stage = str(spec.get("stage", "")).strip()
    artifacts = normalize_artifact_paths(spec.get("artifact_paths"))
    symbols = normalize_symbol_ids(spec.get("symbol_ids") or spec.get("symbols"))
    decision_brief = artifacts.get("decision_brief") or artifacts.get("decision-brief") or artifacts.get("brief")
    if not decision_brief or not symbols:
        return {}

    workspace_dir = str(spec.get("workspace_dir", ""))
    output_dir = Path(str(spec["output_dir"]))
    slice_dir = output_dir / "review-inputs"
    task_name = safe_name(str(spec["task_name"]))
    slice_paths: dict[str, str] = {}

    sources = [("decision_brief", decision_brief)]
    if stage == "judge-review":
        sources.append(("analyst_review", artifacts.get("analyst_review") or artifacts.get("analyst-review") or ""))

    for artifact_key, source_path_text in sources:
        if not source_path_text:
            continue
        source_path = resolve_artifact_path(source_path_text, workspace_dir)
        payload = read_json_if_exists(source_path)
        if payload is None:
            continue
        if artifact_key == "decision_brief":
            sliced = build_review_core_payload(payload, symbols, str(spec.get("agent_role") or ""))
            if stage == "judge-review":
                sliced = add_judge_review_holding_context(sliced, output_dir)
            relative_name = "review-core"
            slice_paths["review_core"] = str(slice_dir / f"{task_name}.{relative_name}.json")
        else:
            sliced = build_analyst_review_slice_payload(payload, symbols)
            relative_name = "analyst-review-slice"
        slice_path = slice_dir / f"{task_name}.{relative_name}.json"
        write_json(slice_path, sliced)
        slice_paths[artifact_key] = str(slice_path)
    return slice_paths


def spec_with_review_slices(spec: dict[str, Any], slice_paths: dict[str, str]) -> dict[str, Any]:
    if not slice_paths:
        return spec
    copied = dict(spec)
    artifacts = dict(normalize_artifact_paths(copied.get("artifact_paths")))
    if "decision_brief" in slice_paths:
        artifacts["decision_brief"] = slice_paths["decision_brief"]
    if "analyst_review" in slice_paths:
        artifacts["analyst_review"] = slice_paths["analyst_review"]
    copied["artifact_paths"] = artifacts
    return copied


def is_compact_review_spec(spec: dict[str, Any]) -> bool:
    if str(spec.get("stage", "")).strip() not in REVIEW_STAGES:
        return False
    if str(spec.get("prompt", "")).strip():
        return False
    artifacts = normalize_artifact_paths(spec.get("artifact_paths"))
    decision_brief = artifacts.get("decision_brief") or artifacts.get("decision-brief") or artifacts.get("brief")
    symbols = normalize_symbol_ids(spec.get("symbol_ids") or spec.get("symbols"))
    return bool(decision_brief and symbols)


def is_compact_review_candidate(spec: dict[str, Any]) -> bool:
    return (
        str(spec.get("stage", "")).strip() in REVIEW_STAGES
        and not str(spec.get("prompt", "")).strip()
        and (spec.get("artifact_paths") is not None or spec.get("symbol_ids") is not None or spec.get("symbols") is not None)
    )


def compact_review_prompt(spec: dict[str, Any]) -> str | None:
    if not is_compact_review_spec(spec):
        return None
    stage = str(spec.get("stage", "")).strip()
    artifacts = normalize_artifact_paths(spec.get("artifact_paths"))
    symbols = normalize_symbol_ids(spec.get("symbol_ids") or spec.get("symbols"))
    decision_brief = artifacts.get("decision_brief") or artifacts.get("decision-brief") or artifacts.get("brief")
    analyst_review = artifacts.get("analyst_review") or artifacts.get("analyst-review")
    persona = artifacts.get("persona") or artifacts.get("persona_path")
    review_format = artifacts.get("review_format") or artifacts.get("analyst-review-format")
    output_dir = str(spec.get("output_dir", "")).strip()
    task_name = safe_name(str(spec.get("task_name", "")))
    agent_role = safe_name(str(spec.get("agent_role", "")))
    sidecar_path = f"{output_dir}/reviews/{stage}--{agent_role}--{task_name}.md"

    lines = [
        "Daily-trading review sub-agent.",
        f"stage: {stage}",
        f"agent_role: {spec.get('agent_role', '')}",
        f"task_name: {spec.get('task_name', '')}",
        f"run_id: {spec.get('run_id', '')}",
        f"started_at: {spec.get('started_at', '')}",
        f"workspace_dir: {spec.get('workspace_dir', '')}",
        f"human_markdown_path: {sidecar_path}",
        "",
        "Use only the supplied local artifact, persona, and rule files.",
        "You may use read-only local shell commands such as cat and jq only for the explicitly listed files.",
        "Do not call KIS, MCP, web, network, account/order APIs, or external data sources.",
        "Do not write files, create Markdown, emit diffs, or wrap output in code fences.",
        "Read only the listed symbol_ids from artifact files; do not load unrelated symbols, raw cache files, secrets, or unlisted paths.",
    ]
    if decision_brief:
        lines.append(f"decision_brief: {decision_brief}")
    if analyst_review:
        lines.append(f"analyst_review: {analyst_review}")
    if persona:
        lines.append(f"persona: {persona}")
    if review_format:
        lines.append(f"review_format: {review_format}")
    if symbols:
        lines.append("symbol_ids: " + ",".join(symbols))
    extra_instructions = [
        str(item).strip()
        for item in spec.get("extra_instructions", [])
        if str(item).strip()
    ] if isinstance(spec.get("extra_instructions"), list) else []
    if extra_instructions:
        lines.extend(
            [
                "",
                "Supplemental schedule instructions:",
                "These instructions may adjust judgment emphasis only. They must not override required JSON schema, safety gates, artifact boundaries, persona/rule files, or order-execution constraints.",
            ]
        )
        lines.extend(f"- {item}" for item in extra_instructions)
    if stage == "analyst-review" and agent_role in COMBINED_ANALYST_REVIEW_ROLE_OUTPUTS:
        output_roles = COMBINED_ANALYST_REVIEW_ROLE_OUTPUTS[agent_role]
        lines.extend(
            [
                "",
                f"For this combined analyst-review task, return two independent view results for every symbol: {', '.join(output_roles)}.",
                "Use a separate pass for each view and evaluate that view only from its own rubric and supplied evidence.",
                "Do not let either view's score, reason_code, or one_line_reason depend on the other view's conclusion.",
                "Use today_trade_price_context to avoid same-day churn: compare last fill price/direction with current_or_last price before strengthening buy/sell opinions.",
                "Every score must be a JSON integer from 0 to 10; malformed, fractional, boolean, string, or out-of-range values invalidate the output.",
                "Carry the directional strength of supplied usable evidence in the score itself: keep scores close to neutral 5 only when that evidence is genuinely weakly directional, stale, or conflicting. Missing optional-domain coverage alone must not pull an included view toward 5. There is no confidence field.",
                f"Return each symbol with a views object keyed by {', '.join(output_roles)}; each view must contain score, reason_code, one_line_reason, and missing_data.",
            ]
        )
    if stage == "judge-review":
        candidate_directions = spec.get("candidate_directions") if isinstance(spec.get("candidate_directions"), dict) else {}
        thresholds = spec.get("score_band_thresholds") if isinstance(spec.get("score_band_thresholds"), dict) else {}
        sell_below = thresholds.get("sell_below", 4)
        buy_above = thresholds.get("buy_above", 6)
        portfolio_snapshot = spec.get("portfolio_snapshot") if isinstance(spec.get("portfolio_snapshot"), list) else []
        debate_bull = artifacts.get("debate_bull_persona")
        debate_bear = artifacts.get("debate_bear_persona")
        if debate_bull:
            lines.append(f"debate_bull_persona: {debate_bull}")
        if debate_bear:
            lines.append(f"debate_bear_persona: {debate_bear}")
        lines.extend(
            [
                "",
                "For judge-review, use the selected-symbol analyst-review slice from analyst_review; agent_scores excluded from aggregation are intentionally omitted from this judgment input.",
                "Optional evidence marked missing, failed, empty, unavailable, or excluded_from_aggregation is non-directional: its absence must not affect debate claims, weaknesses, rebuttals, reason_code, one_line_reason, or target_position_value_krw.",
                "Do not infer safety, risk, favorable news, thesis integrity, or thesis damage from the absence of optional evidence.",
                "Do not use optional-domain coverage counts or completeness to decide evidence sufficiency; judge only the directional strength and conflict of supplied usable evidence.",
                f"The supplied symbols are pre-selected candidates by score band: sell candidates are held symbols with final_first_score <= {sell_below}, buy candidates have final_first_score >= {buy_above}. Symbols between the bands are held by the pipeline and are not yours to decide.",
                "Direction preconditions are hard constraints: a sell candidate may only be sold (partial or full) or held (target_position_value_krw <= baseline); a buy candidate may only be bought or held (target_position_value_krw >= baseline). Violations are rejected by the pipeline.",
                "When the supplied usable evidence itself is insufficient or conflicting, the default decision is hold at the baseline.",
                "final_first_score is the simple mean of the included analyst view scores; per-analyst scores in agent_scores carry the evidence behind it.",
                "Return target_position_value_krw for every supplied symbol as the target KRW position value after this decision.",
                "The pipeline derives final_holding_quantity from target_position_value_krw / price.current_or_last with Decimal ROUND_HALF_UP; judge-supplied final_holding_quantity is optional and ignored for sizing.",
                "No additional buy, no extra exposure, or 추가 확대 없음 means target_position_value_krw must stay at the baseline (holding_quantity_context.expected_holding_quantity * price.current_or_last), not 0.",
                "If today_trade_timeline_context confirms a same-day buy, or its collection_status is partial/unavailable so same-day buy history is unknown, target_position_value_krw may exceed the baseline only when additional_buy_reason supplies new evidence or materially changed price/portfolio context.",
                "Treat an empty recent_trade_context.recent_submitted_trades list as confirmed absence only when coverage_status=complete; otherwise recent trade history is unknown and its absence is non-directional.",
                "For held sell candidates, an intact long-term thesis favors holding despite the low score; sell only when thesis damage, material adverse news/disclosure, or structural deterioration is supported by supplied evidence.",
                "Use strategy_context and symbol_strategy_context as advisory inputs for target_position_value_krw, not as order allow/block rules.",
                "Debate sub-agents: spawn the bull/bear debate sub-agents required by the judge persona, passing the debate persona file contents plus the listed input file paths; spawned sub-agents inherit every other restriction.",
                "Debate procedure: one base round (bull case, bear case, bull rebuttal, bear rebuttal) over all candidates in batch; at most one extra rebuttal round only for symbols you explicitly cannot decide; hard stop after two rounds; if debate agents fail after one retry each, decide without debate using the hold-at-baseline default.",
                "After the debate, compare each side's strongest argument per symbol; if they balance or evidence is insufficient, hold at the baseline. Reflect the decisive argument (or why both sides cancelled out) in one_line_reason. Do not include debate transcripts in the returned JSON.",
            ]
        )
        if candidate_directions:
            lines.append("candidate_directions: " + ",".join(f"{symbol}={direction}" for symbol, direction in sorted(candidate_directions.items())))
        if portfolio_snapshot:
            lines.extend(
                [
                    "Read-only portfolio snapshot of every held symbol (score/quantity/valuation/pnl_rate/candidate_direction); use it for portfolio-level sizing context only and never return decisions for snapshot-only symbols:",
                    "portfolio_snapshot: " + json.dumps(portfolio_snapshot, ensure_ascii=False, separators=(",", ":")),
                ]
            )

    lines.extend(
        [
            "",
            "Return JSON only in the required compact review format. human_markdown_path is informational; the Main agent creates that sidecar from JSON.",
            "Use short reason_code and one_line_reason fields instead of long rationale, risk, evidence, or prose arrays.",
        ]
    )
    return compact_prompt("\n".join(lines))


def build_prompt(spec: dict[str, Any]) -> str:
    return compact_review_prompt(spec) or compact_prompt(str(spec.get("prompt", "")))


def launcher_model_effort(stage: str, agent_role: str) -> tuple[str, str]:
    stage_key = stage.strip().lower()
    role_key = agent_role.strip().lower()
    model_config = load_subagent_model_config()

    if role_key in {"financial", "news"} or stage_key in COLLECTION_STAGES:
        entry = model_config["collection"]
        return entry["model"], entry["model_reasoning_effort"]
    if stage_key == "analyst-review":
        if role_key in SELECTED_ANALYST_REVIEW_ROLES:
            entry = model_config["analyst_review"]
            return entry["model"], entry["model_reasoning_effort"]
        selected = ", ".join(sorted(SELECTED_ANALYST_REVIEW_ROLES))
        raise ValueError(f"analyst-review agent_role must be one of: {selected}")
    if role_key in {"juror"} or role_key.startswith("juror-"):
        entry = model_config["analyst_review"]
        return entry["model"], entry["model_reasoning_effort"]
    if stage_key == "judge-review":
        if role_key == "judge":
            entry = model_config["judge_review"]
            return entry["model"], entry["model_reasoning_effort"]
        raise ValueError("judge-review agent_role must be judge")
    raise ValueError(f"unsupported daily-trading sub-agent stage/role: stage={stage!r}, agent_role={agent_role!r}")



def validate_spec(spec: dict[str, Any]) -> None:
    required = REQUIRED_SPEC_FIELDS
    compact_review_requested = is_compact_review_candidate(spec)
    if compact_review_requested:
        required = REQUIRED_SPEC_FIELDS - {"prompt"}
    missing = sorted(field for field in required if not str(spec.get(field, "")).strip())
    if missing:
        raise ValueError("missing required spec fields: " + ", ".join(missing))
    stage = str(spec.get("stage", "")).strip()
    if stage in REVIEW_STAGES and str(spec.get("prompt", "")).strip():
        raise ValueError("review raw prompt fallback is forbidden; use compact artifact_paths and symbol_ids")
    if compact_review_requested:
        artifacts = normalize_artifact_paths(spec.get("artifact_paths"))
        decision_brief = artifacts.get("decision_brief") or artifacts.get("decision-brief") or artifacts.get("brief")
        analyst_review = artifacts.get("analyst_review") or artifacts.get("analyst-review")
        symbols = normalize_symbol_ids(spec.get("symbol_ids") or spec.get("symbols"))
        if not decision_brief:
            raise ValueError("compact review spec requires artifact_paths.decision_brief")
        if stage == "judge-review" and not analyst_review:
            raise ValueError("judge-review compact spec requires artifact_paths.analyst_review")
        if not symbols:
            raise ValueError("compact review spec requires symbol_ids")
    agent_role = safe_name(str(spec.get("agent_role", ""))).lower()
    task_name = safe_name(str(spec.get("task_name", ""))).lower()
    if stage == "analyst-review":
        if agent_role not in SELECTED_ANALYST_REVIEW_ROLES:
            selected = ", ".join(sorted(SELECTED_ANALYST_REVIEW_ROLES))
            raise ValueError(f"analyst-review agent_role must be one of: {selected}")
    if stage == "judge-review":
        if agent_role != "judge":
            raise ValueError("judge-review agent_role must be judge")
        retry_numbers = [
            int(match.group(1))
            for match in re.finditer(r"(?:retry|attempt)-?(\d+)", task_name)
        ]
        if retry_numbers and max(retry_numbers) > 2:
            raise ValueError("judge retry is limited to at most 2 retries")


def parse_json_output(raw: str) -> tuple[Any | None, list[dict[str, Any]]]:
    if not raw.strip():
        return None, [{"code": "empty_output", "message": "codex exec returned no output"}]
    try:
        return json.loads(raw), []
    except json.JSONDecodeError as exc:
        return None, [
            {
                "code": "invalid_json",
                "message": f"{exc.msg} at line {exc.lineno} column {exc.colno}",
            }
        ]


def normalize_compact_review_payload(payload: Any, stage: str) -> Any:
    if not isinstance(payload, dict) or stage not in REVIEW_STAGES:
        return payload
    normalized = dict(payload)
    symbols = normalized.get("symbols")
    if isinstance(symbols, list):
        normalized_symbols: list[Any] = []
        for symbol in symbols:
            if not isinstance(symbol, dict):
                normalized_symbols.append(symbol)
                continue
            copied = dict(symbol)
            if stage == "analyst-review":
                copied.setdefault("missing_data", [])
                views = copied.get("views")
                if isinstance(views, dict):
                    copied_views = {}
                    for role, view in views.items():
                        if isinstance(view, dict):
                            copied_view = dict(view)
                            copied_view.setdefault("missing_data", [])
                            copied_views[role] = copied_view
                    copied["views"] = copied_views
            normalized_symbols.append(copied)
        normalized["symbols"] = normalized_symbols
    return normalized


def compact_review_payload_errors(payload: Any, stage: str, agent_role: str = "") -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                next_path = f"{path}.{key_text}" if path else key_text
                if key_text in DISALLOWED_COMPACT_REVIEW_KEYS:
                    errors.append(
                        {
                            "code": "disallowed_compact_review_key",
                            "message": f"compact review JSON must not include {next_path}",
                        }
                    )
                walk(item, next_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(payload, "")
    if not isinstance(payload, dict):
        errors.append({"code": "invalid_compact_review_schema", "message": "compact review JSON must be an object"})
        return errors
    if payload.get("stage") != stage:
        errors.append(
            {
                "code": "invalid_compact_review_schema",
                "message": f"compact review JSON stage must be {stage}",
            }
        )
    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        errors.append(
            {
                "code": "invalid_compact_review_schema",
                "message": "compact review JSON must include a non-empty symbols array",
            }
        )
        return errors
    for index, symbol in enumerate(symbols):
        if not isinstance(symbol, dict):
            errors.append(
                {
                    "code": "invalid_compact_review_schema",
                    "message": f"symbols[{index}] must be an object",
                }
            )
            continue
        views = symbol.get("views")
        output_roles = COMBINED_ANALYST_REVIEW_ROLE_OUTPUTS.get(agent_role, ())
        requires_combined_views = stage == "analyst-review" and bool(output_roles)
        has_combined_views = stage == "analyst-review" and bool(output_roles) and isinstance(views, dict) and all(
            isinstance(views.get(role), dict)
            for role in output_roles
        )
        if requires_combined_views and not has_combined_views:
            errors.append(
                {
                    "code": "invalid_compact_review_schema",
                    "message": f"symbols[{index}] for {agent_role} must include views.{', views.'.join(output_roles)}",
                }
            )
        required_symbol_fields = ("symbol_id", "symbol_name") if has_combined_views else ("symbol_id", "symbol_name", "reason_code", "one_line_reason")
        for field in required_symbol_fields:
            if field not in symbol:
                errors.append(
                    {
                        "code": "invalid_compact_review_schema",
                        "message": f"symbols[{index}] missing {field}",
                    }
                )
        if stage == "analyst-review":
            if has_combined_views:
                for role in output_roles:
                    view = views[role]
                    for field in ("score", "reason_code", "one_line_reason"):
                        if field not in view:
                            errors.append(
                                {
                                    "code": "invalid_compact_review_schema",
                                    "message": f"symbols[{index}].views.{role} missing {field}",
                                }
                            )
                    if "score" in view and review_score_value(view.get("score")) is None:
                        errors.append(
                            {
                                "code": "invalid_compact_review_schema",
                                "message": f"symbols[{index}].views.{role}.score must be an integer from 0 to 10",
                            }
                        )
            else:
                for field in ("score",):
                    if field not in symbol:
                        errors.append(
                            {
                                "code": "invalid_compact_review_schema",
                                "message": f"symbols[{index}] missing {field}",
                            }
                        )
                if "score" in symbol and review_score_value(symbol.get("score")) is None:
                    errors.append(
                        {
                            "code": "invalid_compact_review_schema",
                            "message": f"symbols[{index}].score must be an integer from 0 to 10",
                        }
                    )
        if stage == "judge-review":
            final_value = non_negative_int_value(symbol.get("final_holding_quantity")) if "final_holding_quantity" in symbol else None
            target_value = non_negative_number_value(symbol.get("target_position_value_krw")) if "target_position_value_krw" in symbol else None
            if "target_position_value_krw" not in symbol:
                errors.append(
                    {
                        "code": "invalid_compact_review_schema",
                        "message": f"symbols[{index}] missing target_position_value_krw",
                    }
                )
            if "target_position_value_krw" in symbol and target_value is None:
                errors.append(
                    {
                        "code": "invalid_compact_review_schema",
                        "message": f"symbols[{index}].target_position_value_krw must be a non-negative number",
                    }
                )
            if "final_holding_quantity" in symbol and final_value is None:
                errors.append(
                    {
                        "code": "invalid_compact_review_schema",
                        "message": f"symbols[{index}].final_holding_quantity must be a non-negative integer",
                    }
                )
            for field in ("relative_attractiveness_rank",):
                if field not in symbol:
                    errors.append(
                        {
                            "code": "invalid_compact_review_schema",
                            "message": f"symbols[{index}] missing {field}",
                        }
                    )
    return errors


def wrapper_paths(spec: dict[str, Any]) -> tuple[Path, Path]:
    output_dir = Path(str(spec["output_dir"]))
    task_name = safe_name(str(spec["task_name"]))
    subagent_dir = output_dir / "subagents"
    return subagent_dir / f"{task_name}.wrapper.json", subagent_dir / f"{task_name}.raw.txt"


def event_log_paths(spec: dict[str, Any]) -> tuple[Path, Path]:
    output_dir = Path(str(spec["output_dir"]))
    task_name = safe_name(str(spec["task_name"]))
    subagent_dir = output_dir / "subagents"
    return subagent_dir / f"{task_name}.events.jsonl", subagent_dir / f"{task_name}.stderr.txt"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def prune_path(path: Path) -> bool:
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError:
        return False
    return False


def event_log_retention_decision(mode: str, status: str, diagnostics: dict[str, Any]) -> tuple[bool, str]:
    if mode == "always":
        return True, "always"
    if mode == "never":
        return False, "never"
    if status != "success":
        return True, "failure"
    if mode == "failed":
        return False, "success"
    if diagnostics.get("anomaly_detected"):
        return True, "anomaly"
    return False, "no_anomaly"


def file_sha256(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def artifact_content_fingerprints(spec: dict[str, Any]) -> dict[str, str | None]:
    artifacts = normalize_artifact_paths(spec.get("artifact_paths"))
    workspace_dir = str(spec.get("workspace_dir", ""))
    fingerprints: dict[str, str | None] = {}
    for key, path_text in sorted(artifacts.items()):
        if key in {"persona", "persona_path", "review_format", "analyst-review-format"}:
            continue
        fingerprints[key] = file_sha256(resolve_artifact_path(path_text, workspace_dir))
    return fingerprints


def spec_fingerprint(spec: dict[str, Any]) -> str:
    relevant = {
        key: spec.get(key)
        for key in (
            "run_id",
            "started_at",
            "stage",
            "agent_role",
            "task_name",
            "prompt",
            "workspace_dir",
            "output_dir",
            "artifact_paths",
            "symbol_ids",
            "symbols",
        )
    }
    relevant["artifact_content_sha256"] = artifact_content_fingerprints(spec)
    encoded = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def existing_success_wrapper(spec: dict[str, Any], fingerprint: str) -> dict[str, Any] | None:
    if env_bool("CODEX_SUBAGENT_REUSE_SUCCESS", True) is False:
        return None
    wrapper_path, _raw_output_path = wrapper_paths(spec)
    if not wrapper_path.exists():
        return None
    try:
        wrapper = load_json(wrapper_path)
    except Exception:
        return None
    if not isinstance(wrapper, dict):
        return None
    if wrapper.get("status") != "success":
        return None
    if wrapper.get("spec_fingerprint") != fingerprint:
        return None
    wrapper["reused_existing_wrapper"] = True
    return wrapper


def reusable_success_wrapper(spec: dict[str, Any]) -> dict[str, Any] | None:
    validate_spec(spec)
    return existing_success_wrapper(spec, spec_fingerprint(spec))


def run_one(spec: dict[str, Any]) -> dict[str, Any]:
    validate_spec(spec)
    fingerprint = spec_fingerprint(spec)
    reused = existing_success_wrapper(spec, fingerprint)
    if reused is not None:
        return reused
    model, effort = launcher_model_effort(str(spec["stage"]), str(spec["agent_role"]))
    wrapper_path, raw_output_path = wrapper_paths(spec)
    event_log_path, stderr_path = event_log_paths(spec)
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)
    slice_paths = write_review_input_slices(spec)
    prompt_spec = spec_with_review_slices(spec, slice_paths)
    prompt_mode = "compact_review" if compact_review_prompt(prompt_spec) else "raw"

    started_at = now_iso()
    started = time.monotonic()
    cmd = [
        os.getenv("CODEX_BIN", "codex"),
        "exec",
        "--json",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "--skip-git-repo-check",
        "-o",
        str(raw_output_path),
    ]
    if env_bool("CODEX_BYPASS_APPROVALS_AND_SANDBOX", True):
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    cmd.append(build_prompt(prompt_spec))

    env = os.environ.copy()
    if env.get("CODEX_HOME"):
        env["CODEX_HOME"] = env["CODEX_HOME"]
    if env.get("CODEX_MCP_TRADING_ENV"):
        env["CODEX_MCP_TRADING_ENV"] = env["CODEX_MCP_TRADING_ENV"]

    errors: list[dict[str, Any]] = []
    returncode: int | None = None
    stdout = ""
    stderr = ""
    try:
        result = subprocess.run(
            cmd,
            cwd=Path(str(spec["workspace_dir"])),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=int(os.getenv("CODEX_SUBAGENT_TIMEOUT_SECONDS", os.getenv("CODEX_TIMEOUT_SECONDS", "1800"))),
            check=False,
        )
        returncode = result.returncode
        stdout = result.stdout or ""
        stderr = result.stderr or ""
    except Exception as exc:  # noqa: BLE001 - wrapper records sub-agent failures
        errors.append({"code": "exec_failed", "message": str(exc)})

    write_text(event_log_path, stdout)
    write_text(stderr_path, stderr)
    event_summary = parse_codex_json_events(stdout)
    event_diagnostics = summarize_codex_event_stream(stdout, event_summary["token_usage"])
    event_diagnostics["stderr_bytes"] = len(stderr.encode("utf-8", errors="replace"))
    if raw_output_path.exists():
        raw_output = raw_output_path.read_text(encoding="utf-8", errors="replace")
    else:
        raw_output = event_summary.get("last_agent_message") or stdout.strip()
        raw_output_path.write_text(raw_output, encoding="utf-8")

    stage = str(spec["stage"])
    parsed_json = None
    parsed_text = None
    parse_errors: list[dict[str, Any]] = []
    text_errors: list[dict[str, Any]] = []
    compact_review_errors: list[dict[str, Any]] = []
    if stage in TEXT_OUTPUT_STAGES:
        # Collection text stages return cache paths, fixed missing-cache messages,
        # or concise Markdown summaries. The launcher records that text and
        # intentionally does not validate path existence.
        parsed_text = raw_output.strip()
        if not parsed_text:
            text_errors.append({"code": "empty_output", "message": "codex exec returned no text/path output"})
        errors.extend(text_errors)
    else:
        parsed_json, parse_errors = parse_json_output(raw_output)
        errors.extend(parse_errors)
        if stage in REVIEW_STAGES and prompt_mode == "compact_review" and parsed_json is not None:
            parsed_json = normalize_compact_review_payload(parsed_json, stage)
            compact_review_errors = compact_review_payload_errors(parsed_json, stage, str(spec.get("agent_role") or ""))
            errors.extend(compact_review_errors)
    if returncode not in (0, None):
        errors.append({"code": "nonzero_returncode", "message": f"codex exec exited with {returncode}"})
    if stderr.strip():
        errors.append({"code": "stderr", "message": stderr.strip()[-2000:]})

    ended_at = now_iso()
    duration_ms = int((time.monotonic() - started) * 1000)
    if stage in TEXT_OUTPUT_STAGES:
        status = "success" if returncode == 0 and parsed_text and not text_errors else "failed"
    else:
        status = "success" if returncode == 0 and parsed_json is not None and not parse_errors and not compact_review_errors else "failed"
    retention = raw_retention_mode()
    raw_output_retained = True
    if raw_output_path.exists() and (retention == "never" or (retention == "failed" and status == "success")):
        raw_output_path.unlink()
        raw_output_retained = False
    event_retention = event_retention_mode()
    event_log_retained, event_retention_reason = event_log_retention_decision(event_retention, status, event_diagnostics)
    stderr_retained = event_log_retained
    if not event_log_retained:
        prune_path(event_log_path)
        prune_path(stderr_path)
    elif stderr == "":
        stderr_retained = False
        prune_path(stderr_path)
    wrapper = {
        "schema_version": "1",
        "run_id": str(spec["run_id"]),
        "run_started_at": str(spec["started_at"]),
        "stage": str(spec["stage"]),
        "agent_role": str(spec["agent_role"]),
        "task_name": str(spec["task_name"]),
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "returncode": returncode,
        "raw_output_path": str(raw_output_path),
        "raw_output_retained": raw_output_retained,
        "raw_retention": retention,
        "event_log_path": str(event_log_path),
        "event_log_retained": event_log_retained,
        "event_retention": event_retention,
        "event_retention_reason": event_retention_reason,
        "stderr_path": str(stderr_path),
        "stderr_retained": stderr_retained,
        "event_diagnostics": event_diagnostics,
        "parsed_json": parsed_json,
        "parsed_text": parsed_text,
        "errors": errors,
        "command": [part for part in cmd[:-1]],
        "prompt_mode": prompt_mode,
        "review_input_paths": slice_paths,
        "spec_fingerprint": fingerprint,
        "reused_existing_wrapper": False,
        "token_usage": event_summary["token_usage"],
        "token_usage_event_count": event_summary["token_usage_event_count"],
        "rate_limits": event_summary["rate_limits"],
    }
    write_json(wrapper_path, wrapper)
    return wrapper


def group_specs(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        specs = payload
    elif isinstance(payload, dict):
        specs = payload.get("specs") or payload.get("tasks") or payload.get("stages")
    else:
        specs = None
    if not isinstance(specs, list) or not all(isinstance(item, dict) for item in specs):
        raise ValueError("run-group spec must be a JSON list or an object with specs/tasks/stages list")
    return specs


def run_group(specs: list[dict[str, Any]], max_workers: int | None = None) -> dict[str, Any]:
    wrappers: list[dict[str, Any]] = []
    pending_specs: list[dict[str, Any]] = []
    for spec in specs:
        reused = reusable_success_wrapper(spec)
        if reused is None:
            pending_specs.append(spec)
        else:
            wrappers.append(reused)
    if pending_specs:
        workers = max_workers or min(8, max(1, len(pending_specs)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(run_one, spec): spec for spec in pending_specs}
            for future in as_completed(future_map):
                wrappers.append(future.result())
    wrappers.sort(key=lambda item: str(item.get("task_name", "")))
    failed = [item for item in wrappers if item.get("status") != "success"]
    required_failed = [
        item
        for item in failed
        if str(item.get("stage", "")).strip() not in OPTIONAL_GROUP_FAILURE_STAGES
    ]
    optional_failed = [item for item in failed if item not in required_failed]
    if required_failed:
        status = "failed"
    elif optional_failed:
        status = "partial"
    else:
        status = "success"
    return {
        "schema_version": "1",
        "status": status,
        "count": len(wrappers),
        "failed_count": len(failed),
        "required_failed_count": len(required_failed),
        "optional_failed_count": len(optional_failed),
        "wrappers": wrappers,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run daily-trading sub-agents with configured model/effort.")
    parser.add_argument("--self-test", action="store_true", help="Run launcher self-tests with a fake codex binary.")
    subparsers = parser.add_subparsers(dest="command")

    run_one_parser = subparsers.add_parser("run-one", help="Run one sub-agent spec.")
    run_one_parser.add_argument("--spec", type=Path, required=True, help="JSON stage spec file.")

    run_group_parser = subparsers.add_parser("run-group", help="Run independent sub-agent specs in parallel.")
    run_group_parser.add_argument("--spec", type=Path, required=True, help="JSON group spec file.")
    run_group_parser.add_argument("--max-workers", type=int, default=None)

    subparsers.add_parser("self-test", help="Run launcher self-tests with a fake codex binary.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_test()
    if args.command == "self-test":
        return run_self_test()
    if args.command == "run-one":
        print(json.dumps(run_one(load_json(args.spec)), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "run-group":
        payload = load_json(args.spec)
        print(
            json.dumps(
                run_group(group_specs(payload), max_workers=args.max_workers),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    raise SystemExit("a subcommand is required")


def run_self_test() -> int:
    """Run the extracted test suite through the legacy CLI contract."""
    codex_exec_root = Path(__file__).resolve().parents[4]
    codex_exec_root_text = str(codex_exec_root)
    if codex_exec_root_text not in sys.path:
        sys.path.insert(0, codex_exec_root_text)

    from service.pipelines.daily_trading.tests.test_run_subagent import (
        run_self_test as run_external_self_test,
    )

    return run_external_self_test()


if __name__ == "__main__":
    raise SystemExit(main())
