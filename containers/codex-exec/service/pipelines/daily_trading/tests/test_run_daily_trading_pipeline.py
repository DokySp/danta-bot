#!/usr/bin/env python3
"""Offline smoke tests for the daily-trading pipeline."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ..scripts.run_daily_trading_pipeline import (
    DEBATE_SIDES,
    ORDER_PATH_AUTO,
    STRATEGY_POLICY_CONFIG_ENV,
    STRATEGY_POLICY_CONFIG_FILENAME,
    Pipeline,
    as_int,
    cache_coverage,
    cache_evidence_counts,
    evaluate_rebuttal_2_gate,
    file_sha256,
    load_json,
    load_json_if_exists,
    now_iso,
    news_cache_coverage,
    news_cache_evidence_counts,
    repo_root_from,
    resolve_order_path,
    resolve_strategy_policy_config_path,
    script_dir,
    symbol_key,
    write_json,
)


def fake_codex_script(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import re
import sys
from pathlib import Path

output_path = None
for index, arg in enumerate(sys.argv):
    if arg == "-o" and index + 1 < len(sys.argv):
        output_path = Path(sys.argv[index + 1])
        break
if output_path is None:
    print("missing -o", file=sys.stderr)
    sys.exit(2)
task_name = output_path.name.removesuffix(".raw.txt")
fail_once_tasks = {value.strip() for value in os.environ.get("FAKE_CODEX_FAIL_ONCE_TASKS", "").split(",") if value.strip()}
invalid_rebuttal_2_tasks = {
    value.strip()
    for value in os.environ.get("FAKE_CODEX_INVALID_REBUTTAL_2_TASKS", "").split(",")
    if value.strip()
}
fail_state_dir = Path(os.environ.get("FAKE_CODEX_FAIL_STATE_DIR", output_path.parent))
fail_marker = fail_state_dir / f"{task_name}.failed-once"
fail_output = task_name in fail_once_tasks and not fail_marker.exists()
if fail_output:
    fail_marker.parent.mkdir(parents=True, exist_ok=True)
    fail_marker.write_text("failed", encoding="utf-8")

prompt = sys.argv[-1] if sys.argv else ""
if "stage: judge-debate" in prompt:
    stage = "judge-debate"
elif "stage: judge-review" in prompt:
    stage = "judge-review"
else:
    stage = "analyst-review"
agent_role = ""
role_match = re.search(r"agent_role:\\s*([^\\n]+)", prompt)
if role_match:
    agent_role = role_match.group(1).strip()
match = re.search(r"symbol_ids:\\s*([^\\n]+)", prompt)
symbols = [item.strip() for item in (match.group(1).split(",") if match else ["005930"]) if item.strip()]
rows = []
for index, symbol in enumerate(symbols, start=1):
    if stage == "judge-debate":
        phase = next((value for value in ("opening", "rebuttal-1", "rebuttal-2") if f"debate_phase: {value}" in prompt), "opening")
        side = "bull" if agent_role == "debate-bull" else "bear"
        opponent = "bear" if side == "bull" else "bull"
        target_phase = "opening" if phase == "rebuttal-1" else "rebuttal-1"
        kind = {"opening": "claim", "rebuttal-1": "rebuttal", "rebuttal-2": "closing"}[phase]
        decision = {}
        if phase != "opening":
            action = "hold"
            target_quantity = 0
            if os.environ.get("FAKE_CODEX_REBUTTAL_1_CONFLICT") == "1" and side == "bull":
                action = "buy"
                target_quantity = 1
            decision = {
                "recommended_action": action,
                "target_holding_quantity": target_quantity
            }
            if phase == "rebuttal-2" and task_name in invalid_rebuttal_2_tasks:
                decision.pop("recommended_action", None)
        rows.append({
            "symbol_id": symbol,
            "symbol_name": symbol,
            "arguments": [{
                "argument_id": f"{symbol}-{side}-{phase}-1",
                "kind": kind,
                "targets": [] if phase == "opening" else [f"{symbol}-{opponent}-{target_phase}-1"],
                "statement": f"{side} {phase} self-test",
                "evidence_refs": [f"decision-brief:{symbol}:price"]
            }],
            "concessions": [],
            "unresolved_conflicts": [] if phase != "rebuttal-2" else ["self-test conflict"],
            "final_position": "" if phase == "opening" else f"{side} final position",
            **decision
        })
    elif stage == "judge-review":
        rows.append({
            "symbol_id": symbol,
            "symbol_name": symbol,
            "target_position_value_krw": 70000 if symbol == "005930" else 0,
            "relative_attractiveness_rank": index,
            "reason_code": "hold_neutral",
            "one_line_reason": "self-test"
        })
    else:
        row = {"symbol_id": symbol, "symbol_name": symbol}
        if agent_role == "analyst-quality-risk":
            row["views"] = {
                "analyst-quality-value": {
                    "score": 8 if symbol == "005930" else 5,
                    "confidence": 8,
                    "reason_code": "buy_candidate" if symbol == "005930" else "hold_neutral",
                    "one_line_reason": "quality self-test",
                    "missing_data": []
                },
                "analyst-risk-allocation": {
                    "score": 8 if symbol == "005930" else 5,
                    "confidence": 8,
                    "reason_code": "buy_candidate" if symbol == "005930" else "hold_neutral",
                    "one_line_reason": "risk self-test",
                    "missing_data": []
                }
            }
        elif agent_role == "analyst-momentum-news":
            row["views"] = {
                "analyst-momentum-cycle": {
                    "score": 8 if symbol == "005930" else 5,
                    "confidence": 8,
                    "reason_code": "buy_candidate" if symbol == "005930" else "hold_neutral",
                    "one_line_reason": "momentum self-test",
                    "missing_data": []
                },
                "analyst-news-flow": {
                    "score": 5,
                    "confidence": 5,
                    "reason_code": "no_news_excluded",
                    "one_line_reason": "뉴스 정보가 없어 평균에서 제외",
                    "missing_data": ["news_summary"]
                }
            }
        else:
            row.update({
                "score": 8 if symbol == "005930" else 5,
                "confidence": 8,
                "reason_code": "buy_candidate" if symbol == "005930" else "hold_neutral",
                "one_line_reason": "self-test",
                "missing_data": []
            })
        rows.append(row)
if stage == "judge-debate":
    payload = {
        "stage": stage,
        "phase": phase,
        "side": side,
        "symbols": rows,
        "errors": []
    }
else:
    payload = {"stage": stage, "agent_id": "fake", "persona": "fake", "human_markdown_path": "", "symbols": rows, "errors": []}
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text("" if fail_output else json.dumps(payload, ensure_ascii=False), encoding="utf-8")
if "--json" in sys.argv:
    if agent_role == "debate-bull":
        thread_id = "00000000-0000-4000-8000-000000000001"
    elif agent_role == "debate-bear":
        thread_id = "00000000-0000-4000-8000-000000000002"
    else:
        thread_id = "00000000-0000-4000-8000-000000000099"
    thread_id_overrides = json.loads(os.environ.get("FAKE_CODEX_THREAD_ID_OVERRIDES", "{}"))
    if isinstance(thread_id_overrides, dict) and task_name in thread_id_overrides:
        thread_id = str(thread_id_overrides[task_name])
    print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
    print(json.dumps({
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 50,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                    "total_tokens": 120
                }
            }
        }
    }))
sys.exit(0)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def write_self_test_fixtures(workspace: Path, run_dir: Path) -> Path:
    portfolio_path = workspace / "portfolio.json"
    write_json(
        portfolio_path,
        {
            "recommanded": [],
            "recommended": [],
            "specified": ["005930", "000660"],
            "holding": ["005930"],
            "universe": ["005930", "000660"],
        },
    )
    write_json(
        run_dir / "price-chart.json",
        {
            "schema_version": "1",
            "run_id": "pipeline-self-test",
            "started_at": "2026-06-18T09:00:00+09:00",
            "status": "success",
            "symbols": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "product_type": "stock",
                    "eligible_for_review": True,
                    "price": {"current_or_last": 70000, "observed_at": "2026-06-18T09:00:00+09:00", "snapshot_mode": "live"},
                    "local_signals": [],
                    "required_missing": [],
                    "errors": [],
                },
                {
                    "symbol_id": "000660",
                    "symbol_name": "SK하이닉스",
                    "product_type": "stock",
                    "eligible_for_review": True,
                    "price": {"current_or_last": 200000, "observed_at": "2026-06-18T09:00:00+09:00", "snapshot_mode": "live"},
                    "local_signals": [],
                    "required_missing": [],
                    "errors": [],
                },
            ],
        },
    )
    write_json(
        run_dir / "account-before-order.json",
        {
            "schema_version": "1",
            "run_id": "pipeline-self-test",
            "started_at": "2026-06-18T09:00:00+09:00",
            "status": "success",
            "active_order_lookup_performed": False,
            "order_available_lookup_performed": False,
            "account_summary": {"cash_amount": 1000000, "total_evaluation_amount": 1500000},
            "active_orders": [],
            "symbols": [
                {"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 0, "current_price": None},
                {"symbol_id": "000660", "symbol_name": "SK하이닉스", "current_live_holding_quantity": 0, "current_price": 200000},
            ],
        },
    )
    write_json(
        run_dir / "account-asset-snapshot.json",
        {
            "schema_version": "1",
            "run_id": "pipeline-self-test",
            "started_at": "2026-06-18T09:00:00+09:00",
            "generated_at": "2026-06-18T09:00:05+09:00",
            "observed_at": "2026-06-18T09:00:05+09:00",
            "stage": "account-asset-snapshot",
            "status": "success",
            "skipped": False,
            "source_api": "inquire_balance",
            "tot_asst_amt": 20000000,
            "tot_dncl_amt": 1000000,
            "evlu_amt_smtl": 19000000,
            "pchs_amt_smtl": 18000000,
            "evlu_pfls_amt_smtl": 1000000,
            "ovrs_stck_evlu_amt1": 0,
            "account_asset_summary": {
                "source_api": "inquire_balance",
                "observed_at": "2026-06-18T09:00:05+09:00",
                "total_asset_amount": 20000000,
                "cash_deposit_amount": 1000000,
                "evaluated_asset_amount": 19000000,
                "purchase_amount": 18000000,
                "evaluation_pnl_amount": 1000000,
                "evaluation_pnl_rate": 1000000 / 18000000,
                "overseas_stock_evaluation_amount": 0,
            },
            "errors": [],
        },
    )
    write_json(
        run_dir / "today-fills.json",
        {
            "schema_version": "1",
            "run_id": "pipeline-self-test",
            "started_at": "2026-06-18T09:00:00+09:00",
            "stage": "today-fills",
            "status": "success",
            "skipped": False,
            "fill_scope": "account",
            "symbols": [{"symbol_id": "005930"}, {"symbol_id": "000660"}],
            "fills": [],
            "errors": [],
        },
    )
    return portfolio_path


def run_self_test() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp_name:
        workspace = Path(tmp_name)
        run_dir = workspace / "reports" / "runs" / "pipeline-self-test"
        portfolio_path = write_self_test_fixtures(workspace, run_dir)
        incomplete_cache = workspace / "incomplete-cache.json"
        write_json(incomplete_cache, {"symbols": {"005930": {"items": ["probe"]}}})
        covered, missing = cache_coverage(incomplete_cache, ["005930", "000660"])
        if covered or missing != ["000660"]:
            failures.append(f"cache coverage check failed: covered={covered}, missing={missing}")
        empty_payload_cache = workspace / "empty-payload-cache.yaml"
        empty_payload_cache.write_text('date: "2026-06-18"\nsymbols:\n  "005930": {}\n  "000660": []\n', encoding="utf-8")
        covered, missing = cache_coverage(empty_payload_cache, ["005930", "000660"])
        if covered or missing != ["000660", "005930"]:
            failures.append(f"empty payload cache should be incomplete: covered={covered}, missing={missing}")
        empty_news_cache = workspace / "empty-news-cache.yaml"
        empty_news_cache.write_text(
            'date: "2026-06-18"\nsymbols:\n  "005930":\n    articles:\n      - article_date: ""\n        sentiment: neutral\n        content: ""\n',
            encoding="utf-8",
        )
        covered, missing = cache_coverage(empty_news_cache, ["005930"])
        if covered or missing != ["005930"]:
            failures.append(f"empty news article should be incomplete: covered={covered}, missing={missing}")
        no_news_cache = workspace / "no-news-cache.yaml"
        no_news_cache.write_text(
            'date: "2026-06-18"\nsymbols:\n  "005930":\n    articles:\n      - article_date: ""\n        sentiment: neutral\n        content: "2026-06-18 기준 수집된 뉴스가 없습니다."\n',
            encoding="utf-8",
        )
        covered, missing = cache_coverage(no_news_cache, ["005930"])
        if covered or missing != ["005930"]:
            failures.append(f"no-news placeholder should be incomplete: covered={covered}, missing={missing}")
        no_news_counts = cache_evidence_counts(no_news_cache, ["005930"])
        if no_news_counts.get("present_symbol_count") != 1 or no_news_counts.get("usable_symbol_count") != 0:
            failures.append(f"no-news cache counts did not distinguish present from usable: {no_news_counts}")
        stale_news_cache = workspace / "stale-news-cache.yaml"
        stale_news_cache.write_text(
            'date: "2026-06-18"\nsymbols:\n  "005930":\n    articles:\n      - article_date: "2020-01-01"\n        sentiment: neutral\n        content: "old article"\n',
            encoding="utf-8",
        )
        covered, missing = news_cache_coverage(stale_news_cache, ["005930"], "2026-06-18")
        if covered or missing != ["005930"]:
            failures.append(f"stale-only news cache should not satisfy same-date coverage: covered={covered}, missing={missing}")
        stale_news_counts = news_cache_evidence_counts(stale_news_cache, ["005930"], "2026-06-18")
        if stale_news_counts.get("present_symbol_count") != 1 or stale_news_counts.get("usable_symbol_count") != 0:
            failures.append(f"stale-only news cache counts should distinguish present from usable: {stale_news_counts}")
        fresh_news_cache = workspace / "fresh-news-cache.yaml"
        fresh_news_cache.write_text(
            'date: "2026-06-18"\nsymbols:\n  "005930":\n    articles:\n      - article_date: "2026-06-18T09:30:00+09:00"\n        sentiment: positive\n        content: "fresh article"\n',
            encoding="utf-8",
        )
        covered, missing = news_cache_coverage(fresh_news_cache, ["005930"], "2026-06-18")
        if not covered or missing:
            failures.append(f"matching-date news cache should satisfy coverage: covered={covered}, missing={missing}")
        if resolve_order_path(ORDER_PATH_AUTO, "2026-06-18T09:00:00+09:00") != ("immediate", "auto_regular_session"):
            failures.append("auto order path did not select immediate during regular KST session")
        if resolve_order_path(ORDER_PATH_AUTO, "2026-06-18T07:00:00+09:00") != ("reservation", "auto_reservation_session"):
            failures.append("auto order path did not select reservation during KIS reservation session")
        if resolve_order_path(ORDER_PATH_AUTO, "2026-06-20T10:00:00+09:00") != ("reservation", "auto_closed_weekend"):
            failures.append("auto order path did not select reservation during weekend closed session")
        if resolve_order_path("reservation", "2026-06-18T10:00:00+09:00") != ("reservation", "explicit"):
            failures.append("explicit reservation order path was not preserved")
        if resolve_order_path("immediate", "2026-06-18T07:00:00+09:00") != ("immediate", "explicit"):
            failures.append("explicit immediate order path was not preserved")
        try:
            resolve_order_path(ORDER_PATH_AUTO, "2026-06-18T08:00:00+09:00")
            failures.append("auto order path should reject unsupported KIS order window")
        except ValueError:
            pass
        etf_probe_dir = workspace / "reports" / "runs" / "etf-cache-probe"
        write_json(
            etf_probe_dir / "price-chart.json",
            {
                "symbols": [
                    {"symbol_id": "069500", "symbol_name": "KODEX 200", "product_type": "etf"},
                ]
            },
        )
        stale_etf_cache = workspace / "stale-etf-financial.yaml"
        stale_etf_cache.write_text('date: "2026-06-18"\nsymbols:\n  "069500":\n    items:\n      - "price only"\n', encoding="utf-8")
        fresh_etf_cache = workspace / "fresh-etf-financial.yaml"
        fresh_etf_cache.write_text(
            'date: "2026-06-18"\nsymbols:\n  "069500":\n    KODEX 200:\n      ETF/ETN 현재가:\n        응답:\n          - nav: "10000"\n      NAV 비교추이(종목):\n        NAV 비교 요약:\n          - nav: "10000"\n',
            encoding="utf-8",
        )
        etf_probe = Pipeline(
            argparse.Namespace(
                command="run",
                workspace_dir=str(workspace),
                output_dir=str(etf_probe_dir),
                run_id="etf-cache-probe",
                started_at="2026-06-18T09:00:00+09:00",
                env="acct",
                request_type="analysis",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                news_cache_path="",
                main_events="",
                date="2026-06-18",
                reuse_existing_artifacts=True,
                skip_account=False,
                max_workers=3,
            )
        )
        if etf_probe.covered_cache_path("financial", str(stale_etf_cache), ["069500"], detail="stale etf cache"):
            failures.append("ETF financial cache without NAV evidence should not be accepted as covered")
        if not etf_probe.covered_cache_path("financial", str(fresh_etf_cache), ["069500"], detail="fresh etf cache"):
            failures.append("ETF financial cache with NAV evidence should be accepted as covered")
        if etf_probe.covered_cache_path("news", str(stale_news_cache), ["005930"], detail="stale news cache"):
            failures.append("stale-only news cache should not skip same-date news collection")
        if not etf_probe.covered_cache_path("news", str(fresh_news_cache), ["005930"], detail="fresh news cache"):
            failures.append("matching-date news cache should be accepted as covered")
        stage_status_probe = Pipeline(
            argparse.Namespace(
                command="run",
                workspace_dir=str(workspace),
                output_dir=str(workspace / "reports" / "runs" / "status-probe"),
                run_id="status-probe",
                started_at="2026-06-18T09:00:00+09:00",
                env="acct",
                request_type="analysis",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                news_cache_path="",
                main_events="",
                date="2026-06-18",
                reuse_existing_artifacts=True,
                skip_account=False,
                max_workers=3,
            )
        )
        stage_status_probe.add_stage("optional-noop", "skipped", required=False)
        if stage_status_probe.pipeline_status() != "success":
            failures.append(f"optional skipped stage changed pipeline status: {stage_status_probe.pipeline_status()}")
        old_financial_memory = os.environ.get("COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR")
        old_news_memory = os.environ.get("COLLECT_NEWS_INFORMATION_MEMORY_DIR")
        try:
            env_financial_dir = workspace / "env-financial-cache"
            env_news_dir = workspace / "env-news-cache"
            env_financial_dir.mkdir(parents=True, exist_ok=True)
            env_news_dir.mkdir(parents=True, exist_ok=True)
            (env_financial_dir / "financial-2026-06-18.yaml").write_text('date: "2026-06-18"\nsymbols: {}\n', encoding="utf-8")
            (env_news_dir / "news-2026-06-18.yaml").write_text('date: "2026-06-18"\nsymbols: {}\n', encoding="utf-8")
            os.environ["COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR"] = str(env_financial_dir)
            os.environ["COLLECT_NEWS_INFORMATION_MEMORY_DIR"] = str(env_news_dir)
            if Path(stage_status_probe.default_cache_path("financial")).parent != env_financial_dir:
                failures.append("financial env memory dir was not preferred")
            if Path(stage_status_probe.default_cache_path("news")).parent != env_news_dir:
                failures.append("news env memory dir was not preferred")
        finally:
            if old_financial_memory is None:
                os.environ.pop("COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR", None)
            else:
                os.environ["COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR"] = old_financial_memory
            if old_news_memory is None:
                os.environ.pop("COLLECT_NEWS_INFORMATION_MEMORY_DIR", None)
            else:
                os.environ["COLLECT_NEWS_INFORMATION_MEMORY_DIR"] = old_news_memory

        old_codex_home_env = os.environ.get("CODEX_HOME")
        try:
            codex_home = workspace / "codex-home"
            installed_financial_script = codex_home / "skills" / "collect-financial-information" / "scripts" / "financial_cache.py"
            installed_financial_script.parent.mkdir(parents=True, exist_ok=True)
            installed_financial_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            os.environ["CODEX_HOME"] = str(codex_home)
            resolved_installed_script = stage_status_probe.optional_cache_script("financial")
            if resolved_installed_script != installed_financial_script:
                failures.append(f"installed financial cache script was not resolved via CODEX_HOME: {resolved_installed_script}")
        finally:
            if old_codex_home_env is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old_codex_home_env

        class OptionalCacheProbePipeline(Pipeline):
            def __init__(self, args: argparse.Namespace) -> None:
                super().__init__(args)
                self.cache_attempts = 0
                self.get_attempts = 0

            def optional_cache_script(self, domain: str) -> Path:
                return workspace / f"{domain}_cache_probe.py"

            def run_cmd(self, stage: str, cmd: list[str], *, required: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                if stage.endswith("-cache-get"):
                    self.get_attempts += 1
                    domain = stage.removesuffix("-cache-get")
                    subdir = "collect-financial-information" if domain == "financial" else "collect-news-information"
                    prefix = "financial" if domain == "financial" else "news"
                    path = self.workspace_dir / "memory" / subdir / f"{prefix}-2026-06-18.yaml"
                    stdout = str(path) if path.exists() else "missing cache"
                    self.logs.append(
                        {
                            "stage": stage,
                            "command": cmd,
                            "returncode": 0 if path.exists() else 1,
                            "stdout_tail": stdout,
                            "stderr_tail": "",
                            "required": required,
                            "recorded_at": now_iso(),
                        }
                    )
                    write_json(self.command_log_path, {"commands": self.logs})
                    return subprocess.CompletedProcess(cmd, 0 if path.exists() else 1, stdout=stdout, stderr="")
                if stage.endswith("-cache-collect"):
                    self.cache_attempts += 1
                    domain = stage.removesuffix("-cache-collect")
                    subdir = "collect-financial-information" if domain == "financial" else "collect-news-information"
                    prefix = "financial" if domain == "financial" else "news"
                    path = self.workspace_dir / "memory" / subdir / f"{prefix}-2026-06-18.yaml"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        'date: "2026-06-18"\nsource: kis_open_api\nsymbols:\n  "005930":\n    items:\n      - "probe"\n',
                        encoding="utf-8",
                    )
                    self.logs.append(
                        {
                            "stage": stage,
                            "command": cmd,
                            "returncode": 0,
                            "stdout_tail": str(path),
                            "stderr_tail": "",
                            "required": required,
                            "recorded_at": now_iso(),
                        }
                    )
                    write_json(self.command_log_path, {"commands": self.logs})
                    return subprocess.CompletedProcess(cmd, 0, stdout=str(path), stderr="")
                return super().run_cmd(stage, cmd, required=required, env=env)

        optional_cache_dir = workspace / "reports" / "runs" / "optional-cache-probe"
        for probe_script in (workspace / "financial_cache_probe.py", workspace / "news_cache_probe.py"):
            probe_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        optional_probe = OptionalCacheProbePipeline(
            argparse.Namespace(
                command="run",
                workspace_dir=str(workspace),
                output_dir=str(optional_cache_dir),
                run_id="optional-cache-probe",
                started_at="2026-06-18T09:00:00+09:00",
                env="acct",
                request_type="analysis",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                news_cache_path="",
                main_events="",
                date="2026-06-18",
                reuse_existing_artifacts=True,
                skip_account=False,
                max_workers=3,
            )
        )
        financial_partial = optional_probe.collect_optional_cache("financial", ["005930", "000660"])
        news_partial = optional_probe.collect_optional_cache("news", ["005930", "000660"])
        if optional_probe.cache_attempts != 2:
            failures.append(f"optional cache probe should collect once per domain: attempts={optional_probe.cache_attempts}")
        if optional_probe.get_attempts != 4:
            failures.append(f"optional cache probe should get before and after collect per domain: attempts={optional_probe.get_attempts}")
        if not financial_partial or not news_partial:
            failures.append("optional cache probe did not return partial cache paths")
        if [item.get("status") for item in optional_probe.stages] != ["partial", "partial"]:
            failures.append(f"optional cache probe stages unexpected: {optional_probe.stages}")
        unrelated_cache = workspace / "unrelated-cache.yaml"
        unrelated_cache.write_text('date: "2026-06-18"\nsymbols:\n  "123456":\n    items:\n      - "probe"\n', encoding="utf-8")
        if optional_probe.first_existing_cache_path([unrelated_cache], ["005930"]):
            failures.append("unrelated cache symbols should not be returned as partial data")

        class EmptyCacheFallbackProbePipeline(Pipeline):
            def optional_cache_script(self, domain: str) -> Path:
                return workspace / f"{domain}_empty_cache_probe.py"

            def run_cmd(self, stage: str, cmd: list[str], *, required: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                if stage.endswith("-cache-get") or stage.endswith("-cache-collect"):
                    domain = stage.split("-cache-", 1)[0]
                    subdir = "collect-financial-information" if domain == "financial" else "collect-news-information"
                    prefix = "financial" if domain == "financial" else "news"
                    path = self.workspace_dir / "memory" / subdir / f"{prefix}-2026-06-18.yaml"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('date: "2026-06-18"\nsource: kis_open_api\nsymbols: {}\n', encoding="utf-8")
                    self.logs.append(
                        {
                            "stage": stage,
                            "command": cmd,
                            "returncode": 1,
                            "stdout_tail": str(path),
                            "stderr_tail": "",
                            "required": required,
                            "recorded_at": now_iso(),
                        }
                    )
                    write_json(self.command_log_path, {"commands": self.logs})
                    return subprocess.CompletedProcess(cmd, 1, stdout=str(path), stderr="")
                return super().run_cmd(stage, cmd, required=required, env=env)

        for probe_script in (workspace / "financial_empty_cache_probe.py", workspace / "news_empty_cache_probe.py"):
            probe_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        for stale_cache in (
            workspace / "memory" / "collect-financial-information" / "financial-2026-06-18.yaml",
            workspace / "memory" / "collect-news-information" / "news-2026-06-18.yaml",
        ):
            if stale_cache.exists():
                stale_cache.unlink()
        empty_cache_probe = EmptyCacheFallbackProbePipeline(
            argparse.Namespace(
                command="run",
                workspace_dir=str(workspace),
                output_dir=str(workspace / "reports" / "runs" / "empty-cache-probe"),
                run_id="empty-cache-probe",
                started_at="2026-06-18T09:00:00+09:00",
                env="acct",
                request_type="analysis",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                news_cache_path="",
                main_events="",
                date="2026-06-18",
                reuse_existing_artifacts=True,
                skip_account=False,
                max_workers=3,
            )
        )
        if empty_cache_probe.collect_optional_cache("financial", ["005930"]):
            failures.append("empty financial cache should not be returned as partial data")
        empty_news_path = empty_cache_probe.collect_optional_cache("news", ["005930"])
        if not empty_news_path:
            failures.append("empty news cache should be returned so zero usable articles can be reported")
        news_stage = empty_cache_probe.stages[-1] if empty_cache_probe.stages else {}
        if news_stage.get("stage") != "news-cache" or "zero usable articles" not in str(news_stage.get("detail")):
            failures.append(f"empty news cache stage did not describe zero usable articles: {news_stage}")

        retry_dir = workspace / "reports" / "runs" / "retry-probe"
        retry_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            retry_dir / "judge-review-spec.json",
            {
                "run_id": "retry-probe",
                "started_at": "2026-06-18T09:00:00+09:00",
                "stage": "judge-review",
                "symbol_ids": ["005930"],
            },
        )

        class RetryProbePipeline(Pipeline):
            def __init__(self, args: argparse.Namespace) -> None:
                super().__init__(args)
                self.probe_attempts = 0

            def run_cmd(self, stage: str, cmd: list[str], *, required: bool = True) -> subprocess.CompletedProcess[str]:
                self.probe_attempts += 1
                if self.probe_attempts < 3:
                    return subprocess.CompletedProcess(cmd, 1, stdout='{"status":"failed"}', stderr="")
                wrapper = {
                    "status": "success",
                    "stage": "judge-review",
                    "agent_role": "judge",
                    "task_name": "judge",
                    "parsed_json": {
                        "stage": "judge-review",
                        "symbols": [
                            {
                                "symbol_id": "005930",
                                "symbol_name": "삼성전자",
                                "target_position_value_krw": 70000,
                                "price": {"current_or_last": 70000},
                                "holding_quantity_context": {"expected_holding_quantity": 1},
                                "relative_attractiveness_rank": 1,
                                "reason_code": "hold_neutral",
                                "one_line_reason": "retry self-test",
                            }
                        ],
                    },
                    "errors": [],
                }
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(wrapper), stderr="")

        retry_probe = RetryProbePipeline(
            argparse.Namespace(
                command="run",
                workspace_dir=str(workspace),
                output_dir=str(retry_dir),
                run_id="retry-probe",
                started_at="2026-06-18T09:00:00+09:00",
                env="acct",
                request_type="analysis",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                news_cache_path="",
                main_events="",
                date="2026-06-18",
                reuse_existing_artifacts=True,
                skip_account=False,
                max_workers=3,
            )
        )
        retry_probe.run_judge_review()
        if retry_probe.probe_attempts != 3 or not (retry_dir / "judge-review.json").exists():
            failures.append(f"judge-review retry probe failed: attempts={retry_probe.probe_attempts}")
        retry_review = load_json_if_exists(retry_dir / "judge-review.json") or {}
        retry_symbol = (retry_review.get("symbols") or [{}])[0]
        if retry_symbol.get("final_holding_quantity") != 1:
            failures.append(f"final_holding_quantity was not preserved in judge-review.json: {retry_symbol}")
        if retry_symbol.get("target_position_value_krw") != 70000:
            failures.append(f"target_position_value_krw was not preserved in judge-review.json: {retry_symbol}")
        half_up_dir = workspace / "reports" / "runs" / "half-up-probe"
        half_up_dir.mkdir(parents=True, exist_ok=True)
        half_up_pipeline = Pipeline(
            argparse.Namespace(
                command="run",
                workspace_dir=str(workspace),
                output_dir=str(half_up_dir),
                run_id="half-up-probe",
                started_at="2026-06-18T09:00:00+09:00",
                env="acct",
                request_type="analysis",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                news_cache_path="",
                main_events="",
                date="2026-06-18",
                reuse_existing_artifacts=True,
                skip_account=False,
                max_workers=3,
            )
        )
        write_json(
            half_up_dir / "judge-debate.json",
            {"schema_version": "1", "stage": "judge-debate", "status": "success", "phases": []},
        )
        half_up_pipeline.write_judge_review(
            {
                "stage": "judge-review",
                "parsed_json": {
                    "stage": "judge-review",
                    "symbols": [
                        {
                            "symbol_id": "005930",
                            "symbol_name": "삼성전자",
                            "target_position_value_krw": 105000,
                            "final_holding_quantity": 99,
                            "price": {"current_or_last": 70000},
                            "holding_quantity_context": {"expected_holding_quantity": 1},
                            "today_trade_timeline_context": {
                                "collection_status": "complete",
                                "has_same_day_trade": False,
                                "has_same_day_buy": False,
                                "fills": [],
                            },
                            "relative_attractiveness_rank": 1,
                            "reason_code": "increase_target",
                            "one_line_reason": "half-up self-test",
                        }
                    ],
                },
                "errors": [],
            }
        )
        half_up_review = load_json_if_exists(half_up_dir / "judge-review.json") or {}
        half_up_symbol = (half_up_review.get("symbols") or [{}])[0]
        if half_up_symbol.get("final_holding_quantity") != 2:
            failures.append(f"Decimal ROUND_HALF_UP did not derive 1.5 shares as 2 and ignore judge final quantity: {half_up_symbol}")
        forced_baseline, forced_errors = half_up_pipeline.derive_judge_final_quantity(
            {
                "symbol_id": "005930",
                "symbol_name": "삼성전자",
                "target_position_value_krw": 210000,
                "relative_attractiveness_rank": 1,
                "reason_code": "increase_target",
                "one_line_reason": "must be overridden",
            },
            {
                "price": {"current_or_last": 70000},
                "holding_quantity_context": {"expected_holding_quantity": 1},
                "today_trade_timeline_context": {"collection_status": "complete", "has_same_day_buy": False},
            },
            "buy",
            force_baseline=True,
        )
        if (
            forced_errors
            or forced_baseline is None
            or forced_baseline.get("target_position_value_krw") != 70000
            or forced_baseline.get("reason_code") != "hold_debate_incomplete"
        ):
            failures.append(f"incomplete debate did not force deterministic baseline exposure: {forced_baseline} {forced_errors}")
        same_day_dir = workspace / "reports" / "runs" / "same-day-buy-probe"
        same_day_dir.mkdir(parents=True, exist_ok=True)
        same_day_pipeline = Pipeline(
            argparse.Namespace(
                command="run",
                workspace_dir=str(workspace),
                output_dir=str(same_day_dir),
                run_id="same-day-buy-probe",
                started_at="2026-06-18T09:00:00+09:00",
                env="acct",
                request_type="analysis",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                news_cache_path="",
                main_events="",
                date="2026-06-18",
                reuse_existing_artifacts=True,
                skip_account=False,
                max_workers=3,
            )
        )
        write_json(
            same_day_dir / "judge-debate.json",
            {"schema_version": "1", "stage": "judge-debate", "status": "success", "phases": []},
        )
        same_day_pipeline.write_judge_review(
            {
                "stage": "judge-review",
                "parsed_json": {
                    "stage": "judge-review",
                    "symbols": [
                        {
                            "symbol_id": "005930",
                            "symbol_name": "삼성전자",
                            "target_position_value_krw": 140000,
                            "price": {"current_or_last": 70000},
                            "holding_quantity_context": {"expected_holding_quantity": 1},
                            "today_trade_timeline_context": {"buy_fill_count": 1, "buy_quantity": 1},
                            "relative_attractiveness_rank": 1,
                            "reason_code": "increase_without_reason",
                            "one_line_reason": "same-day self-test",
                        }
                    ],
                },
                "errors": [],
            }
        )
        same_day_review = load_json_if_exists(same_day_dir / "judge-review.json") or {}
        if same_day_review.get("symbols"):
            failures.append(f"same-day increased target without additional_buy_reason was accepted: {same_day_review}")
        if not any(item.get("code") == "missing_additional_buy_reason" for item in same_day_review.get("errors", [])):
            failures.append(f"same-day increased target did not require additional_buy_reason: {same_day_review}")
        unknown_item = {
            "symbol_id": "005930",
            "symbol_name": "삼성전자",
            "target_position_value_krw": 140000,
            "price": {"current_or_last": 70000},
            "holding_quantity_context": {"expected_holding_quantity": 1},
            "today_trade_timeline_context": {
                "collection_status": "partial",
                "has_same_day_trade": None,
                "has_same_day_buy": None,
                "fills": [],
            },
            "relative_attractiveness_rank": 1,
            "reason_code": "increase_with_unknown_history",
            "one_line_reason": "unknown-history self-test",
        }
        unknown_normalized, unknown_errors = same_day_pipeline.derive_judge_final_quantity(unknown_item, {}, "buy")
        if unknown_normalized is not None or not any(
            item.get("code") == "missing_additional_buy_reason_unknown_same_day_history" for item in unknown_errors
        ):
            failures.append(f"unknown same-day history did not require additional_buy_reason: {unknown_normalized} {unknown_errors}")
        unknown_item["additional_buy_reason"] = "새 가격 돌파와 포트폴리오 여유가 확인됨"
        reasoned_normalized, reasoned_errors = same_day_pipeline.derive_judge_final_quantity(unknown_item, {}, "buy")
        if reasoned_normalized is None or reasoned_errors:
            failures.append(f"unknown same-day history with additional_buy_reason should allow an increase: {reasoned_normalized} {reasoned_errors}")
        confirmed_absent_item = dict(
            unknown_item,
            today_trade_timeline_context={
                "collection_status": "complete",
                "has_same_day_trade": False,
                "has_same_day_buy": False,
                "fills": [],
            },
        )
        confirmed_absent_item.pop("additional_buy_reason", None)
        absent_normalized, absent_errors = same_day_pipeline.derive_judge_final_quantity(confirmed_absent_item, {}, "buy")
        if absent_normalized is None or absent_errors:
            failures.append(f"complete same-day history with no buy should not require additional_buy_reason: {absent_normalized} {absent_errors}")
        invalid_dir = workspace / "reports" / "runs" / "invalid-final-probe"
        invalid_dir.mkdir(parents=True, exist_ok=True)
        invalid_pipeline = Pipeline(
            argparse.Namespace(
                command="run",
                workspace_dir=str(workspace),
                output_dir=str(invalid_dir),
                run_id="invalid-final-probe",
                started_at="2026-06-18T09:00:00+09:00",
                env="acct",
                request_type="analysis",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                news_cache_path="",
                main_events="",
                date="2026-06-18",
                reuse_existing_artifacts=True,
                skip_account=False,
                max_workers=3,
            )
        )
        write_json(
            invalid_dir / "judge-debate.json",
            {"schema_version": "1", "stage": "judge-debate", "status": "success", "phases": []},
        )
        invalid_pipeline.write_judge_review(
            {
                "stage": "judge-review",
                "parsed_json": {
                    "stage": "judge-review",
                    "symbols": [
                        {
                            "symbol_id": "005930",
                            "symbol_name": "삼성전자",
                            "price": {"current_or_last": 70000},
                            "holding_quantity_context": {"expected_holding_quantity": 1},
                            "relative_attractiveness_rank": 1,
                            "reason_code": "hold_neutral",
                            "one_line_reason": "invalid self-test",
                        }
                    ],
                },
                "errors": [],
            }
        )
        invalid_review = load_json_if_exists(invalid_dir / "judge-review.json") or {}
        if invalid_review.get("symbols"):
            failures.append(f"missing target_position_value_krw was converted into a symbol: {invalid_review}")
        if not any(item.get("code") == "invalid_target_position_value_krw" for item in invalid_review.get("errors", [])):
            failures.append(f"missing target_position_value_krw did not produce an error: {invalid_review}")
        fake_codex = workspace / "fake-codex"
        fake_codex_script(fake_codex)
        fake_market_index_snapshot = workspace / "fake-market-index-snapshot.py"
        fake_market_index_snapshot.write_text(
            """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

