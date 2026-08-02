#!/usr/bin/env python3
"""Run daily-trading sub-agent stages through codex exec."""

from __future__ import annotations

import argparse
import json
import math
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
from zoneinfo import ZoneInfo
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
RUNTIME_CONFIG_ENV = "CODEX_RUNTIME_CONFIG_FILE"
RUNTIME_CONFIG_FILENAME = "codex-runtime.yaml"
MODEL_USAGE_FILENAME = "model-usage.jsonl"
DEFAULT_RUNTIME_CONFIG_PATH = Path("/app/config") / RUNTIME_CONFIG_FILENAME
BAKED_RUNTIME_CONFIG_PATH = Path("/app/default-config") / RUNTIME_CONFIG_FILENAME
DEFAULT_SUBAGENT_MODEL_CONFIG = {
    "collection": {"model": "gpt-5.6-luna", "model_reasoning_effort": "low"},
    "analyst_review": {"model": "gpt-5.6-sol", "model_reasoning_effort": "xhigh"},
    "judge_review": {"model": "gpt-5.6-sol", "model_reasoning_effort": "xhigh"},
}
SUBAGENT_MODEL_CONFIG_KEYS = ("collection", "analyst_review", "judge_review")
COLLECTION_STAGES = {"financial-collection"}
FINANCIAL_PATH_OUTPUT_STAGES = {"financial-collection"}
TEXT_OUTPUT_STAGES = FINANCIAL_PATH_OUTPUT_STAGES
OPTIONAL_GROUP_FAILURE_STAGES = TEXT_OUTPUT_STAGES
REVIEW_STAGES = {"analyst-review", "judge-review"}
AUDIT_LOG_STAGES = {"judge-review"}
SELECTED_ANALYST_REVIEW_ROLES = {
    "analyst-quality-risk",
    "analyst-momentum-news",
}
MARKET_INDEX_SNAPSHOT_AGENT_ROLES = {"analyst-quality-risk", "analyst-momentum-news", "judge"}
MARKET_NEWS_CONTEXT_AGENT_ROLES = {"analyst-momentum-news", "judge"}
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
        "symbol_news_summary",
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
MCP_SERVER_NAME_PATTERNS = (
    re.compile(r"\bmcp server\s+[`'\"]?(?P<name>[A-Za-z0-9_.-]+)", re.IGNORECASE),
    re.compile(r"\bmcp[_-]?server(?:_name)?\s*[:=]\s*[`'\"]?(?P<name>[A-Za-z0-9_.-]+)", re.IGNORECASE),
)
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


def runtime_config_candidates() -> list[Path]:
    configured = os.getenv(RUNTIME_CONFIG_ENV, "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path == DEFAULT_RUNTIME_CONFIG_PATH:
            return [configured_path, BAKED_RUNTIME_CONFIG_PATH]
        return [configured_path]
    repo_root = repo_root_from(script_dir())
    return [
        DEFAULT_RUNTIME_CONFIG_PATH,
        BAKED_RUNTIME_CONFIG_PATH,
        repo_root / "containers/codex-exec/profiles/base/config" / RUNTIME_CONFIG_FILENAME,
        Path("containers/codex-exec/profiles/base/config") / RUNTIME_CONFIG_FILENAME,
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
    candidates = runtime_config_candidates()
    explicit = bool(os.getenv(RUNTIME_CONFIG_ENV, "").strip())
    for path in candidates:
        if path.exists():
            payload = load_config_payload(path)
            if not isinstance(payload, dict):
                raise ValueError(f"codex runtime config must be an object: {path}")
            extra_keys = sorted(str(key) for key in payload if str(key) not in {"defaults", "daily_trading"})
            if extra_keys:
                raise ValueError(f"unsupported codex runtime config keys: {', '.join(extra_keys)}")
            if not isinstance(payload.get("daily_trading"), dict):
                raise ValueError(f"codex runtime config daily_trading must be an object: {path}")
            return normalize_model_config(payload["daily_trading"], path)
    if explicit:
        raise FileNotFoundError(f"{RUNTIME_CONFIG_ENV} does not exist: {candidates[0]}")
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


def mcp_degraded_dependencies(stderr: str) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None, str]] = set()
    for raw_line in stderr.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if not line or "mcp" not in lowered:
            continue
        initialization_failure = (
            "initialized notification" in lowered
            or "initialization" in lowered
            or "initialize" in lowered
        ) and any(word in lowered for word in ("error", "failed", "fatal", "unexpected server response"))
        if not initialization_failure:
            continue
        status_match = re.search(r"\bHTTP\s+([45][0-9]{2})\b", line, re.IGNORECASE)
        http_status = int(status_match.group(1)) if status_match else None
        server_name = ""
        for pattern in MCP_SERVER_NAME_PATTERNS:
            match = pattern.search(line)
            if match:
                server_name = match.group("name")
                break
        dependency_id = f"mcp:{server_name}" if server_name else "mcp:unknown"
        component = "rmcp::transport::worker" if "rmcp::transport::worker" in lowered else "codex-mcp"
        key = (dependency_id, http_status, component)
        if key in seen:
            continue
        seen.add(key)
        dependencies.append(
            {
                "dependency_type": "mcp",
                "dependency_id": dependency_id,
                "server_name": server_name or "unknown",
                "server_identifier_source": "stderr" if server_name else "unavailable",
                "component": component,
                "phase": "initialize",
                "status": "degraded",
                "error_code": "mcp_initialization_http_error" if http_status else "mcp_initialization_error",
                "http_status": http_status,
            }
        )
    return dependencies


