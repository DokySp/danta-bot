import json
from datetime import UTC, datetime
from pathlib import Path


def write_skills_marker(
    marker: Path,
    *,
    source: Path,
    target_root: Path,
    copied: int,
    replaced: int,
    skipped: int,
) -> None:
    payload = {
        "source": str(source),
        "target": str(target_root),
        "copied": copied,
        "replaced_existing": replaced,
        "skipped_existing": skipped,
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
