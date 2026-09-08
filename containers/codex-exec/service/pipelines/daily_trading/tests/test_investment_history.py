from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from ..scripts import investment_history as memory, run_subagent
from ..scripts.run_daily_trading_pipeline import Pipeline


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def thesis(label: str) -> dict:
    return {"core_rationale": label, "invalidation_conditions": [{"condition_id": "x", "description": "original condition"}],
            "evaluation_point": "next reported quarter"}


def fill(order: str, day: str, side: str, amount: int, clock: str = "09:01") -> dict:
    return {"symbol_id": "A", "direction": side, "order_id": order, "order_date": day.replace("-", ""),
            "filled_at": f"{day}T{clock}:00+09:00", "filled_quantity": amount, "filled_price": 100}


def snapshot(root: Path, name: str, stamp: str, held: int, buys: int, sells: int,
             *, fills: list | None = None, label: str | None = None, order: tuple | None = None) -> Path:
    path = root / name
    dt = datetime.fromisoformat(stamp)
    write(path / "account-before-order.json", {
        "run_id": name, "started_at": stamp, "generated_at": stamp, "status": "success", "execution_environment": "real",
        "symbols": [{"symbol_id": "A", "current_live_holding_quantity": held, "today_buy_quantity": buys,
                     "today_sell_quantity": sells, "snapshot_row_available": True, "holding_state_status": "consistent"}],
    })
    write(path / "today-fills.json", {"status": "success", "generated_at": stamp, "execution_environment": "real",
        "fill_scope": "account", "symbols": [{"symbol_id": "A"}], "fills": fills or []})
    if label:
        write(path / "judge-review.json", {"run_id": name, "started_at": stamp,
            "generated_at": (dt + timedelta(seconds=10)).isoformat(), "status": "success", "symbols": [{
                "symbol_id": "A", "target_position_value_krw": 500, "final_holding_quantity": 5,
                "reason_code": label, "one_line_reason": label, "thesis_definition": thesis(label),
            }]})
    if order:
        order_id, side = order
        write(path / "execution.json", {"run_id": name, "started_at": stamp,
            "generated_at": (dt + timedelta(seconds=20)).isoformat(), "execution_environment": "real", "orders": [{
                "symbol_id": "A", "direction": side, "order_path": "immediate", "result": "submitted",
                "order_or_reservation_id": order_id, "broker_reconciliation": {"status": "pending", "filled_quantity": 0},
            }]})
    return path


