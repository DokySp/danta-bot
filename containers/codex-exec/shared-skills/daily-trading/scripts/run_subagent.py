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
COLLECTOR_MODEL = "gpt-5.3-codex-spark"
VERDICT_MODEL = "gpt-5.5"
COLLECTION_STAGES = {"market-collection", "financial-collection", "news-collection"}


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


def mandatory_model_effort(stage: str, agent_role: str) -> tuple[str, str]:
    stage_key = stage.strip().lower()
    role_key = agent_role.strip().lower()

    if role_key in {"account", "collect-account-state"} or stage_key in {
        "account-before-verdict",
        "account-before-order",
    }:
        return COLLECTOR_MODEL, "low"
    if role_key in {"market", "financial", "news"} or stage_key in {
        "market-collection",
        "financial-collection",
        "news-collection",
    }:
        return COLLECTOR_MODEL, "low"
    if role_key in {"analyst", "juror"} or role_key.startswith(("analyst-", "juror-")) or stage_key == "first-verdict":
        return VERDICT_MODEL, "low"
    if role_key == "judge" or role_key.startswith("judge-") or stage_key == "second-verdict":
        return VERDICT_MODEL, "high"
    if role_key in {"final-risk", "final-risk-verdict"} or stage_key == "final-risk-verdict":
        return VERDICT_MODEL, "high"
    raise ValueError(f"unsupported daily-trading sub-agent stage/role: stage={stage!r}, agent_role={agent_role!r}")


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


