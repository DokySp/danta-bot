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
COLLECTION_SUBAGENT_MODEL = "gpt-5.4-mini"
COLLECTION_SUBAGENT_REASONING_EFFORT = "low"
FIRST_VERDICT_SUBAGENT_MODEL = "gpt-5.4-mini"
FIRST_VERDICT_SUBAGENT_REASONING_EFFORT = "medium"
SECOND_VERDICT_SUBAGENT_MODEL = "gpt-5.5"
SECOND_VERDICT_SUBAGENT_REASONING_EFFORT = "low"
COLLECTION_STAGES = {"financial-collection", "news-collection", "market-status-collection"}
FINANCIAL_PATH_OUTPUT_STAGES = {"financial-collection"}
NEWS_PATH_OUTPUT_STAGES = {"news-collection"}
MARKET_STATUS_TEXT_OUTPUT_STAGES = {"market-status-collection"}
TEXT_OUTPUT_STAGES = FINANCIAL_PATH_OUTPUT_STAGES | NEWS_PATH_OUTPUT_STAGES | MARKET_STATUS_TEXT_OUTPUT_STAGES
OPTIONAL_GROUP_FAILURE_STAGES = TEXT_OUTPUT_STAGES
VERDICT_STAGES = {"first-verdict", "second-verdict"}
MAX_BLANK_LINES = 1
RAW_RETENTION_VALUES = {"always", "failed", "never"}
DISALLOWED_COMPACT_VERDICT_KEYS = {
    "cash_rationale",
    "duplicate_exposure_limits",
    "evidence",
    "price_chart_view",
    "rationale",
    "risks",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


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


def raw_retention_mode() -> str:
    mode = os.getenv("CODEX_SUBAGENT_RAW_RETENTION", "always").strip().lower()
    if mode not in RAW_RETENTION_VALUES:
        return "always"
    return mode


def resolve_artifact_path(path_text: str, workspace_dir: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return Path(workspace_dir) / path


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
    return filtered


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

    for artifact_key, source_path_text in (
        ("decision_brief", decision_brief),
        ("verdict_first", artifacts.get("verdict_first") or artifacts.get("verdict-first") or ""),
    ):
        if not source_path_text:
            continue
        source_path = resolve_artifact_path(source_path_text, workspace_dir)
        payload = read_json_if_exists(source_path)
        if payload is None:
            continue
        sliced = filter_symbols(payload, symbols)
        relative_name = "decision-brief" if artifact_key == "decision_brief" else "verdict-first"
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
    if stage == "second-verdict":
        lines.extend(
            [
                "",
                "For second-verdict, use per-symbol scores from verdict_first when available.",
                "Interpret final_first_score as the confidence-adjusted first-verdict score: 5 is neutral, below 5 is a sell/reduce opinion, and above 5 is a buy/increase opinion.",
                "If a symbol's first-verdict score is missing, unavailable, or unusable, treat it as neutral 5 and continue.",
                "First-verdict scores are judgment inputs, not hard buy/sell gates.",
            ]
        )

    lines.extend(
        [
            "",
            "Return JSON only in the required compact verdict format. human_markdown_path is informational; the Main agent creates that sidecar from JSON.",
            "Use short reason_code and one_line_reason fields instead of long rationale, risk, evidence, or prose arrays.",
            "Optional financial/news/market-status absence is context only and must not lower score, target, or eligibility by itself.",
        ]
    )
    return compact_prompt("\n".join(lines))


def build_prompt(spec: dict[str, Any]) -> str:
    return compact_verdict_prompt(spec) or compact_prompt(str(spec.get("prompt", "")))


def launcher_model_effort(stage: str, agent_role: str) -> tuple[str, str]:
    stage_key = stage.strip().lower()
    role_key = agent_role.strip().lower()

    if role_key in {"financial", "news", "market-status"} or stage_key in COLLECTION_STAGES:
        return COLLECTION_SUBAGENT_MODEL, COLLECTION_SUBAGENT_REASONING_EFFORT
    if role_key in {"analyst", "juror"} or role_key.startswith(("analyst-", "juror-")) or stage_key == "first-verdict":
        return FIRST_VERDICT_SUBAGENT_MODEL, FIRST_VERDICT_SUBAGENT_REASONING_EFFORT
    if role_key == "judge" or role_key.startswith("judge-") or stage_key == "second-verdict":
        return SECOND_VERDICT_SUBAGENT_MODEL, SECOND_VERDICT_SUBAGENT_REASONING_EFFORT
    raise ValueError(f"unsupported daily-trading sub-agent stage/role: stage={stage!r}, agent_role={agent_role!r}")


def assert_unsupported_stage_rejected() -> None:
    try:
        launcher_model_effort("unsupported-stage", "unsupported-role")
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
    cases = [
        (
            "financial-collection",
            "financial",
            COLLECTION_SUBAGENT_MODEL,
            COLLECTION_SUBAGENT_REASONING_EFFORT,
        ),
        (
            "market-status-collection",
            "market-status",
            COLLECTION_SUBAGENT_MODEL,
            COLLECTION_SUBAGENT_REASONING_EFFORT,
        ),
        (
            "news-collection",
            "news",
            COLLECTION_SUBAGENT_MODEL,
            COLLECTION_SUBAGENT_REASONING_EFFORT,
        ),
        (
            "first-verdict",
            "analyst",
            FIRST_VERDICT_SUBAGENT_MODEL,
            FIRST_VERDICT_SUBAGENT_REASONING_EFFORT,
        ),
        (
            "first-verdict",
            "analyst-jpmorgan",
            FIRST_VERDICT_SUBAGENT_MODEL,
            FIRST_VERDICT_SUBAGENT_REASONING_EFFORT,
        ),
        (
            "second-verdict",
            "judge",
            SECOND_VERDICT_SUBAGENT_MODEL,
            SECOND_VERDICT_SUBAGENT_REASONING_EFFORT,
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
    if compact_verdict_requested:
        artifacts = normalize_artifact_paths(spec.get("artifact_paths"))
        decision_brief = artifacts.get("decision_brief") or artifacts.get("decision-brief") or artifacts.get("brief")
        symbols = normalize_symbol_ids(spec.get("symbol_ids") or spec.get("symbols"))
        if not decision_brief:
            raise ValueError("compact verdict spec requires artifact_paths.decision_brief")
        if not symbols:
            raise ValueError("compact verdict spec requires symbol_ids")
    stage = str(spec.get("stage", "")).strip()
    agent_role = safe_name(str(spec.get("agent_role", ""))).lower()
    task_name = safe_name(str(spec.get("task_name", ""))).lower()
    if stage == "first-verdict" and ("analyst-statestreet" in agent_role or "analyst-statestreet" in task_name):
        raise ValueError("analyst-statestreet is no longer a selected first-verdict persona")
    if stage == "second-verdict" and ("judge-longterm" in agent_role or "judge-longterm" in task_name):
        raise ValueError("judge-longterm is no longer a selected second-verdict judge")
    if stage == "second-verdict" and agent_role == "judge-midterm":
        retry_numbers = [
            int(match.group(1))
            for match in re.finditer(r"(?:retry|attempt)-?(\d+)", task_name)
        ]
        if retry_numbers and max(retry_numbers) > 2:
            raise ValueError("judge-midterm retry is limited to at most 2 retries")


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


def compact_verdict_payload_errors(payload: Any, stage: str) -> list[dict[str, Any]]:
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
        for field in ("symbol_id", "symbol_name", "reason_code", "one_line_reason"):
            if field not in symbol:
                errors.append(
                    {
                        "code": "invalid_compact_verdict_schema",
                        "message": f"symbols[{index}] missing {field}",
                    }
                )
        if stage == "first-verdict":
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
    if stage == "second-verdict":
        portfolio = payload.get("portfolio")
        if not isinstance(portfolio, dict):
            errors.append(
                {
                    "code": "invalid_compact_verdict_schema",
                    "message": "second-verdict must include portfolio object",
                }
            )
        else:
            for field in ("target_cash_amount", "cash_reason_code", "one_line_portfolio_reason"):
                if field not in portfolio:
                    errors.append(
                        {
                            "code": "invalid_compact_verdict_schema",
                            "message": f"portfolio missing {field}",
                        }
                    )
    return errors


def wrapper_paths(spec: dict[str, Any]) -> tuple[Path, Path]:
    output_dir = Path(str(spec["output_dir"]))
    task_name = safe_name(str(spec["task_name"]))
    subagent_dir = output_dir / "subagents"
    return subagent_dir / f"{task_name}.wrapper.json", subagent_dir / f"{task_name}.raw.txt"


def run_one(spec: dict[str, Any]) -> dict[str, Any]:
    validate_spec(spec)
    model, effort = launcher_model_effort(str(spec["stage"]), str(spec["agent_role"]))
    wrapper_path, raw_output_path = wrapper_paths(spec)
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)
    slice_paths = write_verdict_input_slices(spec)
    prompt_spec = spec_with_verdict_slices(spec, slice_paths)
    prompt_mode = "compact_verdict" if compact_verdict_prompt(prompt_spec) else "raw"

    started_at = now_iso()
    started = time.monotonic()
    cmd = [
        os.getenv("CODEX_BIN", "codex"),
        "exec",
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

    if raw_output_path.exists():
        raw_output = raw_output_path.read_text(encoding="utf-8", errors="replace")
    else:
        raw_output = stdout.strip()
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
            compact_verdict_errors = compact_verdict_payload_errors(parsed_json, stage)
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
        "parsed_json": parsed_json,
        "parsed_text": parsed_text,
        "errors": errors,
        "command": [part for part in cmd[:-1]],
        "prompt_mode": prompt_mode,
        "verdict_input_paths": slice_paths,
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
    workers = max_workers or min(8, max(1, len(specs)))
    wrappers: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(run_one, spec): spec for spec in specs}
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
    parser = argparse.ArgumentParser(description="Run daily-trading sub-agents with fixed model/effort.")
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
    if "financial" in task_name or "news" in task_name or "market-status" in task_name:
        domain = "financial" if "financial" in task_name else "news"
        if "market-status" in task_name:
            domain = "market-status"
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
                            else {"score": 5, "confidence": 5, "missing_data": []}
                        ),
                    }
                ],
                "errors": [],
            }
            if stage == "second-verdict":
                payload["portfolio"] = {
                    "target_cash_amount": 0,
                    "cash_reason_code": "cash_buffer",
                    "one_line_portfolio_reason": "self-test",
                }
        else:
            payload = {"ok": True, "argv": sys.argv[1:]}
    output_path.write_text(json.dumps(payload), encoding="utf-8")
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
        "persona": "references/personas/analyst-jpmorgan.md",
        "verdict_format": "references/rules/verdict-format.md",
    }
    payload["symbol_ids"] = ["005930", {"symbol_id": "000660"}, "005930"]
    return payload


