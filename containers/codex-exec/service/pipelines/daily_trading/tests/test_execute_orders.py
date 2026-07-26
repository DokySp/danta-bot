#!/usr/bin/env python3
"""Tests for daily-trading order execution and reconciliation."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from ..scripts import execute_orders as execute_orders_module
from ..scripts.execute_orders import (
    ENDPOINTS,
    PORTFOLIO_EXCEPT_ENV_VAR,
    adjust_reservation,
    default_reservation_orgno,
    execute,
    fetch_reservations,
    load_json,
    normalize_limit_price,
    normalize_broker_reconciliation,
    normalize_reservation,
    now_iso,
    order_lifecycle_preflight,
    reconcile,
    reconcile_submitted_cash_orders,
    write_json,
)


class FakeKis:
    cano = "12345678"
    product = "01"
    env = "real"

    def headers(self, tr_id: str, payload: dict[str, Any]) -> dict[str, str]:
        return {"tr_id": tr_id}


def step_dry_run_gate_and_portfolio_except_checks(root: Path) -> list[str]:
    """Dry-run gate refresh, invalid final-holding-quantity blocking, and portfolio-except exclusion."""
    failures: list[str] = []
    account = {
        "schema_version": "1",
        "execution_environment": "real",
        "account_summary": {"cash_amount": 500000},
        "order_gate_status": "not_run",
        "active_order_lookup_performed": False,
        "order_available_lookup_performed": False,
        "warnings": ["active_order_lookup_not_performed", "order_available_lookup_not_performed"],
        "active_orders": [{"symbol_id": "005930", "symbol_name": "삼성전자", "order_id": "r1", "order_kind": "reservation", "direction": "sell", "remaining_quantity": 2, "order_price": 70000, "active_status": "active", "order_api": "order_resv", "order_path": "reservation", "execution_environment": "real", "observed_at": now_iso()}],
        "symbols": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 8, "current_price": 70000},
            {"symbol_id": "000270", "symbol_name": "기아", "current_live_holding_quantity": 18, "current_price": 100000},
        ],
    }
    execution = {
        "schema_version": "1",
        "request_type": "real-submit",
        "requires_main_agent_order_execution": True,
        "required_main_agent_actions": ["refresh_active_order_lookup", "refresh_order_available_lookup", "continue_order_execution"],
        "errors": [{"code": "order_submission_blocked"}],
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 6, "order_price": 70000, "direction": "sell", "final_first_score": 3.5, "result": "blocked"},
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 20, "order_price": 100000, "direction": "buy", "final_first_score": 6.5, "result": "blocked"},
        ],
    }
    write_json(root / "account-before-order.json", account)
    write_json(root / "execution.json", execution)
    payload = execute(argparse.Namespace(output_dir=str(root), execution_json="", account_before_order="", env="real", submit=False, offline=True, retries=0, reservation_start_date="", reservation_end_date=""))
    orders = {item["symbol_id"]: item for item in payload["orders"]}
    failures = []
    if payload.get("requires_main_agent_order_execution") is not False:
        failures.append("requires_main_agent_order_execution not cleared")
    if orders["005930"].get("reason") != "existing_matching_reservation_kept":
        failures.append("matching existing reservation not kept")
    if orders["000270"].get("reason") != "buy_cash_limit_missing":
        failures.append(f"dry-run buy without max_buy_amt was not blocked: {orders['000270']}")
    account_after = load_json(root / "account-before-order.json")
    if account_after.get("active_order_lookup_performed") is not True or account_after.get("order_available_lookup_performed") is not True:
        failures.append("account gates not refreshed")
    if account_after.get("order_gate_status") != "success":
        failures.append(f"successful gate refresh was not recorded: {account_after.get('order_gate_status')}")
    write_json(root / "execution.json", {**execution, "orders": [{"symbol_id": "005930", "symbol_name": "삼성전자", "order_price": 70000, "direction": "sell", "final_first_score": 3.5, "result": "blocked"}]})
    invalid_final_payload = execute(argparse.Namespace(output_dir=str(root), execution_json="", account_before_order="", env="real", submit=False, offline=True, retries=0, reservation_start_date="", reservation_end_date=""))
    invalid_final_order = invalid_final_payload["orders"][0]
    if invalid_final_order.get("reason") != "invalid_final_holding_quantity":
        failures.append(f"missing final_holding_quantity was not blocked as invalid: {invalid_final_order}")
    if invalid_final_order.get("validated_order_quantity") != 0 or invalid_final_order.get("additional_required_quantity") != 0:
        failures.append(f"missing final_holding_quantity was converted into an order quantity: {invalid_final_order}")
    (root / "portfolio-except.txt").write_text("# excluded\n000270, 005930\n", encoding="utf-8")
    portfolio_except_execution = {
        "orders": [
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 20, "order_price": 100000, "direction": "buy", "final_first_score": 6.5},
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 6, "order_price": 70000, "direction": "sell", "final_first_score": 3.5},
            {"symbol_id": "035420", "symbol_name": "NAVER", "final_holding_quantity": 1, "order_price": 200000, "direction": "none"},
        ]
    }
    reconcile(
        {"account_summary": {"cash_amount": 10_000_000}, "symbols": [
            {"symbol_id": "000270", "symbol_name": "기아", "current_live_holding_quantity": 18, "current_price": 100000},
            {"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 8, "current_price": 70000},
            {"symbol_id": "035420", "symbol_name": "NAVER", "current_live_holding_quantity": 1, "current_price": 200000},
        ]},
        portfolio_except_execution,
        [],
        {"000270": {"max_buy_qty": 5, "max_buy_amt": 1_000_000}},
        {"005930": {"max_sell_qty": 5}},
        submit=False,
        kis=None,
    )
    portfolio_except_orders = {item["symbol_id"]: item for item in portfolio_except_execution["orders"]}
    for excluded_symbol in ("000270", "005930"):
        excluded_order = portfolio_except_orders[excluded_symbol]
        if excluded_order.get("result") != "blocked" or excluded_order.get("reason") != "symbol_in_portfolio_except_list":
            failures.append(f"portfolio-except symbol was not blocked: {excluded_order}")
        if excluded_order.get("validated_order_quantity") != 0:
            failures.append(f"portfolio-except symbol produced an order quantity: {excluded_order}")
    if portfolio_except_orders["035420"].get("reason") == "symbol_in_portfolio_except_list":
        failures.append(f"symbol outside portfolio-except list was blocked: {portfolio_except_orders['035420']}")
    (root / "portfolio-except.txt").unlink()
    return failures


def step_score_band_and_reservation_normalization_checks(root: Path) -> list[str]:
    """Score-band buy/sell gates, cancel-exempt active orders, reservation normalization, and KRX tick rounding."""
    failures: list[str] = []
    execution = {
        "schema_version": "1",
        "request_type": "real-submit",
        "requires_main_agent_order_execution": True,
        "required_main_agent_actions": ["refresh_active_order_lookup", "refresh_order_available_lookup", "continue_order_execution"],
        "errors": [{"code": "order_submission_blocked"}],
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 6, "order_price": 70000, "direction": "sell", "final_first_score": 3.5, "result": "blocked"},
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 20, "order_price": 100000, "direction": "buy", "final_first_score": 6.5, "result": "blocked"},
        ],
    }
    score_band_execution = {
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 6, "final_first_score": 4.0, "order_price": 70000, "order_path": "immediate"},
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 20, "final_first_score": 6.0, "order_price": 100000, "order_path": "immediate"},
            {"symbol_id": "000810", "symbol_name": "삼성화재", "final_holding_quantity": 0, "order_price": 400000, "order_path": "immediate"},
        ]
    }
    reconcile(
        {"account_summary": {"cash_amount": 10_000_000}, "symbols": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 8, "current_price": 70000},
            {"symbol_id": "000270", "symbol_name": "기아", "current_live_holding_quantity": 18, "current_price": 100000},
            {"symbol_id": "000810", "symbol_name": "삼성화재", "current_live_holding_quantity": 1, "current_price": 400000},
        ]},
        score_band_execution,
        [],
        {"000270": {"max_buy_qty": 5, "max_buy_amt": 1_000_000}},
        {"005930": {"max_sell_qty": 5}, "000810": {"max_sell_qty": 5}},
        submit=False,
        kis=None,
    )
    score_band_orders = {item["symbol_id"]: item for item in score_band_execution["orders"]}
    if score_band_orders["005930"].get("reason") != "validated_dry_run_not_submitted":
        failures.append(f"sell at boundary score 4.0 was not allowed: {score_band_orders['005930']}")
    if score_band_orders["000270"].get("reason") != "validated_dry_run_not_submitted":
        failures.append(f"buy at boundary score 6.0 was not allowed: {score_band_orders['000270']}")
    if score_band_orders["000810"].get("result") != "blocked" or score_band_orders["000810"].get("reason") != "score_band_value_missing":
        failures.append(f"missing score did not fail safe: {score_band_orders['000810']}")
    score_band_cancel_execution = {
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 8, "final_first_score": 5.0, "order_price": 70000, "order_path": "reservation"},
        ]
    }
    reconcile(
        {"account_summary": {"cash_amount": 10_000_000}, "symbols": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 8, "current_price": 70000},
        ]},
        score_band_cancel_execution,
        [{"symbol_id": "005930", "symbol_name": "삼성전자", "order_id": "r9", "order_kind": "reservation", "direction": "sell", "remaining_quantity": 2, "order_price": 70000, "active_status": "active", "order_api": "order_resv", "order_path": "reservation", "execution_environment": "real", "observed_at": now_iso()}],
        {},
        {"005930": {"max_sell_qty": 5}},
        submit=True,
        kis=None,
    )
    cancel_order = score_band_cancel_execution["orders"][0]
    if cancel_order.get("reason") in {"sell_blocked_score_band", "buy_blocked_score_band", "score_band_value_missing"}:
        failures.append(f"cancel of an active order must be exempt from the score-band gate: {cancel_order}")
    write_json(root / "execution.json", execution)

    reservation = normalize_reservation(
        {
            "rsvn_ord_seq": "116426",
            "rsvn_ord_ord_dt": "20260621",
            "pdno": "039490",
            "ord_rsvn_qty": "2",
            "ord_rsvn_unpr": "346000",
            "kor_item_shtn_name": "키움증권",
            "sll_buy_dvsn_cd": "02",
            "prcs_rslt": "접수",
        }
    )
    if reservation.get("remaining_quantity") != 2 or reservation.get("order_price") != 346000:
        failures.append(f"official reservation columns were not normalized: {reservation}")
    if reservation.get("symbol_name") != "키움증권" or reservation.get("active_status") != "active":
        failures.append(f"official reservation identity/status was not normalized: {reservation}")
    if reservation.get("rsvn_ord_orgno") != default_reservation_orgno():
        failures.append(f"reservation orgno fallback was not applied: {reservation}")

    filled_reservation = normalize_reservation(
        {
            "rsvn_ord_seq": "116426",
            "rsvn_ord_ord_dt": "20260622",
            "pdno": "039490",
            "ord_rsvn_qty": "2",
            "tot_ccld_qty": "2",
            "ord_rsvn_unpr": "346000",
            "kor_item_shtn_name": "키움증권",
            "sll_buy_dvsn_cd": "02",
            "prcs_rslt": "처리",
            "ord_tmd": "082209",
        }
    )
    if filled_reservation.get("remaining_quantity") != 0 or filled_reservation.get("active_status") != "inactive":
        failures.append(f"filled reservation was not marked inactive: {filled_reservation}")

    processed_unfilled_reservation = normalize_reservation(
        {
            "rsvn_ord_seq": "137661",
            "rsvn_ord_ord_dt": "20260622",
            "pdno": "032830",
            "ord_rsvn_qty": "1",
            "tot_ccld_qty": "0",
            "ord_rsvn_unpr": "497000",
            "kor_item_shtn_name": "삼성생명",
            "sll_buy_dvsn_cd": "01",
            "prcs_rslt": "처리",
            "ord_tmd": "082228",
        }
    )
    if processed_unfilled_reservation.get("remaining_quantity") != 1 or processed_unfilled_reservation.get("active_status") != "inactive":
        failures.append(f"processed unfilled reservation was not marked inactive: {processed_unfilled_reservation}")

    rejected_reservation = normalize_reservation(
        {
            "rsvn_ord_seq": "137656",
            "rsvn_ord_ord_dt": "20260622",
            "pdno": "000270",
            "ord_rsvn_qty": "2",
            "tot_ccld_qty": "0",
            "ord_rsvn_unpr": "154900",
            "kor_item_shtn_name": "기아",
            "sll_buy_dvsn_cd": "02",
            "prcs_rslt": "미처리",
        }
    )
    if rejected_reservation.get("remaining_quantity") != 2 or rejected_reservation.get("active_status") != "inactive":
        failures.append(f"rejected reservation was not marked inactive: {rejected_reservation}")

    if normalize_limit_price(474250, "sell") != 474000:
        failures.append("sell limit price was not rounded down to KRX tick")
    if normalize_limit_price(474250, "buy") != 474500:
        failures.append("buy limit price was not rounded up to KRX tick")
    return failures


def step_reservation_cancel_request_checks(root: Path) -> list[str]:
    """A reservation cancel request is built from the normalized active order fields."""
    failures: list[str] = []
    captured_payloads: list[dict[str, Any]] = []
    original_retry_json = execute_orders_module.retry_json

    def fake_retry_json(method: str, url: str, headers: dict[str, Any], payload: dict[str, Any] | None = None, retries: int = 0) -> tuple[dict[str, Any], dict[str, str]]:
        captured_payloads.append(dict(payload or {}))
        return {"rt_cd": "0", "output": {"RSVN_ORD_SEQ": "116426"}}, {}


    try:
        execute_orders_module.retry_json = fake_retry_json
        request_id = adjust_reservation(
            FakeKis(),
            {
                "order_id": "116426",
                "rsvn_ord_seq": "116426",
                "rsvn_ord_orgno": default_reservation_orgno(),
                "rsvn_ord_ord_dt": "20260621",
            },
            None,
        )
    finally:
        execute_orders_module.retry_json = original_retry_json
    if request_id != "116426" or not captured_payloads:
        failures.append("reservation cancel request was not built from normalized active order")
    elif captured_payloads[0].get("RSVN_ORD_SEQ") != "116426":
        failures.append(f"reservation cancel payload lost sequence id: {captured_payloads[0]}")
    elif captured_payloads[0].get("RSVN_ORD_ORGNO") != default_reservation_orgno():
        failures.append(f"reservation cancel payload lost default orgno: {captured_payloads[0]}")
    elif captured_payloads[0].get("RSVN_ORD_ORD_DT") != "20260621":
        failures.append(f"reservation cancel payload lost order date: {captured_payloads[0]}")
    elif captured_payloads[0].get("ORD_TYPE") != "cancel":
        failures.append(f"reservation cancel payload lost order type: {captured_payloads[0]}")
    return failures


def step_active_order_conflict_checks(root: Path) -> list[str]:
    """Conflicting/incomplete/multiple active orders block execution until resolved."""
    failures: list[str] = []
    account_after = load_json(root / "account-before-order.json")
    account_after["active_orders"] = [{"symbol_id": "000660", "symbol_name": "SK하이닉스", "order_id": "r2", "order_kind": "reservation", "direction": "buy", "remaining_quantity": 1, "order_price": 140000, "active_status": "active", "order_api": "order_resv", "order_path": "reservation", "execution_environment": "real", "observed_at": now_iso()}]
    write_json(root / "account-before-order.json", account_after)
    write_json(
        root / "execution.json",
        {
            "schema_version": "1",
            "request_type": "real-submit",
            "requires_main_agent_order_execution": True,
            "required_main_agent_actions": ["continue_order_execution"],
            "errors": [],
            "orders": [{"symbol_id": "000660", "symbol_name": "SK하이닉스", "final_holding_quantity": 2, "order_price": 150000, "direction": "buy", "final_first_score": 6.5, "result": "blocked"}],
        },
    )
    mismatch_payload = execute(argparse.Namespace(output_dir=str(root), execution_json="", account_before_order="", env="real", submit=False, offline=True, retries=0, reservation_start_date="", reservation_end_date=""))
    mismatch_order = mismatch_payload["orders"][0]
    if mismatch_order.get("reason") != "active_order_adjustment_required":
        failures.append(f"mismatched same-direction active reservation was not blocked: {mismatch_order}")

    account_after["active_orders"] = [{"symbol_id": "005930", "symbol_name": "삼성전자", "order_id": "r3", "direction": "sell", "remaining_quantity": 2, "order_price": 70000, "active_status": "active"}]
    write_json(root / "account-before-order.json", account_after)
    write_json(
        root / "execution.json",
        {
            "schema_version": "1",
            "request_type": "real-submit",
            "requires_main_agent_order_execution": True,
            "required_main_agent_actions": ["continue_order_execution"],
            "errors": [],
            "orders": [{"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 6, "order_price": 70000, "direction": "sell", "final_first_score": 3.5, "result": "blocked"}],
        },
    )
    missing_field_payload = execute(argparse.Namespace(output_dir=str(root), execution_json="", account_before_order="", env="real", submit=False, offline=True, retries=0, reservation_start_date="", reservation_end_date=""))
    missing_field_order = missing_field_payload["orders"][0]
    if missing_field_order.get("reason") != "active_order_required_fields_missing":
        failures.append(f"active reservation missing api/path was not blocked: {missing_field_order}")

    account_after["active_orders"] = []
    write_json(root / "account-before-order.json", account_after)
    write_json(
        root / "execution.json",
        {
            "schema_version": "1",
            "request_type": "real-submit",
            "requires_main_agent_order_execution": True,
            "required_main_agent_actions": ["continue_order_execution"],
            "errors": [],
            "orders": [{"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 20, "order_price": 100000, "order_path": "immediate", "direction": "buy", "final_first_score": 6.5, "result": "blocked"}],
        },
    )
    immediate_payload = execute(argparse.Namespace(output_dir=str(root), execution_json="", account_before_order="", env="real", submit=False, offline=True, retries=0, reservation_start_date="", reservation_end_date=""))
    immediate_order = immediate_payload["orders"][0]
    if immediate_order.get("order_api") != "order_cash" or immediate_order.get("order_path") != "immediate":
        failures.append(f"immediate order path did not select order_cash: {immediate_order}")
    if immediate_order.get("reason") != "buy_cash_limit_missing":
        failures.append(f"immediate dry-run without max_buy_amt was not blocked: {immediate_order}")

    account_after["active_orders"] = [
        {"symbol_id": "000270", "symbol_name": "기아", "order_id": "c1", "order_kind": "pending", "direction": "buy", "remaining_quantity": 1, "order_price": 100000, "active_status": "active", "order_api": "order_cash", "order_path": "immediate", "execution_environment": "real", "observed_at": now_iso()},
        {"symbol_id": "000270", "symbol_name": "기아", "order_id": "c2", "order_kind": "pending", "direction": "buy", "remaining_quantity": 1, "order_price": 100000, "active_status": "active", "order_api": "order_cash", "order_path": "immediate", "execution_environment": "real", "observed_at": now_iso()},
    ]
    write_json(root / "account-before-order.json", account_after)
    write_json(
        root / "execution.json",
        {
            "schema_version": "1",
            "request_type": "real-submit",
            "requires_main_agent_order_execution": True,
            "required_main_agent_actions": ["continue_order_execution"],
            "errors": [],
            "orders": [{"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 20, "order_price": 100000, "order_path": "immediate", "direction": "buy", "final_first_score": 6.5, "result": "blocked"}],
        },
    )
    multiple_payload = execute(argparse.Namespace(output_dir=str(root), execution_json="", account_before_order="", env="real", submit=False, offline=True, retries=0, reservation_start_date="", reservation_end_date=""))
    multiple_order = multiple_payload["orders"][0]
    if multiple_order.get("reason") != "multiple_active_orders_require_manual_review":
        failures.append(f"multiple active immediate orders were not blocked: {multiple_order}")
    return failures


def step_reduction_and_available_cash_checks(root: Path) -> list[str]:
    """Buy/sell quantities are reduced to KIS order-available caps and remaining cash."""
    failures: list[str] = []
    reduction_account = {
        "account_summary": {"cash_amount": 1_000_000},
        "symbols": [
            {"symbol_id": "000270", "symbol_name": "기아", "current_live_holding_quantity": 0},
            {"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 10},
            {"symbol_id": "000810", "symbol_name": "삼성화재", "current_live_holding_quantity": 0},
        ],
    }
    reduction_execution = {
        "orders": [
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 12, "final_first_score": 6.5, "order_price": 100000, "order_path": "immediate"},
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 4, "final_first_score": 3.5, "order_price": 70000, "order_path": "immediate"},
            {"symbol_id": "000810", "symbol_name": "삼성화재", "final_holding_quantity": 3, "final_first_score": 6.5, "order_price": 400000, "order_path": "immediate"},
        ]
    }
    reconcile(
        reduction_account,
        reduction_execution,
        [],
        {"000270": {"max_buy_qty": 3, "max_buy_amt": 1_000_000}, "000810": {"max_buy_qty": 3, "max_buy_amt": 1_000_000}},
        {"005930": {"max_sell_qty": 2}},
        submit=False,
        kis=None,
    )
    reduction_orders = {item["symbol_id"]: item for item in reduction_execution["orders"]}
    if reduction_orders["000270"].get("validated_order_quantity") != 3:
        failures.append(f"buy order was not reduced to max_buy_qty: {reduction_orders['000270']}")
    if (reduction_orders["000270"].get("quantity_adjustment") or {}).get("reason") != "buy_quantity_reduced_to_order_available_quantity":
        failures.append(f"buy order reduction reason missing: {reduction_orders['000270']}")
    if reduction_orders["005930"].get("validated_order_quantity") != 2:
        failures.append(f"sell order was not reduced to max_sell_qty: {reduction_orders['005930']}")
    if (reduction_orders["005930"].get("quantity_adjustment") or {}).get("reason") != "sell_quantity_reduced_to_order_available_quantity":
        failures.append(f"sell order reduction reason missing: {reduction_orders['005930']}")
    if reduction_orders["000810"].get("validated_order_quantity") != 1:
        failures.append(f"buy order was not reduced to remaining cash: {reduction_orders['000810']}")
    if (reduction_orders["000810"].get("quantity_adjustment") or {}).get("reason") != "buy_quantity_reduced_to_remaining_cash":
        failures.append(f"cash reduction reason missing: {reduction_orders['000810']}")

    order_available_cash_execution = {
        "orders": [
            {"symbol_id": "000660", "symbol_name": "SK하이닉스", "final_holding_quantity": 1, "final_first_score": 6.5, "order_price": 2_955_000, "order_path": "immediate"}
        ]
    }
    reconcile(
        {"account_summary": {"cash_amount": 861_938}, "symbols": [{"symbol_id": "000660", "symbol_name": "SK하이닉스", "current_live_holding_quantity": 0}]},
        order_available_cash_execution,
        [],
        {"000660": {"max_buy_qty": 1, "max_buy_amt": 4_181_123}},
        {},
        submit=False,
        kis=None,
    )
    order_available_cash_order = order_available_cash_execution["orders"][0]
    if order_available_cash_execution.get("latest_available_cash") != 4_181_123:
        failures.append(f"latest available cash should come from KIS max_buy_amt: {order_available_cash_execution}")
    if order_available_cash_order.get("reason") != "validated_dry_run_not_submitted":
        failures.append(f"KIS order-available cash should allow one high-price share despite lower cash_amount: {order_available_cash_order}")

    order_available_multi_execution = {
        "orders": [
            {"symbol_id": "000660", "symbol_name": "SK하이닉스", "final_holding_quantity": 1, "final_first_score": 6.5, "order_price": 2_000_000, "order_path": "immediate"},
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 10, "final_first_score": 6.5, "order_price": 100_000, "order_path": "immediate"},
        ]
    }
    reconcile(
        {
            "account_summary": {"cash_amount": 100_000},
            "symbols": [
                {"symbol_id": "000660", "symbol_name": "SK하이닉스", "current_live_holding_quantity": 0},
                {"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 0},
            ],
        },
        order_available_multi_execution,
        [],
        {"000660": {"max_buy_qty": 1, "max_buy_amt": 2_500_000}, "005930": {"max_buy_qty": 10, "max_buy_amt": 2_500_000}},
        {},
        submit=False,
        kis=None,
    )
    order_available_multi_orders = {item["symbol_id"]: item for item in order_available_multi_execution["orders"]}
    if order_available_multi_execution.get("latest_available_cash") != 2_500_000:
        failures.append(f"multi-buy latest cash should come from KIS max_buy_amt: {order_available_multi_execution}")
    if order_available_multi_orders["000660"].get("reason") != "validated_dry_run_not_submitted":
        failures.append(f"first KIS-cash buy should pass despite lower cash_amount: {order_available_multi_orders['000660']}")
    if order_available_multi_orders["005930"].get("validated_order_quantity") != 5:
        failures.append(f"second KIS-cash buy should be reduced to remaining KIS cash: {order_available_multi_orders['005930']}")
    if (order_available_multi_orders["005930"].get("quantity_adjustment") or {}).get("reason") != "buy_quantity_reduced_to_remaining_cash":
        failures.append(f"second KIS-cash buy reduction reason missing: {order_available_multi_orders['005930']}")
    return failures


def step_max_buy_amt_and_zero_capacity_checks(root: Path) -> list[str]:
    """Missing/mixed/zero max_buy_amt and zero order-available capacity all block correctly."""
    failures: list[str] = []
    reduction_account = {
        "account_summary": {"cash_amount": 1_000_000},
        "symbols": [
            {"symbol_id": "000270", "symbol_name": "기아", "current_live_holding_quantity": 0},
            {"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 10},
            {"symbol_id": "000810", "symbol_name": "삼성화재", "current_live_holding_quantity": 0},
        ],
    }
    missing_max_buy_amt_execution = {
        "orders": [
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 3, "final_first_score": 6.5, "order_price": 100_000, "order_path": "immediate"}
        ]
    }
    reconcile(
        {"account_summary": {"cash_amount": 250_000}, "symbols": [{"symbol_id": "000270", "symbol_name": "기아", "current_live_holding_quantity": 0}]},
        missing_max_buy_amt_execution,
        [],
        {},
        {},
        submit=False,
        kis=None,
    )
    missing_max_buy_amt_order = missing_max_buy_amt_execution["orders"][0]
    if missing_max_buy_amt_execution.get("latest_available_cash") is not None:
        failures.append(f"latest available cash should stay unset without max_buy_amt: {missing_max_buy_amt_execution}")
    if missing_max_buy_amt_order.get("reason") != "buy_cash_limit_missing":
        failures.append(f"buy without max_buy_amt should be blocked instead of using account cash: {missing_max_buy_amt_order}")

    mixed_max_buy_amt_execution = {
        "orders": [
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 1, "final_first_score": 6.5, "order_price": 100_000, "order_path": "immediate"},
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 1, "final_first_score": 6.5, "order_price": 70_000, "order_path": "immediate"},
        ]
    }
    reconcile(
        {
            "account_summary": {"cash_amount": 1_000_000},
            "symbols": [
                {"symbol_id": "000270", "symbol_name": "기아", "current_live_holding_quantity": 0},
                {"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 0},
            ],
        },
        mixed_max_buy_amt_execution,
        [],
        {"000270": {"max_buy_qty": 1}, "005930": {"max_buy_qty": 1, "max_buy_amt": 1_000_000}},
        {},
        submit=False,
        kis=None,
    )
    mixed_max_buy_amt_orders = {item["symbol_id"]: item for item in mixed_max_buy_amt_execution["orders"]}
    if mixed_max_buy_amt_orders["000270"].get("reason") != "buy_cash_limit_missing":
        failures.append(f"symbol missing max_buy_amt should not use another symbol's max_buy_amt: {mixed_max_buy_amt_orders['000270']}")
    if mixed_max_buy_amt_orders["005930"].get("reason") != "validated_dry_run_not_submitted":
        failures.append(f"symbol with max_buy_amt should still pass: {mixed_max_buy_amt_orders['005930']}")

    zero_max_buy_amt_execution = {
        "orders": [
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 1, "final_first_score": 6.5, "order_price": 100_000, "order_path": "immediate"}
        ]
    }
    reconcile(
        {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "000270", "symbol_name": "기아", "current_live_holding_quantity": 0}]},
        zero_max_buy_amt_execution,
        [],
        {"000270": {"max_buy_qty": 1, "max_buy_amt": 0}},
        {},
        submit=False,
        kis=None,
    )
    zero_max_buy_amt_order = zero_max_buy_amt_execution["orders"][0]
    if zero_max_buy_amt_order.get("reason") != "buy_cash_limit_missing":
        failures.append(f"zero max_buy_amt should block buy order: {zero_max_buy_amt_order}")

    zero_capacity_execution = {
        "orders": [
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 2, "final_first_score": 6.5, "order_price": 100000, "order_path": "immediate"},
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 0, "final_first_score": 3.5, "order_price": 70000, "order_path": "immediate"},
        ]
    }
    reconcile(
        reduction_account,
        zero_capacity_execution,
        [],
        {"000270": {"max_buy_qty": 0, "max_buy_amt": 1_000_000}},
        {"005930": {"max_sell_qty": 0}},
        submit=False,
        kis=None,
    )
    zero_orders = {item["symbol_id"]: item for item in zero_capacity_execution["orders"]}
    if zero_orders["000270"].get("reason") != "buy_quantity_exceeds_order_available_quantity":
        failures.append(f"zero max_buy_qty did not block buy order: {zero_orders['000270']}")
    if zero_orders["005930"].get("reason") != "sell_quantity_exceeds_order_available_quantity":
        failures.append(f"zero max_sell_qty did not block sell order: {zero_orders['005930']}")
    return failures


def step_active_sell_correction_checks(root: Path) -> list[str]:
    """A covered active sell is corrected in place instead of hitting a new sell gate."""
    failures: list[str] = []
    active_sell_base = {
        "symbol_id": "402340",
        "symbol_name": "SK스퀘어",
        "order_id": "old-sell",
        "order_kind": "pending",
        "direction": "sell",
        "final_first_score": 3.5,
        "remaining_quantity": 1,
        "order_price": 1_630_000,
        "active_status": "active",
        "order_api": "order_cash",
        "order_path": "immediate",
        "execution_environment": "real",
        "observed_at": now_iso(),
        "krx_fwdg_ord_orgno": "91255",
        "orgn_odno": "old-sell",
    }

    active_sell_correction_execution = {
        "orders": [
            {"symbol_id": "402340", "symbol_name": "SK스퀘어", "final_holding_quantity": 0, "final_first_score": 3.5, "order_price": 1_595_000, "order_path": "immediate"}
        ]
    }
    original_adjust_active_order = execute_orders_module.adjust_active_order
    original_submit_order = execute_orders_module.submit_order
    correction_adjustments: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    unexpected_submissions: list[dict[str, Any]] = []

    def fake_correct_active_sell(kis: Any, active: dict[str, Any], desired: dict[str, Any] | None) -> tuple[str, str, str]:
        correction_adjustments.append((dict(active), dict(desired) if desired else None))
        return "correct-sell", "correct", "fake active sell correction"

    def fake_unexpected_submit(kis: Any, order: dict[str, Any]) -> str:
        unexpected_submissions.append(dict(order))
        return "unexpected-submit"

    try:
        execute_orders_module.adjust_active_order = fake_correct_active_sell
        execute_orders_module.submit_order = fake_unexpected_submit
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "402340", "symbol_name": "SK스퀘어", "current_live_holding_quantity": 1}]},
            active_sell_correction_execution,
            [dict(active_sell_base)],
            {},
            {"402340": {"max_sell_qty": 0}},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.adjust_active_order = original_adjust_active_order
        execute_orders_module.submit_order = original_submit_order
    active_sell_correction_order = active_sell_correction_execution["orders"][0]
    if active_sell_correction_order.get("result") != "submitted" or active_sell_correction_order.get("reason") != "active_order_correction_submitted":
        failures.append(f"covered active sell was not corrected without new sell gate: {active_sell_correction_order}")
    if active_sell_correction_order.get("reason") in {"sell_quantity_exceeds_available_holding", "sell_quantity_exceeds_order_available_quantity"}:
        failures.append(f"covered active sell hit a sell gate: {active_sell_correction_order}")
    if unexpected_submissions:
        failures.append(f"covered active sell correction should not submit a new order: {unexpected_submissions}")
    if not correction_adjustments or (correction_adjustments[0][1] or {}).get("validated_order_quantity") != 1:
        failures.append(f"covered active sell correction used wrong desired order: {correction_adjustments}")
    return failures


def step_active_sell_partial_and_cancel_only_checks(root: Path) -> list[str]:
    """A partially-covered active sell submits only the delta; a fully-covered one can cancel without replacement."""
    failures: list[str] = []
    active_sell_base = {
        "symbol_id": "402340",
        "symbol_name": "SK스퀘어",
        "order_id": "old-sell",
        "order_kind": "pending",
        "direction": "sell",
        "final_first_score": 3.5,
        "remaining_quantity": 1,
        "order_price": 1_630_000,
        "active_status": "active",
        "order_api": "order_cash",
        "order_path": "immediate",
        "execution_environment": "real",
        "observed_at": now_iso(),
        "krx_fwdg_ord_orgno": "91255",
        "orgn_odno": "old-sell",
    }
    original_adjust_active_order = execute_orders_module.adjust_active_order
    original_submit_order = execute_orders_module.submit_order
    partial_active_sell_execution = {
        "orders": [
            {"symbol_id": "402340", "symbol_name": "SK스퀘어", "final_holding_quantity": 1, "final_first_score": 3.5, "order_price": 1_630_000, "order_path": "immediate"}
        ]
    }
    partial_submissions: list[dict[str, Any]] = []

    def fake_submit_partial_sell(kis: Any, order: dict[str, Any]) -> str:
        partial_submissions.append(dict(order))
        return "new-sell"

    try:
        execute_orders_module.submit_order = fake_submit_partial_sell
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "402340", "symbol_name": "SK스퀘어", "current_live_holding_quantity": 5}]},
            partial_active_sell_execution,
            [dict(active_sell_base, remaining_quantity=2)],
            {},
            {"402340": {"max_sell_qty": 1}},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.submit_order = original_submit_order
    partial_active_sell_order = partial_active_sell_execution["orders"][0]
    if partial_active_sell_order.get("result") != "submitted" or partial_active_sell_order.get("reason") != "active_order_kept_and_additional_order_submitted":
        failures.append(f"partial active sell did not submit only additional quantity: {partial_active_sell_order}")
    if partial_active_sell_order.get("validated_order_quantity") != 1:
        failures.append(f"partial active sell additional quantity was not gated independently: {partial_active_sell_order}")
    if (partial_active_sell_order.get("quantity_adjustment") or {}).get("reason") != "sell_quantity_reduced_to_order_available_quantity":
        failures.append(f"partial active sell additional gate reason missing: {partial_active_sell_order}")
    if not partial_submissions or partial_submissions[0].get("validated_order_quantity") != 1:
        failures.append(f"partial active sell submitted wrong additional order: {partial_submissions}")

    active_sell_cancel_only_execution = {
        "orders": [
            {"symbol_id": "402340", "symbol_name": "SK스퀘어", "final_holding_quantity": 1, "final_first_score": 6.5, "order_price": 1_595_000, "order_path": "immediate"}
        ]
    }
    cancel_only_adjustments: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    cancel_only_submissions: list[dict[str, Any]] = []

    def fake_cancel_active_sell(kis: Any, active: dict[str, Any], desired: dict[str, Any] | None) -> tuple[str, str, str]:
        cancel_only_adjustments.append((dict(active), dict(desired) if desired else None))
        return "cancel-sell", "cancel", "fake active sell cancel"

    def fake_cancel_only_submit(kis: Any, order: dict[str, Any]) -> str:
        cancel_only_submissions.append(dict(order))
        return "unexpected-buy"

    try:
        execute_orders_module.adjust_active_order = fake_cancel_active_sell
        execute_orders_module.submit_order = fake_cancel_only_submit
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "402340", "symbol_name": "SK스퀘어", "current_live_holding_quantity": 1}]},
            active_sell_cancel_only_execution,
            [dict(active_sell_base)],
            {"402340": {"max_buy_qty": 1, "max_buy_amt": 1_000_000}},
            {},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.adjust_active_order = original_adjust_active_order
        execute_orders_module.submit_order = original_submit_order
    active_sell_cancel_only_order = active_sell_cancel_only_execution["orders"][0]
    if active_sell_cancel_only_order.get("result") != "submitted" or active_sell_cancel_only_order.get("reason") != "active_order_cancel_submitted":
        failures.append(f"active sell cancel-only case was not cancel-only: {active_sell_cancel_only_order}")
    if not cancel_only_adjustments or cancel_only_adjustments[0][1] is not None:
        failures.append(f"active sell cancel-only should cancel without desired replacement: {cancel_only_adjustments}")
    if cancel_only_submissions:
        failures.append(f"active sell cancel-only should not submit replacement buy: {cancel_only_submissions}")
    return failures


def step_active_sell_replacement_buy_checks(root: Path) -> list[str]:
    """A covered active sell that flips direction is cancelled and replaced with the new-direction order."""
    failures: list[str] = []
    active_sell_base = {
        "symbol_id": "402340",
        "symbol_name": "SK스퀘어",
        "order_id": "old-sell",
        "order_kind": "pending",
        "direction": "sell",
        "final_first_score": 3.5,
        "remaining_quantity": 1,
        "order_price": 1_630_000,
        "active_status": "active",
        "order_api": "order_cash",
        "order_path": "immediate",
        "execution_environment": "real",
        "observed_at": now_iso(),
        "krx_fwdg_ord_orgno": "91255",
        "orgn_odno": "old-sell",
    }
    original_adjust_active_order = execute_orders_module.adjust_active_order
    original_submit_order = execute_orders_module.submit_order
    active_sell_replacement_buy_execution = {
        "orders": [
            {"symbol_id": "402340", "symbol_name": "SK스퀘어", "final_holding_quantity": 2, "final_first_score": 6.5, "order_price": 1_595_000, "order_path": "immediate"}
        ]
    }
    replacement_buy_adjustments: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    replacement_buy_submissions: list[dict[str, Any]] = []

    def fake_cancel_for_replacement_buy(kis: Any, active: dict[str, Any], desired: dict[str, Any] | None) -> tuple[str, str, str]:
        replacement_buy_adjustments.append((dict(active), dict(desired) if desired else None))
        return "cancel-before-buy", "cancel", "fake active sell cancel"

    def fake_submit_replacement_buy(kis: Any, order: dict[str, Any]) -> str:
        replacement_buy_submissions.append(dict(order))
        return "replacement-buy"

    try:
        execute_orders_module.adjust_active_order = fake_cancel_for_replacement_buy
        execute_orders_module.submit_order = fake_submit_replacement_buy
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "402340", "symbol_name": "SK스퀘어", "current_live_holding_quantity": 1}]},
            active_sell_replacement_buy_execution,
            [dict(active_sell_base)],
            {"402340": {"max_buy_qty": 1, "max_buy_amt": 2_000_000}},
            {},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.adjust_active_order = original_adjust_active_order
        execute_orders_module.submit_order = original_submit_order
    active_sell_replacement_buy_order = active_sell_replacement_buy_execution["orders"][0]
    if active_sell_replacement_buy_order.get("result") != "submitted" or active_sell_replacement_buy_order.get("reason") != "active_order_cancel_and_replacement_submitted":
        failures.append(f"active sell replacement-buy case did not cancel and buy: {active_sell_replacement_buy_order}")
    if not replacement_buy_adjustments or replacement_buy_adjustments[0][1] is not None:
        failures.append(f"active sell replacement-buy should cancel before replacement: {replacement_buy_adjustments}")
    if not replacement_buy_submissions or replacement_buy_submissions[0].get("direction") != "buy" or replacement_buy_submissions[0].get("validated_order_quantity") != 1:
        failures.append(f"active sell replacement buy used wrong replacement order: {replacement_buy_submissions}")
    return failures


def step_active_order_additional_and_invalid_price_checks(root: Path) -> list[str]:
    """A same-direction active order is kept and only the additional delta is submitted; invalid price/quantity blocks it."""
    failures: list[str] = []
    active_additional_execution = {
        "orders": [
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 5, "final_first_score": 6.5, "order_price": 100000, "order_path": "immediate"}
        ]
    }
    original_adjust_active_order = execute_orders_module.adjust_active_order
    original_submit_order = execute_orders_module.submit_order
    additional_submissions: list[dict[str, Any]] = []

    def fake_adjust_active_order(kis: Any, active: dict[str, Any], desired: dict[str, Any] | None) -> tuple[str, str, str]:
        failures.append(f"same-direction active order should be kept, not adjusted: active={active}, desired={desired}")
        return "adj1", "correct", "unexpected correction"

    def fake_submit_additional_order(kis: Any, order: dict[str, Any]) -> str:
        additional_submissions.append(dict(order))
        return "new-buy"

    try:
        execute_orders_module.adjust_active_order = fake_adjust_active_order
        execute_orders_module.submit_order = fake_submit_additional_order
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "000270", "symbol_name": "기아", "current_live_holding_quantity": 0}]},
            active_additional_execution,
            [
                {
                    "symbol_id": "000270",
                    "symbol_name": "기아",
                    "order_id": "a1",
                    "order_kind": "pending",
                    "direction": "buy",
                    "final_first_score": 6.5,
                    "remaining_quantity": 1,
                    "order_price": 100000,
                    "active_status": "active",
                    "order_api": "order_cash",
                    "order_path": "immediate",
                    "execution_environment": "real",
                    "observed_at": now_iso(),
                }
            ],
            {"000270": {"max_buy_qty": 2, "max_buy_amt": 1_000_000}},
            {},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.adjust_active_order = original_adjust_active_order
        execute_orders_module.submit_order = original_submit_order
    active_additional_order = active_additional_execution["orders"][0]
    active_additional_adjustment = (active_additional_execution.get("order_adjustments") or [{}])[0]
    if active_additional_order.get("result") != "submitted" or active_additional_order.get("reason") != "active_order_kept_and_additional_order_submitted":
        failures.append(f"same-direction active order did not submit only the additional quantity: {active_additional_order}")
    if active_additional_order.get("validated_order_quantity") != 2:
        failures.append(f"additional quantity gate did not reduce buy order: {active_additional_order}")
    if active_additional_order.get("requested_order_quantity") != 4:
        failures.append(f"additional order did not keep requested delta quantity: {active_additional_order}")
    if active_additional_order.get("kept_active_order_id") != "a1" or active_additional_order.get("order_or_reservation_id") != "new-buy":
        failures.append(f"kept active/new order ids were not recorded: {active_additional_order}")
    if not additional_submissions or additional_submissions[0].get("validated_order_quantity") != 2:
        failures.append(f"additional submission used wrong quantity: {additional_submissions}")
    if active_additional_adjustment.get("action") != "keep" or active_additional_adjustment.get("reason") != "same_direction_active_order_kept":
        failures.append(f"same-direction active order keep adjustment missing: {active_additional_adjustment}")
    if (active_additional_order.get("quantity_adjustment") or {}).get("reason") != "buy_quantity_reduced_to_order_available_quantity":
        failures.append(f"additional order reduction reason missing: {active_additional_order}")

    invalid_additional_execution = {
        "orders": [
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 3, "final_first_score": 6.5, "order_price": 0, "order_path": "immediate"}
        ]
    }
    invalid_additional_submissions: list[dict[str, Any]] = []

    def fake_invalid_submit_order(kis: Any, order: dict[str, Any]) -> str:
        invalid_additional_submissions.append(dict(order))
        return "should-not-submit"

    try:
        execute_orders_module.submit_order = fake_invalid_submit_order
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "000270", "symbol_name": "기아", "current_live_holding_quantity": 0}]},
            invalid_additional_execution,
            [
                {
                    "symbol_id": "000270",
                    "symbol_name": "기아",
                    "order_id": "a0",
                    "order_kind": "pending",
                    "direction": "buy",
                    "final_first_score": 6.5,
                    "remaining_quantity": 1,
                    "order_price": 100000,
                    "active_status": "active",
                    "order_api": "order_cash",
                    "order_path": "immediate",
                    "execution_environment": "real",
                    "observed_at": now_iso(),
                }
            ],
            {"000270": {"max_buy_qty": 2, "max_buy_amt": 1_000_000}},
            {},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.submit_order = original_submit_order
    invalid_additional_order = invalid_additional_execution["orders"][0]
    if invalid_additional_order.get("reason") != "invalid_order_quantity_or_price" or invalid_additional_order.get("result") != "blocked":
        failures.append(f"same-direction additional invalid price was not blocked: {invalid_additional_order}")
    if invalid_additional_submissions:
        failures.append(f"same-direction invalid price should not submit: {invalid_additional_submissions}")
    return failures


def step_active_order_replacement_checks(root: Path) -> list[str]:
    """An opposite-direction active order is cancelled and replaced with the new order."""
    failures: list[str] = []
    replacement_execution = {
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 5, "final_first_score": 3.5, "order_price": 70000, "order_path": "immediate"}
        ]
    }
    replacement_submissions: list[dict[str, Any]] = []
    original_adjust_active_order = execute_orders_module.adjust_active_order
    original_submit_order = execute_orders_module.submit_order

    def fake_cancel_active_order(kis: Any, active: dict[str, Any], desired: dict[str, Any] | None) -> tuple[str, str, str]:
        if desired is not None:
            failures.append(f"replacement path should cancel before submitting new order: {desired}")
        return "cancel1", "cancel", "fake cancel"

    def fake_submit_order(kis: Any, order: dict[str, Any]) -> str:
        replacement_submissions.append(dict(order))
        return "replace1"

    try:
        execute_orders_module.adjust_active_order = fake_cancel_active_order
        execute_orders_module.submit_order = fake_submit_order
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 10}]},
            replacement_execution,
            [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "order_id": "old-buy",
                    "order_kind": "pending",
                    "direction": "buy",
                    "final_first_score": 6.5,
                    "remaining_quantity": 1,
                    "order_price": 70000,
                    "active_status": "active",
                    "order_api": "order_cash",
                    "order_path": "immediate",
                    "execution_environment": "real",
                    "observed_at": now_iso(),
                }
            ],
            {},
            {"005930": {"max_sell_qty": 5}},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.adjust_active_order = original_adjust_active_order
        execute_orders_module.submit_order = original_submit_order
    replacement_order = replacement_execution["orders"][0]
    replacement_adjustment = (replacement_execution.get("order_adjustments") or [{}])[0]
    if replacement_order.get("result") != "submitted" or replacement_order.get("reason") != "active_order_cancel_and_replacement_submitted":
        failures.append(f"cancelled active order did not submit replacement: {replacement_order}")
    if replacement_order.get("cancel_request_id") != "cancel1" or replacement_order.get("order_or_reservation_id") != "replace1":
        failures.append(f"replacement ids were not recorded: {replacement_order}")
    if not replacement_submissions or replacement_submissions[0].get("direction") != "sell" or replacement_submissions[0].get("validated_order_quantity") != 5:
        failures.append(f"replacement sell order was not submitted with expected quantity: {replacement_submissions}")
    if replacement_adjustment.get("replacement_order_id") != "replace1":
        failures.append(f"replacement adjustment row did not record replacement order id: {replacement_adjustment}")
    return failures


def step_active_order_replacement_edge_case_checks(root: Path) -> list[str]:
    """Invalid price, empty order id, and submission exceptions during replacement all block safely."""
    failures: list[str] = []
    original_adjust_active_order = execute_orders_module.adjust_active_order
    original_submit_order = execute_orders_module.submit_order

    def fake_cancel_active_order(kis: Any, active: dict[str, Any], desired: dict[str, Any] | None) -> tuple[str, str, str]:
        if desired is not None:
            failures.append(f"replacement path should cancel before submitting new order: {desired}")
        return "cancel1", "cancel", "fake cancel"

    invalid_replacement_execution = {
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 5, "final_first_score": 3.5, "order_price": 0, "order_path": "immediate"}
        ]
    }
    invalid_replacement_adjustments: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
    invalid_replacement_submissions: list[dict[str, Any]] = []

    def fake_invalid_adjust_active_order(kis: Any, active: dict[str, Any], desired: dict[str, Any] | None) -> tuple[str, str, str]:
        invalid_replacement_adjustments.append(("adjust", dict(active), dict(desired) if desired else None))
        return "invalid-adjust", "cancel", "should not adjust"

    def fake_invalid_replacement_submit_order(kis: Any, order: dict[str, Any]) -> str:
        invalid_replacement_submissions.append(dict(order))
        return "invalid-submit"

    try:
        execute_orders_module.adjust_active_order = fake_invalid_adjust_active_order
        execute_orders_module.submit_order = fake_invalid_replacement_submit_order
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 10}]},
            invalid_replacement_execution,
            [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "order_id": "old-buy-zero-price",
                    "order_kind": "pending",
                    "direction": "buy",
                    "final_first_score": 6.5,
                    "remaining_quantity": 1,
                    "order_price": 70000,
                    "active_status": "active",
                    "order_api": "order_cash",
                    "order_path": "immediate",
                    "execution_environment": "real",
                    "observed_at": now_iso(),
                }
            ],
            {},
            {"005930": {"max_sell_qty": 5}},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.adjust_active_order = original_adjust_active_order
        execute_orders_module.submit_order = original_submit_order
    invalid_replacement_order = invalid_replacement_execution["orders"][0]
    if invalid_replacement_order.get("reason") != "invalid_order_quantity_or_price" or invalid_replacement_order.get("result") != "blocked":
        failures.append(f"opposite-direction replacement invalid price was not blocked: {invalid_replacement_order}")
    if invalid_replacement_adjustments or invalid_replacement_submissions:
        failures.append(f"invalid replacement should not adjust or submit: adjustments={invalid_replacement_adjustments}, submissions={invalid_replacement_submissions}")

    uncertain_replacement_execution = {
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 5, "final_first_score": 3.5, "order_price": 70000, "order_path": "immediate"}
        ]
    }

    def fake_empty_submit_order(kis: Any, order: dict[str, Any]) -> str:
        return ""

    try:
        execute_orders_module.adjust_active_order = fake_cancel_active_order
        execute_orders_module.submit_order = fake_empty_submit_order
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 10}]},
            uncertain_replacement_execution,
            [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "order_id": "old-buy-2",
                    "order_kind": "pending",
                    "direction": "buy",
                    "final_first_score": 6.5,
                    "remaining_quantity": 1,
                    "order_price": 70000,
                    "active_status": "active",
                    "order_api": "order_cash",
                    "order_path": "immediate",
                    "execution_environment": "real",
                    "observed_at": now_iso(),
                }
            ],
            {},
            {"005930": {"max_sell_qty": 5}},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.adjust_active_order = original_adjust_active_order
        execute_orders_module.submit_order = original_submit_order
    uncertain_replacement_order = uncertain_replacement_execution["orders"][0]
    if uncertain_replacement_order.get("result") != "blocked" or uncertain_replacement_order.get("reason") != "replacement_order_submission_uncertain":
        failures.append(f"empty replacement order id was not blocked as uncertain: {uncertain_replacement_order}")

    failed_replacement_execution = {
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 5, "final_first_score": 3.5, "order_price": 70000, "order_path": "immediate"}
        ]
    }

    def fake_failing_submit_order(kis: Any, order: dict[str, Any]) -> str:
        raise RuntimeError("fake replacement submit failure")

    try:
        execute_orders_module.adjust_active_order = fake_cancel_active_order
        execute_orders_module.submit_order = fake_failing_submit_order
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 10}]},
            failed_replacement_execution,
            [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "order_id": "old-buy-3",
                    "order_kind": "pending",
                    "direction": "buy",
                    "final_first_score": 6.5,
                    "remaining_quantity": 1,
                    "order_price": 70000,
                    "active_status": "active",
                    "order_api": "order_cash",
                    "order_path": "immediate",
                    "execution_environment": "real",
                    "observed_at": now_iso(),
                }
            ],
            {},
            {"005930": {"max_sell_qty": 5}},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.adjust_active_order = original_adjust_active_order
        execute_orders_module.submit_order = original_submit_order
    failed_replacement_order = failed_replacement_execution["orders"][0]
    if failed_replacement_order.get("result") != "blocked" or failed_replacement_order.get("reason") != "replacement_order_submission_failed":
        failures.append(f"replacement submit exception was not blocked: {failed_replacement_order}")
    return failures


def self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        previous_portfolio_except_env = os.environ.get(PORTFOLIO_EXCEPT_ENV_VAR)
        os.environ[PORTFOLIO_EXCEPT_ENV_VAR] = str(root / "portfolio-except.txt")
        failures: list[str] = []
        failures.extend(step_dry_run_gate_and_portfolio_except_checks(root))
        failures.extend(step_score_band_and_reservation_normalization_checks(root))
        failures.extend(step_reservation_cancel_request_checks(root))
        failures.extend(step_active_order_conflict_checks(root))
        failures.extend(step_reduction_and_available_cash_checks(root))
        failures.extend(step_max_buy_amt_and_zero_capacity_checks(root))
        failures.extend(step_active_sell_correction_checks(root))
        failures.extend(step_active_sell_partial_and_cancel_only_checks(root))
        failures.extend(step_active_sell_replacement_buy_checks(root))
        failures.extend(step_active_order_additional_and_invalid_price_checks(root))
        failures.extend(step_active_order_replacement_checks(root))
        failures.extend(step_active_order_replacement_edge_case_checks(root))
        if previous_portfolio_except_env is None:
            os.environ.pop(PORTFOLIO_EXCEPT_ENV_VAR, None)
        else:
            os.environ[PORTFOLIO_EXCEPT_ENV_VAR] = previous_portfolio_except_env
        if failures:
            print(json.dumps({"status": "failed", "failures": failures}, ensure_ascii=False, indent=2))
            return 1
    print(json.dumps({"status": "success"}, ensure_ascii=False))
    return 0



class RunSelfTestStepsAreIndividuallyDiscoverableTest(unittest.TestCase):
    """Real (non-mocked) execution of every self_test step_* helper, so each
    one is reachable from ordinary unittest discovery and not only from the
    mocked wrapper-orchestration test below. Steps have a genuine
    prerequisite order (later steps read artifacts an earlier step wrote to
    the shared root), so setUpClass runs them once in that order and each
    test method asserts on its own step's stored result -- unittest does not
    guarantee test methods run in declaration order, so the steps cannot be
    re-invoked independently per test method."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._temp_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._temp_dir.cleanup)
        cls.root = Path(cls._temp_dir.name)
        cls._old_portfolio_except_env = os.environ.get(PORTFOLIO_EXCEPT_ENV_VAR)
        cls.addClassCleanup(cls._restore_portfolio_except_env)
        os.environ[PORTFOLIO_EXCEPT_ENV_VAR] = str(cls.root / "portfolio-except.txt")
        cls.dry_run_gate_failures = step_dry_run_gate_and_portfolio_except_checks(cls.root)
        cls.score_band_failures = step_score_band_and_reservation_normalization_checks(cls.root)
        cls.active_order_conflict_failures = step_active_order_conflict_checks(cls.root)
        cls.reduction_and_available_cash_failures = step_reduction_and_available_cash_checks(cls.root)
        cls.max_buy_amt_and_zero_capacity_failures = step_max_buy_amt_and_zero_capacity_checks(cls.root)
        cls.active_sell_correction_failures = step_active_sell_correction_checks(cls.root)
        cls.active_sell_partial_and_cancel_only_failures = step_active_sell_partial_and_cancel_only_checks(cls.root)
        cls.active_sell_replacement_buy_failures = step_active_sell_replacement_buy_checks(cls.root)
        cls.active_order_additional_and_invalid_price_failures = step_active_order_additional_and_invalid_price_checks(cls.root)
        cls.active_order_replacement_failures = step_active_order_replacement_checks(cls.root)
        cls.active_order_replacement_edge_case_failures = step_active_order_replacement_edge_case_checks(cls.root)

    @classmethod
    def _restore_portfolio_except_env(cls) -> None:
        if cls._old_portfolio_except_env is None:
            os.environ.pop(PORTFOLIO_EXCEPT_ENV_VAR, None)
        else:
            os.environ[PORTFOLIO_EXCEPT_ENV_VAR] = cls._old_portfolio_except_env

    def test_step_dry_run_gate_and_portfolio_except_checks(self) -> None:
        self.assertEqual(self.dry_run_gate_failures, [])

    def test_step_score_band_and_reservation_normalization_checks(self) -> None:
        self.assertEqual(self.score_band_failures, [])

    def test_step_active_order_conflict_checks(self) -> None:
        self.assertEqual(self.active_order_conflict_failures, [])

    def test_step_reduction_and_available_cash_checks(self) -> None:
        self.assertEqual(self.reduction_and_available_cash_failures, [])

    def test_step_max_buy_amt_and_zero_capacity_checks(self) -> None:
        self.assertEqual(self.max_buy_amt_and_zero_capacity_failures, [])

    def test_step_active_sell_correction_checks(self) -> None:
        self.assertEqual(self.active_sell_correction_failures, [])

    def test_step_active_sell_partial_and_cancel_only_checks(self) -> None:
        self.assertEqual(self.active_sell_partial_and_cancel_only_failures, [])

    def test_step_active_sell_replacement_buy_checks(self) -> None:
        self.assertEqual(self.active_sell_replacement_buy_failures, [])

    def test_step_active_order_additional_and_invalid_price_checks(self) -> None:
        self.assertEqual(self.active_order_additional_and_invalid_price_failures, [])

    def test_step_active_order_replacement_checks(self) -> None:
        self.assertEqual(self.active_order_replacement_failures, [])

    def test_step_active_order_replacement_edge_case_checks(self) -> None:
        self.assertEqual(self.active_order_replacement_edge_case_failures, [])


