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


def remove_retired_bundled_skills(target_root: Path, skill_names: tuple[str, ...]) -> None:
    for skill_name in skill_names:
        target = target_root / skill_name
        if not (target.exists() or target.is_symlink()):
            continue
        remove_existing_skill(target)
        logging.info("removed retired bundled Codex skill target=%s", target)
