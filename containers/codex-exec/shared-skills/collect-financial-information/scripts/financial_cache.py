#!/usr/bin/env python3
"""Validate and persist collect-financial-information cache payloads."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATE_RE = re.compile(r"^[0-9]{4}-?[0-9]{2}-?[0-9]{2}$")
VALID_STATUS = {"success", "partial"}


def normalize_date(value: str) -> str:
    raw = value.strip()
    if not DATE_RE.match(raw):
        raise SystemExit(f"date must be YYYY-MM-DD or YYYYMMDD: {value!r}")
    compact = raw.replace("-", "")
    return f"{compact[0:4]}-{compact[4:6]}-{compact[6:8]}"


def cache_dir() -> Path:
    configured = os.environ.get("FINANCIAL_CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "codex" / "collect-financial-information"


def cache_path(date: str) -> Path:
    return cache_dir() / f"{normalize_date(date)}.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json_stdin_or_file(path: str | None) -> Any:
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return json.load(sys.stdin)


def parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[\s,]+", raw.strip())
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not part or part in seen:
            continue
        seen.add(part)
        result.append(part)
    return result


def load_symbols(path: str | None) -> list[str]:
    if not path:
        return []
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return parse_symbols(text)
    if isinstance(data, list):
        return [str(item).strip() for item in data if str(item).strip()]
    if isinstance(data, dict):
        for key in ("symbol_ids", "symbols", "portfolio_symbols", "complete_symbol_list"):
            value = data.get(key)
            if isinstance(value, list):
                ids = []
                for item in value:
                    if isinstance(item, dict):
                        item = item.get("symbol_id") or item.get("pdno") or item.get("code")
                    if str(item).strip():
                        ids.append(str(item).strip())
                return ids
    return []


def symbol_id(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("symbol_id", "pdno", "stock_code", "code"):
            value = item.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    elif item is not None and str(item).strip():
        return str(item).strip()
    return ""


def extract_payload(value: Any) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not isinstance(value, dict):
        return None, {}
    payload = value.get("payload")
    if isinstance(payload, dict):
        return payload, value
    return value, {}


def payload_trading_date(payload: dict[str, Any], wrapper: dict[str, Any]) -> str:
    cache = payload.get("cache") if isinstance(payload.get("cache"), dict) else {}
    update = payload.get("cache_update") if isinstance(payload.get("cache_update"), dict) else {}
    wrapper_update = wrapper.get("cache_update") if isinstance(wrapper.get("cache_update"), dict) else {}
    for candidate in (
        cache.get("trading_date"),
        update.get("trading_date"),
        wrapper.get("trading_date"),
        wrapper_update.get("trading_date"),
    ):
        if candidate:
            return normalize_date(str(candidate))
    return ""


def validate_payload(value: Any, date: str, expected_symbols: list[str] | None = None) -> dict[str, Any]:
    target_date = normalize_date(date)
    expected = expected_symbols or []
    payload, wrapper = extract_payload(value)
    errors: list[dict[str, Any]] = []

    if payload is None:
        errors.append({"code": "payload_type", "message": "cache payload must be a JSON object"})
        return validation_result(False, target_date, "", [], expected, errors)

    if payload.get("schema_version") != "1":
        errors.append({"code": "schema_version", "message": "schema_version must be '1'"})
    if payload.get("stage") != "financial-collection":
        errors.append({"code": "stage", "message": "stage must be financial-collection"})
    if payload.get("domain") != "financial":
        errors.append({"code": "domain", "message": "domain must be financial"})
    if payload.get("status") not in VALID_STATUS:
        errors.append({"code": "status", "message": "status must be success or partial"})

    trading_date = payload_trading_date(payload, wrapper)
    if trading_date != target_date:
        errors.append(
            {
                "code": "trading_date",
                "message": f"trading_date must be {target_date}, got {trading_date or 'missing'}",
            }
        )

    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        errors.append({"code": "symbols", "message": "symbols must be a non-empty list"})
        ids: list[str] = []
    else:
        ids = []
        for index, item in enumerate(symbols):
            sid = symbol_id(item)
            if not sid:
                errors.append({"code": "missing_symbol_id", "message": f"symbol_id is required at symbols[{index}]"})
                continue
            if "," in sid:
                errors.append({"code": "comma_joined_symbol_id", "message": f"symbol_id must represent one symbol at symbols[{index}]"})
            ids.append(sid)

    if expected:
        actual_set = set(ids)
        missing = [sid for sid in expected if sid not in actual_set]
        if missing:
            errors.append({"code": "missing_symbols", "message": "missing symbols: " + ", ".join(missing[:20])})

    return validation_result(not errors, target_date, payload.get("run_id", ""), ids, expected, errors)


def validation_result(
    valid: bool,
    target_date: str,
    run_id: str,
    symbols: list[str],
    expected_symbols: list[str],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "valid": valid,
        "target_date": target_date,
        "run_id": run_id,
        "symbol_count": len(symbols),
        "expected_symbol_count": len(expected_symbols) if expected_symbols else None,
        "matched_symbol_count": len([sid for sid in expected_symbols if sid in set(symbols)]) if expected_symbols else None,
        "errors": errors,
    }


def cached_payload_for_request(payload: dict[str, Any], date: str, requested_symbols: list[str]) -> dict[str, Any]:
    result = deepcopy(payload)
    target_date = normalize_date(date)
    if requested_symbols and isinstance(result.get("symbols"), list):
        wanted = set(requested_symbols)
        result["symbols"] = [item for item in result["symbols"] if symbol_id(item) in wanted]
    cache = result.get("cache") if isinstance(result.get("cache"), dict) else {}
    cache.update(
        {
            "status": "hit",
            "trading_date": target_date,
            "cache_path": str(cache_path(target_date)),
            "used_external_calls": False,
            "reason": "cache hit",
        }
    )
    result["cache"] = cache
    return result


def cache_wrapper(payload: dict[str, Any], date: str) -> dict[str, Any]:
    target_date = normalize_date(date)
    update = payload.get("cache_update") if isinstance(payload.get("cache_update"), dict) else {}
    return {
        "schema_version": "1",
        "trading_date": target_date,
        "source_run_id": payload.get("run_id", ""),
        "cached_at": now_iso(),
        "cache_update": {
            "schema_version": "1",
            "status": "ready",
            "trading_date": target_date,
            "run_id": payload.get("run_id", ""),
            "collected_at": payload.get("generated_at", ""),
            "cache_path": str(cache_path(target_date)),
            "symbols_count": len(payload.get("symbols", [])),
            **{key: value for key, value in update.items() if key not in {"payload", "raw_response"}},
        },
        "payload": payload,
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        tmp = Path(handle.name)
    tmp.replace(path)


def command_get(args: argparse.Namespace) -> int:
    date = normalize_date(args.date)
    expected = parse_symbols(args.symbols) + load_symbols(args.symbols_file)
    path = cache_path(date)
    if not path.exists():
        print(json.dumps({"cache_hit": False, "target_date": date, "cache_path": str(path), "reason": "missing"}, ensure_ascii=False, sort_keys=True))
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - cache validation reports corrupt cache
        print(json.dumps({"cache_hit": False, "target_date": date, "cache_path": str(path), "reason": "read_failed", "errors": [{"code": type(exc).__name__, "message": str(exc)}]}, ensure_ascii=False, sort_keys=True))
        return 1
    validation = validate_payload(data, date, expected)
    if not validation["valid"]:
        print(json.dumps({"cache_hit": False, "target_date": date, "cache_path": str(path), "reason": "invalid", "validation": validation}, ensure_ascii=False, sort_keys=True))
        return 1
    payload, _ = extract_payload(data)
    payload = cached_payload_for_request(payload or {}, date, expected)
    print(json.dumps({"cache_hit": True, "target_date": date, "cache_path": str(path), "validation": validation, "payload": payload}, ensure_ascii=False, sort_keys=True))
    return 0


def command_put(args: argparse.Namespace) -> int:
    date = normalize_date(args.date)
    expected = parse_symbols(args.symbols) + load_symbols(args.symbols_file)
    raw = read_json_stdin_or_file(args.response_file)
    payload, _ = extract_payload(raw)
    validation = validate_payload(raw, date, expected)
    path = cache_path(date)
    if not validation["valid"] or payload is None:
        print(json.dumps({"stored": False, "target_date": date, "cache_path": str(path), "reason": "invalid", "validation": validation}, ensure_ascii=False, sort_keys=True))
        return 1
    wrapped = cache_wrapper(payload, date)
    write_json_atomic(path, wrapped)
    print(json.dumps({"stored": True, "target_date": date, "cache_path": str(path), "validation": validation}, ensure_ascii=False, sort_keys=True))
    return 0


def command_eval(args: argparse.Namespace) -> int:
    date = normalize_date(args.date)
    expected = parse_symbols(args.symbols) + load_symbols(args.symbols_file)
    raw = read_json_stdin_or_file(args.response_file)
    validation = validate_payload(raw, date, expected)
    print(json.dumps(validation, ensure_ascii=False, sort_keys=True))
    return 0 if validation["valid"] else 1


def command_self_test(_: argparse.Namespace) -> int:
    payload = {
        "schema_version": "1",
        "run_id": "self-test",
        "started_at": "2026-06-08T09:00:00+09:00",
        "generated_at": "2026-06-08T09:00:01+09:00",
        "stage": "financial-collection",
        "domain": "financial",
        "status": "partial",
        "skipped": False,
        "skip_reason": "",
        "cache": {"status": "miss", "trading_date": "2026-06-08", "cache_path": "~/.cache/codex/collect-financial-information/2026-06-08.json", "used_external_calls": True, "reason": ""},
        "attempts": [],
        "errors": [],
        "symbols": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "errors": []},
            {"symbol_id": "000660", "symbol_name": "SK하이닉스", "errors": []},
        ],
        "cache_update": {},
    }
    valid = validate_payload(payload, "2026-06-08", ["005930"])
    subset = cached_payload_for_request(payload, "2026-06-08", ["005930"])
    invalid = validate_payload({**payload, "status": "failed"}, "2026-06-08", ["005930"])
    missing = validate_payload(payload, "2026-06-08", ["035420"])
    if not valid["valid"] or len(subset.get("symbols", [])) != 1 or invalid["valid"] or missing["valid"]:
        print(json.dumps({"status": "failed", "valid": valid, "subset": subset, "invalid": invalid, "missing": missing}, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps({"status": "passed"}, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and persist financial collection cache files.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("get", "put", "eval"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--date", required=True, help="Trading date in YYYY-MM-DD or YYYYMMDD.")
        sub.add_argument("--symbols", help="Comma or whitespace separated expected symbol ids.")
        sub.add_argument("--symbols-file", help="Optional file containing expected symbols.")
        if name in {"put", "eval"}:
            sub.add_argument("--response-file", help="Financial collection JSON file. Defaults to stdin.")
        sub.set_defaults(func={"get": command_get, "put": command_put, "eval": command_eval}[name])

    self_test = subparsers.add_parser("self-test")
    self_test.set_defaults(func=command_self_test)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
