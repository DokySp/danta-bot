import json
from typing import Any

from .usage import add_token_usage, token_usage_from, zero_token_usage


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
