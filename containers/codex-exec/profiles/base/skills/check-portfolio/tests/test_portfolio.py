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
    retry_delays,
    retry_json,
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
    http_error = HTTPError("https://example.test/?CANO=12345678", 500, "Internal Server Error", hdrs=None, fp=None)
    event = request_failure_event(path=BALANCE_PATH, attempt=1, max_attempts=4, will_retry=True, next_delay_seconds=30, exc=http_error)
    rendered = json.dumps(event, ensure_ascii=False)
    assert event["http_status"] == 500
    assert event["next_delay_seconds"] == 30
    for forbidden in ("12345678", "Authorization", "Bearer", "appsecret", "APP_SECRET", "CANO", "ACNT_PRDT_CD", "raw_response"):
        assert forbidden not in rendered
    response_event = kis_response_failure_event(path=BALANCE_PATH, message="EGW00000: sanitized response failure", code="EGW00000")
    response_rendered = json.dumps(response_event, ensure_ascii=False)
    assert "raw_response" not in response_rendered
    assert "EGW00000" in response_rendered
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
