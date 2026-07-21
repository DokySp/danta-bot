"""Tests for KIS news cache collection and serialization."""

from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from news_cache import (  # noqa: E402
    QuotedString,
    canonical_cache,
    load_symbols,
    merge_cache,
    news_cache_path,
    normalize_date,
    row_date,
    row_text,
    write_yaml,
)


def command_self_test(_args: argparse.Namespace) -> int:
    date_hyphen = normalize_date("20260610")
    assert date_hyphen == "2026-06-10"
    assert news_cache_path(date_hyphen).name == "news-2026-06-10.yaml"
    item = {
        "hts_titl_cntt": "삼성전자 수주 증가",
        "data_dt": "20260610",
        "data_tm": "093000",
        "iscd1": "005930",
        "kor_isnm1": "삼성전자",
    }
    assert row_date(item) == "2026-06-10T09:30:00+09:00"
    assert row_text(item) == "삼성전자 수주 증가"
    cache = merge_cache(date_hyphen, Path("/tmp/nonexistent-news-cache.yaml"), [("005930", "삼성전자", [item], [])])
    assert list(cache["symbols"]["005930"].keys()) == ["symbol_name", "articles"]
    assert set(cache["symbols"]["005930"]["articles"][0]) == {"article_date", "content"}
    assert cache["symbols"]["005930"]["articles"][0]["article_date"] == "2026-06-10T09:30:00+09:00"
    assert cache["symbols"]["005930"]["symbol_name"] == "삼성전자"
    assert "symbol_id" not in cache["symbols"]["005930"]
    assert "updated_at" not in cache["symbols"]["005930"]
    assert "errors" not in cache["symbols"]["005930"]
    assert "updated_at" not in cache
    existing = {
        "date": date_hyphen,
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
    temp = Path(os.environ.get("TMPDIR", "/tmp")) / "collect-news-information-self-test.yaml"
    write_yaml(temp, existing)
    merged = merge_cache(date_hyphen, temp, [("005930", "삼성전자", [item], [])])
    assert "000660" in merged["symbols"]
    assert "005930" in merged["symbols"]
    assert merged["symbols"]["005930"]["symbol_name"] == "삼성전자"
    assert "symbol_name" not in merged["symbols"]["000660"]
    assert set(merged["symbols"]["000660"]["articles"][0]) == {"article_date", "content"}
    assert merged["symbols"]["000660"]["articles"][0]["article_date"] == ""
    assert "updated_at" not in merged
    assert "title" not in merged
    assert "symbol_id" not in merged
    assert "errors" not in merged
    assert "symbol_id" not in merged["symbols"]["000660"]
    assert "updated_at" not in merged["symbols"]["000660"]
    assert "errors" not in merged["symbols"]["000660"]
    canonical = canonical_cache(merged)
    assert list(canonical.keys()) == ["date", "source", "symbols"]
    assert all(isinstance(symbol_id, QuotedString) for symbol_id in canonical["symbols"])
    assert list(canonical["symbols"]["005930"].keys()) == ["symbol_name", "articles"]
    assert list(canonical["symbols"]["000660"].keys()) == ["articles"]
    write_yaml(temp, merged)
    written = temp.read_text(encoding="utf-8")
    assert '  "000660":' in written
    assert '  "005930":' in written
    symbol_block = written[written.index('  "005930":') :]
    assert symbol_block.index("    symbol_name: 삼성전자") < symbol_block.index("    articles:")
    temp.unlink(missing_ok=True)
    namespace = argparse.Namespace(symbols=["005930,000660"], symbol=None, symbols_file=None)
    assert load_symbols(namespace) == [("005930", "005930"), ("000660", "000660")]
    print("self-test ok")
    return 0


class NewsCacheSelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        self.assertEqual(command_self_test(argparse.Namespace()), 0)
