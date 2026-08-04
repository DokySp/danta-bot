"""Tests for run-local news-context construction."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from ...market_news.collector import canonicalize_url, title_hash
from ...market_news.storage import MarketNewsStore
from ..builder import build_news_context, market_status, select_market_items


def market_article(title: str, url: str, published_at: str) -> dict[str, str]:
    return {
        "provider_article_id": url,
        "canonical_url": canonicalize_url(url),
        "title_hash": title_hash(title),
        "title": title,
        "url": url,
        "domain": "example.com",
        "source_country": "",
        "source_language": "",
        "published_at": published_at,
        "collected_at": published_at,
        "classification": "market_news",
    }


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
            duplicate = market_article(
                "Weekend chip news", "https://example.com/chip", "2026-07-18T01:00:00+00:00"
            )
            distinct = market_article(
                "Global sanctions update", "https://example.com/sanctions", "2026-07-19T09:00:00+00:00"
            )
            store.upsert_articles("global", "kis_open_api", [duplicate, distinct])
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

    def test_market_selection_prioritizes_material_titles_without_discarding_others(self) -> None:
        items = [
            {"title": "지역 행사 안내", "source_ids": ["domestic"]},
            {"title": "Federal Reserve interest rate decision", "source_ids": ["global"]},
            {"title": "한국은행 금리 결정", "source_ids": ["domestic"]},
        ]

        selected = select_market_items(items, 2)

        self.assertEqual(
            {item["title"] for item in selected},
            {"Federal Reserve interest rate decision", "한국은행 금리 결정"},
        )
        self.assertEqual(len(select_market_items(items, 3)), 3)

    def test_material_titles_outrank_source_balance(self) -> None:
        items = [
            {"title": "지역 행사 안내", "source_ids": ["domestic"]},
            {"title": "Federal Reserve interest rate decision", "source_ids": ["global"]},
            {"title": "Global inflation update", "source_ids": ["global"]},
        ]

        selected = select_market_items(items, 2)

        self.assertEqual(
            {item["title"] for item in selected},
            {"Federal Reserve interest rate decision", "Global inflation update"},
        )

    def test_database_candidates_are_limited_per_source_before_balancing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            workspace = Path(temp_name)
            db_path = workspace / "memory" / "market-news" / "market-news.sqlite3"
            store = MarketNewsStore(db_path)
            domestic = [
                market_article(
                    f"Domestic market item {index}",
                    f"https://domestic.example/{index}",
                    f"2026-07-19T{12 - index // 60:02d}:{59 - index % 60:02d}:00+00:00",
                )
                for index in range(110)
            ]
            global_items = [
                market_article(
                    "Older global market item",
                    "https://global.example/older",
                    "2026-07-19T09:00:00+00:00",
                )
            ]
            store.upsert_articles("domestic", "kis_open_api", domestic)
            store.upsert_articles("global", "kis_open_api", global_items)
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
