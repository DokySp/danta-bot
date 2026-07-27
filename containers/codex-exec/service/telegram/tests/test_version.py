from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ..commands import version


class VersionCommandTest(unittest.TestCase):
    def test_image_version_file_takes_precedence_over_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            version_file = Path(tmp) / "VERSION"
            version_file.write_text("v20260728-003\n", encoding="utf-8")

            with (
                patch.object(version, "VERSION_FILE", version_file),
                patch.dict(os.environ, {"APP_VERSION": "v20260728-002"}),
            ):
                self.assertEqual(version.app_version(), "v20260728-003")

    def test_environment_is_used_when_version_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            version_file = Path(tmp) / "VERSION"

            with (
                patch.object(version, "VERSION_FILE", version_file),
                patch.dict(os.environ, {"APP_VERSION": "v20260728-002"}),
            ):
                self.assertEqual(version.app_version(), "v20260728-002")

    def test_unknown_is_used_when_version_sources_are_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            version_file = Path(tmp) / "VERSION"
            version_file.write_text("\n", encoding="utf-8")

            with (
                patch.object(version, "VERSION_FILE", version_file),
                patch.dict(os.environ, {"APP_VERSION": ""}),
            ):
                self.assertEqual(version.app_version(), "unknown")


if __name__ == "__main__":
    unittest.main()
