import json
import logging
from collections.abc import Callable
from typing import Any


def parse_json_object(raw_line: str) -> dict[str, Any] | None:
    line = raw_line.strip()
    if not line:
        return None
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        return None
    return item if isinstance(item, dict) else None


def _agent_message(item: dict[str, Any]) -> str | None:
    if item.get("type") == "item.completed":
        payload = item.get("item")
        if isinstance(payload, dict) and payload.get("type") == "agent_message":
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
        return None

    if item.get("type") != "event_msg":
        return None
    payload = item.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "agent_message":
        return None
    text = payload.get("message")
    return text.strip() if isinstance(text, str) and text.strip() else None


def _public_reasoning(item: dict[str, Any]) -> str | None:
    if item.get("type") == "item.completed":
        payload = item.get("item")
        if isinstance(payload, dict) and payload.get("type") == "reasoning":
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
        return None

    if item.get("type") != "event_msg":
        return None
    payload = item.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "agent_reasoning":
        return None
    text = payload.get("text")
    return text.strip() if isinstance(text, str) and text.strip() else None


def _work_continues(item: dict[str, Any]) -> bool:
    if item.get("type") not in {"item.started", "item.completed"}:
        return False
    payload = item.get("item")
    if not isinstance(payload, dict):
        return False
    return payload.get("type") not in {"agent_message", "reasoning"}


class CodexProgressBridge:
    """Forward public Codex progress while keeping the final answer out of drafts."""

    def __init__(self, on_progress: Callable[[str], None]) -> None:
        self._on_progress = on_progress
        self._pending_agent_message: str | None = None

    def handle_line(self, raw_line: str) -> None:
        item = parse_json_object(raw_line)
        if item is None:
            return

        message = _agent_message(item)
        if message is not None:
            if self._pending_agent_message is not None:
                self._emit(self._pending_agent_message)
            self._pending_agent_message = message
            return

        reasoning = _public_reasoning(item)
        if reasoning is not None:
            # A reasoning event is newer than the buffered agent message, but it
            # does not prove that message was not the final answer. Drop the
            # candidate instead of risking the final answer appearing as a draft.
            self._pending_agent_message = None
            self._emit(reasoning)
            return

        if self._pending_agent_message is not None and _work_continues(item):
            self._emit(self._pending_agent_message)
            self._pending_agent_message = None

    def finish(self) -> None:
        # The last agent_message in the stream is the final answer candidate.
        self._pending_agent_message = None

    def _emit(self, text: str) -> None:
        try:
            self._on_progress(text)
        except Exception:  # noqa: BLE001 - draft delivery must not break Codex
            logging.exception("codex progress callback failed")
