import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .price_monitoring_models import PriceTrigger, Quote


def read_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "triggers": {}}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("price trigger cache must contain a JSON object")
    data.setdefault("version", 1)
    data.setdefault("triggers", {})
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(path)


def write_cache(path: Path, data: dict[str, Any]) -> None:
    write_json(path, data)


def quote_history_path(cache_file: Path, configured: Any) -> Path:
    if configured:
        return Path(str(configured))
    return cache_file.with_name("quote-history.jsonl")


def touch_log_path(cache_file: Path, configured: Any) -> Path:
    if configured:
        return Path(str(configured))
    return cache_file.with_name("touch-events.jsonl")


def write_quote_history(path: Path, quote_items: Any) -> None:
    rows: list[str] = []
    recorded_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for quote_key, quote in quote_items:
        if not isinstance(quote, Quote) or quote.value <= 0:
            continue
        source = quote_key[0] if isinstance(quote_key, tuple) and quote_key else ""
        rows.append(
            json.dumps(
                {
                    "recorded_at": recorded_at,
                    "source": source,
                    "symbol": quote.symbol,
                    "name": quote.name,
                    "value": quote.value,
                    "observed_at": quote.observed_at,
                    "market_status": quote.market_status,
                    "session_change_percent": quote.session_change_percent,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        for row in rows:
            file.write(row + "\n")


def write_touch_event(
    path: Path,
    trigger: PriceTrigger,
    quote: Quote,
    reference: float,
    percent: float,
    direction_label: str,
) -> None:
    row = {
        "type": "price_trigger_touch",
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "trigger_id": trigger.trigger_id,
        "case_title": trigger.case_title,
        "name": trigger.name,
        "symbol": trigger.symbol,
        "source": trigger.source,
        "direction": direction_label,
        "reference_value": reference,
        "touch_value": quote.value,
        "change_percent": round(percent, 4),
        "observed_at": quote.observed_at,
        "market_status": quote.market_status,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def update_touch_state(
    state: dict[str, Any],
    quote: Quote,
    reference: float,
    percent: float,
    direction: str,
) -> None:
    state.update(
        {
            "reference_value": quote.value,
            "reference_observed_at": quote.observed_at,
            "last_checked_value": quote.value,
            "last_checked_at": quote.observed_at,
            "last_checked_change_percent": round(percent, 4),
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "last_touch": {
                "direction": direction,
                "previous_reference_value": reference,
                "touched_value": quote.value,
                "change_percent": round(percent, 4),
                "observed_at": quote.observed_at,
            },
        }
    )
