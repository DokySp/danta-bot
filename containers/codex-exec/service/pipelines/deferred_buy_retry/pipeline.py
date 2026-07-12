from __future__ import annotations

import argparse
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from ...config import Config
from ...telegram.gateway import TelegramGateway
from ..daily_trading.scripts import collect_main_evidence as evidence
from ..daily_trading.scripts import execute_orders


CASH_GATE_REASONS = {
    "buy_quantity_exceeds_order_available_quantity",
    "buy_cash_gate_reduced_reverse_rank",
    "buy_quantity_reduced_to_remaining_cash",
}
DEFAULT_DELAY_SECONDS = 300
DEFAULT_EXPIRES_AFTER_SECONDS = 900
DEFAULT_SLIPPAGE_BPS = 50


@dataclass(frozen=True)
class DeferredBuyRetryConfig:
    enabled: bool
    delay_seconds: int
    expires_after_seconds: int
    slippage_bps: int


def bool_value(raw: Any, *, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"expected boolean value, got {raw!r}")


def non_negative_int(raw: Any, *, name: str, default: int) -> int:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def load_deferred_buy_retry_config(config: Config) -> DeferredBuyRetryConfig:
    path = config.deferred_buy_retry_config_file
    if not path.exists():
        return DeferredBuyRetryConfig(
            enabled=True,
            delay_seconds=DEFAULT_DELAY_SECONDS,
            expires_after_seconds=DEFAULT_EXPIRES_AFTER_SECONDS,
            slippage_bps=DEFAULT_SLIPPAGE_BPS,
        )
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"deferred buy retry config must be a mapping: {path}")
    section = payload.get("deferred_buy_retry", payload)
    if not isinstance(section, dict):
        raise ValueError(f"deferred_buy_retry config must be a mapping: {path}")
    return DeferredBuyRetryConfig(
        enabled=bool_value(section.get("enabled"), default=True),
        delay_seconds=non_negative_int(section.get("delay_seconds"), name="deferred_buy_retry.delay_seconds", default=DEFAULT_DELAY_SECONDS),
        expires_after_seconds=non_negative_int(
            section.get("expires_after_seconds"),
            name="deferred_buy_retry.expires_after_seconds",
            default=DEFAULT_EXPIRES_AFTER_SECONDS,
        ),
        slippage_bps=non_negative_int(section.get("slippage_bps"), name="deferred_buy_retry.slippage_bps", default=DEFAULT_SLIPPAGE_BPS),
    )