def symbol_id_from(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("symbol_id", "pdno", "stock_code", "code", "symbol"):
            raw = value.get(key)
            if raw is not None and str(raw).strip():
                return str(raw).strip()
    elif value is not None and str(value).strip():
        return str(value).strip()
    return ""


def expected_symbol_ids(spec: dict[str, Any]) -> list[str]:
    for key in ("symbol_ids", "symbols", "portfolio_symbols", "complete_symbol_list"):
        raw = spec.get(key)
        if not isinstance(raw, list):
            continue
        result: list[str] = []
        seen: set[str] = set()
        for item in raw:
            sid = symbol_id_from(item)
            if not sid or sid in seen:
                continue
            seen.add(sid)
            result.append(sid)
        if result:
            return result
    return []


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def normalize_market_payload(payload: Any) -> Any:
    """Accept common market-agent aliases but expose the canonical artifact keys."""
    if not isinstance(payload, dict):
        return payload
    if str(payload.get("stage", "")).strip() != "market-collection":
        return payload

    if str(payload.get("schema_version", "")).strip() in {"1.0", "1"}:
        payload["schema_version"] = "1"

    symbols = payload.get("symbols")
    if not isinstance(symbols, list):
        return payload

    for item in symbols:
        if not isinstance(item, dict):
            continue

        identity = item.get("market_identity") if isinstance(item.get("market_identity"), dict) else {}
        price_alias = (
            item.get("current_or_latest_price")
            if isinstance(item.get("current_or_latest_price"), dict)
            else {}
        )

        sid = first_non_empty(item.get("symbol_id"), item.get("symbol"), item.get("pdno"), item.get("stock_code"))
        if sid is not None:
            item["symbol_id"] = str(sid)

        name = first_non_empty(item.get("symbol_name"), identity.get("name"), item.get("name"), item.get("stock_name"))
        if name is not None:
            item["symbol_name"] = str(name)

        product_type = first_non_empty(item.get("product_type"), identity.get("product_type"), "unresolved")
        item["product_type"] = str(product_type)

        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        current_or_last = first_non_empty(price.get("current_or_last"), price_alias.get("current_or_last"), price_alias.get("price"))
        observed_at = first_non_empty(price.get("observed_at"), price_alias.get("observed_at"))
        snapshot_mode = first_non_empty(price.get("snapshot_mode"), price_alias.get("snapshot_mode"))
        item["price"] = {
            **price,
            "current_or_last": current_or_last,
            "observed_at": observed_at or "",
            "snapshot_mode": snapshot_mode or "",
        }

        if "market_context" not in item:
            item["market_context"] = identity
        if "order_book" not in item and isinstance(item.get("order_book_trade_context"), dict):
            item["order_book"] = item.get("order_book_trade_context")
        if "local_signals" not in item and isinstance(item.get("market_signals"), dict):
            item["local_signals"] = [item.get("market_signals")]
        item.setdefault("charts", {"daily": [], "weekly": [], "monthly": []})
        item.setdefault("trades", [])
        item.setdefault("investor_flow", {})
        item.setdefault("rank_and_industry", {})
        item.setdefault("etf_etn", {})
        item.setdefault("sources", [])
        item.setdefault("errors", [])
        item.setdefault("required_missing", [])
        item.setdefault("eligible_for_verdict", bool(current_or_last and observed_at))

    return payload


def normalize_parsed_json(spec: dict[str, Any], parsed_json: Any) -> Any:
    if str(spec.get("stage", "")).strip() == "market-collection":
        return normalize_market_payload(parsed_json)
    return parsed_json


def structural_errors(spec: dict[str, Any], parsed_json: Any) -> list[dict[str, Any]]:
    stage = str(spec.get("stage", "")).strip()
    if stage not in COLLECTION_STAGES:
        return []
    if not isinstance(parsed_json, dict):
        return [{"code": "schema_type", "message": "collection output must be a JSON object"}]

    errors: list[dict[str, Any]] = []
    if parsed_json.get("schema_version") != "1":
        errors.append({"code": "schema_version", "message": "schema_version must be '1'"})
    if str(parsed_json.get("run_id", "")) != str(spec.get("run_id", "")):
        errors.append({"code": "run_id_mismatch", "message": "collection output run_id must match the stage spec"})

    symbols = parsed_json.get("symbols")
    if not isinstance(symbols, list):
        errors.append({"code": "missing_symbols", "message": "top-level symbols list is required"})
        return errors

    ids: list[str] = []
    for index, item in enumerate(symbols):
        sid = symbol_id_from(item)
        if not sid:
            errors.append({"code": "missing_symbol_id", "message": f"symbol_id is required for symbols[{index}]"})
            continue
        if "," in sid:
            errors.append({"code": "comma_joined_symbol_id", "message": f"symbol_id must represent one symbol at symbols[{index}]"})
        ids.append(sid)

        if stage == "market-collection" and isinstance(item, dict):
            price = item.get("price") if isinstance(item.get("price"), dict) else {}
            if not str(item.get("symbol_name", "")).strip():
                errors.append({"code": "missing_symbol_name", "message": f"symbol_name is required for {sid}"})
            if price.get("current_or_last") in (None, "") or not str(price.get("observed_at", "")).strip():
                errors.append({"code": "missing_market_price", "message": f"price.current_or_last and price.observed_at are required for {sid}"})

    expected = expected_symbol_ids(spec)
    if expected:
        unique_ids = set(ids)
        missing = [sid for sid in expected if sid not in unique_ids]
        extra = sorted(unique_ids - set(expected))
        if len(ids) != len(expected):
            errors.append({"code": "symbol_count_mismatch", "message": f"expected {len(expected)} symbols, got {len(ids)}"})
        if missing:
            errors.append({"code": "missing_universe_symbols", "message": "missing symbols: " + ", ".join(missing[:20])})
        if extra:
            errors.append({"code": "extra_symbols", "message": "unexpected symbols: " + ", ".join(extra[:20])})

    return errors


def wrapper_paths(spec: dict[str, Any]) -> tuple[Path, Path]:
    output_dir = Path(str(spec["output_dir"]))
    task_name = safe_name(str(spec["task_name"]))
    subagent_dir = output_dir / "subagents"
    return subagent_dir / f"{task_name}.wrapper.json", subagent_dir / f"{task_name}.raw.txt"


def run_one(spec: dict[str, Any]) -> dict[str, Any]:
    validate_spec(spec)
    actual_model, actual_effort = mandatory_model_effort(str(spec["stage"]), str(spec["agent_role"]))
    wrapper_path, raw_output_path = wrapper_paths(spec)
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)

    started_at = now_iso()
    started = time.monotonic()
    cmd = [
        os.getenv("CODEX_BIN", "codex"),
        "exec",
        "-m",
        actual_model,
        "-c",
        f'model_reasoning_effort="{actual_effort}"',
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

    parsed_json, parse_errors = parse_json_output(raw_output)
    if parsed_json is not None:
        parsed_json = normalize_parsed_json(spec, parsed_json)
    errors.extend(parse_errors)
    schema_errors: list[dict[str, Any]] = []
    if parsed_json is not None and not parse_errors:
        schema_errors = structural_errors(spec, parsed_json)
        errors.extend(schema_errors)
    if returncode not in (0, None):
        errors.append({"code": "nonzero_returncode", "message": f"codex exec exited with {returncode}"})
    if stderr.strip():
        errors.append({"code": "stderr", "message": stderr.strip()[-2000:]})

    ended_at = now_iso()
    duration_ms = int((time.monotonic() - started) * 1000)
    status = "success" if returncode == 0 and parsed_json is not None and not parse_errors and not schema_errors else "failed"
    wrapper = {
        "schema_version": "1",
        "run_id": str(spec["run_id"]),
        "run_started_at": str(spec["started_at"]),
        "stage": str(spec["stage"]),
        "agent_role": str(spec["agent_role"]),
        "task_name": str(spec["task_name"]),
        "status": status,
        "actual_model": actual_model,
        "actual_effort": actual_effort,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "returncode": returncode,
        "raw_output_path": str(raw_output_path),
        "parsed_json": parsed_json,
        "errors": errors,
        "metric": {
            "stage": str(spec["stage"]),
            "agent_role": str(spec["agent_role"]),
            "recommended_model": actual_model,
            "recommended_effort": actual_effort,
            "actual_model": actual_model,
            "actual_effort": actual_effort,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": duration_ms,
            "status": status,
            "token_usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
            "token_source": "unavailable",
            "token_unavailable_reason": "sub-agent launcher did not expose token usage",
        },
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
    return {
        "schema_version": "1",
        "status": "failed" if failed else "success",
        "count": len(wrappers),
        "failed_count": len(failed),
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
if os.environ.get("FAKE_CODEX_INVALID_JSON") == "1":
    output_path.write_text("not json", encoding="utf-8")
else:
    task_name = output_path.name.removesuffix(".raw.txt")
    if "market" in task_name:
        payload = {
            "schema_version": "1",
            "run_id": "self-test",
            "started_at": "2026-06-08T09:00:00+09:00",
            "generated_at": "2026-06-08T09:00:01+09:00",
            "stage": "market-collection",
            "domain": "market",
            "status": "success",
            "skipped": False,
            "skip_reason": "",
            "attempts": [],
            "errors": [],
            "symbols": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "product_type": "stock",
                    "eligible_for_verdict": True,
                    "required_missing": [],
                    "price": {
                        "current_or_last": 100,
                        "observed_at": "2026-06-08T09:00:00+09:00",
                        "snapshot_mode": "live",
                    },
                    "errors": [],
                }
            ],
        }
    elif "financial" in task_name or "news" in task_name:
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
            cases = [
                (spec(tmp, stage="market-collection", agent_role="market", task_name="market"), COLLECTOR_MODEL, "low"),
                (spec(tmp, stage="first-verdict", agent_role="analyst", task_name="first"), VERDICT_MODEL, "low"),
                (spec(tmp, stage="second-verdict", agent_role="judge", task_name="second"), VERDICT_MODEL, "high"),
                (spec(tmp, stage="final-risk-verdict", agent_role="final-risk", task_name="risk"), VERDICT_MODEL, "high"),
            ]
            for test_spec, model, effort in cases:
                wrapper = run_one(test_spec)
                if wrapper["status"] != "success":
                    failures.append(f"{test_spec['task_name']} returned {wrapper['status']}")
                try:
                    assert_argv(argv_log, model=model, effort=effort)
                except AssertionError as exc:
                    failures.append(str(exc))

            os.environ["FAKE_CODEX_INVALID_JSON"] = "1"
            invalid = spec(tmp, stage="news-collection", agent_role="news", task_name="invalid-json")
            wrapper = run_one(invalid)
            if wrapper["status"] != "failed" or wrapper["parsed_json"] is not None:
                failures.append("invalid JSON did not produce failed wrapper with parsed_json=null")
            if (Path(invalid["output_dir"]) / "news.json").exists():
                failures.append("launcher wrote canonical news.json")
            os.environ.pop("FAKE_CODEX_INVALID_JSON", None)

            market_spec = spec(tmp, stage="market-collection", agent_role="market", task_name="market-alias")
            market_spec["symbol_ids"] = ["005930", "000660"]
            market_payload = {
                "schema_version": "1.0",
                "run_id": "self-test",
                "stage": "market-collection",
                "status": "partial",
                "symbols": [
                    {
                        "symbol": "005930",
                        "market_identity": {"name": "삼성전자", "product_type": "stock"},
                        "current_or_latest_price": {
                            "price": 100,
                            "observed_at": "2026-06-08T09:00:00+09:00",
                            "snapshot_mode": "live",
                        },
                    }
                ],
            }
            normalized = normalize_parsed_json(market_spec, market_payload)
            if normalized["schema_version"] != "1" or normalized["symbols"][0].get("symbol_id") != "005930":
                failures.append("market alias payload was not normalized to canonical fields")
            market_errors = structural_errors(market_spec, normalized)
            if not any(error["code"] == "missing_universe_symbols" for error in market_errors):
                failures.append("market structural validation did not reject missing symbol coverage")

            group = run_group(
                [
                    spec(tmp, stage="market-collection", agent_role="market", task_name="g-market"),
                    spec(tmp, stage="financial-collection", agent_role="financial", task_name="g-financial"),
                    spec(tmp, stage="news-collection", agent_role="news", task_name="g-news"),
                ],
                max_workers=3,
            )
            if group["status"] != "success" or group["count"] != 3:
                failures.append(f"run-group returned unexpected result: {group}")
            wrapper_count = len(list((Path(group["wrappers"][0]["raw_output_path"]).parent).glob("g-*.wrapper.json")))
            if wrapper_count != 3:
                failures.append(f"expected 3 group wrapper files, got {wrapper_count}")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    status = "failed" if failures else "passed"
    print(json.dumps({"status": status, "failures": failures}, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
