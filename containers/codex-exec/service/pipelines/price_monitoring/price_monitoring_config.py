import re
from datetime import datetime, time
from pathlib import Path
from typing import Any

import yaml

from .price_monitoring_models import KST, PriceTrigger, TriggerConfig
from .price_monitoring_storage import quote_history_path, touch_log_path


CONFIG_TIME_RE = re.compile(r"^\d{4}$")


def parse_price_trigger_config(path: Path, state_dir: Path) -> TriggerConfig:
    if not path.exists():
        cache_file = state_dir / "touch-points" / "triggers.json"
        return TriggerConfig(
            False,
            60,
            None,
            None,
            None,
            cache_file,
            quote_history_path(cache_file, None),
            touch_log_path(cache_file, None),
            [],
        )

    raw_text = path.read_text()
    quoted_fields = quoted_yaml_scalar_fields(
        raw_text,
        {"active_start_time", "active_end_time"},
    )
    data = yaml.safe_load(raw_text) or {}
    if not isinstance(data, dict):
        raise ValueError("price trigger file must contain a YAML object")

    raw_triggers = data.get("touch_points", data.get("triggers", []))
    if not isinstance(raw_triggers, list):
        raise ValueError("price trigger file must contain a touch_points list")

    defaults = data.get("telegram", {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError("price trigger telegram must be a YAML object")

    triggers: list[PriceTrigger] = []
    for item in raw_triggers:
        if not isinstance(item, dict):
            continue
        trigger_id = str(item.get("id", "")).strip()
        symbol = str(item.get("symbol", "")).strip()
        if not trigger_id or not symbol:
            continue
        up_percent = float(item.get("up_percent", 0))
        down_percent = float(item.get("down_percent", 0))
        if up_percent <= 0:
            raise ValueError(f"{trigger_id}: up_percent must be greater than 0")
        if down_percent >= 0:
            raise ValueError(f"{trigger_id}: down_percent must be less than 0")
        chat_id = item.get("chat_id", defaults.get("chat_id"))
        route = item.get("route", defaults.get("route"))
        triggers.append(
            PriceTrigger(
                trigger_id=trigger_id,
                case_title=str(item.get("case_title") or trigger_id),
                name=str(item.get("name") or symbol),
                symbol=symbol,
                source=str(item.get("source") or "naver_domestic_index"),
                up_percent=up_percent,
                down_percent=down_percent,
                enabled=item.get("enabled", True) is not False,
                send_telegram=item.get("send_telegram", True) is not False,
                chat_id=str(chat_id) if chat_id else None,
                route=str(route) if route else None,
            )
        )

    cache_file = Path(data.get("cache_file") or state_dir / "touch-points" / "triggers.json")
    for field_name in ("active_start_time", "active_end_time"):
        if data.get(field_name) is not None and quoted_fields.get(field_name) is not True:
            raise ValueError(f"{field_name} must use quoted HHMM string format")
    active_start_time = parse_config_time(data.get("active_start_time"), "active_start_time")
    active_end_time = parse_config_time(data.get("active_end_time"), "active_end_time")
    if (active_start_time is None) != (active_end_time is None):
        raise ValueError("active_start_time and active_end_time must be configured together")
    if active_start_time is not None and active_start_time >= active_end_time:
        raise ValueError("active_start_time must be earlier than active_end_time")
    active_weekdays = parse_weekday_expr(data.get("active_weekdays"))

    return TriggerConfig(
        enabled=data.get("enabled", True) is not False,
        poll_seconds=max(60, int(data.get("poll_seconds", 60))),
        active_weekdays=active_weekdays,
        active_start_time=active_start_time,
        active_end_time=active_end_time,
        cache_file=cache_file,
        quote_history_file=quote_history_path(cache_file, data.get("quote_history_file")),
        touch_log_file=touch_log_path(cache_file, data.get("touch_log_file")),
        triggers=triggers,
    )


def parse_config_time(value: Any, field_name: str) -> time | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must use HHMM string format")
    text = value
    if not CONFIG_TIME_RE.fullmatch(text):
        raise ValueError(f"{field_name} must use HHMM string format")
    try:
        return time(int(text[:2]), int(text[2:4]))
    except ValueError as exc:
        raise ValueError(f"{field_name} must use HHMM string format") from exc


def quoted_yaml_scalar_fields(text: str, field_names: set[str]) -> dict[str, bool]:
    node = yaml.compose(text)
    result: dict[str, bool] = {}
    if node is None or not isinstance(getattr(node, "value", None), list):
        return result
    for key_node, value_node in node.value:
        key = getattr(key_node, "value", None)
        if key in field_names:
            result[str(key)] = getattr(value_node, "style", None) in {"'", '"'}
    return result


def parse_weekday_expr(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("active_weekdays must use cron weekday string format")
    text = value.strip()
    if not text:
        raise ValueError("active_weekdays must not be empty")
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValueError("active_weekdays must use cron weekday string format")
        base, step = (part.split("/", 1) + ["1"])[:2] if "/" in part else (part, "1")
        if not step.isdigit() or int(step) <= 0:
            raise ValueError("active_weekdays step must be a positive integer")
        if base == "*":
            continue
        if "-" in base:
            start_text, end_text = base.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError("active_weekdays must use cron weekday string format")
            start, end = int(start_text), int(end_text)
            if start < 0 or end > 7 or start > end:
                raise ValueError("active_weekdays values must be between 0 and 7")
            continue
        if not base.isdigit():
            raise ValueError("active_weekdays must use cron weekday string format")
        weekday = int(base)
        if weekday < 0 or weekday > 7:
            raise ValueError("active_weekdays values must be between 0 and 7")
    return text


def is_active_time(trigger_config: TriggerConfig, now: datetime) -> bool:
    current_datetime = now.astimezone(KST)
    if trigger_config.active_weekdays is not None:
        cron_weekday = (current_datetime.weekday() + 1) % 7
        if not weekday_expr_matches(trigger_config.active_weekdays, cron_weekday):
            return False

    start = trigger_config.active_start_time
    end = trigger_config.active_end_time
    if start is None or end is None:
        return True

    current = current_datetime.time().replace(second=0, microsecond=0)
    return start <= current <= end


def weekday_expr_matches(expr: str, value: int) -> bool:
    for part in expr.split(","):
        part = part.strip()
        base, step = (part.split("/", 1) + ["1"])[:2] if "/" in part else (part, "1")
        step_int = int(step)
        if base == "*":
            start, end = 0, 7
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(base)
        if value == 0 and start <= 7 <= end and (7 - start) % step_int == 0:
            return True
        if value == 0 and start == end == 7:
            return True
        if start <= value <= end and (value - start) % step_int == 0:
            return True
    return False
