"""Tests for KIS financial cache collection and serialization."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from financial_cache import (  # noqa: E402
    api_params,
    cache_sidecar_path,
    canonical_cache,
    command_collect,
    financial_cache_path,
    load_symbols,
    merge_cache,
    normalize_date,
    write_source_fields_yaml,
    write_yaml,
)


def command_self_test(_args: argparse.Namespace) -> int:
    date_hyphen = normalize_date("20260610")
    assert date_hyphen == "2026-06-10"
    assert financial_cache_path(date_hyphen).name == "financial-2026-06-10.yaml"
    namespace = argparse.Namespace(
        symbols=["005930,000660"],
        symbol=None,
        symbols_file=None,
        product_type="300",
        market="J",
        start_date=None,
        include_etf=True,
    )
    assert load_symbols(namespace) == [("005930", "005930"), ("000660", "000660")]
    empty_namespace = argparse.Namespace(
        date=date_hyphen,
        symbols=None,
        symbol=None,
        symbols_file=None,
        product_type="300",
        market="J",
        start_date=None,
        include_etf=False,
        retries=0,
        max_pages=1,
    )
    assert load_symbols(empty_namespace, require=False) == []
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert command_collect(empty_namespace) == 0
    assert stdout.getvalue().strip().endswith("financial-2026-06-10.yaml")
    assert api_params("estimate_perform", "005930", namespace, date_hyphen) == {"SHT_CD": "005930"}
    assert api_params("invest_opinion", "005930", namespace, date_hyphen)["FID_COND_SCR_DIV_CODE"] == "16633"
    assert api_params("invest_opinion", "005930", namespace, date_hyphen)["FID_INPUT_DATE_1"] == "20260513"
    assert api_params("invest_opinion", "005930", namespace, date_hyphen)["FID_INPUT_DATE_2"] == "20260610"
    cache = merge_cache(
        date_hyphen,
        Path("/tmp/nonexistent-financial-cache.yaml"),
        [
            (
                "005930",
                {
                    "symbol_name": "삼성전자",
                    "apis": {
                        "estimate_perform": {
                            "output1": [{"stck_bsop_date": "20260610", "eps": 1234}],
                            "output2": [{"data1": 100, "data2": 200}],
                            "output4": [{"dt": "2025.12"}, {"dt": "2026.12E"}],
                        },
                        "inquire_price": {"output": {"stck_prpr": "80000"}},
                    },
                },
            )
        ],
    )
    canonical = canonical_cache(cache)
    assert list(canonical.keys()) == ["date", "source", "symbols"]
    assert list(canonical["symbols"]["005930"].keys()) == ["symbol_name", "apis"]
    assert "estimate_perform" in canonical["symbols"]["005930"]["apis"]
    estimate_api = canonical["symbols"]["005930"]["apis"]["estimate_perform"]
    assert estimate_api["api_name"] == "국내주식 종목추정실적"
    assert len(estimate_api["outputs"]) == 1
    estimate_output = estimate_api["outputs"][0]
    assert estimate_output["output_name"] == "종목 및 최신 투자의견 요약"
    estimate_fields = estimate_output["rows"][0]["fields"]
    assert estimate_fields["주식 영업일자"] == "20260610"
    assert estimate_fields["주당순이익(EPS)"] == "1234"
    estimate_source_fields = estimate_output["rows"][0]["source_fields"]
    assert estimate_source_fields["주식 영업일자"] == "stck_bsop_date"
    assert estimate_source_fields["주당순이익(EPS)"] == "eps"
    price_api = canonical["symbols"]["005930"]["apis"]["inquire_price"]
    price_fields = price_api["outputs"][0]["rows"][0]["fields"]
    assert price_fields["현재가"] == "80000"
    assert price_api["outputs"][0]["rows"][0]["source_fields"]["현재가"] == "stck_prpr"
    temp = Path(os.environ.get("TMPDIR", "/tmp")) / "collect-financial-information-self-test.yaml"
    write_yaml(temp, cache)
    written = temp.read_text(encoding="utf-8")
    assert '  "005930":' in written
    assert "국내주식 종목추정실적:" in written
    assert "종목 및 최신 투자의견 요약:" in written
    assert "추정 실적 표 1:" not in written
    assert "추정 실적 표 2:" not in written
    assert "추정 실적 기준 기간:" not in written
    assert "현재가: '80000'" in written
    assert "estimate_perform:" not in written
    assert "api_name:" not in written
    assert "output_name:" not in written
    assert "source_output:" not in written
    assert "source_fields:" not in written
    source_temp = Path(os.environ.get("TMPDIR", "/tmp")) / "collect-financial-information-source-fields-self-test.yaml"
    write_source_fields_yaml(source_temp, cache)
    source_written = source_temp.read_text(encoding="utf-8")
    assert "source_api: estimate_perform" in source_written
    assert "source_output: output" in source_written
    assert "source_fields:" in source_written
    assert "현재가: stck_prpr" in source_written
    assert "추정 실적 표 1:" not in source_written
    assert "추정 실적 표 2:" not in source_written
    assert "추정 실적 기준 기간:" not in source_written
    assert "현재가: '80000'" not in source_written
    existing_path = Path(os.environ.get("TMPDIR", "/tmp")) / "collect-financial-information-existing.yaml"
    existing_source_path = cache_sidecar_path(existing_path, date_hyphen)
    write_yaml(existing_path, cache)
    write_source_fields_yaml(existing_source_path, cache)
    failed_update = merge_cache(
        date_hyphen,
        existing_path,
        [
            (
                "005930",
                {
                    "symbol_name": "삼성전자",
                    "apis": {"inquire_price": {"errors": ["temporary_failure"]}},
                },
            )
        ],
    )
    assert failed_update["symbols"]["005930"]["apis"]["inquire_price"]["outputs"][0]["rows"][0]["fields"]["현재가"] == "80000"
    appended = merge_cache(
        date_hyphen,
        existing_path,
        [
            (
                "000660",
                {
                    "symbol_name": "SK하이닉스",
                    "apis": {"inquire_price": {"output": {"stck_prpr": "90000"}}},
                },
            )
        ],
    )
    assert sorted(appended["symbols"]) == ["000660", "005930"]
    assert appended["symbols"]["000660"]["apis"]["inquire_price"]["outputs"][0]["rows"][0]["fields"]["현재가"] == "90000"
    temp.unlink(missing_ok=True)
    source_temp.unlink(missing_ok=True)
    existing_path.unlink(missing_ok=True)
    existing_source_path.unlink(missing_ok=True)
    print("self-test ok")
    return 0


class FinancialCacheSelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        self.assertEqual(command_self_test(argparse.Namespace()), 0)