def write_sample_verdict_inputs(tmp: Path) -> None:
    run_dir = tmp / "reports" / "runs" / "self-test"
    write_json(
        run_dir / "decision-brief.json",
        {
            "schema_version": "1",
            "symbols": [
                {"symbol_id": "005930", "symbol_name": "삼성전자"},
                {"symbol_id": "000660", "symbol_name": "SK하이닉스"},
                {"symbol_id": "035420", "symbol_name": "NAVER"},
            ],
        },
    )
    write_json(
        run_dir / "verdict-first.json",
        {
            "schema_version": "1",
            "symbols": [
                {"symbol_id": "005930", "score": 7},
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
    prompt = build_prompt(compact_spec(tmp, stage="first-verdict", agent_role="analyst-jpmorgan", task_name="first"))
    required_parts = [
        "stage: first-verdict",
        "agent_role: analyst-jpmorgan",
        "You may use read-only local shell commands such as cat and jq only for the explicitly listed files.",
        "Do not call KIS, MCP, web, network, account/order APIs, or external data sources.",
        "Do not write files, create Markdown, emit diffs, or wrap output in code fences.",
        "Read only the listed symbol_ids from artifact files; do not load unrelated symbols, raw cache files, secrets, or unlisted paths.",
        "decision_brief:",
        "persona: references/personas/analyst-jpmorgan.md",
        "verdict_format: references/rules/verdict-format.md",
        "symbol_ids: 005930,000660",
        "Return JSON only",
    ]
    missing = [part for part in required_parts if part not in prompt]
    if missing:
        raise AssertionError(f"compact verdict prompt missing {missing}: {prompt}")

    second_prompt = build_prompt(
        compact_spec(tmp, stage="second-verdict", agent_role="judge-midterm", task_name="second")
    )
    second_required_parts = [
        "stage: second-verdict",
        "verdict_first:",
        "For second-verdict, use per-symbol scores from verdict_first when available.",
        "Interpret final_first_score as the confidence-adjusted first-verdict score: 5 is neutral, below 5 is a sell/reduce opinion, and above 5 is a buy/increase opinion.",
        "If a symbol's first-verdict score is missing, unavailable, or unusable, treat it as neutral 5 and continue.",
        "First-verdict scores are judgment inputs, not hard buy/sell gates.",
    ]
    missing = [part for part in second_required_parts if part not in second_prompt]
    if missing:
        raise AssertionError(f"compact second-verdict prompt missing {missing}: {second_prompt}")


def assert_verdict_input_slices(tmp: Path) -> None:
    write_sample_verdict_inputs(tmp)
    payload = compact_spec(tmp, stage="second-verdict", agent_role="judge", task_name="slice-test")
    slices = write_verdict_input_slices(payload)
    expected_keys = {"decision_brief", "verdict_first"}
    if set(slices) != expected_keys:
        raise AssertionError(f"unexpected slice keys: {slices}")
    for slice_path_text in slices.values():
        slice_payload = load_json(Path(slice_path_text))
        symbols = [item.get("symbol_id") for item in slice_payload.get("symbols", [])]
        if symbols != ["005930", "000660"]:
            raise AssertionError(f"unexpected sliced symbols for {slice_path_text}: {symbols}")


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
                    tmp, stage="first-verdict", agent_role="analyst-jpmorgan", task_name="missing-brief"
                )
                missing_brief["artifact_paths"].pop("decision_brief")
                assert_invalid_spec(missing_brief, "artifact_paths.decision_brief")
                missing_symbols = compact_spec(
                    tmp, stage="first-verdict", agent_role="analyst-jpmorgan", task_name="missing-symbols"
                )
                missing_symbols["symbol_ids"] = []
                assert_invalid_spec(missing_symbols, "symbol_ids")
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="first-verdict",
                        agent_role="analyst-statestreet",
                        task_name="analyst-statestreet",
                    ),
                    "analyst-statestreet",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="first-verdict",
                        agent_role="analyst",
                        task_name="analyst-statestreet-retry1",
                    ),
                    "analyst-statestreet",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge-longterm",
                        task_name="judge-longterm",
                    ),
                    "judge-longterm",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge",
                        task_name="judge-longterm-retry1",
                    ),
                    "judge-longterm",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge-midterm",
                        task_name="judge-midterm-retry3",
                    ),
                    "at most 2 retries",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge-midterm",
                        task_name="judge-midterm-attempt3",
                    ),
                    "at most 2 retries",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge-midterm",
                        task_name="judge-midterm-retry-3",
                    ),
                    "at most 2 retries",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge-midterm",
                        task_name="judge-midterm-attempt-3",
                    ),
                    "at most 2 retries",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="second-verdict",
                        agent_role="judge-midterm",
                        task_name="judge-midterm-retry1-attempt3",
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
                raw_with_artifacts = compact_spec(
                    tmp, stage="first-verdict", agent_role="analyst-jpmorgan", task_name="raw-with-artifacts"
                )
                raw_with_artifacts["prompt"] = '{"return":"json only"}'
                validate_spec(raw_with_artifacts)
                if build_prompt(raw_with_artifacts) != '{"return":"json only"}':
                    raise AssertionError("prompt-based spec with artifact metadata did not remain raw")
                if write_verdict_input_slices(raw_with_artifacts):
                    raise AssertionError("prompt-based spec with artifact metadata created verdict slices")
            except AssertionError as exc:
                failures.append(str(exc))

            cases = [
                (
                    spec(tmp, stage="financial-collection", agent_role="financial", task_name="financial"),
                    COLLECTION_SUBAGENT_MODEL,
                    COLLECTION_SUBAGENT_REASONING_EFFORT,
                ),
                (
                    spec(tmp, stage="first-verdict", agent_role="analyst", task_name="first"),
                    FIRST_VERDICT_SUBAGENT_MODEL,
                    FIRST_VERDICT_SUBAGENT_REASONING_EFFORT,
                ),
                (
                    spec(tmp, stage="second-verdict", agent_role="judge", task_name="second"),
                    SECOND_VERDICT_SUBAGENT_MODEL,
                    SECOND_VERDICT_SUBAGENT_REASONING_EFFORT,
                ),
            ]
            for test_spec, model, effort in cases:
                wrapper = run_one(test_spec)
                if wrapper["status"] != "success":
                    failures.append(f"{test_spec['task_name']} returned {wrapper['status']}")
                try:
                    assert_argv(argv_log, model=model, effort=effort)
                except AssertionError as exc:
                    failures.append(str(exc))

            compact_wrapper = run_one(
                compact_spec(tmp, stage="first-verdict", agent_role="analyst-jpmorgan", task_name="compact-first")
            )
            if compact_wrapper["status"] != "success" or compact_wrapper.get("prompt_mode") != "compact_verdict":
                failures.append(f"compact verdict spec returned unexpected wrapper: {compact_wrapper}")
            if not compact_wrapper.get("verdict_input_paths", {}).get("decision_brief"):
                failures.append(f"compact verdict spec did not create decision brief slice: {compact_wrapper}")

            old_raw_retention = os.environ.get("CODEX_SUBAGENT_RAW_RETENTION")
            os.environ["CODEX_SUBAGENT_RAW_RETENTION"] = "failed"
            retained_wrapper = run_one(
                compact_spec(tmp, stage="first-verdict", agent_role="analyst-jpmorgan", task_name="raw-retention")
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
                    spec(
                        tmp,
                        stage="market-status-collection",
                        agent_role="market-status",
                        task_name="g-market-status",
                    ),
                    spec(tmp, stage="news-collection", agent_role="news", task_name="g-news"),
                ],
                max_workers=3,
            )
            if group["status"] != "success" or group["count"] != 3:
                failures.append(f"run-group returned unexpected result: {group}")
            wrapper_count = len(list((Path(group["wrappers"][0]["raw_output_path"]).parent).glob("g-*.wrapper.json")))
            if wrapper_count != 3:
                failures.append(f"expected 3 group wrapper files, got {wrapper_count}")

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
                    spec(tmp, stage="first-verdict", agent_role="analyst", task_name="required-first"),
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
