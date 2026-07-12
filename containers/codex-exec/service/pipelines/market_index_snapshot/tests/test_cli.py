"""Tests for market index snapshot collection and rendering."""

from __future__ import annotations

import unittest

from ..cli import (
    DEFAULT_INDEXES,
    collect_market_index_snapshot,
    render_markdown,
)


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


class MarketIndexSnapshotSelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        self.assertEqual(command_self_test(), 0)
