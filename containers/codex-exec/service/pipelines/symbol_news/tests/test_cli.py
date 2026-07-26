"""Tests for KIS symbol-news cache collection and serialization.

`command_self_test` is the compatibility body invoked by the production
CLI's `self-test` command and must keep printing `"self-test ok"` and
returning `0`. Each logical block of the old monolithic self-test now
lives in its own `scenario_*` (setup + act) or `check_*` (single
assertion concern, reusable from a plain function or a `TestCase`
method) helper. `command_self_test` and the granular `TestCase` methods
below both call those helpers, so each behavior has exactly one
implementation. The wrapper-orchestration test mocks the helpers rather
than re-running every scenario, so discovery does not execute the real
work twice.
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ..cli import (
    QuotedString,
    canonical_cache,
    load_symbols,
    merge_cache,
    normalize_date,
    row_date,
    row_text,
    symbol_news_cache_path,
    write_yaml,
)

SAMPLE_DATE_HYPHEN = "2026-06-10"
SAMPLE_ITEM = {
    "hts_titl_cntt": "삼성전자 수주 증가",
    "data_dt": "20260610",
    "data_tm": "093000",
    "iscd1": "005930",
    "kor_isnm1": "삼성전자",
}
EXISTING_CACHE = {
    "date": SAMPLE_DATE_HYPHEN,
    "source": "kis_open_api",
    "updated_at": "old",
    "title": "old",
    "symbol_id": "old",
    "errors": ["old"],
    "symbols": {
        660: {
            "symbol_id": "000660",
            "symbol_name": "000660",
            "updated_at": "old",
            "articles": [{"title": "기사", "article_date": None, "sentiment": "중립", "content": "old"}],
            "errors": ["old"],
        },
        "005930": {"symbol_name": "OLD", "articles": []},
    },
}


def check_normalize_date_converts_to_hyphenated_form() -> None:
    if normalize_date("20260610") != SAMPLE_DATE_HYPHEN:
        raise AssertionError("normalize_date did not convert to YYYY-MM-DD form")


def check_cache_path_uses_hyphenated_date() -> None:
    name = symbol_news_cache_path(SAMPLE_DATE_HYPHEN).name
    if name != "symbol-news-2026-06-10.yaml":
        raise AssertionError(f"unexpected cache file name: {name}")


def check_row_date_and_text_extraction() -> None:
    if row_date(SAMPLE_ITEM) != "2026-06-10T09:30:00+09:00":
        raise AssertionError(f"unexpected row_date: {row_date(SAMPLE_ITEM)}")
    if row_text(SAMPLE_ITEM) != "삼성전자 수주 증가":
        raise AssertionError(f"unexpected row_text: {row_text(SAMPLE_ITEM)}")


def scenario_fresh_merge() -> dict:
    """Merge a single symbol into a cache path that does not exist yet."""
    return merge_cache(
        SAMPLE_DATE_HYPHEN,
        Path("/tmp/nonexistent-symbol-news-cache.yaml"),
        [("005930", "삼성전자", [SAMPLE_ITEM], [])],
    )


def check_fresh_merge_symbol_shape(cache: dict) -> None:
    entry = cache["symbols"]["005930"]
    if list(entry.keys()) != ["symbol_name", "articles"]:
        raise AssertionError(f"unexpected symbol keys: {list(entry.keys())}")
    if set(entry["articles"][0]) != {"article_date", "content"}:
        raise AssertionError(f"unexpected article keys: {set(entry['articles'][0])}")
    if entry["articles"][0]["article_date"] != "2026-06-10T09:30:00+09:00":
        raise AssertionError(f"unexpected article_date: {entry['articles'][0]['article_date']}")
    if entry["symbol_name"] != "삼성전자":
        raise AssertionError(f"unexpected symbol_name: {entry['symbol_name']}")


def check_fresh_merge_omits_legacy_fields(cache: dict) -> None:
    entry = cache["symbols"]["005930"]
    for legacy_field in ("symbol_id", "updated_at", "errors"):
        if legacy_field in entry:
            raise AssertionError(f"unexpected legacy field {legacy_field!r} in merged symbol: {entry}")
    if "updated_at" in cache:
        raise AssertionError(f"unexpected top-level updated_at: {cache}")


def scenario_merge_into_existing_cache(cache_path: Path) -> dict:
    """Write a legacy-shaped cache to disk, then merge a fresh symbol into it."""
    write_yaml(cache_path, EXISTING_CACHE)
    return merge_cache(SAMPLE_DATE_HYPHEN, cache_path, [("005930", "삼성전자", [SAMPLE_ITEM], [])])


def check_merge_keeps_untouched_symbol(merged: dict) -> None:
    if "000660" not in merged["symbols"]:
        raise AssertionError(f"untouched symbol 000660 dropped from merge: {merged}")
    if set(merged["symbols"]["000660"]["articles"][0]) != {"article_date", "content"}:
        raise AssertionError(f"unexpected untouched article shape: {merged['symbols']['000660']}")
    if merged["symbols"]["000660"]["articles"][0]["article_date"] != "":
        raise AssertionError("untouched article missing article_date defaulted to empty string")
    if "symbol_name" in merged["symbols"]["000660"]:
        raise AssertionError("untouched symbol unexpectedly gained a symbol_name")


def check_merge_updates_target_symbol(merged: dict) -> None:
    if "005930" not in merged["symbols"]:
        raise AssertionError(f"merged symbol 005930 missing: {merged}")
    if merged["symbols"]["005930"]["symbol_name"] != "삼성전자":
        raise AssertionError(f"merged symbol_name not refreshed: {merged['symbols']['005930']}")


def check_merge_strips_legacy_fields(merged: dict) -> None:
    for legacy_field in ("updated_at", "title", "symbol_id", "errors"):
        if legacy_field in merged:
            raise AssertionError(f"unexpected legacy top-level field {legacy_field!r}: {merged}")
    for legacy_field in ("symbol_id", "updated_at", "errors"):
        if legacy_field in merged["symbols"]["000660"]:
            raise AssertionError(f"unexpected legacy field {legacy_field!r} on untouched symbol: {merged}")


def check_canonical_cache_ordering(merged: dict) -> None:
    canonical = canonical_cache(merged)
    if list(canonical.keys()) != ["date", "source", "symbols"]:
        raise AssertionError(f"unexpected canonical top-level order: {list(canonical.keys())}")
    if not all(isinstance(symbol_id, QuotedString) for symbol_id in canonical["symbols"]):
        raise AssertionError(f"expected every symbol id to be quoted: {canonical['symbols']}")
    if list(canonical["symbols"]["005930"].keys()) != ["symbol_name", "articles"]:
        raise AssertionError(f"unexpected 005930 field order: {canonical['symbols']['005930']}")
    if list(canonical["symbols"]["000660"].keys()) != ["articles"]:
        raise AssertionError(f"unexpected 000660 field order: {canonical['symbols']['000660']}")


def scenario_write_yaml_rendering(cache_path: Path, merged: dict) -> str:
    write_yaml(cache_path, merged)
    return cache_path.read_text(encoding="utf-8")


def check_written_yaml_includes_both_symbols(written: str) -> None:
    if '  "000660":' not in written:
        raise AssertionError(f"written yaml missing 000660 block: {written}")
    if '  "005930":' not in written:
        raise AssertionError(f"written yaml missing 005930 block: {written}")


def check_written_yaml_field_order_within_symbol(written: str) -> None:
    symbol_block = written[written.index('  "005930":') :]
    if symbol_block.index("    symbol_name: 삼성전자") >= symbol_block.index("    articles:"):
        raise AssertionError(f"symbol_name did not precede articles in rendered yaml: {symbol_block}")


def check_load_symbols_parses_comma_separated_list() -> None:
    namespace = argparse.Namespace(symbols=["005930,000660"], symbol=None, symbols_file=None)
    parsed = load_symbols(namespace)
    if parsed != [("005930", "005930"), ("000660", "000660")]:
        raise AssertionError(f"unexpected load_symbols result: {parsed}")


def command_self_test() -> int:
    check_normalize_date_converts_to_hyphenated_form()
    check_cache_path_uses_hyphenated_date()
    check_row_date_and_text_extraction()

    fresh_cache = scenario_fresh_merge()
    check_fresh_merge_symbol_shape(fresh_cache)
    check_fresh_merge_omits_legacy_fields(fresh_cache)

    with tempfile.TemporaryDirectory() as temp_dir:
        cache_path = Path(temp_dir) / "symbol-news-self-test.yaml"
        merged = scenario_merge_into_existing_cache(cache_path)
        check_merge_keeps_untouched_symbol(merged)
        check_merge_updates_target_symbol(merged)
        check_merge_strips_legacy_fields(merged)
        check_canonical_cache_ordering(merged)

        written = scenario_write_yaml_rendering(cache_path, merged)
        check_written_yaml_includes_both_symbols(written)
        check_written_yaml_field_order_within_symbol(written)

    check_load_symbols_parses_comma_separated_list()
    print("self-test ok")
    return 0


class SymbolNewsCliSelfTest(unittest.TestCase):
    def test_self_test_suite_runs_every_check_and_reports_success(self) -> None:
        """Wrapper-orchestration check only: real behavior is covered by the
        granular tests below, so this mocks every helper instead of
        re-running the whole scenario a second time."""
        helper_names = [
            "check_normalize_date_converts_to_hyphenated_form",
            "check_cache_path_uses_hyphenated_date",
            "check_row_date_and_text_extraction",
            "scenario_fresh_merge",
            "check_fresh_merge_symbol_shape",
            "check_fresh_merge_omits_legacy_fields",
            "scenario_merge_into_existing_cache",
            "check_merge_keeps_untouched_symbol",
            "check_merge_updates_target_symbol",
            "check_merge_strips_legacy_fields",
            "check_canonical_cache_ordering",
            "scenario_write_yaml_rendering",
            "check_written_yaml_includes_both_symbols",
            "check_written_yaml_field_order_within_symbol",
            "check_load_symbols_parses_comma_separated_list",
        ]
        patchers = [patch(f"{__name__}.{name}") for name in helper_names]
        mocks = [patcher.start() for patcher in patchers]
        self.addCleanup(lambda: [patcher.stop() for patcher in patchers])

        result = command_self_test()

        self.assertEqual(result, 0)
        for mock in mocks:
            mock.assert_called()


class NormalizeDateAndCachePathTest(unittest.TestCase):
    def test_normalize_date_converts_to_hyphenated_form(self) -> None:
        check_normalize_date_converts_to_hyphenated_form()

    def test_cache_path_uses_hyphenated_date(self) -> None:
        check_cache_path_uses_hyphenated_date()

    def test_row_date_and_text_extraction(self) -> None:
        check_row_date_and_text_extraction()


class FreshMergeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cache = scenario_fresh_merge()

    def test_fresh_merge_symbol_shape(self) -> None:
        check_fresh_merge_symbol_shape(self.cache)

    def test_fresh_merge_omits_legacy_fields(self) -> None:
        check_fresh_merge_omits_legacy_fields(self.cache)


class MergeIntoExistingCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.cache_path = Path(self._temp_dir.name) / "symbol-news-existing.yaml"
        self.merged = scenario_merge_into_existing_cache(self.cache_path)

    def test_merge_keeps_untouched_symbol(self) -> None:
        check_merge_keeps_untouched_symbol(self.merged)

    def test_merge_updates_target_symbol(self) -> None:
        check_merge_updates_target_symbol(self.merged)

    def test_merge_strips_legacy_fields(self) -> None:
        check_merge_strips_legacy_fields(self.merged)

    def test_canonical_cache_ordering(self) -> None:
        check_canonical_cache_ordering(self.merged)

    def test_written_yaml_includes_both_symbols(self) -> None:
        written = scenario_write_yaml_rendering(self.cache_path, self.merged)
        check_written_yaml_includes_both_symbols(written)

    def test_written_yaml_field_order_within_symbol(self) -> None:
        written = scenario_write_yaml_rendering(self.cache_path, self.merged)
        check_written_yaml_field_order_within_symbol(written)


class LoadSymbolsTest(unittest.TestCase):
    def test_load_symbols_parses_comma_separated_list(self) -> None:
        check_load_symbols_parses_comma_separated_list()


if __name__ == "__main__":
    unittest.main()