def parse_codex_json_events(stdout: str) -> dict[str, Any]:
    usage = zero_token_usage()
    event_count = 0
    last_rate_limits: Any | None = None
    last_message = ""
    thread_id = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("type") == "thread.started":
            candidate_thread_id = str(item.get("thread_id") or "").strip()
            if candidate_thread_id:
                thread_id = candidate_thread_id
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
        "thread_id": thread_id,
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
    if agent_role and safe_name(agent_role).lower() not in MARKET_NEWS_CONTEXT_AGENT_ROLES:
        core.pop("market_news_context", None)
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
        if isinstance(raw, float) and not math.isfinite(raw):
            return None
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
        if not math.isfinite(value):
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


class RunArtifactJsonCache:
    """Reuses parsed JSON for unchanged files within one enrichment call.

    Keyed by the exact Path object passed to read() (not resolved), entries
    store the file identity (device, inode, mtime_ns, ctime_ns, size)
    alongside the parsed payload. The observation boundary is an opened file
    descriptor, matching how read_json_if_exists/load_json actually read a
    file: identity is derived via os.fstat() on that descriptor, both before
    and after parsing. If an atomic temp-file replace lands before this open,
    the descriptor legitimately observes the new file, exactly like an
    uncached read would; if it lands after this open, the descriptor still
    refers to the old inode's content, so a cache hit or fresh parse both
    correctly reflect the pre-replace snapshot instead of racing ahead of it.
    A before/after identity mismatch on the same descriptor (in-place
    mutation while a parse is in flight) means the read cannot be trusted as
    one consistent snapshot, so that result is returned but never cached,
    forcing the next call to revisit disk. A replace always installs a new
    inode at the target path even when size and mtime coincidentally match
    the prior file, so identity comparison still detects it. Missing or
    unparseable files are never cached either. Instances must not be shared
    across specs, worker threads, retries, or pipeline runs.
    """

    def __init__(self) -> None:
        self._entries: dict[Path, tuple[tuple[int, int, int, int, int], Any]] = {}

    @staticmethod
    def _identity(stat_result: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
            stat_result.st_size,
        )

    def read(self, path: Path) -> Any | None:
        if not path.exists():
            return None
        handle = open(path, "r", encoding="utf-8")
        try:
            before_identity = self._identity(os.fstat(handle.fileno()))
            cached = self._entries.get(path)
            if cached is not None and cached[0] == before_identity:
                return cached[1]
            payload = json.load(handle)
            after_identity = self._identity(os.fstat(handle.fileno()))
            if after_identity == before_identity:
                self._entries[path] = (after_identity, payload)
            else:
                self._entries.pop(path, None)
            return payload
        finally:
            handle.close()


def read_json_cached(path: Path, cache: RunArtifactJsonCache | None) -> Any | None:
    if cache is None:
        return read_json_if_exists(path)
    return cache.read(path)


DAILY_TRADING_TIMEZONE = ZoneInfo("Asia/Seoul")


