#!/usr/bin/env python3
"""Validate daily-trading run artifacts."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_STATUS = {"success", "partial", "failed"}
DOMAIN_BRIEF_ARTIFACTS = [
    "market.json",
    "financial.json",
    "news.json",
    "merged.json",
    "decision-brief.json",
]
EXPECTED_ARTIFACTS = [
    "run.json",
    "stage-metrics.json",
    "market.json",
    "financial.json",
    "news.json",
    "account-before-verdict.json",
    "merged.json",
    "decision-brief.json",
    "verdict-first.json",
    "verdict-second.json",
    "account-before-order.json",
    "final-order-verdict.json",
    "execution.json",
]

SENSITIVE_KEY_RE = re.compile(
    r"^(access[_-]?token|refresh[_-]?token|app[_-]?secret|appsecret|"
    r"authorization|auth[_-]?header|my[_-]?htsid|hts[_-]?id|"
    r"cano|acnt[_-]?prdt[_-]?cd|account[_-]?number|account[_-]?product[_-]?code)$",
    re.IGNORECASE,
)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
BEARER_RE = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/-]{20,}", re.IGNORECASE)
FIN_NEWS_RE = re.compile(r"(financial|finance|news|disclosure|재무|뉴스|공시)", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def add_issue(
    issues: list[dict[str, Any]],
    code: str,
    message: str,
    artifact: str = "",
    path: str = "",
) -> None:
    issue: dict[str, Any] = {"code": code, "message": message}
    if artifact:
        issue["artifact"] = artifact
    if path:
        issue["path"] = path
    issues.append(issue)


def read_artifact(run_dir: Path, name: str, errors: list[dict[str, Any]]) -> Any:
    path = run_dir / name
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        add_issue(errors, "invalid_json", f"{exc.msg} at line {exc.lineno} column {exc.colno}", name)
    except OSError as exc:
        add_issue(errors, "read_failed", str(exc), name)
    return None


def walk_json(value: Any, path: str = "$"):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, key, child
            yield from walk_json(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            yield child_path, "", child
            yield from walk_json(child, child_path)


def scan_sensitive_values(name: str, data: Any, errors: list[dict[str, Any]]) -> None:
    serialized = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if JWT_RE.search(serialized):
        add_issue(errors, "sensitive_jwt", "raw JWT-like token found", name)
    if BEARER_RE.search(serialized):
        add_issue(errors, "sensitive_bearer", "raw bearer authorization value found", name)

    for path, key, value in walk_json(data):
        if key and SENSITIVE_KEY_RE.match(str(key)):
            add_issue(errors, "sensitive_key", f"sensitive key is persisted: {key}", name, path)
        if isinstance(value, str):
            if JWT_RE.search(value):
                add_issue(errors, "sensitive_jwt", "raw JWT-like token found", name, path)
            if BEARER_RE.search(value):
                add_issue(errors, "sensitive_bearer", "raw bearer authorization value found", name, path)


def symbol_id(symbol: Any) -> str:
    if isinstance(symbol, dict):
        for key in ("symbol_id", "pdno", "stock_code", "code"):
            value = symbol.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    elif isinstance(symbol, str):
        return symbol.strip()
    return ""


def symbol_name(symbol: Any) -> str:
    if not isinstance(symbol, dict):
        return ""
    for key in ("symbol_name", "name", "stock_name", "prdt_name"):
        value = symbol.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def top_level_symbols(data: Any) -> list[Any] | None:
    if isinstance(data, dict) and isinstance(data.get("symbols"), list):
        return data["symbols"]
    return None


def collect_expected_from_run(run_json: Any) -> tuple[list[str], int | None]:
    if not isinstance(run_json, dict):
        return [], None

    expected_count = None
    for key in ("expected_symbol_count", "symbol_count", "universe_count", "portfolio_symbol_count"):
        value = run_json.get(key)
        if isinstance(value, int):
            expected_count = value
            break

    ids: list[str] = []
    for key in ("symbol_universe", "portfolio_universe", "universe", "symbols", "requested_symbols", "configured_symbols"):
        value = run_json.get(key)
        if isinstance(value, dict):
            count = value.get("expected_symbol_count") or value.get("symbol_count")
            if expected_count is None and isinstance(count, int):
                expected_count = count
            value = value.get("symbols") or value.get("items")
        if not isinstance(value, list):
            continue
        for item in value:
            sid = symbol_id(item)
            if sid:
                ids.append(sid)
        if ids:
            break

    return dedupe(ids), expected_count


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def validate_common_envelope(name: str, data: Any, errors: list[dict[str, Any]]) -> None:
    if not isinstance(data, dict):
        add_issue(errors, "schema_type", "artifact must be a JSON object", name)
        return
    if data.get("schema_version") != "1":
        add_issue(errors, "schema_version", "schema_version must be '1'", name)
    if not str(data.get("run_id", "")).strip():
        add_issue(errors, "missing_run_id", "run_id is required", name)
    if not str(data.get("stage", "")).strip():
        add_issue(errors, "missing_stage", "stage is required", name)
    if data.get("status") not in ALLOWED_STATUS:
        add_issue(errors, "invalid_status", "status must be success, partial, or failed", name)
    if not isinstance(data.get("errors", []), list):
        add_issue(errors, "invalid_errors", "errors must be a list", name)


def validate_stage_metrics(data: Any, errors: list[dict[str, Any]]) -> None:
    name = "stage-metrics.json"
    validate_common_envelope(name, data, errors)
    if not isinstance(data, dict):
        return
    metrics = data.get("metrics")
    if not isinstance(metrics, list):
        add_issue(errors, "invalid_metrics", "metrics must be a list", name)
        return
    for index, metric in enumerate(metrics):
        if not isinstance(metric, dict):
            add_issue(errors, "invalid_metric", "metric entry must be an object", name, f"$.metrics[{index}]")
            continue
        expected = mandatory_model_effort(metric)
        if expected is None:
            continue
        expected_model, expected_effort = expected
        path = f"$.metrics[{index}]"
        for key in ("recommended_model", "recommended_effort", "actual_model", "actual_effort"):
            if not str(metric.get(key, "")).strip():
                add_issue(errors, "missing_stage_model_field", f"{key} is required", name, path)
        if metric.get("recommended_model") != expected_model:
            add_issue(errors, "recommended_model_mismatch", f"expected {expected_model}, got {metric.get('recommended_model')}", name, path)
        if metric.get("recommended_effort") != expected_effort:
            add_issue(errors, "recommended_effort_mismatch", f"expected {expected_effort}, got {metric.get('recommended_effort')}", name, path)
        if metric.get("actual_model") != expected_model:
            add_issue(errors, "actual_model_mismatch", f"expected {expected_model}, got {metric.get('actual_model')}", name, path)
        if metric.get("actual_effort") != expected_effort:
            add_issue(errors, "actual_effort_mismatch", f"expected {expected_effort}, got {metric.get('actual_effort')}", name, path)


def mandatory_model_effort(metric: dict[str, Any]) -> tuple[str, str] | None:
    stage = str(metric.get("stage", "")).strip().lower()
    role = str(metric.get("agent_role", "")).strip().lower()

    if role == "account" or stage in {"account-before-verdict", "account-before-order"}:
        return "gpt-5.3-codex-spark", "low"
    if role in {"market", "financial", "news"} or stage in {"market-collection", "financial-collection", "news-collection"}:
        return "gpt-5.3-codex-spark", "low"
    if role in {"analyst", "juror"} or stage == "first-verdict":
        return "gpt-5.5", "low"
    if role == "judge" or stage == "second-verdict":
        return "gpt-5.5", "high"
    if role == "final-risk" or stage == "final-risk-verdict":
        return "gpt-5.5", "high"
    if role == "main" or stage in {"initialize", "merge-and-brief", "execution", "report"}:
        return "gpt-5.5", "medium"
    return None


def validate_symbol_preservation(
    artifacts: dict[str, Any],
    expected_ids: list[str],
    expected_count: int | None,
    errors: list[dict[str, Any]],
) -> None:
    artifact_sets: dict[str, set[str]] = {}
    for name in DOMAIN_BRIEF_ARTIFACTS:
        data = artifacts.get(name)
        if data is None:
            continue
        symbols = top_level_symbols(data)
        if symbols is None:
            add_issue(errors, "missing_symbols", "top-level symbols list is required", name)
            continue
        ids: list[str] = []
        for index, item in enumerate(symbols):
            sid = symbol_id(item)
            if not sid:
                add_issue(errors, "missing_symbol_id", "symbol_id is required for every symbol", name, f"$.symbols[{index}]")
                continue
            if "," in sid:
                add_issue(errors, "comma_joined_symbol_id", "symbol_id must represent exactly one symbol", name, f"$.symbols[{index}].symbol_id")
            ids.append(sid)
        unique_ids = set(ids)
        artifact_sets[name] = unique_ids
        if len(unique_ids) != len(ids):
            add_issue(errors, "duplicate_symbol_id", "symbols contain duplicate symbol_id values", name)
        if expected_count is not None and len(ids) != expected_count:
            add_issue(errors, "symbol_count_mismatch", f"expected {expected_count} symbols, got {len(ids)}", name)
        if expected_ids:
            missing = [sid for sid in expected_ids if sid not in unique_ids]
            extra = sorted(unique_ids - set(expected_ids))
            if missing:
                add_issue(errors, "missing_universe_symbols", "missing symbols: " + ", ".join(missing[:20]), name)
            if extra:
                add_issue(errors, "extra_symbols", "unexpected symbols: " + ", ".join(extra[:20]), name)

    if not expected_ids and artifact_sets:
        largest = max(artifact_sets.values(), key=len)
        for name, ids in artifact_sets.items():
            missing = sorted(largest - ids)
            if missing:
                add_issue(errors, "missing_symbols_relative_to_artifacts", "missing symbols: " + ", ".join(missing[:20]), name)


def price_available(symbol: dict[str, Any]) -> bool:
    price = symbol.get("price")
    if not isinstance(price, dict):
        return False
    current = price.get("current_or_last")
    observed_at = str(price.get("observed_at", "")).strip()
    return current not in (None, "") and bool(observed_at)


def is_fin_news_only(values: list[Any]) -> bool:
    if not values:
        return False
    return all(FIN_NEWS_RE.search(str(value)) for value in values)


def validate_decision_brief(data: Any, errors: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> None:
    name = "decision-brief.json"
    if not isinstance(data, dict):
        return
    symbols = top_level_symbols(data)
    if symbols is None:
        return
    for index, item in enumerate(symbols):
        if not isinstance(item, dict):
            continue
        path = f"$.symbols[{index}]"
        sid = symbol_id(item)
        if item.get("eligible_for_verdict") is True:
            if not sid:
                add_issue(errors, "eligible_missing_symbol_id", "eligible symbol requires symbol_id", name, path)
            if not symbol_name(item):
                add_issue(errors, "eligible_missing_symbol_name", "eligible symbol requires symbol_name", name, path)
            if not price_available(item):
                add_issue(errors, "eligible_missing_price", "eligible symbol requires price.current_or_last and price.observed_at", name, path)
            financial_missing = not bool(item.get("financial_summary"))
            news_missing = not bool(item.get("news_summary"))
            if financial_missing or news_missing:
                if item.get("evidence_mode") != "price_only":
                    add_issue(
                        warnings,
                        "price_only_missing_evidence_mode",
                        "price-only eligible symbol should set evidence_mode='price_only'",
                        name,
                        path,
                    )
        elif item.get("eligible_for_verdict") is False and price_available(item):
            reasons = list(item.get("exclusion_reasons") or []) + list(item.get("required_missing") or [])
            if is_fin_news_only(reasons):
                add_issue(
                    errors,
                    "price_only_marked_ineligible",
                    "financial/news absence alone must not make a priced symbol ineligible",
                    name,
                    path,
                )


def validate_holiday_record(run_json: Any, decision_brief: Any, warnings: list[dict[str, Any]]) -> None:
    for name, data in (("run.json", run_json), ("decision-brief.json", decision_brief)):
        if not isinstance(data, dict):
            continue
        status = None
        for key in ("market_holiday", "holiday", "market_status", "korean_market_status"):
            value = data.get(key)
            if isinstance(value, dict) and value.get("status") in {"open", "closed", "unknown"}:
                status = value.get("status")
                break
            if isinstance(value, str) and value in {"open", "closed", "unknown"}:
                status = value
                break
        if status is None:
            add_issue(
                warnings,
                "missing_holiday_status",
                "open/closed/unknown Korean market status was not recorded",
                name,
            )


def validate_run(run_dir: Path, mark_run: bool = False) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    artifacts: dict[str, Any] = {}

    run_json = read_artifact(run_dir, "run.json", errors)
    if run_json is None:
        add_issue(errors, "missing_run_json", "run.json is required", "run.json")
    else:
        artifacts["run.json"] = run_json
        validate_common_envelope("run.json", run_json, errors)

    if isinstance(run_json, dict) and run_json.get("status") in {"success", "partial"}:
        for name in EXPECTED_ARTIFACTS:
            if not (run_dir / name).exists():
                add_issue(errors, "missing_artifact", f"{name} is required for {run_json.get('status')} runs", name)

    for name in EXPECTED_ARTIFACTS:
        if name == "run.json":
            continue
        data = read_artifact(run_dir, name, errors)
        if data is None:
            continue
        artifacts[name] = data
        if name == "stage-metrics.json":
            validate_stage_metrics(data, errors)
        else:
            validate_common_envelope(name, data, errors)
        scan_sensitive_values(name, data, errors)

    if run_json is not None:
        scan_sensitive_values("run.json", run_json, errors)

    expected_ids, expected_count = collect_expected_from_run(run_json)
    validate_symbol_preservation(artifacts, expected_ids, expected_count, errors)
    validate_decision_brief(artifacts.get("decision-brief.json"), errors, warnings)
    validate_holiday_record(run_json, artifacts.get("decision-brief.json"), warnings)

    status = "failed" if errors else "passed"
    result = {
        "schema_version": "1",
        "status": status,
        "run_dir": str(run_dir),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "generated_at": now_iso(),
    }
    if mark_run and isinstance(run_json, dict):
        mark_run_json(run_dir / "run.json", run_json, result)
    return result


def mark_run_json(path: Path, run_json: dict[str, Any], validation: dict[str, Any]) -> None:
    updated = dict(run_json)
    updated["validation_status"] = validation["status"]
    updated["validation"] = {
        "schema_version": validation["schema_version"],
        "status": validation["status"],
        "generated_at": validation["generated_at"],
        "error_count": validation["error_count"],
        "warning_count": validation["warning_count"],
        "errors": validation["errors"][:50],
        "warnings": validation["warnings"][:50],
    }
    if validation["status"] != "passed":
        updated["validation_failed"] = True
        updated["status_before_validation"] = run_json.get("status")
        updated["status"] = "failed"
        updated["status_reason"] = "validation_failed"
    else:
        updated["validation_failed"] = False

    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(updated, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def build_symbols(count: int, evidence_mode: str = "price_only") -> list[dict[str, Any]]:
    symbols = []
    for index in range(count):
        symbols.append(
            {
                "symbol_id": f"{index + 1:06d}",
                "symbol_name": f"종목{index + 1}",
                "product_type": "stock",
                "eligible_for_verdict": True,
                "evidence_mode": evidence_mode,
                "exclusion_reasons": [],
                "price": {
                    "current_or_last": 1000 + index,
                    "observed_at": "2026-06-05T15:30:00+09:00",
                },
                "market_signals": [],
                "financial_summary": {},
                "news_summary": [],
                "account_exposure": {},
                "required_missing": ["financial", "news"],
                "warnings": ["price_only"],
                "errors": [],
            }
        )
    return symbols


def common_artifact(run_id: str, stage: str, symbols: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": "2026-06-07T09:00:00+09:00",
        "generated_at": "2026-06-07T09:01:00+09:00",
        "stage": stage,
        "status": "success",
        "skipped": False,
        "skip_reason": "",
        "errors": [],
        "symbols": symbols,
    }


def stage_metrics(run_id: str, bad_model: bool = False) -> dict[str, Any]:
    metrics = [
        ("initialize", "main", "gpt-5.5", "medium"),
        ("account-before-verdict", "account", "gpt-5.3-codex-spark", "low"),
        ("market-collection", "market", "gpt-5.3-codex-spark", "low"),
        ("financial-collection", "financial", "gpt-5.3-codex-spark", "low"),
        ("news-collection", "news", "gpt-5.3-codex-spark", "low"),
        ("first-verdict", "analyst", "gpt-5.5", "low"),
        ("first-verdict", "juror", "gpt-5.5", "low"),
        ("second-verdict", "judge", "gpt-5.5", "high"),
        ("final-risk-verdict", "final-risk", "gpt-5.5", "high"),
        ("execution", "main", "gpt-5.5", "medium"),
        ("report", "main", "gpt-5.5", "medium"),
    ]
    entries = []
    for stage, role, model, effort in metrics:
        actual_model = "gpt-5.5" if bad_model and stage == "market-collection" else model
        actual_effort = "medium" if bad_model and stage == "market-collection" else effort
        entries.append(
            {
                "stage": stage,
                "agent_role": role,
                "recommended_model": model,
                "recommended_effort": effort,
                "actual_model": actual_model,
                "actual_effort": actual_effort,
                "started_at": "2026-06-07T09:00:00+09:00",
                "ended_at": "2026-06-07T09:00:01+09:00",
                "duration_ms": 1000,
                "status": "success",
                "token_usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
                "token_source": "unavailable",
                "token_unavailable_reason": "runtime did not expose per-stage token usage",
            }
        )
    return common_artifact(run_id, "stage-metrics", []) | {"metrics": entries}


def write_fixture(
    run_dir: Path,
    *,
    count: int = 29,
    comma_joined: bool = False,
    bad_model: bool = False,
    secret: bool = False,
) -> None:
    run_id = "self-test"
    symbols = build_symbols(count)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_json = common_artifact(run_id, "run", [])
    run_json.update(
        {
            "symbol_universe": [{"symbol_id": item["symbol_id"], "symbol_name": item["symbol_name"]} for item in symbols],
            "expected_symbol_count": count,
            "market_holiday": {"status": "closed", "target_date": "20260607"},
        }
    )
    write_json(run_dir / "run.json", run_json)
    write_json(run_dir / "stage-metrics.json", stage_metrics(run_id, bad_model=bad_model))

    artifact_symbols = list(symbols)
    if comma_joined:
        artifact_symbols = symbols[:18] + [
            {
                **symbols[18],
                "symbol_id": ",".join(item["symbol_id"] for item in symbols[18:]),
            }
        ]

    for name, stage in (
        ("market.json", "market-collection"),
        ("financial.json", "financial-collection"),
        ("news.json", "news-collection"),
        ("merged.json", "merge-and-brief"),
        ("decision-brief.json", "decision-brief"),
        ("account-before-verdict.json", "account-before-verdict"),
        ("verdict-first.json", "first-verdict"),
        ("verdict-second.json", "second-verdict"),
        ("account-before-order.json", "account-before-order"),
        ("final-order-verdict.json", "final-risk-verdict"),
        ("execution.json", "execution"),
    ):
        payload = common_artifact(run_id, stage, artifact_symbols if name in DOMAIN_BRIEF_ARTIFACTS else [])
        if name == "decision-brief.json":
            payload["market_holiday"] = {"status": "closed", "target_date": "20260607"}
        if secret and name == "market.json":
            payload["access_token"] = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJraXMifQ.secretpart1234567890"
        write_json(run_dir / name, payload)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def run_self_test() -> int:
    cases = [
        ("price-only 29 symbol run passes", {}, True),
        ("comma-joined symbol_id fails", {"comma_joined": True}, False),
        ("wrong collector model fails", {"bad_model": True}, False),
        ("raw access token/JWT fails", {"secret": True}, False),
    ]
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for index, (label, kwargs, should_pass) in enumerate(cases):
            run_dir = root / f"case-{index}"
            write_fixture(run_dir, **kwargs)
            result = validate_run(run_dir)
            passed = result["status"] == "passed"
            if passed != should_pass:
                failures.append({"case": label, "result": result})
    if failures:
        print(json.dumps({"status": "failed", "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"status": "passed", "case_count": len(cases)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate daily-trading run artifacts.")
    parser.add_argument("--run-dir", type=Path, help="reports/runs/<run_id> directory to validate.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--mark-run", action="store_true", help="Write validation status back into run.json.")
    parser.add_argument("--self-test", action="store_true", help="Run built-in fixture tests.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.self_test:
        return run_self_test()
    if args.run_dir is None:
        parser.error("--run-dir is required unless --self-test is used")
    result = validate_run(args.run_dir, mark_run=args.mark_run)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"status={result['status']} errors={result['error_count']} warnings={result['warning_count']}")
        for item in result["errors"][:20]:
            artifact = f"{item.get('artifact')}: " if item.get("artifact") else ""
            print(f"- {artifact}{item.get('code')}: {item.get('message')}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
