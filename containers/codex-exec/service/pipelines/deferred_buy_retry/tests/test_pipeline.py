"""Tests for deferred buy retry scheduling and execution.

`self_test` is the compatibility body invoked by the production
`pipeline.py self-test` command and must keep its behavior: run without
raising, return `None`. Each logical block of the old monolithic
self-test now lives in its own `scenario_*` (setup + act) or `check_*`
(single assertion concern, reusable from a plain function or a
`TestCase` method) helper. `self_test` and the granular `TestCase`
methods below both call those helpers, so each behavior has exactly one
implementation. The wrapper-orchestration test mocks the helpers rather
than re-running every scenario, so discovery does not execute the real
work twice.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ..pipeline import (
    Config,
    cash_gate_reason,
    enqueue_deferred_buy_retries,
    execute_orders,
    load_deferred_buy_retry_config,
    parse_time,
    retry_quantity,
)

BLOCKED_ORDER = {
    "direction": "buy",
    "result": "blocked",
    "requested_order_quantity": 3,
    "validated_order_quantity": 3,
    "reason": "buy_quantity_exceeds_order_available_quantity",
}
ADJUSTED_ORDER = {
    "direction": "buy",
    "result": "submitted",
    "requested_order_quantity": 5,
    "validated_order_quantity": 2,
    "quantity_adjustment": {"to": 2, "reason": "buy_quantity_reduced_to_remaining_cash"},
}
SUBMITTED_ADJUSTED_ORDER = {
    "direction": "buy",
    "result": "submitted",
    "requested_order_quantity": 5,
    "validated_order_quantity": 2,
    "reason": "cash_order_submitted",
    "quantity_adjustment": {"to": 2, "reason": "buy_quantity_reduced_to_remaining_cash"},
}


def check_retry_quantity_uses_requested_when_blocked() -> None:
    if retry_quantity(BLOCKED_ORDER) != 3:
        raise AssertionError(f"expected blocked order retry_quantity 3, got: {retry_quantity(BLOCKED_ORDER)}")


def check_retry_quantity_uses_requested_minus_adjusted_to_when_not_blocked() -> None:
    if retry_quantity(ADJUSTED_ORDER) != 3:
        raise AssertionError(f"expected adjusted order retry_quantity 3, got: {retry_quantity(ADJUSTED_ORDER)}")


def check_cash_gate_reason_reads_quantity_adjustment_reason() -> None:
    if cash_gate_reason(ADJUSTED_ORDER) != "buy_quantity_reduced_to_remaining_cash":
        raise AssertionError(f"unexpected cash_gate_reason: {cash_gate_reason(ADJUSTED_ORDER)}")


def check_cash_gate_reason_prefers_quantity_adjustment_over_top_level_reason() -> None:
    reason = cash_gate_reason(SUBMITTED_ADJUSTED_ORDER)
    if reason != "buy_quantity_reduced_to_remaining_cash":
        raise AssertionError(f"unexpected cash_gate_reason with top-level reason present: {reason}")


def make_config(tmp_path: Path, retry_config_path: Path) -> Config:
    return Config(
        host="",
        port=0,
        codex_bin="",
        codex_home=tmp_path,
        state_dir=tmp_path,
        workspace_dir=tmp_path,
        schedule_file=tmp_path / "schedules.yaml",
        price_trigger_file=tmp_path / "touch-points.yaml",
        deferred_buy_retry_config_file=retry_config_path,
        telegram_gateway_url="",
        telegram_route=None,
        mcp_trading_env="paper",
        codex_runtime_config_file=tmp_path / "codex-runtime.yaml",
        codex_timeout_seconds=0,
        scheduler_poll_seconds=0,
        telegram_typing_interval_seconds=0,
        bypass_sandbox=True,
        usage_script=tmp_path / "usage.py",
        usage_timeout_seconds=0,
        bundled_skills_dir=tmp_path,
        sync_skills_overwrite=False,
        portfolio_file=tmp_path / "portfolio.txt",
        portfolio_except_file=tmp_path / "portfolio-except.txt",
    )


def scenario_load_enabled_config(tmp_path: Path) -> tuple:
    retry_config_path = tmp_path / "deferred-buy-retry.yaml"
    retry_config_path.write_text(
        "deferred_buy_retry:\n"
        "  enabled: true\n"
        "  delay_seconds: 120\n"
        "  expires_after_seconds: 600\n"
        "  slippage_bps: 25\n",
        encoding="utf-8",
    )
    config = make_config(tmp_path, retry_config_path)
    return config, load_deferred_buy_retry_config(config)


def check_loaded_config_matches_yaml(loaded) -> None:
    if loaded.enabled is not True:
        raise AssertionError(f"expected enabled=True, got: {loaded.enabled}")
    if loaded.delay_seconds != 120:
        raise AssertionError(f"expected delay_seconds=120, got: {loaded.delay_seconds}")
    if loaded.expires_after_seconds != 600:
        raise AssertionError(f"expected expires_after_seconds=600, got: {loaded.expires_after_seconds}")
    if loaded.slippage_bps != 25:
        raise AssertionError(f"expected slippage_bps=25, got: {loaded.slippage_bps}")


def write_sample_execution(run_dir: Path) -> None:
    execute_orders.write_json(
        run_dir / "execution.json",
        {
            "execution_environment": "real",
            "exchange": "SOR",
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


def scenario_enqueue_creates_artifact(tmp_path: Path, loaded) -> tuple:
    run_dir = tmp_path / "reports" / "runs" / "run-1"
    write_sample_execution(run_dir)
    with patch.dict("os.environ", {execute_orders.PORTFOLIO_EXCEPT_ENV_VAR: str(tmp_path / "portfolio-except.txt")}):
        created = enqueue_deferred_buy_retries(
            workspace_dir=tmp_path,
            source_run_dir=run_dir,
            delay_seconds=loaded.delay_seconds,
            expires_after_seconds=loaded.expires_after_seconds,
            slippage_bps=loaded.slippage_bps,
        )
    return run_dir, created


def check_enqueue_creates_exactly_one_artifact(created: list) -> None:
    if len(created) != 1:
        raise AssertionError(f"expected exactly one created artifact, got: {created}")


def check_enqueued_artifact_quantities_and_price(created: list) -> dict:
    artifact = execute_orders.load_json(created[0])
    if artifact["retry_quantity"] != 3:
        raise AssertionError(f"unexpected retry_quantity: {artifact}")
    if artifact["slippage_bps"] != 25:
        raise AssertionError(f"unexpected slippage_bps: {artifact}")
    if artifact["max_acceptable_price"] != 10030:
        raise AssertionError(f"unexpected max_acceptable_price: {artifact}")
    if artifact["excg_id_dvsn_cd"] != "SOR":
        raise AssertionError(f"deferred retry lost exchange routing: {artifact}")
    return artifact


def check_enqueued_artifact_expiry_window(artifact: dict) -> None:
    due_at = parse_time(artifact["due_at"])
    expires_at = parse_time(artifact["expires_at"])
    if due_at is None or expires_at is None:
        raise AssertionError(f"expected parseable due_at/expires_at, got: {artifact}")
    if int((expires_at - due_at).total_seconds()) != 600:
        raise AssertionError(f"unexpected expiry window: due_at={due_at} expires_at={expires_at}")


def scenario_enqueue_skips_excluded_symbol(tmp_path: Path, run_dir: Path, loaded) -> list:
    (tmp_path / "portfolio-except.txt").write_text("000660\n", encoding="utf-8")
    with patch.dict("os.environ", {execute_orders.PORTFOLIO_EXCEPT_ENV_VAR: str(tmp_path / "portfolio-except.txt")}):
        return enqueue_deferred_buy_retries(
            workspace_dir=tmp_path / "excluded-check",
            source_run_dir=run_dir,
            delay_seconds=loaded.delay_seconds,
            expires_after_seconds=loaded.expires_after_seconds,
            slippage_bps=loaded.slippage_bps,
        )


def check_enqueue_skips_excluded_symbol(skipped: list) -> None:
    if skipped != []:
        raise AssertionError(f"expected no artifacts for an excluded symbol, got: {skipped}")


def scenario_disable_config(config: Config):
    config.deferred_buy_retry_config_file.write_text("deferred_buy_retry:\n  enabled: false\n", encoding="utf-8")
    return load_deferred_buy_retry_config(config)


def check_disabled_config_reports_enabled_false(disabled) -> None:
    if disabled.enabled is not False:
        raise AssertionError(f"expected enabled=False after rewrite, got: {disabled.enabled}")


def self_test() -> None:
    check_retry_quantity_uses_requested_when_blocked()
    check_retry_quantity_uses_requested_minus_adjusted_to_when_not_blocked()
    check_cash_gate_reason_reads_quantity_adjustment_reason()
    check_cash_gate_reason_prefers_quantity_adjustment_over_top_level_reason()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        config, loaded = scenario_load_enabled_config(tmp_path)
        check_loaded_config_matches_yaml(loaded)

        run_dir, created = scenario_enqueue_creates_artifact(tmp_path, loaded)
        check_enqueue_creates_exactly_one_artifact(created)
        artifact = check_enqueued_artifact_quantities_and_price(created)
        check_enqueued_artifact_expiry_window(artifact)

        skipped = scenario_enqueue_skips_excluded_symbol(tmp_path, run_dir, loaded)
        check_enqueue_skips_excluded_symbol(skipped)

        disabled = scenario_disable_config(config)
        check_disabled_config_reports_enabled_false(disabled)


class DeferredBuyRetrySelfTest(unittest.TestCase):
    def test_self_test_suite_runs_every_check_without_raising(self) -> None:
        """Wrapper-orchestration check only: real behavior is covered by the
        granular tests below, so this mocks every helper instead of
        re-running the whole scenario a second time."""
        helper_names = [
            "check_retry_quantity_uses_requested_when_blocked",
            "check_retry_quantity_uses_requested_minus_adjusted_to_when_not_blocked",
            "check_cash_gate_reason_reads_quantity_adjustment_reason",
            "check_cash_gate_reason_prefers_quantity_adjustment_over_top_level_reason",
            "scenario_load_enabled_config",
            "check_loaded_config_matches_yaml",
            "scenario_enqueue_creates_artifact",
            "check_enqueue_creates_exactly_one_artifact",
            "check_enqueued_artifact_quantities_and_price",
            "check_enqueued_artifact_expiry_window",
            "scenario_enqueue_skips_excluded_symbol",
            "check_enqueue_skips_excluded_symbol",
            "scenario_disable_config",
            "check_disabled_config_reports_enabled_false",
        ]
        defaults = {
            "scenario_load_enabled_config": (None, None),
            "scenario_enqueue_creates_artifact": (None, []),
        }
        patchers = [patch(f"{__name__}.{name}", return_value=defaults.get(name)) for name in helper_names]
        mocks = [patcher.start() for patcher in patchers]
        self.addCleanup(lambda: [patcher.stop() for patcher in patchers])

        self_test()

        for mock in mocks:
            mock.assert_called()


class RetryQuantityAndCashGateTest(unittest.TestCase):
    def test_retry_quantity_uses_requested_when_blocked(self) -> None:
        check_retry_quantity_uses_requested_when_blocked()

    def test_retry_quantity_uses_requested_minus_adjusted_to_when_not_blocked(self) -> None:
        check_retry_quantity_uses_requested_minus_adjusted_to_when_not_blocked()

    def test_cash_gate_reason_reads_quantity_adjustment_reason(self) -> None:
        check_cash_gate_reason_reads_quantity_adjustment_reason()

    def test_cash_gate_reason_prefers_quantity_adjustment_over_top_level_reason(self) -> None:
        check_cash_gate_reason_prefers_quantity_adjustment_over_top_level_reason()


class DeferredBuyRetryLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.tmp_path = Path(self._temp_dir.name)
        self.config, self.loaded = scenario_load_enabled_config(self.tmp_path)
        self.run_dir, self.created = scenario_enqueue_creates_artifact(self.tmp_path, self.loaded)

    def test_loaded_config_matches_yaml(self) -> None:
        check_loaded_config_matches_yaml(self.loaded)

    def test_enqueue_creates_exactly_one_artifact(self) -> None:
        check_enqueue_creates_exactly_one_artifact(self.created)

    def test_enqueued_artifact_quantities_and_price(self) -> None:
        check_enqueued_artifact_quantities_and_price(self.created)

    def test_enqueued_artifact_expiry_window(self) -> None:
        artifact = check_enqueued_artifact_quantities_and_price(self.created)
        check_enqueued_artifact_expiry_window(artifact)

    def test_enqueue_skips_excluded_symbol(self) -> None:
        skipped = scenario_enqueue_skips_excluded_symbol(self.tmp_path, self.run_dir, self.loaded)
        check_enqueue_skips_excluded_symbol(skipped)

    def test_disabled_config_reports_enabled_false(self) -> None:
        disabled = scenario_disable_config(self.config)
        check_disabled_config_reports_enabled_false(disabled)


if __name__ == "__main__":
    unittest.main()
