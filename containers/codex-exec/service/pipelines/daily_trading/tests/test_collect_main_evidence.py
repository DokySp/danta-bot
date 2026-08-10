#!/usr/bin/env python3
"""Tests for daily-trading main-evidence collection helpers.

`command_self_test` is the compatibility body invoked by the production
CLI's `self-test` command and must keep printing `"self-test ok"` and
returning `0`. Each logical block of the old monolithic self-test now
lives in its own `scenario_*` (setup + act) or `check_*` (single
behavior concern, reusable from a plain function or a `TestCase`
method) helper. `command_self_test` and the granular `TestCase` methods
below both call those helpers, so each behavior has exactly one
implementation. The wrapper-orchestration test mocks the helpers rather
than re-running every scenario, so discovery does not execute the real
work twice. The independent granular tests that already existed below
the umbrella (account-asset API shape, investor flow, account
collection, today-fills scope) are unchanged.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ..scripts.collect_main_evidence import (
    account_asset_params,
    account_asset_summary_from_row,
    append_account_asset_history,
    build_account_summary,
    build_collection_summary,
    build_price_row,
    collect_account_artifact,
    collect_account_asset_snapshot,
    collect_extended_market_evidence,
    collect_today_fills_artifact,
    exchange_preflight,
    latest_investor_flow_row,
    merge_duplicate_fills,
    normalize_fill,
    normalize_holding,
    normalize_trading_env,
    parse_float,
    parse_int,
    parse_symbols,
    previous_market_session_day,
    resolve_order_market,
    safe_error,
    skipped_account_asset_snapshot,
    summarize_investor_flow,
    summarize_orderbook,
)


def check_trading_env_and_parsing_helpers() -> None:
    if normalize_trading_env("acct") != "real":
        raise AssertionError("normalize_trading_env('acct') should be 'real'")
    if normalize_trading_env("paper") != "demo":
        raise AssertionError("normalize_trading_env('paper') should be 'demo'")
    if parse_symbols("5930,000660,000660") != ["005930", "000660"]:
        raise AssertionError(f"unexpected parse_symbols result: {parse_symbols('5930,000660,000660')}")
    if parse_int("1,234.00") != 1234:
        raise AssertionError(f"unexpected parse_int result: {parse_int('1,234.00')}")
    if parse_float("-1.25") != -1.25:
        raise AssertionError(f"unexpected parse_float result: {parse_float('-1.25')}")


def check_summarize_orderbook_expected_price_and_volume() -> None:
    orderbook = summarize_orderbook(
        {"askp1": "1010", "bidp1": "1000", "total_askp_rsqn": "20", "total_bidp_rsqn": "30", "new_mkop_cls_code": "20"},
        {"antc_cnpr": "1005", "antc_vol": "12", "vi_cls_code": "N"},
    )
    if orderbook["expected_price"] != 1005:
        raise AssertionError(f"unexpected expected_price: {orderbook}")
    if orderbook["expected_volume"] != 12:
        raise AssertionError(f"unexpected expected_volume: {orderbook}")
    if orderbook["market_operation_code"] != "20":
        raise AssertionError(f"unexpected market_operation_code: {orderbook}")


def scenario_build_etf_price_row() -> dict:
    info = {"prdt_abrv_name": "ACE GOLD ETF", "scty_grp_id_cd": "EF", "etf_dvsn_cd": "02", "tr_stop_yn": "N"}
    price = {"stck_prpr": "18590", "prdy_ctrt": "1.23", "acml_vol": "1000"}
    return build_price_row(
        "411060",
        info,
        price,
        observed_at="2026-06-18T09:00:00+09:00",
        env_dv="real",
        market="J",
        errors=[],
        charts={
            "daily": [
                {"date": "20260617", "close": 18000, "volume": 100},
                {"date": "20260618", "close": 18590, "volume": 150},
            ],
            "weekly": [],
            "monthly": [],
        },
        orderbook={"best_ask": 18600, "best_bid": 18590, "spread_pct": 0.054, "expected_price": 18595},
        investor_flow={"foreign_net_buy_quantity": 1000},
    )


def check_price_row_product_type_and_price_fields(row: dict) -> None:
    if row["product_type"] != "etf":
        raise AssertionError(f"unexpected product_type: {row}")
    if row["price"]["current_or_last"] != 18590:
        raise AssertionError(f"unexpected current_or_last price: {row['price']}")
    if row["charts"]["daily"][0]["date"] != "20260617":
        raise AssertionError(f"unexpected first daily chart date: {row['charts']['daily']}")


def check_price_row_local_signals_and_summaries(row: dict) -> None:
    if not any(signal["name"] == "daily_change_pct" for signal in row["local_signals"]):
        raise AssertionError(f"expected a daily_change_pct local signal: {row['local_signals']}")
    if row["orderbook_summary"]["best_bid"] != 18590:
        raise AssertionError(f"unexpected orderbook best_bid: {row['orderbook_summary']}")
    if row["orderbook_summary"]["expected_price"] != 18595:
        raise AssertionError(f"unexpected orderbook expected_price: {row['orderbook_summary']}")
    if row["investor_flow_summary"]["foreign_net_buy_quantity"] != 1000:
        raise AssertionError(f"unexpected investor flow summary: {row['investor_flow_summary']}")
    if not row["eligible_for_review"]:
        raise AssertionError(f"expected row to be eligible_for_review: {row}")


def check_normalize_holding_basic_row() -> None:
    holding = normalize_holding(
        {
            "pdno": "0183J0",
            "prdt_name": "Samsung Electronics",
            "hldg_qty": "3",
            "ord_psbl_qty": "2",
            "prpr": "70000",
            "evlu_amt": "210000",
            "evlu_pfls_amt": "1000",
            "evlu_pfls_rt": "0.48",
        },
        observed_at="2026-06-18T09:00:00+09:00",
    )
    if holding["symbol_id"] != "0183J0":
        raise AssertionError(f"unexpected symbol_id: {holding}")
    if holding["current_live_holding_quantity"] != 3:
        raise AssertionError(f"unexpected holding quantity: {holding}")
    if holding["current_price"] != 70000:
        raise AssertionError(f"unexpected current_price: {holding}")
    if holding["average_purchase_price"] is not None:
        raise AssertionError(f"expected no average_purchase_price without pchs fields: {holding}")
    if holding["purchase_amount"] is not None:
        raise AssertionError(f"expected no purchase_amount without pchs fields: {holding}")


def check_normalize_holding_average_only_row() -> None:
    holding = normalize_holding(
        {
            "pdno": "005930",
            "hldg_qty": "1",
            "pchs_avg_pric": "60000",
            "pchs_amt": "60000",
            "evlu_amt": "70000",
        },
        observed_at="2026-06-18T09:00:00+09:00",
    )
    if holding["current_price"] is not None:
        raise AssertionError(f"expected no current_price without prpr field: {holding}")
    if holding["average_purchase_price"] != 60000.0:
        raise AssertionError(f"unexpected average_purchase_price: {holding}")
    if holding["purchase_amount"] != 60000:
        raise AssertionError(f"unexpected purchase_amount: {holding}")


def check_normalize_holding_rejects_non_positive_or_non_finite_average_price() -> None:
    # (row, expect_average_purchase_price_none, expect_purchase_amount_none)
    cases = {
        "zero average": (
            {"pdno": "005935", "hldg_qty": "1", "pchs_avg_pric": "0", "pchs_amt": "0", "evlu_amt": "0"},
            True,
            True,
        ),
        "negative purchase amount only": (
            {"pdno": "005936", "hldg_qty": "1", "pchs_avg_pric": "60000", "pchs_amt": "-1", "evlu_amt": "70000"},
            False,
            True,
        ),
        "positive infinity": (
            {"pdno": "005937", "hldg_qty": "1", "pchs_avg_pric": "Infinity", "pchs_amt": "Infinity", "evlu_amt": "70000"},
            True,
            True,
        ),
        "negative infinity": (
            {"pdno": "005938", "hldg_qty": "1", "pchs_avg_pric": "-Infinity", "pchs_amt": "-Infinity", "evlu_amt": "70000"},
            True,
            True,
        ),
        "NaN": (
            {"pdno": "005939", "hldg_qty": "1", "pchs_avg_pric": "NaN", "pchs_amt": "NaN", "evlu_amt": "70000"},
            True,
            True,
        ),
    }
    for label, (row, expect_average_none, expect_purchase_none) in cases.items():
        holding = normalize_holding(row, observed_at="2026-06-18T09:00:00+09:00")
        if expect_average_none and holding["average_purchase_price"] is not None:
            raise AssertionError(f"expected average_purchase_price=None for {label}: {holding}")
        if not expect_average_none and holding["average_purchase_price"] != 60000.0:
            raise AssertionError(f"expected average_purchase_price preserved for {label}: {holding}")
        if expect_purchase_none and holding["purchase_amount"] is not None:
            raise AssertionError(f"expected purchase_amount=None for {label}: {holding}")


def check_build_account_summary_cash_amounts() -> None:
    basic = build_account_summary({"dnca_tot_amt": "1000", "tot_evlu_amt": "2000"})
    if basic["cash_amount"] != 1000:
        raise AssertionError(f"unexpected cash_amount: {basic}")
    orderable_summary = build_account_summary(
        {"dnca_tot_amt": "5183620", "prvs_rcdl_excc_amt": "1043015", "nxdy_excc_amt": "1276976"}
    )
    if orderable_summary["cash_amount"] != 5183620:
        raise AssertionError(f"unexpected cash_amount: {orderable_summary}")
    if orderable_summary["orderable_cash_amount"] != 1043015:
        raise AssertionError(f"unexpected orderable_cash_amount: {orderable_summary}")


def scenario_normalize_fill() -> dict:
    return normalize_fill(
        {
            "pdno": "005930",
            "prdt_name": "Samsung Electronics",
            "sll_buy_dvsn_cd": "02",
            "tot_ccld_qty": "3",
            "avg_prvs": "70100",
            "ord_dt": "20260618",
            "ord_tmd": "094200",
            "odno": "1",
            "ordr_empno": "N한국",
            "excg_id_dvsn_cd": "SOR",
        },
        env_dv="real",
        observed_at="2026-06-18T09:43:00+09:00",
    )


def check_normalize_fill_fields(fill: dict) -> None:
    if fill["symbol_id"] != "005930":
        raise AssertionError(f"unexpected symbol_id: {fill}")
    if fill["direction"] != "buy":
        raise AssertionError(f"unexpected direction: {fill}")
    if fill["filled_price"] != 70100:
        raise AssertionError(f"unexpected filled_price: {fill}")
    if fill["filled_at"] != "2026-06-18T09:42:00+09:00":
        raise AssertionError(f"unexpected filled_at: {fill}")
    if fill["source_actor"] != "non_bot_user":
        raise AssertionError(f"unexpected source_actor: {fill}")
    if fill["exchange_id"] != "SOR":
        raise AssertionError(f"unexpected exchange_id: {fill}")


def check_merge_duplicate_fills_combines_source_queries(fill: dict) -> None:
    duplicate_fills = merge_duplicate_fills([dict(fill, daily_ccld_query="default"), dict(fill, daily_ccld_query="SOR")])
    if len(duplicate_fills) != 1:
        raise AssertionError(f"expected duplicates merged into one fill: {duplicate_fills}")
    if duplicate_fills[0]["source_queries"] != ["default", "SOR"]:
        raise AssertionError(f"unexpected merged source_queries: {duplicate_fills[0]}")


def scenario_account_asset_summary() -> dict:
    return account_asset_summary_from_row(
        {
            "tot_asst_amt": "20000000",
            "tot_dncl_amt": "1000000",
            "evlu_amt_smtl": "19000000",
            "pchs_amt_smtl": "18000000",
            "evlu_pfls_amt_smtl": "1000000",
            "ovrs_stck_evlu_amt1": "0",
        }
    )


def check_account_asset_summary_fields(asset_summary: dict) -> None:
    if asset_summary["total_asset_amount"] != 20000000:
        raise AssertionError(f"unexpected total_asset_amount: {asset_summary}")
    if asset_summary["source_api"] != "inquire_account_balance":
        raise AssertionError(f"unexpected source_api: {asset_summary}")
    if asset_summary["evaluation_pnl_rate"] != 1000000 / 18000000:
        raise AssertionError(f"unexpected evaluation_pnl_rate: {asset_summary}")


def check_account_asset_params_shape() -> None:
    params = account_asset_params("account", "01")
    expected = {"CANO": "account", "ACNT_PRDT_CD": "01", "INQR_DVSN_1": "", "BSPR_BF_DT_APLY_YN": ""}
    if params != expected:
        raise AssertionError(f"unexpected account_asset_params: {params}")


def check_skipped_account_asset_snapshot_shape() -> None:
    skipped_asset = skipped_account_asset_snapshot(
        run_id="self-test", started_at="2026-06-18T09:00:00+09:00", env_dv="real", reason="skip-account option"
    )
    if not skipped_asset["skipped"] or skipped_asset["status"] != "success":
        raise AssertionError(f"unexpected skipped account asset snapshot: {skipped_asset}")


def scenario_build_collection_summary(fill: dict) -> dict:
    return build_collection_summary(
        run_id="self-test",
        started_at="2026-06-18T09:00:00+09:00",
        env_dv="real",
        symbols=["005930"],
        price_artifact={"status": "success", "symbols": [{"symbol_id": "005930"}], "errors": []},
        account_artifact={"status": "success", "symbols": [{"symbol_id": "005930"}], "warnings": [], "errors": []},
        account_asset_snapshot={
            "stage": "account-asset-snapshot",
            "status": "failed",
            "skipped": False,
            "errors": [safe_error("optional probe", stage="account-asset-snapshot", required=False)],
        },
        today_fills_artifact={
            "stage": "today-fills",
            "status": "success",
            "skipped": False,
            "fills": [fill],
            "errors": [],
        },
        output_dir=Path("/tmp/daily-trading-self-test"),
        token_status="existing_token",
        token_expires_at="",
    )


def check_collection_summary_status_and_counts(summary: dict) -> None:
    if summary["status"] != "success":
        raise AssertionError(f"unexpected summary status: {summary}")
    if summary["errors"] != []:
        raise AssertionError(f"unexpected summary errors: {summary}")
    if summary["counts"]["account_asset_errors"] != 1:
        raise AssertionError(f"unexpected account_asset_errors count: {summary['counts']}")
    if summary["counts"]["today_fill_count"] != 1:
        raise AssertionError(f"unexpected today_fill_count: {summary['counts']}")


def check_collection_summary_optional_stage_order(summary: dict) -> None:
    if summary["optional_stages"][0]["stage"] != "account-asset-snapshot":
        raise AssertionError(f"unexpected first optional stage: {summary['optional_stages']}")
    if summary["optional_stages"][1]["stage"] != "today-fills":
        raise AssertionError(f"unexpected second optional stage: {summary['optional_stages']}")


def scenario_append_account_asset_history(history_path: Path, asset_summary: dict) -> dict:
    snapshot = {
        "schema_version": "1",
        "run_id": "self-test",
        "started_at": "2026-06-18T09:00:00+09:00",
        "observed_at": "2026-06-18T09:01:00+09:00",
        "generated_at": "2026-06-18T09:01:00+09:00",
        "status": "success",
        "source_api": "inquire_account_balance",
        "execution_environment": "real",
        "tot_asst_amt": 20000000,
        "tot_dncl_amt": 1000000,
        "evlu_amt_smtl": 19000000,
        "pchs_amt_smtl": 18000000,
        "evlu_pfls_amt_smtl": 1000000,
        "ovrs_stck_evlu_amt1": 0,
        "account_asset_summary": asset_summary,
        "raw_secret_probe": "must_not_be_written",
    }
    append_account_asset_history(history_path, snapshot)
    return json.loads(history_path.read_text(encoding="utf-8").strip())


def check_account_asset_history_row_redacts_internal_fields(row: dict) -> None:
    if row["tot_asst_amt"] != 20000000:
        raise AssertionError(f"unexpected persisted tot_asst_amt: {row}")
    if "account_asset_summary" in row:
        raise AssertionError(f"persisted row unexpectedly retained account_asset_summary: {row}")
    if "raw_secret_probe" in row:
        raise AssertionError(f"persisted row unexpectedly retained raw_secret_probe: {row}")


def command_self_test(_args: argparse.Namespace) -> int:
    check_trading_env_and_parsing_helpers()
    check_summarize_orderbook_expected_price_and_volume()

    price_row = scenario_build_etf_price_row()
    check_price_row_product_type_and_price_fields(price_row)
    check_price_row_local_signals_and_summaries(price_row)

    check_normalize_holding_basic_row()
    check_normalize_holding_average_only_row()
    check_normalize_holding_rejects_non_positive_or_non_finite_average_price()

    check_build_account_summary_cash_amounts()

    fill = scenario_normalize_fill()
    check_normalize_fill_fields(fill)
    check_merge_duplicate_fills_combines_source_queries(fill)

    asset_summary = scenario_account_asset_summary()
    check_account_asset_summary_fields(asset_summary)
    check_account_asset_params_shape()
    check_skipped_account_asset_snapshot_shape()

    summary = scenario_build_collection_summary(fill)
    check_collection_summary_status_and_counts(summary)
    check_collection_summary_optional_stage_order(summary)

    with tempfile.TemporaryDirectory() as tmp_name:
        history_path = Path(tmp_name) / "memory" / "account-assets" / "account-assets.jsonl"
        row = scenario_append_account_asset_history(history_path, asset_summary)
        check_account_asset_history_row_redacts_internal_fields(row)

    print("self-test ok")
    return 0


class CollectMainEvidenceSelfTest(unittest.TestCase):
    def test_self_test_suite_runs_every_check_and_reports_success(self) -> None:
        """Wrapper-orchestration check only: real behavior is covered by the
        granular tests below, so this mocks every helper instead of
        re-running the whole scenario a second time."""
        helper_names = [
            "check_trading_env_and_parsing_helpers",
            "check_summarize_orderbook_expected_price_and_volume",
            "scenario_build_etf_price_row",
            "check_price_row_product_type_and_price_fields",
            "check_price_row_local_signals_and_summaries",
            "check_normalize_holding_basic_row",
            "check_normalize_holding_average_only_row",
            "check_normalize_holding_rejects_non_positive_or_non_finite_average_price",
            "check_build_account_summary_cash_amounts",
            "scenario_normalize_fill",
            "check_normalize_fill_fields",
            "check_merge_duplicate_fills_combines_source_queries",
            "scenario_account_asset_summary",
            "check_account_asset_summary_fields",
            "check_account_asset_params_shape",
            "check_skipped_account_asset_snapshot_shape",
            "scenario_build_collection_summary",
            "check_collection_summary_status_and_counts",
            "check_collection_summary_optional_stage_order",
            "scenario_append_account_asset_history",
            "check_account_asset_history_row_redacts_internal_fields",
        ]
        patchers = [patch(f"{__name__}.{name}") for name in helper_names]
        mocks = [patcher.start() for patcher in patchers]
        self.addCleanup(lambda: [patcher.stop() for patcher in patchers])

        result = command_self_test(argparse.Namespace())

        self.assertEqual(result, 0)
        for mock in mocks:
            mock.assert_called()

    def test_account_asset_snapshot_uses_dedicated_account_balance_api(self) -> None:
        calls: list[tuple[str, dict[str, str]]] = []

        def fake_call_endpoint(endpoint_name: str, params: dict[str, str], *_args: object, **_kwargs: object) -> tuple[dict[str, object], dict[str, str]]:
            calls.append((endpoint_name, params))
            return (
                {
                    "output2": {
                        "tot_asst_amt": "20000000",
                        "tot_dncl_amt": "1000000",
                        "evlu_amt_smtl": "19000000",
                        "pchs_amt_smtl": "18000000",
                        "evlu_pfls_amt_smtl": "1000000",
                        "ovrs_stck_evlu_amt1": "0",
                    }
                },
                {},
            )

        with (
            patch("service.pipelines.daily_trading.scripts.collect_main_evidence.account_parts", return_value=("account", "01")),
            patch("service.pipelines.daily_trading.scripts.collect_main_evidence.call_endpoint", side_effect=fake_call_endpoint),
        ):
            snapshot = collect_account_asset_snapshot(
                run_id="asset-api",
                started_at="2026-07-15T09:00:00+09:00",
                env_dv="real",
                app_key="masked",
                app_secret="masked",
                token="masked",
                retries=0,
            )

        self.assertEqual(calls[0][0], "inquire_account_balance")
        self.assertEqual(calls[0][1]["INQR_DVSN_1"], "")
        self.assertEqual(snapshot["status"], "success")
        self.assertEqual(snapshot["source_api"], "inquire_account_balance")
        self.assertEqual(snapshot["tot_asst_amt"], 20000000)

    def test_investor_flow_uses_latest_usable_estimate(self) -> None:
        rows = [
            {"bsop_hour_gb": "3", "frgn_fake_ntby_qty": "100", "orgn_fake_ntby_qty": "-20", "sum_fake_ntby_qty": "80"},
            {"bsop_hour_gb": "5"},
            {"bsop_hour_gb": "4", "frgn_fake_ntby_qty": "200", "orgn_fake_ntby_qty": "10", "sum_fake_ntby_qty": "210"},
        ]

        summary = summarize_investor_flow(latest_investor_flow_row(rows))

        self.assertEqual(summary["estimate_time_code"], "4")
        self.assertEqual(summary["foreign_net_buy_quantity"], 200)
        self.assertEqual(summary["institution_net_buy_quantity"], 10)
        self.assertEqual(summary["combined_net_buy_quantity"], 210)
        self.assertEqual(summarize_investor_flow({"bsop_hour_gb": "5"}), {})

    def test_empty_investor_flow_is_recorded_as_optional_unavailable(self) -> None:
        with (
            patch("service.pipelines.daily_trading.scripts.collect_main_evidence.collect_period_chart", return_value=[]),
            patch("service.pipelines.daily_trading.scripts.collect_main_evidence.collect_intraday_chart", return_value=[]),
            patch("service.pipelines.daily_trading.scripts.collect_main_evidence.collect_orderbook_summary", return_value={}),
            patch("service.pipelines.daily_trading.scripts.collect_main_evidence.collect_trade_flow_summary", return_value={}),
            patch("service.pipelines.daily_trading.scripts.collect_main_evidence.collect_investor_flow_summary", return_value={}),
        ):
            *_evidence, errors = collect_extended_market_evidence(
                "005930",
                market="J",
                app_key="masked",
                app_secret="masked",
                token="masked",
                retries=0,
                env_dv="real",
            )

        self.assertIn("investor_flow_unavailable", {item.get("code") for item in errors})
        self.assertTrue(all(item.get("required") is False for item in errors))

    def test_account_collection_success_does_not_require_order_gates(self) -> None:
        with patch(
            "service.pipelines.daily_trading.scripts.collect_main_evidence.fetch_account_balance",
            return_value=(
                [
                    {
                        "pdno": "005930",
                        "prdt_name": "Samsung",
                        "hldg_qty": "0",
                        "thdt_buyqty": "0",
                        "thdt_sll_qty": "2",
                    }
                ],
                {"dnca_tot_amt": "1000", "tot_evlu_amt": "2000"},
                [],
            ),
        ):
            artifact = collect_account_artifact(
                ["005930"],
                run_id="account-status",
                started_at="2026-07-15T09:00:00+09:00",
                env_dv="real",
                app_key="masked",
                app_secret="masked",
                token="masked",
                retries=0,
                max_pages=1,
                request_type="analysis",
            )

        self.assertEqual(artifact["status"], "success")
        self.assertEqual(artifact["order_gate_status"], "not_run")
        self.assertEqual(artifact["warnings"], [])
        self.assertTrue(artifact["symbols"][0]["snapshot_row_available"])
        self.assertEqual(artifact["symbols"][0]["today_sell_quantity"], 2)

    def test_today_fills_preserve_account_wide_symbols(self) -> None:
        outside_universe_row = {
            "pdno": "999999",
            "prdt_name": "Outside Universe",
            "sll_buy_dvsn_cd": "01",
            "tot_ccld_qty": "2",
            "avg_prvs": "12000",
            "ord_dt": "20260618",
            "ord_tmd": "101500",
            "odno": "outside-1",
        }
        previous_session_row = {
            "pdno": "005930",
            "prdt_name": "Samsung",
            "sll_buy_dvsn_cd": "01",
            "tot_ccld_qty": "3",
            "avg_prvs": "71000",
            "ord_dt": "20260617",
            "ord_tmd": "151500",
            "odno": "previous-1",
        }
        with (
            patch(
                "service.pipelines.daily_trading.scripts.collect_main_evidence.account_parts",
                return_value=("account", "product"),
            ),
            patch(
                "service.pipelines.daily_trading.scripts.collect_main_evidence.daily_ccld_query_variants",
                return_value=[("default", {})],
            ),
            patch(
                "service.pipelines.daily_trading.scripts.collect_main_evidence.fetch_daily_ccld_rows",
                side_effect=lambda **kwargs: [
                    outside_universe_row if kwargs["day"] == "20260618" else previous_session_row
                ],
            ),
            patch(
                "service.pipelines.daily_trading.scripts.collect_main_evidence.fetch_period_trade_profit_rows",
                return_value=[
                    {
                        "trad_dt": "20260617",
                        "pdno": "005930",
                        "prdt_name": "Samsung",
                        "rlzt_pfls": "-3694",
                    }
                ],
            ),
        ):
            artifact = collect_today_fills_artifact(
                ["005930"],
                run_id="account-wide-fills",
                started_at="2026-06-18T10:30:00+09:00",
                env_dv="real",
                app_key="masked",
                app_secret="masked",
                token="masked",
                retries=0,
                previous_session_day="20260617",
            )

        self.assertEqual(artifact["fill_scope"], "account")
        self.assertEqual([item["symbol_id"] for item in artifact["fills"]], ["999999"])
        previous = artifact["previous_session"]
        self.assertEqual(previous["fill_collection_status"], "complete")
        self.assertEqual(previous["fills"][0]["filled_quantity"], 3)
        self.assertEqual(previous["realized_pnl"]["symbols"][0]["amount_krw"], -3694)
        self.assertEqual(
            previous_market_session_day(
                {
                    "symbols": [
                        {"charts": {"daily": [{"date": "20260618"}, {"date": "20260617"}]}}
                    ]
                },
                "2026-06-18T10:30:00+09:00",
            ),
            "20260617",
        )


class ParsingAndSummaryHelperTest(unittest.TestCase):
    def test_trading_env_and_parsing_helpers(self) -> None:
        check_trading_env_and_parsing_helpers()

    def test_summarize_orderbook_expected_price_and_volume(self) -> None:
        check_summarize_orderbook_expected_price_and_volume()

    def test_build_account_summary_cash_amounts(self) -> None:
        check_build_account_summary_cash_amounts()

    def test_account_asset_params_shape(self) -> None:
        check_account_asset_params_shape()

    def test_skipped_account_asset_snapshot_shape(self) -> None:
        check_skipped_account_asset_snapshot_shape()


class BuildPriceRowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.row = scenario_build_etf_price_row()

    def test_price_row_product_type_and_price_fields(self) -> None:
        check_price_row_product_type_and_price_fields(self.row)

    def test_price_row_local_signals_and_summaries(self) -> None:
        check_price_row_local_signals_and_summaries(self.row)

    def test_exchange_preflight_uses_live_kis_flags(self) -> None:
        open_orderbook = {"market_operation_code": "20"}
        self.assertEqual(
            exchange_preflight(
                {"cptt_trad_tr_psbl_yn": "Y", "nxt_tr_stop_yn": "N"},
                open_orderbook,
                market="UN",
                order_path="immediate",
            )["status"],
            "eligible",
        )
        self.assertEqual(
            exchange_preflight(
                {"cptt_trad_tr_psbl_yn": "N", "nxt_tr_stop_yn": "N"},
                open_orderbook,
                market="UN",
                order_path="immediate",
            )["reasons"],
            ["exchange.nxt_not_tradable"],
        )
        self.assertEqual(
            exchange_preflight(
                {"cptt_trad_tr_psbl_yn": "Y", "nxt_tr_stop_yn": "N"},
                {"market_operation_code": "30"},
                market="NX",
                order_path="immediate",
            )["reasons"],
            ["exchange.session_not_open"],
        )
        self.assertEqual(
            exchange_preflight(
                {"tr_stop_yn": "N"},
                {},
                market="J",
                order_path="reservation",
            )["status"],
            "eligible",
        )
        self.assertEqual(
            exchange_preflight(
                {},
                open_orderbook,
                market="J",
                order_path="immediate",
            )["reasons"],
            ["exchange.krx_trade_status_unknown"],
        )
        self.assertEqual(
            exchange_preflight(
                {"cptt_trad_tr_psbl_yn": "Y"},
                open_orderbook,
                market="NX",
                order_path="immediate",
            )["reasons"],
            ["exchange.nxt_trade_status_unknown"],
        )
        blocked_row = build_price_row(
            "411060",
            {"prdt_abrv_name": "ACE KRX Gold", "cptt_trad_tr_psbl_yn": "N"},
            {"stck_prpr": "18590"},
            observed_at="2026-08-07T09:05:00+09:00",
            env_dv="real",
            market="UN",
            errors=[],
            orderbook=open_orderbook,
            order_path="immediate",
        )
        self.assertTrue(blocked_row["eligible_for_review"])
        self.assertFalse(blocked_row["eligible_for_order"])
        self.assertNotIn("exchange.nxt_not_tradable", blocked_row["required_missing"])
        self.assertIn("exchange.nxt_not_tradable", blocked_row["order_block_reasons"])

    def test_auto_market_uses_live_nxt_eligibility_without_symbol_rules(self) -> None:
        self.assertEqual(
            resolve_order_market(
                {"cptt_trad_tr_psbl_yn": "Y"},
                requested_market="AUTO",
                order_path="immediate",
            ),
            ("UN", []),
        )
        self.assertEqual(
            resolve_order_market(
                {"cptt_trad_tr_psbl_yn": "N"},
                requested_market="AUTO",
                order_path="immediate",
            ),
            ("J", []),
        )
        self.assertEqual(
            resolve_order_market({}, requested_market="AUTO", order_path="immediate"),
            ("J", ["exchange.nxt_tradability_unknown"]),
        )
        self.assertEqual(
            resolve_order_market(
                {"cptt_trad_tr_psbl_yn": "Y"},
                requested_market="AUTO",
                order_path="reservation",
            ),
            ("J", []),
        )


class NormalizeHoldingTest(unittest.TestCase):
    def test_normalize_holding_basic_row(self) -> None:
        check_normalize_holding_basic_row()

    def test_normalize_holding_average_only_row(self) -> None:
        check_normalize_holding_average_only_row()

    def test_normalize_holding_rejects_non_positive_or_non_finite_average_price(self) -> None:
        check_normalize_holding_rejects_non_positive_or_non_finite_average_price()


class FillNormalizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fill = scenario_normalize_fill()

    def test_normalize_fill_fields(self) -> None:
        check_normalize_fill_fields(self.fill)

    def test_merge_duplicate_fills_combines_source_queries(self) -> None:
        check_merge_duplicate_fills_combines_source_queries(self.fill)


class AccountAssetAndCollectionSummaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.asset_summary = scenario_account_asset_summary()
        cls.fill = scenario_normalize_fill()
        cls.summary = scenario_build_collection_summary(cls.fill)

    def test_account_asset_summary_fields(self) -> None:
        check_account_asset_summary_fields(self.asset_summary)

    def test_collection_summary_status_and_counts(self) -> None:
        check_collection_summary_status_and_counts(self.summary)

    def test_collection_summary_optional_stage_order(self) -> None:
        check_collection_summary_optional_stage_order(self.summary)


class AccountAssetHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.history_path = Path(self._temp_dir.name) / "memory" / "account-assets" / "account-assets.jsonl"

    def test_account_asset_history_row_redacts_internal_fields(self) -> None:
        row = scenario_append_account_asset_history(self.history_path, scenario_account_asset_summary())
        check_account_asset_history_row_redacts_internal_fields(row)


if __name__ == "__main__":
    unittest.main()
