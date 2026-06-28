from dataclasses import dataclass
from datetime import time, timedelta, timezone
from pathlib import Path


KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class PriceTrigger:
    trigger_id: str
    case_title: str
    name: str
    symbol: str
    source: str
    up_percent: float
    down_percent: float
    enabled: bool
    send_telegram: bool
    chat_id: str | None
    route: str | None


@dataclass(frozen=True)
class TriggerConfig:
    enabled: bool
    poll_seconds: int
    active_weekdays: str | None
    active_start_time: time | None
    active_end_time: time | None
    cache_file: Path
    quote_history_file: Path
    touch_log_file: Path
    triggers: list[PriceTrigger]


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    value: float
    observed_at: str
    market_status: str | None
    session_change_percent: float | None = None


@dataclass(frozen=True)
class TouchNotification:
    trigger: PriceTrigger
    quote: Quote
    reference: float
    percent: float
    direction_label: str
