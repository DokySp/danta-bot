#!/usr/bin/env python3
"""Cache and normalize KIS chk_holiday responses."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATE_RE = re.compile(r"^[0-9]{8}$")


def find_repo_root(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def memory_root() -> Path:
    configured = os.environ.get("DAILY_TRADING_MEMORY_DIR")
    if configured:
        return Path(configured).expanduser()
    repo_root = find_repo_root()
    if repo_root is not None:
        return repo_root / "memory"
    return Path.cwd() / "memory"


def cache_dir() -> Path:
    configured = os.environ.get("CHECK_HOLIDAY_CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    return memory_root() / "check-holiday"


def cache_path(date: str) -> Path:
    return cache_dir() / f"holiday-{date}.json"


def validate_date(date: str) -> str:
    if not DATE_RE.match(date):
        raise SystemExit(f"date must be YYYYMMDD: {date!r}")
    return date


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json_stdin_or_file(path: str | None) -> Any:
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return json.load(sys.stdin)


def iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def find_bass_dt_row(response: Any, date: str) -> dict[str, Any] | None:
    for item in iter_dicts(response):
        if str(item.get("bass_dt", "")) == date:
            return item
    return None


def response_ok(response: Any) -> bool:
    if not isinstance(response, dict):
        return True
    if response.get("ok") is False:
        return False
    data = response.get("data")
    if isinstance(data, dict) and str(data.get("rt_cd", "0")) not in ("0", ""):
        return False
    return True


def normalize(response: Any, date: str, source: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "cache_hit": source == "cache",
        "source": source,
        "target_date": date,
        "status": "unknown",
        "is_open": None,
        "is_closed": None,
        "bass_dt": None,
        "opnd_yn": None,
        "row": None,
        "reason": None,
    }

    if not response_ok(response):
        result["reason"] = "api_failure"
        result["raw_response"] = response
        return result

    row = find_bass_dt_row(response, date)
    if row is None:
        result["reason"] = "missing_target_date_row"
        result["raw_response"] = response
        return result

    opnd_yn = row.get("opnd_yn")
    result.update(
        {
            "bass_dt": str(row.get("bass_dt", "")),
            "opnd_yn": opnd_yn,
            "row": row,
        }
    )

    if opnd_yn is None:
        result["reason"] = "missing_opnd_yn"
    elif str(opnd_yn) == "Y":
        result.update({"status": "open", "is_open": True, "is_closed": False, "reason": "opnd_yn_y"})
    else:
        result.update({"status": "closed", "is_open": False, "is_closed": True, "reason": "opnd_yn_not_y"})

    return result


def command_get(args: argparse.Namespace) -> int:
    date = validate_date(args.date)
    path = cache_path(date)
    if not path.exists():
        print(json.dumps({"cache_hit": False, "target_date": date, "cache_path": str(path)}, ensure_ascii=False))
        return 1

    with open(path, "r", encoding="utf-8") as handle:
        cached = json.load(handle)

    if cached.get("target_date") != date:
        print(json.dumps({"cache_hit": False, "target_date": date, "cache_path": str(path), "reason": "date_mismatch"}, ensure_ascii=False))
        return 1

    cached["cache_hit"] = True
    cached["source"] = "cache"
    cached["cache_path"] = str(path)
    print(json.dumps(cached, ensure_ascii=False, sort_keys=True))
    return 0


def command_put(args: argparse.Namespace) -> int:
    date = validate_date(args.date)
    response = read_json_stdin_or_file(args.response_file)
    result = normalize(response, date, "kis_mcp")
    result.update({"cached_at": now_iso(), "cache_path": str(cache_path(date)), "raw_response": response})
    path = cache_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def command_eval(args: argparse.Namespace) -> int:
    date = validate_date(args.date)
    response = read_json_stdin_or_file(args.response_file)
    result = normalize(response, date, "kis_mcp")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cache and normalize KIS chk_holiday responses.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    get_parser = subparsers.add_parser("get", help="Read the date cache.")
    get_parser.add_argument("--date", required=True, help="Target date in YYYYMMDD.")
    get_parser.set_defaults(func=command_get)

    put_parser = subparsers.add_parser("put", help="Normalize and cache a KIS response from stdin or a file.")
    put_parser.add_argument("--date", required=True, help="Target date in YYYYMMDD.")
    put_parser.add_argument("--response-file", help="Path to a raw KIS response JSON file. Defaults to stdin.")
    put_parser.set_defaults(func=command_put)

    eval_parser = subparsers.add_parser("eval", help="Normalize a KIS response without writing cache.")
    eval_parser.add_argument("--date", required=True, help="Target date in YYYYMMDD.")
    eval_parser.add_argument("--response-file", help="Path to a raw KIS response JSON file. Defaults to stdin.")
    eval_parser.set_defaults(func=command_eval)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
