#!/usr/bin/env python3
"""Run daily-trading sub-agent stages through codex exec."""

from __future__ import annotations

import argparse
import json
import os
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
SUBAGENT_MODEL = "gpt-5.5"
SUBAGENT_REASONING_EFFORT = "low"
COLLECTION_STAGES = {"financial-collection", "news-collection", "market-status-collection"}
FINANCIAL_PATH_OUTPUT_STAGES = {"financial-collection"}
NEWS_PATH_OUTPUT_STAGES = {"news-collection"}
MARKET_STATUS_TEXT_OUTPUT_STAGES = {"market-status-collection"}
TEXT_OUTPUT_STAGES = FINANCIAL_PATH_OUTPUT_STAGES | NEWS_PATH_OUTPUT_STAGES | MARKET_STATUS_TEXT_OUTPUT_STAGES
OPTIONAL_GROUP_FAILURE_STAGES = TEXT_OUTPUT_STAGES


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


def safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    return cleaned.strip(".-") or "subagent"


def launcher_model_effort(stage: str, agent_role: str) -> tuple[str, str]:
    stage_key = stage.strip().lower()
    role_key = agent_role.strip().lower()

    if (
        role_key in {"financial", "news", "market-status", "analyst", "juror", "judge"}
        or role_key.startswith(("analyst-", "juror-", "judge-"))
        or stage_key in {
            "financial-collection",
            "market-status-collection",
            "news-collection",
            "first-verdict",
            "second-verdict",
        }
    ):
        return SUBAGENT_MODEL, SUBAGENT_REASONING_EFFORT
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


def assert_all_supported_stages_use_subagent_defaults() -> None:
    cases = [
        ("financial-collection", "financial"),
        ("market-status-collection", "market-status"),
        ("news-collection", "news"),
        ("first-verdict", "analyst"),
        ("first-verdict", "analyst-jpmorgan"),
        ("second-verdict", "judge"),
    ]
    for stage, role in cases:
        assert_model_effort(stage, role, model=SUBAGENT_MODEL, effort=SUBAGENT_REASONING_EFFORT)

def validate_spec(spec: dict[str, Any]) -> None:
    missing = sorted(field for field in REQUIRED_SPEC_FIELDS if not str(spec.get(field, "")).strip())
    if missing:
        raise ValueError("missing required spec fields: " + ", ".join(missing))


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
    cmd.append(str(spec["prompt"]))

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
    if returncode not in (0, None):
        errors.append({"code": "nonzero_returncode", "message": f"codex exec exited with {returncode}"})
    if stderr.strip():
        errors.append({"code": "stderr", "message": stderr.strip()[-2000:]})

    ended_at = now_iso()
    duration_ms = int((time.monotonic() - started) * 1000)
    if stage in TEXT_OUTPUT_STAGES:
        status = "success" if returncode == 0 and parsed_text and not text_errors else "failed"
    else:
        status = "success" if returncode == 0 and parsed_json is not None and not parse_errors else "failed"
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
        "parsed_json": parsed_json,
        "parsed_text": parsed_text,
        "errors": errors,
        "command": [part for part in cmd[:-1]],
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
                assert_all_supported_stages_use_subagent_defaults()
                assert_unsupported_stage_rejected()
            except AssertionError as exc:
                failures.append(str(exc))

            cases = [
                (
                    spec(tmp, stage="first-verdict", agent_role="analyst", task_name="first"),
                    SUBAGENT_MODEL,
                    SUBAGENT_REASONING_EFFORT,
                ),
                (
                    spec(tmp, stage="second-verdict", agent_role="judge", task_name="second"),
                    SUBAGENT_MODEL,
                    SUBAGENT_REASONING_EFFORT,
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
