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
    from market_index_snapshot import collector

    original_google = collector.fetch_google_finance_index
    original_kis = collector.fetch_kis_index

    def fake_google(symbol: str):
        return collector.IndexQuote(
            symbol=symbol,
            name=collector.DISPLAY_NAMES[symbol],
            source="google_finance",
            status="success",
            value=100.0,
            change_percent=0.25,
            observed_at="2026-06-28T00:00:00+00:00",
            market_status="latest_available",
        )

    def fake_kis(symbol: str):
        return collector.IndexQuote(
            symbol=symbol,
            name=collector.DISPLAY_NAMES[symbol],
            source="kis_domestic_index",
            status="success",
            value=3000.0,
            change_percent=-0.2,
            observed_at="2026-06-28T09:00:00+09:00",
            market_status="장마감",
        )

    try:
        collector.fetch_google_finance_index = fake_google
        collector.fetch_kis_index = fake_kis
        payload = collect_market_index_snapshot(run_id="self-test", started_at="2026-06-28T09:00:00+09:00")
    finally:
        collector.fetch_google_finance_index = original_google
        collector.fetch_kis_index = original_kis
    if payload.get("status") != "success":
        raise AssertionError(f"unexpected status: {payload}")
    indexes = payload.get("indexes")
    if not isinstance(indexes, list) or len(indexes) != 5:
        raise AssertionError(f"expected five indexes: {payload}")
    symbols = {item.get("symbol") for item in indexes if isinstance(item, dict)}
    if symbols != set(DEFAULT_INDEXES):
        raise AssertionError(f"unexpected symbols: {symbols}")
    rendered = render_markdown(payload)
    for expected in ("S&P 500:", "Nasdaq:", "Dow:", "코스피:", "코스닥:"):
        if expected not in rendered:
            raise AssertionError(f"rendered output omitted {expected}: {rendered}")
    print("market_index_snapshot self-test passed")
    return 0


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
