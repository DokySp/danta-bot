from pathlib import Path
from typing import Any

import yaml


def parse_yaml_schedule(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    schedules = data.get("schedules", [])
    if not isinstance(schedules, list):
        raise ValueError("schedule file must contain a schedules list")
    return [item for item in schedules if isinstance(item, dict)]
