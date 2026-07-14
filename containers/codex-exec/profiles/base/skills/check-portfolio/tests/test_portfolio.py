"""Tests for configured and held portfolio symbol collection."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
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


def command_self_test(_args: argparse.Namespace) -> int:
    assert symbols_from_text("005930 삼성전자, 0183J0 TIGER # 035420\nKR70183J0002") == ["005930", "0183J0", "KR70183J0002"]
    assert dedupe(["005930", "000660", "005930"]) == ["005930", "000660"]
    sample = {"output1": [{"pdno": "0183J0", "hldg_qty": "10"}, {"pdno": "000660", "hldg_qty": "0"}]}
    assert [normalize_holding_symbol(row) for row in output_rows(sample)] == ["0183J0", "000660"]
    assert positive_quantity(sample["output1"][0])
    assert not positive_quantity(sample["output1"][1])
    assert continuation_context({"ctx_area_fk100": "A", "ctx_area_nk100": "B"}) == ("A", "B")
    assert continuation_context({"output2": [{"ctx_area_fk100": "C", "ctx_area_nk100": "D"}]}) == ("C", "D")
    payload = compose_payload(["111111", "005930"], ["005930", "000660"], ["005930", "035420", "035420"])
    assert payload["holding"] == ["005930", "035420"]
    assert payload["universe"] == ["111111", "005930", "000660", "035420"]
    assert payload["portfolio_except"] == []
    excluded_payload = compose_payload(["111111", "005930"], ["005930", "000660"], ["005930", "035420"], ["005930", "000660"])
    assert excluded_payload["recommanded"] == ["111111"]
    assert excluded_payload["specified"] == []
    assert excluded_payload["holding"] == ["035420"]
    assert excluded_payload["universe"] == ["111111", "035420"]
    assert excluded_payload["portfolio_except"] == ["000660", "005930"]
    assert normalize_trading_env("acct") == "real"
    assert normalize_trading_env("paper") == "demo"
    assert balance_tr_id("demo") == "VTTC8434R"
    assert balance_tr_id("real") == "TTTC8434R"
    assert retry_delays(0) == []
    assert retry_delays(3) == [30, 30, 30]
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
    rendered = json.dumps(event, ensure_ascii=False)
    assert event["http_status"] == 500
    assert event["next_delay_seconds"] == 30
    assert event["response_request_id"] == "request-123"
    assert event["response_transaction_id"] == "TTTC8434R"
    assert event["response_kis_code"] == "EGW00123"
    assert event["response_message"] == "temporary failure <redacted>"
    for forbidden in ("12345678", "Authorization", "Bearer", "appsecret", "APP_SECRET", "CANO", "ACNT_PRDT_CD", "raw_response"):
        assert forbidden not in rendered
    success_event = request_success_event(
        path=BALANCE_PATH,
        attempt=3,
        max_attempts=4,
        response_headers={"gt_uid": "uid-123", "tr_id": "TTTC8434R"},
    )
    assert success_event["outcome"] == "success"
    assert success_event["recovered_after_failures"] == 2
    assert success_event["response_request_id"] == "uid-123"
    response_event = kis_response_failure_event(
        path=BALANCE_PATH,
        message="failure CANO=12345678 Authorization: Bearer top-secret-token",
        code="EGW00000",
    )
    response_rendered = json.dumps(response_event, ensure_ascii=False)
    assert response_event["response_kis_code"] == "EGW00000"
    assert response_event["response_message"] == "failure <redacted> <redacted>"
    assert "raw_response" not in response_rendered
    for forbidden in ("12345678", "top-secret-token", "Authorization", "Bearer", "CANO"):
        assert forbidden not in response_rendered
    assert safe_diagnostic_text("Bearer standalone-secret") == "<redacted>"
    original_request_json = portfolio_module.request_json
    original_sleep = time.sleep
    old_attempt_log_file = os.environ.get("CHECK_PORTFOLIO_ATTEMPT_LOG_FILE")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_log = Path(tmpdir) / "attempts.jsonl"
            os.environ["CHECK_PORTFOLIO_ATTEMPT_LOG_FILE"] = str(attempt_log)
            calls: list[int] = []
            sleeps: list[int] = []

            def failing_request_json(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, str]]:
                calls.append(1)
                raise TimeoutError()

            portfolio_module.request_json = failing_request_json
            time.sleep = lambda seconds: sleeps.append(seconds)  # type: ignore[assignment]
            try:
                with contextlib.redirect_stdout(io.StringIO()) as stdout:
                    retry_json("GET", BALANCE_PATH, headers={}, retries=2)
                raise AssertionError("retry_json should fail after retries")
            except RuntimeError as exc:
                assert "KIS request failed after retries" in str(exc)
            assert stdout.getvalue() == ""
            assert len(calls) == 3
            assert sleeps == [30, 30]
            lines = attempt_log.read_text(encoding="utf-8").splitlines()
            assert len(lines) == 3
            assert json.loads(lines[-1])["will_retry"] is False

            recovered_calls: list[int] = []

            def recovering_request_json(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, str]]:
                recovered_calls.append(1)
                if len(recovered_calls) == 1:
                    raise TimeoutError()
                return {"rt_cd": "0"}, {"gt_uid": "recovered-uid"}

            attempt_log.unlink()
            sleeps.clear()
            portfolio_module.request_json = recovering_request_json
            response, headers = retry_json("GET", BALANCE_PATH, headers={}, retries=2)
            assert response["rt_cd"] == "0"
            assert headers["gt_uid"] == "recovered-uid"
            assert sleeps == [30]
            recovered_lines = [json.loads(line) for line in attempt_log.read_text(encoding="utf-8").splitlines()]
            assert [item["outcome"] for item in recovered_lines] == ["failed", "success"]
            assert recovered_lines[-1]["attempt"] == 2
            assert recovered_lines[-1]["recovered_after_failures"] == 1
    finally:
        portfolio_module.request_json = original_request_json
        time.sleep = original_sleep  # type: ignore[assignment]
        if old_attempt_log_file is None:
            os.environ.pop("CHECK_PORTFOLIO_ATTEMPT_LOG_FILE", None)
        else:
            os.environ["CHECK_PORTFOLIO_ATTEMPT_LOG_FILE"] = old_attempt_log_file
    print("self-test ok")
    return 0


class PortfolioSelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        self.assertEqual(command_self_test(argparse.Namespace()), 0)
