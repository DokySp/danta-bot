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
    active_buy_correction_gate,
    adjust_reservation,
    as_int,
    decision_guard_block_reason,
    default_reservation_orgno,
    execute,
    fetch_fresh_domestic_balance,
    fetch_reservations,
    load_json,
    normalize_limit_price,
    normalize_broker_reconciliation,
    normalize_execution_order_prices,
    normalize_reservation,
    now_iso,
    order_lifecycle_preflight,
    reconcile,
    refresh_gates,
    reconcile_submitted_cash_orders,
    verify_concentration_rebalance,
    verify_fresh_reduction_bounds,
    verify_profit_protection_pnl,
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
            {"symbol_id": "035420", "symbol_name": "NAVER", "current_live_holding_quantity": 5, "current_price": 200000},
        ],
    }
    execution = {
        "schema_version": "1",
        "request_type": "real-submit",
        "requires_main_agent_order_execution": True,
        "required_main_agent_actions": ["refresh_active_order_lookup", "refresh_order_available_lookup", "continue_order_execution"],
        "errors": [{"code": "order_submission_blocked"}],
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 6, "order_price": 70000, "direction": "sell", "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}, "result": "blocked"},
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 20, "order_price": 100000, "direction": "buy", "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "result": "blocked"},
            {"symbol_id": "035420", "symbol_name": "NAVER", "final_holding_quantity": 4, "order_price": 200000, "direction": "sell", "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}, "result": "blocked"},
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
    if orders["035420"].get("reason") != "validated_dry_run_not_submitted":
        failures.append(f"offline dry-run sell was incorrectly required to have live broker capacity: {orders['035420']}")
    account_after = load_json(root / "account-before-order.json")
    if account_after.get("active_order_lookup_performed") is not True or account_after.get("order_available_lookup_performed") is not True:
        failures.append("account gates not refreshed")
    if account_after.get("order_gate_status") != "success":
        failures.append(f"successful gate refresh was not recorded: {account_after.get('order_gate_status')}")
    write_json(root / "execution.json", {**execution, "orders": [{"symbol_id": "005930", "symbol_name": "삼성전자", "order_price": 70000, "direction": "sell", "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}, "result": "blocked"}]})
    invalid_final_payload = execute(argparse.Namespace(output_dir=str(root), execution_json="", account_before_order="", env="real", submit=False, offline=True, retries=0, reservation_start_date="", reservation_end_date=""))
    invalid_final_order = invalid_final_payload["orders"][0]
    if invalid_final_order.get("reason") != "invalid_final_holding_quantity":
        failures.append(f"missing final_holding_quantity was not blocked as invalid: {invalid_final_order}")
    if invalid_final_order.get("validated_order_quantity") != 0 or invalid_final_order.get("additional_required_quantity") != 0:
        failures.append(f"missing final_holding_quantity was converted into an order quantity: {invalid_final_order}")
    (root / "portfolio-except.txt").write_text("# excluded\n000270, 005930\n", encoding="utf-8")
    portfolio_except_execution = {
        "orders": [
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 20, "order_price": 100000, "direction": "buy", "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}},
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 6, "order_price": 70000, "direction": "sell", "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}},
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


def step_decision_guard_and_reservation_normalization_checks(root: Path) -> list[str]:
    """decision_guard buy/sell gates, cancel-exempt active orders, reservation normalization, and KRX tick rounding."""
    failures: list[str] = []
    execution = {
        "schema_version": "1",
        "request_type": "real-submit",
        "requires_main_agent_order_execution": True,
        "required_main_agent_actions": ["refresh_active_order_lookup", "refresh_order_available_lookup", "continue_order_execution"],
        "errors": [{"code": "order_submission_blocked"}],
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 6, "order_price": 70000, "direction": "sell", "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}, "result": "blocked"},
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 20, "order_price": 100000, "direction": "buy", "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "result": "blocked"},
        ],
    }
    decision_guard_execution = {
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 6, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}, "order_price": 70000, "order_path": "immediate"},
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 20, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 100000, "order_path": "immediate"},
            {"symbol_id": "000810", "symbol_name": "삼성화재", "final_holding_quantity": 0, "order_price": 400000, "order_path": "immediate"},
        ]
    }
    reconcile(
        {"account_summary": {"cash_amount": 10_000_000}, "symbols": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 8, "current_price": 70000},
            {"symbol_id": "000270", "symbol_name": "기아", "current_live_holding_quantity": 18, "current_price": 100000},
            {"symbol_id": "000810", "symbol_name": "삼성화재", "current_live_holding_quantity": 1, "current_price": 400000},
        ]},
        decision_guard_execution,
        [],
        {"000270": {"max_buy_qty": 5, "max_buy_amt": 1_000_000}},
        {"005930": {"max_sell_qty": 5}, "000810": {"max_sell_qty": 5}},
        submit=False,
        kis=None,
    )
    decision_guard_orders = {item["symbol_id"]: item for item in decision_guard_execution["orders"]}
    if decision_guard_orders["005930"].get("reason") != "validated_dry_run_not_submitted":
        failures.append(f"sell with an allowed decision_guard was not allowed: {decision_guard_orders['005930']}")
    if decision_guard_orders["000270"].get("reason") != "validated_dry_run_not_submitted":
        failures.append(f"buy with an allowed decision_guard was not allowed: {decision_guard_orders['000270']}")
    if decision_guard_orders["000810"].get("result") != "blocked" or decision_guard_orders["000810"].get("reason") != "decision_guard_not_allowed":
        failures.append(f"missing decision_guard did not fail safe: {decision_guard_orders['000810']}")
    decision_guard_cancel_execution = {
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 8, "order_price": 70000, "order_path": "reservation"},
        ]
    }
    reconcile(
        {"account_summary": {"cash_amount": 10_000_000}, "symbols": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 8, "current_price": 70000},
        ]},
        decision_guard_cancel_execution,
        [{"symbol_id": "005930", "symbol_name": "삼성전자", "order_id": "r9", "order_kind": "reservation", "direction": "sell", "remaining_quantity": 2, "order_price": 70000, "active_status": "active", "order_api": "order_resv", "order_path": "reservation", "execution_environment": "real", "observed_at": now_iso()}],
        {},
        {"005930": {"max_sell_qty": 5}},
        submit=True,
        kis=None,
    )
    cancel_order = decision_guard_cancel_execution["orders"][0]
    if cancel_order.get("reason") == "decision_guard_not_allowed":
        failures.append(f"cancel of an active order must be exempt from the decision-guard gate: {cancel_order}")
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
            "orders": [{"symbol_id": "000660", "symbol_name": "SK하이닉스", "final_holding_quantity": 2, "order_price": 150000, "direction": "buy", "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "result": "blocked"}],
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
            "orders": [{"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 6, "order_price": 70000, "direction": "sell", "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}, "result": "blocked"}],
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
            "orders": [{"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 20, "order_price": 100000, "order_path": "immediate", "direction": "buy", "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "result": "blocked"}],
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
            "orders": [{"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 20, "order_price": 100000, "order_path": "immediate", "direction": "buy", "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "result": "blocked"}],
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
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 12, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 100000, "order_path": "immediate"},
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 4, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}, "order_price": 70000, "order_path": "immediate"},
            {"symbol_id": "000810", "symbol_name": "삼성화재", "final_holding_quantity": 3, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 400000, "order_path": "immediate"},
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
            {"symbol_id": "000660", "symbol_name": "SK하이닉스", "final_holding_quantity": 1, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 2_955_000, "order_path": "immediate"}
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
            {"symbol_id": "000660", "symbol_name": "SK하이닉스", "final_holding_quantity": 1, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 2_000_000, "order_path": "immediate"},
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 10, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 100_000, "order_path": "immediate"},
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
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 3, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 100_000, "order_path": "immediate"}
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
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 1, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 100_000, "order_path": "immediate"},
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 1, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 70_000, "order_path": "immediate"},
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
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 1, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 100_000, "order_path": "immediate"}
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
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 2, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 100000, "order_path": "immediate"},
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 0, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "exit", "basis": "thesis"}, "order_price": 70000, "order_path": "immediate"},
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
        "decision_guard": {"status": "allowed"}, "remaining_quantity": 1,
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
            {"symbol_id": "402340", "symbol_name": "SK스퀘어", "final_holding_quantity": 0, "decision_guard": {"status": "allowed"}, "order_price": 1_595_000, "order_path": "immediate"}
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
        "decision_guard": {"status": "allowed"}, "remaining_quantity": 1,
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
            {"symbol_id": "402340", "symbol_name": "SK스퀘어", "final_holding_quantity": 1, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}, "order_price": 1_630_000, "order_path": "immediate"}
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
            {"symbol_id": "402340", "symbol_name": "SK스퀘어", "final_holding_quantity": 1, "decision_guard": {"status": "allowed"}, "order_price": 1_595_000, "order_path": "immediate"}
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
    """A covered active sell that flips direction has its cancellation submitted, but the
    replacement buy is deferred to a later run -- a cancel request id is only broker acceptance,
    not confirmed release, so submitting a replacement in the same run would risk double
    exposure."""
    failures: list[str] = []
    active_sell_base = {
        "symbol_id": "402340",
        "symbol_name": "SK스퀘어",
        "order_id": "old-sell",
        "order_kind": "pending",
        "direction": "sell",
        "decision_guard": {"status": "allowed"}, "remaining_quantity": 1,
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
            {"symbol_id": "402340", "symbol_name": "SK스퀘어", "final_holding_quantity": 2, "decision_guard": {"status": "allowed"}, "order_price": 1_595_000, "order_path": "immediate"}
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
    active_sell_replacement_buy_adjustment = (active_sell_replacement_buy_execution.get("order_adjustments") or [{}])[0]
    if active_sell_replacement_buy_order.get("result") != "submitted" or active_sell_replacement_buy_order.get("reason") != "active_order_cancel_submitted":
        failures.append(f"active sell replacement-buy case did not defer the replacement: {active_sell_replacement_buy_order}")
    if active_sell_replacement_buy_order.get("direction") != "none" or active_sell_replacement_buy_order.get("validated_order_quantity") != 0:
        failures.append(f"active sell replacement-buy should not claim a buy was submitted: {active_sell_replacement_buy_order}")
    if active_sell_replacement_buy_order.get("order_or_reservation_id") != "cancel-before-buy":
        failures.append(f"active sell replacement-buy should record the cancel request id, not a replacement id: {active_sell_replacement_buy_order}")
    if len(replacement_buy_adjustments) != 1 or replacement_buy_adjustments[0][1] is not None:
        failures.append(f"active sell replacement-buy should cancel exactly once with no desired order: {replacement_buy_adjustments}")
    if replacement_buy_submissions:
        failures.append(f"active sell replacement-buy should not submit a replacement in the same run: {replacement_buy_submissions}")
    if active_sell_replacement_buy_adjustment.get("replacement_required") is not True:
        failures.append(f"active sell replacement-buy should still flag replacement as required for a later run: {active_sell_replacement_buy_adjustment}")
    if active_sell_replacement_buy_adjustment.get("replacement_order_id"):
        failures.append(f"active sell replacement-buy should not record a replacement order id: {active_sell_replacement_buy_adjustment}")
    return failures


def step_active_order_additional_and_invalid_price_checks(root: Path) -> list[str]:
    """A same-direction active order is kept and only the additional delta is submitted; invalid price/quantity blocks it."""
    failures: list[str] = []
    active_additional_execution = {
        "orders": [
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 5, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 100000, "order_path": "immediate"}
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
                    "decision_guard": {"status": "allowed"}, "remaining_quantity": 1,
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
            {"symbol_id": "000270", "symbol_name": "기아", "final_holding_quantity": 3, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 0, "order_path": "immediate"}
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
                    "decision_guard": {"status": "allowed"}, "remaining_quantity": 1,
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


def _run_active_buy_correction_case(
    *,
    active_qty: int,
    active_price: int,
    final_holding_quantity: int,
    current_holding: int,
    order_price: int,
    capacities: dict[str, dict[str, int]],
) -> tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any] | None]], list[dict[str, Any]]]:
    active_buy_base = {
        "symbol_id": "078930",
        "symbol_name": "GS",
        "order_id": "0015300400",
        "order_kind": "pending",
        "direction": "buy",
        "decision_guard": {"status": "allowed"}, "remaining_quantity": active_qty,
        "order_price": active_price,
        "active_status": "active",
        "order_api": "order_cash",
        "order_path": "immediate",
        "execution_environment": "real",
        "observed_at": now_iso(),
        "krx_fwdg_ord_orgno": "91252",
        "orgn_odno": "0015300400",
    }
    execution = {
        "orders": [
            {
                "symbol_id": "078930",
                "symbol_name": "GS",
                "final_holding_quantity": final_holding_quantity,
                "decision_guard": {"status": "allowed"}, "order_price": order_price,
                "order_path": "immediate",
            }
        ]
    }
    original_adjust_active_order = execute_orders_module.adjust_active_order
    original_submit_order = execute_orders_module.submit_order
    correction_calls: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    submit_calls: list[dict[str, Any]] = []

    def fake_adjust_active_order(kis: Any, active: dict[str, Any], desired: dict[str, Any] | None) -> tuple[str, str, str]:
        correction_calls.append((dict(active), dict(desired) if desired else None))
        return "correct-buy-078930", "correct", "fake active buy correction"

    def fake_submit(kis: Any, order: dict[str, Any]) -> str:
        submit_calls.append(dict(order))
        return "unexpected-submit"

    try:
        execute_orders_module.adjust_active_order = fake_adjust_active_order
        execute_orders_module.submit_order = fake_submit
        reconcile(
            {
                "account_summary": {"cash_amount": 10_000_000},
                "symbols": [{"symbol_id": "078930", "symbol_name": "GS", "current_live_holding_quantity": current_holding}],
            },
            execution,
            [dict(active_buy_base)],
            capacities,
            {},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.adjust_active_order = original_adjust_active_order
        execute_orders_module.submit_order = original_submit_order
    return execution["orders"][0], correction_calls, submit_calls


def step_active_buy_correction_checks(root: Path) -> list[str]:
    """A covered active buy is corrected in place instead of being pushed through the gross new-buy gate."""
    failures: list[str] = []

    # GS replay: current holding 11, active pending buy 1 @ 82,300, final target 12 @ 83,200 --
    # same quantity, price increase. Only the incremental notional (900) should be validated.
    gs_replay_order, gs_replay_calls, gs_replay_submits = _run_active_buy_correction_case(
        active_qty=1,
        active_price=82_300,
        final_holding_quantity=12,
        current_holding=11,
        order_price=83_200,
        capacities={"078930": {"max_buy_qty": 5, "max_buy_amt": 5_000_000}},
    )
    if gs_replay_order.get("result") != "submitted" or gs_replay_order.get("reason") != "active_order_correction_submitted":
        failures.append(f"GS replay same-quantity price increase was not corrected in place: {gs_replay_order}")
    if gs_replay_order.get("validated_order_quantity") != 1 or gs_replay_order.get("additional_required_quantity") != 1:
        failures.append(f"GS replay correction used wrong quantity: {gs_replay_order}")
    if gs_replay_submits:
        failures.append(f"GS replay correction should not call submit_order: {gs_replay_submits}")
    if not gs_replay_calls or (gs_replay_calls[0][1] or {}).get("validated_order_quantity") != 1:
        failures.append(f"GS replay correction used wrong desired order: {gs_replay_calls}")

    # GS replay boundary: available cash exactly equal to the incremental notional (900) must
    # still pass -- the gate only blocks when the incremental exceeds what is available.
    gs_boundary_pass_order, _gs_boundary_pass_calls, gs_boundary_pass_submits = _run_active_buy_correction_case(
        active_qty=1,
        active_price=82_300,
        final_holding_quantity=12,
        current_holding=11,
        order_price=83_200,
        capacities={"078930": {"max_buy_qty": 5, "max_buy_amt": 900}},
    )
    if gs_boundary_pass_order.get("result") != "submitted" or gs_boundary_pass_order.get("reason") != "active_order_correction_submitted":
        failures.append(f"incremental notional exactly at available cash (900) should pass: {gs_boundary_pass_order}")
    if gs_boundary_pass_submits:
        failures.append(f"boundary-pass correction should not call submit_order: {gs_boundary_pass_submits}")

    # Same quantity, price decrease -- must not require gross new-buy capacity.
    price_decrease_order, _price_decrease_calls, price_decrease_submits = _run_active_buy_correction_case(
        active_qty=1,
        active_price=100_000,
        final_holding_quantity=11,
        current_holding=10,
        order_price=95_000,
        capacities={},
    )
    if price_decrease_order.get("result") != "submitted" or price_decrease_order.get("reason") != "active_order_correction_submitted":
        failures.append(f"same-quantity price decrease was not corrected without gross capacity: {price_decrease_order}")
    if price_decrease_submits:
        failures.append(f"price decrease correction should not call submit_order: {price_decrease_submits}")

    # Quantity decrease -- must not require gross new-buy capacity.
    qty_decrease_order, _qty_decrease_calls, qty_decrease_submits = _run_active_buy_correction_case(
        active_qty=3,
        active_price=100_000,
        final_holding_quantity=13,
        current_holding=11,
        order_price=100_000,
        capacities={},
    )
    if qty_decrease_order.get("result") != "submitted" or qty_decrease_order.get("reason") != "active_order_correction_submitted":
        failures.append(f"quantity decrease was not corrected without gross capacity: {qty_decrease_order}")
    if qty_decrease_order.get("validated_order_quantity") != 2:
        failures.append(f"quantity decrease correction used wrong quantity: {qty_decrease_order}")
    if qty_decrease_submits:
        failures.append(f"quantity decrease correction should not call submit_order: {qty_decrease_submits}")

    # Quantity decrease whose higher price still raises total notional -- the incremental
    # notional (not the quantity direction alone) drives the gate, and it passes once the
    # symbol's fresh capacity covers it.
    notional_up_order, _notional_up_calls, notional_up_submits = _run_active_buy_correction_case(
        active_qty=2,
        active_price=100_000,
        final_holding_quantity=11,
        current_holding=10,
        order_price=250_000,
        capacities={"078930": {"max_buy_qty": 5, "max_buy_amt": 5_000_000}},
    )
    if notional_up_order.get("result") != "submitted" or notional_up_order.get("reason") != "active_order_correction_submitted":
        failures.append(f"quantity decrease with higher total notional was not corrected with sufficient capacity: {notional_up_order}")
    if notional_up_order.get("validated_order_quantity") != 1:
        failures.append(f"quantity decrease with higher notional correction used wrong quantity: {notional_up_order}")
    if notional_up_submits:
        failures.append(f"quantity decrease with higher notional correction should not call submit_order: {notional_up_submits}")

    # Quantity increase: the final delta now agrees with the active buy's direction, so it is
    # kept and only the incremental quantity is submitted as an additional order (pre-existing
    # invariant) -- this must keep working once buy corrections are gated incrementally too.
    original_adjust_active_order = execute_orders_module.adjust_active_order
    original_submit_order = execute_orders_module.submit_order
    qty_increase_corrections: list[Any] = []
    qty_increase_submissions: list[dict[str, Any]] = []

    def fake_reject_correction(kis: Any, active: dict[str, Any], desired: dict[str, Any] | None) -> tuple[str, str, str]:
        qty_increase_corrections.append((dict(active), dict(desired) if desired else None))
        return "unexpected-correction", "correct", "unexpected correction"

    def fake_submit_additional(kis: Any, order: dict[str, Any]) -> str:
        qty_increase_submissions.append(dict(order))
        return "additional-buy-078930"

    qty_increase_execution = {
        "orders": [
            {"symbol_id": "078930", "symbol_name": "GS", "final_holding_quantity": 13, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 100_000, "order_path": "immediate"}
        ]
    }
    try:
        execute_orders_module.adjust_active_order = fake_reject_correction
        execute_orders_module.submit_order = fake_submit_additional
        reconcile(
            {"account_summary": {"cash_amount": 10_000_000}, "symbols": [{"symbol_id": "078930", "symbol_name": "GS", "current_live_holding_quantity": 10}]},
            qty_increase_execution,
            [
                {
                    "symbol_id": "078930",
                    "symbol_name": "GS",
                    "order_id": "0015300400",
                    "order_kind": "pending",
                    "direction": "buy",
                    "decision_guard": {"status": "allowed"}, "remaining_quantity": 1,
                    "order_price": 100_000,
                    "active_status": "active",
                    "order_api": "order_cash",
                    "order_path": "immediate",
                    "execution_environment": "real",
                    "observed_at": now_iso(),
                }
            ],
            {"078930": {"max_buy_qty": 5, "max_buy_amt": 5_000_000}},
            {},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.adjust_active_order = original_adjust_active_order
        execute_orders_module.submit_order = original_submit_order
    qty_increase_order = qty_increase_execution["orders"][0]
    if qty_increase_order.get("result") != "submitted" or qty_increase_order.get("reason") != "active_order_kept_and_additional_order_submitted":
        failures.append(f"quantity increase should keep the active order and submit only the increment: {qty_increase_order}")
    if qty_increase_order.get("validated_order_quantity") != 2:
        failures.append(f"quantity increase gated the wrong incremental quantity: {qty_increase_order}")
    if qty_increase_corrections:
        failures.append(f"quantity increase should not call active-order correction: {qty_increase_corrections}")
    if not qty_increase_submissions or qty_increase_submissions[0].get("validated_order_quantity") != 2:
        failures.append(f"quantity increase used wrong additional-order quantity: {qty_increase_submissions}")
    return failures


def step_active_buy_correction_capacity_gate_checks(root: Path) -> list[str]:
    """Buy corrections that add exposure must fail closed without fresh incremental capacity evidence."""
    failures: list[str] = []

    # Missing capacity for a same-quantity price increase must fail closed, not fall through to
    # the gross new-buy gate (this is the GS replay's `buy_cash_limit_missing` regression).
    missing_capacity_order, missing_capacity_calls, missing_capacity_submits = _run_active_buy_correction_case(
        active_qty=1,
        active_price=82_300,
        final_holding_quantity=12,
        current_holding=11,
        order_price=83_200,
        capacities={},
    )
    if missing_capacity_order.get("result") != "blocked" or missing_capacity_order.get("reason") != "buy_cash_limit_missing":
        failures.append(f"missing incremental capacity did not fail closed: {missing_capacity_order}")
    if missing_capacity_order.get("order_or_reservation_id") != "0015300400":
        failures.append(f"missing capacity block should retain the original active order id: {missing_capacity_order}")
    if missing_capacity_calls:
        failures.append(f"missing capacity should not call active-order correction: {missing_capacity_calls}")
    if missing_capacity_submits:
        failures.append(f"missing capacity should not call submit_order: {missing_capacity_submits}")

    # Insufficient (but present) capacity for the same price increase must also fail closed.
    insufficient_capacity_order, insufficient_capacity_calls, insufficient_capacity_submits = _run_active_buy_correction_case(
        active_qty=1,
        active_price=82_300,
        final_holding_quantity=12,
        current_holding=11,
        order_price=83_200,
        capacities={"078930": {"max_buy_qty": 5, "max_buy_amt": 500}},
    )
    if insufficient_capacity_order.get("result") != "blocked" or insufficient_capacity_order.get("reason") != "buy_cash_gate_reduced_reverse_rank":
        failures.append(f"insufficient incremental capacity did not fail closed: {insufficient_capacity_order}")
    if insufficient_capacity_calls:
        failures.append(f"insufficient capacity should not call active-order correction: {insufficient_capacity_calls}")
    if insufficient_capacity_submits:
        failures.append(f"insufficient capacity should not call submit_order: {insufficient_capacity_submits}")

    # GS replay boundary: available cash one short of the incremental notional (899 < 900) must
    # block -- confirms the boundary is strictly ">" and not ">=".
    gs_boundary_block_order, gs_boundary_block_calls, gs_boundary_block_submits = _run_active_buy_correction_case(
        active_qty=1,
        active_price=82_300,
        final_holding_quantity=12,
        current_holding=11,
        order_price=83_200,
        capacities={"078930": {"max_buy_qty": 5, "max_buy_amt": 899}},
    )
    if gs_boundary_block_order.get("result") != "blocked" or gs_boundary_block_order.get("reason") != "buy_cash_gate_reduced_reverse_rank":
        failures.append(f"incremental notional one above available cash (899) should fail closed: {gs_boundary_block_order}")
    if gs_boundary_block_calls or gs_boundary_block_submits:
        failures.append(f"boundary-block case should not adjust or submit: calls={gs_boundary_block_calls}, submits={gs_boundary_block_submits}")

    # A quantity decrease whose higher price still raises total notional must fail closed without
    # capacity, even though the quantity direction alone is a decrease.
    notional_up_missing_order, notional_up_missing_calls, notional_up_missing_submits = _run_active_buy_correction_case(
        active_qty=2,
        active_price=100_000,
        final_holding_quantity=11,
        current_holding=10,
        order_price=250_000,
        capacities={},
    )
    if notional_up_missing_order.get("result") != "blocked" or notional_up_missing_order.get("reason") != "buy_cash_limit_missing":
        failures.append(f"quantity decrease with higher notional and missing capacity did not fail closed: {notional_up_missing_order}")
    if notional_up_missing_order.get("order_or_reservation_id") != "0015300400":
        failures.append(f"quantity-decrease/notional-up block should retain the original active order id: {notional_up_missing_order}")
    if notional_up_missing_calls:
        failures.append(f"quantity-decrease/notional-up missing capacity should not call active-order correction: {notional_up_missing_calls}")
    if notional_up_missing_submits:
        failures.append(f"quantity-decrease/notional-up missing capacity should not call submit_order: {notional_up_missing_submits}")

    return failures


def step_active_buy_correction_gate_independence_checks(root: Path) -> list[str]:
    """Incremental quantity and incremental notional are independent checks inside
    active_buy_correction_gate: a positive quantity increase must be validated against
    max_buy_qty even if notional does not rise, and a positive notional increase must be
    validated against max_buy_amt/cash even if quantity does not rise."""
    failures: list[str] = []
    conflict = {"remaining_quantity": 1, "order_price": 100_000}

    # Quantity increase with a lower price (notional does not increase) must still be blocked by
    # an insufficient max_buy_qty, even though max_buy_amt is ample.
    qty_gate_order = {"attempts": []}
    qty_gate_cash, qty_gate_blocked = active_buy_correction_gate(
        qty_gate_order,
        conflict=conflict,
        desired_qty=3,
        price=30_000,
        symbol="078930",
        capacities={"078930": {"max_buy_qty": 1, "max_buy_amt": 5_000_000}},
        used_cash=0,
        cash_limit=5_000_000,
    )
    if not qty_gate_blocked or qty_gate_order.get("reason") != "buy_quantity_exceeds_order_available_quantity":
        failures.append(f"quantity increase with flat/lower notional did not gate on max_buy_qty independently: cash={qty_gate_cash}, blocked={qty_gate_blocked}, order={qty_gate_order}")

    # The same quantity increase passes once max_buy_qty covers the incremental shares.
    qty_gate_ok_order = {"attempts": []}
    qty_gate_ok_cash, qty_gate_ok_blocked = active_buy_correction_gate(
        qty_gate_ok_order,
        conflict=conflict,
        desired_qty=3,
        price=30_000,
        symbol="078930",
        capacities={"078930": {"max_buy_qty": 5, "max_buy_amt": 5_000_000}},
        used_cash=0,
        cash_limit=5_000_000,
    )
    if qty_gate_ok_blocked or qty_gate_ok_cash != 0:
        failures.append(f"quantity increase with sufficient max_buy_qty should pass with zero incremental cash: cash={qty_gate_ok_cash}, blocked={qty_gate_ok_blocked}, order={qty_gate_ok_order}")

    # Notional increase with a flat/lower quantity must gate on cash even when max_buy_qty is
    # zero, since no additional shares are being requested.
    cash_gate_order = {"attempts": []}
    cash_gate_cash, cash_gate_blocked = active_buy_correction_gate(
        cash_gate_order,
        conflict=conflict,
        desired_qty=1,
        price=250_000,
        symbol="078930",
        capacities={"078930": {"max_buy_qty": 0, "max_buy_amt": 5_000_000}},
        used_cash=0,
        cash_limit=5_000_000,
    )
    if cash_gate_blocked or cash_gate_cash != 150_000:
        failures.append(f"notional increase with flat/lower quantity should not require max_buy_qty: cash={cash_gate_cash}, blocked={cash_gate_blocked}, order={cash_gate_order}")

    # used_cash from earlier orders in the same run must be included in the boundary: exactly
    # filling the remaining cash passes, one more than that blocks.
    gs_conflict = {"remaining_quantity": 1, "order_price": 82_300}
    used_cash_boundary_pass_order = {"attempts": []}
    used_cash_boundary_pass_cash, used_cash_boundary_pass_blocked = active_buy_correction_gate(
        used_cash_boundary_pass_order,
        conflict=gs_conflict,
        desired_qty=1,
        price=83_200,
        symbol="078930",
        capacities={"078930": {"max_buy_qty": 5, "max_buy_amt": 5_000_000}},
        used_cash=4_999_100,
        cash_limit=5_000_000,
    )
    if used_cash_boundary_pass_blocked or used_cash_boundary_pass_cash != 900:
        failures.append(f"used_cash + incremental exactly at cash_limit should pass: cash={used_cash_boundary_pass_cash}, blocked={used_cash_boundary_pass_blocked}, order={used_cash_boundary_pass_order}")

    used_cash_boundary_block_order = {"attempts": []}
    used_cash_boundary_block_cash, used_cash_boundary_block_blocked = active_buy_correction_gate(
        used_cash_boundary_block_order,
        conflict=gs_conflict,
        desired_qty=1,
        price=83_200,
        symbol="078930",
        capacities={"078930": {"max_buy_qty": 5, "max_buy_amt": 5_000_000}},
        used_cash=4_999_101,
        cash_limit=5_000_000,
    )
    if not used_cash_boundary_block_blocked or used_cash_boundary_block_order.get("reason") != "buy_cash_gate_reduced_reverse_rank":
        failures.append(f"used_cash + incremental one above cash_limit should fail closed: cash={used_cash_boundary_block_cash}, blocked={used_cash_boundary_block_blocked}, order={used_cash_boundary_block_order}")

    return failures


def step_refresh_gates_active_buy_capacity_prefetch_checks(root: Path) -> list[str]:
    """refresh_gates must prefetch buy capacity for a symbol with an active pending buy even when
    the pre-reconcile order direction is 'none', so a later same-direction buy correction has
    fresh capacity evidence to validate against instead of discovering it missing mid-reconcile."""
    failures: list[str] = []
    original_kis = execute_orders_module.Kis
    original_fetch_reservations = execute_orders_module.fetch_reservations
    original_fetch_pending_orders = execute_orders_module.fetch_pending_orders
    original_buy_capacity = execute_orders_module.buy_capacity
    capacity_calls: list[tuple[str, int]] = []

    class FakeRefreshKis:
        env = "real"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    def fake_fetch_reservations(kis: Any, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return []

    def fake_fetch_pending_orders(kis: Any) -> list[dict[str, Any]]:
        return [
            {
                "symbol_id": "078930",
                "symbol_name": "GS",
                "order_id": "0015300400",
                "order_kind": "pending",
                "direction": "buy",
                "remaining_quantity": 1,
                "order_price": 82_300,
                "active_status": "active",
                "order_api": "order_cash",
                "order_path": "immediate",
                "execution_environment": "real",
                "observed_at": now_iso(),
            }
        ]

    def fake_buy_capacity(kis: Any, symbol: str, price: int) -> dict[str, int]:
        capacity_calls.append((symbol, price))
        return {"max_buy_qty": 5, "max_buy_amt": 5_000_000}

    try:
        execute_orders_module.Kis = FakeRefreshKis
        execute_orders_module.fetch_reservations = fake_fetch_reservations
        execute_orders_module.fetch_pending_orders = fake_fetch_pending_orders
        execute_orders_module.buy_capacity = fake_buy_capacity
        active, capacities, sell_capacities, errors, kis = refresh_gates(
            argparse.Namespace(env="real", retries=0, offline=False, reservation_start_date="20260701", reservation_end_date="20260731"),
            {"account_summary": {"cash_amount": 10_000_000}, "symbols": [{"symbol_id": "078930", "symbol_name": "GS", "current_live_holding_quantity": 11}]},
            {
                "orders": [
                    {
                        "symbol_id": "078930",
                        "symbol_name": "GS",
                        "final_holding_quantity": 12,
                        "decision_guard": {"status": "allowed"}, "order_price": 83_200,
                        "order_path": "immediate",
                        "direction": "none",
                    }
                ]
            },
        )
    finally:
        execute_orders_module.Kis = original_kis
        execute_orders_module.fetch_reservations = original_fetch_reservations
        execute_orders_module.fetch_pending_orders = original_fetch_pending_orders
        execute_orders_module.buy_capacity = original_buy_capacity

    if "078930" not in capacities:
        failures.append(f"refresh_gates did not prefetch buy capacity for a symbol with an active pending buy: {capacities}")
    if capacity_calls != [("078930", 83_200)]:
        failures.append(f"refresh_gates fetched buy capacity with unexpected arguments: {capacity_calls}")
    if errors:
        failures.append(f"refresh_gates recorded unexpected errors: {errors}")
    if len(active) != 1:
        failures.append(f"refresh_gates did not return the fetched active orders: {active}")

    # A prefetch failure for a symbol whose capacity is only opportunistically fetched (its
    # pre-reconcile direction is 'none', not an initial new buy) must not become a required gate
    # error -- a de-risking correction (price/quantity decrease) needs no capacity evidence at
    # all, so it must still be able to proceed through reconcile() afterwards.
    def fake_failing_buy_capacity(kis: Any, symbol: str, price: int) -> dict[str, int]:
        raise RuntimeError("transient order-available lookup failure")

    try:
        execute_orders_module.Kis = FakeRefreshKis
        execute_orders_module.fetch_reservations = fake_fetch_reservations
        execute_orders_module.fetch_pending_orders = fake_fetch_pending_orders
        execute_orders_module.buy_capacity = fake_failing_buy_capacity
        _, failed_prefetch_capacities, _, failed_prefetch_errors, _ = refresh_gates(
            argparse.Namespace(env="real", retries=0, offline=False, reservation_start_date="20260701", reservation_end_date="20260731"),
            {"account_summary": {"cash_amount": 10_000_000}, "symbols": [{"symbol_id": "078930", "symbol_name": "GS", "current_live_holding_quantity": 11}]},
            {
                "orders": [
                    {
                        "symbol_id": "078930",
                        "symbol_name": "GS",
                        "final_holding_quantity": 12,
                        "decision_guard": {"status": "allowed"}, "order_price": 80_000,
                        "order_path": "immediate",
                        "direction": "none",
                    }
                ]
            },
        )
    finally:
        execute_orders_module.Kis = original_kis
        execute_orders_module.fetch_reservations = original_fetch_reservations
        execute_orders_module.fetch_pending_orders = original_fetch_pending_orders
        execute_orders_module.buy_capacity = original_buy_capacity

    if failed_prefetch_errors:
        failures.append(f"opportunistic active-buy capacity prefetch failure should not be a required gate error: {failed_prefetch_errors}")
    if "078930" in failed_prefetch_capacities:
        failures.append(f"failed prefetch should not leave stale/fabricated capacity data: {failed_prefetch_capacities}")

    # With capacity absent, a de-risking correction (price decrease, same quantity) must still
    # succeed through reconcile() -- it must not require the failed evidence at all.
    de_risking_order, _de_risking_calls, de_risking_submits = _run_active_buy_correction_case(
        active_qty=1,
        active_price=100_000,
        final_holding_quantity=11,
        current_holding=10,
        order_price=80_000,
        capacities=failed_prefetch_capacities,
    )
    if de_risking_order.get("result") != "submitted" or de_risking_order.get("reason") != "active_order_correction_submitted":
        failures.append(f"de-risking correction should proceed despite a failed opportunistic capacity prefetch: {de_risking_order}")
    if de_risking_submits:
        failures.append(f"de-risking correction after failed prefetch should not call submit_order: {de_risking_submits}")

    return failures


def _run_refresh_gates_case(
    *,
    active_orders: list[dict[str, Any]],
    execution_orders: list[dict[str, Any]],
    current_holding: int,
    buy_capacity_impl: Any = None,
    sell_capacity_impl: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]], dict[str, dict[str, int]], list[dict[str, Any]]]:
    original_kis = execute_orders_module.Kis
    original_fetch_reservations = execute_orders_module.fetch_reservations
    original_fetch_pending_orders = execute_orders_module.fetch_pending_orders
    original_buy_capacity = execute_orders_module.buy_capacity
    original_sell_capacity = execute_orders_module.sell_capacity

    class FakeRefreshKis:
        env = "real"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    def fake_fetch_reservations(kis: Any, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return []

    def fake_fetch_pending_orders(kis: Any) -> list[dict[str, Any]]:
        return active_orders

    def default_buy_capacity(kis: Any, symbol: str, price: int) -> dict[str, int]:
        return {"max_buy_qty": 5, "max_buy_amt": 5_000_000}

    def default_sell_capacity(kis: Any, symbol: str) -> dict[str, int]:
        return {"max_sell_qty": 5}

    try:
        execute_orders_module.Kis = FakeRefreshKis
        execute_orders_module.fetch_reservations = fake_fetch_reservations
        execute_orders_module.fetch_pending_orders = fake_fetch_pending_orders
        execute_orders_module.buy_capacity = buy_capacity_impl or default_buy_capacity
        execute_orders_module.sell_capacity = sell_capacity_impl or default_sell_capacity
        active, capacities, sell_capacities, errors, _kis = refresh_gates(
            argparse.Namespace(env="real", retries=0, offline=False, reservation_start_date="20260701", reservation_end_date="20260731"),
            {"account_summary": {"cash_amount": 10_000_000}, "symbols": [{"symbol_id": "078930", "symbol_name": "GS", "current_live_holding_quantity": current_holding}]},
            {"orders": execution_orders},
        )
    finally:
        execute_orders_module.Kis = original_kis
        execute_orders_module.fetch_reservations = original_fetch_reservations
        execute_orders_module.fetch_pending_orders = original_fetch_pending_orders
        execute_orders_module.buy_capacity = original_buy_capacity
        execute_orders_module.sell_capacity = original_sell_capacity
    return active, capacities, sell_capacities, errors


def step_refresh_gates_stale_direction_checks(root: Path) -> list[str]:
    """refresh_gates must decide whether a capacity-lookup failure is globally required from the
    fresh active orders it just fetched, not from the stale pre-reconcile execution.direction
    field -- a stale label can no longer describe what the run is actually about to do once fresh
    active orders are known."""
    failures: list[str] = []

    def gs_active_buy(price: int = 100_000, qty: int = 1) -> dict[str, Any]:
        return {
            "symbol_id": "078930",
            "symbol_name": "GS",
            "order_id": "0015300400",
            "order_kind": "pending",
            "direction": "buy",
            "remaining_quantity": qty,
            "order_price": price,
            "active_status": "active",
            "order_api": "order_cash",
            "order_path": "immediate",
            "execution_environment": "real",
            "observed_at": now_iso(),
        }

    def gs_active_sell(price: int = 100_000, qty: int = 1) -> dict[str, Any]:
        return {
            "symbol_id": "078930",
            "symbol_name": "GS",
            "order_id": "0015300401",
            "order_kind": "pending",
            "direction": "sell",
            "remaining_quantity": qty,
            "order_price": price,
            "active_status": "active",
            "order_api": "order_cash",
            "order_path": "immediate",
            "execution_environment": "real",
            "observed_at": now_iso(),
        }

    def failing_buy_capacity(kis: Any, symbol: str, price: int) -> dict[str, int]:
        raise RuntimeError("transient buy-capacity lookup failure")

    def failing_sell_capacity(kis: Any, symbol: str) -> dict[str, int]:
        raise RuntimeError("transient sell-capacity lookup failure")

    # Reproduction 1: stale direction=buy, current=10, final=11, fresh active buy 1@100000,
    # desired 80000 -- a de-risk price correction. A transient buy-capacity failure must not
    # globally abort.
    _active1, _capacities1, _sell1, errors1 = _run_refresh_gates_case(
        active_orders=[gs_active_buy(price=100_000, qty=1)],
        execution_orders=[
            {"symbol_id": "078930", "symbol_name": "GS", "final_holding_quantity": 11, "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 80_000, "order_path": "immediate", "direction": "buy"}
        ],
        current_holding=10,
        buy_capacity_impl=failing_buy_capacity,
    )
    if errors1:
        failures.append(f"stale direction=buy de-risk correction should not globally abort on buy-capacity failure: {errors1}")

    # Reproduction 2: stale direction=sell, current=10, final=9, fresh active buy 2 -- an
    # active-buy shrink/correction. A transient sell-capacity failure must not globally abort.
    _active2, _capacities2, _sell2, errors2 = _run_refresh_gates_case(
        active_orders=[gs_active_buy(price=100_000, qty=2)],
        execution_orders=[
            {"symbol_id": "078930", "symbol_name": "GS", "final_holding_quantity": 9, "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}, "order_price": 100_000, "order_path": "immediate", "direction": "sell"}
        ],
        current_holding=10,
        sell_capacity_impl=failing_sell_capacity,
    )
    if errors2:
        failures.append(f"stale direction=sell active-buy shrink should not globally abort on sell-capacity failure: {errors2}")

    # Reproduction 3: stale direction=buy with a fresh active sell that must be cancelled before
    # a deferred buy replacement -- a buy-capacity failure must not block the cancel.
    _active3, _capacities3, _sell3, errors3 = _run_refresh_gates_case(
        active_orders=[gs_active_sell(price=100_000, qty=1)],
        execution_orders=[
            {"symbol_id": "078930", "symbol_name": "GS", "final_holding_quantity": 12, "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 100_000, "order_path": "immediate", "direction": "buy"}
        ],
        current_holding=10,
        buy_capacity_impl=failing_buy_capacity,
    )
    if errors3:
        failures.append(f"stale direction=buy with a fresh active sell to cancel should not globally abort on buy-capacity failure: {errors3}")

    # Genuine no-active buy must still retain a required lookup failure.
    _active4, _capacities4, _sell4, errors4 = _run_refresh_gates_case(
        active_orders=[],
        execution_orders=[
            {"symbol_id": "078930", "symbol_name": "GS", "final_holding_quantity": 5, "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 100_000, "order_path": "immediate", "direction": "buy"}
        ],
        current_holding=0,
        buy_capacity_impl=failing_buy_capacity,
    )
    if not errors4 or errors4[0].get("code") != "order_available_lookup_failed":
        failures.append(f"genuine no-active buy should still retain a required buy-capacity lookup failure: {errors4}")

    # Genuine no-active sell must still retain a required lookup failure.
    _active5, _capacities5, _sell5, errors5 = _run_refresh_gates_case(
        active_orders=[],
        execution_orders=[
            {"symbol_id": "078930", "symbol_name": "GS", "final_holding_quantity": 0, "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}, "order_price": 100_000, "order_path": "immediate", "direction": "sell"}
        ],
        current_holding=5,
        sell_capacity_impl=failing_sell_capacity,
    )
    if not errors5 or errors5[0].get("code") != "sell_available_lookup_failed":
        failures.append(f"genuine no-active sell should still retain a required sell-capacity lookup failure: {errors5}")

    # A stale buy label must not add an unrelated required buy lookup to a genuine fresh sell.
    _active6, capacities6, sell6, errors6 = _run_refresh_gates_case(
        active_orders=[],
        execution_orders=[
            {"symbol_id": "078930", "symbol_name": "GS", "final_holding_quantity": 0, "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}, "order_price": 100_000, "order_path": "immediate", "direction": "buy"}
        ],
        current_holding=5,
        buy_capacity_impl=failing_buy_capacity,
    )
    if errors6 or capacities6 or sell6.get("078930", {}).get("max_sell_qty") != 5:
        failures.append(f"fresh sell should ignore a stale buy direction during gate lookup: capacities={capacities6}, sell={sell6}, errors={errors6}")

    # A no-op must not perform a required lookup solely because a stale direction remains.
    _active7, capacities7, sell7, errors7 = _run_refresh_gates_case(
        active_orders=[],
        execution_orders=[
            {"symbol_id": "078930", "symbol_name": "GS", "final_holding_quantity": 5, "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}, "order_price": 100_000, "order_path": "immediate", "direction": "sell"}
        ],
        current_holding=5,
        sell_capacity_impl=failing_sell_capacity,
    )
    if errors7 or capacities7 or sell7:
        failures.append(f"fresh no-op should not run capacity lookup from a stale direction: capacities={capacities7}, sell={sell7}, errors={errors7}")

    return failures


def step_refresh_gates_and_correction_price_normalization_checks(root: Path) -> list[str]:
    """A raw order price of 83250 must be normalized to the buy tick (83300) consistently for
    both the refresh_gates buy-capacity lookup and the reconcile() correction payload sent to
    adjust_active_order -- otherwise capacity evidence is checked against a different price than
    what is actually submitted to the broker."""
    failures: list[str] = []
    capacity_calls: list[tuple[str, int]] = []

    def recording_buy_capacity(kis: Any, symbol: str, price: int) -> dict[str, int]:
        capacity_calls.append((symbol, price))
        return {"max_buy_qty": 5, "max_buy_amt": 5_000_000}

    _active, capacities, _sell, errors = _run_refresh_gates_case(
        active_orders=[
            {
                "symbol_id": "078930",
                "symbol_name": "GS",
                "order_id": "0015300400",
                "order_kind": "pending",
                "direction": "buy",
                "remaining_quantity": 1,
                "order_price": 82_300,
                "active_status": "active",
                "order_api": "order_cash",
                "order_path": "immediate",
                "execution_environment": "real",
                "observed_at": now_iso(),
            }
        ],
        execution_orders=[
            {"symbol_id": "078930", "symbol_name": "GS", "final_holding_quantity": 12, "decision_guard": {"status": "allowed"}, "order_price": 83_250, "order_path": "immediate", "direction": "none"}
        ],
        current_holding=11,
        buy_capacity_impl=recording_buy_capacity,
    )
    if capacity_calls != [("078930", 83_300)]:
        failures.append(f"refresh_gates should normalize 83250 to the buy tick 83300 for the capacity lookup: {capacity_calls}")
    if errors:
        failures.append(f"refresh_gates should not record errors for this correction scenario: {errors}")

    correction_order, correction_calls, correction_submits = _run_active_buy_correction_case(
        active_qty=1,
        active_price=82_300,
        final_holding_quantity=12,
        current_holding=11,
        order_price=83_250,
        capacities=capacities,
    )
    if correction_order.get("result") != "submitted" or correction_order.get("reason") != "active_order_correction_submitted":
        failures.append(f"83250 correction should submit using the capacity fetched at 83300: {correction_order}")
    if not correction_calls or (correction_calls[0][1] or {}).get("order_price") != 83_300:
        failures.append(f"correction payload should use the buy-tick-normalized 83300, not the raw 83250: {correction_calls}")
    if correction_submits:
        failures.append(f"normalized-price correction should not call submit_order: {correction_submits}")

    return failures


def step_pre_refresh_normalization_preserves_fresh_side_checks(root: Path) -> list[str]:
    """Pre-refresh normalization must use the fresh account holding, not stale execution-side or
    expected-holding fields, and must feed the same tick-normalized price to capacity and broker
    correction paths."""
    failures: list[str] = []
    capacity_calls: list[tuple[str, int]] = []

    def recording_buy_capacity(kis: Any, symbol: str, price: int) -> dict[str, int]:
        capacity_calls.append((symbol, price))
        return {"max_buy_qty": 5, "max_buy_amt": 5_000_000}

    execution = {
        "orders": [
            {
                "symbol_id": "078930",
                "symbol_name": "GS",
                "direction": "sell",
                "current_live_holding_quantity": 11,
                "expected_holding_quantity": 20,
                "final_holding_quantity": 12,
                "decision_guard": {"status": "allowed"}, "order_price": 83_250,
                "order_path": "immediate",
            }
        ]
    }
    fresh_buy_account = {
        "symbols": [{"symbol_id": "078930", "symbol_name": "GS", "current_live_holding_quantity": 11}]
    }
    normalize_execution_order_prices(execution, fresh_buy_account)
    if as_int(execution["orders"][0].get("order_price")) != 83_300:
        failures.append(f"pre-refresh normalization should use fresh current-to-final buy side despite stale sell evidence: {execution['orders'][0]}")

    _active, capacities, _sell, errors = _run_refresh_gates_case(
        active_orders=[
            {
                "symbol_id": "078930",
                "symbol_name": "GS",
                "order_id": "0015300400",
                "order_kind": "pending",
                "direction": "buy",
                "remaining_quantity": 1,
                "order_price": 82_300,
                "active_status": "active",
                "order_api": "order_cash",
                "order_path": "immediate",
                "execution_environment": "real",
                "observed_at": now_iso(),
            }
        ],
        execution_orders=execution["orders"],
        current_holding=11,
        buy_capacity_impl=recording_buy_capacity,
    )
    if capacity_calls != [("078930", 83_300)]:
        failures.append(f"fresh-side capacity lookup after pre-refresh normalization should use the buy tick 83300: {capacity_calls}")
    if errors:
        failures.append(f"refresh_gates should not record errors for this correction scenario: {errors}")

    correction_order, correction_calls, correction_submits = _run_active_buy_correction_case(
        active_qty=1,
        active_price=82_300,
        final_holding_quantity=12,
        current_holding=11,
        order_price=as_int(execution["orders"][0].get("order_price")),
        capacities=capacities,
    )
    if correction_order.get("result") != "submitted" or correction_order.get("reason") != "active_order_correction_submitted":
        failures.append(f"correction after pre-refresh normalization should still submit using the recovered buy tick: {correction_order}")
    if not correction_calls or (correction_calls[0][1] or {}).get("order_price") != 83_300:
        failures.append(f"correction payload after pre-refresh normalization should use 83300, not a stale-direction-rounded price: {correction_calls}")
    if correction_submits:
        failures.append(f"this correction should not call submit_order: {correction_submits}")

    # Symmetric risk: stale buy evidence must not pre-round a fresh sell upward.
    symmetric_execution = {
        "orders": [
            {
                "symbol_id": "078930",
                "symbol_name": "GS",
                "direction": "buy",
                "current_live_holding_quantity": 12,
                "expected_holding_quantity": 5,
                "final_holding_quantity": 11,
                "decision_guard": {"status": "allowed"}, "order_price": 83_250,
                "order_path": "immediate",
            }
        ]
    }
    fresh_sell_account = {
        "symbols": [{"symbol_id": "078930", "symbol_name": "GS", "current_live_holding_quantity": 12}]
    }
    normalize_execution_order_prices(symmetric_execution, fresh_sell_account)
    if as_int(symmetric_execution["orders"][0].get("order_price")) != 83_200:
        failures.append(f"pre-refresh normalization should use fresh current-to-final sell side despite stale buy evidence: {symmetric_execution['orders'][0]}")

    return failures


def step_active_order_replacement_checks(root: Path) -> list[str]:
    """An opposite-direction active order has its cancellation submitted, but the replacement is
    deferred to a later run instead of being submitted immediately -- a cancel request id is only
    broker acceptance, not confirmed release of the held quantity/cash."""
    failures: list[str] = []
    replacement_execution = {
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 5, "decision_guard": {"status": "allowed"}, "order_price": 70000, "order_path": "immediate"}
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
                    "decision_guard": {"status": "allowed"}, "remaining_quantity": 1,
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
    if replacement_order.get("result") != "submitted" or replacement_order.get("reason") != "active_order_cancel_submitted":
        failures.append(f"cancelled active order should defer the replacement to a later run: {replacement_order}")
    if replacement_order.get("direction") != "none" or replacement_order.get("validated_order_quantity") != 0:
        failures.append(f"deferred replacement should not claim a sell was submitted: {replacement_order}")
    if replacement_order.get("order_or_reservation_id") != "cancel1":
        failures.append(f"deferred replacement should record the cancel request id: {replacement_order}")
    if replacement_submissions:
        failures.append(f"deferred replacement should not call submit_order in the same run: {replacement_submissions}")
    if replacement_adjustment.get("replacement_required") is not True:
        failures.append(f"replacement adjustment row should still flag replacement as required for a later run: {replacement_adjustment}")
    if replacement_adjustment.get("replacement_order_id"):
        failures.append(f"deferred replacement should not record a replacement order id: {replacement_adjustment}")
    return failures


def step_active_order_replacement_edge_case_checks(root: Path) -> list[str]:
    """Invalid price blocks safely before any adjustment; and once a cancel is submitted, the
    deferred replacement path never calls submit_order in the same run, even if the stub would
    fail or return an uncertain id -- there is no code path left where that stub can fire."""
    failures: list[str] = []
    original_adjust_active_order = execute_orders_module.adjust_active_order
    original_submit_order = execute_orders_module.submit_order

    def fake_cancel_active_order(kis: Any, active: dict[str, Any], desired: dict[str, Any] | None) -> tuple[str, str, str]:
        if desired is not None:
            failures.append(f"replacement path should cancel before submitting new order: {desired}")
        return "cancel1", "cancel", "fake cancel"

    invalid_replacement_execution = {
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 5, "decision_guard": {"status": "allowed"}, "order_price": 0, "order_path": "immediate"}
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
                    "decision_guard": {"status": "allowed"}, "remaining_quantity": 1,
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

    deferred_replacement_execution = {
        "orders": [
            {"symbol_id": "005930", "symbol_name": "삼성전자", "final_holding_quantity": 5, "decision_guard": {"status": "allowed"}, "order_price": 70000, "order_path": "immediate"}
        ]
    }
    deferred_replacement_submissions: list[dict[str, Any]] = []

    def fake_failing_submit_order(kis: Any, order: dict[str, Any]) -> str:
        deferred_replacement_submissions.append(dict(order))
        raise RuntimeError("submit_order should not be called for a deferred replacement")

    try:
        execute_orders_module.adjust_active_order = fake_cancel_active_order
        execute_orders_module.submit_order = fake_failing_submit_order
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 10}]},
            deferred_replacement_execution,
            [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "order_id": "old-buy-2",
                    "order_kind": "pending",
                    "direction": "buy",
                    "decision_guard": {"status": "allowed"}, "remaining_quantity": 1,
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
    deferred_replacement_order = deferred_replacement_execution["orders"][0]
    if deferred_replacement_order.get("result") != "submitted" or deferred_replacement_order.get("reason") != "active_order_cancel_submitted":
        failures.append(f"opposite-direction cancel should defer replacement even when submit_order would fail: {deferred_replacement_order}")
    if deferred_replacement_submissions:
        failures.append(f"deferred replacement must never call submit_order, even one rigged to fail: {deferred_replacement_submissions}")
    return failures


def step_deferred_replacement_capacity_and_sell_increase_checks(root: Path) -> list[str]:
    """Insufficient capacity for a deferred replacement must not block the cancellation of the
    stale active order it is replacing, but missing capacity must still fail closed for a
    genuine same-direction active-order increase."""
    failures: list[str] = []
    original_adjust_active_order = execute_orders_module.adjust_active_order
    original_submit_order = execute_orders_module.submit_order

    # An active buy must still be cancelled even though the deferred sell replacement's own
    # capacity (max_sell_qty=0) is insufficient -- that capacity validation belongs to the later
    # run, not to gating this run's cancellation.
    insufficient_replacement_execution = {
        "orders": [
            {"symbol_id": "078930", "symbol_name": "GS", "final_holding_quantity": 5, "decision_guard": {"status": "allowed"}, "order_price": 100_000, "order_path": "immediate"}
        ]
    }
    insufficient_replacement_calls: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    insufficient_replacement_submits: list[dict[str, Any]] = []

    def fake_cancel_active_buy(kis: Any, active: dict[str, Any], desired: dict[str, Any] | None) -> tuple[str, str, str]:
        insufficient_replacement_calls.append((dict(active), dict(desired) if desired else None))
        return "cancel-buy-for-sell", "cancel", "fake cancel"

    def fake_submit_order(kis: Any, order: dict[str, Any]) -> str:
        insufficient_replacement_submits.append(dict(order))
        return "should-not-submit"

    try:
        execute_orders_module.adjust_active_order = fake_cancel_active_buy
        execute_orders_module.submit_order = fake_submit_order
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "078930", "symbol_name": "GS", "current_live_holding_quantity": 10}]},
            insufficient_replacement_execution,
            [
                {
                    "symbol_id": "078930",
                    "symbol_name": "GS",
                    "order_id": "0015300400",
                    "order_kind": "pending",
                    "direction": "buy",
                    "decision_guard": {"status": "allowed"}, "remaining_quantity": 1,
                    "order_price": 100_000,
                    "active_status": "active",
                    "order_api": "order_cash",
                    "order_path": "immediate",
                    "execution_environment": "real",
                    "observed_at": now_iso(),
                }
            ],
            {},
            {"078930": {"max_sell_qty": 0}},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.adjust_active_order = original_adjust_active_order
        execute_orders_module.submit_order = original_submit_order
    insufficient_replacement_order = insufficient_replacement_execution["orders"][0]
    if insufficient_replacement_order.get("result") != "submitted" or insufficient_replacement_order.get("reason") != "active_order_cancel_submitted":
        failures.append(f"insufficient future replacement capacity should not block cancelling the stale active order: {insufficient_replacement_order}")
    if len(insufficient_replacement_calls) != 1:
        failures.append(f"exactly one cancel/adjust call was expected: {insufficient_replacement_calls}")
    if insufficient_replacement_submits:
        failures.append(f"submit_order should remain zero when the deferred replacement's capacity is insufficient: {insufficient_replacement_submits}")

    # Missing sell capacity must still fail closed for a genuine same-direction active-sell
    # increase (the pre-existing "kept + additional order" invariant).
    sell_increase_execution = {
        "orders": [
            {"symbol_id": "078930", "symbol_name": "GS", "final_holding_quantity": 5, "decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}, "order_price": 100_000, "order_path": "immediate"}
        ]
    }
    sell_increase_calls: list[Any] = []
    sell_increase_submits: list[dict[str, Any]] = []

    def fake_reject_sell_correction(kis: Any, active: dict[str, Any], desired: dict[str, Any] | None) -> tuple[str, str, str]:
        sell_increase_calls.append((dict(active), dict(desired) if desired else None))
        return "should-not-correct", "correct", "unexpected correction"

    def fake_reject_sell_submit(kis: Any, order: dict[str, Any]) -> str:
        sell_increase_submits.append(dict(order))
        return "should-not-submit"

    try:
        execute_orders_module.adjust_active_order = fake_reject_sell_correction
        execute_orders_module.submit_order = fake_reject_sell_submit
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "078930", "symbol_name": "GS", "current_live_holding_quantity": 10}]},
            sell_increase_execution,
            [
                {
                    "symbol_id": "078930",
                    "symbol_name": "GS",
                    "order_id": "0015300402",
                    "order_kind": "pending",
                    "direction": "sell",
                    "decision_guard": {"status": "allowed"}, "remaining_quantity": 1,
                    "order_price": 100_000,
                    "active_status": "active",
                    "order_api": "order_cash",
                    "order_path": "immediate",
                    "execution_environment": "real",
                    "observed_at": now_iso(),
                }
            ],
            {},
            {},
            submit=True,
            kis=FakeKis(),
        )
    finally:
        execute_orders_module.adjust_active_order = original_adjust_active_order
        execute_orders_module.submit_order = original_submit_order
    sell_increase_order = sell_increase_execution["orders"][0]
    if sell_increase_order.get("result") != "blocked" or sell_increase_order.get("reason") != "sell_quantity_capacity_missing":
        failures.append(f"missing sell capacity should still block a genuine same-direction active-sell increase: {sell_increase_order}")
    if sell_increase_calls:
        failures.append(f"missing sell capacity should not call active-order correction: {sell_increase_calls}")
    if sell_increase_submits:
        failures.append(f"missing sell capacity should not call submit_order: {sell_increase_submits}")

    return failures


def self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        previous_portfolio_except_env = os.environ.get(PORTFOLIO_EXCEPT_ENV_VAR)
        os.environ[PORTFOLIO_EXCEPT_ENV_VAR] = str(root / "portfolio-except.txt")
        failures: list[str] = []
        failures.extend(step_dry_run_gate_and_portfolio_except_checks(root))
        failures.extend(step_decision_guard_and_reservation_normalization_checks(root))
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
        cls.decision_guard_failures = step_decision_guard_and_reservation_normalization_checks(cls.root)
        cls.active_order_conflict_failures = step_active_order_conflict_checks(cls.root)
        cls.reduction_and_available_cash_failures = step_reduction_and_available_cash_checks(cls.root)
        cls.max_buy_amt_and_zero_capacity_failures = step_max_buy_amt_and_zero_capacity_checks(cls.root)
        cls.active_sell_correction_failures = step_active_sell_correction_checks(cls.root)
        cls.active_sell_partial_and_cancel_only_failures = step_active_sell_partial_and_cancel_only_checks(cls.root)
        cls.active_sell_replacement_buy_failures = step_active_sell_replacement_buy_checks(cls.root)
        cls.active_order_additional_and_invalid_price_failures = step_active_order_additional_and_invalid_price_checks(cls.root)
        cls.active_buy_correction_failures = step_active_buy_correction_checks(cls.root)
        cls.active_buy_correction_capacity_gate_failures = step_active_buy_correction_capacity_gate_checks(cls.root)
        cls.active_buy_correction_gate_independence_failures = step_active_buy_correction_gate_independence_checks(cls.root)
        cls.refresh_gates_active_buy_capacity_prefetch_failures = step_refresh_gates_active_buy_capacity_prefetch_checks(cls.root)
        cls.refresh_gates_stale_direction_failures = step_refresh_gates_stale_direction_checks(cls.root)
        cls.refresh_gates_and_correction_price_normalization_failures = step_refresh_gates_and_correction_price_normalization_checks(cls.root)
        cls.pre_refresh_normalization_preserves_fresh_side_failures = step_pre_refresh_normalization_preserves_fresh_side_checks(cls.root)
        cls.active_order_replacement_failures = step_active_order_replacement_checks(cls.root)
        cls.active_order_replacement_edge_case_failures = step_active_order_replacement_edge_case_checks(cls.root)
        cls.deferred_replacement_capacity_and_sell_increase_failures = step_deferred_replacement_capacity_and_sell_increase_checks(cls.root)

    @classmethod
    def _restore_portfolio_except_env(cls) -> None:
        if cls._old_portfolio_except_env is None:
            os.environ.pop(PORTFOLIO_EXCEPT_ENV_VAR, None)
        else:
            os.environ[PORTFOLIO_EXCEPT_ENV_VAR] = cls._old_portfolio_except_env

    def test_step_dry_run_gate_and_portfolio_except_checks(self) -> None:
        self.assertEqual(self.dry_run_gate_failures, [])

    def test_step_decision_guard_and_reservation_normalization_checks(self) -> None:
        self.assertEqual(self.decision_guard_failures, [])

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

    def test_step_active_buy_correction_checks(self) -> None:
        self.assertEqual(self.active_buy_correction_failures, [])

    def test_step_active_buy_correction_capacity_gate_checks(self) -> None:
        self.assertEqual(self.active_buy_correction_capacity_gate_failures, [])

    def test_step_active_buy_correction_gate_independence_checks(self) -> None:
        self.assertEqual(self.active_buy_correction_gate_independence_failures, [])

    def test_step_refresh_gates_active_buy_capacity_prefetch_checks(self) -> None:
        self.assertEqual(self.refresh_gates_active_buy_capacity_prefetch_failures, [])

    def test_step_refresh_gates_stale_direction_checks(self) -> None:
        self.assertEqual(self.refresh_gates_stale_direction_failures, [])

    def test_step_refresh_gates_and_correction_price_normalization_checks(self) -> None:
        self.assertEqual(self.refresh_gates_and_correction_price_normalization_failures, [])

    def test_step_pre_refresh_normalization_preserves_fresh_side_checks(self) -> None:
        self.assertEqual(self.pre_refresh_normalization_preserves_fresh_side_failures, [])

    def test_step_active_order_replacement_checks(self) -> None:
        self.assertEqual(self.active_order_replacement_failures, [])

    def test_step_active_order_replacement_edge_case_checks(self) -> None:
        self.assertEqual(self.active_order_replacement_edge_case_failures, [])

    def test_step_deferred_replacement_capacity_and_sell_increase_checks(self) -> None:
        self.assertEqual(self.deferred_replacement_capacity_and_sell_increase_failures, [])


class ExecuteOrdersSelfTest(unittest.TestCase):
    def test_self_test_suite_runs_every_step_and_reports_success(self) -> None:
        """Wrapper-orchestration check only: each step's real behavior is
        covered by the granular tests below and by calling the step_*
        functions directly, so this mocks every step instead of re-running
        the whole scenario a second time."""
        step_names = [
            "step_dry_run_gate_and_portfolio_except_checks",
            "step_decision_guard_and_reservation_normalization_checks",
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
            f"{__name__}.step_decision_guard_and_reservation_normalization_checks", return_value=[]
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


class DecisionGuardActionBasisMismatchTest(unittest.TestCase):
    """Regression coverage: decision_guard_block_reason must fail closed unless status,
    canonical_action, and decision_basis all genuinely match this submission's side."""

    def test_allowed_matching_action_and_basis_is_not_blocked(self) -> None:
        order = {"decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}}
        self.assertEqual(decision_guard_block_reason(order, "buy"), "")

    def test_action_mismatch_blocks_even_when_status_allowed(self) -> None:
        # Guard says increase (a buy), but this submission is trying to sell -- must block.
        order = {"decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": "increase", "basis": "thesis"}}
        self.assertEqual(decision_guard_block_reason(order, "sell"), "decision_guard_action_mismatch")

    def test_reduce_and_exit_both_satisfy_a_sell_side(self) -> None:
        for action in ("reduce", "exit"):
            order = {"decision_basis": "thesis", "decision_guard": {"status": "allowed", "canonical_action": action, "basis": "thesis"}}
            self.assertEqual(decision_guard_block_reason(order, "sell"), "", msg=action)

    def test_basis_mismatch_blocks_even_with_matching_action(self) -> None:
        # A forged/stale guard: canonical_action matches the side, but decision_basis on the
        # order does not match what the guard was actually derived for.
        order = {"decision_basis": "profit_protection", "decision_guard": {"status": "allowed", "canonical_action": "reduce", "basis": "thesis"}}
        self.assertEqual(decision_guard_block_reason(order, "sell"), "decision_guard_basis_mismatch")

    def test_missing_guard_blocks(self) -> None:
        self.assertEqual(decision_guard_block_reason({}, "buy"), "decision_guard_not_allowed")

    def test_none_side_is_always_exempt(self) -> None:
        # Lifecycle-only cancellation/correction paths call this with side="none".
        self.assertEqual(decision_guard_block_reason({}, "none"), "")


class FreshBalanceRecheckTest(unittest.TestCase):
    """Regression coverage: profit_protection/concentration_rebalance rechecks must use a
    fresh KIS balance snapshot and fail closed when it is unavailable or no longer supports
    the guard's approved bounds."""

    class FakeBalanceKis:
        cano = "12345678"
        product = "01"
        env = "real"

        def __init__(self, body: dict[str, Any] | None = None, raise_error: bool = False) -> None:
            self._body = body or {}
            self._raise_error = raise_error

        def call(self, name: str, *, params: dict[str, str] | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
            if self._raise_error:
                raise RuntimeError("transient KIS balance lookup failure")
            return self._body

    def test_fetch_fresh_domestic_balance_returns_none_on_network_failure(self) -> None:
        kis = self.FakeBalanceKis(raise_error=True)
        self.assertIsNone(fetch_fresh_domestic_balance(kis))

    def test_fetch_fresh_domestic_balance_returns_none_without_kis(self) -> None:
        self.assertIsNone(fetch_fresh_domestic_balance(None))

    def test_fetch_fresh_domestic_balance_returns_none_when_total_evaluation_missing(self) -> None:
        kis = self.FakeBalanceKis({"output1": [{"pdno": "005930", "hldg_qty": "10", "pchs_avg_pric": "65000", "evlu_amt": "700000"}], "output2": [{}]})
        self.assertIsNone(fetch_fresh_domestic_balance(kis))

    def test_fetch_fresh_domestic_balance_parses_symbols_and_total(self) -> None:
        kis = self.FakeBalanceKis(
            {
                "output1": [{"pdno": "005930", "hldg_qty": "10", "pchs_avg_pric": "65000", "evlu_amt": "700000"}],
                "output2": [{"tot_evlu_amt": "10000000"}],
            }
        )
        fresh = fetch_fresh_domestic_balance(kis)
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh["total_evaluation_amount"], 10000000)
        self.assertEqual(fresh["symbols"]["005930"]["quantity"], 10)
        self.assertEqual(fresh["symbols"]["005930"]["average_purchase_price"], 65000)

    def test_fetch_fresh_domestic_balance_collects_every_page(self) -> None:
        class PaginatedBalanceKis:
            cano = "12345678"
            product = "01"

            def __init__(self) -> None:
                self.calls: list[tuple[dict[str, str], str]] = []

            def call_with_headers(
                self,
                name: str,
                *,
                params: dict[str, str] | None = None,
                payload: dict[str, Any] | None = None,
                tr_cont: str = "",
            ) -> tuple[dict[str, Any], dict[str, str]]:
                self.calls.append((dict(params or {}), tr_cont))
                if len(self.calls) == 1:
                    return (
                        {
                            "output1": [
                                {
                                    "pdno": "005930",
                                    "hldg_qty": "10",
                                    "pchs_avg_pric": "65000",
                                    "evlu_amt": "700000",
                                }
                            ],
                            "output2": [
                                {
                                    "tot_evlu_amt": "10000000",
                                    "ctx_area_fk100": "next-fk",
                                    "ctx_area_nk100": "next-nk",
                                }
                            ],
                        },
                        {"tr_cont": "F"},
                    )
                return (
                    {
                        "output1": [
                            {
                                "pdno": "000660",
                                "hldg_qty": "3",
                                "pchs_avg_pric": "180000",
                                "evlu_amt": "600000",
                            }
                        ],
                        "output2": [],
                    },
                    {"tr_cont": ""},
                )

        kis = PaginatedBalanceKis()
        fresh = fetch_fresh_domestic_balance(kis)
        self.assertIsNotNone(fresh)
        self.assertEqual(set(fresh["symbols"]), {"005930", "000660"})
        self.assertEqual(len(kis.calls), 2)
        self.assertEqual(kis.calls[1][0]["CTX_AREA_FK100"], "next-fk")
        self.assertEqual(kis.calls[1][0]["CTX_AREA_NK100"], "next-nk")
        self.assertEqual(kis.calls[1][1], "N")

    def test_fetch_fresh_domestic_balance_fails_closed_on_unpageable_continuation(self) -> None:
        class UnpageableBalanceKis:
            cano = "12345678"
            product = "01"

            def call_with_headers(
                self,
                name: str,
                *,
                params: dict[str, str] | None = None,
                payload: dict[str, Any] | None = None,
                tr_cont: str = "",
            ) -> tuple[dict[str, Any], dict[str, str]]:
                return (
                    {
                        "output1": [],
                        "output2": [{"tot_evlu_amt": "10000000"}],
                    },
                    {"tr_cont": "F"},
                )

        self.assertIsNone(fetch_fresh_domestic_balance(UnpageableBalanceKis()))

    def test_verify_profit_protection_pnl_blocks_when_fresh_balance_unavailable(self) -> None:
        kis = self.FakeBalanceKis({"output1": [], "output2": [{"tot_evlu_amt": "1"}]})
        self.assertFalse(verify_profit_protection_pnl(kis, None, "005930"))

    def test_verify_profit_protection_pnl_blocks_when_symbol_missing_from_fresh_balance(self) -> None:
        class CurrentPriceKis:
            def current_price(self, symbol: str) -> int:
                return 80000

        fresh_balance = {"symbols": {}, "total_evaluation_amount": 10_000_000}
        self.assertFalse(verify_profit_protection_pnl(CurrentPriceKis(), fresh_balance, "005930"))

    def test_verify_profit_protection_pnl_allows_when_current_price_above_fresh_average_cost(self) -> None:
        class CurrentPriceKis:
            def current_price(self, symbol: str) -> int:
                return 80000

        fresh_balance = {"symbols": {"005930": {"average_purchase_price": 65000}}, "total_evaluation_amount": 10_000_000}
        self.assertTrue(verify_profit_protection_pnl(CurrentPriceKis(), fresh_balance, "005930"))

    def test_verify_concentration_rebalance_blocks_when_fresh_concentration_now_within_cap(self) -> None:
        # The pipeline's guard was derived from a stale, above-cap snapshot; the fresh
        # snapshot shows the position has already fallen back within the approved cap.
        fresh_balance = {
            "symbols": {"005930": {"quantity": 10, "valuation_amount": 1_000_000}},  # 10% of total
            "total_evaluation_amount": 10_000_000,
        }
        order = {
            "order_price": 100_000,
            "validated_order_quantity": 2,
            "decision_guard": {"cap_pct": 15.0, "max_reduction_pct": 25.0},
        }
        self.assertFalse(verify_concentration_rebalance(fresh_balance, "005930", order))

    def test_verify_concentration_rebalance_blocks_when_reduction_would_cross_below_cap_floor(self) -> None:
        fresh_balance = {
            "symbols": {"005930": {"quantity": 10, "valuation_amount": 2_000_000}},  # 20% of total, above 15% cap
            "total_evaluation_amount": 10_000_000,
        }
        # Cap floor at 15% of 10,000,000 / 100,000 price = 15 shares; already below that at
        # qty 10, so any further reduction should still be checked against the floor.
        order = {
            "order_price": 100_000,
            "validated_order_quantity": 10,
            "decision_guard": {"cap_pct": 15.0, "max_reduction_pct": 25.0},
        }
        self.assertFalse(verify_concentration_rebalance(fresh_balance, "005930", order))

    def test_verify_concentration_rebalance_allows_reduction_within_cap_floor(self) -> None:
        fresh_balance = {
            "symbols": {"005930": {"quantity": 20, "valuation_amount": 2_000_000}},  # 20% of total, above 15% cap
            "total_evaluation_amount": 10_000_000,
        }
        # Cap floor = 15% of 10,000,000 / 100,000 = 15 shares; reducing 2 shares leaves 18, still above floor.
        order = {
            "order_price": 100_000,
            "validated_order_quantity": 2,
            "decision_guard": {"cap_pct": 15.0, "max_reduction_pct": 25.0},
        }
        self.assertTrue(verify_concentration_rebalance(fresh_balance, "005930", order))

    def test_verify_concentration_rebalance_uses_fresh_valuation_not_stale_order_price(self) -> None:
        fresh_balance = {
            "symbols": {"005930": {"quantity": 20, "valuation_amount": 1_800_000}},
            "total_evaluation_amount": 10_000_000,
        }
        # Fresh implied price is 90,000 and the 15% floor is 1,500,000.
        # Selling 4 leaves 1,440,000, below the floor. A stale 200,000
        # order_price would incorrectly make the old quantity-floor check pass.
        order = {
            "order_price": 200_000,
            "validated_order_quantity": 4,
            "decision_guard": {"cap_pct": 15.0, "max_reduction_pct": 25.0},
        }
        self.assertFalse(verify_concentration_rebalance(fresh_balance, "005930", order))

    def test_verify_concentration_rebalance_includes_existing_pending_sell(self) -> None:
        fresh_balance = {
            "symbols": {"005930": {"quantity": 3, "valuation_amount": 300_000}},
            "total_evaluation_amount": 1_000_000,
        }
        # One pending sell plus one additional sell would leave 1 share
        # (100,000, or 10%), below the approved 15% floor.
        order = {
            "validated_order_quantity": 1,
            "pending_and_reserved_sell_quantity": 1,
            "decision_guard": {"cap_pct": 15.0, "max_reduction_pct": 25.0},
        }
        self.assertFalse(verify_concentration_rebalance(fresh_balance, "005930", order))

    def test_verify_fresh_reduction_bounds_counts_pending_and_new_sell(self) -> None:
        fresh_balance = {
            "symbols": {"005930": {"quantity": 8}},
            "total_evaluation_amount": 10_000_000,
        }
        allowed = {
            "validated_order_quantity": 1,
            "pending_and_reserved_sell_quantity": 1,
            "decision_guard": {"max_reduction_pct": 25.0},
        }
        excessive = {
            **allowed,
            "validated_order_quantity": 2,
        }
        self.assertTrue(verify_fresh_reduction_bounds(fresh_balance, "005930", allowed))
        self.assertFalse(verify_fresh_reduction_bounds(fresh_balance, "005930", excessive))

    def test_reconcile_uses_fresh_quantity_after_pending_sell_fills(self) -> None:
        class CurrentPriceKis:
            def current_price(self, symbol: str) -> int:
                return 80_000

        execution = {
            "orders": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "final_holding_quantity": 6,
                    "order_price": 70_000,
                    "order_path": "immediate",
                    "decision_basis": "profit_protection",
                    "decision_guard": {
                        "status": "allowed",
                        "canonical_action": "reduce",
                        "basis": "profit_protection",
                        "max_reduction_pct": 25.0,
                    },
                }
            ]
        }
        fresh_balance = {
            "symbols": {
                "005930": {
                    "quantity": 8,
                    "average_purchase_price": 65_000,
                    "valuation_amount": 640_000,
                }
            },
            "total_evaluation_amount": 10_000_000,
        }

        reconcile(
            {
                "account_summary": {"cash_amount": 1_000_000},
                "symbols": [
                    {
                        "symbol_id": "005930",
                        "symbol_name": "삼성전자",
                        # Stale snapshot from before a pending 2-share sell filled.
                        "current_live_holding_quantity": 10,
                    }
                ],
            },
            execution,
            [],
            {},
            {"005930": {"max_sell_qty": 8}},
            submit=False,
            kis=CurrentPriceKis(),
            fresh_balance=fresh_balance,
        )

        order = execution["orders"][0]
        self.assertEqual(order.get("current_live_holding_quantity"), 8)
        self.assertEqual(order.get("validated_order_quantity"), 2)
        self.assertEqual(order.get("reason"), "validated_dry_run_not_submitted")

    def test_reconcile_blocks_fresh_reduction_above_approved_percentage(self) -> None:
        class CurrentPriceKis:
            def current_price(self, symbol: str) -> int:
                return 80_000

        execution = {
            "orders": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "final_holding_quantity": 8,
                    "order_price": 70_000,
                    "order_path": "immediate",
                    "decision_basis": "profit_protection",
                    "decision_guard": {
                        "status": "allowed",
                        "canonical_action": "reduce",
                        "basis": "profit_protection",
                        "max_reduction_pct": 25.0,
                    },
                }
            ]
        }
        fresh_balance = {
            "symbols": {
                "005930": {
                    "quantity": 12,
                    "average_purchase_price": 65_000,
                    "valuation_amount": 960_000,
                }
            },
            "total_evaluation_amount": 10_000_000,
        }

        reconcile(
            {
                "account_summary": {"cash_amount": 1_000_000},
                "symbols": [
                    {
                        "symbol_id": "005930",
                        "symbol_name": "삼성전자",
                        "current_live_holding_quantity": 10,
                    }
                ],
            },
            execution,
            [],
            {},
            {"005930": {"max_sell_qty": 12}},
            submit=False,
            kis=CurrentPriceKis(),
            fresh_balance=fresh_balance,
        )

        order = execution["orders"][0]
        self.assertEqual(order.get("current_live_holding_quantity"), 12)
        self.assertEqual(order.get("result"), "blocked")
        self.assertEqual(
            order.get("reason"),
            "profit_protection_reduction_bound_recheck_failed",
        )

    def test_reconcile_blocks_profit_protection_sell_when_fresh_balance_unavailable(self) -> None:
        execution = {
            "orders": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "final_holding_quantity": 8,
                    "order_price": 70000,
                    "order_path": "immediate",
                    "decision_basis": "profit_protection",
                    "decision_guard": {
                        "status": "allowed",
                        "canonical_action": "reduce",
                        "basis": "profit_protection",
                        "max_reduction_pct": 25.0,
                    },
                }
            ]
        }
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 10}]},
            execution,
            [],
            {},
            {"005930": {"max_sell_qty": 5}},
            submit=False,
            kis=self.FakeBalanceKis(raise_error=True),
            fresh_balance=None,
        )
        order = execution["orders"][0]
        self.assertEqual(order.get("result"), "blocked")
        self.assertEqual(order.get("reason"), "profit_protection_pnl_recheck_failed")
        # A failed recheck must still persist a compact sanitized audit entry, not only successes.
        audit = order.get("fresh_recheck_audit")
        self.assertEqual(len(audit), 1)
        self.assertIn("checked_at", audit[0])
        self.assertFalse(audit[0]["pnl_verification_outcome"])

    def test_reconcile_persists_fresh_recheck_audit_on_a_successful_profit_protection_sell(self) -> None:
        class CurrentPriceKis:
            def current_price(self, symbol: str) -> int:
                return 80_000

        execution = {
            "orders": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "final_holding_quantity": 8,
                    "order_price": 70000,
                    "order_path": "immediate",
                    "decision_basis": "profit_protection",
                    "decision_guard": {
                        "status": "allowed",
                        "canonical_action": "reduce",
                        "basis": "profit_protection",
                        "max_reduction_pct": 25.0,
                    },
                }
            ]
        }
        reconcile(
            {"account_summary": {"cash_amount": 1_000_000}, "symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 10}]},
            execution,
            [],
            {},
            {"005930": {"max_sell_qty": 5}},
            submit=False,
            kis=CurrentPriceKis(),
            fresh_balance={
                "symbols": {"005930": {"quantity": 10, "average_purchase_price": 65000, "valuation_amount": 700000}},
                "total_evaluation_amount": 10_000_000,
            },
        )
        order = execution["orders"][0]
        # A successful recheck must ALSO be audited -- the pre-existing code only persisted failures.
        self.assertNotEqual(order.get("reason"), "profit_protection_pnl_recheck_failed")
        self.assertNotEqual(order.get("reason"), "profit_protection_reduction_bound_recheck_failed")
        audit = order.get("fresh_recheck_audit")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["fresh_holding_quantity"], 10)
        self.assertTrue(audit[0]["pnl_verification_outcome"])
        self.assertTrue(audit[0]["reduction_bound_outcome"])
        self.assertEqual(audit[0]["approved_max_reduction_pct"], 25.0)

    def test_reconcile_blocks_every_duplicate_execution_symbol(self) -> None:
        duplicate_order = {
            "symbol_id": "005930",
            "symbol_name": "삼성전자",
            "final_holding_quantity": 1,
            "order_price": 70_000,
            "order_path": "immediate",
            "decision_basis": "thesis",
            "decision_guard": {
                "status": "allowed",
                "canonical_action": "increase",
                "basis": "thesis",
            },
        }
        execution = {"orders": [dict(duplicate_order), dict(duplicate_order)]}

        reconcile(
            {
                "account_summary": {"cash_amount": 1_000_000},
                "symbols": [
                    {
                        "symbol_id": "005930",
                        "current_live_holding_quantity": 0,
                    }
                ],
            },
            execution,
            [],
            {"005930": {"max_buy_qty": 10, "max_buy_amt": 1_000_000}},
            {},
            submit=False,
            kis=None,
        )

        self.assertEqual(
            [order.get("reason") for order in execution["orders"]],
            ["duplicate_execution_symbol", "duplicate_execution_symbol"],
        )
