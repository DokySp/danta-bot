"""Behavior tests for the price_monitoring config/engine/tick boundary.

`fetch_quote` is the pipeline's only external I/O call (KIS/Naver HTTP);
it is patched at its call site so these tests exercise the real
config-parsing, threshold-evaluation, and cache/log persistence logic
offline and deterministically.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, time
from pathlib import Path
from unittest.mock import patch

from .. import monitor
from ..price_monitoring_config import is_active_time, parse_price_trigger_config
from ..price_monitoring_engine import execute_price_monitoring
from ..price_monitoring_models import KST, PriceTrigger, Quote, TriggerConfig
from ..price_monitoring_storage import read_cache

FETCH_QUOTE_TARGET = "service.pipelines.price_monitoring.price_monitoring_engine.fetch_quote"


def make_trigger(
    trigger_id: str = "kospi-case-1",
    symbol: str = "KOSPI",
    source: str = "kis_domestic_index",
    up_percent: float = 1.0,
    down_percent: float = -1.0,
    enabled: bool = True,
    send_telegram: bool = True,
) -> PriceTrigger:
    return PriceTrigger(
        trigger_id=trigger_id,
        case_title="case 1",
        name="KOSPI",
        symbol=symbol,
        source=source,
        up_percent=up_percent,
        down_percent=down_percent,
        enabled=enabled,
        send_telegram=send_telegram,
        chat_id=None,
        route=None,
    )


def make_quote(value: float, observed_at: str = "2026-01-02T09:00:00+09:00") -> Quote:
    return Quote(
        symbol="KOSPI",
        name="코스피",
        value=value,
        observed_at=observed_at,
        market_status=None,
    )


class ExecutePriceMonitoringTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cache_file = self.root / "triggers.json"

    def trigger_config(self, triggers: list[PriceTrigger]) -> TriggerConfig:
        return TriggerConfig(
            enabled=True,
            poll_seconds=60,
            active_weekdays=None,
            active_start_time=None,
            active_end_time=None,
            cache_file=self.cache_file,
            quote_history_file=self.root / "quote-history.jsonl",
            touch_log_file=self.root / "touch-events.jsonl",
            triggers=triggers,
        )

    def test_first_observation_initializes_reference_without_notification(self) -> None:
        config = self.trigger_config([make_trigger()])
        with patch(FETCH_QUOTE_TARGET, return_value=make_quote(2500.0)):
            notifications = execute_price_monitoring(config, service_config=None)

        self.assertEqual(notifications, [])
        cache = read_cache(self.cache_file)
        self.assertEqual(cache["triggers"]["kospi-case-1"]["reference_value"], 2500.0)
        self.assertTrue(config.quote_history_file.is_file())
        self.assertFalse(config.touch_log_file.exists())

    def test_upward_touch_beyond_threshold_emits_notification_and_touch_log(self) -> None:
        config = self.trigger_config([make_trigger(up_percent=1.0, down_percent=-1.0)])
        with patch(FETCH_QUOTE_TARGET, return_value=make_quote(2500.0)):
            execute_price_monitoring(config, service_config=None)

        touched: list = []
        with patch(FETCH_QUOTE_TARGET, return_value=make_quote(2530.0)):
            notifications = execute_price_monitoring(config, service_config=None, on_touch=touched.append)

        self.assertEqual(len(notifications), 1)
        notification = notifications[0]
        self.assertAlmostEqual(notification.percent, 1.2, places=4)
        self.assertEqual(notification.direction_label, "상승")
        self.assertEqual(touched, notifications)

        cache = read_cache(self.cache_file)
        self.assertEqual(cache["triggers"]["kospi-case-1"]["reference_value"], 2530.0)

        touch_lines = config.touch_log_file.read_text().strip().splitlines()
        self.assertEqual(len(touch_lines), 1)
        touch_row = json.loads(touch_lines[0])
        self.assertEqual(touch_row["direction"], "상승")
        self.assertEqual(touch_row["trigger_id"], "kospi-case-1")

    def test_downward_touch_below_threshold_emits_notification(self) -> None:
        config = self.trigger_config([make_trigger(up_percent=1.0, down_percent=-1.0)])
        with patch(FETCH_QUOTE_TARGET, return_value=make_quote(2500.0)):
            execute_price_monitoring(config, service_config=None)
        with patch(FETCH_QUOTE_TARGET, return_value=make_quote(2470.0)):
            notifications = execute_price_monitoring(config, service_config=None)

        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].direction_label, "하락")

    def test_within_threshold_movement_emits_no_notification_and_does_not_rewrite_cache(self) -> None:
        config = self.trigger_config([make_trigger(up_percent=1.0, down_percent=-1.0)])
        with patch(FETCH_QUOTE_TARGET, return_value=make_quote(2500.0)):
            execute_price_monitoring(config, service_config=None)
        cache_mtime_before = self.cache_file.stat().st_mtime_ns

        with patch(FETCH_QUOTE_TARGET, return_value=make_quote(2505.0)):
            notifications = execute_price_monitoring(config, service_config=None)

        self.assertEqual(notifications, [])
        self.assertEqual(self.cache_file.stat().st_mtime_ns, cache_mtime_before)

    def test_disabled_trigger_is_skipped_entirely(self) -> None:
        config = self.trigger_config([make_trigger(enabled=False)])
        with patch(FETCH_QUOTE_TARGET) as fetch_quote:
            notifications = execute_price_monitoring(config, service_config=None)

        fetch_quote.assert_not_called()
        self.assertEqual(notifications, [])
        cache = read_cache(self.cache_file)
        self.assertEqual(cache["triggers"], {})

    def test_non_positive_quote_is_ignored_without_state_update(self) -> None:
        config = self.trigger_config([make_trigger()])
        with patch(FETCH_QUOTE_TARGET, return_value=make_quote(0.0)):
            notifications = execute_price_monitoring(config, service_config=None)

        self.assertEqual(notifications, [])
        cache = read_cache(self.cache_file)
        self.assertEqual(cache["triggers"], {})

    def test_two_triggers_sharing_source_and_symbol_fetch_quote_only_once(self) -> None:
        config = self.trigger_config(
            [
                make_trigger(trigger_id="kospi-case-1", up_percent=1.0, down_percent=-1.0),
                make_trigger(trigger_id="kospi-case-2", up_percent=2.0, down_percent=-2.0),
            ]
        )
        with patch(FETCH_QUOTE_TARGET, return_value=make_quote(2500.0)) as fetch_quote:
            execute_price_monitoring(config, service_config=None)

        fetch_quote.assert_called_once()


class RunPriceMonitoringTickTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config_file = self.root / "touch-points.yaml"
        self.state_dir = self.root / "state"

    def write_config(self, text: str) -> None:
        self.config_file.write_text(text)

    def test_disabled_config_skips_engine_and_returns_configured_poll_seconds(self) -> None:
        self.write_config(
            "enabled: false\n"
            "poll_seconds: 90\n"
            "touch_points:\n"
            "  - id: kospi-case-1\n"
            "    symbol: KOSPI\n"
            "    up_percent: 1.0\n"
            "    down_percent: -1.0\n"
        )
        with patch(
            "service.pipelines.price_monitoring.monitor.execute_price_monitoring"
        ) as execute:
            wait_seconds, notifications = monitor.run_price_monitoring_tick(
                self.config_file, self.state_dir, service_config=None
            )

        execute.assert_not_called()
        self.assertEqual(wait_seconds, 90)
        self.assertEqual(notifications, [])

    def test_inactive_time_window_skips_engine(self) -> None:
        self.write_config(
            "enabled: true\n"
            "active_start_time: '0900'\n"
            "active_end_time: '0901'\n"
            "touch_points:\n"
            "  - id: kospi-case-1\n"
            "    symbol: KOSPI\n"
            "    up_percent: 1.0\n"
            "    down_percent: -1.0\n"
        )
        with patch(
            "service.pipelines.price_monitoring.monitor.datetime"
        ) as fake_datetime, patch(
            "service.pipelines.price_monitoring.monitor.execute_price_monitoring"
        ) as execute:
            fake_datetime.now.return_value = datetime(2026, 1, 2, 12, 0, tzinfo=KST)
            wait_seconds, notifications = monitor.run_price_monitoring_tick(
                self.config_file, self.state_dir, service_config=None
            )

        execute.assert_not_called()
        self.assertEqual(notifications, [])

    def test_active_config_delegates_to_engine(self) -> None:
        self.write_config(
            "enabled: true\n"
            "touch_points:\n"
            "  - id: kospi-case-1\n"
            "    symbol: KOSPI\n"
            "    up_percent: 1.0\n"
            "    down_percent: -1.0\n"
        )
        with patch(FETCH_QUOTE_TARGET, return_value=make_quote(2500.0)):
            wait_seconds, notifications = monitor.run_price_monitoring_tick(
                self.config_file, self.state_dir, service_config=None
            )

        self.assertEqual(wait_seconds, 60)
        self.assertEqual(notifications, [])
        cache = read_cache(self.state_dir / "touch-points" / "triggers.json")
        self.assertIn("kospi-case-1", cache["triggers"])

    def test_missing_config_file_disables_monitoring_with_default_poll(self) -> None:
        wait_seconds, notifications = monitor.run_price_monitoring_tick(
            self.config_file, self.state_dir, service_config=None
        )
        self.assertEqual(wait_seconds, 60)
        self.assertEqual(notifications, [])


class PriceTriggerConfigValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config_file = self.root / "touch-points.yaml"
        self.state_dir = self.root / "state"

    def write_config(self, text: str) -> None:
        self.config_file.write_text(text)

    def test_up_percent_must_be_positive(self) -> None:
        self.write_config(
            "touch_points:\n"
            "  - id: bad\n"
            "    symbol: KOSPI\n"
            "    up_percent: 0\n"
            "    down_percent: -1.0\n"
        )
        with self.assertRaisesRegex(ValueError, "up_percent must be greater than 0"):
            parse_price_trigger_config(self.config_file, self.state_dir)

    def test_down_percent_must_be_negative(self) -> None:
        self.write_config(
            "touch_points:\n"
            "  - id: bad\n"
            "    symbol: KOSPI\n"
            "    up_percent: 1.0\n"
            "    down_percent: 0\n"
        )
        with self.assertRaisesRegex(ValueError, "down_percent must be less than 0"):
            parse_price_trigger_config(self.config_file, self.state_dir)

    def test_unquoted_active_time_is_rejected(self) -> None:
        self.write_config(
            "active_start_time: 900\n"
            "active_end_time: 1530\n"
            "touch_points: []\n"
        )
        with self.assertRaisesRegex(ValueError, "must use quoted HHMM string format"):
            parse_price_trigger_config(self.config_file, self.state_dir)

    def test_active_start_must_be_before_active_end(self) -> None:
        self.write_config(
            "active_start_time: '1530'\n"
            "active_end_time: '0900'\n"
            "touch_points: []\n"
        )
        with self.assertRaisesRegex(ValueError, "active_start_time must be earlier than active_end_time"):
            parse_price_trigger_config(self.config_file, self.state_dir)

    def test_invalid_weekday_expression_is_rejected(self) -> None:
        self.write_config(
            "active_weekdays: '8'\n"
            "touch_points: []\n"
        )
        with self.assertRaisesRegex(ValueError, "active_weekdays values must be between 0 and 7"):
            parse_price_trigger_config(self.config_file, self.state_dir)


class IsActiveTimeTest(unittest.TestCase):
    def config(self, **overrides) -> TriggerConfig:
        base = dict(
            enabled=True,
            poll_seconds=60,
            active_weekdays=None,
            active_start_time=None,
            active_end_time=None,
            cache_file=Path("/tmp/triggers.json"),
            quote_history_file=Path("/tmp/quote-history.jsonl"),
            touch_log_file=Path("/tmp/touch-events.jsonl"),
            triggers=[],
        )
        base.update(overrides)
        return TriggerConfig(**base)

    def test_no_window_configured_is_always_active(self) -> None:
        config = self.config()
        self.assertTrue(is_active_time(config, datetime(2026, 1, 3, 3, 0, tzinfo=KST)))

    def test_weekday_outside_configured_range_is_inactive(self) -> None:
        config = self.config(active_weekdays="1-5")
        sunday = datetime(2026, 1, 4, 10, 0, tzinfo=KST)
        self.assertTrue(sunday.weekday() == 6)
        self.assertFalse(is_active_time(config, sunday))

    def test_time_inside_configured_window_is_active(self) -> None:
        config = self.config(active_start_time=time(9, 0), active_end_time=time(15, 30))
        monday = datetime(2026, 1, 5, 12, 0, tzinfo=KST)
        self.assertTrue(is_active_time(config, monday))

    def test_time_outside_configured_window_is_inactive(self) -> None:
        config = self.config(active_start_time=time(9, 0), active_end_time=time(15, 30))
        monday_evening = datetime(2026, 1, 5, 20, 0, tzinfo=KST)
        self.assertFalse(is_active_time(config, monday_evening))


if __name__ == "__main__":
    unittest.main()