def parse_iso_datetime(value: Any) -> datetime | None:
    """Parse a daily-trading timestamp, matching run_daily_trading_pipeline.parse_kst_datetime.

    A timezone-naive timestamp is treated as KST (not UTC) so both selection
    paths agree on legacy artifacts that omit an offset.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DAILY_TRADING_TIMEZONE)
    return parsed


THESIS_CONDITION_ID_MAX_LENGTH = 64


def normalize_thesis_condition_id(value: Any) -> str:
    """Normalize a thesis invalidation condition_id.

    Mirrors run_daily_trading_pipeline.normalize_thesis_condition_id exactly:
    non-string input, or input with nothing left after normalization, returns
    "" (never a placeholder like "unknown"). Runs of characters other than
    Unicode alphanumerics, '.', '_', '-' (including whitespace) collapse to a
    single '-'; the result is lowercased, trimmed of leading/trailing
    separators, and capped at THESIS_CONDITION_ID_MAX_LENGTH characters.
    Korean/non-ASCII identifiers are preserved, while separator-only values
    remain unusable.
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

    Mirrors the pipeline's persistence rule so the judge and pipeline select the
    same usable prior thesis context. core_rationale,
    condition_id, and description must be actual strings, not other JSON types
    coerced with str().
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


def prior_decision_context(
    output_dir: Path,
    symbol_id: str,
    current_started_at: str = "",
    today_trade_context: dict[str, Any] | None = None,
    cache: RunArtifactJsonCache | None = None,
) -> dict[str, Any]:
    unavailable = {"status": "no_prior_decision", "realized_pnl": {"status": "unavailable"}}
    if not symbol_id:
        return unavailable
    runs_dir = output_dir.parent
    if not runs_dir.is_dir():
        return unavailable
    current_started = parse_iso_datetime(current_started_at)
    if current_started is None:
        return unavailable
    current = output_dir.resolve()
    best: tuple[datetime, str, Path, dict[str, Any]] | None = None
    best_thesis: tuple[datetime, str, dict[str, Any]] | None = None
    for run_dir in (path for path in runs_dir.iterdir() if path.is_dir()):
        if run_dir.resolve() == current:
            continue
        try:
            payload = read_json_cached(run_dir / "judge-review.json", cache)
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("status") != "success":
            continue
        candidate_started = parse_iso_datetime(payload.get("started_at"))
        if candidate_started is None or not candidate_started < current_started:
            continue
        symbols = payload.get("symbols")
        if not isinstance(symbols, list):
            continue
        for item in symbols:
            if not isinstance(item, dict) or symbol_key(item) != symbol_id:
                continue
            source_run_id = str(payload.get("run_id") or run_dir.name)
            candidate_key = (candidate_started, source_run_id)
            if best is None or candidate_key > (best[0], best[1]):
                best = (candidate_started, source_run_id, run_dir, item)
            thesis = item.get("thesis_definition")
            if thesis_definition_is_valid(thesis) and (
                best_thesis is None or candidate_key > (best_thesis[0], best_thesis[1])
            ):
                best_thesis = (candidate_started, source_run_id, thesis)
    if best is None:
        return unavailable
    decided_at, source_run_id, source_run_dir, decision = best
    lifecycle = read_json_cached(output_dir / "order-lifecycle.json", cache)
    lifecycle_orders = lifecycle.get("previous_submitted_cash_orders", []) if isinstance(lifecycle, dict) else []
    lifecycle_by_order_id = {
        str(item.get("order_id") or "").strip(): item
        for item in lifecycle_orders
        if isinstance(item, dict) and str(item.get("order_id") or "").strip()
    }

    outcomes: list[dict[str, Any]] = []
    seen_order_ids: set[str] = set()

    def append_outcome(order: dict[str, Any], started_at: str) -> None:
        order_id = str(order.get("order_or_reservation_id") or order.get("order_id") or "").strip()
        result_text = str(order.get("result") or ("submitted" if order_id else ""))
        lifecycle_order = lifecycle_by_order_id.get(order_id, {})
        broker = (
            lifecycle_order.get("broker_reconciliation")
            if isinstance(lifecycle_order.get("broker_reconciliation"), dict)
            else order.get("broker_reconciliation")
            if isinstance(order.get("broker_reconciliation"), dict)
            else {}
        )
        outcomes.append(
            {
                "started_at": started_at,
                "direction": str(order.get("direction") or ""),
                "result": result_text,
                "reason": str(order.get("reason") or "")[:120],
                "submitted_quantity": int_or_zero(
                    order.get("validated_order_quantity") or order.get("requested_quantity")
                )
                if order_id or result_text.lower().startswith("submitted")
                else 0,
                "order_id": order_id,
                "broker_status": str(broker.get("status") or ("unconfirmed" if order_id else "not_submitted")),
                "broker_filled_quantity": int_or_zero(broker.get("filled_quantity")),
                "broker_rejected_quantity": int_or_zero(broker.get("rejected_quantity")),
                "broker_canceled_quantity": int_or_zero(broker.get("canceled_quantity")),
                "broker_remaining_quantity": int_or_zero(broker.get("remaining_quantity")),
            }
        )
        if order_id:
            seen_order_ids.add(order_id)

    try:
        execution = read_json_cached(source_run_dir / "execution.json", cache)
    except (OSError, ValueError):
        execution = None
    if isinstance(execution, dict) and isinstance(execution.get("orders"), list):
        for order in execution["orders"]:
            if isinstance(order, dict) and symbol_key(order) == symbol_id:
                append_outcome(order, str(execution.get("started_at") or ""))
    for order in lifecycle_orders:
        if not isinstance(order, dict) or symbol_key(order) != symbol_id:
            continue
        order_id = str(order.get("order_id") or "").strip()
        order_started_at = str(order.get("started_at") or "")
        order_started = parse_iso_datetime(order_started_at)
        if order_id in seen_order_ids or order_started is None or order_started < decided_at:
            continue
        append_outcome(order, order_started_at)

    trade_context = today_trade_context if isinstance(today_trade_context, dict) else {}
    source_fills = trade_context.get("fills", [])
    subsequent_fills = []
    for fill in source_fills:
        if not isinstance(fill, dict):
            continue
        filled_at = parse_iso_datetime(fill.get("filled_at"))
        if filled_at is not None and filled_at >= decided_at:
            subsequent_fills.append(fill)
    coverage_status = str(trade_context.get("collection_status") or "unavailable")
    if int_or_zero(trade_context.get("fill_count")) > len(source_fills):
        coverage_status = "partial"
    fill_summary = {
        "coverage_status": coverage_status,
        "fill_count": len(subsequent_fills),
        "buy_quantity": sum(int_or_zero(item.get("quantity")) for item in subsequent_fills if item.get("direction") == "buy"),
        "sell_quantity": sum(int_or_zero(item.get("quantity")) for item in subsequent_fills if item.get("direction") == "sell"),
    }
    result = {
        "status": "available",
        "source_run_id": source_run_id,
        "decided_at": decided_at.isoformat(),
        "target_position_value_krw": non_negative_number_value(decision.get("target_position_value_krw")),
        "final_holding_quantity": non_negative_int_value(decision.get("final_holding_quantity")),
        "reason_code": str(decision.get("reason_code") or ""),
        "one_line_reason": str(decision.get("one_line_reason") or "")[:240],
        "order_outcomes": outcomes,
        "subsequent_fill_summary": fill_summary,
        "realized_pnl": {"status": "unavailable"},
    }
    if best_thesis is not None:
        result["thesis_source_run_id"] = best_thesis[1]
        result["thesis_definition"] = best_thesis[2]
    return result


