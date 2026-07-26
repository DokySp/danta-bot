"""Tests for configured and held portfolio symbol collection.

`command_self_test` is the compatibility body invoked by the production
CLI's `self-test` command and must keep printing `"self-test ok"` and
returning `0`. Each logical block of the old monolithic self-test now
lives in its own `scenario_*` (setup + act) or `check_*` (single
assertion concern, reusable from a plain function or a `TestCase`
method) helper. `command_self_test` and the granular `TestCase` methods
below both call those helpers, so each behavior has exactly one
implementation. The wrapper-orchestration test mocks the helpers rather
than re-running every scenario, so discovery does not execute the real
work (including the retry/sleep scenarios) twice.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import portfolio as portfolio_module  # noqa: E402
from portfolio import (  # noqa: E402
    BALANCE_PATH,
    balance_tr_id,
    compose_payload,
    continuation_context,
    dedupe,
    kis_response_failure_event,
    normalize_holding_symbol,
    normalize_trading_env,
    output_rows,
    positive_quantity,
    request_failure_event,
    request_success_event,
    retry_delays,
    retry_json,
    safe_diagnostic_text,
    symbols_from_text,
)

HOLDING_SAMPLE = {"output1": [{"pdno": "0183J0", "hldg_qty": "10"}, {"pdno": "000660", "hldg_qty": "0"}]}


def check_symbols_from_text_parses_mixed_delimiters() -> None:
    parsed = symbols_from_text("005930 삼성전자, 0183J0 TIGER # 035420\nKR70183J0002")
    if parsed != ["005930", "0183J0", "KR70183J0002"]:
        raise AssertionError(f"unexpected parsed symbols: {parsed}")


def check_dedupe_preserves_first_occurrence_order() -> None:
    result = dedupe(["005930", "000660", "005930"])
    if result != ["005930", "000660"]:
        raise AssertionError(f"unexpected dedupe result: {result}")


def check_holding_rows_normalize_symbol_and_positive_quantity() -> None:
    symbols = [normalize_holding_symbol(row) for row in output_rows(HOLDING_SAMPLE)]
    if symbols != ["0183J0", "000660"]:
        raise AssertionError(f"unexpected normalized holding symbols: {symbols}")
    if not positive_quantity(HOLDING_SAMPLE["output1"][0]):
        raise AssertionError("expected first holding row to have positive quantity")
    if positive_quantity(HOLDING_SAMPLE["output1"][1]):
        raise AssertionError("expected second holding row to have zero quantity")


def check_continuation_context_reads_top_level_and_nested_output2() -> None:
    if continuation_context({"ctx_area_fk100": "A", "ctx_area_nk100": "B"}) != ("A", "B"):
        raise AssertionError("continuation_context did not read top-level ctx fields")
    if continuation_context({"output2": [{"ctx_area_fk100": "C", "ctx_area_nk100": "D"}]}) != ("C", "D"):
        raise AssertionError("continuation_context did not read nested output2 ctx fields")


def check_compose_payload_without_exclusions() -> None:
    payload = compose_payload(["111111", "005930"], ["005930", "000660"], ["005930", "035420", "035420"])
    if payload["holding"] != ["005930", "035420"]:
        raise AssertionError(f"unexpected holding: {payload['holding']}")
    if payload["universe"] != ["111111", "005930", "000660", "035420"]:
        raise AssertionError(f"unexpected universe: {payload['universe']}")
    if payload["portfolio_except"] != []:
        raise AssertionError(f"unexpected portfolio_except: {payload['portfolio_except']}")


def check_compose_payload_with_exclusions() -> None:
    payload = compose_payload(["111111", "005930"], ["005930", "000660"], ["005930", "035420"], ["005930", "000660"])
    if payload["recommanded"] != ["111111"]:
        raise AssertionError(f"unexpected recommanded: {payload['recommanded']}")
    if payload["specified"] != []:
        raise AssertionError(f"unexpected specified: {payload['specified']}")
    if payload["holding"] != ["035420"]:
        raise AssertionError(f"unexpected holding: {payload['holding']}")
    if payload["universe"] != ["111111", "035420"]:
        raise AssertionError(f"unexpected universe: {payload['universe']}")
    if payload["portfolio_except"] != ["000660", "005930"]:
        raise AssertionError(f"unexpected portfolio_except: {payload['portfolio_except']}")


def check_trading_env_and_balance_tr_id_mapping() -> None:
    if normalize_trading_env("acct") != "real":
        raise AssertionError("normalize_trading_env('acct') should be 'real'")
    if normalize_trading_env("paper") != "demo":
        raise AssertionError("normalize_trading_env('paper') should be 'demo'")
    if balance_tr_id("demo") != "VTTC8434R":
        raise AssertionError("balance_tr_id('demo') mismatch")
    if balance_tr_id("real") != "TTTC8434R":
        raise AssertionError("balance_tr_id('real') mismatch")


def check_retry_delays_schedule() -> None:
    if retry_delays(0) != []:
        raise AssertionError(f"unexpected retry_delays(0): {retry_delays(0)}")
    if retry_delays(3) != [30, 30, 30]:
        raise AssertionError(f"unexpected retry_delays(3): {retry_delays(3)}")


def scenario_http_error_failure_event() -> tuple[dict, str]:
    response_headers = Message()
    response_headers["x-request-id"] = "request-123"
    response_headers["tr_id"] = "TTTC8434R"
    http_error = HTTPError(
        "https://example.test/?CANO=12345678",
        500,
        "Internal Server Error",
        hdrs=response_headers,
        fp=BytesIO(
            json.dumps(
                {
                    "rt_cd": "1",
                    "msg_cd": "EGW00123",
                    "msg1": "temporary failure CANO=12345678",
                    "raw_response": "must not be retained",
                }
            ).encode("utf-8")
        ),
    )
    event = request_failure_event(path=BALANCE_PATH, attempt=1, max_attempts=4, will_retry=True, next_delay_seconds=30, exc=http_error)
    return event, json.dumps(event, ensure_ascii=False)


def check_http_error_failure_event_fields(event: dict) -> None:
    if event["http_status"] != 500:
        raise AssertionError(f"unexpected http_status: {event}")
    if event["next_delay_seconds"] != 30:
        raise AssertionError(f"unexpected next_delay_seconds: {event}")
    if event["response_request_id"] != "request-123":
        raise AssertionError(f"unexpected response_request_id: {event}")
    if event["response_transaction_id"] != "TTTC8434R":
        raise AssertionError(f"unexpected response_transaction_id: {event}")
    if event["response_kis_code"] != "EGW00123":
        raise AssertionError(f"unexpected response_kis_code: {event}")
    if event["response_message"] != "temporary failure <redacted>":
        raise AssertionError(f"unexpected response_message: {event}")


def check_http_error_failure_event_redacts_sensitive_values(rendered: str) -> None:
    for forbidden in ("12345678", "Authorization", "Bearer", "appsecret", "APP_SECRET", "CANO", "ACNT_PRDT_CD", "raw_response"):
        if forbidden in rendered:
            raise AssertionError(f"rendered failure event leaked {forbidden!r}: {rendered}")


def scenario_request_success_event() -> dict:
    return request_success_event(
        path=BALANCE_PATH,
        attempt=3,
        max_attempts=4,
        response_headers={"gt_uid": "uid-123", "tr_id": "TTTC8434R"},
    )


def check_request_success_event_fields(success_event: dict) -> None:
    if success_event["outcome"] != "success":
        raise AssertionError(f"unexpected outcome: {success_event}")
    if success_event["recovered_after_failures"] != 2:
        raise AssertionError(f"unexpected recovered_after_failures: {success_event}")
    if success_event["response_request_id"] != "uid-123":
        raise AssertionError(f"unexpected response_request_id: {success_event}")


def scenario_kis_response_failure_event() -> tuple[dict, str]:
    response_event = kis_response_failure_event(
        path=BALANCE_PATH,
        message="failure CANO=12345678 Authorization: Bearer top-secret-token",
        code="EGW00000",
    )
    return response_event, json.dumps(response_event, ensure_ascii=False)


def check_kis_response_failure_event_fields(response_event: dict) -> None:
    if response_event["response_kis_code"] != "EGW00000":
        raise AssertionError(f"unexpected response_kis_code: {response_event}")
    if response_event["response_message"] != "failure <redacted> <redacted>":
        raise AssertionError(f"unexpected response_message: {response_event}")


def check_kis_response_failure_event_redacts_sensitive_values(rendered: str) -> None:
    if "raw_response" in rendered:
        raise AssertionError(f"rendered response event unexpectedly contains raw_response: {rendered}")
    for forbidden in ("12345678", "top-secret-token", "Authorization", "Bearer", "CANO"):
        if forbidden in rendered:
            raise AssertionError(f"rendered response event leaked {forbidden!r}: {rendered}")


def check_safe_diagnostic_text_redacts_bearer_tokens() -> None:
    if safe_diagnostic_text("Bearer standalone-secret") != "<redacted>":
        raise AssertionError("safe_diagnostic_text did not redact a standalone Bearer token")


def scenario_retry_exhaustion(attempt_log: Path) -> dict:
    """retry_json should raise after exhausting retries against an always-failing request_json."""
    calls: list[int] = []
    sleeps: list[int] = []

    def failing_request_json(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, str]]:
        calls.append(1)
        raise TimeoutError()

    raised_message = ""
    stdout = io.StringIO()
    with patch.object(portfolio_module, "request_json", failing_request_json), patch.object(
        portfolio_module.time, "sleep", lambda seconds: sleeps.append(seconds)
    ), patch.dict("os.environ", {"CHECK_PORTFOLIO_ATTEMPT_LOG_FILE": str(attempt_log)}):
        with contextlib.redirect_stdout(stdout):
            try:
                retry_json("GET", BALANCE_PATH, headers={}, retries=2)
            except RuntimeError as exc:
                raised_message = str(exc)

    log_lines = attempt_log.read_text(encoding="utf-8").splitlines() if attempt_log.exists() else []
    return {
        "raised_message": raised_message,
        "stdout": stdout.getvalue(),
        "call_count": len(calls),
        "sleeps": sleeps,
        "log_lines": log_lines,
    }


def check_retry_exhaustion_raises_runtime_error(result: dict) -> None:
    if "KIS request failed after retries" not in result["raised_message"]:
        raise AssertionError(f"expected retry exhaustion error message, got: {result['raised_message']!r}")


def check_retry_exhaustion_writes_nothing_to_stdout(result: dict) -> None:
    if result["stdout"] != "":
        raise AssertionError(f"expected no stdout output, got: {result['stdout']!r}")


def check_retry_exhaustion_makes_expected_attempts_and_delays(result: dict) -> None:
    if result["call_count"] != 3:
        raise AssertionError(f"expected 3 attempts (1 + 2 retries), got: {result['call_count']}")
    if result["sleeps"] != [30, 30]:
        raise AssertionError(f"unexpected sleep delays: {result['sleeps']}")


def check_retry_exhaustion_logs_final_attempt_as_terminal(result: dict) -> None:
    if len(result["log_lines"]) != 3:
        raise AssertionError(f"expected 3 attempt log lines, got: {len(result['log_lines'])}")
    if json.loads(result["log_lines"][-1])["will_retry"] is not False:
        raise AssertionError("final attempt log entry should have will_retry=False")


def scenario_retry_recovery(attempt_log: Path) -> dict:
    """retry_json should return the successful response once request_json recovers."""
    recovered_calls: list[int] = []
    sleeps: list[int] = []

    def recovering_request_json(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, str]]:
        recovered_calls.append(1)
        if len(recovered_calls) == 1:
            raise TimeoutError()
        return {"rt_cd": "0"}, {"gt_uid": "recovered-uid"}

    with patch.object(portfolio_module, "request_json", recovering_request_json), patch.object(
        portfolio_module.time, "sleep", lambda seconds: sleeps.append(seconds)
    ), patch.dict("os.environ", {"CHECK_PORTFOLIO_ATTEMPT_LOG_FILE": str(attempt_log)}):
        response, headers = retry_json("GET", BALANCE_PATH, headers={}, retries=2)

    log_lines = [json.loads(line) for line in attempt_log.read_text(encoding="utf-8").splitlines()]
    return {"response": response, "headers": headers, "sleeps": sleeps, "log_lines": log_lines}


def check_retry_recovery_returns_successful_response(result: dict) -> None:
    if result["response"]["rt_cd"] != "0":
        raise AssertionError(f"unexpected recovered response: {result['response']}")
    if result["headers"]["gt_uid"] != "recovered-uid":
        raise AssertionError(f"unexpected recovered headers: {result['headers']}")


def check_retry_recovery_sleeps_once_before_success(result: dict) -> None:
    if result["sleeps"] != [30]:
        raise AssertionError(f"unexpected sleep delays before recovery: {result['sleeps']}")


def check_retry_recovery_logs_failure_then_success(result: dict) -> None:
    outcomes = [item["outcome"] for item in result["log_lines"]]
    if outcomes != ["failed", "success"]:
        raise AssertionError(f"unexpected attempt log outcomes: {outcomes}")
    last = result["log_lines"][-1]
    if last["attempt"] != 2:
        raise AssertionError(f"unexpected final attempt number: {last}")
    if last["recovered_after_failures"] != 1:
        raise AssertionError(f"unexpected recovered_after_failures: {last}")


def command_self_test(_args: argparse.Namespace) -> int:
    check_symbols_from_text_parses_mixed_delimiters()
    check_dedupe_preserves_first_occurrence_order()
    check_holding_rows_normalize_symbol_and_positive_quantity()
    check_continuation_context_reads_top_level_and_nested_output2()
    check_compose_payload_without_exclusions()
    check_compose_payload_with_exclusions()
    check_trading_env_and_balance_tr_id_mapping()
    check_retry_delays_schedule()

    failure_event, failure_rendered = scenario_http_error_failure_event()
    check_http_error_failure_event_fields(failure_event)
    check_http_error_failure_event_redacts_sensitive_values(failure_rendered)

    check_request_success_event_fields(scenario_request_success_event())

    response_event, response_rendered = scenario_kis_response_failure_event()
    check_kis_response_failure_event_fields(response_event)
    check_kis_response_failure_event_redacts_sensitive_values(response_rendered)

    check_safe_diagnostic_text_redacts_bearer_tokens()

    with tempfile.TemporaryDirectory() as tmpdir:
        exhaustion = scenario_retry_exhaustion(Path(tmpdir) / "attempts-exhaustion.jsonl")
        check_retry_exhaustion_raises_runtime_error(exhaustion)
        check_retry_exhaustion_writes_nothing_to_stdout(exhaustion)
        check_retry_exhaustion_makes_expected_attempts_and_delays(exhaustion)
        check_retry_exhaustion_logs_final_attempt_as_terminal(exhaustion)

        recovery = scenario_retry_recovery(Path(tmpdir) / "attempts-recovery.jsonl")
        check_retry_recovery_returns_successful_response(recovery)
        check_retry_recovery_sleeps_once_before_success(recovery)
        check_retry_recovery_logs_failure_then_success(recovery)

    print("self-test ok")
    return 0


class PortfolioSelfTest(unittest.TestCase):
    def test_self_test_suite_runs_every_check_and_reports_success(self) -> None:
        """Wrapper-orchestration check only: real behavior is covered by the
        granular tests below, so this mocks every helper instead of
        re-running the whole scenario (including the retry/sleep scenarios)
        a second time."""
        helper_names = [
            "check_symbols_from_text_parses_mixed_delimiters",
            "check_dedupe_preserves_first_occurrence_order",
            "check_holding_rows_normalize_symbol_and_positive_quantity",
            "check_continuation_context_reads_top_level_and_nested_output2",
            "check_compose_payload_without_exclusions",
            "check_compose_payload_with_exclusions",
            "check_trading_env_and_balance_tr_id_mapping",
            "check_retry_delays_schedule",
            "scenario_http_error_failure_event",
            "check_http_error_failure_event_fields",
            "check_http_error_failure_event_redacts_sensitive_values",
            "scenario_request_success_event",
            "check_request_success_event_fields",
            "scenario_kis_response_failure_event",
            "check_kis_response_failure_event_fields",
            "check_kis_response_failure_event_redacts_sensitive_values",
            "check_safe_diagnostic_text_redacts_bearer_tokens",
            "scenario_retry_exhaustion",
            "check_retry_exhaustion_raises_runtime_error",
            "check_retry_exhaustion_writes_nothing_to_stdout",
            "check_retry_exhaustion_makes_expected_attempts_and_delays",
            "check_retry_exhaustion_logs_final_attempt_as_terminal",
            "scenario_retry_recovery",
            "check_retry_recovery_returns_successful_response",
            "check_retry_recovery_sleeps_once_before_success",
            "check_retry_recovery_logs_failure_then_success",
        ]
        patchers = [patch(f"{__name__}.{name}", return_value=(None, None)) for name in helper_names]
        mocks = [patcher.start() for patcher in patchers]
        self.addCleanup(lambda: [patcher.stop() for patcher in patchers])

        result = command_self_test(argparse.Namespace())

        self.assertEqual(result, 0)
        for mock in mocks:
            mock.assert_called()


class PureHelperTest(unittest.TestCase):
    def test_symbols_from_text_parses_mixed_delimiters(self) -> None:
        check_symbols_from_text_parses_mixed_delimiters()

    def test_dedupe_preserves_first_occurrence_order(self) -> None:
        check_dedupe_preserves_first_occurrence_order()

    def test_holding_rows_normalize_symbol_and_positive_quantity(self) -> None:
        check_holding_rows_normalize_symbol_and_positive_quantity()

    def test_continuation_context_reads_top_level_and_nested_output2(self) -> None:
        check_continuation_context_reads_top_level_and_nested_output2()

    def test_compose_payload_without_exclusions(self) -> None:
        check_compose_payload_without_exclusions()

    def test_compose_payload_with_exclusions(self) -> None:
        check_compose_payload_with_exclusions()

    def test_trading_env_and_balance_tr_id_mapping(self) -> None:
        check_trading_env_and_balance_tr_id_mapping()

    def test_retry_delays_schedule(self) -> None:
        check_retry_delays_schedule()

    def test_safe_diagnostic_text_redacts_bearer_tokens(self) -> None:
        check_safe_diagnostic_text_redacts_bearer_tokens()


class DiagnosticEventTest(unittest.TestCase):
    def test_http_error_failure_event_fields(self) -> None:
        event, _ = scenario_http_error_failure_event()
        check_http_error_failure_event_fields(event)

    def test_http_error_failure_event_redacts_sensitive_values(self) -> None:
        _, rendered = scenario_http_error_failure_event()
        check_http_error_failure_event_redacts_sensitive_values(rendered)

    def test_request_success_event_fields(self) -> None:
        check_request_success_event_fields(scenario_request_success_event())

    def test_kis_response_failure_event_fields(self) -> None:
        event, _ = scenario_kis_response_failure_event()
        check_kis_response_failure_event_fields(event)

    def test_kis_response_failure_event_redacts_sensitive_values(self) -> None:
        _, rendered = scenario_kis_response_failure_event()
        check_kis_response_failure_event_redacts_sensitive_values(rendered)


class RetryJsonTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.temp_root = Path(self._temp_dir.name)

    def test_exhaustion_raises_runtime_error(self) -> None:
        result = scenario_retry_exhaustion(self.temp_root / "attempts.jsonl")
        check_retry_exhaustion_raises_runtime_error(result)

    def test_exhaustion_writes_nothing_to_stdout(self) -> None:
        result = scenario_retry_exhaustion(self.temp_root / "attempts.jsonl")
        check_retry_exhaustion_writes_nothing_to_stdout(result)

    def test_exhaustion_makes_expected_attempts_and_delays(self) -> None:
        result = scenario_retry_exhaustion(self.temp_root / "attempts.jsonl")
        check_retry_exhaustion_makes_expected_attempts_and_delays(result)

    def test_exhaustion_logs_final_attempt_as_terminal(self) -> None:
        result = scenario_retry_exhaustion(self.temp_root / "attempts.jsonl")
        check_retry_exhaustion_logs_final_attempt_as_terminal(result)

    def test_recovery_returns_successful_response(self) -> None:
        result = scenario_retry_recovery(self.temp_root / "attempts.jsonl")
        check_retry_recovery_returns_successful_response(result)

    def test_recovery_sleeps_once_before_success(self) -> None:
        result = scenario_retry_recovery(self.temp_root / "attempts.jsonl")
        check_retry_recovery_sleeps_once_before_success(result)

    def test_recovery_logs_failure_then_success(self) -> None:
        result = scenario_retry_recovery(self.temp_root / "attempts.jsonl")
        check_retry_recovery_logs_failure_then_success(result)


if __name__ == "__main__":
    unittest.main()
