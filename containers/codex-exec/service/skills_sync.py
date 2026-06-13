import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from .config import Config


def sync_bundled_skills(config: Config) -> None:
    source = config.bundled_skills_dir
    if not source.exists():
        logging.info("bundled skills dir does not exist: %s", source)
        return

    target_root = config.codex_home / "skills"
    marker = config.codex_home / ".bundled_skills_initialized"

    target_root.mkdir(parents=True, exist_ok=True)

    copied = 0
    replaced = 0
    skipped = 0
    for skill_dir in sorted(path for path in source.iterdir() if path.is_dir()):
        target = target_root / skill_dir.name
        if (target.exists() or target.is_symlink()) and config.sync_skills_overwrite:
            remove_existing_skill(target)
            replaced += 1
        if target.exists() or target.is_symlink():
            skipped += 1
            continue
        shutil.copytree(skill_dir, target)
        copied += 1

    write_skills_marker(config, marker, copied=copied, replaced=replaced, skipped=skipped)

    logging.info(
        "synced bundled skills copied=%s replaced_existing=%s skipped_existing=%s source=%s target=%s",
        copied,
        replaced,
        skipped,
        source,
        target_root,
    )


def remove_existing_skill(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    shutil.rmtree(path)


def write_skills_marker(config: Config, marker: Path, copied: int, replaced: int, skipped: int) -> None:
    payload = {
        "source": str(config.bundled_skills_dir),
        "target": str(config.codex_home / "skills"),
        "copied": copied,
        "replaced_existing": replaced,
        "skipped_existing": skipped,
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