def add_judge_review_holding_context(payload: Any, output_dir: Path | None = None, current_started_at: str = "") -> Any:
    if not isinstance(payload, dict):
        return payload
    context_output_dir = output_dir or Path("")
    cache = RunArtifactJsonCache()
    symbols = payload.get("symbols")
    if isinstance(symbols, list):
        enriched: list[Any] = []
        for item in symbols:
            if isinstance(item, dict):
                copied = dict(item)
                copied["holding_quantity_context"] = build_holding_quantity_context(copied)
                copied["prior_decision_context"] = prior_decision_context(
                    context_output_dir,
                    symbol_key(copied),
                    current_started_at,
                    copied.get("today_trade_timeline_context"),
                    cache,
                )
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
                prior_decision_context=prior_decision_context(
                    context_output_dir,
                    symbol_id,
                    current_started_at,
                    item.get("today_trade_timeline_context"),
                    cache,
                ),
            )
            if isinstance(item, dict)
            else item
            for symbol_id, item in symbols.items()
        }
        return copied_payload
    return payload


POSITION_COST_CONTEXT_ADVISORY = (
    "Use position_cost_context as reference information for profit/loss, risk, and position adjustments. "
    "Determine final direction and target exposure by considering thesis, market evidence, and portfolio risk "
    "together."
)


