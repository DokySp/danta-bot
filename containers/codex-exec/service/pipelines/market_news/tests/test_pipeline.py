"""Tests for deterministic KIS market-news collection and storage."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from .. import collector
from ..collector import collect_market_news, fetch_kis_source, parse_kis_rows
from ..storage import MarketNewsStore


DOMESTIC_ROW = {
    "cntt_usiq_srno": "100",
    "data_dt": "20260804",
    "data_tm": "093000",
    "hts_pbnt_titl_cntt": "<b>한국은행 금리 발표</b>",
    "news_lrdv_code": "01",
    "dorg": "연합뉴스",
}
GLOBAL_ROW = {
    "news_key": "200",
    "data_dt": "20260804",
    "data_tm": "103000",
    "class_name": "Economy",
    "source": "Reuters",
    "nation_cd": "US",
    "title": "Federal Reserve rate decision",
}


class MarketNewsPipelineTest(unittest.TestCase):
    def test_kis_rows_map_to_existing_article_contract(self) -> None:
        domestic = parse_kis_rows(
            [DOMESTIC_ROW], source_id="domestic", collected_at="2026-08-04T01:00:00+00:00"
        )[0]
        global_item = parse_kis_rows(
            [GLOBAL_ROW], source_id="global", collected_at="2026-08-04T02:00:00+00:00"
        )[0]

        self.assertEqual(domestic["title"], "한국은행 금리 발표")
        self.assertEqual(domestic["published_at"], "2026-08-04T00:30:00+00:00")
        self.assertEqual(domestic["provider_article_id"], "domestic:100")
        self.assertEqual(global_item["domain"], "Reuters")
        self.assertEqual(global_item["source_country"], "US")
        self.assertEqual(global_item["classification"], "Economy")

    def test_missing_domestic_ids_fall_back_to_distinct_title_hashes(self) -> None:
        rows = [
            {**DOMESTIC_ROW, "cntt_usiq_srno": None, "hts_pbnt_titl_cntt": title}
            for title in ("첫 번째 뉴스", "두 번째 뉴스")
        ]

        articles = parse_kis_rows(
            rows, source_id="domestic", collected_at="2026-08-04T01:00:00+00:00"
        )

        self.assertEqual(len({item["provider_article_id"] for item in articles}), 2)
        self.assertTrue(all(item["provider_article_id"].startswith("domestic:") for item in articles))

    def test_domestic_request_uses_official_kis_contract(self) -> None:
        with patch.object(
            collector.symbol_news,
            "retry_json",
            return_value=({"rt_cd": "0", "output": [DOMESTIC_ROW]}, {}),
        ) as request:
            rows, request_count = fetch_kis_source(
                "domestic", app_key="key", app_secret="secret", token="token"
            )

        self.assertEqual(rows, [DOMESTIC_ROW])
        self.assertEqual(request_count, 1)
        self.assertEqual(request.call_args.args[:2], ("GET", "/uapi/domestic-stock/v1/quotations/news-title"))
        self.assertEqual(request.call_args.kwargs["headers"]["tr_id"], "FHKST01011800")
        self.assertEqual(request.call_args.kwargs["params"]["FID_INPUT_ISCD"], "")

    def test_global_request_follows_kis_continuation_header(self) -> None:
        second = {**GLOBAL_ROW, "news_key": "201", "title": "Global inflation update"}
        with patch.object(
            collector.symbol_news,
            "retry_json",
            side_effect=[
                ({"rt_cd": "0", "outblock1": [GLOBAL_ROW]}, {"tr_cont": "M"}),
                ({"rt_cd": "0", "outblock1": [second]}, {}),
            ],
        ) as request:
            rows, request_count = fetch_kis_source(
                "global", app_key="key", app_secret="secret", token="token"
            )

        self.assertEqual(rows, [GLOBAL_ROW, second])
        self.assertEqual(request_count, 2)
        self.assertEqual(request.call_args_list[0].args[:2], ("GET", "/uapi/overseas-price/v1/quotations/news-title"))
        self.assertEqual(request.call_args_list[0].kwargs["headers"]["tr_id"], "HHPSTH60100C1")
        self.assertEqual(request.call_args_list[1].kwargs["headers"]["tr_cont"], "N")

    def test_one_source_failure_keeps_the_other_source_usable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            db_path = Path(temp_name) / "market.sqlite3"

            def fetch(source_id: str):
                if source_id == "global":
                    raise RuntimeError("overseas unavailable")
                return [DOMESTIC_ROW], 1

            result = collect_market_news(
                db_path=db_path,
                current_time=datetime(2026, 8, 4, 2, tzinfo=timezone.utc),
                fetcher=fetch,
            )

            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["inserted_count"], 1)
            statuses = MarketNewsStore(db_path).latest_run_statuses()
            self.assertEqual(statuses["domestic"]["status"], "success")
            self.assertEqual(statuses["global"]["status"], "failed")

    def test_cross_source_title_dedup_preserves_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            db_path = Path(temp_name) / "market.sqlite3"
            same_global = {**GLOBAL_ROW, "title": "한국은행 금리 발표"}

            def fetch(source_id: str):
                return ([DOMESTIC_ROW] if source_id == "domestic" else [same_global]), 1

            result = collect_market_news(
                db_path=db_path,
                current_time=datetime(2026, 8, 4, 2, tzinfo=timezone.utc),
                fetcher=fetch,
            )
            rows = MarketNewsStore(db_path).query_between(
                "2026-08-04T00:00:00+00:00", "2026-08-04T03:00:00+00:00"
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["inserted_count"], 1)
            self.assertEqual(rows[0]["source_ids"], ["domestic", "global"])

    def test_lock_prevents_overlapping_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            store = MarketNewsStore(Path(temp_name) / "market.sqlite3")
            with store.acquire_run_lock() as first:
                self.assertTrue(first)
                with store.acquire_run_lock() as second:
                    self.assertFalse(second)


def command_self_test() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(MarketNewsPipelineTest)
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    if not result.wasSuccessful():
        return 1
    print(json.dumps({"status": "ok", "tests_run": result.testsRun}))
    return 0


if __name__ == "__main__":
    raise SystemExit(command_self_test())
