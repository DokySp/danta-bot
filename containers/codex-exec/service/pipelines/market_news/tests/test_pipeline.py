"""Tests for deterministic market-news collection and storage."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import yaml

from ..collector import (
    GdeltRateLimitedError,
    canonicalize_url,
    collect_market_news,
    fetch_gdelt,
    load_config,
    parse_gdelt_articles,
    parse_retry_after,
)
from ..storage import MarketNewsStore


def source_payload(title: str = "Global market update", url: str = "https://example.com/a?utm_source=x") -> dict:
    return {
        "articles": [
            {
                "title": title,
                "url": url,
                "domain": "example.com",
                "sourcecountry": "United States",
                "language": "English",
                "seendate": "20260719T120000Z",
            }
        ]
    }


def write_config(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "provider": "gdelt_doc_2",
                "initial_lookback_hours": 72,
                "max_lookback_hours": 72,
                "overlap_minutes": 30,
                "request": {
                    "base_url": "https://example.invalid/api",
                    "timeout_seconds": 1,
                    "retries": 0,
                    "max_records": 250,
                },
                "sources": [
                    {"id": "domestic", "query": "domestic query"},
                    {"id": "global", "query": "global query"},
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def capped_payload(query: str, window_start: datetime, window_end: datetime, count: int) -> dict:
    window_key = f"{window_start.isoformat()}-{window_end.isoformat()}"
    return {
        "articles": [
            {
                "title": f"{query} {window_key} item {index}",
                "url": f"https://example.com/{query.split()[0]}/{window_start.timestamp():.0f}/{window_end.timestamp():.0f}/{index}",
                "domain": "example.com",
                "sourcecountry": "United States",
                "language": "English",
                "seendate": "20260719T120000Z",
            }
            for index in range(count)
        ]
    }


class MarketNewsPipelineTest(unittest.TestCase):
    def test_base_config_separates_domestic_and_global_source_countries(self) -> None:
        config_path = Path(__file__).resolve().parents[4] / "profiles" / "base" / "config" / "market-news.yaml"
        config = load_config(config_path)
        queries = {str(source["id"]): str(source["query"]) for source in config["sources"]}

        self.assertIn("sourcecountry:southkorea", queries["domestic"])
        self.assertIn("-sourcecountry:southkorea", queries["global"])

    def test_url_and_cross_source_dedup_preserve_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            store = MarketNewsStore(Path(temp_name) / "market.sqlite3")
            collected_at = "2026-07-19T12:05:00+00:00"
            first = parse_gdelt_articles(source_payload(), source_id="domestic", collected_at=collected_at)
            second = parse_gdelt_articles(
                source_payload(url="https://example.com/a?utm_medium=social"),
                source_id="global",
                collected_at=collected_at,
            )
            self.assertEqual(canonicalize_url(first[0]["url"]), "https://example.com/a")
            self.assertEqual(store.upsert_articles("domestic", "gdelt", first).inserted_count, 1)
            self.assertEqual(store.upsert_articles("global", "gdelt", second).duplicate_count, 1)
            rows = store.query_between("2026-07-19T00:00:00+00:00", "2026-07-20T00:00:00+00:00")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_ids"], ["domestic", "global"])

    def test_collect_records_partial_source_failure_without_losing_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "market-news.yaml"
            db_path = root / "market.sqlite3"
            write_config(config_path)

            def fake_fetcher(**kwargs):
                if "global" in kwargs["query"]:
                    raise RuntimeError("HTTP 429")
                return source_payload(title="Korean economy update")

            result = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=datetime(2026, 7, 19, 13, tzinfo=timezone.utc),
                fetcher=fake_fetcher,
                sleep=lambda _seconds: None,
            )
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["inserted_count"], 1)
            statuses = MarketNewsStore(db_path).latest_run_statuses()
            self.assertEqual(statuses["domestic"]["status"], "success")
            self.assertEqual(statuses["global"]["status"], "failed")

    def test_saturated_windows_are_split_before_cursor_advances(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "market-news.yaml"
            db_path = root / "market.sqlite3"
            write_config(config_path)
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config.update(
                {
                    "initial_lookback_hours": 1,
                    "max_lookback_hours": 1,
                    "min_request_window_minutes": 15,
                    "max_requests_per_source": 16,
                }
            )
            config["request"]["max_records"] = 2
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            def fake_fetcher(**kwargs):
                duration_minutes = (kwargs["window_end"] - kwargs["window_start"]).total_seconds() / 60
                count = 2 if duration_minutes > 15 else 1
                return capped_payload(kwargs["query"], kwargs["window_start"], kwargs["window_end"], count)

            result = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=datetime(2026, 7, 19, 13, tzinfo=timezone.utc),
                fetcher=fake_fetcher,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["request_count"], 14)
            self.assertEqual(result["saturated_window_count"], 0)
            store = MarketNewsStore(db_path)
            self.assertEqual(store.get_cursor("domestic"), "2026-07-19T13:00:00+00:00")
            self.assertEqual(store.get_cursor("global"), "2026-07-19T13:00:00+00:00")

    def test_unresolved_provider_cap_is_partial_and_does_not_advance_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "market-news.yaml"
            db_path = root / "market.sqlite3"
            write_config(config_path)
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config.update(
                {
                    "initial_lookback_hours": 1,
                    "max_lookback_hours": 1,
                    "min_request_window_minutes": 60,
                    "max_requests_per_source": 1,
                }
            )
            config["request"]["max_records"] = 2
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            def fake_fetcher(**kwargs):
                return capped_payload(kwargs["query"], kwargs["window_start"], kwargs["window_end"], 2)

            result = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=datetime(2026, 7, 19, 13, tzinfo=timezone.utc),
                fetcher=fake_fetcher,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["saturated_window_count"], 2)
            store = MarketNewsStore(db_path)
            self.assertIsNone(store.get_cursor("domestic"))
            self.assertIsNone(store.get_cursor("global"))
            self.assertEqual(store.latest_run_statuses()["domestic"]["status"], "partial")

    def test_lock_prevents_overlapping_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            store = MarketNewsStore(Path(temp_name) / "market.sqlite3")
            with store.acquire_run_lock() as first:
                self.assertTrue(first)
                with store.acquire_run_lock() as second:
                    self.assertFalse(second)

    def test_read_only_store_does_not_create_a_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            db_path = Path(temp_name) / "missing" / "market.sqlite3"

            with self.assertRaises(FileNotFoundError):
                MarketNewsStore(db_path, read_only=True)

            self.assertFalse(db_path.parent.exists())

    def test_http_429_is_not_retried_and_carries_retry_after(self) -> None:
        error = HTTPError("https://example.invalid", 429, "rate limited", {"Retry-After": "120"}, None)
        with patch("service.pipelines.market_news.collector.request_json", side_effect=[error, source_payload()]) as request:
            delays: list[float] = []
            with self.assertRaises(GdeltRateLimitedError) as ctx:
                fetch_gdelt(
                    request_config={
                        "base_url": "https://example.invalid",
                        "timeout_seconds": 1,
                        "retries": 3,
                        "retry_backoff_seconds": [0.25],
                        "max_records": 10,
                    },
                    query="market",
                    window_start=datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
                    window_end=datetime(2026, 7, 19, 13, tzinfo=timezone.utc),
                    sleep=delays.append,
                )
        self.assertEqual(delays, [])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(ctx.exception.retry_after_seconds, 120.0)

    def test_parse_retry_after_handles_delta_seconds_http_date_and_malformed(self) -> None:
        reference = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_retry_after("120", now=reference), 120.0)
        self.assertAlmostEqual(
            parse_retry_after("Sun, 19 Jul 2026 12:05:00 GMT", now=reference), 300.0
        )
        self.assertIsNone(parse_retry_after("not-a-value", now=reference))
        self.assertIsNone(parse_retry_after("", now=reference))
        self.assertEqual(parse_retry_after("0", now=reference), 0.0)

    def test_parse_retry_after_rejects_non_finite_numeric_values(self) -> None:
        reference = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(parse_retry_after("inf", now=reference))
        self.assertIsNone(parse_retry_after("-inf", now=reference))
        self.assertIsNone(parse_retry_after("nan", now=reference))

    def test_parse_retry_after_rejects_overflowing_http_date(self) -> None:
        reference = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(
            parse_retry_after("Sun, 19 Jul 100000000000 12:00:00 GMT", now=reference)
        )

    def test_parse_retry_after_clamps_past_http_date_to_zero(self) -> None:
        reference = datetime(2026, 7, 19, 12, 0, 0, 500000, tzinfo=timezone.utc)
        self.assertEqual(
            parse_retry_after("Sun, 19 Jul 2026 12:00:00 GMT", now=reference), 0.0
        )
        self.assertEqual(
            parse_retry_after("Sun, 19 Jul 2026 11:00:00 GMT", now=reference), 0.0
        )

    def test_http_429_with_overflowing_http_date_retry_after_falls_back_to_none(self) -> None:
        error = HTTPError(
            "https://example.invalid",
            429,
            "rate limited",
            {"Retry-After": "Sun, 19 Jul 100000000000 12:00:00 GMT"},
            None,
        )
        with patch("service.pipelines.market_news.collector.request_json", side_effect=[error]):
            with self.assertRaises(GdeltRateLimitedError) as ctx:
                fetch_gdelt(
                    request_config={
                        "base_url": "https://example.invalid",
                        "timeout_seconds": 1,
                        "retries": 3,
                        "max_records": 10,
                    },
                    query="market",
                    window_start=datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
                    window_end=datetime(2026, 7, 19, 13, tzinfo=timezone.utc),
                    sleep=lambda _seconds: None,
                )
        self.assertIsNone(ctx.exception.retry_after_seconds)

    def test_overflowing_retry_after_header_stops_remaining_source_and_uses_fallback_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "market-news.yaml"
            db_path = root / "market.sqlite3"
            write_config(config_path)
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["rate_limit_cooldown_seconds"] = 60
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            calls: list[str] = []

            def fake_fetcher(**kwargs):
                calls.append(kwargs["query"])
                raise GdeltRateLimitedError(
                    parse_retry_after("Sun, 19 Jul 100000000000 12:00:00 GMT")
                )

            fixed_now = datetime(2026, 7, 19, 13, 0, 0, tzinfo=timezone.utc)
            result = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=fixed_now,
                fetcher=fake_fetcher,
                sleep=lambda _seconds: None,
                clock=lambda: fixed_now,
            )

            self.assertEqual(result["status"], "skipped_rate_limited")
            self.assertEqual(calls, ["domestic query"])
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["rate_limited_until"], "2026-07-19T13:01:00+00:00")

    def test_precise_clock_preserves_microseconds_and_cooldown_stays_active_until_full_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "market-news.yaml"
            db_path = root / "market.sqlite3"
            write_config(config_path)
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["rate_limit_cooldown_seconds"] = 1
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            rate_limited_at = datetime(2026, 7, 19, 10, 0, 0, 900000, tzinfo=timezone.utc)

            def rate_limited_fetcher(**_kwargs):
                raise GdeltRateLimitedError(None)

            run1 = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=rate_limited_at,
                fetcher=rate_limited_fetcher,
                sleep=lambda _seconds: None,
                clock=lambda: rate_limited_at,
            )
            self.assertEqual(run1["status"], "skipped_rate_limited")
            self.assertEqual(run1["rate_limited_until"], "2026-07-19T10:00:01.900000+00:00")

            def unreachable_fetcher(**_kwargs):
                raise AssertionError("provider must not be called before full cooldown elapses")

            still_within_cooldown = rate_limited_at + timedelta(milliseconds=500)
            run2 = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=still_within_cooldown,
                fetcher=unreachable_fetcher,
                sleep=lambda _seconds: None,
                clock=lambda: still_within_cooldown,
            )
            self.assertEqual(run2["status"], "skipped_rate_limited")
            self.assertEqual(run2["request_count"], 0)

    def test_first_rate_limit_stops_remaining_source_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "market-news.yaml"
            db_path = root / "market.sqlite3"
            write_config(config_path)
            calls: list[str] = []

            def fake_fetcher(**kwargs):
                calls.append(kwargs["query"])
                raise GdeltRateLimitedError(120)

            result = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=datetime(2026, 7, 19, 13, tzinfo=timezone.utc),
                fetcher=fake_fetcher,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(result["status"], "skipped_rate_limited")
            self.assertEqual(calls, ["domestic query"])
            self.assertEqual(result["request_count"], 1)
            self.assertEqual(result["errors"], [])
            self.assertTrue(result["alert"])
            state = MarketNewsStore(db_path).get_provider_state("gdelt_doc_2")
            self.assertEqual(state["status"], "rate_limited")

    def test_cooldown_starts_from_operational_clock_at_429_not_stale_window_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "market-news.yaml"
            db_path = root / "market.sqlite3"
            write_config(config_path)
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["rate_limit_cooldown_seconds"] = 60
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            window_end = datetime(2026, 7, 19, 13, 0, 0, tzinfo=timezone.utc)
            # Simulate provider work taking 2 seconds between run start and the 429 response.
            operational_now_at_429 = window_end + timedelta(seconds=2)

            def fake_fetcher(**_kwargs):
                raise GdeltRateLimitedError(1)

            result = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=window_end,
                fetcher=fake_fetcher,
                sleep=lambda _seconds: None,
                clock=lambda: operational_now_at_429,
            )

            self.assertEqual(result["status"], "skipped_rate_limited")
            expected_deadline = (operational_now_at_429 + timedelta(seconds=1)).isoformat()
            stale_deadline = (window_end + timedelta(seconds=1)).isoformat()
            self.assertEqual(result["rate_limited_until"], expected_deadline)
            self.assertNotEqual(result["rate_limited_until"], stale_deadline)

    def test_genuine_failure_before_429_is_still_reported_and_cooldown_stays_silent_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "market-news.yaml"
            db_path = root / "market.sqlite3"
            write_config(config_path)

            def fake_fetcher(**kwargs):
                if "global" in kwargs["query"]:
                    raise GdeltRateLimitedError(60)
                raise RuntimeError("ordinary source failure")

            result = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=datetime(2026, 7, 19, 13, tzinfo=timezone.utc),
                fetcher=fake_fetcher,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["errors"], ["domestic: ordinary source failure"])
            statuses = {item["source_id"]: item["status"] for item in result["sources"]}
            self.assertEqual(statuses["domestic"], "failed")
            self.assertEqual(statuses["global"], "skipped_rate_limited")
            state = MarketNewsStore(db_path).get_provider_state("gdelt_doc_2")
            self.assertEqual(state["status"], "rate_limited")

            def unreachable_fetcher(**_kwargs):
                raise AssertionError("provider must not be called during active cooldown")

            follow_up = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=datetime(2026, 7, 19, 13, 0, 30, tzinfo=timezone.utc),
                fetcher=unreachable_fetcher,
                sleep=lambda _seconds: None,
            )
            self.assertEqual(follow_up["status"], "skipped_rate_limited")
            self.assertEqual(follow_up["request_count"], 0)

    def test_unrepresentably_large_retry_after_falls_back_to_configured_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "market-news.yaml"
            db_path = root / "market.sqlite3"
            write_config(config_path)
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["rate_limit_cooldown_seconds"] = 60
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            def huge_retry_after_fetcher(**_kwargs):
                raise GdeltRateLimitedError(1e18)

            fixed_now = datetime(2026, 7, 19, 10, 0, 0, tzinfo=timezone.utc)
            result = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=fixed_now,
                fetcher=huge_retry_after_fetcher,
                sleep=lambda _seconds: None,
                clock=lambda: fixed_now,
            )

            self.assertEqual(result["status"], "skipped_rate_limited")
            self.assertEqual(result["rate_limited_until"], "2026-07-19T10:01:00+00:00")

    def test_zero_retry_after_is_honored_instead_of_fallback_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "market-news.yaml"
            db_path = root / "market.sqlite3"
            write_config(config_path)
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["rate_limit_cooldown_seconds"] = 60
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            def zero_retry_after_fetcher(**_kwargs):
                raise GdeltRateLimitedError(0)

            fixed_now = datetime(2026, 7, 19, 10, 0, 0, tzinfo=timezone.utc)
            result = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=fixed_now,
                fetcher=zero_retry_after_fetcher,
                sleep=lambda _seconds: None,
                clock=lambda: fixed_now,
            )

            self.assertEqual(result["status"], "skipped_rate_limited")
            self.assertEqual(result["rate_limited_until"], "2026-07-19T10:00:00+00:00")

    def test_past_http_date_retry_after_is_honored_instead_of_fallback_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "market-news.yaml"
            db_path = root / "market.sqlite3"
            write_config(config_path)
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["rate_limit_cooldown_seconds"] = 60
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            fixed_now = datetime(2026, 7, 19, 10, 0, 0, 500000, tzinfo=timezone.utc)

            def past_http_date_fetcher(**_kwargs):
                raise GdeltRateLimitedError(
                    parse_retry_after(
                        "Sun, 19 Jul 2026 10:00:00 GMT", now=fixed_now
                    )
                )

            result = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=fixed_now,
                fetcher=past_http_date_fetcher,
                sleep=lambda _seconds: None,
                clock=lambda: fixed_now,
            )

            self.assertEqual(result["status"], "skipped_rate_limited")
            self.assertEqual(result["rate_limited_until"], "2026-07-19T10:00:00.500000+00:00")

    def test_completed_source_is_preserved_when_later_source_is_rate_limited(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "market-news.yaml"
            db_path = root / "market.sqlite3"
            write_config(config_path)

            def fake_fetcher(**kwargs):
                if "global" in kwargs["query"]:
                    raise GdeltRateLimitedError(30)
                return source_payload(title="Korean economy update")

            result = collect_market_news(
                config_path=config_path,
                db_path=db_path,
                current_time=datetime(2026, 7, 19, 13, tzinfo=timezone.utc),
                fetcher=fake_fetcher,
                sleep=lambda _seconds: None,
            )

            self.assertEqual(result["status"], "skipped_rate_limited")
            self.assertEqual(result["inserted_count"], 1)
            store = MarketNewsStore(db_path)
            self.assertIsNotNone(store.get_cursor("domestic"))
            self.assertIsNone(store.get_cursor("global"))
            statuses = store.latest_run_statuses()
            self.assertEqual(statuses["domestic"]["status"], "success")
            self.assertEqual(statuses["global"]["status"], "skipped_rate_limited")

    def test_persisted_cooldown_blocks_further_provider_calls_and_alert_resets_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "market-news.yaml"
            db_path = root / "market.sqlite3"
            write_config(config_path)
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["rate_limit_cooldown_seconds"] = 60
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            def rate_limited_fetcher(**_kwargs):
                raise GdeltRateLimitedError(None)

            def unreachable_fetcher(**_kwargs):
                raise AssertionError("provider must not be called during active cooldown")

            def success_fetcher(**kwargs):
                return source_payload(title=f"update {kwargs['query']}")

            t0 = datetime(2026, 7, 19, 10, 0, 0, tzinfo=timezone.utc)

            run1 = collect_market_news(
                config_path=config_path, db_path=db_path, current_time=t0,
                fetcher=rate_limited_fetcher, sleep=lambda _s: None,
                clock=lambda: t0,
            )
            self.assertEqual(run1["status"], "skipped_rate_limited")
            self.assertTrue(run1["alert"])

            run2 = collect_market_news(
                config_path=config_path, db_path=db_path,
                current_time=t0 + timedelta(seconds=30),
                fetcher=unreachable_fetcher, sleep=lambda _s: None,
                clock=lambda: t0 + timedelta(seconds=30),
            )
            self.assertEqual(run2["status"], "skipped_rate_limited")
            self.assertEqual(run2["request_count"], 0)
            self.assertFalse(run2["alert"])

            run3 = collect_market_news(
                config_path=config_path, db_path=db_path,
                current_time=t0 + timedelta(seconds=70),
                fetcher=rate_limited_fetcher, sleep=lambda _s: None,
                clock=lambda: t0 + timedelta(seconds=70),
            )
            self.assertEqual(run3["status"], "skipped_rate_limited")
            self.assertGreater(run3["request_count"], 0)
            self.assertFalse(run3["alert"])

            run4 = collect_market_news(
                config_path=config_path, db_path=db_path,
                current_time=t0 + timedelta(seconds=200),
                fetcher=success_fetcher, sleep=lambda _s: None,
                clock=lambda: t0 + timedelta(seconds=200),
            )
            self.assertEqual(run4["status"], "success")
            self.assertTrue(run4["recovered"])

            run5 = collect_market_news(
                config_path=config_path, db_path=db_path,
                current_time=t0 + timedelta(seconds=205),
                fetcher=rate_limited_fetcher, sleep=lambda _s: None,
                clock=lambda: t0 + timedelta(seconds=205),
            )
            self.assertEqual(run5["status"], "skipped_rate_limited")
            self.assertTrue(run5["alert"])

    def test_non_latin_headline_is_kept_for_global_collection(self) -> None:
        articles = parse_gdelt_articles(
            source_payload(title="日本銀行が政策金利を維持"),
            source_id="global",
            collected_at="2026-07-19T12:05:00+00:00",
        )

        self.assertEqual(len(articles), 1)
        self.assertTrue(articles[0]["title_hash"])


def command_self_test() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(MarketNewsPipelineTest)
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    if not result.wasSuccessful():
        return 1
    print(json.dumps({"status": "ok", "tests_run": result.testsRun}))
    return 0


if __name__ == "__main__":
    raise SystemExit(command_self_test())