class ExecuteOrdersSelfTest(unittest.TestCase):
    def test_self_test_suite_runs_every_step_and_reports_success(self) -> None:
        """Wrapper-orchestration check only: each step's real behavior is
        covered by the granular tests below and by calling the step_*
        functions directly, so this mocks every step instead of re-running
        the whole scenario a second time."""
        step_names = [
            "step_dry_run_gate_and_portfolio_except_checks",
            "step_score_band_and_reservation_normalization_checks",
            "step_reservation_cancel_request_checks",
            "step_active_order_conflict_checks",
            "step_reduction_and_available_cash_checks",
            "step_max_buy_amt_and_zero_capacity_checks",
            "step_active_sell_correction_checks",
            "step_active_sell_partial_and_cancel_only_checks",
            "step_active_sell_replacement_buy_checks",
            "step_active_order_additional_and_invalid_price_checks",
            "step_active_order_replacement_checks",
            "step_active_order_replacement_edge_case_checks",
        ]
        patchers = [patch(f"{__name__}.{name}", return_value=[]) for name in step_names]
        mocks = [patcher.start() for patcher in patchers]
        self.addCleanup(lambda: [patcher.stop() for patcher in patchers])

        result = self_test()

        self.assertEqual(result, 0)
        for mock in mocks:
            mock.assert_called_once()

    def test_self_test_suite_reports_failure_when_a_step_fails(self) -> None:
        with patch(f"{__name__}.step_dry_run_gate_and_portfolio_except_checks", return_value=["boom"]), patch(
            f"{__name__}.step_score_band_and_reservation_normalization_checks", return_value=[]
        ), patch(f"{__name__}.step_reservation_cancel_request_checks", return_value=[]), patch(
            f"{__name__}.step_active_order_conflict_checks", return_value=[]
        ), patch(f"{__name__}.step_reduction_and_available_cash_checks", return_value=[]), patch(
            f"{__name__}.step_max_buy_amt_and_zero_capacity_checks", return_value=[]
        ), patch(f"{__name__}.step_active_sell_correction_checks", return_value=[]), patch(
            f"{__name__}.step_active_sell_partial_and_cancel_only_checks", return_value=[]
        ), patch(f"{__name__}.step_active_sell_replacement_buy_checks", return_value=[]), patch(
            f"{__name__}.step_active_order_additional_and_invalid_price_checks", return_value=[]
        ), patch(f"{__name__}.step_active_order_replacement_checks", return_value=[]), patch(
            f"{__name__}.step_active_order_replacement_edge_case_checks", return_value=[]
        ):
            result = self_test()

        self.assertEqual(result, 1)

    def test_step_reservation_cancel_request_checks_in_isolation(self) -> None:
        """A representative direct call into one of self_test's extracted
        step_* functions, proving each step is independently callable
        against its own temp workspace rather than only reachable via the
        full umbrella."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failures = step_reservation_cancel_request_checks(root)

        self.assertEqual(failures, [])

    def test_gate_lookup_failure_is_recorded_separately_from_account_collection(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(
                root / "account-before-order.json",
                {
                    "schema_version": "1",
                    "status": "success",
                    "execution_environment": "real",
                    "order_gate_status": "not_run",
                    "account_summary": {},
                    "warnings": [],
                    "symbols": [],
                },
            )
            write_json(
                root / "execution.json",
                {
                    "schema_version": "1",
                    "request_type": "real-submit",
                    "status": "success",
                    "requires_main_agent_order_execution": True,
                    "required_main_agent_actions": ["continue_order_execution"],
                    "errors": [],
                    "orders": [],
                },
            )
            gate_error = {"code": "order_available_lookup_failed", "message": "masked"}
            with patch.object(execute_orders_module, "refresh_gates", return_value=([], {}, {}, [gate_error], None)):
                execution = execute(
                    argparse.Namespace(
                        output_dir=str(root),
                        execution_json="",
                        account_before_order="",
                        env="real",
                        submit=False,
                        offline=False,
                        retries=0,
                        reservation_start_date="",
                        reservation_end_date="",
                    )
                )

            account = load_json(root / "account-before-order.json")
            self.assertEqual(account["status"], "success")
            self.assertEqual(account["order_gate_status"], "failed")
            self.assertEqual(execution["status"], "failed")

    def test_broker_reconciliation_distinguishes_fill_and_rejection(self) -> None:
        class FakeBrokerKis:
            cano = "12345678"
            product = "01"

            def call(self, name: str, *, params: dict[str, str] | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
                self.last_name = name
                self.last_params = params
                return {
                    "output1": [
                        {
                            "odno": "fill-1",
                            "ord_qty": "1",
                            "tot_ccld_qty": "1",
                            "rjct_qty": "0",
                            "rmn_qty": "0",
                            "avg_prvs": "70100",
                        },
                        {
                            "odno": "reject-1",
                            "ord_qty": "1",
                            "tot_ccld_qty": "0",
                            "rjct_qty": "1",
                            "rmn_qty": "0",
                        },
                        {
                            "odno": "partial-reject-1",
                            "ord_qty": "2",
                            "tot_ccld_qty": "1",
                            "rjct_qty": "1",
                            "rmn_qty": "0",
                            "avg_prvs": "70200",
                        },
                    ]
                }

        execution = {
            "started_at": "2026-07-15T14:30:00+09:00",
            "status": "success",
            "errors": [],
            "orders": [
                {
                    "symbol_id": "005930",
                    "direction": "buy",
                    "result": "submitted",
                    "order_path": "immediate",
                    "validated_order_quantity": 1,
                    "order_or_reservation_id": "fill-1",
                },
                {
                    "symbol_id": "042660",
                    "direction": "buy",
                    "result": "submitted",
                    "order_path": "immediate",
                    "validated_order_quantity": 1,
                    "order_or_reservation_id": "reject-1",
                },
                {
                    "symbol_id": "000660",
                    "direction": "buy",
                    "result": "submitted",
                    "order_path": "immediate",
                    "validated_order_quantity": 2,
                    "order_or_reservation_id": "partial-reject-1",
                },
            ],
        }

        summary = reconcile_submitted_cash_orders(FakeBrokerKis(), execution, poll_delays=(0.0,))

        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["filled_order_count"], 1)
        self.assertEqual(summary["rejected_order_count"], 2)
        self.assertEqual(summary["partially_filled_order_count"], 0)
        counted_orders = sum(
            summary[key]
            for key in (
                "filled_order_count",
                "partially_filled_order_count",
                "pending_order_count",
                "rejected_order_count",
                "canceled_order_count",
                "unconfirmed_order_count",
            )
        )
        self.assertEqual(counted_orders, summary["submitted_cash_order_count"])
        self.assertEqual(execution["status"], "partial")
        self.assertEqual(execution["orders"][0]["broker_reconciliation"]["status"], "filled")
        self.assertEqual(execution["orders"][1]["broker_reconciliation"]["status"], "rejected")
        self.assertEqual(execution["orders"][1]["broker_reconciliation"]["rejected_quantity"], 1)
        self.assertEqual(execution["orders"][2]["broker_reconciliation"]["status"], "partially_filled_rejected")
        self.assertIn("broker_order_rejected", {item.get("code") for item in execution["errors"]})

    def test_broker_reconciliation_falls_back_to_order_id_lookup(self) -> None:
        self.assertEqual(ENDPOINTS["inquire_daily_ccld"][2:], ("TTTC0081R", "VTTC0081R"))

        class FakeBrokerKis:
            cano = "12345678"
            product = "01"

            def __init__(self) -> None:
                self.requested_order_ids: list[str] = []

            def call(self, name: str, *, params: dict[str, str] | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
                order_id = str((params or {}).get("ODNO") or "")
                self.requested_order_ids.append(order_id)
                if not order_id:
                    return {"output1": []}
                return {
                    "output1": [
                        {
                            "odno": order_id,
                            "ord_qty": "1",
                            "tot_ccld_qty": "0",
                            "rjct_qty": "1",
                            "rmn_qty": "0",
                        }
                    ]
                }

        kis = FakeBrokerKis()
        execution = {
            "started_at": "2026-07-15T14:30:00+09:00",
            "status": "success",
            "errors": [],
            "orders": [
                {
                    "symbol_id": "042660",
                    "direction": "buy",
                    "result": "submitted",
                    "order_path": "immediate",
                    "validated_order_quantity": 1,
                    "order_or_reservation_id": "late-page-1",
                }
            ],
        }

        summary = reconcile_submitted_cash_orders(kis, execution, poll_delays=(0.0,))

        self.assertEqual(kis.requested_order_ids, ["", "late-page-1"])
        self.assertEqual(summary["rejected_order_count"], 1)
        self.assertEqual(execution["orders"][0]["broker_reconciliation"]["status"], "rejected")

    def test_broker_reconciliation_keeps_pending_order_partial(self) -> None:
        pending = normalize_broker_reconciliation(
            {"ord_qty": "2", "tot_ccld_qty": "0", "rjct_qty": "0", "rmn_qty": "2"},
            requested_quantity=2,
            observed_at="2026-07-15T06:00:00+00:00",
        )

        self.assertEqual(pending["status"], "pending")
        self.assertFalse(pending["terminal"])
        self.assertEqual(pending["remaining_quantity"], 2)

    def test_broker_reconciliation_reads_kis_cancel_confirm_quantity(self) -> None:
        canceled = normalize_broker_reconciliation(
            {
                "ord_qty": "2",
                "tot_ccld_qty": "1",
                "cncl_cfrm_qty": "1",
                "rmn_qty": "0",
            },
            requested_quantity=2,
            observed_at="2026-07-15T06:00:00+00:00",
        )

        self.assertEqual(canceled["status"], "partially_filled_canceled")
        self.assertEqual(canceled["canceled_quantity"], 1)

    def test_preflight_carries_pending_and_blocks_lagging_filled_holding(self) -> None:
        import tempfile

        class FakeLifecycleKis:
            env = "real"
            cano = "12345678"
            product = "01"

            def call(
                self,
                name: str,
                *,
                params: dict[str, str] | None = None,
                payload: dict[str, Any] | None = None,
            ) -> dict[str, Any]:
                if name == "order_resv_ccnl":
                    return {"output1": []}
                if name == "inquire_psbl_rvsecncl":
                    return {
                        "output1": [
                            {
                                "pdno": "005930",
                                "prdt_name": "삼성전자",
                                "odno": "pending-1",
                                "ord_uncc_qty": "1",
                                "ord_unpr": "70000",
                                "sll_buy_dvsn_cd": "02",
                                "ord_dvsn": "00",
                            }
                        ]
                    }
                if name == "inquire_daily_ccld":
                    order_id = str((params or {}).get("ODNO") or "")
                    rows = [
                        {
                            "pdno": "005930",
                            "odno": "pending-1",
                            "ord_qty": "1",
                            "tot_ccld_qty": "0",
                            "rmn_qty": "1",
                        },
                        {
                            "pdno": "042660",
                            "odno": "filled-1",
                            "ord_qty": "1",
                            "tot_ccld_qty": "1",
                            "rmn_qty": "0",
                        },
                    ]
                    return {"output1": [row for row in rows if not order_id or row["odno"] == order_id]}
                raise AssertionError(name)

        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            previous_dir = runs_dir / "previous"
            current_dir = runs_dir / "current"
            previous_dir.mkdir(parents=True)
            current_dir.mkdir(parents=True)
            write_json(
                previous_dir / "execution.json",
                {
                    "run_id": "previous",
                    "started_at": "2026-07-15T12:00:00+09:00",
                    "orders": [
                        {
                            "symbol_id": "005930",
                            "symbol_name": "삼성전자",
                            "direction": "buy",
                            "result": "submitted",
                            "order_path": "immediate",
                            "validated_order_quantity": 1,
                            "order_or_reservation_id": "pending-1",
                        },
                        {
                            "symbol_id": "042660",
                            "symbol_name": "한화오션",
                            "direction": "buy",
                            "result": "submitted",
                            "order_path": "immediate",
                            "validated_order_quantity": 1,
                            "order_or_reservation_id": "filled-1",
                        },
                    ],
                },
            )
            write_json(
                current_dir / "account-before-order.json",
                {
                    "run_id": "current",
                    "started_at": "2026-07-15T12:30:00+09:00",
                    "execution_environment": "real",
                    "warnings": ["active_order_lookup_not_performed"],
                    "symbols": [
                        {
                            "symbol_id": "005930",
                            "symbol_name": "삼성전자",
                            "current_live_holding_quantity": 10,
                            "today_buy_quantity": 0,
                            "today_sell_quantity": 0,
                        },
                        {
                            "symbol_id": "042660",
                            "symbol_name": "한화오션",
                            "current_live_holding_quantity": 0,
                            "today_buy_quantity": 0,
                            "today_sell_quantity": 0,
                        },
                    ],
                },
            )
            lifecycle = order_lifecycle_preflight(
                argparse.Namespace(
                    output_dir=str(current_dir),
                    account_before_order="",
                    output="",
                    env="real",
                    retries=0,
                    reservation_start_date="",
                    reservation_end_date="",
                ),
                kis=FakeLifecycleKis(),
            )

            account = load_json(current_dir / "account-before-order.json")
            by_symbol = {item["symbol_id"]: item for item in account["symbols"]}
            statuses = {
                item["order_id"]: item["broker_reconciliation"]["status"]
                for item in lifecycle["previous_submitted_cash_orders"]
            }
            self.assertEqual(lifecycle["status"], "partial")
            self.assertEqual(lifecycle["active_order_count"], 1)
            self.assertEqual(statuses, {"pending-1": "pending", "filled-1": "filled"})
            self.assertEqual(by_symbol["005930"]["pending_and_reserved_buy_quantity"], 1)
            self.assertEqual(by_symbol["005930"]["holding_state_status"], "consistent")
            self.assertEqual(by_symbol["042660"]["holding_state_status"], "inconsistent")

    def test_unverified_holding_still_allows_cancel_only_reconciliation(self) -> None:
        execution = {
            "orders": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "final_holding_quantity": 10,
                    "order_price": 70000,
                    "order_path": "immediate",
                    "reconciliation_only": True,
                    "active_cancel_only": True,
                    "holding_state_status": "unconfirmed",
                }
            ]
        }
        active = [
            {
                "symbol_id": "005930",
                "symbol_name": "삼성전자",
                "order_id": "active-1",
                "order_kind": "pending",
                "direction": "buy",
                "remaining_quantity": 1,
                "order_price": 70000,
                "active_status": "active",
                "order_api": "order_cash",
                "order_path": "immediate",
                "execution_environment": "real",
                "observed_at": now_iso(),
            }
        ]

        reconcile(
            {
                "account_summary": {"cash_amount": 1_000_000},
                "symbols": [
                    {
                        "symbol_id": "005930",
                        "current_live_holding_quantity": 10,
                        "holding_state_status": "unconfirmed",
                    }
                ],
            },
            execution,
            active,
            {},
            {},
            submit=False,
            kis=None,
        )

        self.assertEqual(execution["orders"][0]["reason"], "active_order_adjustment_required")

    def test_normalize_reservation_preserves_reservation_id_and_resulting_odno(self) -> None:
        row = {
            "rsvn_ord_seq": "103586",
            "odno": "0001452900",
            "pdno": "021240",
            "sll_buy_dvsn_cd": "02",
            "ord_qty": "1",
            "tot_ccld_qty": "1",
            "ord_unpr": "95700",
        }

        item = normalize_reservation(row)

        self.assertEqual(item["order_id"], "103586")
        self.assertEqual(item["rsvn_ord_seq"], "103586")
        self.assertEqual(item["odno"], "0001452900")

    def test_normalize_reservation_falls_back_to_odno_when_no_reservation_id(self) -> None:
        row = {"odno": "0001452900", "pdno": "021240", "ord_qty": "1"}

        item = normalize_reservation(row)

        self.assertEqual(item["order_id"], "0001452900")
        self.assertEqual(item["rsvn_ord_seq"], "")
        self.assertEqual(item["odno"], "0001452900")

    def test_fetch_reservations_pages_through_all_results_preserving_continuation(self) -> None:
        class FakePaginatedKis:
            env = "real"
            cano = "12345678"
            product = "01"

            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def call_with_headers(
                self,
                name: str,
                *,
                params: dict[str, str] | None = None,
                payload: dict[str, Any] | None = None,
                tr_cont: str = "",
            ) -> tuple[dict[str, Any], dict[str, str]]:
                self.calls.append({"params": dict(params or {}), "tr_cont": tr_cont})
                if len(self.calls) == 1:
                    return (
                        {
                            "output1": [{"rsvn_ord_seq": "103586", "pdno": "021240", "ord_qty": "1"}],
                            "ctx_area_fk200": "FK1",
                            "ctx_area_nk200": "NK1",
                        },
                        {"tr_cont": "F"},
                    )
                return (
                    {
                        "output1": [{"rsvn_ord_seq": "999999", "pdno": "005930", "ord_qty": "1"}],
                        "ctx_area_fk200": "",
                        "ctx_area_nk200": "",
                    },
                    {"tr_cont": "D"},
                )

        kis = FakePaginatedKis()

        results = fetch_reservations(kis, "20260716", "20260716")

        self.assertEqual(len(kis.calls), 2)
        self.assertEqual(kis.calls[0]["tr_cont"], "")
        self.assertEqual(kis.calls[0]["params"]["CTX_AREA_FK200"], "")
        self.assertEqual(kis.calls[1]["tr_cont"], "N")
        self.assertEqual(kis.calls[1]["params"]["CTX_AREA_FK200"], "FK1")
        self.assertEqual(kis.calls[1]["params"]["CTX_AREA_NK200"], "NK1")
        self.assertEqual({item["rsvn_ord_seq"] for item in results}, {"103586", "999999"})

    def test_fetch_reservations_pagination_is_bounded(self) -> None:
        class FakeInfinitePaginationKis:
            env = "real"
            cano = "12345678"
            product = "01"

            def __init__(self) -> None:
                self.calls = 0

            def call_with_headers(
                self,
                name: str,
                *,
                params: dict[str, str] | None = None,
                payload: dict[str, Any] | None = None,
                tr_cont: str = "",
            ) -> tuple[dict[str, Any], dict[str, str]]:
                self.calls += 1
                return (
                    {
                        "output1": [{"rsvn_ord_seq": f"seq-{self.calls}", "pdno": "021240"}],
                        "ctx_area_fk200": "FK",
                        "ctx_area_nk200": "NK",
                    },
                    {"tr_cont": "F"},
                )

        kis = FakeInfinitePaginationKis()

        with self.assertRaises(RuntimeError):
            fetch_reservations(kis, "20260716", "20260716", max_pages=3)

        self.assertEqual(kis.calls, 3)

    def test_fetch_reservations_falls_back_to_single_page_without_call_with_headers(self) -> None:
        class FakeSinglePageKis:
            env = "real"
            cano = "12345678"
            product = "01"

            def __init__(self) -> None:
                self.calls = 0

            def call(self, name: str, *, params: dict[str, str] | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
                self.calls += 1
                return {"output1": [{"rsvn_ord_seq": "103586", "pdno": "021240"}]}

        kis = FakeSinglePageKis()

        results = fetch_reservations(kis, "20260716", "20260716")

        self.assertEqual(kis.calls, 1)
        self.assertEqual(len(results), 1)
