"""Tests for deterministic market-news collection and storage."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import yaml

from ..collector import (
    canonicalize_url,
    collect_market_news,
    fetch_gdelt,
    load_config,
    parse_gdelt_articles,
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

    def test_http_429_is_retried_with_configured_backoff(self) -> None:
        error = HTTPError("https://example.invalid", 429, "rate limited", {}, None)
        with patch("service.pipelines.market_news.collector.request_json", side_effect=[error, source_payload()]) as request:
            delays: list[float] = []
            payload = fetch_gdelt(
                request_config={
                    "base_url": "https://example.invalid",
                    "timeout_seconds": 1,
                    "retries": 1,
                    "retry_backoff_seconds": [0.25],
                    "max_records": 10,
                },
                query="market",
                window_start=datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
                window_end=datetime(2026, 7, 19, 13, tzinfo=timezone.utc),
                sleep=delays.append,
            )
        self.assertEqual(len(payload["articles"]), 1)
        self.assertEqual(delays, [0.25])
        self.assertEqual(request.call_count, 2)

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
