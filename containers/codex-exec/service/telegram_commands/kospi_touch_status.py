from typing import Any

from ..price_trigger import fetch_quote, parse_float, parse_price_trigger_config, read_cache
from ..telegram_gateway import TypingIndicator


def handle_kospi_touch_status(worker: Any, task: Any, args: str) -> None:
    if args:
        worker.gateway.send_message(
            "사용법: <code>/kospi_touch_status</code>\n"
            "코스피 터치 기준값 상태 확인 명령은 메시지를 함께 받지 않습니다.",
            task.chat_id,
            task.route,
        )
        return

    trigger_config = parse_price_trigger_config(
        worker.config.price_trigger_file,
        worker.config.state_dir,
    )
    trigger = next(
        (
            item
            for item in trigger_config.triggers
            if item.trigger_id == "kospi" or item.symbol.upper() == "KOSPI"
        ),
        None,
    )
    if trigger is None:
        worker.gateway.send_message(
            "KOSPI 가격 터치 설정을 찾을 수 없습니다.",
            task.chat_id,
            task.route,
        )
        return

    with TypingIndicator(
        worker.gateway,
        task.chat_id,
        task.route,
        worker.config.telegram_typing_interval_seconds,
    ):
        quote = fetch_quote(trigger, worker.config)

    cached_value, _ = cached_reference(trigger_config.cache_file, trigger.trigger_id)
    cached_change = percent_change(quote.value, cached_value)
    text = (
        f"현재 코스피: <code>{format_number(quote.value)}</code>\n"
        f"캐시 기준값: <code>{format_number(cached_value)}</code>\n"
        f"캐시 기준 대비: <code>{format_percent(cached_change)}</code>\n"
        f"장중 등락률: <code>{format_percent(quote.session_change_percent)}</code>"
    )

    worker.gateway.send_message(text, task.chat_id, task.route)


def cached_reference(cache_file: Any, trigger_id: str) -> tuple[float | None, str | None]:
    try:
        cache = read_cache(cache_file)
    except (OSError, ValueError):
        return None, None
    triggers = cache.get("triggers")
    if not isinstance(triggers, dict):
        return None, None
    state = triggers.get(trigger_id)
    if not isinstance(state, dict):
        return None, None
    return parse_float(state.get("reference_value")), string_or_none(state.get("reference_observed_at"))


def percent_change(current: float, reference: float | None) -> float | None:
    if reference is None or reference <= 0:
        return None
    return ((current - reference) / reference) * 100


def format_number(value: float | None) -> str:
    if value is None:
        return "확인불가"
    return f"{value:,.2f}"


def format_percent(value: float | None) -> str:
    if value is None:
        return "확인불가"
    return f"{value:+.2f}%"


def string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
