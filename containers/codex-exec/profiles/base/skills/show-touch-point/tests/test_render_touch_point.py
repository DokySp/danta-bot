"""Tests for touch-point parsing and KIS chart fallback.

`command_self_test` is the compatibility body invoked by the production
CLI's `self-test`/`--self-test` command and must keep printing
`"self-test passed"` and returning `0`. Each logical block of the old
monolithic self-test now lives in its own `scenario_*` (setup + act) or
`check_*` (single assertion concern, reusable from a plain function or
a `TestCase` method) helper. `command_self_test` and the granular
`TestCase` methods below both call those helpers, so each behavior has
exactly one implementation. The wrapper-orchestration test mocks the
helpers rather than re-running every scenario, so discovery does not
execute the real work twice.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import render_touch_point as touch_module  # noqa: E402
from render_touch_point import (  # noqa: E402
    Candle,
    Trigger,
    expected_market_start,
    fetch_kis_index_series,
    fetch_kis_index_series_once,
    load_quote_history_series,
    parse_kis_minute,
    parse_touch_record,
)

TRIGGER = Trigger(
    trigger_id="kospi-case-1",
    case_title="case 1 - 기본 민감도",
    name="KOSPI",
    symbol="KOSPI",
    source="kis_domestic_index",
)
TOUCH_RECORD = {
    "type": "price_trigger_touch",
    "trigger_id": "kospi-case-1",
    "case_title": "case 1 - 기본 민감도",
    "name": "KOSPI",
    "symbol": "KOSPI",
    "source": "kis_domestic_index",
    "direction": "상승",
    "reference_value": 8864.24,
    "touch_value": 8963.76,
    "change_percent": 1.12,
    "observed_at": "2026-06-18T09:06:35+09:00",
}


def check_touch_record_matches_trigger_id() -> None:
    parsed_touch = parse_touch_record(TOUCH_RECORD, TRIGGER)
    if parsed_touch is None or parsed_touch.value != 8963.76:
        raise AssertionError("structured touch parsing failed")
    if parsed_touch.change_percent != 1.12:
        raise AssertionError("structured touch change percent parsing failed")


def check_touch_record_rejects_mismatched_trigger_id() -> None:
    wrong_touch = parse_touch_record({**TOUCH_RECORD, "trigger_id": "kospi-case-2"}, TRIGGER)
    if wrong_touch is not None:
        raise AssertionError("wrong trigger id should not match")


def write_quote_history(history_path: Path) -> None:
    history_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "recorded_at": "2026-06-18T09:00:00+09:00",
                        "source": "kis_domestic_index",
                        "symbol": "KOSPI",
                        "open": 8950.0,
                        "high": 8970.0,
                        "low": 8940.0,
                        "close": 8960.0,
                        "value": 8960.0,
                        "observed_at": "2026-06-18T09:00:00+09:00",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "recorded_at": "2026-06-18T09:01:00+09:00",
                        "source": "naver_domestic_index",
                        "symbol": "KOSPI",
                        "value": 1.0,
                        "observed_at": "2026-06-18T09:01:00+09:00",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n"
    )


def scenario_quote_history_series(history_dir: Path) -> list:
    history_path = history_dir / "history.jsonl"
    write_quote_history(history_path)
    return load_quote_history_series(history_path, TRIGGER)


def check_quote_history_series_filters_to_trigger_source(candles: list) -> None:
    if len(candles) != 1:
        raise AssertionError(f"expected only the matching-source row to survive, got: {candles}")
    candle = candles[0]
    if (candle.open, candle.high, candle.low, candle.close) != (8950.0, 8970.0, 8940.0, 8960.0):
        raise AssertionError(f"quote history OHLC values mismatched: {candle}")


def check_kis_minute_candle_parsing() -> None:
    parsed = parse_kis_minute(
        {
            "stck_bsop_date": "20260618",
            "stck_cntg_hour": "090000",
            "bstp_nmix_oprc": "8950.00",
            "bstp_nmix_hgpr": "8970.00",
            "bstp_nmix_lwpr": "8940.00",
            "bstp_nmix_prpr": "8960.00",
        }
    )
    if parsed is None or parsed.open != 8950.0 or parsed.high != 8970.0 or parsed.low != 8940.0:
        raise AssertionError(f"KIS minute candle parsing failed: {parsed}")


def check_kis_summary_row_is_skipped() -> None:
    summary_row = parse_kis_minute(
        {
            "stck_bsop_date": "20260618",
            "stck_cntg_hour": "999999",
            "bstp_nmix_prpr": "9063.84",
        }
    )
    if summary_row is not None:
        raise AssertionError(f"KIS summary row should be skipped, got: {summary_row}")


def fake_fetch_once(calls: list, fake_trigger: Trigger, fake_state_dir: Path) -> list:
    calls.append(str(fake_state_dir))
    return [
        Candle(
            observed_at=expected_market_start(date(2026, 6, 18)),
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
        )
    ]


def scenario_fetch_kis_index_series_single_call(state_dir: Path) -> tuple[list, list]:
    """fetch_kis_index_series should call the once-fetcher exactly once and return its rows."""
    calls: list[str] = []
    with patch.object(touch_module, "fetch_kis_index_series_once", lambda trigger, sd: fake_fetch_once(calls, trigger, sd)):
        merged = fetch_kis_index_series(TRIGGER, state_dir)
    return calls, merged


def check_fetch_kis_index_series_calls_once_fetcher_a_single_time(calls: list, state_dir: Path) -> None:
    if calls != [str(state_dir)]:
        raise AssertionError(f"KIS single-call lookup failed: {calls}")


def check_fetch_kis_index_series_returns_once_fetcher_rows(merged: list) -> None:
    if len(merged) != 1 or merged[0].observed_at != expected_market_start(date(2026, 6, 18)):
        raise AssertionError(f"KIS single-call result handling failed: {merged}")


def command_self_test() -> int:
    check_touch_record_matches_trigger_id()
    check_touch_record_rejects_mismatched_trigger_id()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        candles = scenario_quote_history_series(tmp_path)
        check_quote_history_series_filters_to_trigger_source(candles)

        check_kis_minute_candle_parsing()
        check_kis_summary_row_is_skipped()

        calls, merged = scenario_fetch_kis_index_series_single_call(tmp_path)
        check_fetch_kis_index_series_calls_once_fetcher_a_single_time(calls, tmp_path)
        check_fetch_kis_index_series_returns_once_fetcher_rows(merged)

    print("self-test passed")
    return 0


class RenderTouchPointSelfTest(unittest.TestCase):
    def test_self_test_suite_runs_every_check_and_reports_success(self) -> None:
        """Wrapper-orchestration check only: real behavior is covered by the
        granular tests below, so this mocks every helper instead of
        re-running the whole scenario a second time."""
        helper_names = [
            "check_touch_record_matches_trigger_id",
            "check_touch_record_rejects_mismatched_trigger_id",
            "scenario_quote_history_series",
            "check_quote_history_series_filters_to_trigger_source",
            "check_kis_minute_candle_parsing",
            "check_kis_summary_row_is_skipped",
            "scenario_fetch_kis_index_series_single_call",
            "check_fetch_kis_index_series_calls_once_fetcher_a_single_time",
            "check_fetch_kis_index_series_returns_once_fetcher_rows",
        ]
        patchers = [patch(f"{__name__}.{name}", return_value=(None, None)) for name in helper_names]
        mocks = [patcher.start() for patcher in patchers]
        self.addCleanup(lambda: [patcher.stop() for patcher in patchers])

        result = command_self_test()

        self.assertEqual(result, 0)
        for mock in mocks:
            mock.assert_called()


class TouchRecordParsingTest(unittest.TestCase):
    def test_touch_record_matches_trigger_id(self) -> None:
        check_touch_record_matches_trigger_id()

    def test_touch_record_rejects_mismatched_trigger_id(self) -> None:
        check_touch_record_rejects_mismatched_trigger_id()


class QuoteHistoryAndKisMinuteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.temp_root = Path(self._temp_dir.name)

    def test_quote_history_series_filters_to_trigger_source(self) -> None:
        candles = scenario_quote_history_series(self.temp_root)
        check_quote_history_series_filters_to_trigger_source(candles)

    def test_kis_minute_candle_parsing(self) -> None:
        check_kis_minute_candle_parsing()

    def test_kis_summary_row_is_skipped(self) -> None:
        check_kis_summary_row_is_skipped()

    def test_fetch_kis_index_series_calls_once_fetcher_a_single_time(self) -> None:
        calls, _ = scenario_fetch_kis_index_series_single_call(self.temp_root)
        check_fetch_kis_index_series_calls_once_fetcher_a_single_time(calls, self.temp_root)

    def test_fetch_kis_index_series_returns_once_fetcher_rows(self) -> None:
        _, merged = scenario_fetch_kis_index_series_single_call(self.temp_root)
        check_fetch_kis_index_series_returns_once_fetcher_rows(merged)


if __name__ == "__main__":
    unittest.main()
