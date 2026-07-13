from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, ScalarNode


RUNTIME_CONFIG_TOP_LEVEL_KEYS = {"defaults", "daily_trading"}
RUNTIME_DEFAULT_KEYS = {"model", "model_reasoning_effort", "new_session_prompt"}
DEFAULT_RUNTIME_CONFIG_PATH = Path("/app/config/codex-runtime.yaml")
BAKED_RUNTIME_CONFIG_PATH = Path("/app/default-config/codex-runtime.yaml")


@dataclass(frozen=True)
class CodexRuntimeDefaults:
    model: str
    model_reasoning_effort: str
    new_session_prompt: str


@dataclass(frozen=True)
class CodexRuntimeDefaultsUpdate:
    previous_value: str
    current_value: str
    changed: bool


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


def update_codex_runtime_reasoning_effort(
    path: Path,
    reasoning_effort: str,
) -> CodexRuntimeDefaultsUpdate:
    normalized_effort = reasoning_effort.strip()
    if not normalized_effort:
        raise ValueError("model_reasoning_effort must not be empty")

    source_path = path
    if (
        not source_path.exists()
        and path == DEFAULT_RUNTIME_CONFIG_PATH
        and BAKED_RUNTIME_CONFIG_PATH.exists()
    ):
        source_path = BAKED_RUNTIME_CONFIG_PATH
    defaults = load_codex_runtime_defaults(path)
    source_text = source_path.read_text(encoding="utf-8")
    updated_text = _replace_defaults_reasoning_effort(source_text, normalized_effort, source_path)
    changed = defaults.model_reasoning_effort != normalized_effort

    if changed or source_path != path:
        _atomic_write_text(path, updated_text, mode_source=source_path)

    return CodexRuntimeDefaultsUpdate(
        previous_value=defaults.model_reasoning_effort,
        current_value=normalized_effort,
        changed=changed,
    )


def _replace_defaults_reasoning_effort(source_text: str, value: str, source: Path) -> str:
    document = yaml.compose(source_text)
    if not isinstance(document, MappingNode):
        raise ValueError(f"codex runtime config must be an object: {source}")

    defaults_node: MappingNode | None = None
    for key_node, value_node in document.value:
        if isinstance(key_node, ScalarNode) and key_node.value == "defaults":
            if not isinstance(value_node, MappingNode):
                raise ValueError(f"codex runtime config defaults must be an object: {source}")
            defaults_node = value_node
            break
    if defaults_node is None:
        raise ValueError(f"codex runtime config defaults must be an object: {source}")

    effort_node: ScalarNode | None = None
    for key_node, value_node in defaults_node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == "model_reasoning_effort":
            if not isinstance(value_node, ScalarNode):
                raise ValueError(
                    f"codex runtime config defaults.model_reasoning_effort must be text: {source}"
                )
            effort_node = value_node
            break
    if effort_node is None:
        raise ValueError(
            f"codex runtime config defaults.model_reasoning_effort must not be empty: {source}"
        )

    serialized_value = json.dumps(value, ensure_ascii=False)
    return (
        source_text[: effort_node.start_mark.index]
        + serialized_value
        + source_text[effort_node.end_mark.index :]
    )


def _atomic_write_text(path: Path, content: str, *, mode_source: Path) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        temporary_path.chmod(stat.S_IMODE(mode_source.stat().st_mode))
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
