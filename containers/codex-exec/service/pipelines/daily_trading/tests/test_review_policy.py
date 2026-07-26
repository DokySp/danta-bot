#!/usr/bin/env python3
"""Tests for the deterministic broker-preflight review policy.

Covers due-slot/first-run/fingerprint decision rules, safety gating, fixed
review time config parsing/validation, fingerprint canonicalization
(timestamp exclusion, ordering independence), and review-trigger state
persistence.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ..review_policy import (
    ReviewTriggerState,
    build_fingerprint_payload,
    changed_components,
    decide_full_review,
    due_slot,
    evaluate_safety,
    fingerprint_hash,
    load_full_review_times,
    load_review_trigger_state,
    parse_time_minutes,
    save_review_trigger_state,
    unexpected_non_universe_holdings,
)

TIMES = ["07:00", "09:05", "10:00", "11:30", "13:00", "14:00", "15:15"]


class ParseTimeAndDueSlotTest(unittest.TestCase):
    def test_parse_time_minutes(self) -> None:
        self.assertEqual(parse_time_minutes("00:00"), 0)
        self.assertEqual(parse_time_minutes("09:05"), 545)
        self.assertEqual(parse_time_minutes("23:59"), 1439)

    def test_parse_time_minutes_rejects_invalid(self) -> None:
        for bad in ("9:05", "24:00", "09:60", "abc", ""):
            with self.assertRaises(ValueError):
                parse_time_minutes(bad)

    def test_due_slot_before_first_time_is_none(self) -> None:
        self.assertIsNone(due_slot(TIMES, parse_time_minutes("06:59")))

    def test_due_slot_at_exact_fixed_time(self) -> None:
        self.assertEqual(due_slot(TIMES, parse_time_minutes("09:05")), "09:05")

    def test_due_slot_between_fixed_times_uses_latest_passed(self) -> None:
        self.assertEqual(due_slot(TIMES, parse_time_minutes("09:20")), "09:05")
        self.assertEqual(due_slot(TIMES, parse_time_minutes("09:40")), "09:05")
        self.assertEqual(due_slot(TIMES, parse_time_minutes("12:00")), "11:30")

    def test_due_slot_after_last_time(self) -> None:
        self.assertEqual(due_slot(TIMES, parse_time_minutes("23:00")), "15:15")


class LoadFullReviewTimesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "full-review-times.yaml"

    def test_valid_config_parses_in_order(self) -> None:
        self.path.write_text("full_review_times:\n  - \"07:00\"\n  - \"09:05\"\n")
        self.assertEqual(load_full_review_times(self.path), ["07:00", "09:05"])

    def test_rejects_unsorted_times(self) -> None:
        self.path.write_text("full_review_times:\n  - \"09:05\"\n  - \"07:00\"\n")
        with self.assertRaises(ValueError):
            load_full_review_times(self.path)

    def test_rejects_duplicate_times(self) -> None:
        self.path.write_text("full_review_times:\n  - \"07:00\"\n  - \"07:00\"\n")
        with self.assertRaises(ValueError):
            load_full_review_times(self.path)

    def test_rejects_empty_list(self) -> None:
        self.path.write_text("full_review_times: []\n")
        with self.assertRaises(ValueError):
            load_full_review_times(self.path)

    def test_rejects_non_mapping(self) -> None:
        self.path.write_text("- 1\n- 2\n")
        with self.assertRaises(ValueError):
            load_full_review_times(self.path)


class EvaluateSafetyTest(unittest.TestCase):
    def test_safe_when_all_checks_pass(self) -> None:
        result = evaluate_safety(
            lookup_complete=True, orderable_cash_amount=1_000_000, holding_state_issue_count=0, account_status="success"
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.reasons, [])

    def test_unsafe_on_partial_account_status(self) -> None:
        result = evaluate_safety(
            lookup_complete=True, orderable_cash_amount=1_000_000, holding_state_issue_count=0, account_status="partial"
        )
        self.assertFalse(result.safe)
        self.assertIn("account_lookup_failed", result.reasons)

    def test_unsafe_on_unknown_or_missing_account_status(self) -> None:
        for status in ("", "unknown", "not_run"):
            result = evaluate_safety(
                lookup_complete=True, orderable_cash_amount=1_000_000, holding_state_issue_count=0, account_status=status
            )
            self.assertFalse(result.safe, msg=f"account_status={status!r} should be unsafe")
            self.assertIn("account_lookup_failed", result.reasons)

    def test_unsafe_on_incomplete_lookup(self) -> None:
        result = evaluate_safety(
            lookup_complete=False, orderable_cash_amount=1_000_000, holding_state_issue_count=0, account_status="success"
        )
        self.assertFalse(result.safe)
        self.assertIn("order_lifecycle_lookup_incomplete", result.reasons)

    def test_unsafe_on_missing_orderable_cash(self) -> None:
        result = evaluate_safety(
            lookup_complete=True, orderable_cash_amount=None, holding_state_issue_count=0, account_status="success"
        )
        self.assertFalse(result.safe)
        self.assertIn("orderable_cash_unavailable", result.reasons)

    def test_unsafe_on_holding_state_issue(self) -> None:
        result = evaluate_safety(
            lookup_complete=True, orderable_cash_amount=1_000_000, holding_state_issue_count=1, account_status="success"
        )
        self.assertFalse(result.safe)
        self.assertIn("holding_state_issue_detected", result.reasons)

    def test_unsafe_on_failed_account_status_reports_all_applicable_reasons(self) -> None:
        result = evaluate_safety(
            lookup_complete=False, orderable_cash_amount=None, holding_state_issue_count=2, account_status="failed"
        )
        self.assertFalse(result.safe)
        self.assertEqual(
            set(result.reasons),
            {
                "account_lookup_failed",
                "order_lifecycle_lookup_incomplete",
                "orderable_cash_unavailable",
                "holding_state_issue_detected",
            },
        )

    def test_unsafe_on_incomplete_today_fills_lookup(self) -> None:
        result = evaluate_safety(
            lookup_complete=True,
            orderable_cash_amount=1_000_000,
            holding_state_issue_count=0,
            account_status="success",
            today_fills_complete=False,
        )
        self.assertFalse(result.safe)
        self.assertIn("today_fills_lookup_incomplete", result.reasons)

    def test_today_fills_complete_defaults_to_true_for_backward_compatible_callers(self) -> None:
        result = evaluate_safety(
            lookup_complete=True, orderable_cash_amount=1_000_000, holding_state_issue_count=0, account_status="success"
        )
        self.assertTrue(result.safe)

    def test_unsafe_on_unexpected_non_universe_symbols(self) -> None:
        result = evaluate_safety(
            lookup_complete=True,
            orderable_cash_amount=1_000_000,
            holding_state_issue_count=0,
            account_status="success",
            unexpected_non_universe_symbols=["999999"],
        )
        self.assertFalse(result.safe)
        self.assertIn("unexpected_non_universe_holding", result.reasons)


class UnexpectedNonUniverseHoldingsTest(unittest.TestCase):
    def test_live_holding_outside_universe_is_unexpected_by_default(self) -> None:
        account = {"non_universe_account_positions": [{"symbol_id": "999999", "current_live_holding_quantity": 5}]}
        self.assertEqual(unexpected_non_universe_holdings(account, []), ["999999"])

    def test_symbol_listed_in_portfolio_except_is_allowed(self) -> None:
        account = {"non_universe_account_positions": [{"symbol_id": "999999", "current_live_holding_quantity": 5}]}
        self.assertEqual(unexpected_non_universe_holdings(account, ["999999"]), [])

    def test_no_positions_is_empty(self) -> None:
        self.assertEqual(unexpected_non_universe_holdings({}, []), [])
        self.assertEqual(unexpected_non_universe_holdings({"non_universe_account_positions": []}, []), [])


class DecideFullReviewTest(unittest.TestCase):
    def test_manual_invocation_is_always_full_regardless_of_state(self) -> None:
        state = ReviewTriggerState(date="2026-07-27", fingerprint="same", last_satisfied_time="15:15")
        result = decide_full_review(
            now_minutes=parse_time_minutes("09:20"),
            full_review_times=TIMES,
            today="2026-07-27",
            state=state,
            fingerprint="same",
            invocation_type="manual",
        )
        self.assertEqual(result["decision"], "full")
        self.assertIn("manual_invocation", result["reasons"])

    def test_first_safe_run_of_day_is_full(self) -> None:
        state = ReviewTriggerState(date="2026-07-26", fingerprint="x", last_satisfied_time="15:15")
        result = decide_full_review(
            now_minutes=parse_time_minutes("07:00"),
            full_review_times=TIMES,
            today="2026-07-27",
            state=state,
            fingerprint="y",
            invocation_type="scheduled",
        )
        self.assertEqual(result["decision"], "full")
        self.assertIn("first_safe_run_of_day", result["reasons"])

    def test_due_fixed_time_not_yet_satisfied_is_full(self) -> None:
        state = ReviewTriggerState(date="2026-07-27", fingerprint="same", last_satisfied_time="07:00")
        result = decide_full_review(
            now_minutes=parse_time_minutes("09:05"),
            full_review_times=TIMES,
            today="2026-07-27",
            state=state,
            fingerprint="same",
            invocation_type="scheduled",
        )
        self.assertEqual(result["decision"], "full")
        self.assertIn("fixed_review_time_due", result["reasons"])
        self.assertEqual(result["due_slot"], "09:05")

    def test_missed_09_05_full_review_remains_due_at_next_invocation(self) -> None:
        # 09:05 never completed (crashed/failed), so last_satisfied_time is still 07:00.
        state = ReviewTriggerState(date="2026-07-27", fingerprint="same", last_satisfied_time="07:00")
        result = decide_full_review(
            now_minutes=parse_time_minutes("09:20"),
            full_review_times=TIMES,
            today="2026-07-27",
            state=state,
            fingerprint="same",
            invocation_type="scheduled",
        )
        self.assertEqual(result["decision"], "full")
        self.assertIn("fixed_review_time_due", result["reasons"])
        self.assertEqual(result["due_slot"], "09:05")

    def test_non_fixed_time_after_satisfied_slot_with_unchanged_fingerprint_is_skipped(self) -> None:
        state = ReviewTriggerState(date="2026-07-27", fingerprint="same", last_satisfied_time="09:05")
        result = decide_full_review(
            now_minutes=parse_time_minutes("09:20"),
            full_review_times=TIMES,
            today="2026-07-27",
            state=state,
            fingerprint="same",
            invocation_type="scheduled",
        )
        self.assertEqual(result["decision"], "skipped")
        self.assertEqual(result["reasons"], [])

    def test_fingerprint_change_forces_full_even_off_schedule(self) -> None:
        state = ReviewTriggerState(date="2026-07-27", fingerprint="old", last_satisfied_time="09:05")
        result = decide_full_review(
            now_minutes=parse_time_minutes("09:20"),
            full_review_times=TIMES,
            today="2026-07-27",
            state=state,
            fingerprint="new",
            invocation_type="scheduled",
        )
        self.assertEqual(result["decision"], "full")
        self.assertIn("broker_fingerprint_changed", result["reasons"])


class FingerprintPayloadTest(unittest.TestCase):
    def test_excludes_volatile_timestamps_and_is_order_independent(self) -> None:
        account_a = {
            "symbols": [
                {"symbol_id": "005930", "current_live_holding_quantity": 10, "today_buy_quantity": 0, "today_sell_quantity": 0},
                {"symbol_id": "000660", "current_live_holding_quantity": 0, "today_buy_quantity": 0, "today_sell_quantity": 0},
            ],
            "account_summary": {"orderable_cash_amount": 500000},
        }
        account_b = {
            "symbols": [
                {"symbol_id": "000660", "current_live_holding_quantity": 0, "today_buy_quantity": 0, "today_sell_quantity": 0},
                {"symbol_id": "005930", "current_live_holding_quantity": 10, "today_buy_quantity": 0, "today_sell_quantity": 0},
            ],
            "account_summary": {"orderable_cash_amount": 500000},
        }
        lifecycle = {
            "active_orders": [
                {"order_id": "1", "symbol_id": "005930", "direction": "buy", "order_price": 70000, "remaining_quantity": 5, "active_status": "active"}
            ]
        }
        today_fills_a = {"fills": [{"order_id": "f1", "symbol_id": "005930", "direction": "buy", "filled_quantity": 3, "filled_at": "2026-07-27T09:00:00+09:00"}]}
        today_fills_b = {"fills": [{"order_id": "f1", "symbol_id": "005930", "direction": "buy", "filled_quantity": 3, "filled_at": "2026-07-27T09:10:00+09:00"}]}

        payload_a = build_fingerprint_payload(
            universe=["005930", "000660"], account=account_a, lifecycle=lifecycle, today_fills=today_fills_a, config_fingerprint="cfg"
        )
        payload_b = build_fingerprint_payload(
            universe=["000660", "005930"], account=account_b, lifecycle=lifecycle, today_fills=today_fills_b, config_fingerprint="cfg"
        )
        self.assertEqual(fingerprint_hash(payload_a), fingerprint_hash(payload_b))

    def test_holding_quantity_change_changes_fingerprint(self) -> None:
        base_account = {
            "symbols": [{"symbol_id": "005930", "current_live_holding_quantity": 10, "today_buy_quantity": 0, "today_sell_quantity": 0}],
            "account_summary": {"orderable_cash_amount": 500000},
        }
        changed_account = {
            "symbols": [{"symbol_id": "005930", "current_live_holding_quantity": 15, "today_buy_quantity": 0, "today_sell_quantity": 0}],
            "account_summary": {"orderable_cash_amount": 500000},
        }
        payload_before = build_fingerprint_payload(universe=["005930"], account=base_account, lifecycle={}, today_fills={}, config_fingerprint="cfg")
        payload_after = build_fingerprint_payload(universe=["005930"], account=changed_account, lifecycle={}, today_fills={}, config_fingerprint="cfg")
        self.assertNotEqual(fingerprint_hash(payload_before), fingerprint_hash(payload_after))

    def test_config_fingerprint_change_changes_fingerprint(self) -> None:
        account = {"symbols": [], "account_summary": {"orderable_cash_amount": 1}}
        payload_a = build_fingerprint_payload(universe=[], account=account, lifecycle={}, today_fills={}, config_fingerprint="cfg-a")
        payload_b = build_fingerprint_payload(universe=[], account=account, lifecycle={}, today_fills={}, config_fingerprint="cfg-b")
        self.assertNotEqual(fingerprint_hash(payload_a), fingerprint_hash(payload_b))

    def test_inactive_lifecycle_rows_are_excluded_from_the_fingerprint(self) -> None:
        account = {"symbols": [], "account_summary": {"orderable_cash_amount": 1}}
        lifecycle_with_inactive = {
            "active_orders": [
                {"order_id": "1", "symbol_id": "005930", "direction": "buy", "order_price": 70000, "remaining_quantity": 0, "active_status": "inactive"}
            ]
        }
        payload_with_inactive = build_fingerprint_payload(
            universe=[], account=account, lifecycle=lifecycle_with_inactive, today_fills={}, config_fingerprint="cfg"
        )
        payload_without = build_fingerprint_payload(universe=[], account=account, lifecycle={}, today_fills={}, config_fingerprint="cfg")
        self.assertEqual(fingerprint_hash(payload_with_inactive), fingerprint_hash(payload_without))
        self.assertEqual(payload_with_inactive["active_orders"], [])

    def test_active_lifecycle_row_is_included_in_the_fingerprint(self) -> None:
        account = {"symbols": [], "account_summary": {"orderable_cash_amount": 1}}
        lifecycle_with_active = {
            "active_orders": [
                {"order_id": "1", "symbol_id": "005930", "direction": "buy", "order_price": 70000, "remaining_quantity": 5, "active_status": "active"}
            ]
        }
        payload_with_active = build_fingerprint_payload(
            universe=[], account=account, lifecycle=lifecycle_with_active, today_fills={}, config_fingerprint="cfg"
        )
        payload_without = build_fingerprint_payload(universe=[], account=account, lifecycle={}, today_fills={}, config_fingerprint="cfg")
        self.assertNotEqual(fingerprint_hash(payload_with_active), fingerprint_hash(payload_without))
        self.assertEqual(len(payload_with_active["active_orders"]), 1)


class ChangedComponentsTest(unittest.TestCase):
    def test_no_prior_payload_reports_no_changed_components(self) -> None:
        current = build_fingerprint_payload(universe=["005930"], account={}, lifecycle={}, today_fills={}, config_fingerprint="cfg")
        self.assertEqual(changed_components({}, current), [])
        self.assertEqual(changed_components(None, current), [])

    def test_identifies_exactly_the_components_that_differ(self) -> None:
        prior = build_fingerprint_payload(
            universe=["005930"],
            account={"symbols": [{"symbol_id": "005930", "current_live_holding_quantity": 10}], "account_summary": {"orderable_cash_amount": 500000}},
            lifecycle={},
            today_fills={},
            config_fingerprint="cfg",
        )
        current = build_fingerprint_payload(
            universe=["005930"],
            account={"symbols": [{"symbol_id": "005930", "current_live_holding_quantity": 15}], "account_summary": {"orderable_cash_amount": 400000}},
            lifecycle={},
            today_fills={},
            config_fingerprint="cfg",
        )
        self.assertEqual(set(changed_components(prior, current)), {"holdings", "cash"})

    def test_unchanged_payloads_report_no_changed_components(self) -> None:
        payload = build_fingerprint_payload(
            universe=["005930"],
            account={"symbols": [{"symbol_id": "005930", "current_live_holding_quantity": 10}], "account_summary": {"orderable_cash_amount": 500000}},
            lifecycle={},
            today_fills={},
            config_fingerprint="cfg",
        )
        self.assertEqual(changed_components(payload, dict(payload)), [])


class ReviewTriggerStatePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "review-trigger-state-real.json"

    def test_missing_state_file_loads_as_empty_state(self) -> None:
        state = load_review_trigger_state(self.path)
        self.assertEqual(state, ReviewTriggerState())

    def test_round_trips_through_save_and_load(self) -> None:
        state = ReviewTriggerState(date="2026-07-27", fingerprint="abc123", last_satisfied_time="09:05")
        save_review_trigger_state(self.path, state)
        loaded = load_review_trigger_state(self.path)
        self.assertEqual(loaded, state)

    def test_corrupt_state_file_loads_as_empty_state(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("not json", encoding="utf-8")
        state = load_review_trigger_state(self.path)
        self.assertEqual(state, ReviewTriggerState())

    def test_schema_version_1_state_without_fingerprint_payload_loads_as_no_prior_payload(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            '{"schema_version": "1", "date": "2026-07-27", "fingerprint": "abc123", "last_satisfied_time": "09:05"}',
            encoding="utf-8",
        )
        state = load_review_trigger_state(self.path)
        self.assertEqual(state.date, "2026-07-27")
        self.assertEqual(state.fingerprint, "abc123")
        self.assertEqual(state.last_satisfied_time, "09:05")
        self.assertEqual(state.fingerprint_payload, {})

    def test_round_trips_fingerprint_payload(self) -> None:
        payload = {"holdings": [{"symbol_id": "005930", "current_live_holding_quantity": 10}]}
        state = ReviewTriggerState(date="2026-07-27", fingerprint="abc123", last_satisfied_time="09:05", fingerprint_payload=payload)
        save_review_trigger_state(self.path, state)
        loaded = load_review_trigger_state(self.path)
        self.assertEqual(loaded.fingerprint_payload, payload)


if __name__ == "__main__":
    unittest.main()
