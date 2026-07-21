#!/usr/bin/env python3
"""Tests for daily-trading main-evidence collection helpers."""

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
    latest_investor_flow_row,
    merge_duplicate_fills,
    normalize_fill,
    normalize_holding,
    normalize_trading_env,
    parse_float,
    parse_int,
    parse_symbols,
    safe_error,
    skipped_account_asset_snapshot,
    summarize_investor_flow,
    summarize_orderbook,
)


def command_self_test(_args: argparse.Namespace) -> int:
    assert normalize_trading_env("acct") == "real"
    assert normalize_trading_env("paper") == "demo"
    assert parse_symbols("5930,000660,000660") == ["005930", "000660"]
    assert parse_int("1,234.00") == 1234
    assert parse_float("-1.25") == -1.25
    orderbook = summarize_orderbook(
        {"askp1": "1010", "bidp1": "1000", "total_askp_rsqn": "20", "total_bidp_rsqn": "30"},
        {"antc_cnpr": "1005", "antc_vol": "12", "vi_cls_code": "N"},
    )
    assert orderbook["expected_price"] == 1005
    assert orderbook["expected_volume"] == 12
    info = {"prdt_abrv_name": "ACE GOLD ETF", "scty_grp_id_cd": "EF", "etf_dvsn_cd": "02"}
    price = {"stck_prpr": "18590", "prdy_ctrt": "1.23", "acml_vol": "1000"}
    row = build_price_row(
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
    assert row["product_type"] == "etf"
    assert row["price"]["current_or_last"] == 18590
    assert row["charts"]["daily"][0]["date"] == "20260617"
    assert any(signal["name"] == "daily_change_pct" for signal in row["local_signals"])
    assert row["orderbook_summary"]["best_bid"] == 18590
    assert row["orderbook_summary"]["expected_price"] == 18595
    assert row["investor_flow_summary"]["foreign_net_buy_quantity"] == 1000
    assert row["eligible_for_review"]
    sample_account = normalize_holding(
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
    assert sample_account["symbol_id"] == "0183J0"
    assert sample_account["current_live_holding_quantity"] == 3
    assert sample_account["current_price"] == 70000
    assert sample_account["average_purchase_price"] is None
    assert sample_account["purchase_amount"] is None
    average_only_account = normalize_holding(
        {
            "pdno": "005930",
            "hldg_qty": "1",
            "pchs_avg_pric": "60000",
            "pchs_amt": "60000",
            "evlu_amt": "70000",
        },
        observed_at="2026-06-18T09:00:00+09:00",
    )
    assert average_only_account["current_price"] is None
    assert average_only_account["average_purchase_price"] == 60000.0
    assert average_only_account["purchase_amount"] == 60000
    invalid_average_account = normalize_holding(
        {
            "pdno": "005935",
            "hldg_qty": "1",
            "pchs_avg_pric": "0",
            "pchs_amt": "0",
            "evlu_amt": "0",
        },
        observed_at="2026-06-18T09:00:00+09:00",
    )
    assert invalid_average_account["average_purchase_price"] is None
    assert invalid_average_account["purchase_amount"] is None
    negative_purchase_amount_account = normalize_holding(
        {
            "pdno": "005936",
            "hldg_qty": "1",
            "pchs_avg_pric": "60000",
            "pchs_amt": "-1",
            "evlu_amt": "70000",
        },
        observed_at="2026-06-18T09:00:00+09:00",
    )
    assert negative_purchase_amount_account["purchase_amount"] is None
    non_finite_account = normalize_holding(
        {
            "pdno": "005937",
            "hldg_qty": "1",
            "pchs_avg_pric": "Infinity",
            "pchs_amt": "Infinity",
            "evlu_amt": "70000",
        },
        observed_at="2026-06-18T09:00:00+09:00",
    )
    assert non_finite_account["average_purchase_price"] is None
    assert non_finite_account["purchase_amount"] is None
    negative_infinity_account = normalize_holding(
        {
            "pdno": "005938",
            "hldg_qty": "1",
            "pchs_avg_pric": "-Infinity",
            "pchs_amt": "-Infinity",
            "evlu_amt": "70000",
        },
        observed_at="2026-06-18T09:00:00+09:00",
    )
    assert negative_infinity_account["average_purchase_price"] is None
    assert negative_infinity_account["purchase_amount"] is None
    nan_account = normalize_holding(
        {
            "pdno": "005939",
            "hldg_qty": "1",
            "pchs_avg_pric": "NaN",
            "pchs_amt": "NaN",
            "evlu_amt": "70000",
        },
        observed_at="2026-06-18T09:00:00+09:00",
    )
    assert nan_account["average_purchase_price"] is None
    assert nan_account["purchase_amount"] is None
    assert build_account_summary({"dnca_tot_amt": "1000", "tot_evlu_amt": "2000"})["cash_amount"] == 1000
    orderable_summary = build_account_summary({"dnca_tot_amt": "5183620", "prvs_rcdl_excc_amt": "1043015", "nxdy_excc_amt": "1276976"})
    assert orderable_summary["cash_amount"] == 5183620
    assert orderable_summary["orderable_cash_amount"] == 1043015
    fill = normalize_fill(
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
    assert fill["symbol_id"] == "005930"
    assert fill["direction"] == "buy"
    assert fill["filled_price"] == 70100
    assert fill["filled_at"] == "2026-06-18T09:42:00+09:00"
    assert fill["source_actor"] == "non_bot_user"
    assert fill["exchange_id"] == "SOR"
    duplicate_fills = merge_duplicate_fills([dict(fill, daily_ccld_query="default"), dict(fill, daily_ccld_query="SOR")])
    assert len(duplicate_fills) == 1
    assert duplicate_fills[0]["source_queries"] == ["default", "SOR"]
    asset_summary = account_asset_summary_from_row(
        {
            "tot_asst_amt": "20000000",
            "tot_dncl_amt": "1000000",
            "evlu_amt_smtl": "19000000",
            "pchs_amt_smtl": "18000000",
            "evlu_pfls_amt_smtl": "1000000",
            "ovrs_stck_evlu_amt1": "0",
        }
    )
    assert asset_summary["total_asset_amount"] == 20000000
    assert asset_summary["source_api"] == "inquire_account_balance"
    assert asset_summary["evaluation_pnl_rate"] == 1000000 / 18000000
    assert account_asset_params("account", "01") == {
        "CANO": "account",
        "ACNT_PRDT_CD": "01",
        "INQR_DVSN_1": "",
        "BSPR_BF_DT_APLY_YN": "",
    }
    skipped_asset = skipped_account_asset_snapshot(run_id="self-test", started_at="2026-06-18T09:00:00+09:00", env_dv="real", reason="skip-account option")
    assert skipped_asset["skipped"] and skipped_asset["status"] == "success"
    summary = build_collection_summary(
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
    assert summary["status"] == "success"
    assert summary["errors"] == []
    assert summary["counts"]["account_asset_errors"] == 1
    assert summary["counts"]["today_fill_count"] == 1
    assert summary["optional_stages"][0]["stage"] == "account-asset-snapshot"
    assert summary["optional_stages"][1]["stage"] == "today-fills"
    with tempfile.TemporaryDirectory() as tmp_name:
        history_path = Path(tmp_name) / "memory" / "account-assets" / "account-assets.jsonl"
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
        row = json.loads(history_path.read_text(encoding="utf-8").strip())
        assert row["tot_asst_amt"] == 20000000
        assert "account_asset_summary" not in row
        assert "raw_secret_probe" not in row
    print("self-test ok")
    return 0


class CollectMainEvidenceSelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        self.assertEqual(command_self_test(argparse.Namespace()), 0)

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
            return_value=([], {"dnca_tot_amt": "1000", "tot_evlu_amt": "2000"}, []),
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
                return_value=[outside_universe_row],
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
            )

        self.assertEqual(artifact["fill_scope"], "account")
        self.assertEqual([item["symbol_id"] for item in artifact["fills"]], ["999999"])
