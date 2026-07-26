#!/usr/bin/env python3
"""Consistency check between the fixed full-review times and schedules.yaml.

`daily-trading-full-review-times.yaml` must list exactly the 7 KST times that
are due a full review; the other 5 weekday daily-trading invocation times
(09:20, 09:40, 10:30, 11:00, 12:00) must normally end after preflight.
schedules.yaml itself must not be touched by this feature -- this test only
reads it.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
SCHEDULES_PATH = REPO_ROOT / "profiles/base/config/schedules.yaml"
FULL_REVIEW_TIMES_PATH = REPO_ROOT / "profiles/base/config/daily-trading-full-review-times.yaml"

EXPECTED_NON_FULL_WEEKDAY_TIMES = {"09:20", "09:40", "10:30", "11:00", "12:00"}


def parse_cron_field(field: str) -> list[int]:
    values: list[int] = []
    for part in field.split(","):
        values.append(int(part))
    return values


def weekday_daily_trading_times(schedules: list[dict]) -> set[str]:
    """HH:MM times of every weekday (1-5) daily_trading schedule entry."""
    times: set[str] = set()
    for item in schedules:
        if not isinstance(item, dict) or item.get("daily_trading") is None:
            continue
        cron = str(item.get("cron") or "").strip()
        parts = cron.split()
        if len(parts) != 5:
            continue
        minute_field, hour_field, dom, month, dow = parts
        if dom != "*" or month != "*" or dow != "1-5":
            continue
        for hour in parse_cron_field(hour_field):
            for minute in parse_cron_field(minute_field):
                times.add(f"{hour:02d}:{minute:02d}")
    return times


class FullReviewTimesScheduleConsistencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schedules = yaml.safe_load(SCHEDULES_PATH.read_text(encoding="utf-8"))["schedules"]
        self.full_review_times = yaml.safe_load(FULL_REVIEW_TIMES_PATH.read_text(encoding="utf-8"))["full_review_times"]

    def test_weekday_daily_trading_schedule_has_exactly_twelve_invocations(self) -> None:
        self.assertEqual(len(weekday_daily_trading_times(self.schedules)), 12)

    def test_fixed_full_review_times_are_a_subset_of_the_weekday_schedule(self) -> None:
        weekday_times = weekday_daily_trading_times(self.schedules)
        self.assertTrue(set(self.full_review_times).issubset(weekday_times))

    def test_fixed_full_review_times_match_the_seven_specified_times(self) -> None:
        self.assertEqual(
            self.full_review_times,
            ["07:00", "09:05", "10:00", "11:30", "13:00", "14:00", "15:15"],
        )

    def test_remaining_weekday_times_are_the_five_non_full_times(self) -> None:
        weekday_times = weekday_daily_trading_times(self.schedules)
        remaining = weekday_times - set(self.full_review_times)
        self.assertEqual(remaining, EXPECTED_NON_FULL_WEEKDAY_TIMES)


if __name__ == "__main__":
    unittest.main()
