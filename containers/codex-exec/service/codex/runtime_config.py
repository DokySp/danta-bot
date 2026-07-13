from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


RUNTIME_CONFIG_TOP_LEVEL_KEYS = {"defaults", "daily_trading"}
RUNTIME_DEFAULT_KEYS = {"model", "model_reasoning_effort", "new_session_prompt"}
DEFAULT_RUNTIME_CONFIG_PATH = Path("/app/config/codex-runtime.yaml")
BAKED_RUNTIME_CONFIG_PATH = Path("/app/default-config/codex-runtime.yaml")


@dataclass(frozen=True)
class CodexRuntimeDefaults:
    model: str
    model_reasoning_effort: str
    new_session_prompt: str


def _required_text(payload: dict[str, Any], key: str, source: Path) -> str:
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"codex runtime config defaults.{key} must not be empty: {source}")
    return raw.strip()


def load_codex_runtime_defaults(path: Path) -> CodexRuntimeDefaults:
    if not path.exists() and path == DEFAULT_RUNTIME_CONFIG_PATH and BAKED_RUNTIME_CONFIG_PATH.exists():
        path = BAKED_RUNTIME_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"codex runtime config does not exist: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"codex runtime config must be an object: {path}")

    extra_top_level = sorted(str(key) for key in payload if str(key) not in RUNTIME_CONFIG_TOP_LEVEL_KEYS)
    if extra_top_level:
        raise ValueError(f"unsupported codex runtime config keys: {', '.join(extra_top_level)}")

    if not isinstance(payload.get("daily_trading"), dict):
        raise ValueError(f"codex runtime config daily_trading must be an object: {path}")

    defaults = payload.get("defaults")
    if not isinstance(defaults, dict):
        raise ValueError(f"codex runtime config defaults must be an object: {path}")
    extra_defaults = sorted(str(key) for key in defaults if str(key) not in RUNTIME_DEFAULT_KEYS)
    if extra_defaults:
        raise ValueError(f"unsupported codex runtime config defaults keys: {', '.join(extra_defaults)}")

    return CodexRuntimeDefaults(
        model=_required_text(defaults, "model", path),
        model_reasoning_effort=_required_text(defaults, "model_reasoning_effort", path),
        new_session_prompt=_required_text(defaults, "new_session_prompt", path),
    )
