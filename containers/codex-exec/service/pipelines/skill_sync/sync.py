import logging
from pathlib import Path

from .skill_sync_filesystem import (
    copy_bundled_skill,
    remove_existing_skill,
    remove_stale_daily_trading_skill,
)
from .skill_sync_marker import write_skills_marker


def sync_bundled_skills(
    source: Path,
    target_root: Path,
    marker: Path,
    overwrite: bool,
) -> None:
    if not source.exists():
        logging.info("bundled skills dir does not exist: %s", source)
        return

    target_root.mkdir(parents=True, exist_ok=True)
    remove_stale_daily_trading_skill(target_root)

    copied = 0
    replaced = 0
    skipped = 0
    for skill_dir in sorted(path for path in source.iterdir() if path.is_dir()):
        target = target_root / skill_dir.name
        if (target.exists() or target.is_symlink()) and overwrite:
            remove_existing_skill(target)
            replaced += 1
        if target.exists() or target.is_symlink():
            skipped += 1
            continue
        copy_bundled_skill(skill_dir, target)
        copied += 1

    write_skills_marker(
        marker,
        source=source,
        target_root=target_root,
        copied=copied,
        replaced=replaced,
        skipped=skipped,
    )

    logging.info(
        "synced bundled skills copied=%s replaced_existing=%s skipped_existing=%s source=%s target=%s",
        copied,
        replaced,
        skipped,
        source,
        target_root,
    )