class InvestmentHistoryTest(unittest.TestCase):
    def test_entry_add_trim_exit_and_reentry_preserve_original_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = "2026-09-07"
            snapshot(root, "entry", day + "T09:00:00+09:00", 0, 0, 0, label="entry-plan", order=("1", "buy"))
            initial = fill("1", day, "buy", 2)
            snapshot(root, "add", day + "T10:00:00+09:00", 2, 2, 0, fills=[initial], label="add-plan", order=("2", "buy"))
            added = fill("2", day, "buy", 3, "10:01")
            snapshot(root, "trim", day + "T11:00:00+09:00", 5, 5, 0, fills=[initial, added], label="trim-plan", order=("3", "sell"))
            trimmed = fill("3", day, "sell", 2, "11:01")
            current = snapshot(root, "hold", day + "T12:00:00+09:00", 3, 5, 2, fills=[initial, added, trimmed], label="unfilled-replacement")
            history = memory.build_history(current, memory.timestamp(day + "T12:01:00+09:00"), read)
            active = history["symbols"]["A"]["active_investment"]
            self.assertEqual(active["entry"]["rationale"]["thesis_definition"], thesis("entry-plan"))
            self.assertEqual([change["rationale"]["reason_code"] for change in active["changes"]], ["add-plan", "trim-plan"])
            first_id = active["investment_id"]
            snapshot(root, "exit", day + "T13:00:00+09:00", 3, 5, 2, fills=[initial, added, trimmed], label="exit-plan", order=("4", "sell"))
            closed = fill("4", day, "sell", 3, "13:01")
            flat = snapshot(root, "flat", day + "T14:00:00+09:00", 0, 5, 5, fills=[initial, added, trimmed, closed])
            closed_history = memory.build_history(flat, memory.timestamp(day + "T14:00:00+09:00"), read)
            self.assertIsNone(closed_history["symbols"]["A"]["active_investment"])
            self.assertEqual(closed_history["symbols"]["A"]["closed_investments"][-1]["exit"]["rationale"]["reason_code"], "exit-plan")
            day2 = "2026-09-08"
            snapshot(root, "reentry", day2 + "T09:00:00+09:00", 0, 0, 0, label="new-plan", order=("1", "buy"))
            current = snapshot(root, "current", day2 + "T10:00:00+09:00", 1, 1, 0, fills=[fill("1", day2, "buy", 1)])
            context = memory.review_context(memory.build_history(current, memory.timestamp(day2 + "T10:00:00+09:00"), read), "A", 1)
            self.assertNotEqual(context["active_investment"]["investment_id"], first_id)
            self.assertEqual(context["active_investment"]["entry"]["rationale"]["reason_code"], "new-plan")
            self.assertEqual(context["last_closed_investment"]["investment_id"], first_id)

    def test_cumulative_partial_fills_and_repeated_snapshots_are_not_duplicate_trades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, day = Path(tmp), "2026-09-07"
            source = snapshot(root, "entry", day + "T09:00:00+09:00", 0, 0, 0, label="entry", order=("1", "buy"))
            first = fill("1", day, "buy", 1)
            snapshot(root, "partial", day + "T10:00:00+09:00", 1, 1, 0, fills=[first])
            current = snapshot(root, "completed", day + "T11:00:00+09:00", 2, 2, 0, fills=[fill("1", day, "buy", 2)])
            history = memory.build_history(current, memory.timestamp(day + "T11:00:00+09:00"), read)
            active = history["symbols"]["A"]["active_investment"]
            self.assertEqual(active["entry"]["quantity"], 1)
            self.assertEqual([change["quantity"] for change in active["changes"]], [1])
            self.assertEqual(memory.build_history(current, memory.timestamp(day + "T11:00:00+09:00"), read), history)
            # A confirmed reconciliation is usable even before today-fills catches up.
            evidence = read(source / "execution.json")
            evidence["orders"][0]["broker_reconciliation"] = {"status": "filled", "filled_quantity": 2,
                "observed_at": day + "T09:30:00+09:00"}
            write(source / "execution.json", evidence)
            write(current / "today-fills.json", {})
            write(root / "partial/today-fills.json", {})
            confirmed = memory.build_history(current, memory.timestamp(day + "T11:00:00+09:00"), read)
            self.assertEqual(confirmed["symbols"]["A"]["active_investment"]["entry"]["quantity"], 2)

    def test_legacy_external_unfilled_missing_and_future_evidence_never_invent_an_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, day = Path(tmp), "2026-09-07"
            snapshot(root, "old", day + "T09:00:00+09:00", 5, 0, 0, label="old-thesis")
            current = snapshot(root, "current", day + "T10:00:00+09:00", 5, 0, 0, label="unfilled-thesis", order=("1", "buy"))
            cutoff = memory.timestamp(day + "T10:01:00+09:00")
            active = memory.build_history(current, cutoff, read)["symbols"]["A"]["active_investment"]
            self.assertIsNone(active["entry"])
            self.assertEqual(active["origin_status"], "unavailable")
            # Matching quantities alone must not hide a bad current account snapshot.
            account = read(current / "account-before-order.json")
            account["symbols"][0]["holding_state_status"] = "inconsistent"
            write(current / "account-before-order.json", account)
            self.assertEqual(memory.review_context(memory.build_history(current, cutoff, read), "A", 5)["status"], "unavailable")
            account["symbols"][0].update(holding_state_status="consistent", observed_at=day + "T09:00:00+09:00")
            account["generated_at"] = day + "T12:00:00+09:00"
            write(current / "account-before-order.json", account)
            self.assertEqual(memory.review_context(memory.build_history(current, cutoff, read), "A", 5)["status"], "unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = snapshot(root, "decision", day + "T09:00:00+09:00", 0, 0, 0, label="not-this-order", order=("different", "buy"))
            current = snapshot(root, "current", day + "T10:00:00+09:00", 1, 1, 0, fills=[fill("manual", day, "buy", 1)])
            context = memory.build_history(current, cutoff, read)["symbols"]["A"]["active_investment"]
            self.assertEqual(context["entry"]["rationale"]["status"], "unavailable")
            before = memory.build_history(current, cutoff, read)
            snapshot(root, "future", day + "T11:00:00+09:00", 0, 1, 1, label="future", order=("manual", "buy"))
            self.assertEqual(memory.build_history(current, cutoff, read), before)
            delayed = read(source / "execution.json")
            delayed["generated_at"] = day + "T12:00:00+09:00"
            delayed["orders"][0]["order_or_reservation_id"] = "manual"
            write(source / "execution.json", delayed)
            self.assertEqual(memory.build_history(current, cutoff, read), before)

    def test_launcher_input_and_saved_provenance_use_entry_not_latest_advice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, day = Path(tmp), "2026-09-07"
            snapshot(root, "entry", day + "T09:00:00+09:00", 0, 0, 0, label="entry", order=("1", "buy"))
            current = snapshot(root, "current", day + "T10:00:00+09:00", 2, 2, 0, fills=[fill("1", day, "buy", 2)], label="unused")
            brief = {"generated_at": day + "T10:00:00+09:00", "source_artifacts": ["account-before-order.json"],
                "symbols": [{"symbol_id": "A", "price": {"current_or_last": 100},
                "account_exposure": {"current_live_holding_quantity": 2}}]}
            payload = run_subagent.add_judge_review_holding_context(brief, current, day + "T10:00:00+09:00")
            row = payload["symbols"][0]
            self.assertEqual(row["prior_decision_context"]["thesis_definition"], thesis("entry"))
            self.assertEqual(row["investment_context"]["active_investment"]["entry"]["rationale"]["source_run_id"], "entry")
            self.assertTrue((current / "investment-history.json").is_file())
            analyst = run_subagent.build_review_core_payload(payload, ["A"], "analyst-quality-risk")
            self.assertNotIn("investment_context", analyst["symbols"][0])
            pipeline = object.__new__(Pipeline)
            pipeline.output_dir, pipeline.run_id, pipeline.started_at = current, "current", day + "T10:00:00+09:00"
            proposal = {"symbol_id": "A", "target_position_value_krw": 0, "thesis_definition": thesis("new-unfilled")}
            normalized, errors = pipeline.derive_judge_final_quantity(proposal, row)
            self.assertEqual(errors, [])
            self.assertEqual(normalized["final_holding_quantity"], 0)  # no sell gate
            self.assertEqual(normalized["prior_thesis_context"]["source_run_id"], "entry")
            self.assertEqual(normalized["thesis_definition"]["evaluation_point"], "next reported quarter")
            forged = {**proposal, "investment_context": {"status": "forged"}}
            normalized, _ = pipeline.derive_judge_final_quantity(forged, {"price": {"current_or_last": 100}})
            self.assertNotIn("investment_context", normalized)
            mapping = copy.deepcopy(brief)
            mapping["symbols"] = {"A": mapping["symbols"][0]}
            mapping["symbols"]["A"].pop("symbol_id")
            self.assertEqual(run_subagent.add_judge_review_holding_context(mapping, current)["symbols"]["A"]["investment_context"], row["investment_context"])

    def test_delayed_lifecycle_fill_and_next_session_balance_recover_exact_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, day = Path(tmp), "2026-09-07"
            source = snapshot(root, "entry", day + "T15:00:00+09:00", 0, 0, 0, label="entry")
            # Deferred submission belongs to the original Judge run, not the observing run.
            current = snapshot(root, "next-day", "2026-09-08T09:00:00+09:00", 2, 0, 0, label="unused")
            write(current / "order-lifecycle.json", {"generated_at": "2026-09-08T09:00:00+09:00",
                "previous_submitted_cash_orders": [{"run_id": source.name, "started_at": day + "T15:01:00+09:00",
                    "symbol_id": "A", "direction": "buy", "order_id": "late", "order_path": "immediate"}]})
            fills = read(current / "today-fills.json")
            fills["previous_session"] = {"session_date": "20260907", "fill_collection_status": "complete",
                "fills": [fill("late", day, "buy", 2, "15:02")]}
            write(current / "today-fills.json", fills)
            cutoff = memory.timestamp("2026-09-08T09:01:00+09:00")
            context = memory.review_context(memory.build_history(current, cutoff, read), "A", 2)
            self.assertEqual(context["status"], "reconciled")
            self.assertEqual(context["active_investment"]["entry"]["rationale"]["source_run_id"], "entry")
            # Without complete previous-session evidence, do not infer missing trades.
            fills["previous_session"]["fill_collection_status"] = "partial"
            write(current / "today-fills.json", fills)
            context = memory.review_context(memory.build_history(current, cutoff, read), "A", 2)
            self.assertEqual(context["active_investment"]["origin_status"], "unavailable")

    def test_replay_uses_only_simulated_fills_and_excludes_future_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, day = Path(tmp), "2026-09-07"
            source = snapshot(root, "day1", day + "T09:00:00+09:00", 0, 0, 0, label="virtual-entry")
            # Production replay stores wall-clock generated_at and virtual started_at.
            judge = read(source / "judge-review.json")
            judge["generated_at"] = "2026-10-01T10:00:00+09:00"
            write(source / "judge-review.json", judge)
            mode = {"mode": "archived_point_in_time_replay"}
            write(source / "decision-brief.json", {"source_artifacts": mode})
            write(source / "simulated-judge-review.json", {"status": "success", "symbols": [{"symbol_id": "A", "simulated_final_quantity": 2}],
                "fills": [fill("unused-broker-id", day, "buy", 2)]})
            before = memory.build_history(source, memory.timestamp(day + "T09:00:00+09:00"), read, simulated=True)
            self.assertIsNone(before["symbols"]["A"]["active_investment"])
            current = snapshot(root, "day2", "2026-09-08T09:00:00+09:00", 2, 0, 0)
            write(current / "decision-brief.json", {"source_artifacts": mode})
            snapshot(root, "unrelated-live", "2026-09-08T09:00:00+09:00", 9, 9, 0, label="wrong-live-plan")
            context = memory.review_context(memory.build_history(current, memory.timestamp("2026-09-08T09:00:00+09:00"), read, simulated=True), "A", 2)
            self.assertEqual(context["evidence_mode"], "simulated_fills")
            self.assertEqual(context["active_investment"]["entry"]["rationale"]["reason_code"], "virtual-entry")
            self.assertEqual(context["active_investment"]["entry"]["rationale"]["decision_clock"], "replay_information_cutoff")
            judge["started_at"] = "2026-09-08T10:00:00+09:00"
            write(source / "judge-review.json", judge)
            context = memory.review_context(memory.build_history(current, memory.timestamp("2026-09-08T09:00:00+09:00"), read, simulated=True), "A", 2)
            self.assertEqual(context["active_investment"]["origin_status"], "unavailable")

    def test_confirmed_closed_day_counters_do_not_erase_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, day = Path(tmp), "2026-09-04"
            snapshot(root, "entry", day + "T09:00:00+09:00", 0, 0, 0, label="entry", order=("1", "buy"))
            snapshot(root, "filled", day + "T10:00:00+09:00", 2, 2, 0, fills=[fill("1", day, "buy", 2)])
            holiday = snapshot(root, "holiday", "2026-09-06T22:00:00+09:00", 2, 2, 0)
            write(holiday / "price-chart.json", {"generated_at": "2026-09-06T22:00:00+09:00",
                "market_open_day_checked": True, "market_open_day": False})
            current = snapshot(root, "current", "2026-09-07T09:00:00+09:00", 2, 0, 0)
            cutoff = memory.timestamp("2026-09-07T09:00:00+09:00")
            active = memory.build_history(current, cutoff, read)["symbols"]["A"]["active_investment"]
            self.assertEqual(active["entry"]["rationale"]["source_run_id"], "entry")
            # A balance discontinuity still invalidates attribution on a closed day.
            account = read(holiday / "account-before-order.json")
            account["symbols"][0]["current_live_holding_quantity"] = 3
            write(holiday / "account-before-order.json", account)
            self.assertIsNone(memory.build_history(current, cutoff, read)["symbols"]["A"]["active_investment"]["entry"])

    def test_missing_session_and_incomplete_quantity_history_do_not_revive_old_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, day = Path(tmp), "2026-09-07"
            snapshot(root, "entry", day + "T09:00:00+09:00", 0, 0, 0, label="entry", order=("1", "buy"))
            snapshot(root, "filled", day + "T10:00:00+09:00", 2, 2, 0, fills=[fill("1", day, "buy", 2)])
            current = snapshot(root, "gap", "2026-09-09T09:00:00+09:00", 2, 0, 0)
            context = memory.review_context(memory.build_history(current, memory.timestamp("2026-09-09T09:00:00+09:00"), read), "A", 2)
            self.assertIsNone(context["active_investment"]["entry"])
            broken = read(current / "account-before-order.json")
            broken["symbols"][0]["today_buy_quantity"] = 3
            write(current / "account-before-order.json", broken)
            context = memory.review_context(memory.build_history(current, memory.timestamp("2026-09-09T09:00:00+09:00"), read), "A", 2)
            self.assertEqual(context["status"], "incomplete")

    def test_new_broker_confirmation_invalidates_saved_judge_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, day = Path(tmp), "2026-09-07"
            source = snapshot(root, "entry", day + "T09:00:00+09:00", 0, 0, 0, label="entry", order=("1", "buy"))
            current = snapshot(root, "current", day + "T10:00:00+09:00", 2, 2, 0)
            write(current / "decision-brief.json", {"generated_at": day + "T10:00:00+09:00", "symbols": [{
                "symbol_id": "A", "account_exposure": {"current_live_holding_quantity": 2}}]})
            spec = {"run_id": "current", "started_at": day + "T10:00:00+09:00", "task_name": "judge",
                "stage": "judge-review", "agent_role": "judge", "output_dir": str(current), "workspace_dir": str(root),
                "symbol_ids": ["A"], "artifact_paths": {"decision_brief": str(current / "decision-brief.json"),
                    "persona": "prompts/judge.md", "review_format": "prompts/judge-review-format.md"}}
            run_subagent.write_review_input_slices(spec)
            first = run_subagent.spec_fingerprint(spec)
            run_subagent.write_review_input_slices(spec)
            self.assertEqual(run_subagent.spec_fingerprint(spec), first)
            execution = read(source / "execution.json")
            execution["orders"][0]["broker_reconciliation"] = {"status": "filled", "filled_quantity": 2,
                "observed_at": day + "T09:30:00+09:00"}
            write(source / "execution.json", execution)
            slices = run_subagent.write_review_input_slices(spec)
            self.assertNotEqual(run_subagent.spec_fingerprint(spec), first)
            self.assertEqual(read(Path(slices["review_core"]))["symbols"][0]["investment_context"]["status"], "reconciled")
