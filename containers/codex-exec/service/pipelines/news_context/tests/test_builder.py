"""Tests for run-local news-context construction."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from ...market_news.collector import parse_gdelt_articles
from ...market_news.storage import MarketNewsStore
from ..builder import build_news_context, market_status, select_market_items


class NewsContextBuilderTest(unittest.TestCase):
    def test_friday_to_sunday_window_and_cross_scope_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            workspace = Path(temp_name)
            prior = workspace / "reports" / "runs" / "friday"
            prior.mkdir(parents=True)
            (prior / "run.json").write_text(
                json.dumps({"status": "success", "started_at": "2026-07-17T15:15:00+09:00"}),
                encoding="utf-8",
            )
            (prior / "pipeline-summary.json").write_text(
                json.dumps({"status": "success"}),
                encoding="utf-8",
            )
            incomplete = workspace / "reports" / "runs" / "saturday-incomplete"
            incomplete.mkdir(parents=True)
            (incomplete / "run.json").write_text(
                json.dumps({"status": "success", "started_at": "2026-07-18T12:00:00+09:00"}),
                encoding="utf-8",
            )
            symbol_path = workspace / "memory" / "symbol-news-cache" / "symbol-news-2026-07-19.yaml"
            symbol_path.parent.mkdir(parents=True)
            symbol_path.write_text(
                yaml.safe_dump(
                    {
                        "date": "2026-07-19",
                        "symbols": {
                            "005930": {
                                "symbol_name": "삼성전자",
                                "articles": [
                                    {"article_date": "2026-07-18T10:00:00+09:00", "content": "Weekend chip news"}
                                ],
                            }
                        },
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            db_path = workspace / "memory" / "market-news" / "market-news.sqlite3"
            store = MarketNewsStore(db_path)
            duplicate = parse_gdelt_articles(
                {"articles": [{"title": "Weekend chip news", "url": "https://example.com/chip", "seendate": "20260718T010000Z"}]},
                source_id="global",
                collected_at="2026-07-18T01:05:00+00:00",
            )
            distinct = parse_gdelt_articles(
                {"articles": [{"title": "Global sanctions update", "url": "https://example.com/sanctions", "seendate": "20260719T090000Z"}]},
                source_id="global",
                collected_at="2026-07-19T09:05:00+00:00",
            )
            store.upsert_articles("global", "gdelt", duplicate + distinct)
            store.record_run(
                source_id="global",
                started_at="2026-07-19T09:05:00+00:00",
                finished_at="2026-07-19T09:05:01+00:00",
                status="success",
                window_start="2026-07-17T06:15:00+00:00",
                window_end="2026-07-19T13:00:00+00:00",
                fetched_count=2,
                inserted_count=2,
                duplicate_count=0,
            )
            context = build_news_context(
                workspace_dir=workspace,
                current_run_id="sunday",
                current_started_at="2026-07-19T22:00:00+09:00",
                symbol_news_cache_path=symbol_path,
                market_news_db_path=db_path,
            )
            self.assertEqual(context["window_start"], "2026-07-17T06:15:00+00:00")
            self.assertEqual(context["window_source"], "previous_daily_trading_run")
            self.assertEqual(context["deduplicated_count"], 1)
            self.assertEqual(context["symbol_news"]["items"][0]["scopes"], ["market_news", "symbol_news"])
            self.assertEqual(context["market_news"]["selected_count"], 1)
            self.assertEqual(context["market_news"]["items"][0]["title"], "Global sanctions update")

    def test_missing_database_is_non_blocking_partial_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            workspace = Path(temp_name)
            context = build_news_context(
                workspace_dir=workspace,
                current_run_id="run",
                current_started_at="2026-07-19T22:00:00+09:00",
                symbol_news_cache_path=None,
                market_news_db_path=workspace / "missing.sqlite3",
            )
            self.assertEqual(context["status"], "partial")
            self.assertEqual(context["market_news"]["status"], "missing")
            self.assertEqual(context["window_source"], "fallback_lookback")

    def test_market_selection_balances_domestic_and_global_sources(self) -> None:
        items = [
            {"title": f"global-{index}", "source_ids": ["global"]}
            for index in range(5)
        ] + [{"title": "domestic", "source_ids": ["domestic"]}]

        selected = select_market_items(items, 2)

        self.assertEqual({item["title"] for item in selected}, {"global-0", "domestic"})

    def test_database_candidates_are_limited_per_source_before_balancing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            workspace = Path(temp_name)
            db_path = workspace / "memory" / "market-news" / "market-news.sqlite3"
            store = MarketNewsStore(db_path)
            domestic = parse_gdelt_articles(
                {
                    "articles": [
                        {
                            "title": f"Domestic market item {index}",
                            "url": f"https://domestic.example/{index}",
                            "seendate": f"20260719T{12 - index // 60:02d}{59 - index % 60:02d}00Z",
                        }
                        for index in range(110)
                    ]
                },
                source_id="domestic",
                collected_at="2026-07-19T13:00:00+00:00",
            )
            global_items = parse_gdelt_articles(
                {
                    "articles": [
                        {
                            "title": "Older global market item",
                            "url": "https://global.example/older",
                            "seendate": "20260719T090000Z",
                        }
                    ]
                },
                source_id="global",
                collected_at="2026-07-19T13:00:00+00:00",
            )
            store.upsert_articles("domestic", "gdelt", domestic)
            store.upsert_articles("global", "gdelt", global_items)
            for source_id in ("domestic", "global"):
                store.record_run(
                    source_id=source_id,
                    started_at="2026-07-19T13:00:00+00:00",
                    finished_at="2026-07-19T13:00:01+00:00",
                    status="success",
                    window_start="2026-07-16T13:00:00+00:00",
                    window_end="2026-07-19T13:00:00+00:00",
                    fetched_count=1,
                    inserted_count=1,
                    duplicate_count=0,
                )

            context = build_news_context(
                workspace_dir=workspace,
                current_run_id="current",
                current_started_at="2026-07-19T22:00:00+09:00",
                symbol_news_cache_path=None,
                market_news_db_path=db_path,
                max_market_items=2,
            )

            selected_sources = {
                source_id
                for item in context["market_news"]["items"]
                for source_id in item["source_ids"]
            }
            self.assertEqual(selected_sources, {"domestic", "global"})

    def test_stored_items_remain_partial_usable_when_latest_sources_fail(self) -> None:
        status = market_status(
            {"domestic": {"status": "failed"}, "global": {"status": "failed"}},
            [{"title": "stored prior article"}],
            True,
        )

        self.assertEqual(status, "partial")


def command_self_test() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NewsContextBuilderTest)
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    if not result.wasSuccessful():
        return 1
    print(json.dumps({"status": "ok", "tests_run": result.testsRun}))
    return 0


if __name__ == "__main__":
    raise SystemExit(command_self_test())
