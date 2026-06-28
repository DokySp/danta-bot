import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .price_monitoring_models import PriceTrigger, Quote, TouchNotification, TriggerConfig
from .price_monitoring_quotes import fetch_quote
from .price_monitoring_storage import (
    read_cache,
    update_touch_state,
    write_cache,
    write_quote_history,
    write_touch_event,
)
from .price_monitoring_util import parse_float


def execute_price_monitoring(
    trigger_config: TriggerConfig,
    service_config: Any,
    on_touch: Callable[[TouchNotification], None] | None = None,
) -> list[TouchNotification]:
    cache = read_cache(trigger_config.cache_file)
    changed = False
    states = cache.setdefault("triggers", {})
    if not isinstance(states, dict):
        states = {}
        cache["triggers"] = states

    notifications: list[TouchNotification] = []
    quotes: dict[tuple[str, str], Quote] = {}
    for trigger in trigger_config.triggers:
        if not trigger.enabled:
            continue
        quote_key = (trigger.source, trigger.symbol.upper())
        quote = quotes.get(quote_key)
        if quote is None:
            quote = fetch_quote(trigger, service_config)
            quotes[quote_key] = quote
        state = states.setdefault(trigger.trigger_id, {})
        if not isinstance(state, dict):
            state = {}
            states[trigger.trigger_id] = state
        notification, did_change = evaluate_quote(trigger_config, trigger, quote, state, on_touch)
        changed = did_change or changed
        if notification is not None:
            notifications.append(notification)

    if changed:
        write_cache(trigger_config.cache_file, cache)
    try:
        write_quote_history(trigger_config.quote_history_file, quotes.items())
    except Exception:
        logging.exception("failed to write price trigger quote history")
    return notifications


def evaluate_quote(
    trigger_config: TriggerConfig,
    trigger: PriceTrigger,
    quote: Quote,
    state: dict[str, Any],
    on_touch: Callable[[TouchNotification], None] | None = None,
) -> tuple[TouchNotification | None, bool]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    if quote.value <= 0:
        logging.warning(
            "ignored non-positive price trigger quote id=%s value=%s observed_at=%s",
            trigger.trigger_id,
            quote.value,
            quote.observed_at,
        )
        return None, False

    reference = parse_float(state.get("reference_value"))
    if reference is None or reference <= 0:
        state.update(
            {
                "reference_value": quote.value,
                "reference_observed_at": quote.observed_at,
                "last_checked_value": quote.value,
                "last_checked_at": quote.observed_at,
                "updated_at": now,
            }
        )
        logging.info(
            "initialized price trigger reference id=%s value=%s",
            trigger.trigger_id,
            quote.value,
        )
        return None, True

    percent = ((quote.value - reference) / reference) * 100
    if percent >= trigger.up_percent:
        direction_label = "상승"
        notification = TouchNotification(trigger, quote, reference, percent, direction_label)
        write_touch_event(trigger_config.touch_log_file, trigger, quote, reference, percent, direction_label)
        if on_touch is not None:
            on_touch(notification)
        update_touch_state(state, quote, reference, percent, "up")
        return notification, True
    if percent <= trigger.down_percent:
        direction_label = "하락"
        notification = TouchNotification(trigger, quote, reference, percent, direction_label)
        write_touch_event(trigger_config.touch_log_file, trigger, quote, reference, percent, direction_label)
        if on_touch is not None:
            on_touch(notification)
        update_touch_state(state, quote, reference, percent, "down")
        return notification, True
    return None, False
