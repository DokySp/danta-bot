#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PIPELINES_DIR = Path(__file__).resolve().parent.parent
if str(PIPELINES_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINES_DIR))

from market_index_snapshot.collector import (  # noqa: E402
    DEFAULT_INDEXES,
    collect_market_index_snapshot,
    render_markdown,
    write_json,
)


def command_collect(args: argparse.Namespace) -> int:
    payload = collect_market_index_snapshot(
        run_id=args.run_id,
        started_at=args.started_at,
        indexes=tuple(args.indexes or DEFAULT_INDEXES),
    )
    write_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


def command_render(args: argparse.Namespace) -> int:
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    text = render_markdown(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


def command_self_test() -> int:
    """Run the extracted test suite through the legacy CLI contract."""
    from market_index_snapshot.tests.test_cli import (
        command_self_test as run_external_self_test,
    )

    return run_external_self_test()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect compact market index snapshot for codex-exec pipelines.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="Collect market-index-snapshot.json.")
    collect.add_argument("--run-id", required=True)
    collect.add_argument("--started-at", required=True)
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument("--indexes", nargs="*", default=list(DEFAULT_INDEXES))

    render = subparsers.add_parser("render", help="Render market-index-snapshot.json as Markdown.")
    render.add_argument("--input", type=Path, required=True)
    render.add_argument("--output", type=Path)

    subparsers.add_parser("self-test", help="Run offline market index snapshot self-tests.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "collect":
        return command_collect(args)
    if args.command == "render":
        return command_render(args)
    if args.command == "self-test":
        return command_self_test()
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
