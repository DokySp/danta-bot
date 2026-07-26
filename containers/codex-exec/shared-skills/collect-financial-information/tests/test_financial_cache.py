"""Tests for KIS financial cache collection and serialization.

`command_self_test` is the compatibility body invoked by the production
CLI's `self-test` command and must keep printing `"self-test ok"` and
returning `0`. Each logical block of the old monolithic self-test now
lives in its own `scenario_*` (setup + act, using a real
`TemporaryDirectory` instead of a fixed TMPDIR filename) or `check_*`
(single assertion concern, reusable from a plain function or a
`TestCase` method) helper. `command_self_test` and the granular
`TestCase` methods below both call those helpers, so each behavior has
exactly one implementation. The wrapper-orchestration test mocks the
helpers rather than re-running every scenario, so discovery does not
execute the real work twice.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

DATE_HYPHEN = normalize_date("20260610")

LOAD_SYMBOLS_NAMESPACE = argparse.Namespace(
    symbols=["005930,000660"],
    symbol=None,
    symbols_file=None,
    product_type="300",
    market="J",
    start_date=None,
    include_etf=True,
)
EMPTY_COLLECT_NAMESPACE = argparse.Namespace(
    date=DATE_HYPHEN,
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

FRESH_SYMBOL_PAYLOAD = (
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


def check_normalize_date_converts_to_hyphenated_form() -> None:
    if DATE_HYPHEN != "2026-06-10":
        raise AssertionError(f"unexpected normalized date: {DATE_HYPHEN}")


def check_cache_path_uses_hyphenated_date() -> None:
    name = financial_cache_path(DATE_HYPHEN).name
    if name != "financial-2026-06-10.yaml":
        raise AssertionError(f"unexpected cache file name: {name}")


def check_load_symbols_parses_comma_separated_list() -> None:
    parsed = load_symbols(LOAD_SYMBOLS_NAMESPACE)
    if parsed != [("005930", "005930"), ("000660", "000660")]:
        raise AssertionError(f"unexpected load_symbols result: {parsed}")


def check_load_symbols_allows_empty_when_not_required() -> None:
    parsed = load_symbols(EMPTY_COLLECT_NAMESPACE, require=False)
    if parsed != []:
        raise AssertionError(f"expected empty symbol list, got: {parsed}")


def scenario_collect_with_no_symbols() -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = command_collect(EMPTY_COLLECT_NAMESPACE)
    return exit_code, stdout.getvalue().strip()


def check_collect_with_no_symbols_still_writes_dated_cache(exit_code: int, printed_path: str) -> None:
    if exit_code != 0:
        raise AssertionError(f"command_collect returned non-zero exit code: {exit_code}")
    if not printed_path.endswith("financial-2026-06-10.yaml"):
        raise AssertionError(f"unexpected printed cache path: {printed_path}")


def check_api_params_date_windows() -> None:
    if api_params("estimate_perform", "005930", LOAD_SYMBOLS_NAMESPACE, DATE_HYPHEN) != {"SHT_CD": "005930"}:
        raise AssertionError("estimate_perform api_params should only carry SHT_CD")
    invest_opinion = api_params("invest_opinion", "005930", LOAD_SYMBOLS_NAMESPACE, DATE_HYPHEN)
    if invest_opinion["FID_COND_SCR_DIV_CODE"] != "16633":
        raise AssertionError(f"unexpected invest_opinion screen code: {invest_opinion}")
    if invest_opinion["FID_INPUT_DATE_1"] != "20260513":
        raise AssertionError(f"unexpected invest_opinion start date: {invest_opinion}")
    if invest_opinion["FID_INPUT_DATE_2"] != "20260610":
        raise AssertionError(f"unexpected invest_opinion end date: {invest_opinion}")


def scenario_fresh_merge() -> dict:
    return merge_cache(
        DATE_HYPHEN,
        Path("/tmp/nonexistent-financial-cache.yaml"),
        [FRESH_SYMBOL_PAYLOAD],
    )


def scenario_canonical_fresh_cache() -> dict:
    return canonical_cache(scenario_fresh_merge())


def check_canonical_top_level_order(canonical: dict) -> None:
    if list(canonical.keys()) != ["date", "source", "symbols"]:
        raise AssertionError(f"unexpected top-level order: {list(canonical.keys())}")
    if list(canonical["symbols"]["005930"].keys()) != ["symbol_name", "apis"]:
        raise AssertionError(f"unexpected symbol field order: {canonical['symbols']['005930']}")


def check_canonical_estimate_perform_shape(canonical: dict) -> None:
    apis = canonical["symbols"]["005930"]["apis"]
    if "estimate_perform" not in apis:
        raise AssertionError(f"estimate_perform missing from canonical apis: {apis}")
    estimate_api = apis["estimate_perform"]
    if estimate_api["api_name"] != "국내주식 종목추정실적":
        raise AssertionError(f"unexpected api_name: {estimate_api['api_name']}")
    if len(estimate_api["outputs"]) != 1:
        raise AssertionError(f"expected exactly one estimate_perform output: {estimate_api['outputs']}")
    estimate_output = estimate_api["outputs"][0]
    if estimate_output["output_name"] != "종목 및 최신 투자의견 요약":
        raise AssertionError(f"unexpected output_name: {estimate_output['output_name']}")


def check_canonical_estimate_perform_fields(canonical: dict) -> None:
    estimate_output = canonical["symbols"]["005930"]["apis"]["estimate_perform"]["outputs"][0]
    fields = estimate_output["rows"][0]["fields"]
    if fields["주식 영업일자"] != "20260610":
        raise AssertionError(f"unexpected 주식 영업일자: {fields}")
    if fields["주당순이익(EPS)"] != "1234":
        raise AssertionError(f"unexpected 주당순이익(EPS): {fields}")
    source_fields = estimate_output["rows"][0]["source_fields"]
    if source_fields["주식 영업일자"] != "stck_bsop_date":
        raise AssertionError(f"unexpected source field mapping: {source_fields}")
    if source_fields["주당순이익(EPS)"] != "eps":
        raise AssertionError(f"unexpected source field mapping: {source_fields}")


def check_canonical_inquire_price_fields(canonical: dict) -> None:
    price_api = canonical["symbols"]["005930"]["apis"]["inquire_price"]
    price_row = price_api["outputs"][0]["rows"][0]
    if price_row["fields"]["현재가"] != "80000":
        raise AssertionError(f"unexpected 현재가 field: {price_row['fields']}")
    if price_row["source_fields"]["현재가"] != "stck_prpr":
        raise AssertionError(f"unexpected 현재가 source field: {price_row['source_fields']}")


def scenario_write_yaml_rendering(cache_path: Path, cache: dict) -> str:
    write_yaml(cache_path, cache)
    return cache_path.read_text(encoding="utf-8")


def check_written_yaml_includes_symbol_and_human_labels(written: str) -> None:
    if '  "005930":' not in written:
        raise AssertionError(f"written yaml missing symbol block: {written}")
    if "국내주식 종목추정실적:" not in written:
        raise AssertionError(f"written yaml missing api human label: {written}")
    if "종목 및 최신 투자의견 요약:" not in written:
        raise AssertionError(f"written yaml missing output human label: {written}")
    if "현재가: '80000'" not in written:
        raise AssertionError(f"written yaml missing quoted price field: {written}")


def check_written_yaml_omits_raw_output_table_names_and_internal_keys(written: str) -> None:
    for forbidden in (
        "추정 실적 표 1:",
        "추정 실적 표 2:",
        "추정 실적 기준 기간:",
        "estimate_perform:",
        "api_name:",
        "output_name:",
        "source_output:",
        "source_fields:",
    ):
        if forbidden in written:
            raise AssertionError(f"written yaml unexpectedly contains {forbidden!r}: {written}")


def scenario_write_source_fields_yaml_rendering(source_path: Path, cache: dict) -> str:
    write_source_fields_yaml(source_path, cache)
    return source_path.read_text(encoding="utf-8")


def check_source_fields_yaml_maps_human_labels_to_source_keys(source_written: str) -> None:
    if "source_api: estimate_perform" not in source_written:
        raise AssertionError(f"source-fields yaml missing source_api: {source_written}")
    if "source_output: output" not in source_written:
        raise AssertionError(f"source-fields yaml missing source_output: {source_written}")
    if "source_fields:" not in source_written:
        raise AssertionError(f"source-fields yaml missing source_fields block: {source_written}")
    if "현재가: stck_prpr" not in source_written:
        raise AssertionError(f"source-fields yaml missing 현재가 mapping: {source_written}")


def check_source_fields_yaml_omits_rendered_values(source_written: str) -> None:
    for forbidden in ("추정 실적 표 1:", "추정 실적 표 2:", "추정 실적 기준 기간:", "현재가: '80000'"):
        if forbidden in source_written:
            raise AssertionError(f"source-fields yaml unexpectedly contains {forbidden!r}: {source_written}")


def scenario_merge_updates_and_appends(existing_path: Path, existing_source_path: Path, cache: dict) -> tuple[dict, dict]:
    """Seed an on-disk cache, then run a failed-field update and a new-symbol append against it."""
    write_yaml(existing_path, cache)
    write_source_fields_yaml(existing_source_path, cache)

    failed_update = merge_cache(
        DATE_HYPHEN,
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
    appended = merge_cache(
        DATE_HYPHEN,
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
    return failed_update, appended


def check_failed_update_preserves_previous_value(failed_update: dict) -> None:
    price = failed_update["symbols"]["005930"]["apis"]["inquire_price"]["outputs"][0]["rows"][0]["fields"]["현재가"]
    if price != "80000":
        raise AssertionError(f"failed update should keep previous 현재가, got: {price}")


def check_appended_symbol_is_added_alongside_existing(appended: dict) -> None:
    if sorted(appended["symbols"]) != ["000660", "005930"]:
        raise AssertionError(f"expected both symbols present after append: {sorted(appended['symbols'])}")
    price = appended["symbols"]["000660"]["apis"]["inquire_price"]["outputs"][0]["rows"][0]["fields"]["현재가"]
    if price != "90000":
        raise AssertionError(f"unexpected appended symbol 현재가: {price}")


def command_self_test(_args: argparse.Namespace) -> int:
    check_normalize_date_converts_to_hyphenated_form()
    check_cache_path_uses_hyphenated_date()
    check_load_symbols_parses_comma_separated_list()
    check_load_symbols_allows_empty_when_not_required()

    exit_code, printed_path = scenario_collect_with_no_symbols()
    check_collect_with_no_symbols_still_writes_dated_cache(exit_code, printed_path)

    check_api_params_date_windows()

    canonical = scenario_canonical_fresh_cache()
    check_canonical_top_level_order(canonical)
    check_canonical_estimate_perform_shape(canonical)
    check_canonical_estimate_perform_fields(canonical)
    check_canonical_inquire_price_fields(canonical)

    cache = scenario_fresh_merge()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        written = scenario_write_yaml_rendering(temp_root / "financial-self-test.yaml", cache)
        check_written_yaml_includes_symbol_and_human_labels(written)
        check_written_yaml_omits_raw_output_table_names_and_internal_keys(written)

        source_written = scenario_write_source_fields_yaml_rendering(
            temp_root / "financial-source-fields-self-test.yaml", cache
        )
        check_source_fields_yaml_maps_human_labels_to_source_keys(source_written)
        check_source_fields_yaml_omits_rendered_values(source_written)

        existing_path = temp_root / "financial-existing.yaml"
        existing_source_path = cache_sidecar_path(existing_path, DATE_HYPHEN)
        failed_update, appended = scenario_merge_updates_and_appends(existing_path, existing_source_path, cache)
        check_failed_update_preserves_previous_value(failed_update)
        check_appended_symbol_is_added_alongside_existing(appended)

    print("self-test ok")
    return 0


class FinancialCacheSelfTest(unittest.TestCase):
    def test_self_test_suite_runs_every_check_and_reports_success(self) -> None:
        """Wrapper-orchestration check only: real behavior is covered by the
        granular tests below, so this mocks every helper instead of
        re-running the whole scenario a second time."""
        helper_names = [
            "check_normalize_date_converts_to_hyphenated_form",
            "check_cache_path_uses_hyphenated_date",
            "check_load_symbols_parses_comma_separated_list",
            "check_load_symbols_allows_empty_when_not_required",
            "scenario_collect_with_no_symbols",
            "check_collect_with_no_symbols_still_writes_dated_cache",
            "check_api_params_date_windows",
            "scenario_canonical_fresh_cache",
            "check_canonical_top_level_order",
            "check_canonical_estimate_perform_shape",
            "check_canonical_estimate_perform_fields",
            "check_canonical_inquire_price_fields",
            "scenario_fresh_merge",
            "scenario_write_yaml_rendering",
            "check_written_yaml_includes_symbol_and_human_labels",
            "check_written_yaml_omits_raw_output_table_names_and_internal_keys",
            "scenario_write_source_fields_yaml_rendering",
            "check_source_fields_yaml_maps_human_labels_to_source_keys",
            "check_source_fields_yaml_omits_rendered_values",
            "scenario_merge_updates_and_appends",
            "check_failed_update_preserves_previous_value",
            "check_appended_symbol_is_added_alongside_existing",
        ]
        patchers = [patch(f"{__name__}.{name}", return_value=(None, None)) for name in helper_names]
        mocks = [patcher.start() for patcher in patchers]
        self.addCleanup(lambda: [patcher.stop() for patcher in patchers])

        result = command_self_test(argparse.Namespace())

        self.assertEqual(result, 0)
        for mock in mocks:
            mock.assert_called()


class DateAndSymbolHelpersTest(unittest.TestCase):
    def test_normalize_date_converts_to_hyphenated_form(self) -> None:
        check_normalize_date_converts_to_hyphenated_form()

    def test_cache_path_uses_hyphenated_date(self) -> None:
        check_cache_path_uses_hyphenated_date()

    def test_load_symbols_parses_comma_separated_list(self) -> None:
        check_load_symbols_parses_comma_separated_list()

    def test_load_symbols_allows_empty_when_not_required(self) -> None:
        check_load_symbols_allows_empty_when_not_required()

    def test_collect_with_no_symbols_still_writes_dated_cache(self) -> None:
        exit_code, printed_path = scenario_collect_with_no_symbols()
        check_collect_with_no_symbols_still_writes_dated_cache(exit_code, printed_path)

    def test_api_params_date_windows(self) -> None:
        check_api_params_date_windows()


class CanonicalCacheShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.canonical = scenario_canonical_fresh_cache()

    def test_canonical_top_level_order(self) -> None:
        check_canonical_top_level_order(self.canonical)

    def test_canonical_estimate_perform_shape(self) -> None:
        check_canonical_estimate_perform_shape(self.canonical)

    def test_canonical_estimate_perform_fields(self) -> None:
        check_canonical_estimate_perform_fields(self.canonical)

    def test_canonical_inquire_price_fields(self) -> None:
        check_canonical_inquire_price_fields(self.canonical)


class YamlRenderingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.temp_root = Path(self._temp_dir.name)
        self.cache = scenario_fresh_merge()

    def test_written_yaml_includes_symbol_and_human_labels(self) -> None:
        written = scenario_write_yaml_rendering(self.temp_root / "financial.yaml", self.cache)
        check_written_yaml_includes_symbol_and_human_labels(written)

    def test_written_yaml_omits_raw_output_table_names_and_internal_keys(self) -> None:
        written = scenario_write_yaml_rendering(self.temp_root / "financial.yaml", self.cache)
        check_written_yaml_omits_raw_output_table_names_and_internal_keys(written)

    def test_source_fields_yaml_maps_human_labels_to_source_keys(self) -> None:
        source_written = scenario_write_source_fields_yaml_rendering(self.temp_root / "financial-source.yaml", self.cache)
        check_source_fields_yaml_maps_human_labels_to_source_keys(source_written)

    def test_source_fields_yaml_omits_rendered_values(self) -> None:
        source_written = scenario_write_source_fields_yaml_rendering(self.temp_root / "financial-source.yaml", self.cache)
        check_source_fields_yaml_omits_rendered_values(source_written)


class MergeUpdatesAndAppendsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        temp_root = Path(self._temp_dir.name)
        existing_path = temp_root / "financial-existing.yaml"
        existing_source_path = cache_sidecar_path(existing_path, DATE_HYPHEN)
        self.failed_update, self.appended = scenario_merge_updates_and_appends(
            existing_path, existing_source_path, scenario_fresh_merge()
        )

    def test_failed_update_preserves_previous_value(self) -> None:
        check_failed_update_preserves_previous_value(self.failed_update)

    def test_appended_symbol_is_added_alongside_existing(self) -> None:
        check_appended_symbol_is_added_alongside_existing(self.appended)


if __name__ == "__main__":
    unittest.main()