def positive_number_value(raw: Any) -> int | float | None:
    value = non_negative_number_value(raw)
    return value if value is not None and value > 0 else None


def compact_position_cost_context(
    account_item: dict[str, Any] | None,
    current_review_price: Any,
    price_observed_at: str,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "status": "unavailable",
        "held": None,
        "average_purchase_price": None,
        "purchase_amount": None,
        "current_quantity": None,
        "current_review_price": None,
        "current_review_price_observed_at": "",
        "pct_distance_from_average_price": None,
        "source": "",
        "observed_at": "",
        "advisory_semantics": POSITION_COST_CONTEXT_ADVISORY,
    }
    if not isinstance(account_item, dict) or "current_live_holding_quantity" not in account_item:
        return context
    quantity = non_negative_int_value(account_item.get("current_live_holding_quantity"))
    if quantity is None:
        return context
    if quantity == 0:
        context["status"] = "not_held"
        context["held"] = False
        context["current_quantity"] = 0
        return context
    context["held"] = True
    context["current_quantity"] = quantity
    context["purchase_amount"] = positive_number_value(account_item.get("purchase_amount"))
    context["observed_at"] = str(account_item.get("observed_at") or "")
    average_price = positive_number_value(account_item.get("average_purchase_price"))
    if average_price is not None:
        context["average_purchase_price"] = average_price
        context["source"] = "direct_kis.inquire_balance.pchs_avg_pric"
        context["status"] = "held_available"
    else:
        context["status"] = "held_average_price_unavailable"
    review_price = positive_number_value(current_review_price)
    if review_price is not None:
        context["current_review_price"] = review_price
        context["current_review_price_observed_at"] = str(price_observed_at or "")
    if average_price is not None and review_price is not None:
        try:
            pct_distance = ((review_price - average_price) / average_price) * 100
        except (OverflowError, ZeroDivisionError, ArithmeticError):
            pct_distance = None
        if pct_distance is not None and math.isfinite(pct_distance):
            context["pct_distance_from_average_price"] = round(pct_distance, 2)
    return context


ACCOUNT_ARTIFACT_USABLE_STATUSES = {"success", "partial"}


def account_artifact_holdings_usable(account: Any) -> bool:
    if not isinstance(account, dict):
        return False
    if account.get("skipped"):
        return False
    return str(account.get("status") or "") in ACCOUNT_ARTIFACT_USABLE_STATUSES


def account_holdings_by_symbol(output_dir: Path) -> dict[str, dict[str, Any]]:
    account = read_json_if_exists(output_dir / "account-before-order.json")
    holdings: dict[str, dict[str, Any]] = {}
    if not account_artifact_holdings_usable(account):
        return holdings
    symbols = account.get("symbols")
    if isinstance(symbols, list):
        for item in symbols:
            key = symbol_key(item)
            if key and isinstance(item, dict):
                holdings[key] = item
    return holdings


