from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from ..usage import append_token_usage_summary, format_percent_delta, usage_window


def snapshot(*, primary: dict | None, secondary: dict | None) -> dict:
    return {"rateLimits": {"primary": primary, "secondary": secondary}}


def window(used_percent: float, duration_mins: int | None = None) -> dict:
    value = {"usedPercent": used_percent}
    if duration_mins is not None:
        value["windowDurationMins"] = duration_mins
    return value


RUNTIME_USAGE_PATH = Path(__file__).resolve().parents[3] / "runtime" / "codex_usage.py"
RUNTIME_USAGE_SPEC = importlib.util.spec_from_file_location("runtime_codex_usage", RUNTIME_USAGE_PATH)
if RUNTIME_USAGE_SPEC is None or RUNTIME_USAGE_SPEC.loader is None:
    raise AssertionError()
runtime_codex_usage = importlib.util.module_from_spec(RUNTIME_USAGE_SPEC)
RUNTIME_USAGE_SPEC.loader.exec_module(runtime_codex_usage)


class UsageWindowTest(unittest.TestCase):
    def test_resolves_legacy_5h_and_weekly_windows_by_duration(self) -> None:
        before = snapshot(
            primary=window(10, 300),
            secondary=window(20, 10080),
        )
        after = snapshot(
            primary=window(13, 300),
            secondary=window(21, 10080),
        )

        self.assertEqual(format_percent_delta(before, after, "5h"), "3%")
        self.assertEqual(format_percent_delta(before, after, "weekly"), "1%")

    def test_resolves_weekly_only_primary_window(self) -> None:
        before = snapshot(primary=window(40, 10080), secondary=None)
        after = snapshot(primary=window(43, 10080), secondary=None)

        self.assertIsNone(usage_window(after, "5h"))
        self.assertIs(usage_window(after, "weekly"), after["rateLimits"]["primary"])
        self.assertEqual(format_percent_delta(before, after, "5h"), "n/a")
        self.assertEqual(format_percent_delta(before, after, "weekly"), "3%")

        summary = append_token_usage_summary(
            "결과\n총 사용 토큰: 1",
            Path("."),
            None,
            {},
            before,
            after,
        )
        self.assertIn("<b>5h: n/a</b>", summary)
        self.assertIn("<b>weekly: 3%</b>", summary)

    def test_keeps_positional_fallback_when_duration_metadata_is_absent(self) -> None:
        before = snapshot(primary=window(5), secondary=window(10))
        after = snapshot(primary=window(6), secondary=window(12))

        self.assertEqual(format_percent_delta(before, after, "5h"), "1%")
        self.assertEqual(format_percent_delta(before, after, "weekly"), "2%")

    def test_runtime_probe_uses_the_same_weekly_only_mapping(self) -> None:
        limits = snapshot(primary=window(55, 10080), secondary=None)["rateLimits"]

        self.assertIsNone(runtime_codex_usage.select_window(limits, "5h"))
        self.assertIs(
            runtime_codex_usage.select_window(limits, "weekly"),
            limits["primary"],
        )


if __name__ == "__main__":
    unittest.main()
