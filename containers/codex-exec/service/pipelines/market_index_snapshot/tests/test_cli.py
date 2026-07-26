"""Tests for market index snapshot collection and rendering.

`command_self_test` is the compatibility body invoked by the production
`cli.py self-test` command (via a lazy import) and must keep its exact
CLI contract: same call syntax, a "self-test passed" stdout line, and a
`0`/`AssertionError` result.

The behavioral checks themselves live in exactly one place each, as the
module-level `scenario_*`/`check_*` helpers below. `command_self_test`
and the granular `MarketIndexSnapshotCollectionTest` methods both call
those same helpers instead of each re-implementing the assertions, so
there is one source of truth per check and no duplicated behavior.

This is the pattern later umbrella-suite refactors should follow: pull
each logical block out of the large `test_self_test_suite` bodies into
a named `scenario_*` (setup + act) or `check_*` (bare-assert, reusable
from both a plain function and a `TestCase` method) helper, then have
the legacy `command_self_test` wrapper and new granular `TestCase`
methods both call those helpers rather than keeping two copies of the
same checks.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from ..cli import (
    DEFAULT_INDEXES,
    collect_market_index_snapshot,
    render_markdown,
)
from ..collector import DISPLAY_NAMES, IndexQuote, parse_google_finance_quote


def fake_google_quote(symbol: str) -> IndexQuote:
    return IndexQuote(
        symbol=symbol,
        name=DISPLAY_NAMES[symbol],
        source="google_finance",
        status="success",
        value=100.0,
        change_percent=0.25,
        observed_at="2026-06-28T00:00:00+00:00",
        market_status="latest_available",
    )


def fake_kis_quote(symbol: str) -> IndexQuote:
    return IndexQuote(
        symbol=symbol,
        name=DISPLAY_NAMES[symbol],
        source="kis_domestic_index",
        status="success",
        value=3000.0,
        change_percent=-0.2,
        observed_at="2026-06-28T09:00:00+09:00",
        market_status="장마감",
    )


def scenario_default_collection() -> dict:
    """Collect a snapshot with both external quote sources faked offline."""
    with patch("market_index_snapshot.collector.fetch_google_finance_index", side_effect=fake_google_quote), patch(
        "market_index_snapshot.collector.fetch_kis_index", side_effect=fake_kis_quote
    ):
        return collect_market_index_snapshot(run_id="self-test", started_at="2026-06-28T09:00:00+09:00")


def check_success_status(payload: dict) -> None:
    if payload.get("status") != "success":
        raise AssertionError(f"unexpected status: {payload}")


def check_five_default_indexes(payload: dict) -> None:
    indexes = payload.get("indexes")
    if not isinstance(indexes, list) or len(indexes) != 5:
        raise AssertionError(f"expected five indexes: {payload}")
    symbols = {item.get("symbol") for item in indexes if isinstance(item, dict)}
    if symbols != set(DEFAULT_INDEXES):
        raise AssertionError(f"unexpected symbols: {symbols}")


def check_rendered_markdown_includes_display_names(payload: dict) -> None:
    rendered = render_markdown(payload)
    for expected in ("S&P 500:", "Nasdaq:", "Dow:", "코스피:", "코스닥:"):
        if expected not in rendered:
            raise AssertionError(f"rendered output omitted {expected}: {rendered}")


def command_self_test() -> int:
    payload = scenario_default_collection()
    check_success_status(payload)
    check_five_default_indexes(payload)
    check_rendered_markdown_includes_display_names(payload)
    print("market_index_snapshot self-test passed")
    return 0


class MarketIndexSnapshotSelfTest(unittest.TestCase):
    def test_self_test_suite_calls_every_check_and_reports_success(self) -> None:
        """Wrapper-orchestration check only: the real scenario/checks are
        exercised for real by MarketIndexSnapshotCollectionTest below, so
        this mocks all three helpers instead of re-running the scenario."""
        with patch(f"{__name__}.scenario_default_collection", return_value={"fake": "payload"}) as scenario, patch(
            f"{__name__}.check_success_status"
        ) as check_status, patch(f"{__name__}.check_five_default_indexes") as check_indexes, patch(
            f"{__name__}.check_rendered_markdown_includes_display_names"
        ) as check_markdown:
            result = command_self_test()

        self.assertEqual(result, 0)
        scenario.assert_called_once_with()
        payload = scenario.return_value
        check_status.assert_called_once_with(payload)
        check_indexes.assert_called_once_with(payload)
        check_markdown.assert_called_once_with(payload)

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


class MarketIndexSnapshotCollectionTest(unittest.TestCase):
    """Runs the same scenario/check helpers `command_self_test` uses, one check per test.

    The scenario runs once for the whole class (setUpClass), not once per
    test method, so this class does not re-run collection three times.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = scenario_default_collection()

    def test_collection_reports_success_status(self) -> None:
        check_success_status(self.payload)

    def test_collection_returns_all_default_indexes(self) -> None:
        check_five_default_indexes(self.payload)

    def test_rendered_markdown_includes_every_index_display_name(self) -> None:
        check_rendered_markdown_includes_display_names(self.payload)


if __name__ == "__main__":
    unittest.main()