def queue_dir(workspace_dir: Path) -> Path:
    return workspace_dir / "memory" / "deferred-buy-retry"


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_at(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def order_reasons(order: dict[str, Any]) -> set[str]:
    reasons: set[str] = set()
    reason = str(order.get("reason") or "").strip()
    if reason:
        reasons.add(reason)
    adjustment = order.get("quantity_adjustment")
    if isinstance(adjustment, dict):
        adjustment_reason = str(adjustment.get("reason") or "").strip()
        if adjustment_reason:
            reasons.add(adjustment_reason)
    return reasons


def cash_gate_reason(order: dict[str, Any]) -> str:
    for reason in order_reasons(order):
        if reason in CASH_GATE_REASONS:
            return reason
    return ""


def retry_quantity(order: dict[str, Any]) -> int:
    requested = execute_orders.as_int(order.get("requested_order_quantity"), execute_orders.as_int(order.get("validated_order_quantity")))
    validated = execute_orders.as_int(order.get("validated_order_quantity"))
    if str(order.get("result") or "").strip() == "blocked":
        return max(requested, validated)
    adjustment = order.get("quantity_adjustment")
    if isinstance(adjustment, dict):
        adjusted_to = execute_orders.as_int(adjustment.get("to"), validated)
        return max(0, requested - adjusted_to)
    return max(0, requested - validated)


def target_quantity(order: dict[str, Any]) -> int:
    return execute_orders.as_int(order.get("final_holding_quantity"), execute_orders.as_int(order.get("target_holding_quantity")))


def source_run_id(source_run_dir: Path) -> str:
    return source_run_dir.name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def artifact_path(workspace_dir: Path, source_run_dir: Path, symbol: str) -> Path:
    return queue_dir(workspace_dir) / f"{source_run_id(source_run_dir)}--{symbol}.json"


def max_acceptable_price(order_price: int, slippage_bps: int) -> int:
    raw = int(math.ceil(order_price * (10_000 + slippage_bps) / 10_000))
    return execute_orders.normalize_limit_price(raw, "buy")


def enqueue_deferred_buy_retries(
    *,
    workspace_dir: Path,
    source_run_dir: Path,
    chat_id: str | None = None,
    route: str | None = None,
    delay_seconds: int = DEFAULT_DELAY_SECONDS,
    expires_after_seconds: int = DEFAULT_EXPIRES_AFTER_SECONDS,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
) -> list[Path]:
    execution_path = source_run_dir / "execution.json"
    if not execution_path.is_file():
        return []
    execution = execute_orders.load_json(execution_path)
    orders = execution.get("orders") if isinstance(execution, dict) else None
    if not isinstance(orders, list):
        return []
    has_submitted_sell = any(
        isinstance(item, dict)
        and str(item.get("direction") or "").strip() == "sell"
        and str(item.get("result") or "").strip() == "submitted"
        for item in orders
    )
    if not has_submitted_sell:
        return []

    now = datetime.now(timezone.utc)
    due_at = now + timedelta(seconds=max(0, delay_seconds))
    expires_at = due_at + timedelta(seconds=max(0, expires_after_seconds))
    created: list[Path] = []
    env = str(execution.get("execution_environment") or "").strip() if isinstance(execution, dict) else ""
    portfolio_except = execute_orders.load_portfolio_except_symbols()
    for order in orders:
        if not isinstance(order, dict) or str(order.get("direction") or "").strip() != "buy":
            continue
        reason = cash_gate_reason(order)
        if not reason:
            continue
        symbol = execute_orders.symbol_key(order)
        if symbol in portfolio_except:
            continue
        qty = retry_quantity(order)
        target = target_quantity(order)
        price = execute_orders.as_price(order.get("order_price"))
        if not symbol or qty <= 0 or target <= 0 or price <= 0:
            continue
        payload = {
            "version": 1,
            "state": "pending",
            "source_run_id": source_run_id(source_run_dir),
            "source_run_dir": str(source_run_dir),
            "source_execution_path": str(execution_path),
            "created_at": iso_at(now),
            "due_at": iso_at(due_at),
            "expires_at": iso_at(expires_at),
            "retry_reason": reason,
            "symbol_id": symbol,
            "symbol_name": str(order.get("symbol_name") or symbol),
            "retry_quantity": qty,
            "target_holding_quantity": target,
            "final_holding_quantity": target,
            "original_order_price": price,
            "max_acceptable_price": max_acceptable_price(price, slippage_bps),
            "slippage_bps": slippage_bps,
            "execution_environment": env,
            "chat_id": chat_id or "",
            "route": route or "",
            "attempt_count": 0,
        }
        path = artifact_path(workspace_dir, source_run_dir, symbol)
        execute_orders.write_json(path, payload)
        created.append(path)
    return created


def pending_artifacts(workspace_dir: Path, *, now: datetime | None = None) -> list[Path]:
    current = now or datetime.now(timezone.utc)
    paths: list[Path] = []
    root = queue_dir(workspace_dir)
    if not root.is_dir():
        return paths
    for path in sorted(root.glob("*.json")):
        try:
            payload = execute_orders.load_json(path)
        except Exception:
            logging.exception("failed to read deferred buy retry artifact path=%s", path)
            continue
        if not isinstance(payload, dict):
            continue
        expires_at = parse_time(payload.get("expires_at"))
        if payload.get("state") == "running" and expires_at is not None and expires_at <= current:
            mark_terminal(path, payload, state="expired", reason="running_retry_window_expired")
            continue
        if payload.get("state") != "pending":
            continue
        due_at = parse_time(payload.get("due_at"))
        if due_at is not None and due_at <= current:
            paths.append(path)
    return paths


def mark_terminal(path: Path, payload: dict[str, Any], *, state: str, reason: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload["state"] = state
    payload["terminal_reason"] = reason
    payload["completed_at"] = iso_at(datetime.now(timezone.utc))
    if extra:
        payload.update(extra)
    execute_orders.write_json(path, payload)
    return payload


def current_holding_quantity(kis: execute_orders.Kis, symbol: str) -> tuple[int, dict[str, Any]]:
    rows, _summary, errors = evidence.fetch_account_balance(
        env_dv=kis.env,
        app_key=kis.app_key,
        app_secret=kis.app_secret,
        token=kis.token,
        retries=kis.retries,
        max_pages=10,
    )
    if errors:
        raise RuntimeError(str(errors[0].get("message") or errors[0].get("code") or "inquire_balance_failed"))
    observed_at = execute_orders.now_iso()
    for row in rows:
        holding = evidence.normalize_holding(row, observed_at=observed_at)
        if execute_orders.symbol_key(holding.get("symbol_id")) == symbol:
            return execute_orders.as_int(holding.get("current_live_holding_quantity")), holding
    return 0, {"symbol_id": symbol, "current_live_holding_quantity": 0, "observed_at": observed_at}


def current_price(kis: execute_orders.Kis, symbol: str) -> int:
    body, _headers = evidence.call_endpoint(
        "inquire_price",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
        },
        kis.app_key,
        kis.app_secret,
        kis.token,
        kis.retries,
        env_dv=kis.env,
    )
    row = evidence.output_first(body, "output")
    return evidence.parse_int(evidence.first_present(row, ("stck_prpr", "thdt_clpr", "stck_prdy_clpr", "price"))) or 0


def active_for_symbol(kis: execute_orders.Kis, symbol: str) -> list[dict[str, Any]]:
    start_date, end_date = execute_orders.default_date_range()
    active = execute_orders.fetch_reservations(kis, start_date, end_date) + execute_orders.fetch_pending_orders(kis)
    return [
        item
        for item in active
        if item.get("active_status") == "active" and execute_orders.symbol_key(item) == symbol
    ]


def process_artifact(path: Path, config: Config) -> dict[str, Any]:
    payload = execute_orders.load_json(path)
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid deferred buy retry artifact: {path}")
    if payload.get("state") != "pending":
        return payload
    now = datetime.now(timezone.utc)
    expires_at = parse_time(payload.get("expires_at"))
    if expires_at is not None and now > expires_at:
        return mark_terminal(path, payload, state="expired", reason="retry_window_expired")

    payload["state"] = "running"
    payload["attempt_count"] = execute_orders.as_int(payload.get("attempt_count")) + 1
    payload["attempted_at"] = iso_at(now)
    execute_orders.write_json(path, payload)

    symbol = execute_orders.symbol_key(payload)
    if symbol in execute_orders.load_portfolio_except_symbols():
        return mark_terminal(path, payload, state="dropped", reason="symbol_in_portfolio_except_list")
    target = execute_orders.as_int(payload.get("target_holding_quantity"))
    requested_qty = execute_orders.as_int(payload.get("retry_quantity"))
    env = execute_orders.env_dv(str(payload.get("execution_environment") or config.mcp_trading_env))
    kis = execute_orders.Kis(env, retries=2)

    current_qty, holding = current_holding_quantity(kis, symbol)
    active_orders = active_for_symbol(kis, symbol)
    if active_orders:
        return mark_terminal(
            path,
            payload,
            state="dropped",
            reason="active_order_conflict",
            extra={"refreshed_holding": holding, "active_orders": active_orders},
        )
    deficit = max(0, target - current_qty)
    if deficit <= 0:
        return mark_terminal(
            path,
            payload,
            state="dropped",
            reason="target_holding_already_reached",
            extra={"refreshed_holding": holding, "refreshed_expected_holding_quantity": current_qty},
        )

    price = current_price(kis, symbol)
    max_price = execute_orders.as_price(payload.get("max_acceptable_price"))
    if price <= 0:
        return mark_terminal(path, payload, state="dropped", reason="current_price_unavailable")
    order_price = execute_orders.normalize_limit_price(price, "buy")
    if max_price > 0 and order_price > max_price:
        return mark_terminal(
            path,
            payload,
            state="dropped",
            reason="price_outside_allowed_range",
            extra={"current_price": price, "order_price": order_price, "max_acceptable_price": max_price},
        )

    capacity = execute_orders.buy_capacity(kis, symbol, order_price)
    max_qty = execute_orders.as_int(capacity.get("max_buy_qty"))
    max_amt = execute_orders.as_int(capacity.get("max_buy_amt"))
    cash_qty = max_amt // order_price if max_amt > 0 and order_price > 0 else 0
    qty = min(requested_qty, deficit, max_qty, cash_qty)
    if qty <= 0:
        return mark_terminal(
            path,
            payload,
            state="dropped",
            reason="buy_capacity_still_insufficient",
            extra={
                "current_price": price,
                "order_price": order_price,
                "buy_capacity": capacity,
                "refreshed_holding": holding,
                "refreshed_expected_holding_quantity": current_qty,
            },
        )

    order = {
        "symbol_id": symbol,
        "symbol_name": str(payload.get("symbol_name") or symbol),
        "direction": "buy",
        "validated_order_quantity": qty,
        "order_price": order_price,
        "order_path": "immediate",
        "order_api": "order_cash",
    }
    order_id = execute_orders.submit_order(kis, order)
    return mark_terminal(
        path,
        payload,
        state="submitted",
        reason="retry_order_submitted",
        extra={
            "submitted_quantity": qty,
            "order_or_reservation_id": order_id,
            "current_price": price,
            "order_price": order_price,
            "buy_capacity": capacity,
            "refreshed_holding": holding,
            "refreshed_expected_holding_quantity": current_qty,
        },
    )


def format_result(payload: dict[str, Any]) -> str:
    symbol = str(payload.get("symbol_name") or payload.get("symbol_id") or "").strip()
    state = str(payload.get("state") or "").strip()
    reason = str(payload.get("terminal_reason") or "").strip()
    qty = execute_orders.as_int(payload.get("submitted_quantity"))
    if state == "submitted":
        return f"<b>후속 매수 재시도 제출</b>\n<code>{symbol}</code> {qty}주\n<code>{reason}</code>"
    return f"<b>후속 매수 재시도 드롭</b>\n<code>{symbol}</code>\n<code>{reason or state}</code>"


def run_due_deferred_buy_retries(config: Config, gateway: TelegramGateway | None = None) -> list[dict[str, Any]]:
    retry_config = load_deferred_buy_retry_config(config)
    if not retry_config.enabled:
        return []
    results: list[dict[str, Any]] = []
    for path in pending_artifacts(config.workspace_dir):
        try:
            result = process_artifact(path, config)
        except Exception as exc:  # noqa: BLE001 - make the one-shot terminal and visible
            logging.exception("deferred buy retry failed path=%s", path)
            payload = execute_orders.load_json(path)
            if isinstance(payload, dict):
                result = mark_terminal(path, payload, state="dropped", reason="retry_failed", extra={"error": str(exc)})
            else:
                continue
        results.append(result)
        if gateway is not None:
            chat_id = str(result.get("chat_id") or "").strip() or None
            route = str(result.get("route") or "").strip() or None
            gateway.send_message(format_result(result), chat_id, route)
    return results


def self_test() -> None:
    """Run the extracted test suite through the legacy CLI contract."""
    from .tests.test_pipeline import self_test as run_external_self_test

    run_external_self_test()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["self-test"])
    args = parser.parse_args()
    if args.command == "self-test":
        self_test()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
