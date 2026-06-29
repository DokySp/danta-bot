from typing import Any

from ..config import Config
from .skill_sync import sync as skill_sync


def load_skill_sync_module() -> Any:
    return skill_sync


def sync_bundled_skills(config: Config) -> None:
    load_skill_sync_module().sync_bundled_skills(
        config.bundled_skills_dir,
        config.codex_home / "skills",
        config.codex_home / ".bundled_skills_initialized",
        config.sync_skills_overwrite,
    )
