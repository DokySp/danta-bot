"""Tests for deferred buy retry scheduling and execution."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ..pipeline import (
    Config,
    cash_gate_reason,
    enqueue_deferred_buy_retries,
    execute_orders,
    load_deferred_buy_retry_config,
    parse_time,
    retry_quantity,
)


def self_test() -> None:
    blocked = {
        "direction": "buy",
        "result": "blocked",
        "requested_order_quantity": 3,
        "validated_order_quantity": 3,
        "reason": "buy_quantity_exceeds_order_available_quantity",
    }
    adjusted = {
        "direction": "buy",
        "result": "submitted",
        "requested_order_quantity": 5,
        "validated_order_quantity": 2,
        "quantity_adjustment": {"to": 2, "reason": "buy_quantity_reduced_to_remaining_cash"},
    }
    assert retry_quantity(blocked) == 3
    assert retry_quantity(adjusted) == 3
    submitted_adjusted = {
        "direction": "buy",
        "result": "submitted",
        "requested_order_quantity": 5,
        "validated_order_quantity": 2,
        "reason": "cash_order_submitted",
        "quantity_adjustment": {"to": 2, "reason": "buy_quantity_reduced_to_remaining_cash"},
    }
    assert cash_gate_reason(adjusted) == "buy_quantity_reduced_to_remaining_cash"
    assert cash_gate_reason(submitted_adjusted) == "buy_quantity_reduced_to_remaining_cash"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        previous_portfolio_except_env = os.environ.get(execute_orders.PORTFOLIO_EXCEPT_ENV_VAR)
        os.environ[execute_orders.PORTFOLIO_EXCEPT_ENV_VAR] = str(tmp_path / "portfolio-except.txt")
        path = tmp_path / "deferred-buy-retry.yaml"
        path.write_text(
            "deferred_buy_retry:\n"
            "  enabled: true\n"
            "  delay_seconds: 120\n"
            "  expires_after_seconds: 600\n"
            "  slippage_bps: 25\n",
            encoding="utf-8",
        )
        config = Config(
            host="",
            port=0,
            codex_bin="",
            codex_home=Path(tmp),
            state_dir=Path(tmp),
            workspace_dir=Path(tmp),
            schedule_file=Path(tmp) / "schedules.yaml",
            price_trigger_file=Path(tmp) / "touch-points.yaml",
            deferred_buy_retry_config_file=path,
            telegram_gateway_url="",
            telegram_route=None,
            mcp_trading_env="paper",
            codex_runtime_config_file=Path(tmp) / "codex-runtime.yaml",
            codex_timeout_seconds=0,
            scheduler_poll_seconds=0,
            telegram_typing_interval_seconds=0,
            bypass_sandbox=True,
            usage_script=Path(tmp) / "usage.py",
            usage_timeout_seconds=0,
            bundled_skills_dir=Path(tmp),
            sync_skills_overwrite=False,
            portfolio_file=Path(tmp) / "portfolio.txt",
            portfolio_except_file=Path(tmp) / "portfolio-except.txt",
        )
        loaded = load_deferred_buy_retry_config(config)
        assert loaded.enabled is True
        assert loaded.delay_seconds == 120
        assert loaded.expires_after_seconds == 600
        assert loaded.slippage_bps == 25
        run_dir = tmp_path / "reports" / "runs" / "run-1"
        execute_orders.write_json(
            run_dir / "execution.json",
            {
                "execution_environment": "real",
                "orders": [
                    {"direction": "sell", "result": "submitted", "symbol_id": "005930"},
                    {
                        "direction": "buy",
                        "result": "submitted",
                        "symbol_id": "000660",
                        "symbol_name": "SK hynix",
                        "requested_order_quantity": 5,
                        "validated_order_quantity": 2,
                        "final_holding_quantity": 10,
                        "order_price": 10000,
                        "quantity_adjustment": {"to": 2, "reason": "buy_quantity_reduced_to_remaining_cash"},
                    },
                ],
            },
        )
        created = enqueue_deferred_buy_retries(
            workspace_dir=tmp_path,
            source_run_dir=run_dir,
            delay_seconds=loaded.delay_seconds,
            expires_after_seconds=loaded.expires_after_seconds,
            slippage_bps=loaded.slippage_bps,
        )
        assert len(created) == 1
        artifact = execute_orders.load_json(created[0])
        assert artifact["retry_quantity"] == 3
        assert artifact["slippage_bps"] == 25
        assert artifact["max_acceptable_price"] == 10030
        due_at = parse_time(artifact["due_at"])
        expires_at = parse_time(artifact["expires_at"])
        assert due_at is not None and expires_at is not None
        assert int((expires_at - due_at).total_seconds()) == 600
        (tmp_path / "portfolio-except.txt").write_text("000660\n", encoding="utf-8")
        skipped = enqueue_deferred_buy_retries(
            workspace_dir=tmp_path / "excluded-check",
            source_run_dir=run_dir,
            delay_seconds=loaded.delay_seconds,
            expires_after_seconds=loaded.expires_after_seconds,
            slippage_bps=loaded.slippage_bps,
        )
        assert skipped == []
        (tmp_path / "portfolio-except.txt").unlink()
        path.write_text("deferred_buy_retry:\n  enabled: false\n", encoding="utf-8")
        disabled = load_deferred_buy_retry_config(config)
        assert disabled.enabled is False
        if previous_portfolio_except_env is None:
            os.environ.pop(execute_orders.PORTFOLIO_EXCEPT_ENV_VAR, None)
        else:
            os.environ[execute_orders.PORTFOLIO_EXCEPT_ENV_VAR] = previous_portfolio_except_env


class DeferredBuyRetrySelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        self_test()
