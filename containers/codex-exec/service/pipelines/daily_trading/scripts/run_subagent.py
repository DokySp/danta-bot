#!/usr/bin/env python3
"""Run daily-trading sub-agent stages through codex exec."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import tempfile
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
    "collection": {"model": "gpt-5.4-mini", "model_reasoning_effort": "low"},
    "first_verdict": {"model": "gpt-5.5", "model_reasoning_effort": "medium"},
    "second_verdict": {"model": "gpt-5.5", "model_reasoning_effort": "medium"},
}
SUBAGENT_MODEL_CONFIG_KEYS = ("collection", "first_verdict", "second_verdict")
VALID_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
COLLECTION_STAGES = {"financial-collection", "news-collection"}
FINANCIAL_PATH_OUTPUT_STAGES = {"financial-collection"}
NEWS_PATH_OUTPUT_STAGES = {"news-collection"}
TEXT_OUTPUT_STAGES = FINANCIAL_PATH_OUTPUT_STAGES | NEWS_PATH_OUTPUT_STAGES
OPTIONAL_GROUP_FAILURE_STAGES = TEXT_OUTPUT_STAGES
VERDICT_STAGES = {"first-verdict", "second-verdict"}
SELECTED_FIRST_VERDICT_ROLES = {
    "analyst-fundamental-risk",
    "analyst-market-news",
}
MARKET_INDEX_SNAPSHOT_AGENT_ROLES = {"analyst-fundamental-risk", "analyst-market-news", "judge-final"}
COMBINED_FIRST_VERDICT_ROLE_OUTPUTS = {
    "analyst-fundamental-risk": (
        "analyst-quality-value",
        "analyst-risk-allocation",
    ),
    "analyst-market-news": (
        "analyst-momentum-cycle",
        "analyst-news-flow",
    ),
}
FIRST_VERDICT_VIEW_INPUT_FIELDS = {
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
FIRST_VERDICT_ALWAYS_SYMBOL_FIELDS = {
    "symbol_id",
    "symbol",
    "symbol_name",
    "code",
    "name",
    "market",
    "eligible_for_verdict",
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
DISALLOWED_COMPACT_VERDICT_KEYS = {
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
    for key, entry in config.items():
        effort = entry["model_reasoning_effort"]
        if effort not in VALID_REASONING_EFFORTS:
            raise ValueError(f"unsupported model_reasoning_effort for {key}: {effort}")
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


def first_verdict_output_roles(agent_role: str) -> tuple[str, ...]:
    role = safe_name(agent_role).lower()
    return COMBINED_FIRST_VERDICT_ROLE_OUTPUTS.get(role, (role,))


def first_verdict_symbol_fields(agent_role: str) -> set[str] | None:
    roles = first_verdict_output_roles(agent_role)
    if not roles:
        return None
    fields = set(FIRST_VERDICT_ALWAYS_SYMBOL_FIELDS)
    matched = False
    for role in roles:
        role_fields = FIRST_VERDICT_VIEW_INPUT_FIELDS.get(role)
        if role_fields:
            fields.update(role_fields)
            matched = True
    return fields if matched else None


def filter_symbol_fields_for_agent(payload: Any, agent_role: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    fields = first_verdict_symbol_fields(agent_role)
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
    filtered["slice_output_roles"] = list(first_verdict_output_roles(agent_role))
    return filtered


def build_verdict_core_payload(payload: Any, symbol_ids: list[str], agent_role: str = "") -> Any:
    filtered = filter_symbols(payload, symbol_ids)
    if not isinstance(filtered, dict):
        return filtered
    core = dict(filter_symbol_fields_for_agent(filtered, agent_role) if agent_role else filtered)
    if agent_role and safe_name(agent_role).lower() not in MARKET_INDEX_SNAPSHOT_AGENT_ROLES:
        core.pop("market_index_snapshot", None)
    core["slice_type"] = "verdict-core"
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
        "target_holding_quantity_semantics": "final total holding quantity after active pending/reserved orders; not order quantity",
        "direction_examples": {
            "maintain": expected,
            "increase_by_1": expected + 1,
            "reduce_by_1": max(0, expected - 1),
        },
    }


def run_started_sort_key(run_dir: Path, payload: dict[str, Any]) -> tuple[str, str]:
    started_at = str(payload.get("started_at") or "").strip()
    return (started_at or run_dir.name, run_dir.name)


def recent_submitted_trade_context(output_dir: Path, symbol_id: str, run_limit: int = 2) -> dict[str, Any]:
    if not symbol_id or run_limit <= 0:
        return {"recent_submitted_trades": [], "policy": "no recent submitted trade context"}
    runs_dir = output_dir.parent
    if not runs_dir.is_dir():
        return {"recent_submitted_trades": [], "policy": "no recent submitted trade context"}

    previous_runs: list[tuple[tuple[str, str], Path, dict[str, Any]]] = []
    for run_dir in (path for path in runs_dir.iterdir() if path.is_dir()):
        if run_dir.resolve() == output_dir.resolve():
            continue
        payload = read_json_if_exists(run_dir / "execution.json")
        if isinstance(payload, dict):
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
                    "target_holding_quantity": int_or_zero(order.get("target_holding_quantity")),
                    "additional_required_quantity": int_or_zero(order.get("additional_required_quantity")),
                    "order_path": order.get("order_path") or "",
                    "order_api": order.get("order_api") or "",
                    "reason": str(order.get("reason") or "")[:120],
                }
            )
    return {
        "recent_submitted_trades": trades,
        "inspected_run_ids": inspected_runs,
        "policy": "Use recent submitted trades as reassessment input, not a default hold/block reason; same-direction additional trades and opposite-direction target changes are allowed after explicitly reassessing price movement, final_first_score, risk, order/fill state, and thesis evidence.",
    }


def add_second_verdict_holding_context(payload: Any, output_dir: Path | None = None) -> Any:
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


def build_verdict_first_slice_payload(payload: Any, symbol_ids: list[str]) -> Any:
    filtered = filter_symbols(payload, symbol_ids)
    if not isinstance(filtered, dict):
        return filtered
    sliced = dict(filtered)
    sliced["slice_type"] = "verdict-first-slice"
    sliced["source_stage"] = filtered.get("stage") or "verdict-first"
    return sliced


def write_verdict_input_slices(spec: dict[str, Any]) -> dict[str, str]:
    if not is_compact_verdict_spec(spec):
        return {}
    stage = str(spec.get("stage", "")).strip()
    artifacts = normalize_artifact_paths(spec.get("artifact_paths"))
    symbols = normalize_symbol_ids(spec.get("symbol_ids") or spec.get("symbols"))
    decision_brief = artifacts.get("decision_brief") or artifacts.get("decision-brief") or artifacts.get("brief")
    if not decision_brief or not symbols:
        return {}

    workspace_dir = str(spec.get("workspace_dir", ""))
    output_dir = Path(str(spec["output_dir"]))
    slice_dir = output_dir / "verdict-inputs"
    task_name = safe_name(str(spec["task_name"]))
    slice_paths: dict[str, str] = {}

    sources = [("decision_brief", decision_brief)]
    if stage == "second-verdict":
        sources.append(("verdict_first", artifacts.get("verdict_first") or artifacts.get("verdict-first") or ""))

    for artifact_key, source_path_text in sources:
        if not source_path_text:
            continue
        source_path = resolve_artifact_path(source_path_text, workspace_dir)
        payload = read_json_if_exists(source_path)
        if payload is None:
            continue
        if artifact_key == "decision_brief":
            sliced = build_verdict_core_payload(payload, symbols, str(spec.get("agent_role") or ""))
            if stage == "second-verdict":
                sliced = add_second_verdict_holding_context(sliced, output_dir)
            relative_name = "verdict-core"
            slice_paths["verdict_core"] = str(slice_dir / f"{task_name}.{relative_name}.json")
        else:
            sliced = build_verdict_first_slice_payload(payload, symbols)
            relative_name = "verdict-first-slice"
        slice_path = slice_dir / f"{task_name}.{relative_name}.json"
        write_json(slice_path, sliced)
        slice_paths[artifact_key] = str(slice_path)
    return slice_paths


def spec_with_verdict_slices(spec: dict[str, Any], slice_paths: dict[str, str]) -> dict[str, Any]:
    if not slice_paths:
        return spec
    copied = dict(spec)
    artifacts = dict(normalize_artifact_paths(copied.get("artifact_paths")))
    if "decision_brief" in slice_paths:
        artifacts["decision_brief"] = slice_paths["decision_brief"]
    if "verdict_first" in slice_paths:
        artifacts["verdict_first"] = slice_paths["verdict_first"]
    copied["artifact_paths"] = artifacts
    return copied


def is_compact_verdict_spec(spec: dict[str, Any]) -> bool:
    if str(spec.get("stage", "")).strip() not in VERDICT_STAGES:
        return False
    if str(spec.get("prompt", "")).strip():
        return False
    artifacts = normalize_artifact_paths(spec.get("artifact_paths"))
    decision_brief = artifacts.get("decision_brief") or artifacts.get("decision-brief") or artifacts.get("brief")
    symbols = normalize_symbol_ids(spec.get("symbol_ids") or spec.get("symbols"))
    return bool(decision_brief and symbols)


def is_compact_verdict_candidate(spec: dict[str, Any]) -> bool:
    return (
        str(spec.get("stage", "")).strip() in VERDICT_STAGES
        and not str(spec.get("prompt", "")).strip()
        and (spec.get("artifact_paths") is not None or spec.get("symbol_ids") is not None or spec.get("symbols") is not None)
    )


def compact_verdict_prompt(spec: dict[str, Any]) -> str | None:
    if not is_compact_verdict_spec(spec):
        return None
    stage = str(spec.get("stage", "")).strip()
    artifacts = normalize_artifact_paths(spec.get("artifact_paths"))
    symbols = normalize_symbol_ids(spec.get("symbol_ids") or spec.get("symbols"))
    decision_brief = artifacts.get("decision_brief") or artifacts.get("decision-brief") or artifacts.get("brief")
    verdict_first = artifacts.get("verdict_first") or artifacts.get("verdict-first")
    persona = artifacts.get("persona") or artifacts.get("persona_path")
    verdict_format = artifacts.get("verdict_format") or artifacts.get("verdict-format")
    output_dir = str(spec.get("output_dir", "")).strip()
    task_name = safe_name(str(spec.get("task_name", "")))
    agent_role = safe_name(str(spec.get("agent_role", "")))
    sidecar_path = f"{output_dir}/verdicts/{stage}--{agent_role}--{task_name}.md"

    lines = [
        "Daily-trading verdict sub-agent.",
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
    if verdict_first:
        lines.append(f"verdict_first: {verdict_first}")
    if persona:
        lines.append(f"persona: {persona}")
    if verdict_format:
        lines.append(f"verdict_format: {verdict_format}")
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
    if stage == "first-verdict" and agent_role in COMBINED_FIRST_VERDICT_ROLE_OUTPUTS:
        output_roles = COMBINED_FIRST_VERDICT_ROLE_OUTPUTS[agent_role]
        lines.extend(
            [
                "",
                f"For this combined first-verdict task, return two independent view results for every symbol: {', '.join(output_roles)}.",
                "Use a separate pass for each view and evaluate that view only from its own rubric and supplied evidence.",
                "Do not let either view's score, confidence, reason_code, or one_line_reason depend on the other view's conclusion.",
                "Use today_trade_price_context to avoid same-day churn: compare last fill price/direction with current_or_last price before strengthening buy/sell opinions.",
                f"Return each symbol with a views object keyed by {', '.join(output_roles)}; each view must contain score, confidence, reason_code, one_line_reason, and missing_data.",
            ]
        )
    if stage == "second-verdict":
        lines.extend(
            [
                "",
                "For second-verdict, use the lossless selected-symbol first-verdict slice from verdict_first.",
                "Interpret final_first_score as the unrounded confidence-adjusted first-verdict score: >= 6 is a buy/increase candidate, <= 4 is a reduce/exit candidate, and 5 is neutral.",
                "When referring to per-analyst scores in agent_scores, use confidence_adjusted_score as the score; score and confidence are supporting inputs explaining that adjusted score.",
                "If a symbol's first-verdict score is missing, unavailable, or unusable, treat it as neutral 5 and continue.",
                "First-verdict scores are judgment inputs, not hard buy/sell gates.",
                "Use recent_trade_context as reassessment input, not a default hold/block reason: same-direction additional trades and opposite-direction target changes are allowed after explicitly reassessing price movement, final_first_score, risk, order/fill state, and thesis evidence.",
                "Use today_trade_timeline_context as reassessment input, not a same-day churn block: same-direction additional trades and opposite-direction target changes are allowed after explicitly reassessing price movement, final_first_score, risk, order/fill state, and thesis evidence.",
                "Treat long_term_thesis_intact as a sell/reduce suppression signal, not as add permission; increase only when add_allowed evidence is also present.",
            ]
        )

    lines.extend(
        [
            "",
            "Return JSON only in the required compact verdict format. human_markdown_path is informational; the Main agent creates that sidecar from JSON.",
            "Use short reason_code and one_line_reason fields instead of long rationale, risk, evidence, or prose arrays.",
            "Optional financial/news absence is context only and must not lower score, target, or eligibility by itself.",
        ]
    )
    return compact_prompt("\n".join(lines))


def build_prompt(spec: dict[str, Any]) -> str:
    return compact_verdict_prompt(spec) or compact_prompt(str(spec.get("prompt", "")))


def launcher_model_effort(stage: str, agent_role: str) -> tuple[str, str]:
    stage_key = stage.strip().lower()
    role_key = agent_role.strip().lower()
    model_config = load_subagent_model_config()

    if role_key in {"financial", "news"} or stage_key in COLLECTION_STAGES:
        entry = model_config["collection"]
        return entry["model"], entry["model_reasoning_effort"]
    if stage_key == "first-verdict":
        if role_key in SELECTED_FIRST_VERDICT_ROLES:
            entry = model_config["first_verdict"]
            return entry["model"], entry["model_reasoning_effort"]
        selected = ", ".join(sorted(SELECTED_FIRST_VERDICT_ROLES))
        raise ValueError(f"first-verdict agent_role must be one of: {selected}")
    if role_key in {"juror"} or role_key.startswith("juror-"):
        entry = model_config["first_verdict"]
        return entry["model"], entry["model_reasoning_effort"]
    if stage_key == "second-verdict":
        if role_key == "judge-final":
            entry = model_config["second_verdict"]
            return entry["model"], entry["model_reasoning_effort"]
        raise ValueError("second-verdict agent_role must be judge-final")
    raise ValueError(f"unsupported daily-trading sub-agent stage/role: stage={stage!r}, agent_role={agent_role!r}")


def assert_unsupported_stage_rejected() -> None:
    try:
        launcher_model_effort("unsupported-stage", "unsupported-role")
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported daily-trading sub-agent stage/role was accepted")
    try:
        launcher_model_effort("unsupported-stage", "judge-final")
    except ValueError:
        return
    raise AssertionError("unsupported daily-trading sub-agent stage/role was accepted")


def assert_model_effort(stage: str, agent_role: str, *, model: str, effort: str) -> None:
    actual_model, actual_effort = launcher_model_effort(stage, agent_role)
    if (actual_model, actual_effort) != (model, effort):
        raise AssertionError(
            f"expected {model}/{effort} for {stage}/{agent_role}, got {actual_model}/{actual_effort}"
        )


def assert_all_supported_stages_use_expected_models() -> None:
    model_config = load_subagent_model_config()
    cases = [
        (
            "financial-collection",
            "financial",
            model_config["collection"]["model"],
            model_config["collection"]["model_reasoning_effort"],
        ),
        (
            "news-collection",
            "news",
            model_config["collection"]["model"],
            model_config["collection"]["model_reasoning_effort"],
        ),
        (
            "first-verdict",
            "analyst-market-news",
            model_config["first_verdict"]["model"],
            model_config["first_verdict"]["model_reasoning_effort"],
        ),
        (
            "second-verdict",
            "judge-final",
            model_config["second_verdict"]["model"],
            model_config["second_verdict"]["model_reasoning_effort"],
        ),
    ]
    for stage, role, model, effort in cases:
        assert_model_effort(stage, role, model=model, effort=effort)

def validate_spec(spec: dict[str, Any]) -> None:
    required = REQUIRED_SPEC_FIELDS
    compact_verdict_requested = is_compact_verdict_candidate(spec)
    if compact_verdict_requested:
        required = REQUIRED_SPEC_FIELDS - {"prompt"}
    missing = sorted(field for field in required if not str(spec.get(field, "")).strip())
    if missing:
        raise ValueError("missing required spec fields: " + ", ".join(missing))
    stage = str(spec.get("stage", "")).strip()
    if stage in VERDICT_STAGES and str(spec.get("prompt", "")).strip():
        raise ValueError("verdict raw prompt fallback is forbidden; use compact artifact_paths and symbol_ids")
    if compact_verdict_requested:
        artifacts = normalize_artifact_paths(spec.get("artifact_paths"))
        decision_brief = artifacts.get("decision_brief") or artifacts.get("decision-brief") or artifacts.get("brief")
        verdict_first = artifacts.get("verdict_first") or artifacts.get("verdict-first")
        symbols = normalize_symbol_ids(spec.get("symbol_ids") or spec.get("symbols"))
        if not decision_brief:
            raise ValueError("compact verdict spec requires artifact_paths.decision_brief")
        if stage == "second-verdict" and not verdict_first:
            raise ValueError("second-verdict compact spec requires artifact_paths.verdict_first")
        if not symbols:
            raise ValueError("compact verdict spec requires symbol_ids")
    agent_role = safe_name(str(spec.get("agent_role", ""))).lower()
    task_name = safe_name(str(spec.get("task_name", ""))).lower()
    if stage == "first-verdict":
        if agent_role not in SELECTED_FIRST_VERDICT_ROLES:
            selected = ", ".join(sorted(SELECTED_FIRST_VERDICT_ROLES))
            raise ValueError(f"first-verdict agent_role must be one of: {selected}")
    if stage == "second-verdict":
        if agent_role != "judge-final":
            raise ValueError("second-verdict agent_role must be judge-final")
        retry_numbers = [
            int(match.group(1))
            for match in re.finditer(r"(?:retry|attempt)-?(\d+)", task_name)
        ]
        if retry_numbers and max(retry_numbers) > 2:
            raise ValueError("judge-final retry is limited to at most 2 retries")


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


def normalize_compact_verdict_payload(payload: Any, stage: str) -> Any:
    if not isinstance(payload, dict) or stage not in VERDICT_STAGES:
        return payload
    normalized = dict(payload)
    if not isinstance(normalized.get("symbols"), list):
        for key in ("verdicts", "results", "items"):
            if isinstance(normalized.get(key), list):
                normalized["symbols"] = normalized[key]
                break
    symbols = normalized.get("symbols")
    if isinstance(symbols, list):
        normalized_symbols: list[Any] = []
        for symbol in symbols:
            if not isinstance(symbol, dict):
                normalized_symbols.append(symbol)
                continue
            copied = dict(symbol)
            if stage == "first-verdict":
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


def compact_verdict_payload_errors(payload: Any, stage: str, agent_role: str = "") -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                next_path = f"{path}.{key_text}" if path else key_text
                if key_text in DISALLOWED_COMPACT_VERDICT_KEYS:
                    errors.append(
                        {
                            "code": "disallowed_compact_verdict_key",
                            "message": f"compact verdict JSON must not include {next_path}",
                        }
                    )
                walk(item, next_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(payload, "")
    if not isinstance(payload, dict):
        errors.append({"code": "invalid_compact_verdict_schema", "message": "compact verdict JSON must be an object"})
        return errors
    if payload.get("stage") != stage:
        errors.append(
            {
                "code": "invalid_compact_verdict_schema",
                "message": f"compact verdict JSON stage must be {stage}",
            }
        )
    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        errors.append(
            {
                "code": "invalid_compact_verdict_schema",
                "message": "compact verdict JSON must include a non-empty symbols array",
            }
        )
        return errors
    for index, symbol in enumerate(symbols):
        if not isinstance(symbol, dict):
            errors.append(
                {
                    "code": "invalid_compact_verdict_schema",
                    "message": f"symbols[{index}] must be an object",
                }
            )
            continue
        views = symbol.get("views")
        output_roles = COMBINED_FIRST_VERDICT_ROLE_OUTPUTS.get(agent_role, ())
        requires_combined_views = stage == "first-verdict" and bool(output_roles)
        has_combined_views = stage == "first-verdict" and bool(output_roles) and isinstance(views, dict) and all(
            isinstance(views.get(role), dict)
            for role in output_roles
        )
        if requires_combined_views and not has_combined_views:
            errors.append(
                {
                    "code": "invalid_compact_verdict_schema",
                    "message": f"symbols[{index}] for {agent_role} must include views.{', views.'.join(output_roles)}",
                }
            )
        required_symbol_fields = ("symbol_id", "symbol_name") if has_combined_views else ("symbol_id", "symbol_name", "reason_code", "one_line_reason")
        for field in required_symbol_fields:
            if field not in symbol:
                errors.append(
                    {
                        "code": "invalid_compact_verdict_schema",
                        "message": f"symbols[{index}] missing {field}",
                    }
                )
        if stage == "first-verdict":
            if has_combined_views:
                for role in output_roles:
                    view = views[role]
                    for field in ("score", "confidence", "reason_code", "one_line_reason"):
                        if field not in view:
                            errors.append(
                                {
                                    "code": "invalid_compact_verdict_schema",
                                    "message": f"symbols[{index}].views.{role} missing {field}",
                                }
                            )
            else:
                for field in ("score", "confidence"):
                    if field not in symbol:
                        errors.append(
                            {
                                "code": "invalid_compact_verdict_schema",
                                "message": f"symbols[{index}] missing {field}",
                            }
                        )
        if stage == "second-verdict":
            for field in ("target_holding_quantity", "relative_attractiveness_rank"):
                if field not in symbol:
                    errors.append(
                        {
                            "code": "invalid_compact_verdict_schema",
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
        if key in {"persona", "persona_path", "verdict_format", "verdict-format"}:
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
    slice_paths = write_verdict_input_slices(spec)
    prompt_spec = spec_with_verdict_slices(spec, slice_paths)
    prompt_mode = "compact_verdict" if compact_verdict_prompt(prompt_spec) else "raw"

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
    compact_verdict_errors: list[dict[str, Any]] = []
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
        if stage in VERDICT_STAGES and prompt_mode == "compact_verdict" and parsed_json is not None:
            parsed_json = normalize_compact_verdict_payload(parsed_json, stage)
            compact_verdict_errors = compact_verdict_payload_errors(parsed_json, stage, str(spec.get("agent_role") or ""))
            errors.extend(compact_verdict_errors)
    if returncode not in (0, None):
        errors.append({"code": "nonzero_returncode", "message": f"codex exec exited with {returncode}"})
    if stderr.strip():
        errors.append({"code": "stderr", "message": stderr.strip()[-2000:]})

    ended_at = now_iso()
    duration_ms = int((time.monotonic() - started) * 1000)
    if stage in TEXT_OUTPUT_STAGES:
        status = "success" if returncode == 0 and parsed_text and not text_errors else "failed"
    else:
        status = "success" if returncode == 0 and parsed_json is not None and not parse_errors and not compact_verdict_errors else "failed"
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
        "verdict_input_paths": slice_paths,
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


def fake_codex_script(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

argv_path = Path(os.environ["FAKE_CODEX_ARGV_LOG"])
with argv_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")

output_path = None
for index, arg in enumerate(sys.argv):
    if arg == "-o" and index + 1 < len(sys.argv):
        output_path = Path(sys.argv[index + 1])
        break
if output_path is None:
    print("missing -o", file=sys.stderr)
    sys.exit(2)
output_path.parent.mkdir(parents=True, exist_ok=True)
task_name = output_path.name.removesuffix(".raw.txt")
empty_tasks = {item.strip() for item in os.environ.get("FAKE_CODEX_EMPTY_TASKS", "").split(",") if item.strip()}
if task_name in empty_tasks:
    output_path.write_text("", encoding="utf-8")
    sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
if os.environ.get("FAKE_CODEX_INVALID_JSON") == "1":
    output_path.write_text("not json", encoding="utf-8")
else:
    if "financial" in task_name or "news" in task_name:
        domain = "financial" if "financial" in task_name else "news"
        payload = {
            "schema_version": "1",
            "run_id": "self-test",
            "started_at": "2026-06-08T09:00:00+09:00",
            "generated_at": "2026-06-08T09:00:01+09:00",
            "stage": f"{domain}-collection",
            "domain": domain,
            "status": "success",
            "skipped": False,
            "skip_reason": "",
            "attempts": [],
            "errors": [],
            "symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "errors": []}],
        }
    else:
        prompt = sys.argv[-1] if sys.argv else ""
        if "stage: first-verdict" in prompt or "stage: second-verdict" in prompt:
            stage = "second-verdict" if "stage: second-verdict" in prompt else "first-verdict"
            if "agent_role: analyst-fundamental-risk" in prompt:
                first_payload = {
                    "views": {
                        "analyst-quality-value": {
                            "score": 5,
                            "confidence": 5,
                            "reason_code": "hold_neutral",
                            "one_line_reason": "quality self-test",
                            "missing_data": [],
                        },
                        "analyst-risk-allocation": {
                            "score": 5,
                            "confidence": 5,
                            "reason_code": "hold_neutral",
                            "one_line_reason": "risk self-test",
                            "missing_data": [],
                        },
                    }
                }
            elif "agent_role: analyst-market-news" in prompt:
                first_payload = {
                    "views": {
                        "analyst-momentum-cycle": {
                            "score": 5,
                            "confidence": 5,
                            "reason_code": "hold_neutral",
                            "one_line_reason": "momentum self-test",
                            "missing_data": [],
                        },
                        "analyst-news-flow": {
                            "score": 5,
                            "confidence": 5,
                            "reason_code": "no_news_neutral",
                            "one_line_reason": "뉴스 정보가 없어 중립 5점",
                            "missing_data": ["news_summary"],
                        },
                    }
                }
            else:
                first_payload = {"score": 5, "confidence": 5, "missing_data": []}
            payload = {
                "agent_id": "fake",
                "persona": "fake",
                "stage": stage,
                "human_markdown_path": "",
                "symbols": [
                    {
                        "symbol_id": "005930",
                        "symbol_name": "삼성전자",
                        "reason_code": "hold_neutral",
                        "one_line_reason": "self-test",
                        **(
                            {"target_holding_quantity": 0, "relative_attractiveness_rank": 1}
                            if stage == "second-verdict"
                            else first_payload
                        ),
                    }
                ],
                "errors": [],
            }
        else:
            payload = {"ok": True, "argv": sys.argv[1:]}
    output_path.write_text(json.dumps(payload), encoding="utf-8")
if "--json" in sys.argv:
    if os.environ.get("FAKE_CODEX_DIAGNOSTIC_EVENTS") == "1":
        print(json.dumps({"type": "event_msg", "payload": {"type": "tool_call", "tool_name": "shell", "command": ["cat", "artifact.json"]}}))
        print(json.dumps({"type": "event_msg", "payload": {"type": "tool_result", "tool_name": "shell", "content": "x" * 2048}}))
        print(json.dumps({"type": "event_msg", "payload": {"type": "tool_call", "tool_name": "shell", "command": ["cat", "artifact.json"]}}))
        print(json.dumps({"type": "event_msg", "payload": {"type": "tool_result", "tool_name": "shell", "content": "y" * 1024}}))
        print(json.dumps({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 7, "cached_input_tokens": 3, "output_tokens": 2, "reasoning_output_tokens": 1, "total_tokens": 9}}}))
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 11, "cached_input_tokens": 5, "output_tokens": 4, "reasoning_output_tokens": 2, "total_tokens": 15}}))
        print("diagnostic stderr", file=sys.stderr)
    else:
        print(json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 120
                    }
                }
            },
            "rate_limits": {
                "primary": {"used_percent": 1.0},
                "secondary": {"used_percent": 2.0}
            }
        }))
sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def spec(tmp: Path, *, stage: str, agent_role: str, task_name: str) -> dict[str, Any]:
    return {
        "run_id": "self-test",
        "started_at": "2026-06-08T09:00:00+09:00",
        "stage": stage,
        "agent_role": agent_role,
        "task_name": task_name,
        "prompt": '{"return":"json only"}',
        "workspace_dir": str(tmp),
        "output_dir": str(tmp / "reports" / "runs" / "self-test"),
    }


def compact_spec(tmp: Path, *, stage: str, agent_role: str, task_name: str) -> dict[str, Any]:
    payload = spec(tmp, stage=stage, agent_role=agent_role, task_name=task_name)
    payload.pop("prompt")
    payload["artifact_paths"] = {
        "decision_brief": str(tmp / "reports" / "runs" / "self-test" / "decision-brief.json"),
        "verdict_first": str(tmp / "reports" / "runs" / "self-test" / "verdict-first.json"),
        "persona": f"prompts/{agent_role}.md",
        "verdict_format": "prompts/verdict-format.md",
    }
    payload["symbol_ids"] = ["005930", {"symbol_id": "000660"}, "005930"]
    return payload


def write_sample_verdict_inputs(tmp: Path) -> None:
    run_dir = tmp / "reports" / "runs" / "self-test"
    write_json(
        run_dir / "execution.json",
        {
            "schema_version": "1",
            "run_id": "self-test",
            "started_at": "2026-06-08T10:00:00+09:00",
            "orders": [
                {
                    "symbol_id": "005930",
                    "direction": "buy",
                    "result": "submitted",
                    "validated_order_quantity": 99,
                }
            ],
        },
    )
    newer_other_run_dir = tmp / "reports" / "runs" / "self-test-newer-other"
    write_json(
        newer_other_run_dir / "execution.json",
        {
            "schema_version": "1",
            "run_id": "self-test-newer-other",
            "started_at": "2026-06-08T09:05:00+09:00",
            "orders": [
                {
                    "symbol_id": "000660",
                    "direction": "buy",
                    "result": "submitted",
                    "validated_order_quantity": 1,
                }
            ],
        },
    )
    previous_run_dir = tmp / "reports" / "runs" / "self-test-prev"
    write_json(
        previous_run_dir / "execution.json",
        {
            "schema_version": "1",
            "run_id": "self-test-prev",
            "started_at": "2026-06-08T09:00:00+09:00",
            "orders": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "direction": "sell",
                    "result": "submitted",
                    "validated_order_quantity": 1,
                    "current_live_holding_quantity": 12,
                    "expected_holding_quantity": 12,
                    "target_holding_quantity": 11,
                    "additional_required_quantity": -1,
                    "order_path": "immediate",
                    "order_api": "order_cash",
                    "reason": "self-test recent sell",
                }
            ],
        },
    )
    older_run_dir = tmp / "reports" / "runs" / "self-test-older"
    write_json(
        older_run_dir / "execution.json",
        {
            "schema_version": "1",
            "run_id": "self-test-older",
            "started_at": "2026-06-08T08:00:00+09:00",
            "orders": [
                {
                    "symbol_id": "005930",
                    "direction": "buy",
                    "result": "submitted",
                    "validated_order_quantity": 1,
                }
            ],
        },
    )
    write_json(
        run_dir / "decision-brief.json",
        {
            "schema_version": "1",
            "brief_type": "decision-brief",
            "market_index_snapshot": {
                "status": "success",
                "indexes": [
                    {
                        "symbol": "NASDAQ",
                        "name": "Nasdaq",
                        "source": "google_finance",
                        "status": "success",
                        "value": 18000.0,
                        "change_percent": 0.3,
                        "observed_at": "2026-06-08T00:00:00+00:00",
                        "market_status": "latest_available",
                    }
                ],
            },
            "errors": [
                {"symbol_id": "005930", "code": "keep_symbol_error"},
                {"symbol_id": "035420", "code": "drop_symbol_error"},
                {"code": "keep_run_error"},
            ],
            "symbols": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "price": {
                        "current_or_last": 70000,
                        "observed_at": "2026-06-08T09:00:00+09:00",
                        "snapshot_mode": "live",
                        "open": 69000,
                        "high": 71000,
                    },
                    "price_chart_signals": [
                        {"signal": "s1", "strength": 1, "timeframe": "D"},
                        {"signal": "s2", "strength": 2, "timeframe": "W"},
                        {"signal": "s3", "strength": 3, "timeframe": "M"},
                        {"signal": "s4", "strength": 4, "timeframe": "Y"},
                    ],
                    "chart_context": {"daily": [{"close": 70000}], "weekly": [{"close": 69000}]},
                    "financial_summary": [{"metric": "roe", "value": "10"}],
                    "account_exposure": {
                        "current_live_holding_quantity": 10,
                        "pending_and_reserved_buy_quantity": 2,
                        "pending_and_reserved_sell_quantity": 1,
                    },
                    "today_trade_price_context": {
                        "has_same_day_trade": True,
                        "last_direction": "buy",
                        "last_fill_price": 70100,
                        "current_or_last_price": 70000,
                        "move_since_last_fill_pct": -0.14,
                    },
                    "today_trade_timeline_context": {
                        "has_same_day_trade": True,
                        "last_direction": "buy",
                        "net_quantity": 3,
                        "fills": [
                            {"filled_at": "2026-06-08T09:31:00+09:00", "direction": "buy", "quantity": 3, "price": 70100}
                        ],
                    },
                    "orderbook_summary": {"bid_depth": 100},
                    "trade_flow_summary": {"tick_count": 3},
                    "investor_flow_summary": {"foreign_net_buy_quantity": 1000},
                    "news_summary": [
                        {"content": "n1", "url": "u1"},
                        {"content": "n2", "url": "u2"},
                        {"content": "n3", "url": "u3"},
                        {"content": "n4", "url": "u4"},
                    ],
                    "warnings": ["w1", "w2", "w3", "w4"],
                    "custom_detail": {"keep": True},
                },
                {"symbol_id": "000660", "symbol_name": "SK하이닉스"},
                {"symbol_id": "035420", "symbol_name": "NAVER", "custom_detail": {"drop": True}},
            ],
        },
    )
    write_json(
        run_dir / "verdict-first.json",
        {
            "schema_version": "1",
            "stage": "verdict-first",
            "symbols": [
                {
                    "symbol_id": "005930",
                    "score": 7,
                    "agent_scores": [
                        {
                            "agent_role": "analyst-risk-allocation",
                            "score": 7,
                            "confidence": 6,
                            "one_line_reason": "full reason should remain",
                        }
                    ],
                    "custom_verdict_detail": {"keep": True},
                },
                {"symbol_id": "000660", "score": 8},
                {"symbol_id": "035420", "score": 5},
            ],
        },
    )


def assert_prompt_compaction() -> None:
    raw = "  keep leading instruction  \n\n\nnext line   \r\n\r\nfinal"
    expected = "  keep leading instruction\n\nnext line\n\nfinal"
    actual = compact_prompt(raw)
    if actual != expected:
        raise AssertionError(f"unexpected compact prompt: {actual!r}")


def assert_compact_verdict_prompt(tmp: Path) -> None:
    prompt = build_prompt(compact_spec(tmp, stage="first-verdict", agent_role="analyst-fundamental-risk", task_name="first"))
    required_parts = [
        "stage: first-verdict",
        "agent_role: analyst-fundamental-risk",
        "You may use read-only local shell commands such as cat and jq only for the explicitly listed files.",
        "Do not call KIS, MCP, web, network, account/order APIs, or external data sources.",
        "Do not write files, create Markdown, emit diffs, or wrap output in code fences.",
        "Read only the listed symbol_ids from artifact files; do not load unrelated symbols, raw cache files, secrets, or unlisted paths.",
        "decision_brief:",
        "persona: prompts/analyst-fundamental-risk.md",
        "verdict_format: prompts/verdict-format.md",
        "symbol_ids: 005930,000660",
        "Return each symbol with a views object keyed by analyst-quality-value, analyst-risk-allocation",
        "Use today_trade_price_context to avoid same-day churn:",
        "Return JSON only",
    ]
    missing = [part for part in required_parts if part not in prompt]
    if missing:
        raise AssertionError(f"compact verdict prompt missing {missing}: {prompt}")

    second_prompt = build_prompt(
        compact_spec(tmp, stage="second-verdict", agent_role="judge-final", task_name="second")
    )
    second_required_parts = [
        "stage: second-verdict",
        "verdict_first:",
        "For second-verdict, use the lossless selected-symbol first-verdict slice from verdict_first.",
        "Interpret final_first_score as the unrounded confidence-adjusted first-verdict score: >= 6 is a buy/increase candidate, <= 4 is a reduce/exit candidate, and 5 is neutral.",
        "When referring to per-analyst scores in agent_scores, use confidence_adjusted_score as the score;",
        "If a symbol's first-verdict score is missing, unavailable, or unusable, treat it as neutral 5 and continue.",
        "First-verdict scores are judgment inputs, not hard buy/sell gates.",
        "Use recent_trade_context as reassessment input, not a default hold/block reason:",
        "Use today_trade_timeline_context as reassessment input, not a same-day churn block:",
        "Treat long_term_thesis_intact as a sell/reduce suppression signal",
    ]
    missing = [part for part in second_required_parts if part not in second_prompt]
    if missing:
        raise AssertionError(f"compact second-verdict prompt missing {missing}: {second_prompt}")


def assert_verdict_input_slices(tmp: Path) -> None:
    write_sample_verdict_inputs(tmp)
    first_payload = compact_spec(tmp, stage="first-verdict", agent_role="analyst-market-news", task_name="slice-first")
    first_slices = write_verdict_input_slices(first_payload)
    first_core = load_json(Path(first_slices["decision_brief"]))
    first_symbol = first_core["symbols"][0]
    if first_core.get("slice_output_roles") != ["analyst-momentum-cycle", "analyst-news-flow"]:
        raise AssertionError(f"first-verdict slice did not record output roles: {first_core}")
    if first_symbol.get("chart_context", {}).get("daily", [{}])[0].get("close") != 70000:
        raise AssertionError(f"market-news slice dropped chart_context: {first_symbol}")
    if len(first_symbol.get("news_summary", [])) != 4:
        raise AssertionError(f"market-news slice dropped news_summary: {first_symbol}")
    if first_core.get("market_index_snapshot", {}).get("indexes", [{}])[0].get("symbol") != "NASDAQ":
        raise AssertionError(f"market-news slice dropped market_index_snapshot: {first_core}")
    if first_symbol.get("today_trade_price_context", {}).get("last_fill_price") != 70100:
        raise AssertionError(f"market-news slice dropped same-day trade price context: {first_symbol}")
    if "today_trade_timeline_context" in first_symbol:
        raise AssertionError(f"first-verdict slice kept full same-day timeline: {first_symbol}")
    if "financial_summary" in first_symbol or "account_exposure" in first_symbol or "custom_detail" in first_symbol:
        raise AssertionError(f"market-news slice kept unrelated fields: {first_symbol}")
    fundamental_payload = compact_spec(tmp, stage="first-verdict", agent_role="analyst-fundamental-risk", task_name="slice-fundamental")
    fundamental_slices = write_verdict_input_slices(fundamental_payload)
    fundamental_core = load_json(Path(fundamental_slices["decision_brief"]))
    if fundamental_core.get("market_index_snapshot", {}).get("indexes", [{}])[0].get("symbol") != "NASDAQ":
        raise AssertionError(f"fundamental-risk slice dropped market_index_snapshot: {fundamental_core}")

    payload = compact_spec(tmp, stage="second-verdict", agent_role="judge-final", task_name="slice-test")
    slices = write_verdict_input_slices(payload)
    expected_keys = {"decision_brief", "verdict_core", "verdict_first"}
    if set(slices) != expected_keys:
        raise AssertionError(f"unexpected slice keys: {slices}")
    for key, slice_path_text in slices.items():
        slice_text = Path(slice_path_text).read_text(encoding="utf-8")
        if "\n  " in slice_text:
            raise AssertionError(f"verdict input slice should be stored as compact JSON: {slice_path_text}")
        slice_payload = load_json(Path(slice_path_text))
        symbols = [item.get("symbol_id") for item in slice_payload.get("symbols", [])]
        if symbols != ["005930", "000660"]:
            raise AssertionError(f"unexpected sliced symbols for {slice_path_text}: {symbols}")
        if key == "verdict_core" and slice_payload.get("slice_type") != "verdict-core":
            raise AssertionError(f"verdict-core slice missing slice_type: {slice_payload}")
        if key == "verdict_core":
            if slice_payload.get("market_index_snapshot", {}).get("indexes", [{}])[0].get("symbol") != "NASDAQ":
                raise AssertionError(f"second-verdict slice dropped market_index_snapshot: {slice_payload}")
            error_codes = [item.get("code") for item in slice_payload.get("errors", []) if isinstance(item, dict)]
            if error_codes != ["keep_symbol_error", "keep_run_error"]:
                raise AssertionError(f"verdict-core did not filter symbol-scoped errors: {slice_payload}")
            first_symbol = slice_payload["symbols"][0]
            if first_symbol.get("price", {}).get("open") != 69000:
                raise AssertionError(f"verdict-core did not preserve nested price fields: {first_symbol}")
            if len(first_symbol.get("price_chart_signals", [])) != 4:
                raise AssertionError(f"verdict-core truncated price_chart_signals: {first_symbol}")
            if len(first_symbol.get("news_summary", [])) != 4:
                raise AssertionError(f"verdict-core truncated news_summary: {first_symbol}")
            if first_symbol.get("warnings") != ["w1", "w2", "w3", "w4"]:
                raise AssertionError(f"verdict-core truncated warnings: {first_symbol}")
            if first_symbol.get("custom_detail") != {"keep": True}:
                raise AssertionError(f"second-verdict verdict-core dropped custom detail: {first_symbol}")
            if first_symbol.get("today_trade_timeline_context", {}).get("fills", [{}])[0].get("price") != 70100:
                raise AssertionError(f"second-verdict verdict-core dropped same-day fill timeline: {first_symbol}")
            holding_context = first_symbol.get("holding_quantity_context", {})
            if holding_context.get("expected_holding_quantity") != 11:
                raise AssertionError(f"verdict-core did not add expected holding context: {first_symbol}")
            if holding_context.get("direction_examples", {}).get("reduce_by_1") != 10:
                raise AssertionError(f"verdict-core did not add target direction examples: {first_symbol}")
            if "recent_trade_context" not in first_symbol:
                raise AssertionError(f"verdict-core did not add recent trade context: {first_symbol}")
            recent_trades = first_symbol.get("recent_trade_context", {}).get("recent_submitted_trades", [])
            if len(recent_trades) != 1 or recent_trades[0].get("direction") != "sell":
                raise AssertionError(f"verdict-core did not preserve recent submitted trade context: {first_symbol}")
            inspected_runs = first_symbol.get("recent_trade_context", {}).get("inspected_run_ids", [])
            if inspected_runs != ["self-test-newer-other", "self-test-prev"]:
                raise AssertionError(f"verdict-core inspected wrong recent runs: {first_symbol}")
        if key == "verdict_first" and slice_payload.get("slice_type") != "verdict-first-slice":
            raise AssertionError(f"second-verdict first slice missing slice_type: {slice_payload}")
        if key == "verdict_first":
            first_symbol = slice_payload["symbols"][0]
            agent_scores = first_symbol.get("agent_scores", [])
            if not agent_scores or agent_scores[0].get("one_line_reason") != "full reason should remain":
                raise AssertionError(f"verdict-first slice dropped agent score reason: {first_symbol}")
            if first_symbol.get("custom_verdict_detail") != {"keep": True}:
                raise AssertionError(f"verdict-first slice dropped custom detail: {first_symbol}")


def assert_invalid_spec(spec_payload: dict[str, Any], expected: str) -> None:
    try:
        validate_spec(spec_payload)
    except ValueError as exc:
        if expected not in str(exc):
            raise AssertionError(f"expected {expected!r}, got {exc}") from exc
        return
    raise AssertionError(f"invalid spec was accepted; expected {expected!r}")


def assert_argv(argv_log: Path, *, model: str, effort: str) -> None:
    lines = [json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise AssertionError("fake codex argv log is empty")
    argv = lines[-1]
    if "-m" not in argv or argv[argv.index("-m") + 1] != model:
        raise AssertionError(f"expected model {model}, argv={shlex.join(argv)}")
    expected_effort = f'model_reasoning_effort="{effort}"'
    if "-c" not in argv or expected_effort not in argv:
        raise AssertionError(f"expected effort {expected_effort}, argv={shlex.join(argv)}")


def assert_event_diagnostics_wrapper(wrapper: dict[str, Any]) -> None:
    event_path = Path(str(wrapper.get("event_log_path") or ""))
    stderr_path = Path(str(wrapper.get("stderr_path") or ""))
    diagnostics = wrapper.get("event_diagnostics")
    if not isinstance(diagnostics, dict):
        raise AssertionError(f"wrapper missing event diagnostics: {wrapper}")
    if not wrapper.get("event_log_retained") or not event_path.exists():
        raise AssertionError(f"event log was not retained: {wrapper}")
    if not wrapper.get("stderr_retained") or not stderr_path.exists():
        raise AssertionError(f"stderr log was not retained: {wrapper}")
    if diagnostics.get("event_type_counts", {}).get("event_msg:tool_call") != 2:
        raise AssertionError(f"tool call event count missing: {diagnostics}")
    if diagnostics.get("event_type_counts", {}).get("event_msg:tool_result") != 2:
        raise AssertionError(f"tool result event count missing: {diagnostics}")
    if diagnostics.get("event_type_counts", {}).get("turn.completed") != 1:
        raise AssertionError(f"turn.completed event count missing: {diagnostics}")
    if diagnostics.get("event_type_counts", {}).get("token_count") != 1:
        raise AssertionError(f"legacy token_count event count missing: {diagnostics}")
    if diagnostics.get("tool_call_count") != 2 or diagnostics.get("tool_result_count") != 2:
        raise AssertionError(f"tool summary count mismatch: {diagnostics}")
    if int(diagnostics.get("max_tool_result_bytes", 0)) < 2048:
        raise AssertionError(f"max tool result bytes missing: {diagnostics}")
    repeated = diagnostics.get("repeated_tool_fingerprints")
    if not isinstance(repeated, list) or not repeated or repeated[0].get("count") != 2:
        raise AssertionError(f"repeated command fingerprint missing: {diagnostics}")
    usage_events = diagnostics.get("usage_events")
    if not isinstance(usage_events, list) or [item.get("kind") for item in usage_events] != ["token_count", "turn.completed"]:
        raise AssertionError(f"usage event sequence missing: {diagnostics}")
    if wrapper.get("token_usage", {}).get("total_tokens") != 24:
        raise AssertionError(f"mixed usage events were not accumulated as current semantics: {wrapper}")


def run_self_test() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        fake = tmp / "codex"
        argv_log = tmp / "argv.jsonl"
        fake_codex_script(fake)
        old_env = os.environ.copy()
        os.environ["CODEX_BIN"] = str(fake)
        os.environ["FAKE_CODEX_ARGV_LOG"] = str(argv_log)
        os.environ["CODEX_BYPASS_APPROVALS_AND_SANDBOX"] = "1"
        try:
            try:
                assert_all_supported_stages_use_expected_models()
                assert_unsupported_stage_rejected()
                assert_prompt_compaction()
                assert_compact_verdict_prompt(tmp)
                assert_verdict_input_slices(tmp)
                missing_brief = compact_spec(
                    tmp, stage="first-verdict", agent_role="analyst-market-news", task_name="missing-brief"
                )
                missing_brief["artifact_paths"].pop("decision_brief")
                assert_invalid_spec(missing_brief, "artifact_paths.decision_brief")
                missing_symbols = compact_spec(
                    tmp, stage="first-verdict", agent_role="analyst-market-news", task_name="missing-symbols"
                )
                missing_symbols["symbol_ids"] = []
                assert_invalid_spec(missing_symbols, "symbol_ids")
                missing_verdict_first = compact_spec(
                    tmp,
                    stage="second-verdict",
                    agent_role="judge-final",
                    task_name="missing-verdict-first",
                )
                missing_verdict_first["artifact_paths"].pop("verdict_first")
                assert_invalid_spec(missing_verdict_first, "artifact_paths.verdict_first")
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="first-verdict",
                        agent_role="analyst-random",
                        task_name="analyst-random",
                    ),
                    "first-verdict agent_role must be one of",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge-longterm",
                        task_name="judge-longterm",
                    ),
                    "agent_role must be judge-final",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge",
                        task_name="judge-longterm-retry1",
                    ),
                    "agent_role must be judge-final",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge-random",
                        task_name="judge-random",
                    ),
                    "agent_role must be judge-final",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge-final",
                        task_name="judge-final-retry3",
                    ),
                    "at most 2 retries",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge-final",
                        task_name="judge-final-attempt3",
                    ),
                    "at most 2 retries",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge-final",
                        task_name="judge-final-retry-3",
                    ),
                    "at most 2 retries",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge-final",
                        task_name="judge-final-attempt-3",
                    ),
                    "at most 2 retries",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge-final",
                        task_name="judge-final-retry1-attempt3",
                    ),
                    "at most 2 retries",
                )
                compact_errors = compact_verdict_payload_errors(
                    {
                        "stage": "first-verdict",
                        "symbols": [
                            {
                                "symbol_id": "005930",
                                "score": 5,
                                "evidence": ["too long for compact verdict"],
                            }
                        ],
                    },
                    "first-verdict",
                )
                if not compact_errors or compact_errors[0].get("code") != "disallowed_compact_verdict_key":
                    raise AssertionError(f"compact verdict disallowed keys were not rejected: {compact_errors}")
                invalid_second_errors = compact_verdict_payload_errors(
                    {"stage": "second-verdict", "portfolio": {}, "symbols": [{}]},
                    "second-verdict",
                )
                if not any(error.get("code") == "invalid_compact_verdict_schema" for error in invalid_second_errors):
                    raise AssertionError(f"invalid compact second-verdict schema was accepted: {invalid_second_errors}")
                alias_payload = normalize_compact_verdict_payload(
                    {
                        "stage": "first-verdict",
                        "verdicts": [
                            {
                                "symbol_id": "005930",
                                "symbol_name": "삼성전자",
                                "score": 6,
                                "confidence": 5,
                                "reason_code": "hold_neutral",
                                "one_line_reason": "alias output",
                            }
                        ],
                    },
                    "first-verdict",
                )
                alias_errors = compact_verdict_payload_errors(alias_payload, "first-verdict")
                if alias_errors:
                    raise AssertionError(f"compact verdict alias normalization failed: {alias_errors}")
                combined_old_shape_errors = compact_verdict_payload_errors(alias_payload, "first-verdict", "analyst-fundamental-risk")
                if not any("must include views" in error.get("message", "") for error in combined_old_shape_errors):
                    raise AssertionError(f"combined first-verdict old shape was accepted: {combined_old_shape_errors}")
                raw_with_artifacts = compact_spec(
                    tmp, stage="first-verdict", agent_role="analyst-market-news", task_name="raw-with-artifacts"
                )
                raw_with_artifacts["prompt"] = '{"return":"json only"}'
                assert_invalid_spec(raw_with_artifacts, "raw prompt fallback is forbidden")
            except AssertionError as exc:
                failures.append(str(exc))

            model_config = load_subagent_model_config()
            cases = [
                (
                    spec(tmp, stage="financial-collection", agent_role="financial", task_name="financial"),
                    model_config["collection"]["model"],
                    model_config["collection"]["model_reasoning_effort"],
                ),
                (
                    compact_spec(tmp, stage="first-verdict", agent_role="analyst-fundamental-risk", task_name="first"),
                    model_config["first_verdict"]["model"],
                    model_config["first_verdict"]["model_reasoning_effort"],
                ),
                (
                    compact_spec(tmp, stage="second-verdict", agent_role="judge-final", task_name="second"),
                    model_config["second_verdict"]["model"],
                    model_config["second_verdict"]["model_reasoning_effort"],
                ),
            ]
            for test_spec, model, effort in cases:
                wrapper = run_one(test_spec)
                if wrapper["status"] != "success":
                    failures.append(f"{test_spec['task_name']} returned {wrapper['status']}")
                if wrapper.get("token_usage", {}).get("total_tokens") != 120:
                    failures.append(f"{test_spec['task_name']} missing token usage: {wrapper}")
                try:
                    assert_argv(argv_log, model=model, effort=effort)
                except AssertionError as exc:
                    failures.append(str(exc))

            old_event_retention = os.environ.get("CODEX_SUBAGENT_EVENT_RETENTION")
            os.environ["CODEX_SUBAGENT_EVENT_RETENTION"] = "always"
            os.environ["FAKE_CODEX_DIAGNOSTIC_EVENTS"] = "1"
            try:
                diagnostic_wrapper = run_one(
                    compact_spec(
                        tmp,
                        stage="first-verdict",
                        agent_role="analyst-market-news",
                        task_name="event-diagnostics",
                    )
                )
                try:
                    assert_event_diagnostics_wrapper(diagnostic_wrapper)
                except AssertionError as exc:
                    failures.append(str(exc))
            finally:
                os.environ.pop("FAKE_CODEX_DIAGNOSTIC_EVENTS", None)
                if old_event_retention is None:
                    os.environ.pop("CODEX_SUBAGENT_EVENT_RETENTION", None)
                else:
                    os.environ["CODEX_SUBAGENT_EVENT_RETENTION"] = old_event_retention

            saved_event_retention = os.environ.pop("CODEX_SUBAGENT_EVENT_RETENTION", None)
            try:
                default_retention_wrapper = run_one(
                    compact_spec(
                        tmp,
                        stage="first-verdict",
                        agent_role="analyst-market-news",
                        task_name="event-default-retention",
                    )
                )
            finally:
                if saved_event_retention is not None:
                    os.environ["CODEX_SUBAGENT_EVENT_RETENTION"] = saved_event_retention
            if default_retention_wrapper.get("event_retention") != "anomaly":
                failures.append(f"default event retention mode was not anomaly: {default_retention_wrapper}")
            if default_retention_wrapper.get("event_retention_reason") != "no_anomaly":
                failures.append(f"default anomaly event retention reason was not no_anomaly: {default_retention_wrapper}")
            if default_retention_wrapper.get("event_log_retained") is not False:
                failures.append(f"default anomaly event retention did not prune normal event log: {default_retention_wrapper}")
            if default_retention_wrapper.get("stderr_retained") is not False:
                failures.append(f"default anomaly event retention did not mark stderr pruned: {default_retention_wrapper}")
            if Path(default_retention_wrapper["event_log_path"]).exists():
                failures.append("default anomaly event retention left normal event log on disk")
            if Path(default_retention_wrapper["stderr_path"]).exists():
                failures.append("default anomaly event retention left normal stderr log on disk")

            custom_model_config = tmp / "daily-trading-subagents.yaml"
            custom_model_config.write_text(
                "\n".join(
                    [
                        "collection:",
                        "  model: gpt-5.4-mini",
                        "  model_reasoning_effort: low",
                        "first_verdict:",
                        "  model: gpt-5.4",
                        "  model_reasoning_effort: high",
                        "second_verdict:",
                        "  model: gpt-5.5",
                        "  model_reasoning_effort: medium",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            os.environ[SUBAGENT_MODEL_CONFIG_ENV] = str(custom_model_config)
            custom_wrapper = run_one(
                compact_spec(tmp, stage="first-verdict", agent_role="analyst-market-news", task_name="first-custom-model")
            )
            if custom_wrapper["status"] != "success":
                failures.append(f"custom model config returned {custom_wrapper['status']}")
            try:
                assert_argv(argv_log, model="gpt-5.4", effort="high")
            except AssertionError as exc:
                failures.append(str(exc))
            os.environ.pop(SUBAGENT_MODEL_CONFIG_ENV, None)

            write_sample_verdict_inputs(tmp)
            compact_wrapper = run_one(
                compact_spec(tmp, stage="first-verdict", agent_role="analyst-market-news", task_name="compact-first")
            )
            if compact_wrapper["status"] != "success" or compact_wrapper.get("prompt_mode") != "compact_verdict":
                failures.append(f"compact verdict spec returned unexpected wrapper: {compact_wrapper}")
            if not compact_wrapper.get("verdict_input_paths", {}).get("decision_brief"):
                failures.append(f"compact verdict spec did not create decision brief slice: {compact_wrapper}")

            write_sample_verdict_inputs(tmp)
            reuse_spec = compact_spec(
                tmp, stage="first-verdict", agent_role="analyst-market-news", task_name="reuse-first"
            )
            first_reuse_wrapper = run_one(reuse_spec)
            argv_before = len(argv_log.read_text(encoding="utf-8").splitlines())
            second_reuse_wrapper = run_one(reuse_spec)
            argv_after = len(argv_log.read_text(encoding="utf-8").splitlines())
            if first_reuse_wrapper.get("status") != "success":
                failures.append(f"reuse setup wrapper failed: {first_reuse_wrapper}")
            if not second_reuse_wrapper.get("reused_existing_wrapper") or argv_after != argv_before:
                failures.append(f"successful wrapper was not reused: {second_reuse_wrapper}")
            group_reuse_before = len(argv_log.read_text(encoding="utf-8").splitlines())
            group_reuse = run_group([reuse_spec], max_workers=1)
            group_reuse_after = len(argv_log.read_text(encoding="utf-8").splitlines())
            if (
                group_reuse.get("status") != "success"
                or group_reuse_after != group_reuse_before
                or not group_reuse.get("wrappers", [{}])[0].get("reused_existing_wrapper")
            ):
                failures.append(f"run-group did not pre-reuse successful wrapper: {group_reuse}")
            changed_brief_path = tmp / "reports" / "runs" / "self-test" / "decision-brief.json"
            changed_brief = load_json(changed_brief_path)
            changed_brief["symbols"][0]["custom_detail"] = {"keep": "changed"}
            write_json(changed_brief_path, changed_brief)
            changed_reuse_before = len(argv_log.read_text(encoding="utf-8").splitlines())
            changed_wrapper = run_one(reuse_spec)
            changed_reuse_after = len(argv_log.read_text(encoding="utf-8").splitlines())
            if changed_wrapper.get("reused_existing_wrapper") or changed_reuse_after != changed_reuse_before + 1:
                failures.append(f"changed artifact content incorrectly reused wrapper: {changed_wrapper}")

            old_raw_retention = os.environ.get("CODEX_SUBAGENT_RAW_RETENTION")
            os.environ["CODEX_SUBAGENT_RAW_RETENTION"] = "failed"
            retained_wrapper = run_one(
                compact_spec(tmp, stage="first-verdict", agent_role="analyst-market-news", task_name="raw-retention")
            )
            if retained_wrapper.get("raw_output_retained") is not False:
                failures.append(f"successful raw output was not pruned with failed retention: {retained_wrapper}")
            if Path(retained_wrapper["raw_output_path"]).exists():
                failures.append("raw output path still exists after successful failed-retention run")
            if old_raw_retention is None:
                os.environ.pop("CODEX_SUBAGENT_RAW_RETENTION", None)
            else:
                os.environ["CODEX_SUBAGENT_RAW_RETENTION"] = old_raw_retention

            text_spec = spec(tmp, stage="news-collection", agent_role="news", task_name="text-news")
            os.environ["FAKE_CODEX_INVALID_JSON"] = "1"
            wrapper = run_one(text_spec)
            if wrapper["status"] != "success" or wrapper["parsed_json"] is not None or wrapper.get("parsed_text") != "not json":
                failures.append("text collection output was not accepted without JSON parsing")
            os.environ.pop("FAKE_CODEX_INVALID_JSON", None)

            group = run_group(
                [
                    spec(tmp, stage="financial-collection", agent_role="financial", task_name="g-financial"),
                    spec(tmp, stage="news-collection", agent_role="news", task_name="g-news"),
                ],
                max_workers=3,
            )
            if group["status"] != "success" or group["count"] != 2:
                failures.append(f"run-group returned unexpected result: {group}")
            wrapper_count = len(list((Path(group["wrappers"][0]["raw_output_path"]).parent).glob("g-*.wrapper.json")))
            if wrapper_count != 2:
                failures.append(f"expected 2 group wrapper files, got {wrapper_count}")

            os.environ["FAKE_CODEX_EMPTY_TASKS"] = "optional-news"
            optional_group = run_group(
                [
                    spec(tmp, stage="news-collection", agent_role="news", task_name="optional-news"),
                ],
                max_workers=2,
            )
            if (
                optional_group["status"] != "partial"
                or optional_group["failed_count"] != 1
                or optional_group["required_failed_count"] != 0
                or optional_group["optional_failed_count"] != 1
            ):
                failures.append(f"optional text failure did not produce partial group: {optional_group}")

            os.environ["FAKE_CODEX_EMPTY_TASKS"] = "required-first"
            required_group = run_group(
                [
                    compact_spec(tmp, stage="first-verdict", agent_role="analyst-fundamental-risk", task_name="required-first"),
                    spec(tmp, stage="news-collection", agent_role="news", task_name="required-news"),
                ],
                max_workers=2,
            )
            if (
                required_group["status"] != "failed"
                or required_group["failed_count"] != 1
                or required_group["required_failed_count"] != 1
                or required_group["optional_failed_count"] != 0
            ):
                failures.append(f"required failure did not produce failed group: {required_group}")
            os.environ.pop("FAKE_CODEX_EMPTY_TASKS", None)
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    status = "failed" if failures else "passed"
    print(json.dumps({"status": status, "failures": failures}, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
