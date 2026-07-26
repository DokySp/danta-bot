"""Tests for persistent bundled-skill synchronization migrations."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ..skill_sync.sync import sync_bundled_skills


class SkillSyncTest(unittest.TestCase):
    def test_sync_removes_retired_bundled_news_skill_from_persistent_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "bundled"
            target = root / "persistent-skills"
            current = source / "current-skill"
            retired = target / "collect-news-information"
            current.mkdir(parents=True)
            retired.mkdir(parents=True)
            (current / "SKILL.md").write_text("current\n", encoding="utf-8")
            (retired / "SKILL.md").write_text("stale\n", encoding="utf-8")

            sync_bundled_skills(
                source,
                target,
                root / "skills-marker.json",
                overwrite=True,
            )

            self.assertFalse(retired.exists())
            self.assertEqual((target / "current-skill" / "SKILL.md").read_text(encoding="utf-8"), "current\n")


if __name__ == "__main__":
    unittest.main()
