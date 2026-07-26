#!/usr/bin/env python3
"""Integration tests for the scheduled daily-trading broker-preflight gate.

Proves the deterministic contract wired into Pipeline: a safety_block ends
the run before any full-review stage (decision-brief/Analyst/Debate/Judge/
execution-plan) and is reported as a completed-but-partial (non-failed) run;
a due fixed-review-time slot forces a full review while an already-satisfied
non-fixed-time invocation with an unchanged broker fingerprint ends after
preflight; manual invocations always resolve to a full review after a safe
preflight; a `full` decision does not persist its fingerprint until the full
review actually completes; and the preflight/full-continuation collector
split never duplicates KIS account/fill/lifecycle calls.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ..review_policy import review_trigger_state_path
from ..scripts.run_daily_trading_pipeline import Pipeline, load_json, write_json
from .test_run_daily_trading_pipeline import write_self_test_fixtures

FULL_REVIEW_STAGES = {"decision-brief", "first-specs", "merge-first", "second-spec", "execution-plan", "order-execution"}


def write_fake_lifecycle_execute_orders(path: Path, *, holding_state_issue_count: int, lookup_complete: bool = True) -> None:
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[sys.argv.index("--output-dir") + 1])
account_path = output_dir / "account-before-order.json"
account = json.loads(account_path.read_text(encoding="utf-8"))
account["active_order_lookup_performed"] = True
account["active_orders"] = []
lifecycle = {{
    "status": "partial" if {holding_state_issue_count} else ("success" if {lookup_complete} else "partial"),
    "lookup_complete": {lookup_complete},
    "active_order_count": 0,
    "previous_submitted_cash_order_count": 0,
    "holding_state_issue_count": {holding_state_issue_count},
    "holding_state_issues": [],
}}
account_path.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
(output_dir / "order-lifecycle.json").write_text(json.dumps(lifecycle, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(lifecycle, ensure_ascii=False))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def write_call_recording_main_evidence(path: Path, *, calls_log: Path) -> None:
    """Fake collect_main_evidence.py CLI that records every invocation's flags.

    Appends a one-line JSON record of which collection flags each call used
    (to a fixed, test-provided log path baked into the script), and only
    writes account-before-order.json/today-fills.json/price-chart.json when
    the corresponding flag was NOT passed and the file doesn't already exist
    -- so a test can assert account/fills/price were each collected (written)
    at most once across the whole pipeline run.
    """
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

argv = sys.argv
output_dir = Path(argv[argv.index("--output-dir") + 1])
calls_log = Path({str(calls_log)!r})
record = {{
    "skip_price_chart": "--skip-price-chart" in argv,
    "skip_account_asset": "--skip-account-asset" in argv,
    "reuse_account": "--reuse-account" in argv,
    "skip_account": "--skip-account" in argv,
}}
with calls_log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")

price_path = output_dir / "price-chart.json"
account_path = output_dir / "account-before-order.json"
today_fills_path = output_dir / "today-fills.json"
run_id = argv[argv.index("--run-id") + 1]
if not record["skip_price_chart"] and not price_path.exists():
    price_path.write_text(json.dumps({{"schema_version": "1", "status": "success", "symbols": []}}), encoding="utf-8")
if not record["reuse_account"]:
    account_path.write_text(
        json.dumps({{"schema_version": "1", "run_id": run_id, "status": "success",
                     "account_summary": {{"orderable_cash_amount": 900000}}, "symbols": []}}),
        encoding="utf-8",
    )
    today_fills_path.write_text(
        json.dumps({{"schema_version": "1", "run_id": run_id, "status": "success",
                     "skipped": False, "fill_scope": "account", "fills": []}}),
        encoding="utf-8",
    )
print(json.dumps({{"status": "success", "paths": {{}}, "counts": {{}}, "warnings": []}}))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def gate_args(
    *, workspace: Path, run_dir: Path, portfolio_path: Path, invocation_type: str, started_at: str
) -> argparse.Namespace:
    return argparse.Namespace(
        command="run",
        workspace_dir=str(workspace),
        output_dir=str(run_dir),
        run_id=run_dir.name,
        started_at=started_at,
        env="acct",
        request_type="real-submit",
        portfolio_json=str(portfolio_path),
        financial_cache_path="",
        symbol_news_cache_path="",
        main_events="",
        date="2026-07-27",
        reuse_existing_artifacts=True,
        skip_account=False,
        max_workers=2,
        submit_orders=True,
        invocation_type=invocation_type,
        full_review_times_config="",
    )


def make_pipeline(fake_execute_orders: Path, *, args: argparse.Namespace) -> Pipeline:
    class GatePipeline(Pipeline):
        def order_execution_script(self) -> str:
            return str(fake_execute_orders)

    return GatePipeline(args)


class SafetyBlockEndsBeforeFullReviewTest(unittest.TestCase):
    def test_holding_state_issue_blocks_full_review_and_reports_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "gate-safety-block"
            portfolio_path = write_self_test_fixtures(workspace, run_dir)
            fake_execute_orders = workspace / "fake-execute-orders-unsafe.py"
            write_fake_lifecycle_execute_orders(fake_execute_orders, holding_state_issue_count=1)

            pipeline = make_pipeline(
                fake_execute_orders,
                args=gate_args(
                    workspace=workspace,
                    run_dir=run_dir,
                    portfolio_path=portfolio_path,
                    invocation_type="scheduled",
                    started_at="2026-07-27T09:20:00+09:00",
                ),
            )
            summary = pipeline.run()

            # Safety_block still exits successfully (not a runner failure), but
            # it skipped a would-be-due review, so it's reported partial, and
            # run.json/pipeline-summary.json/stage required flags must agree.
            self.assertEqual(summary["status"], "partial")
            run_json = load_json(run_dir / "run.json")
            self.assertEqual(run_json["status"], "partial")
            stages = [item.get("stage") for item in run_json.get("stages", []) if isinstance(item, dict)]
            self.assertNotIn("decision-brief", stages)
            self.assertEqual(FULL_REVIEW_STAGES & set(stages), set())
            lifecycle_stage = next(item for item in run_json["stages"] if item.get("stage") == "order-lifecycle-preflight")
            self.assertFalse(lifecycle_stage["required"])
            self.assertFalse(any(item.get("required") and item.get("status") == "failed" for item in run_json["stages"]))
            trigger = load_json(run_dir / "review-trigger.json")
            self.assertEqual(trigger["decision"], "safety_block")
            self.assertIn("holding_state_issue_detected", trigger["safety"]["reasons"])
            telegram_summary = (run_dir / "telegram-summary.txt").read_text(encoding="utf-8")
            self.assertTrue(telegram_summary.strip())

    def test_incomplete_today_fills_lookup_blocks_full_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "gate-fills-incomplete"
            portfolio_path = write_self_test_fixtures(workspace, run_dir)
            write_json(
                run_dir / "today-fills.json",
                {
                    "schema_version": "1",
                    "run_id": run_dir.name,
                    "status": "partial",
                    "skipped": False,
                    "fill_scope": "account",
                    "fills": [],
                    "errors": [{"code": "today_fills_query_variant_failed"}],
                },
            )
            fake_execute_orders = workspace / "fake-execute-orders-fills-incomplete.py"
            write_fake_lifecycle_execute_orders(fake_execute_orders, holding_state_issue_count=0)

            pipeline = make_pipeline(
                fake_execute_orders,
                args=gate_args(
                    workspace=workspace,
                    run_dir=run_dir,
                    portfolio_path=portfolio_path,
                    invocation_type="scheduled",
                    started_at="2026-07-27T09:20:00+09:00",
                ),
            )
            summary = pipeline.run()

            self.assertEqual(summary["status"], "partial")
            trigger = load_json(run_dir / "review-trigger.json")
            self.assertEqual(trigger["decision"], "safety_block")
            self.assertIn("today_fills_lookup_incomplete", trigger["safety"]["reasons"])

    def test_unexpected_non_universe_holding_blocks_full_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "gate-unexpected-holding"
            portfolio_path = write_self_test_fixtures(workspace, run_dir)
            account = load_json(run_dir / "account-before-order.json")
            account["non_universe_account_positions"] = [
                {"symbol_id": "999999", "symbol_name": "unexpected", "current_live_holding_quantity": 5}
            ]
            write_json(run_dir / "account-before-order.json", account)
            fake_execute_orders = workspace / "fake-execute-orders-unexpected-holding.py"
            write_fake_lifecycle_execute_orders(fake_execute_orders, holding_state_issue_count=0)

            pipeline = make_pipeline(
                fake_execute_orders,
                args=gate_args(
                    workspace=workspace,
                    run_dir=run_dir,
                    portfolio_path=portfolio_path,
                    invocation_type="scheduled",
                    started_at="2026-07-27T09:20:00+09:00",
                ),
            )
            summary = pipeline.run()

            self.assertEqual(summary["status"], "partial")
            trigger = load_json(run_dir / "review-trigger.json")
            self.assertEqual(trigger["decision"], "safety_block")
            self.assertIn("unexpected_non_universe_holding", trigger["safety"]["reasons"])
            self.assertIn("999999", trigger["safety"]["unexpected_non_universe_symbols"])

    def test_explicitly_excepted_non_universe_holding_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "gate-excepted-holding"
            portfolio_path = write_self_test_fixtures(workspace, run_dir)
            portfolio = load_json(portfolio_path)
            portfolio["portfolio_except"] = ["999999"]
            write_json(portfolio_path, portfolio)
            account = load_json(run_dir / "account-before-order.json")
            account["non_universe_account_positions"] = [
                {"symbol_id": "999999", "symbol_name": "excepted", "current_live_holding_quantity": 5}
            ]
            write_json(run_dir / "account-before-order.json", account)
            fake_execute_orders = workspace / "fake-execute-orders-excepted-holding.py"
            write_fake_lifecycle_execute_orders(fake_execute_orders, holding_state_issue_count=0)

            pipeline = make_pipeline(
                fake_execute_orders,
                args=gate_args(
                    workspace=workspace,
                    run_dir=run_dir,
                    portfolio_path=portfolio_path,
                    invocation_type="manual",
                    started_at="2026-07-27T09:20:00+09:00",
                ),
            )
            pipeline.add_stage("portfolio-universe", "success", detail="fixture")
            self.assertIsNone(pipeline.run_broker_preflight_gate(["005930", "000660"], ["999999"]))
            trigger = load_json(run_dir / "review-trigger.json")
            self.assertEqual(trigger["decision"], "full")
            self.assertTrue(trigger["safety"]["safe"])


class DueSlotAndFingerprintGateStateTest(unittest.TestCase):
    def test_due_fixed_time_runs_full_and_next_non_fixed_time_ends_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            fake_execute_orders = workspace / "fake-execute-orders-safe.py"
            write_fake_lifecycle_execute_orders(fake_execute_orders, holding_state_issue_count=0)
            symbols = ["005930", "000660"]

            run_dir_1 = workspace / "reports" / "runs" / "gate-full-0905"
            portfolio_path = write_self_test_fixtures(workspace, run_dir_1)
            pipeline_1 = make_pipeline(
                fake_execute_orders,
                args=gate_args(
                    workspace=workspace,
                    run_dir=run_dir_1,
                    portfolio_path=portfolio_path,
                    invocation_type="scheduled",
                    started_at="2026-07-27T09:05:00+09:00",
                ),
            )
            pipeline_1.add_stage("portfolio-universe", "success", detail="fixture")
            self.assertIsNone(pipeline_1.run_broker_preflight_gate(symbols, []))
            trigger_1 = load_json(run_dir_1 / "review-trigger.json")
            self.assertEqual(trigger_1["decision"], "full")
            self.assertIn("fixed_review_time_due", trigger_1["reasons"])
            self.assertTrue(trigger_1["full_review_selected"])
            self.assertFalse(trigger_1["full_review_completed"])

            # Rule 2: selecting "full" must not persist a new fingerprint yet.
            state_path = review_trigger_state_path(workspace, "real")
            self.assertFalse(state_path.exists())

            # Simulate the full review this gate authorized completing successfully.
            pipeline_1.finalize_review_gate_state({"status": "success"})
            state_after_full = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state_after_full["last_satisfied_time"], "09:05")
            completed_trigger = load_json(run_dir_1 / "review-trigger.json")
            self.assertTrue(completed_trigger["full_review_completed"])

            # A later non-fixed-time invocation (09:20) with an unchanged broker
            # snapshot must end after preflight rather than repeat the full review.
            run_dir_2 = workspace / "reports" / "runs" / "gate-skip-0920"
            write_self_test_fixtures(workspace, run_dir_2)
            pipeline_2 = make_pipeline(
                fake_execute_orders,
                args=gate_args(
                    workspace=workspace,
                    run_dir=run_dir_2,
                    portfolio_path=portfolio_path,
                    invocation_type="scheduled",
                    started_at="2026-07-27T09:20:00+09:00",
                ),
            )
            pipeline_2.add_stage("portfolio-universe", "success", detail="fixture")
            short_circuit = pipeline_2.run_broker_preflight_gate(symbols, [])
            self.assertIsNotNone(short_circuit)
            self.assertEqual(short_circuit["status"], "success")
            trigger_2 = load_json(run_dir_2 / "review-trigger.json")
            self.assertEqual(trigger_2["decision"], "skipped")
            self.assertEqual(trigger_2["changed_components"], [])

    def test_manual_invocation_is_full_even_at_a_non_fixed_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            fake_execute_orders = workspace / "fake-execute-orders-manual.py"
            write_fake_lifecycle_execute_orders(fake_execute_orders, holding_state_issue_count=0)

            run_dir = workspace / "reports" / "runs" / "gate-manual"
            portfolio_path = write_self_test_fixtures(workspace, run_dir)
            pipeline = make_pipeline(
                fake_execute_orders,
                args=gate_args(
                    workspace=workspace,
                    run_dir=run_dir,
                    portfolio_path=portfolio_path,
                    invocation_type="manual",
                    started_at="2026-07-27T09:20:00+09:00",
                ),
            )
            pipeline.add_stage("portfolio-universe", "success", detail="fixture")
            self.assertIsNone(pipeline.run_broker_preflight_gate(["005930", "000660"], []))
            trigger = load_json(run_dir / "review-trigger.json")
            self.assertEqual(trigger["decision"], "full")
            self.assertIn("manual_invocation", trigger["reasons"])

    def test_selected_full_review_that_never_finalizes_still_selects_full_next_time_on_changed_fingerprint(self) -> None:
        """Regression for rule 2: a crashed/failed full review must not lose a
        detected broker change. Seed prior state as if 09:05 already completed
        with an old fingerprint, then have 09:20 observe a changed broker
        snapshot and select full -- simulate the crash by never finalizing --
        and assert the next invocation with the same changed snapshot still
        selects full for broker_fingerprint_changed (not a wrongly-skipped run
        because the unsaved "full" decision looked like an unchanged fingerprint).
        """
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            fake_execute_orders = workspace / "fake-execute-orders-crash.py"
            write_fake_lifecycle_execute_orders(fake_execute_orders, holding_state_issue_count=0)
            symbols = ["005930", "000660"]

            state_path = review_trigger_state_path(workspace, "real")
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": "2",
                        "date": "2026-07-27",
                        "fingerprint": "stale-fingerprint-from-before-the-change",
                        "last_satisfied_time": "09:05",
                        "fingerprint_payload": {"holdings": [{"symbol_id": "005930", "current_live_holding_quantity": 1}]},
                    }
                ),
                encoding="utf-8",
            )

            run_dir_1 = workspace / "reports" / "runs" / "gate-0920-crash"
            portfolio_path = write_self_test_fixtures(workspace, run_dir_1)
            account = load_json(run_dir_1 / "account-before-order.json")
            for item in account["symbols"]:
                if item["symbol_id"] == "005930":
                    item["current_live_holding_quantity"] = 999  # the "broker change"
            write_json(run_dir_1 / "account-before-order.json", account)

            pipeline_1 = make_pipeline(
                fake_execute_orders,
                args=gate_args(
                    workspace=workspace,
                    run_dir=run_dir_1,
                    portfolio_path=portfolio_path,
                    invocation_type="scheduled",
                    started_at="2026-07-27T09:20:00+09:00",
                ),
            )
            pipeline_1.add_stage("portfolio-universe", "success", detail="fixture")
            self.assertIsNone(pipeline_1.run_broker_preflight_gate(symbols, []))
            trigger_1 = load_json(run_dir_1 / "review-trigger.json")
            self.assertEqual(trigger_1["decision"], "full")
            self.assertIn("broker_fingerprint_changed", trigger_1["reasons"])
            # Simulate a crash: finalize_review_gate_state is never called, so
            # state on disk must be exactly what it was before this run.
            unchanged_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(unchanged_state["fingerprint"], "stale-fingerprint-from-before-the-change")

            # Next invocation (still 09:20-equivalent slot, same changed broker
            # snapshot) must select full again for the same reason.
            run_dir_2 = workspace / "reports" / "runs" / "gate-0925-retry"
            write_self_test_fixtures(workspace, run_dir_2)
            account_2 = load_json(run_dir_2 / "account-before-order.json")
            for item in account_2["symbols"]:
                if item["symbol_id"] == "005930":
                    item["current_live_holding_quantity"] = 999
            write_json(run_dir_2 / "account-before-order.json", account_2)
            pipeline_2 = make_pipeline(
                fake_execute_orders,
                args=gate_args(
                    workspace=workspace,
                    run_dir=run_dir_2,
                    portfolio_path=portfolio_path,
                    invocation_type="scheduled",
                    started_at="2026-07-27T09:25:00+09:00",
                ),
            )
            pipeline_2.add_stage("portfolio-universe", "success", detail="fixture")
            self.assertIsNone(pipeline_2.run_broker_preflight_gate(symbols, []))
            trigger_2 = load_json(run_dir_2 / "review-trigger.json")
            self.assertEqual(trigger_2["decision"], "full")
            self.assertIn("broker_fingerprint_changed", trigger_2["reasons"])
            self.assertIn("holdings", trigger_2["changed_components"])


class CollectorReuseContractTest(unittest.TestCase):
    def test_preflight_snapshot_skips_price_chart_and_account_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "gate-collector-preflight"
            portfolio_path = write_self_test_fixtures(workspace, run_dir)
            # Force a real (non-reused) preflight collection call.
            (run_dir / "account-before-order.json").unlink()
            (run_dir / "today-fills.json").unlink()
            (run_dir / "price-chart.json").unlink()
            calls_log = workspace / "calls.jsonl"
            fake_main_evidence = workspace / "fake-main-evidence.py"
            write_call_recording_main_evidence(fake_main_evidence, calls_log=calls_log)
            fake_execute_orders = workspace / "fake-execute-orders-collector.py"
            write_fake_lifecycle_execute_orders(fake_execute_orders, holding_state_issue_count=0)

            class RecordingPipeline(Pipeline):
                def order_execution_script(self) -> str:
                    return str(fake_execute_orders)

                def main_evidence_script(self) -> str:
                    return str(fake_main_evidence)

            args = gate_args(
                workspace=workspace,
                run_dir=run_dir,
                portfolio_path=portfolio_path,
                invocation_type="scheduled",
                started_at="2026-07-27T09:05:00+09:00",
            )
            pipeline = RecordingPipeline(args)
            pipeline.add_stage("portfolio-universe", "success", detail="fixture")

            self.assertIsNone(pipeline.run_broker_preflight_gate(["005930", "000660"], []))
            calls = [json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0]["skip_price_chart"])
            self.assertTrue(calls[0]["skip_account_asset"])
            self.assertFalse(calls[0]["reuse_account"])
            self.assertFalse((run_dir / "price-chart.json").exists())

    def test_full_continuation_reuses_account_and_fills_without_recollecting_or_rerunning_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "gate-collector-full"
            portfolio_path = write_self_test_fixtures(workspace, run_dir)
            calls_log = workspace / "calls.jsonl"
            fake_main_evidence = workspace / "fake-main-evidence.py"
            write_call_recording_main_evidence(fake_main_evidence, calls_log=calls_log)
            fake_execute_orders = workspace / "fake-execute-orders-full-collector.py"
            write_fake_lifecycle_execute_orders(fake_execute_orders, holding_state_issue_count=0)

            class RecordingPipeline(Pipeline):
                def order_execution_script(self) -> str:
                    return str(fake_execute_orders)

                def main_evidence_script(self) -> str:
                    return str(fake_main_evidence)

            args = gate_args(
                workspace=workspace,
                run_dir=run_dir,
                portfolio_path=portfolio_path,
                invocation_type="scheduled",
                started_at="2026-07-27T09:05:00+09:00",
            )
            args.reuse_existing_artifacts = False
            account_before = load_json(run_dir / "account-before-order.json")
            pipeline = RecordingPipeline(args)
            pipeline.add_stage("portfolio-universe", "success", detail="fixture")
            self.assertIsNone(pipeline.run_broker_preflight_gate(["005930", "000660"], []))
            # Lifecycle already ran once inside the gate; the account artifact
            # now carries lifecycle-added fields the fake execute-orders wrote.
            account_after_gate = load_json(run_dir / "account-before-order.json")
            self.assertTrue(account_after_gate["active_order_lookup_performed"])

            pipeline.collect_main_evidence(["005930", "000660"], reuse_account_and_fills=True)

            calls = [json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(calls), 2)
            preflight_call, full_call = calls
            self.assertTrue(preflight_call["skip_price_chart"])
            self.assertFalse(preflight_call["reuse_account"])
            self.assertFalse(full_call["skip_price_chart"])
            self.assertTrue(full_call["reuse_account"])
            self.assertFalse(full_call["skip_account_asset"])

            # The full continuation must not have re-collected/overwritten the
            # already lifecycle-reconciled account artifact with a fresh one.
            account_after_full = load_json(run_dir / "account-before-order.json")
            self.assertTrue(account_after_full["active_order_lookup_performed"])
            self.assertNotEqual(account_before, account_after_full)

            stage_names = [item.get("stage") for item in pipeline.stages if isinstance(item, dict)]
            self.assertEqual(stage_names.count("order-lifecycle-preflight"), 1)


class StaleReuseExistingArtifactsIdentityTest(unittest.TestCase):
    """The gate's --reuse-existing-artifacts shortcut must apply the exact same
    identity/completeness validation as the live subprocess-collected path: a
    stale or wrong-run account/today-fills artifact must never reach lifecycle
    preflight or a full review, only safety_block.
    """

    def test_wrong_run_id_account_artifact_forces_safety_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "gate-stale-account"
            portfolio_path = write_self_test_fixtures(workspace, run_dir)
            account = load_json(run_dir / "account-before-order.json")
            account["run_id"] = "some-other-run"
            write_json(run_dir / "account-before-order.json", account)
            fake_execute_orders = workspace / "fake-execute-orders-stale-account.py"
            write_fake_lifecycle_execute_orders(fake_execute_orders, holding_state_issue_count=0)

            pipeline = make_pipeline(
                fake_execute_orders,
                args=gate_args(
                    workspace=workspace,
                    run_dir=run_dir,
                    portfolio_path=portfolio_path,
                    invocation_type="manual",
                    started_at="2026-07-27T09:20:00+09:00",
                ),
            )
            pipeline.add_stage("portfolio-universe", "success", detail="fixture")
            short_circuit = pipeline.run_broker_preflight_gate(["005930", "000660"], [])
            self.assertIsNotNone(short_circuit)
            trigger = load_json(run_dir / "review-trigger.json")
            self.assertEqual(trigger["decision"], "safety_block")
            self.assertIn("account_lookup_failed", trigger["safety"]["reasons"])
            # A wrong-run account artifact must never let lifecycle preflight run.
            self.assertFalse((run_dir / "order-lifecycle.json").exists())

    def test_partial_status_account_artifact_forces_safety_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "gate-partial-account"
            portfolio_path = write_self_test_fixtures(workspace, run_dir)
            account = load_json(run_dir / "account-before-order.json")
            account["status"] = "partial"
            write_json(run_dir / "account-before-order.json", account)
            fake_execute_orders = workspace / "fake-execute-orders-partial-account.py"
            write_fake_lifecycle_execute_orders(fake_execute_orders, holding_state_issue_count=0)

            pipeline = make_pipeline(
                fake_execute_orders,
                args=gate_args(
                    workspace=workspace,
                    run_dir=run_dir,
                    portfolio_path=portfolio_path,
                    invocation_type="manual",
                    started_at="2026-07-27T09:20:00+09:00",
                ),
            )
            pipeline.add_stage("portfolio-universe", "success", detail="fixture")
            short_circuit = pipeline.run_broker_preflight_gate(["005930", "000660"], [])
            self.assertIsNotNone(short_circuit)
            trigger = load_json(run_dir / "review-trigger.json")
            self.assertEqual(trigger["decision"], "safety_block")
            self.assertIn("account_lookup_failed", trigger["safety"]["reasons"])

    def test_wrong_scope_today_fills_artifact_forces_safety_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "gate-wrong-scope-fills"
            portfolio_path = write_self_test_fixtures(workspace, run_dir)
            today_fills = load_json(run_dir / "today-fills.json")
            today_fills["fill_scope"] = "universe"
            write_json(run_dir / "today-fills.json", today_fills)
            fake_execute_orders = workspace / "fake-execute-orders-wrong-scope-fills.py"
            write_fake_lifecycle_execute_orders(fake_execute_orders, holding_state_issue_count=0)

            pipeline = make_pipeline(
                fake_execute_orders,
                args=gate_args(
                    workspace=workspace,
                    run_dir=run_dir,
                    portfolio_path=portfolio_path,
                    invocation_type="manual",
                    started_at="2026-07-27T09:20:00+09:00",
                ),
            )
            pipeline.add_stage("portfolio-universe", "success", detail="fixture")
            short_circuit = pipeline.run_broker_preflight_gate(["005930", "000660"], [])
            self.assertIsNotNone(short_circuit)
            trigger = load_json(run_dir / "review-trigger.json")
            self.assertEqual(trigger["decision"], "safety_block")
            self.assertIn("today_fills_lookup_incomplete", trigger["safety"]["reasons"])


class ReviewTriggerStatePersistenceFailureTest(unittest.TestCase):
    def test_os_error_on_post_full_review_persist_does_not_fail_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "gate-persist-failure"
            portfolio_path = write_self_test_fixtures(workspace, run_dir)
            fake_execute_orders = workspace / "fake-execute-orders-persist-failure.py"
            write_fake_lifecycle_execute_orders(fake_execute_orders, holding_state_issue_count=0)

            state_path = review_trigger_state_path(workspace, "real")
            state_path.parent.mkdir(parents=True, exist_ok=True)
            prior_state_json = {
                "schema_version": "2",
                "date": "2026-07-26",
                "fingerprint": "prior-fingerprint",
                "last_satisfied_time": "15:15",
                "fingerprint_payload": {"holdings": []},
            }
            state_path.write_text(json.dumps(prior_state_json), encoding="utf-8")

            pipeline = make_pipeline(
                fake_execute_orders,
                args=gate_args(
                    workspace=workspace,
                    run_dir=run_dir,
                    portfolio_path=portfolio_path,
                    invocation_type="scheduled",
                    started_at="2026-07-27T09:05:00+09:00",
                ),
            )
            pipeline.add_stage("portfolio-universe", "success", detail="fixture")
            self.assertIsNone(pipeline.run_broker_preflight_gate(["005930", "000660"], []))

            # run_daily_trading_pipeline.py imports review_policy via an
            # explicit sys.path insert (so it also works as a bare subprocess
            # script), which registers it under the flat "review_policy" name
            # in sys.modules -- distinct from this test's package-relative
            # `..review_policy` import. Patch the flat module Pipeline actually
            # calls through.
            with patch("review_policy.save_review_trigger_state", side_effect=OSError("disk full")):
                # Must not raise even though persistence fails.
                pipeline.finalize_review_gate_state({"status": "success"})

            # Prior state on disk is untouched by the failed write.
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), prior_state_json)

            persist_stage = next(item for item in pipeline.stages if item.get("stage") == "review-trigger-state-persist")
            self.assertEqual(persist_stage["status"], "partial")
            self.assertFalse(persist_stage["required"])

            trigger = load_json(run_dir / "review-trigger.json")
            self.assertTrue(trigger["full_review_completed"])


if __name__ == "__main__":
    unittest.main()
