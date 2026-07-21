#!/usr/bin/env python3
"""CLI for deterministic market-news collection and status inspection."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


CODEX_EXEC_ROOT = Path(__file__).resolve().parents[3]
if str(CODEX_EXEC_ROOT) not in sys.path:
    sys.path.insert(0, str(CODEX_EXEC_ROOT))

from service.pipelines.market_news.collector import (  # noqa: E402
    collect_market_news,
    resolve_config_path,
    resolve_db_path,
)
from service.pipelines.market_news.storage import MarketNewsStore  # noqa: E402


def command_collect(args: argparse.Namespace) -> int:
    workspace = args.workspace_dir.expanduser().resolve()
    result = collect_market_news(
        config_path=resolve_config_path(args.config),
        db_path=resolve_db_path(workspace, args.db_path),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"success", "skipped_locked"} else 1


def command_status(args: argparse.Namespace) -> int:
    workspace = args.workspace_dir.expanduser().resolve()
    path = resolve_db_path(workspace, args.db_path)
    if not path.exists():
        print(json.dumps({"status": "missing", "db_path": str(path), "sources": {}}, ensure_ascii=False))
        return 1
    payload = {
        "status": "available",
        "db_path": str(path),
        "sources": MarketNewsStore(path, read_only=True).latest_run_statuses(),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def command_self_test() -> int:
    from service.pipelines.market_news.tests.test_pipeline import command_self_test as run_external_self_test

    return run_external_self_test()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect market-wide news without Codex/LLM.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect")
    collect.add_argument("--workspace-dir", type=Path, default=Path("."))
    collect.add_argument("--config", default="")
    collect.add_argument("--db-path", default="")

    status = subparsers.add_parser("status")
    status.add_argument("--workspace-dir", type=Path, default=Path("."))
    status.add_argument("--db-path", default="")

    subparsers.add_parser("self-test")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "collect":
        return command_collect(args)
    if args.command == "status":
        return command_status(args)
    if args.command == "self-test":
        return command_self_test()
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
