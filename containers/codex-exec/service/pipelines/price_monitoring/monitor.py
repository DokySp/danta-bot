from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .price_monitoring_config import is_active_time, parse_price_trigger_config
from .price_monitoring_engine import execute_price_monitoring
from .price_monitoring_models import KST, TouchNotification


def run_price_monitoring_tick(
    config_file: Path,
    state_dir: Path,
    service_config: Any,
    on_touch: Callable[[TouchNotification], None] | None = None,
) -> tuple[int, list[TouchNotification]]:
    trigger_config = parse_price_trigger_config(config_file, state_dir)
    if not trigger_config.enabled or not is_active_time(trigger_config, datetime.now(KST)):
        return trigger_config.poll_seconds, []
    return trigger_config.poll_seconds, execute_price_monitoring(trigger_config, service_config, on_touch)
