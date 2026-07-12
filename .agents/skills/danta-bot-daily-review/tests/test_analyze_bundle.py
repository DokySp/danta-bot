"""Tests for copied daily-trading bundle analysis."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from analyze_bundle import (  # noqa: E402
    SafeZip,
    analyze,
    build_fork_argv,
    collect_day,
    collect_git_context,
    collect_previous_days,
    prepare_output_dir,
    run_forks,
    shell_quote_command,
)


def run_self_tests() -> None:
    tests = [
        test_safe_zip_rejects_traversal,
        test_latest_and_explicit_run_selection,
        test_previous_day_and_telegram_summary,
        test_git_context_fallback,
        test_issue_count_shortfall_and_ranking,
        test_fork_argv_and_outputs,
        test_execute_fork_logging,
        test_force_keeps_unknown_files,
    ]
    for test in tests:
        test()
    print(f"self-test passed: {len(tests)} tests")


def make_fixture_zip(path: Path) -> None:
    def put_json(zipf: zipfile.ZipFile, name: str, data: Any) -> None:
        zipf.writestr(name, json.dumps(data, ensure_ascii=False))

    with zipfile.ZipFile(path, "w") as zipf:
        for run_id, order_count, submitted, status in [
            ("20260701T151500+0900-prev", 1, 0, "success"),
            ("20260702T090000+0900-old", 1, 0, "success"),
            ("20260702T151500+0900-new", 3, 1, "success"),
        ]:
            orders = []
            for index in range(order_count):
                result = "submitted" if index < submitted else "skipped"
                orders.append(
                    {
                        "symbol_id": f"00000{index}",
                        "symbol_name": f"Name {index}",
                        "direction": "sell" if result == "submitted" else "none",
                        "result": result,
                        "reason": "fixture",
                        "validated_order_quantity": 1 if result == "submitted" else 0,
                    }
                )
            put_json(
                zipf,
                f"codex-exec/reports/runs/{run_id}/execution.json",
                {"status": "success", "run_id": run_id, "orders": orders},
            )
            put_json(
                zipf,
                f"codex-exec/reports/runs/{run_id}/pipeline-summary.json",
                {"status": status, "run_id": run_id, "execution": {"status": status, "order_count": order_count}},
            )
            put_json(
                zipf,
                f"codex-exec/reports/runs/{run_id}/today-fills.json",
                {"status": "success", "fills": []},
            )
            put_json(
                zipf,
                f"codex-exec/reports/runs/{run_id}/account-before-order.json",
                {"status": "partial", "account_summary": {"total_pnl_amount": -1000}},
            )
            put_json(
                zipf,
                f"codex-exec/reports/runs/{run_id}/decision-brief.json",
                {
                    "status": "success",
                    "market_index_snapshot": {
                        "indexes": [
                            {"symbol": "KOSPI", "name": "KOSPI", "change_percent": -5.0, "status": "success"}
                        ]
                    },
                },
            )
            put_json(
                zipf,
                f"codex-exec/reports/runs/{run_id}/pipeline-command-log.json",
                {"status": "success", "commands": [{"returncode": 0}]},
            )
            zipf.writestr(f"codex-exec/reports/runs/{run_id}/main-events.jsonl", '{"type":"turn.completed"}\n')
            zipf.writestr(f"codex-exec/reports/runs/{run_id}/telegram-summary.txt", f"주문 수: {order_count}")
        zipf.writestr("codex-exec/reports/2026-07-02_포트폴리오.md", "portfolio")
        zipf.writestr(
            "telegram-gateway/memory/telegram-conversations/2026-07-02.jsonl",
            "\n".join(
                [
                    json.dumps({"direction": "inbound", "type": "telegram_message", "text": "/version"}, ensure_ascii=False),
                    json.dumps({"direction": "outbound", "type": "telegram_send", "text": "거래 주문 수: 3"}, ensure_ascii=False),
                ]
            )
            + "\n",
        )
        zipf.writestr(
            "telegram-gateway/memory/telegram-conversations/2026-07-01.jsonl",
            json.dumps({"direction": "outbound", "type": "telegram_send", "text": "주문 수: 1"}, ensure_ascii=False) + "\n",
        )


def test_safe_zip_rejects_traversal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.zip"
        with zipfile.ZipFile(path, "w") as zipf:
            zipf.writestr("../bad.txt", "bad")
        try:
            with SafeZip(path):
                raise AssertionError("unsafe zip was accepted")
        except ValueError as exc:
            assert "unsafe zip member" in str(exc)


def test_latest_and_explicit_run_selection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "fixture.zip"
        make_fixture_zip(zip_path)
        with SafeZip(zip_path) as bundle:
            day = collect_day(bundle, dt.date(2026, 7, 2), None)
            assert day["primary_run_id"] == "20260702T151500+0900-new"
            explicit = collect_day(bundle, dt.date(2026, 7, 2), "20260702T090000+0900-old")
            assert explicit["primary_run_id"] == "20260702T090000+0900-old"


def test_previous_day_and_telegram_summary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "fixture.zip"
        make_fixture_zip(zip_path)
        with SafeZip(zip_path) as bundle:
            previous = collect_previous_days(bundle, dt.date(2026, 7, 2), 1)
            assert len(previous) == 1
            assert previous[0]["telegram_conversation"]["present"] is True
            assert previous[0]["primary_run"]["logs"]["pipeline_command_log"]["present"] is True
            assert previous[0]["primary_run"]["logs"]["main_events"]["present"] is True


def test_git_context_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        context = collect_git_context(Path(tmp) / "missing")
        assert context["available"] is False


def test_issue_count_shortfall_and_ranking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "fixture.zip"
        output_dir = Path(tmp) / "out"
        make_fixture_zip(zip_path)
        args = argparse.Namespace(
            zip=str(zip_path),
            date=dt.date(2026, 7, 2),
            issue_count=2,
            repo=str(Path(tmp) / "missing"),
            output_dir=str(output_dir),
            previous_days=1,
            run_id=None,
            fork_session_id=None,
            execute_forks=False,
            dry_run=True,
            force=True,
        )
        result = analyze(args)
        issues = json.loads((output_dir / "issues.json").read_text(encoding="utf-8"))["issues"]
        assert result["issue_count"] == 2
        assert issues[0]["id"] == "order-count-contract"


def test_fork_argv_and_outputs() -> None:
    argv = build_fork_argv("session-1", "1번째 문제점에 대해서 구체적으로 설명해봐.")
    assert argv[:6] == ["codex", "fork", "--sandbox", "read-only", "--ask-for-approval", "never"]
    assert argv[6] == "session-1"
    quoted = shell_quote_command(argv)
    assert "codex fork" in quoted


def test_execute_fork_logging() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake = tmp_path / "codex-fake"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('forked session 12345678-1234-1234-1234-123456789abc')\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        issues = [
            {
                "id": "x",
                "rank": 1,
                "question_prompt": "1번째 문제점에 대해서 구체적으로 설명해봐.",
            }
        ]
        run_forks(tmp_path, issues, "session-1", codex_bin=str(fake))
        rows = [json.loads(line) for line in (tmp_path / "fork-results.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[0]["exit_code"] == 0
        assert rows[0]["detected_session_id"] == "12345678-1234-1234-1234-123456789abc"


def test_force_keeps_unknown_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp) / "out"
        output_dir.mkdir()
        keep = output_dir / "keep.txt"
        generated = output_dir / "analysis.md"
        keep.write_text("keep", encoding="utf-8")
        generated.write_text("old", encoding="utf-8")
        prepare_output_dir(output_dir, True)
        assert keep.exists()
        assert not generated.exists()


class AnalyzeBundleSelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        run_self_tests()
