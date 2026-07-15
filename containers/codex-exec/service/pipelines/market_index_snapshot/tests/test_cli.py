"""Tests for market index snapshot collection and rendering."""

from __future__ import annotations

import unittest

from ..cli import (
    DEFAULT_INDEXES,
    collect_market_index_snapshot,
    render_markdown,
)
from ..collector import parse_google_finance_quote


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

    def test_google_quote_uses_symbol_scoped_structured_data(self) -> None:
        body = """
        <style>.chart { transform: translateY(-50%); }</style>
        <script>
        AF_initDataCallback({data:[["/m/016yss",[".INX","INDEXSP"],
        "S\\u0026P 500",1,null,[7548.25,4.6601562,0.061778475,2,2,2],null,7543.59]]});
        </script>
        """

        self.assertEqual(parse_google_finance_quote(body, "SP500"), (7548.25, 0.061778475))

    def test_google_quote_explicit_attributes_ignore_unrelated_percentages(self) -> None:
        body = """
        <style>.chart { transform: translateY(-50%); }</style>
        <div data-exchange="INDEXSP" data-last-price="7543.59"
             data-last-price-change-percent="+0.38%"></div>
        <div data-exchange="INDEXNASDAQ" data-last-price="26281.50"
             data-last-price-change-percent="+0.67%"></div>
        """

        self.assertEqual(parse_google_finance_quote(body, "NASDAQ"), (26281.5, 0.67))

    def test_google_quote_does_not_fall_back_to_unscoped_percentage(self) -> None:
        body = """
        <style>.chart { transform: translateY(-50%); }</style>
        <div data-last-price="52508.27"></div>
        <div>(-12.34%)</div>
        """

        self.assertEqual(parse_google_finance_quote(body, "DOW"), (52508.27, None))
