#!/usr/bin/env python3
"""Toggle enabled flags for daily-N schedule blocks without rewriting YAML."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


DAILY_ID_RE = re.compile(r"^daily-[0-9]+$")
SCHEDULE_ID_RE = re.compile(r"^(\s*)-\s+id:\s*['\"]?([A-Za-z0-9_.-]+)['\"]?\s*$")
ENABLED_RE = re.compile(r"^(\s*)enabled:\s*(true|false|True|False|yes|no|on|off|1|0)\s*(#.*)?$")


@dataclass(frozen=True)
class ScheduleBlock:
    schedule_id: str
    start: int
    end: int
    indent: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Toggle daily-N schedules in schedules.yaml.")
    parser.add_argument(
        "--file",
        default=os.environ.get("SCHEDULE_FILE", "/app/config/schedules.yaml"),
        help="schedules.yaml path; defaults to SCHEDULE_FILE or /app/config/schedules.yaml",
    )
    parser.add_argument("--state", required=True, choices=("on", "off", "enable", "disable", "true", "false"))
    parser.add_argument("--ids", help="Comma-separated daily IDs, for example daily-1,daily-3")
    parser.add_argument("--numbers", help="Comma-separated daily numbers, for example 1,3")
    parser.add_argument("--dry-run", action="store_true", help="Report intended changes without writing the file")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser.parse_args()


def normalize_state(raw: str) -> bool:
    return raw in {"on", "enable", "true"}


def target_ids(args: argparse.Namespace) -> set[str] | None:
    ids: set[str] = set()
    if args.ids:
        ids.update(part.strip() for part in args.ids.split(",") if part.strip())
    if args.numbers:
        for part in args.numbers.split(","):
            text = part.strip()
            if text:
                ids.add(f"daily-{text}")
    for schedule_id in ids:
        if not DAILY_ID_RE.fullmatch(schedule_id):
            raise ValueError(f"target is not a daily schedule id: {schedule_id}")
    return ids or None


def find_blocks(lines: list[str]) -> list[ScheduleBlock]:
    blocks: list[ScheduleBlock] = []
    current: tuple[str, int, str] | None = None
    for index, line in enumerate(lines):
        match = SCHEDULE_ID_RE.match(line.rstrip("\n"))
        if not match:
            continue
        if current is not None:
            schedule_id, start, indent = current
            blocks.append(ScheduleBlock(schedule_id, start, index, indent))
        current = (match.group(2), index, match.group(1))
    if current is not None:
        schedule_id, start, indent = current
        blocks.append(ScheduleBlock(schedule_id, start, len(lines), indent))
    return blocks


def current_enabled(lines: list[str], block: ScheduleBlock) -> tuple[int | None, bool | None]:
    for index in range(block.start + 1, block.end):
        match = ENABLED_RE.match(lines[index].rstrip("\n"))
        if not match:
            continue
        value = match.group(2).lower()
        return index, value in {"true", "yes", "on", "1"}
    return None, None


def enabled_line(indent: str, enabled: bool, comment: str = "") -> str:
    child_indent = indent + "  "
    value = "true" if enabled else "false"
    suffix = f" {comment}" if comment else ""
    return f"{child_indent}enabled: {value}{suffix}\n"


def toggle(lines: list[str], wanted: bool, selected_ids: set[str] | None) -> dict[str, object]:
    blocks = [block for block in find_blocks(lines) if DAILY_ID_RE.fullmatch(block.schedule_id)]
    if selected_ids is not None:
        blocks = [block for block in blocks if block.schedule_id in selected_ids]

    found_ids = {block.schedule_id for block in blocks}
    missing_ids = sorted(selected_ids - found_ids) if selected_ids is not None else []
    changed: list[str] = []
    unchanged: list[str] = []
    offset = 0
    for block in blocks:
        shifted = ScheduleBlock(block.schedule_id, block.start + offset, block.end + offset, block.indent)
        enabled_index, previous = current_enabled(lines, shifted)
        if previous == wanted:
            unchanged.append(block.schedule_id)
            continue
        if enabled_index is None:
            insert_at = shifted.start + 1
            lines.insert(insert_at, enabled_line(shifted.indent, wanted))
            offset += 1
        else:
            comment_match = ENABLED_RE.match(lines[enabled_index].rstrip("\n"))
            comment = comment_match.group(3) if comment_match and comment_match.group(3) else ""
            lines[enabled_index] = enabled_line(shifted.indent, wanted, comment)
        changed.append(block.schedule_id)

    return {
        "changed": sorted(changed),
        "unchanged": sorted(unchanged),
        "missing": missing_ids,
        "state": "on" if wanted else "off",
    }


def main() -> int:
    args = parse_args()
    try:
        schedule_file = Path(args.file)
        selected_ids = target_ids(args)
        wanted = normalize_state(args.state)
        lines = schedule_file.read_text().splitlines(keepends=True)
        result = toggle(lines, wanted, selected_ids)
        result["file"] = str(schedule_file)
        result["dry_run"] = args.dry_run
        if not result["changed"] and not result["unchanged"] and not result["missing"]:
            result["error"] = "no matching daily schedules found"
            print_output(result, args.json)
            return 2
        if not args.dry_run:
            write_text_in_place(schedule_file, "".join(lines))
        print_output(result, args.json)
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        result = {"error": str(exc)}
        print_output(result, args.json)
        return 1


def write_text_in_place(path: Path, text: str) -> None:
    mode = path.stat().st_mode
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


def print_output(result: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
    print(f"file: {result.get('file', '')}")
    print(f"state: {result.get('state', '')}")
    print(f"dry_run: {result.get('dry_run', False)}")
    print(f"changed: {', '.join(result.get('changed', [])) or '-'}")
    print(f"unchanged: {', '.join(result.get('unchanged', [])) or '-'}")
    print(f"missing: {', '.join(result.get('missing', [])) or '-'}")


if __name__ == "__main__":
    raise SystemExit(main())