output = Path(sys.argv[sys.argv.index("--output") + 1])
payload = {
    "schema_version": "1",
    "run_id": "pipeline-self-test",
    "started_at": "2026-06-18T09:00:00+09:00",
    "generated_at": "2026-06-18T09:00:01+09:00",
    "status": "success",
    "indexes": [
        {"symbol": "SP500", "name": "S&P 500", "source": "google_finance", "status": "success", "value": 5000.0, "change_percent": 0.1, "observed_at": "2026-06-18T00:00:00+00:00", "market_status": "latest_available"},
        {"symbol": "NASDAQ", "name": "Nasdaq", "source": "google_finance", "status": "success", "value": 18000.0, "change_percent": 0.2, "observed_at": "2026-06-18T00:00:00+00:00", "market_status": "latest_available"},
        {"symbol": "DOW", "name": "Dow", "source": "google_finance", "status": "success", "value": 39000.0, "change_percent": -0.1, "observed_at": "2026-06-18T00:00:00+00:00", "market_status": "latest_available"},
        {"symbol": "KOSPI", "name": "KOSPI", "source": "kis_domestic_index", "status": "success", "value": 3000.0, "change_percent": 0.3, "observed_at": "2026-06-18T09:00:00+09:00", "market_status": "장중"},
        {"symbol": "KOSDAQ", "name": "KOSDAQ", "source": "kis_domestic_index", "status": "success", "value": 900.0, "change_percent": 0.4, "observed_at": "2026-06-18T09:00:00+09:00", "market_status": "장중"},
    ],
    "warnings": [],
    "errors": [],
}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
""",
            encoding="utf-8",
        )
        override_policy = workspace / "override-strategy-policy.yaml"
        default_policy = repo_root_from(script_dir()) / "containers/codex-exec/profiles/base/config" / STRATEGY_POLICY_CONFIG_FILENAME
        override_policy.write_text(
            default_policy.read_text(encoding="utf-8").replace(
                "risk_on_all_gte_pct: 1.5",
                "risk_on_all_gte_pct: 0.1",
            ),
            encoding="utf-8",
        )
        env_conflict_policy = workspace / "env-conflict-strategy-policy.yaml"
        env_conflict_policy.write_text(
            default_policy.read_text(encoding="utf-8").replace(
                "risk_on_all_gte_pct: 1.5",
                "risk_on_all_gte_pct: 9.9",
            ),
            encoding="utf-8",
        )
        old_codex_bin = os.environ.get("CODEX_BIN")
        old_reuse = os.environ.get("CODEX_SUBAGENT_REUSE_SUCCESS")
        old_market_index_snapshot = os.environ.get("DAILY_TRADING_MARKET_INDEX_SNAPSHOT_SCRIPT")
        old_strategy_policy = os.environ.get(STRATEGY_POLICY_CONFIG_ENV)
        os.environ[STRATEGY_POLICY_CONFIG_ENV] = str(override_policy)
        try:
            if resolve_strategy_policy_config_path(workspace, repo_root_from(workspace), "") != override_policy.resolve():
                failures.append("strategy policy env override did not resolve to override file")
        finally:
            if old_strategy_policy is None:
                os.environ.pop(STRATEGY_POLICY_CONFIG_ENV, None)
            else:
                os.environ[STRATEGY_POLICY_CONFIG_ENV] = old_strategy_policy
        os.environ["CODEX_BIN"] = str(fake_codex)
        os.environ["CODEX_SUBAGENT_REUSE_SUCCESS"] = "0"
        os.environ["DAILY_TRADING_MARKET_INDEX_SNAPSHOT_SCRIPT"] = str(fake_market_index_snapshot)
        os.environ[STRATEGY_POLICY_CONFIG_ENV] = str(env_conflict_policy)
        if resolve_strategy_policy_config_path(workspace, repo_root_from(workspace), str(override_policy)) != override_policy.resolve():
            failures.append("strategy policy CLI override did not take priority over env override")
        try:
            main_events = workspace / "main-events.jsonl"
            main_events.write_text(
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
                + "\n"
                + json.dumps({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1, "output_tokens": 1}}})
                + "\n",
                encoding="utf-8",
            )
            pipeline = Pipeline(
                argparse.Namespace(
                    command="run",
                    workspace_dir=str(workspace),
                    output_dir=str(run_dir),
                    run_id="pipeline-self-test",
                    started_at="2026-06-18T09:00:00+09:00",
                    env="acct",
                    request_type="real-submit",
                    portfolio_json=str(portfolio_path),
                    financial_cache_path="",
                    news_cache_path="",
                    main_events=str(main_events),
                    date="2026-06-18",
                    reuse_existing_artifacts=True,
                    skip_account=False,
                    max_workers=2,
                    strategy_policy_config=str(override_policy),
                )
            )
            summary = pipeline.run()
            if summary["status"] != "partial":
                failures.append(f"real-submit summary should remain partial before submit-order execution: {summary['status']}")
            run_stage_names = [item.get("stage") for item in load_json(run_dir / "run.json").get("stages", []) if isinstance(item, dict)]
            if "market-index-snapshot" not in run_stage_names or not (run_dir / "market-index-snapshot.json").exists():
                failures.append(f"pipeline did not record optional market-index-snapshot stage: {run_stage_names}")
            decision_payload = load_json(run_dir / "decision-brief.json")
            if len((decision_payload.get("market_index_snapshot") or {}).get("indexes", [])) != 5:
                failures.append(f"decision brief did not include five market index snapshot indexes: {decision_payload.get('market_index_snapshot')}")
            if (decision_payload.get("strategy_context") or {}).get("regime") != "risk_on":
                failures.append(f"decision brief did not include computed strategy context: {decision_payload.get('strategy_context')}")
            run_config = (load_json(run_dir / "run.json").get("daily_trading_config") or {})
            if (
                run_config.get("strategy_policy_config_path") != str(override_policy.resolve())
                or run_config.get("strategy_policy_config_sha256") != file_sha256(override_policy)
            ):
                failures.append(f"run config did not record strategy policy path/hash: {run_config}")
            order_path_selection = (summary.get("execution") or {}).get("order_path_selection") if isinstance(summary.get("execution"), dict) else {}
            if order_path_selection.get("resolved") != "immediate" or order_path_selection.get("reason") != "auto_regular_session":
                failures.append(f"pipeline did not resolve auto order path to immediate: {order_path_selection}")
            command_log = load_json(run_dir / "pipeline-command-log.json")
            debate_artifact = load_json(run_dir / "judge-debate.json")
            debate_phases = debate_artifact.get("phases") if isinstance(debate_artifact.get("phases"), list) else []
            if debate_artifact.get("status") != "success" or [item.get("phase") for item in debate_phases] != [
                "opening",
                "rebuttal-1",
                "rebuttal-2",
            ]:
                failures.append(f"pipeline did not record the conditional debate phases: {debate_artifact}")
            if (
                debate_artifact.get("executed_flow") != ["opening", "rebuttal-1"]
                or debate_artifact.get("final_phase") != "rebuttal-1"
                or (debate_artifact.get("rebuttal_2_gate") or {}).get("required") is not False
                or (debate_phases[-1] if debate_phases else {}).get("status") != "skipped"
            ):
                failures.append(f"pipeline did not skip an unnecessary rebuttal-2: {debate_artifact}")
            expected_debate_sessions = {
                "bull": "00000000-0000-4000-8000-000000000001",
                "bear": "00000000-0000-4000-8000-000000000002",
            }
            if debate_artifact.get("session_ids") != expected_debate_sessions:
                failures.append(f"pipeline did not preserve opening debate sessions: {debate_artifact.get('session_ids')}")
            for phase_item in [item for item in debate_phases if item.get("status") == "success"]:
                for side in DEBATE_SIDES:
                    side_item = ((phase_item.get("sides") or {}).get(side) or {})
                    if (
                        side_item.get("session_id") != expected_debate_sessions[side]
                        or not side_item.get("event_log_retained")
                        or not Path(str(side_item.get("event_log_path") or "")).is_file()
                    ):
                        failures.append(f"{phase_item.get('phase')} {side} lost session/audit log continuity: {side_item}")
                    if phase_item.get("phase") != "opening" and side_item.get("resume_session_id") != expected_debate_sessions[side]:
                        failures.append(f"{phase_item.get('phase')} {side} did not resume opening session: {side_item}")
            debate_stage_order = [
                item.get("stage")
                for item in command_log.get("commands", [])
                if isinstance(item, dict) and str(item.get("stage") or "").startswith("judge-debate-")
            ]
            if debate_stage_order != [
                "judge-debate-opening-attempt-01",
                "judge-debate-rebuttal-1-attempt-01",
            ]:
                failures.append(f"debate wait barriers ran out of order or retried unexpectedly: {debate_stage_order}")
            decision_commands = [
                item.get("command")
                for item in command_log.get("commands", [])
                if isinstance(item, dict) and item.get("stage") == "decision-brief"
            ]
            decision_command = decision_commands[-1] if decision_commands else []
            if "--strategy-policy-config" not in decision_command:
                failures.append(f"decision-brief command should receive strategy policy config: {decision_commands}")
            expected_news_date_index = decision_command.index("--expected-news-date") if "--expected-news-date" in decision_command else -1
            if expected_news_date_index < 0 or decision_command[expected_news_date_index + 1 : expected_news_date_index + 2] != ["2026-06-18"]:
                failures.append(f"decision-brief command should receive the run news date: {decision_commands}")
            execution_commands = [
                item.get("command")
                for item in command_log.get("commands", [])
                if isinstance(item, dict) and item.get("stage") == "execution-plan"
            ]
            if not execution_commands or "--decision-brief" in execution_commands[-1]:
                failures.append(f"execution-plan command should rely on the default decision-brief path: {execution_commands}")
            execution_payload = load_json(run_dir / "execution.json")
            execution_by_symbol = {
                symbol_key(item): item for item in execution_payload.get("orders", []) if isinstance(item, dict)
            }
            if as_int(execution_by_symbol.get("005930", {}).get("order_price")) != 70000:
                failures.append(
                    "execution-plan did not fall back to decision-brief price for a new holding with missing account current_price"
                )
            if summary["token_usage"]["subagents"]["total_tokens"] != 840:
                failures.append(f"unexpected subagent token total: {summary['token_usage']}")
            if summary["token_usage"]["main"]["total_tokens"] != 17 or summary["token_usage"]["total"]["total_tokens"] != 857:
                failures.append(f"unexpected pipeline token summary with main events: {summary['token_usage']}")
            review_summary = summary.get("review_summary") if isinstance(summary.get("review_summary"), dict) else {}
            if review_summary.get("symbol_count") != 1 or not review_summary.get("symbols"):
                failures.append(f"pipeline summary omitted compact review summary: {review_summary}")
            if (
                review_summary.get("debate_status") != "success"
                or review_summary.get("debate_phase_count") != 3
                or review_summary.get("debate_executed_phase_count") != 2
                or review_summary.get("debate_final_phase") != "rebuttal-1"
                or review_summary.get("rebuttal_2_status") != "skipped"
            ):
                failures.append(f"pipeline summary omitted conditional debate status: {review_summary}")
            today_trade_summary = summary.get("today_trade_summary") if isinstance(summary.get("today_trade_summary"), dict) else {}
            if (
                today_trade_summary.get("collection_status") != "complete"
                or today_trade_summary.get("confirmed_no_trade_symbol_count") != 2
                or today_trade_summary.get("unknown_symbol_count") != 0
            ):
                failures.append(f"pipeline summary did not distinguish confirmed empty same-day history: {today_trade_summary}")
            today_fills_summary = summary.get("today_fills_summary") if isinstance(summary.get("today_fills_summary"), dict) else {}
            if today_fills_summary.get("status") != "success" or today_fills_summary.get("fill_count") != 0:
                failures.append(f"pipeline summary omitted account fill collection status: {today_fills_summary}")
            account_display = summary.get("account_display_summary") if isinstance(summary.get("account_display_summary"), dict) else {}
            if "today_buy_amount" in account_display or "today_sell_amount" in account_display:
                failures.append(f"display account summary should not expose same-day totals as main fields: {account_display}")
            if not isinstance(account_display.get("today_trade_amounts"), dict):
                failures.append(f"display account summary omitted separate same-day trade bucket: {account_display}")
            account_summary = summary.get("account_summary") if isinstance(summary.get("account_summary"), dict) else {}
            if account_summary.get("total_evaluation_amount") != 1500000:
                failures.append(f"account asset snapshot should not overwrite account_summary: {account_summary}")
            account_asset_summary = summary.get("account_asset_summary") if isinstance(summary.get("account_asset_summary"), dict) else {}
            if account_asset_summary.get("total_asset_amount") != 20000000:
                failures.append(f"pipeline summary omitted account_asset_summary: {account_asset_summary}")
            artifacts = summary.get("artifacts") if isinstance(summary.get("artifacts"), dict) else {}
            if not str(artifacts.get("account_asset_snapshot", "")).endswith("account-asset-snapshot.json"):
                failures.append(f"pipeline summary omitted account asset artifact path: {artifacts}")
            if not str(artifacts.get("model_usage", "")).endswith("model-usage.jsonl"):
                failures.append(f"pipeline summary omitted model usage artifact path: {artifacts}")
            if not str(artifacts.get("judge_debate", "")).endswith("judge-debate.json"):
                failures.append(f"pipeline summary omitted judge debate artifact path: {artifacts}")
            if not str(artifacts.get("html_report", "")).endswith("daily-trading-report.html"):
                failures.append(f"pipeline summary omitted HTML report artifact path: {artifacts}")
            run_payload = load_json(run_dir / "run.json")
            if not str(run_payload.get("model_usage", "")).endswith("model-usage.jsonl"):
                failures.append(f"run.json omitted model usage artifact path: {run_payload}")
            evidence_summary = summary.get("evidence_summary") if isinstance(summary.get("evidence_summary"), dict) else {}
            if not isinstance(evidence_summary.get("news"), dict) or "display_text" not in evidence_summary.get("news", {}):
                failures.append(f"pipeline summary omitted displayable news evidence status: {evidence_summary}")
            telegram_policy = summary.get("telegram_response_policy") if isinstance(summary.get("telegram_response_policy"), dict) else {}
            if telegram_policy.get("gate_label") != "주문 전 기존 미체결/예약 주문":
                failures.append(f"telegram response policy omitted explicit gate label: {telegram_policy}")
            if "telegram-summary.txt" not in str(telegram_policy.get("source", "")):
                failures.append(f"telegram response policy did not require fixed renderer output: {telegram_policy}")
            telegram_summary_path = Path(str(summary.get("telegram_summary_path") or ""))
            if not telegram_summary_path.exists():
                failures.append(f"telegram summary was not written: {telegram_summary_path}")
            else:
                telegram_text = telegram_summary_path.read_text(encoding="utf-8")
                for required_text in ("<b>daily-trading", "<b>계좌</b>", "<b>이번 run</b>", "상세 리포트:", "토큰:"):
                    if required_text not in telegram_text:
                        failures.append(f"telegram summary omitted {required_text}: {telegram_summary_path}")
            html_report_path = Path(str(summary.get("html_report_path") or ""))
            if not summary.get("html_report_available") or not html_report_path.exists():
                failures.append(f"HTML report was not written: {html_report_path}")
            else:
                html_text = html_report_path.read_text(encoding="utf-8")
                for required_text in ("당일 누적 거래·판단 리포트", "계좌·시장 통합 추이", "시간대별 거래·전체 종목 판단"):
                    if required_text not in html_text:
                        failures.append(f"HTML report omitted {required_text}: {html_report_path}")
            report_path = Path(str(summary.get("report_path") or ""))
            if not report_path.exists():
                failures.append(f"portfolio report was not written: {report_path}")
            else:
                report_text = report_path.read_text(encoding="utf-8")
                if "## 4. `analyst-review` 독립 평결" not in report_text or "## 5. `judge-review` 포트폴리오 평결" not in report_text:
                    failures.append(f"portfolio report omitted review sections: {report_path}")
                if "최종점수(원점수 평균, 0-10)" not in report_text or "role별 점수" not in report_text:
                    failures.append("portfolio report omitted analyst-review score columns")
                if "| 8.0 | 2 |" not in report_text:
                    failures.append("portfolio report omitted analyst-review simple-mean score values")
                if "analyst-quality-value: 5(평균 제외)" not in report_text or "analyst-news-flow: 5(평균 제외)" not in report_text:
                    failures.append("portfolio report omitted role-level score details")
                if "보정 신뢰도" in report_text or "confidence" in report_text:
                    failures.append("portfolio report still contains confidence artifacts")
                if "주문 전 기존 미체결/예약 주문 조회: no" not in report_text or "주문 전 기존 미체결/예약 주문: 미조회" not in report_text:
                    failures.append("portfolio report did not preserve active-order gate lookup state")
                if "주문 전 기존 미체결/예약 주문 미조회" not in report_text:
                    failures.append("portfolio report did not mark unrefreshed active-order adjustment gate")
                if "수집 상태: complete · 체결 없음 확인 2종목 · 미확인 0종목" not in report_text:
                    failures.append("portfolio report omitted same-day trade collection coverage")
            execution_summary = summary.get("execution") if isinstance(summary.get("execution"), dict) else {}
            if execution_summary.get("requires_main_agent_order_execution") is not True:
                failures.append("real-submit pipeline summary did not request submit-order execution")
            expected_actions = ["refresh_active_order_lookup", "refresh_order_available_lookup", "continue_order_execution"]
            if execution_summary.get("required_main_agent_actions") != expected_actions:
                failures.append(f"unexpected submit-order action list: {execution_summary.get('required_main_agent_actions')}")
            read_policy = summary.get("main_agent_read_policy", "")
            if "execution-plan order_price values as the default limit price candidates" not in read_policy:
                failures.append(f"pipeline summary read policy omitted default order_price guidance: {read_policy}")

            retry_debate_dir = workspace / "reports" / "runs" / "debate-retry-probe"
            retry_debate_dir.mkdir(parents=True, exist_ok=True)
            retry_debate_spec = load_json(run_dir / "judge-review-spec.json")
            retry_debate_spec["run_id"] = "debate-retry-probe"
            retry_debate_spec["output_dir"] = str(retry_debate_dir)
            retry_debate_spec["artifact_paths"]["debate_artifact"] = str(retry_debate_dir / "judge-debate.json")
            write_json(retry_debate_dir / "judge-review-spec.json", retry_debate_spec)
            retry_debate_pipeline = Pipeline(
                argparse.Namespace(
                    command="run",
                    workspace_dir=str(workspace),
                    output_dir=str(retry_debate_dir),
                    run_id="debate-retry-probe",
                    started_at="2026-06-18T09:00:00+09:00",
                    env="acct",
                    request_type="analysis",
                    portfolio_json=str(portfolio_path),
                    financial_cache_path="",
                    news_cache_path="",
                    main_events="",
                    date="2026-06-18",
                    reuse_existing_artifacts=True,
                    skip_account=False,
                    max_workers=2,
                    strategy_policy_config=str(override_policy),
                )
            )
            os.environ["FAKE_CODEX_FAIL_ONCE_TASKS"] = "judge-debate-bear-opening-attempt-01"
            os.environ["FAKE_CODEX_FAIL_STATE_DIR"] = str(retry_debate_dir / "fake-state")
            os.environ["FAKE_CODEX_REBUTTAL_1_CONFLICT"] = "1"
            os.environ["FAKE_CODEX_INVALID_REBUTTAL_2_TASKS"] = (
                "judge-debate-bear-rebuttal-2-attempt-01"
            )
            os.environ["FAKE_CODEX_THREAD_ID_OVERRIDES"] = json.dumps(
                {
                    "judge-debate-bear-rebuttal-1-attempt-01": "00000000-0000-4000-8000-000000000009",
                }
            )
            try:
                retry_debate = retry_debate_pipeline.run_judge_debate()
            finally:
                os.environ.pop("FAKE_CODEX_FAIL_ONCE_TASKS", None)
                os.environ.pop("FAKE_CODEX_FAIL_STATE_DIR", None)
                os.environ.pop("FAKE_CODEX_REBUTTAL_1_CONFLICT", None)
                os.environ.pop("FAKE_CODEX_INVALID_REBUTTAL_2_TASKS", None)
                os.environ.pop("FAKE_CODEX_THREAD_ID_OVERRIDES", None)
            retry_opening = (retry_debate.get("phases") or [{}])[0]
            retry_opening_sides = retry_opening.get("sides") if isinstance(retry_opening.get("sides"), dict) else {}
            if (
                retry_debate.get("status") != "success"
                or len((retry_opening_sides.get("bull") or {}).get("attempts") or []) != 1
                or len((retry_opening_sides.get("bear") or {}).get("attempts") or []) != 2
            ):
                failures.append(f"debate retry did not preserve successful bull and retry only bear: {retry_debate}")
            retry_specs = load_json(retry_debate_dir / "debate" / "opening.attempt-02.specs.json")
            retry_specs_list = retry_specs.get("specs") if isinstance(retry_specs.get("specs"), list) else []
            if (
                len(retry_specs_list) != 1
                or retry_specs_list[0].get("agent_role") != "debate-bear"
                or retry_specs_list[0].get("resume_session_id") != "00000000-0000-4000-8000-000000000002"
            ):
                failures.append(f"debate retry did not resume only the failed bear session: {retry_specs}")
            retry_phases = retry_debate.get("phases") if isinstance(retry_debate.get("phases"), list) else []
            if (
                retry_debate.get("final_phase") != "rebuttal-2"
                or (retry_debate.get("rebuttal_2_gate") or {}).get("required") is not True
                or len(retry_phases) != 3
                or retry_phases[-1].get("status") != "success"
            ):
                failures.append(f"debate conflict did not require rebuttal-2: {retry_debate}")
            retry_rebuttal = retry_phases[1] if len(retry_phases) > 1 else {}
            retry_rebuttal_sides = retry_rebuttal.get("sides") if isinstance(retry_rebuttal.get("sides"), dict) else {}
            retry_bear_rebuttal = retry_rebuttal_sides.get("bear") or {}
            retry_bear_attempts = retry_bear_rebuttal.get("attempts") or []
            if (
                len(retry_bear_attempts) != 2
                or retry_bear_attempts[0].get("session_id") != "00000000-0000-4000-8000-000000000002"
                or retry_bear_attempts[0].get("reported_session_id") != "00000000-0000-4000-8000-000000000009"
            ):
                failures.append(f"mismatched resumed thread id was not isolated from the persistent session: {retry_bear_rebuttal}")
            retry_rebuttal_specs = load_json(retry_debate_dir / "debate" / "rebuttal-1.attempt-02.specs.json")
            retry_rebuttal_specs_list = (
                retry_rebuttal_specs.get("specs") if isinstance(retry_rebuttal_specs.get("specs"), list) else []
            )
            if (
                len(retry_rebuttal_specs_list) != 1
                or retry_rebuttal_specs_list[0].get("agent_role") != "debate-bear"
                or retry_rebuttal_specs_list[0].get("resume_session_id") != "00000000-0000-4000-8000-000000000002"
            ):
                failures.append(f"mismatched resumed thread id replaced the opening session: {retry_rebuttal_specs}")
            retry_closing = retry_phases[2] if len(retry_phases) > 2 else {}
            retry_closing_sides = retry_closing.get("sides") if isinstance(retry_closing.get("sides"), dict) else {}
            retry_bull_closing = retry_closing_sides.get("bull") or {}
            retry_bear_closing = retry_closing_sides.get("bear") or {}
            if (
                len(retry_bull_closing.get("attempts") or []) != 1
                or len(retry_bear_closing.get("attempts") or []) != 2
                or retry_bear_closing.get("session_id") != "00000000-0000-4000-8000-000000000002"
            ):
                failures.append(
                    f"invalid rebuttal-2 did not retry only bear in the persistent session: {retry_closing}"
                )
            retry_closing_specs = load_json(retry_debate_dir / "debate" / "rebuttal-2.attempt-02.specs.json")
            retry_closing_specs_list = (
                retry_closing_specs.get("specs") if isinstance(retry_closing_specs.get("specs"), list) else []
            )
            if (
                len(retry_closing_specs_list) != 1
                or retry_closing_specs_list[0].get("agent_role") != "debate-bear"
                or retry_closing_specs_list[0].get("resume_session_id")
                != "00000000-0000-4000-8000-000000000002"
            ):
                failures.append(f"invalid rebuttal-2 retry lost the bear session: {retry_closing_specs}")

            fake_execute_orders = workspace / "fake-execute-orders.py"
            fake_execute_orders.write_text(
                """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[sys.argv.index("--output-dir") + 1])
