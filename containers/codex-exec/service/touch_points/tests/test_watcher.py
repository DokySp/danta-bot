"""Behavior tests for PriceTriggerWatcher's tick loop and touch notification seam."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ...pipelines.price_monitoring.price_monitoring_models import PriceTrigger, Quote, TouchNotification
from ..watcher import PriceTriggerWatcher


def make_config(**overrides) -> SimpleNamespace:
    base = dict(
        price_trigger_file=Path("/nonexistent/touch-points.yaml"),
        state_dir=Path("/nonexistent/state"),
        telegram_typing_interval_seconds=4.0,
        telegram_route=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_gateway(telegram_route: str | None = None) -> Mock:
    gateway = Mock()
    gateway.config = SimpleNamespace(telegram_route=telegram_route)
    return gateway


def make_notification(
    send_telegram: bool = True,
    chat_id: str | None = None,
    route: str | None = None,
    market_status: str | None = None,
) -> TouchNotification:
    trigger = PriceTrigger(
        trigger_id="kospi-case-1",
        case_title="case 1 - 기본 민감도",
        name="KOSPI",
        symbol="KOSPI",
        source="kis_domestic_index",
        up_percent=1.0,
        down_percent=-1.0,
        enabled=True,
        send_telegram=send_telegram,
        chat_id=chat_id,
        route=route,
    )
    quote = Quote(
        symbol="KOSPI",
        name="코스피",
        value=2530.0,
        observed_at="2026-01-02T09:00:00+09:00",
        market_status=market_status,
    )
    return TouchNotification(trigger, quote, reference=2500.0, percent=1.2, direction_label="상승")


class PriceTriggerWatcherConstructionTest(unittest.TestCase):
    def test_thread_is_created_but_not_started_until_start_is_called(self) -> None:
        watcher = PriceTriggerWatcher(make_config(), make_gateway())
        try:
            self.assertFalse(watcher.thread.is_alive())
            watcher.start()
            self.assertTrue(watcher.thread.is_alive())
        finally:
            watcher.stop()
            watcher.thread.join(timeout=2)

    def test_stop_sets_stop_event(self) -> None:
        watcher = PriceTriggerWatcher(make_config(), make_gateway())
        self.assertFalse(watcher.stop_event.is_set())
        watcher.stop()
        self.assertTrue(watcher.stop_event.is_set())


class SendTouchTest(unittest.TestCase):
    def test_sends_message_with_expected_fields_when_telegram_enabled(self) -> None:
        gateway = make_gateway()
        watcher = PriceTriggerWatcher(make_config(), gateway)
        notification = make_notification(send_telegram=True, market_status="장중")

        watcher._send_touch(notification)

        gateway.send_message.assert_called_once()
        text, chat_id, route = gateway.send_message.call_args.args
        self.assertIn("case 1 - 기본 민감도", text)
        self.assertIn("kospi-case-1", text)
        self.assertIn("상승", text)
        self.assertIn("2,500.00", text)
        self.assertIn("2,530.00", text)
        self.assertIn("+1.20%", text)
        self.assertIn("장중", text)
        self.assertIsNone(chat_id)
        self.assertIsNone(route)

    def test_does_not_send_message_when_send_telegram_disabled(self) -> None:
        gateway = make_gateway()
        watcher = PriceTriggerWatcher(make_config(), gateway)
        notification = make_notification(send_telegram=False)

        watcher._send_touch(notification)

        gateway.send_message.assert_not_called()

    def test_omits_market_status_line_when_absent(self) -> None:
        gateway = make_gateway()
        watcher = PriceTriggerWatcher(make_config(), gateway)
        notification = make_notification(send_telegram=True, market_status=None)

        watcher._send_touch(notification)

        text = gateway.send_message.call_args.args[0]
        self.assertNotIn("시장상태", text)

    def test_forwards_chat_id_and_route_to_gateway(self) -> None:
        gateway = make_gateway()
        watcher = PriceTriggerWatcher(make_config(), gateway)
        notification = make_notification(send_telegram=True, chat_id="12345", route="v2")

        watcher._send_touch(notification)

        _, chat_id, route = gateway.send_message.call_args.args
        self.assertEqual(chat_id, "12345")
        self.assertEqual(route, "v2")


class WatcherLoopTest(unittest.TestCase):
    def test_loop_delegates_to_injected_pipeline_with_configured_paths_and_stops(self) -> None:
        config = make_config(price_trigger_file="/cfg/touch-points.yaml", state_dir="/state")
        watcher = PriceTriggerWatcher(config, make_gateway())

        def fake_tick(price_trigger_file, state_dir, service_config, on_touch):
            watcher.stop_event.set()
            return 0.01, []

        watcher.pipeline = SimpleNamespace(run_price_monitoring_tick=Mock(side_effect=fake_tick))

        watcher._loop()

        watcher.pipeline.run_price_monitoring_tick.assert_called_once_with(
            "/cfg/touch-points.yaml", "/state", config, watcher._send_touch
        )

    def test_loop_survives_pipeline_exception_and_still_stops(self) -> None:
        watcher = PriceTriggerWatcher(make_config(), make_gateway())

        def failing_tick(*_args, **_kwargs):
            watcher.stop_event.set()
            raise RuntimeError("boom")

        watcher.pipeline = SimpleNamespace(run_price_monitoring_tick=Mock(side_effect=failing_tick))

        with patch("service.touch_points.watcher.logging.exception") as log_exception:
            watcher._loop()

        log_exception.assert_called_once()

    def test_loop_exits_immediately_when_already_stopped(self) -> None:
        watcher = PriceTriggerWatcher(make_config(), make_gateway())
        watcher.pipeline = SimpleNamespace(run_price_monitoring_tick=Mock())
        watcher.stop_event.set()

        watcher._loop()

        watcher.pipeline.run_price_monitoring_tick.assert_not_called()


if __name__ == "__main__":
    unittest.main()
