import logging
import shutil
from pathlib import Path


def copy_bundled_skill(source: Path, target: Path) -> None:
    shutil.copytree(source, target)


def remove_existing_skill(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    shutil.rmtree(path)


def remove_stale_daily_trading_skill(target_root: Path) -> None:
    target = target_root / "daily-trading"
    if not (target.exists() or target.is_symlink()):
        return
    remove_existing_skill(target)
    logging.info("removed stale daily-trading Codex skill target=%s", target)
