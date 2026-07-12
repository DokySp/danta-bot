"""Tests for touch-point parsing and KIS chart fallback."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

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


def command_self_test() -> int:
    trigger = Trigger(
        trigger_id="kospi-case-1",
        case_title="case 1 - 기본 민감도",
        name="KOSPI",
        symbol="KOSPI",
        source="kis_domestic_index",
    )
    touch_record = {
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
    parsed_touch = parse_touch_record(touch_record, trigger)
    wrong_touch = parse_touch_record({**touch_record, "trigger_id": "kospi-case-2"}, trigger)
    if parsed_touch is None or parsed_touch.value != 8963.76:
        raise RuntimeError("structured touch parsing failed")
    if parsed_touch.change_percent != 1.12:
        raise RuntimeError("structured touch change percent parsing failed")
    if wrong_touch is not None:
        raise RuntimeError("wrong trigger id should not match")

    with tempfile.TemporaryDirectory() as tmpdir:
        history_path = Path(tmpdir) / "history.jsonl"
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
        candles = load_quote_history_series(history_path, trigger)
        if (
            len(candles) != 1
            or candles[0].open != 8950.0
            or candles[0].high != 8970.0
            or candles[0].low != 8940.0
            or candles[0].close != 8960.0
        ):
            raise RuntimeError("quote history loading failed")

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
            raise RuntimeError("KIS minute candle parsing failed")
        summary_row = parse_kis_minute(
            {
                "stck_bsop_date": "20260618",
                "stck_cntg_hour": "999999",
                "bstp_nmix_prpr": "9063.84",
            }
        )
        if summary_row is not None:
            raise RuntimeError("KIS summary row should be skipped")

        calls: list[str] = []
        original_fetch_once = fetch_kis_index_series_once

        def fake_fetch_once(
            fake_trigger: Trigger,
            fake_state_dir: Path,
        ) -> list[Candle]:
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

        try:
            touch_module.fetch_kis_index_series_once = fake_fetch_once
            merged = fetch_kis_index_series(
                trigger,
                Path(tmpdir),
            )
        finally:
            touch_module.fetch_kis_index_series_once = original_fetch_once

        if calls != [tmpdir]:
            raise RuntimeError(f"KIS single-call lookup failed: {calls}")
        if len(merged) != 1 or merged[0].observed_at != expected_market_start(date(2026, 6, 18)):
            raise RuntimeError("KIS single-call result handling failed")

    print("self-test passed")
    return 0


class RenderTouchPointSelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        self.assertEqual(command_self_test(), 0)