def add_judge_review_position_cost_context(payload: Any, output_dir: Path | None = None) -> Any:
    if not isinstance(payload, dict):
        return payload
    holdings = account_holdings_by_symbol(output_dir or Path(""))

    def context_for(symbol_id: str, item: dict[str, Any]) -> dict[str, Any]:
        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        return compact_position_cost_context(
            holdings.get(symbol_id),
            price.get("current_or_last"),
            str(price.get("observed_at") or ""),
        )

    symbols = payload.get("symbols")
    if isinstance(symbols, list):
        copied_payload = dict(payload)
        copied_payload["symbols"] = [
            dict(item, position_cost_context=context_for(symbol_key(item), item))
            if isinstance(item, dict)
            else item
            for item in symbols
        ]
        return copied_payload
    if isinstance(symbols, dict):
        copied_payload = dict(payload)
        copied_payload["symbols"] = {
            symbol_id: dict(item, position_cost_context=context_for(symbol_id, item))
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
    stage = str(spec.get("stage", "")).strip()
    if not is_compact_review_spec(spec):
        return {}
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
                sliced = add_judge_review_holding_context(sliced, output_dir, str(spec.get("started_at") or ""))
                sliced = add_judge_review_position_cost_context(sliced, output_dir)
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
        review_scope_reasons = spec.get("review_scope_reasons") if isinstance(spec.get("review_scope_reasons"), dict) else {}
        portfolio_snapshot = spec.get("portfolio_snapshot") if isinstance(spec.get("portfolio_snapshot"), list) else []
        lines.extend(
            [
                "",
                "For judge-review, use the selected-symbol analyst-review slice from analyst_review; agent_scores excluded from aggregation are intentionally omitted from this judgment input.",
                "Optional evidence marked missing, failed, empty, unavailable, or excluded_from_aggregation is non-directional: its absence must not affect opposing_view cases, reason_code, one_line_reason, or target_position_value_krw.",
                "Do not infer safety, risk, favorable news, thesis integrity, or thesis damage from the absence of optional evidence.",
                "Do not use optional-domain coverage counts or completeness to decide evidence sufficiency; judge only the directional strength and conflict of supplied usable evidence.",
                "The supplied symbols are every eligible held symbol (review_scope_reasons=held_position, regardless of score or missing score), every eligible symbol with an active order (review_scope_reasons=active_order), plus the top-ranked remaining unheld symbols by score (review_scope_reasons=unheld_score_rank). There is no score band and no assigned buy/sell candidate direction: you may propose an increase or a decrease for any supplied symbol.",
                "For every supplied symbol, first build a compact opposing_view: the single strongest exposure-increase/maintain case (increase_case) and the single strongest exposure-reduce/avoid case (reduce_case), each with a short summary and its own evidence_refs drawn only from supplied usable evidence. Then resolve the comparison yourself into one target_position_value_krw. Return opposing_view: {increase_case: {summary, evidence_refs}, reduce_case: {summary, evidence_refs}}. Keep both cases short and auditable; do not return long prose, a transcript, or hidden chain-of-thought.",
                "Conflict alone is not a hold rule. Compare the cases by materiality, freshness, source quality, and portfolio impact, then set the target change magnitude in proportion to the supported net advantage. Hold only when neither case has enough supported net advantage to justify a change.",
                "Return no separate action. Return target_position_value_krw, reason_code, and one_line_reason. decision_basis (none|thesis|profit_protection|concentration_rebalance) is optional audit metadata.",
                "final_first_score is the simple mean of the included analyst view scores; per-analyst scores in agent_scores carry the evidence behind it. It is advisory/ranking/reporting context only, never an order precondition.",
                "Return target_position_value_krw for every supplied symbol as the target KRW position value after this decision.",
                "The pipeline derives final_holding_quantity from target_position_value_krw / price.current_or_last with Decimal ROUND_HALF_UP; judge-supplied final_holding_quantity is optional and ignored for sizing.",
                "No additional buy, no extra exposure, or 추가 확대 없음 means target_position_value_krw must stay at the baseline (holding_quantity_context.expected_holding_quantity * price.current_or_last), not 0.",
                "When increasing after a same-day buy, additional_buy_reason may record new evidence or materially changed price/portfolio context; it is optional audit text and never an authorization gate.",
                "prior_decision_context links the latest earlier Judge target and reason to its order outcomes and subsequent confirmed-fill summary. Compare it with current evidence before changing the target; it is continuity context, never a hold or trade gate.",
                "For a held symbol with a low score, treat thesis integrity as one input, not a reduction gate. Thesis damage can support reduction, but relative attractiveness, concentration, profit/loss risk, opportunity cost, or a stronger alternative may independently support reducing or exiting even when the thesis remains intact.",
                "Use strategy_context and symbol_strategy_context as advisory inputs for target_position_value_krw, not as order allow/block rules.",
                "Use position_cost_context (average_purchase_price, purchase_amount, current_review_price, pct_distance_from_average_price) as reference information for profit/loss, risk, and position adjustments. Determine final direction and target exposure by considering thesis, market evidence, and portfolio risk together.",
                "prior_decision_context may carry the latest valid historical thesis_definition even when it came from an older decision. Use it as context, not as a mechanical authorization condition.",
                "When the historical thesis materially affects the target, you may return thesis_assessment: {status: intact|damaged|uncertain, matched_invalidation_condition_ids: [...]} and/or a new thesis_definition for future context. These are audit fields; missing or malformed values do not block the target. realized_pnl.status=unavailable is non-directional and must not be estimated.",
            ]
        )
        if review_scope_reasons:
            lines.append("review_scope_reasons: " + ",".join(f"{symbol}={reason}" for symbol, reason in sorted(review_scope_reasons.items())))
        if portfolio_snapshot:
            lines.extend(
                [
                    "Read-only portfolio snapshot of every held symbol (score/quantity/valuation/pnl_rate); use it for portfolio-level sizing context only and never return decisions for snapshot-only symbols:",
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

    if role_key == "financial" or stage_key in COLLECTION_STAGES:
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


def compact_review_payload_errors(
    payload: Any,
    stage: str,
    agent_role: str = "",
    expected_symbol_ids: Any | None = None,
) -> list[dict[str, Any]]:
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
    enforce_expected_symbols = stage == "judge-review" and expected_symbol_ids is not None
    expected_symbols = normalize_symbol_ids(expected_symbol_ids) if enforce_expected_symbols else []
    seen_judge_symbols: set[str] = set()
    for index, symbol in enumerate(symbols):
        if not isinstance(symbol, dict):
            errors.append(
                {
                    "code": "invalid_compact_review_schema",
                    "message": f"symbols[{index}] must be an object",
                }
            )
            continue
        if stage == "judge-review":
            symbol_id = symbol_key(symbol)
            if symbol_id:
                if symbol_id in seen_judge_symbols:
                    errors.append(
                        {
                            "code": "invalid_compact_review_schema",
                            "message": f"duplicate judge-review symbol {symbol_id}",
                        }
                    )
                seen_judge_symbols.add(symbol_id)
                if enforce_expected_symbols and symbol_id not in expected_symbols:
                    errors.append(
                        {
                            "code": "invalid_compact_review_schema",
                            "message": f"unexpected judge-review symbol {symbol_id}",
                        }
                    )
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
            for field in ("relative_attractiveness_rank", "reason_code", "one_line_reason", "opposing_view"):
                if field not in symbol:
                    errors.append(
                        {
                            "code": "invalid_compact_review_schema",
                            "message": f"symbols[{index}] missing {field}",
                        }
                    )
            if "decision_basis" in symbol and symbol.get("decision_basis") not in {"none", "thesis", "profit_protection", "concentration_rebalance"}:
                errors.append(
                    {
                        "code": "invalid_compact_review_schema",
                        "message": f"symbols[{index}].decision_basis must be one of none, thesis, profit_protection, concentration_rebalance",
                    }
                )
            if "opposing_view" in symbol:
                opposing_view = symbol.get("opposing_view")
                if not isinstance(opposing_view, dict):
                    errors.append(
                        {
                            "code": "invalid_compact_review_schema",
                            "message": f"symbols[{index}].opposing_view must be an object",
                        }
                    )
    if enforce_expected_symbols:
        missing_symbols = [
            symbol_id for symbol_id in expected_symbols if symbol_id not in seen_judge_symbols
        ]
        if missing_symbols:
            errors.append(
                {
                    "code": "invalid_compact_review_schema",
                    "message": "judge-review output missing symbols: " + ", ".join(missing_symbols),
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


def append_model_usage(
    spec: dict[str, Any],
    *,
    model: str,
    reasoning_effort: str,
    started_at: str,
) -> Path:
    path = Path(str(spec["output_dir"])) / MODEL_USAGE_FILENAME
    payload = {
        "schema_version": "1",
        "run_id": str(spec["run_id"]),
        "run_started_at": str(spec["started_at"]),
        "started_at": started_at,
        "source": "daily-trading-subagent",
        "stage": str(spec["stage"]),
        "agent_role": str(spec["agent_role"]),
        "task_name": str(spec["task_name"]),
        "model": model,
        "model_reasoning_effort": reasoning_effort,
    }
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        written = os.write(descriptor, encoded)
    finally:
        os.close(descriptor)
    if written != len(encoded):
        raise OSError(f"short write while recording model usage: {path}")
    return path


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
    if str(spec.get("stage", "")).strip() == "judge-review":
        output_dir = str(spec.get("output_dir", "")).strip()
        if output_dir:
            fingerprints["account_before_order_position_cost"] = file_sha256(Path(output_dir) / "account-before-order.json")
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
            "review_contract_version",
            "review_scope_reasons",
            "portfolio_snapshot",
            "extra_instructions",
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
    model_usage_path = append_model_usage(
        spec,
        model=model,
        reasoning_effort=effort,
        started_at=started_at,
    )
    cmd = [os.getenv("CODEX_BIN", "codex"), "exec"]
    cmd.extend(
        [
            "--json",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "--skip-git-repo-check",
            "-o",
            str(raw_output_path),
        ]
    )
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
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        errors.append(
            {
                "code": "exec_timeout",
                "message": f"codex exec exceeded the configured timeout after {exc.timeout} seconds",
            }
        )
    except Exception as exc:  # noqa: BLE001 - wrapper records sub-agent failures
        errors.append({"code": "exec_failed", "message": str(exc)})

    write_text(event_log_path, stdout)
    write_text(stderr_path, stderr)
    event_summary = parse_codex_json_events(stdout)
    event_diagnostics = summarize_codex_event_stream(stdout, event_summary["token_usage"])
    event_diagnostics["stderr_bytes"] = len(stderr.encode("utf-8", errors="replace"))
    degraded_dependencies = mcp_degraded_dependencies(stderr)
    event_diagnostics["degraded_dependency_count"] = len(degraded_dependencies)
    if degraded_dependencies:
        event_diagnostics["anomaly_detected"] = True
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
            compact_review_errors = compact_review_payload_errors(
                parsed_json,
                stage,
                str(spec.get("agent_role") or ""),
                normalize_symbol_ids(spec.get("symbol_ids") or spec.get("symbols"))
                if stage == "judge-review"
                else None,
            )
            errors.extend(compact_review_errors)
    event_thread_id = str(event_summary.get("thread_id") or "").strip()
    if returncode not in (0, None):
        errors.append({"code": "nonzero_returncode", "message": f"codex exec exited with {returncode}"})
    if stderr.strip():
        errors.append({"code": "stderr", "message": stderr.strip()[-2000:]})

    ended_at = now_iso()
    duration_ms = int((time.monotonic() - started) * 1000)
    if stage in TEXT_OUTPUT_STAGES:
        status = "success" if returncode == 0 and parsed_text and not text_errors else "failed"
    else:
        status = (
            "success"
            if returncode == 0
            and parsed_json is not None
            and not parse_errors
            and not compact_review_errors
            else "failed"
        )
    retention = "always" if stage in AUDIT_LOG_STAGES else raw_retention_mode()
    raw_output_retained = True
    if raw_output_path.exists() and (retention == "never" or (retention == "failed" and status == "success")):
        raw_output_path.unlink()
        raw_output_retained = False
    event_retention = "always" if stage in AUDIT_LOG_STAGES else event_retention_mode()
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
        "model": model,
        "model_reasoning_effort": effort,
        "model_usage_path": str(model_usage_path),
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "returncode": returncode,
        "wrapper_path": str(wrapper_path),
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
        "degraded_dependencies": degraded_dependencies,
        "parsed_json": parsed_json,
        "parsed_text": parsed_text,
        "errors": errors,
        "command": [part for part in cmd[:-1]],
        "prompt_mode": prompt_mode,
        "session_id": event_thread_id,
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
    degraded_dependencies = [
        {
            "task_name": str(wrapper.get("task_name") or ""),
            **dependency,
        }
        for wrapper in wrappers
        for dependency in wrapper.get("degraded_dependencies", [])
        if isinstance(dependency, dict)
    ]
    return {
        "schema_version": "1",
        "status": status,
        "count": len(wrappers),
        "failed_count": len(failed),
        "required_failed_count": len(required_failed),
        "optional_failed_count": len(optional_failed),
        "degraded_dependency_count": len(degraded_dependencies),
        "degraded_dependencies": degraded_dependencies,
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
