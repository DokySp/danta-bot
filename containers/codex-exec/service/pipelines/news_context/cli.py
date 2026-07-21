#!/usr/bin/env python3
"""CLI for read-only news-context construction."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


CODEX_EXEC_ROOT = Path(__file__).resolve().parents[3]
if str(CODEX_EXEC_ROOT) not in sys.path:
    sys.path.insert(0, str(CODEX_EXEC_ROOT))

from service.pipelines.market_news.collector import load_config, resolve_config_path, resolve_db_path  # noqa: E402
from service.pipelines.news_context.builder import build_news_context, write_json  # noqa: E402


def command_build(args: argparse.Namespace) -> int:
    workspace = args.workspace_dir.expanduser().resolve()
    config_path = resolve_config_path(args.config)
    config = load_config(config_path) if config_path.exists() else {}
    symbol_path = Path(args.symbol_news_cache_path).expanduser() if args.symbol_news_cache_path else None
    if symbol_path is not None and not symbol_path.is_absolute():
        symbol_path = workspace / symbol_path
    payload = build_news_context(
        workspace_dir=workspace,
        current_run_id=args.run_id,
        current_started_at=args.started_at,
        symbol_news_cache_path=symbol_path,
        market_news_db_path=resolve_db_path(workspace, args.market_news_db_path),
        max_market_items=args.max_market_items or int(config.get("context_max_items", 30)),
        max_lookback_hours=args.max_lookback_hours or int(config.get("max_lookback_hours", 72)),
    )
    write_json(args.output, payload)
    print(json.dumps({"status": payload["status"], "path": str(args.output)}, ensure_ascii=False))
    return 0


def command_self_test() -> int:
    from service.pipelines.news_context.tests.test_builder import command_self_test as run_external_self_test

    return run_external_self_test()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build deduplicated symbol/market news context.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--workspace-dir", type=Path, required=True)
    build.add_argument("--run-id", required=True)
    build.add_argument("--started-at", required=True)
    build.add_argument("--config", default="")
    build.add_argument("--symbol-news-cache-path", default="")
    build.add_argument("--market-news-db-path", default="")
    build.add_argument("--max-market-items", type=int, default=0)
    build.add_argument("--max-lookback-hours", type=int, default=0)
    build.add_argument("--output", type=Path, required=True)
    subparsers.add_parser("self-test")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "build":
        return command_build(args)
    if args.command == "self-test":
        return command_self_test()
    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