execution_path = output_dir / "execution.json"
account_path = output_dir / "account-before-order.json"
execution = json.loads(execution_path.read_text(encoding="utf-8"))
account = json.loads(account_path.read_text(encoding="utf-8"))
account["active_order_lookup_performed"] = True
account["order_available_lookup_performed"] = True
account["active_orders"] = []
orders = execution.get("orders") if isinstance(execution.get("orders"), list) else []
for item in orders:
    if isinstance(item, dict):
        item["result"] = "submitted"
        item["reason"] = "fake_submit_order"
        item["order_or_reservation_id"] = "fake-resv-1"
        item["attempts"] = [{"api_name": "order_resv", "result": "submitted"}]
execution["status"] = "success"
execution["requires_main_agent_order_execution"] = False
execution["required_main_agent_actions"] = []
execution["order_execution_mode"] = "submit"
account_path.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
execution_path.write_text(json.dumps(execution, ensure_ascii=False, indent=2), encoding="utf-8")
(output_dir / "order-execution-log.json").write_text(json.dumps({"status": "success"}, ensure_ascii=False), encoding="utf-8")
print(json.dumps(execution, ensure_ascii=False))
""",
                encoding="utf-8",
            )
            fake_execute_orders.chmod(0o755)

            class SubmitOrdersProbePipeline(Pipeline):
                def order_execution_script(self) -> str:
                    return str(fake_execute_orders)

            submit_run_dir = workspace / "reports" / "runs" / "submit-orders-probe"
            write_self_test_fixtures(workspace, submit_run_dir)
            submit_pipeline = SubmitOrdersProbePipeline(
                argparse.Namespace(
                    command="run",
                    workspace_dir=str(workspace),
                    output_dir=str(submit_run_dir),
                    run_id="submit-orders-probe",
                    started_at="2026-06-18T09:00:00+09:00",
                    env="acct",
                    request_type="real-submit",
                    portfolio_json=str(portfolio_path),
                    financial_cache_path="",
                    news_cache_path="",
                    main_events=str(main_events),
                    date="2026-06-18",
                    reuse_existing_artifacts=True,
                    skip_account=False,
                    max_workers=3,
                    submit_orders=True,
                )
            )
            submit_summary = submit_pipeline.run()
            submit_stages = [item.get("stage") for item in load_json(submit_run_dir / "run.json").get("stages", []) if isinstance(item, dict)]
            if "order-execution" not in submit_stages:
                failures.append(f"submit-orders pipeline did not run order-execution stage: {submit_stages}")
            if submit_summary.get("status") != "success":
                failures.append(f"submit-orders summary did not reflect fake submitted order: {submit_summary.get('status')}")
            submit_execution = submit_summary.get("execution") if isinstance(submit_summary.get("execution"), dict) else {}
            if submit_execution.get("requires_main_agent_order_execution") is not False:
                failures.append(f"submit-orders summary did not clear execution handoff: {submit_execution}")
            submit_telegram = Path(str(submit_summary.get("telegram_summary_path") or ""))
            if not submit_telegram.exists():
                failures.append(f"submit-orders summary did not render telegram summary: {submit_telegram}")
            if not (run_dir / "pipeline-summary.json").exists():
                failures.append("pipeline-summary.json was not written")
            if not (run_dir / "execution.json").exists():
                failures.append("execution.json was not written")
            execution_payload = load_json(run_dir / "execution.json")
            execution_payload["status"] = "success"
            execution_payload["requires_main_agent_order_execution"] = False
            execution_payload["required_main_agent_actions"] = []
            if execution_payload.get("orders"):
                execution_payload["orders"][0]["result"] = "submitted"
                execution_payload["orders"][0]["reason"] = "accepted_reservation_order"
                execution_payload["orders"][0]["order_or_reservation_id"] = "selftest-resv-1"
            write_json(run_dir / "execution.json", execution_payload)
            run_payload = load_json(run_dir / "run.json")
            run_payload.setdefault("stages", []).append(
                {
                    "stage": "order-execution",
                    "status": "success",
                    "required": True,
                    "detail": "self-test order execution completed",
                    "path": str(run_dir / "execution.json"),
                }
            )
            write_json(run_dir / "run.json", run_payload)
            summarize_result = subprocess.run(
                [
                    sys.executable,
                    str(script_dir() / "run_daily_trading_pipeline.py"),
                    "summarize",
                    "--workspace-dir",
                    str(workspace),
                    "--output-dir",
                    str(run_dir.relative_to(workspace)),
                    "--request-type",
                    "real-submit",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if summarize_result.returncode != 0:
                failures.append(f"summarize CLI failed: stdout={summarize_result.stdout} stderr={summarize_result.stderr}")
            summarized = load_json(run_dir / "pipeline-summary.json")
            summarized_run = load_json(run_dir / "run.json")
            if len(summarized_run.get("stages", [])) != len(run_payload.get("stages", [])):
                failures.append("summarize did not preserve run.json stages")
            if summarized.get("status") != "success":
                failures.append(f"summarize did not reflect completed order execution: {summarized.get('status')}")
            if (summarized.get("review_summary") or {}).get("submitted_order_count") != 1:
                failures.append(f"summarize did not carry submitted order count: {summarized.get('review_summary')}")
            summarized_telegram = Path(str(summarized.get("telegram_summary_path") or ""))
            if not summarized_telegram.exists() or "selftest-resv-1" not in summarized_telegram.read_text(encoding="utf-8"):
                failures.append("summarize did not refresh telegram summary with submitted order evidence")
            summarized_html = Path(str(summarized.get("html_report_path") or ""))
            if not summarized.get("html_report_available") or not summarized_html.exists():
                failures.append("summarize did not refresh the cumulative HTML report")
            final_report = Path(str(summarized.get("report_path") or ""))
            final_report_text = final_report.read_text(encoding="utf-8") if final_report.exists() else ""
            if "selftest-resv-1" not in final_report_text or "submitted" not in final_report_text:
                failures.append("summarized report did not include submitted order evidence")
            empty_run_dir = workspace / "reports" / "runs" / "empty-summary-probe"
            empty_run_dir.mkdir(parents=True, exist_ok=True)
            empty_result = subprocess.run(
                [
                    sys.executable,
                    str(script_dir() / "run_daily_trading_pipeline.py"),
                    "summarize",
                    "--workspace-dir",
                    str(workspace),
                    "--output-dir",
                    str(empty_run_dir.relative_to(workspace)),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if empty_result.returncode == 0:
                failures.append(f"summarize accepted an empty run directory: {empty_result.stdout}")
            bad_json_dir = workspace / "reports" / "runs" / "bad-json-summary-probe"
            bad_json_dir.mkdir(parents=True, exist_ok=True)
            for source_name in (
                "run.json",
                "check-portfolio.json",
                "decision-brief.json",
                "analyst-review.json",
                "judge-review.json",
                "account-before-order.json",
                "execution.json",
            ):
                target = bad_json_dir / source_name
                target.write_text((run_dir / source_name).read_text(encoding="utf-8"), encoding="utf-8")
            (bad_json_dir / "execution.json").write_text("{bad-json", encoding="utf-8")
            bad_json_result = subprocess.run(
                [
                    sys.executable,
                    str(script_dir() / "run_daily_trading_pipeline.py"),
                    "summarize",
                    "--workspace-dir",
                    str(workspace),
                    "--output-dir",
                    str(bad_json_dir.relative_to(workspace)),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if bad_json_result.returncode == 0:
                failures.append(f"summarize accepted invalid required JSON: {bad_json_result.stdout}")
            empty_stages_dir = workspace / "reports" / "runs" / "empty-stages-summary-probe"
            empty_stages_dir.mkdir(parents=True, exist_ok=True)
            for source_name in (
                "run.json",
                "check-portfolio.json",
                "decision-brief.json",
                "analyst-review.json",
                "judge-review.json",
                "account-before-order.json",
                "execution.json",
            ):
                target = empty_stages_dir / source_name
                target.write_text((run_dir / source_name).read_text(encoding="utf-8"), encoding="utf-8")
            empty_stages_payload = load_json(empty_stages_dir / "run.json")
            empty_stages_payload["stages"] = []
            write_json(empty_stages_dir / "run.json", empty_stages_payload)
            empty_stages_result = subprocess.run(
                [
                    sys.executable,
                    str(script_dir() / "run_daily_trading_pipeline.py"),
                    "summarize",
                    "--workspace-dir",
                    str(workspace),
                    "--output-dir",
                    str(empty_stages_dir.relative_to(workspace)),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if empty_stages_result.returncode == 0:
                failures.append(f"summarize accepted empty run stages: {empty_stages_result.stdout}")
        finally:
            if old_codex_bin is None:
                os.environ.pop("CODEX_BIN", None)
            else:
                os.environ["CODEX_BIN"] = old_codex_bin
            if old_reuse is None:
                os.environ.pop("CODEX_SUBAGENT_REUSE_SUCCESS", None)
            else:
                os.environ["CODEX_SUBAGENT_REUSE_SUCCESS"] = old_reuse
            if old_market_index_snapshot is None:
                os.environ.pop("DAILY_TRADING_MARKET_INDEX_SNAPSHOT_SCRIPT", None)
            else:
                os.environ["DAILY_TRADING_MARKET_INDEX_SNAPSHOT_SCRIPT"] = old_market_index_snapshot
            if old_strategy_policy is None:
                os.environ.pop(STRATEGY_POLICY_CONFIG_ENV, None)
            else:
                os.environ[STRATEGY_POLICY_CONFIG_ENV] = old_strategy_policy

    payload = {"status": "passed" if not failures else "failed", "failures": failures}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not failures else 1


class RunDailyTradingPipelineSelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        self.assertEqual(run_self_test(), 0)

    def test_rebuttal_2_gate_skips_agreement_and_runs_for_conflict(self) -> None:
        artifact = {
            "sides": {
                side: {
                    "debate_decision_issues": [],
                    "output": {
                        "symbols": [
                            {
                                "symbol_id": "005930",
                                "recommended_action": "hold",
                                "target_holding_quantity": 2,
                            }
                        ]
                    },
                }
                for side in DEBATE_SIDES
            }
        }
        gate = evaluate_rebuttal_2_gate(artifact, ["005930"])
        self.assertFalse(gate["required"])
        self.assertEqual(gate["status"], "skipped")

        artifact["sides"]["bear"]["output"]["symbols"][0].update(
            {"recommended_action": "buy", "target_holding_quantity": 3}
        )
        gate = evaluate_rebuttal_2_gate(artifact, ["005930"])
        self.assertTrue(gate["required"])
        self.assertEqual(
            set(gate["reason_codes"]),
            {"recommended_action_disagreement", "target_holding_quantity_gap"},
        )

        artifact["sides"]["bear"]["debate_decision_issues"] = [
            {
                "symbol_id": "005930",
                "code": "incomplete_debate_final_position",
            }
        ]
        gate = evaluate_rebuttal_2_gate(artifact, ["005930"])
        self.assertIn("incomplete_debate_final_position", gate["reason_codes"])

    def test_build_review_summary_reports_final_decisions_not_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "review-summary-probe"
            run_dir.mkdir(parents=True, exist_ok=True)
            pipeline = Pipeline(
                argparse.Namespace(
                    command="summarize",
                    workspace_dir=str(workspace),
                    output_dir=str(run_dir),
                    run_id="review-summary-probe",
                    started_at="2026-06-18T09:00:00+09:00",
                )
            )
            write_json(
                run_dir / "judge-debate.json",
                {"schema_version": "1", "stage": "judge-debate", "status": "success", "phases": []},
            )
            write_json(
                run_dir / "analyst-review.json",
                {"symbols": [{"symbol_id": symbol_id} for symbol_id in ("005930", "000660", "035720", "402340")]},
            )
            # 402340 is a buy candidate whose judge decision is invalid, so it must stay unresolved.
            write_json(
                run_dir / "judge-review-spec.json",
                {"candidate_directions": {"005930": "buy", "000660": "sell", "402340": "buy"}},
            )
            write_json(
                run_dir / "judge-review.json",
                {
                    "status": "success",
                    "symbols": [
                        {"symbol_id": "005930", "final_holding_quantity": 2},   # 0 -> 2 매수
                        {"symbol_id": "000660", "final_holding_quantity": 0},   # 3 -> 0 매도
                        {"symbol_id": "035720", "final_holding_quantity": 5},   # 5 -> 5 유지 (후보 아님)
                        {"symbol_id": "402340", "final_holding_quantity": -1},  # 무효 최종수량 -> 미결
                    ],
                },
            )
            account = {
                "symbols": [
                    {"symbol_id": "005930", "current_live_holding_quantity": 0},
                    {"symbol_id": "000660", "current_live_holding_quantity": 3},
                    {"symbol_id": "035720", "current_live_holding_quantity": 5},
                    {"symbol_id": "402340", "current_live_holding_quantity": 0},
                ]
            }
            review = pipeline.build_review_summary(account, {"orders": []})
            # Final decisions come from the current->final holding-quantity direction of resolved rows.
            self.assertEqual(review["final_buy_count"], 1)
            self.assertEqual(review["final_sell_count"], 1)
            # The invalid candidate (402340) is not silently folded into 유지: naive scored-minus-directions
            # would report 2 holds, but the delta-derived count is 1 (only 035720).
            self.assertEqual(review["final_hold_count"], 1)
            self.assertEqual(review["unresolved_candidate_count"], 1)
            self.assertNotIn("402340", {row["symbol_id"] for row in review["symbols"]})
            # Judge candidate counts remain for diagnostics but are no longer the reported verdict.
            self.assertEqual(review["buy_candidate_count"], 2)
            self.assertEqual(review["sell_candidate_count"], 1)
