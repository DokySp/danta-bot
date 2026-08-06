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
from unittest import mock

from ..scripts.run_daily_trading_pipeline import (
    ORDER_PATH_AUTO,
    STRATEGY_POLICY_CONFIG_ENV,
    STRATEGY_POLICY_CONFIG_FILENAME,
    Pipeline,
    as_int,
    cache_coverage,
    cache_evidence_counts,
    file_sha256,
    load_json,
    load_json_if_exists,
    normalize_thesis_condition_id,
    now_iso,
    symbol_news_cache_coverage,
    symbol_news_cache_evidence_counts,
    repo_root_from,
    resolve_order_path,
    resolve_strategy_policy_config_path,
    script_dir,
    symbol_key,
    thesis_definition_is_valid,
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
fail_state_dir = Path(os.environ.get("FAKE_CODEX_FAIL_STATE_DIR", output_path.parent))
fail_marker = fail_state_dir / f"{task_name}.failed-once"
fail_output = task_name in fail_once_tasks and not fail_marker.exists()
if fail_output:
    fail_marker.parent.mkdir(parents=True, exist_ok=True)
    fail_marker.write_text("failed", encoding="utf-8")

prompt = sys.argv[-1] if sys.argv else ""
if "stage: judge-review" in prompt:
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
    if stage == "judge-review":
        rows.append({
            "symbol_id": symbol,
            "symbol_name": symbol,
            "target_position_value_krw": 70000 if symbol == "005930" else 0,
            "relative_attractiveness_rank": index,
            "decision_basis": "thesis" if symbol == "005930" else "none",
            "evidence_refs": [f"analyst-review:{symbol}:analyst-quality-value"],
            "reason_code": "hold_neutral",
            "one_line_reason": "self-test",
            "opposing_view": {
                "increase_case": {
                    "summary": "self-test increase case",
                    "evidence_refs": [f"analyst-review:{symbol}:analyst-quality-value"]
                },
                "reduce_case": {
                    "summary": "self-test reduce case",
                    "evidence_refs": [f"analyst-review:{symbol}:analyst-news-flow"]
                }
            },
            "thesis_definition": {
                "core_rationale": "self-test entry rationale",
                "invalidation_conditions": [
                    {"condition_id": "self-test-condition", "description": "self-test invalidation condition"}
                ]
            }
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
                    "missing_data": ["symbol_news_summary"]
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
payload = {"stage": stage, "agent_id": "fake", "persona": "fake", "human_markdown_path": "", "symbols": rows, "errors": []}
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text("" if fail_output else json.dumps(payload, ensure_ascii=False), encoding="utf-8")
if "--json" in sys.argv:
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
            "run_id": run_dir.name,
            "started_at": "2026-06-18T09:00:00+09:00",
            "status": "success",
            "symbols": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "product_type": "stock",
                    "eligible_for_review": True,
                    "price": {"current_or_last": 70000, "observed_at": "2026-06-18T09:00:00+09:00", "snapshot_mode": "live"},
                    "investor_flow_summary": {
                        "estimate_time_code": "5",
                        "foreign_net_buy_quantity": 1000,
                        "institution_net_buy_quantity": -100,
                        "combined_net_buy_quantity": 900,
                    },
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
            "run_id": run_dir.name,
            "started_at": "2026-06-18T09:00:00+09:00",
            "status": "success",
            "order_gate_status": "not_run",
            "active_order_lookup_performed": False,
            "order_available_lookup_performed": False,
            "account_summary": {
                "cash_amount": 1000000,
                "orderable_cash_amount": 900000,
                "total_evaluation_amount": 1500000,
            },
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
            "run_id": run_dir.name,
            "started_at": "2026-06-18T09:00:00+09:00",
            "generated_at": "2026-06-18T09:00:05+09:00",
            "observed_at": "2026-06-18T09:00:05+09:00",
            "stage": "account-asset-snapshot",
            "status": "success",
            "skipped": False,
            "source_api": "inquire_account_balance",
            "tot_asst_amt": 20000000,
            "tot_dncl_amt": 1000000,
            "evlu_amt_smtl": 19000000,
            "pchs_amt_smtl": 18000000,
            "evlu_pfls_amt_smtl": 1000000,
            "ovrs_stck_evlu_amt1": 0,
            "account_asset_summary": {
                "source_api": "inquire_account_balance",
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
            "run_id": run_dir.name,
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


def step_cache_coverage_and_evidence_checks(workspace: Path, run_dir: Path) -> list[str]:
    """cache_coverage/evidence-count helpers correctly flag incomplete, empty, and stale caches."""
    failures: list[str] = []
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
    empty_symbol_news_cache = workspace / "empty-symbol-news-cache.yaml"
    empty_symbol_news_cache.write_text(
        'date: "2026-06-18"\nsymbols:\n  "005930":\n    articles:\n      - article_date: ""\n        sentiment: neutral\n        content: ""\n',
        encoding="utf-8",
    )
    covered, missing = cache_coverage(empty_symbol_news_cache, ["005930"])
    if covered or missing != ["005930"]:
        failures.append(f"empty news article should be incomplete: covered={covered}, missing={missing}")
    no_symbol_news_cache = workspace / "no-symbol-news-cache.yaml"
    no_symbol_news_cache.write_text(
        'date: "2026-06-18"\nsymbols:\n  "005930":\n    articles:\n      - article_date: ""\n        sentiment: neutral\n        content: "2026-06-18 기준 수집된 뉴스가 없습니다."\n',
        encoding="utf-8",
    )
    covered, missing = cache_coverage(no_symbol_news_cache, ["005930"])
    if covered or missing != ["005930"]:
        failures.append(f"no-news placeholder should be incomplete: covered={covered}, missing={missing}")
    no_news_counts = cache_evidence_counts(no_symbol_news_cache, ["005930"])
    if no_news_counts.get("present_symbol_count") != 1 or no_news_counts.get("usable_symbol_count") != 0:
        failures.append(f"no-news cache counts did not distinguish present from usable: {no_news_counts}")
    stale_symbol_news_cache = workspace / "stale-symbol-news-cache.yaml"
    stale_symbol_news_cache.write_text(
        'date: "2026-06-18"\nsymbols:\n  "005930":\n    articles:\n      - article_date: "2020-01-01"\n        sentiment: neutral\n        content: "old article"\n',
        encoding="utf-8",
    )
    covered, missing = symbol_news_cache_coverage(stale_symbol_news_cache, ["005930"], "2026-06-18")
    if covered or missing != ["005930"]:
        failures.append(f"stale-only news cache should not satisfy same-date coverage: covered={covered}, missing={missing}")
    stale_news_counts = symbol_news_cache_evidence_counts(stale_symbol_news_cache, ["005930"], "2026-06-18")
    if stale_news_counts.get("present_symbol_count") != 1 or stale_news_counts.get("usable_symbol_count") != 0:
        failures.append(f"stale-only news cache counts should distinguish present from usable: {stale_news_counts}")
    fresh_symbol_news_cache = workspace / "fresh-symbol-news-cache.yaml"
    fresh_symbol_news_cache.write_text(
        'date: "2026-06-18"\nsymbols:\n  "005930":\n    articles:\n      - article_date: "2026-06-18T09:30:00+09:00"\n        sentiment: positive\n        content: "fresh article"\n',
        encoding="utf-8",
    )
    covered, missing = symbol_news_cache_coverage(fresh_symbol_news_cache, ["005930"], "2026-06-18")
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
    return failures


def step_financial_cache_reuse_and_memory_dir_checks(workspace: Path, portfolio_path: Path) -> list[str]:
    """ETF-aware financial cache reuse, optional-stage-skip status propagation, and env-overridden cache memory directories."""
    failures: list[str] = []
    # Written to disk by step_cache_coverage_and_evidence_checks, which always runs first.
    stale_symbol_news_cache = workspace / "stale-symbol-news-cache.yaml"
    fresh_symbol_news_cache = workspace / "fresh-symbol-news-cache.yaml"
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
            symbol_news_cache_path="",
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
    if etf_probe.covered_cache_path("symbol_news", str(stale_symbol_news_cache), ["005930"], detail="stale news cache"):
        failures.append("stale-only news cache should not skip same-date news collection")
    if not etf_probe.covered_cache_path("symbol_news", str(fresh_symbol_news_cache), ["005930"], detail="fresh news cache"):
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
            symbol_news_cache_path="",
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
    old_news_memory = os.environ.get("SYMBOL_NEWS_CACHE_MEMORY_DIR")
    try:
        env_financial_dir = workspace / "env-financial-cache"
        env_news_dir = workspace / "env-symbol-news-cache"
        env_financial_dir.mkdir(parents=True, exist_ok=True)
        env_news_dir.mkdir(parents=True, exist_ok=True)
        (env_financial_dir / "financial-2026-06-18.yaml").write_text('date: "2026-06-18"\nsymbols: {}\n', encoding="utf-8")
        (env_news_dir / "symbol-news-2026-06-18.yaml").write_text('date: "2026-06-18"\nsymbols: {}\n', encoding="utf-8")
        os.environ["COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR"] = str(env_financial_dir)
        os.environ["SYMBOL_NEWS_CACHE_MEMORY_DIR"] = str(env_news_dir)
        if Path(stage_status_probe.default_cache_path("financial")).parent != env_financial_dir:
            failures.append("financial env memory dir was not preferred")
        if Path(stage_status_probe.default_cache_path("symbol_news")).parent != env_news_dir:
            failures.append("news env memory dir was not preferred")
    finally:
        if old_financial_memory is None:
            os.environ.pop("COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR", None)
        else:
            os.environ["COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR"] = old_financial_memory
        if old_news_memory is None:
            os.environ.pop("SYMBOL_NEWS_CACHE_MEMORY_DIR", None)
        else:
            os.environ["SYMBOL_NEWS_CACHE_MEMORY_DIR"] = old_news_memory

    old_codex_home_env = os.environ.get("CODEX_HOME")
    try:
        codex_home = workspace / "codex-home"
        installed_financial_script = codex_home / "skills" / "collect-financial-information" / "scripts" / "financial_cache.py"
        installed_financial_script.parent.mkdir(parents=True, exist_ok=True)
        installed_financial_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        os.environ["CODEX_HOME"] = str(codex_home)

        # Production intentionally checks /app/skills/... before the CODEX_HOME
        # candidate (see optional_cache_script_candidates), so this fixture is
        # only a valid probe of the CODEX_HOME fallback when /app/skills/...
        # does not exist. That candidate exists in the real deployed image but
        # not on a bare host, so its existence is machine-dependent, not
        # something this test controls. Assert the CODEX_HOME candidate is
        # present at the documented fallback position instead of depending on
        # whether the earlier /app/skills candidate happens to exist here.
        app_skills_candidate = Path("/app/skills/collect-financial-information/scripts/financial_cache.py")
        codex_home_skills_candidate = Path("/codex-home/skills/collect-financial-information/scripts/financial_cache.py")
        candidates = stage_status_probe.optional_cache_script_candidates("financial")
        if installed_financial_script not in candidates:
            failures.append(f"CODEX_HOME financial cache script candidate missing: {candidates}")
        elif app_skills_candidate not in candidates or candidates.index(app_skills_candidate) != 0:
            failures.append(f"/app/skills candidate is no longer checked first: {candidates}")
        elif candidates.index(installed_financial_script) != 1:
            failures.append(f"CODEX_HOME candidate is not the second (fallback) candidate: {candidates}")
        elif codex_home_skills_candidate not in candidates or candidates.index(codex_home_skills_candidate) != 2:
            failures.append(f"/codex-home/skills candidate is no longer checked last: {candidates}")

        # Resolve under a controlled existence seam so the outcome does not
        # depend on whether /app/skills/... happens to exist on this machine:
        # only the CODEX_HOME-installed fixture script "exists" here.
        real_exists = Path.exists

        def fake_exists(path: Path) -> bool:
            if path == app_skills_candidate:
                return False
            return real_exists(path)

        with mock.patch.object(Path, "exists", fake_exists):
            resolved_installed_script = stage_status_probe.optional_cache_script("financial")
        if resolved_installed_script != installed_financial_script:
            failures.append(f"installed financial cache script was not resolved via CODEX_HOME: {resolved_installed_script}")
    finally:
        if old_codex_home_env is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old_codex_home_env
    return failures


def step_optional_cache_probe_checks(workspace: Path, portfolio_path: Path) -> list[str]:
    """Optional per-stage cache collection tolerates partial failures and empty-cache fallbacks."""
    failures: list[str] = []
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
                subdir = "collect-financial-information" if domain == "financial" else "symbol-news-cache"
                prefix = "financial" if domain == "financial" else "symbol-news"
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
                subdir = "collect-financial-information" if domain == "financial" else "symbol-news-cache"
                prefix = "financial" if domain == "financial" else "symbol-news"
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
    for probe_script in (workspace / "financial_cache_probe.py", workspace / "symbol_news_cache_probe.py"):
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
            symbol_news_cache_path="",
            main_events="",
            date="2026-06-18",
            reuse_existing_artifacts=True,
            skip_account=False,
            max_workers=3,
        )
    )
    financial_partial = optional_probe.collect_optional_cache("financial", ["005930", "000660"])
    news_partial = optional_probe.collect_optional_cache("symbol_news", ["005930", "000660"])
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
                subdir = "collect-financial-information" if domain == "financial" else "symbol-news-cache"
                prefix = "financial" if domain == "financial" else "symbol-news"
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
        workspace / "memory" / "symbol-news-cache" / "symbol-news-2026-06-18.yaml",
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
            symbol_news_cache_path="",
            main_events="",
            date="2026-06-18",
            reuse_existing_artifacts=True,
            skip_account=False,
            max_workers=3,
        )
    )
    if empty_cache_probe.collect_optional_cache("financial", ["005930"]):
        failures.append("empty financial cache should not be returned as partial data")
    empty_news_path = empty_cache_probe.collect_optional_cache("symbol_news", ["005930"])
    if not empty_news_path:
        failures.append("empty news cache should be returned so zero usable articles can be reported")
    news_stage = empty_cache_probe.stages[-1] if empty_cache_probe.stages else {}
    if news_stage.get("stage") != "symbol-news-cache" or "zero usable articles" not in str(news_stage.get("detail")):
        failures.append(f"empty news cache stage did not describe zero usable articles: {news_stage}")
    return failures


def step_retry_review_and_rounding_probe_checks(workspace: Path, portfolio_path: Path) -> list[str]:
    """Deferred-buy retry review context and half-up target-quantity rounding."""
    failures: list[str] = []
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
            symbol_news_cache_path="",
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
            symbol_news_cache_path="",
            main_events="",
            date="2026-06-18",
            reuse_existing_artifacts=True,
            skip_account=False,
            max_workers=3,
        )
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
                        "decision_basis": "thesis",
                        "reason_code": "increase_target",
                        "one_line_reason": "half-up self-test",
                        "thesis_definition": {
                            "core_rationale": "half-up self-test entry rationale",
                            "invalidation_conditions": [
                                {"condition_id": "self-test-condition", "description": "self-test invalidation condition"}
                            ],
                        },
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
    return failures


def step_same_day_buy_and_invalid_final_probe_checks(workspace: Path, portfolio_path: Path) -> list[str]:
    """Same-day buy exposure baseline and invalid final-position validation."""
    failures: list[str] = []
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
            symbol_news_cache_path="",
            main_events="",
            date="2026-06-18",
            reuse_existing_artifacts=True,
            skip_account=False,
            max_workers=3,
        )
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
    same_day_symbols = same_day_review.get("symbols") if isinstance(same_day_review.get("symbols"), list) else []
    if len(same_day_symbols) != 1 or same_day_symbols[0].get("target_position_value_krw") != 140000:
        failures.append(f"same-day target without optional additional_buy_reason was not accepted: {same_day_review}")
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
        "decision_basis": "thesis",
        "thesis_definition": {
            "core_rationale": "same-day self-test entry rationale",
            "invalidation_conditions": [
                {"condition_id": "same-day-self-test-condition", "description": "same-day self-test invalidation condition"}
            ],
        },
    }
    unknown_normalized, unknown_errors = same_day_pipeline.derive_judge_final_quantity(unknown_item, {})
    if unknown_normalized is None or unknown_errors:
        failures.append(f"unknown same-day history unexpectedly blocked the Judge target: {unknown_normalized} {unknown_errors}")
    unknown_item["additional_buy_reason"] = "새 가격 돌파와 포트폴리오 여유가 확인됨"
    reasoned_normalized, reasoned_errors = same_day_pipeline.derive_judge_final_quantity(unknown_item, {})
    if reasoned_normalized is None or reasoned_errors:
        failures.append(f"optional additional_buy_reason was not preserved: {reasoned_normalized} {reasoned_errors}")
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
    absent_normalized, absent_errors = same_day_pipeline.derive_judge_final_quantity(confirmed_absent_item, {})
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
            symbol_news_cache_path="",
            main_events="",
            date="2026-06-18",
            reuse_existing_artifacts=True,
            skip_account=False,
            max_workers=3,
        )
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
    return failures




def step_main_pipeline_run_checks(workspace: Path, run_dir: Path, portfolio_path: Path) -> list[str]:
    """End-to-end pipeline run against a fake codex binary and fake market-index-snapshot script, with strategy-policy override precedence."""
    failures: list[str] = []
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
                exchange="SOR",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                symbol_news_cache_path="",
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
        if summary.get("account_collection_status") != "success" or summary.get("order_gate_status") != "not_run":
            failures.append(
                "pipeline summary mixed account collection and pending order gate states: "
                f"collection={summary.get('account_collection_status')}, gate={summary.get('order_gate_status')}"
            )
        run_stage_names = [item.get("stage") for item in load_json(run_dir / "run.json").get("stages", []) if isinstance(item, dict)]
        if any(str(stage).startswith("judge-debate") for stage in run_stage_names):
            failures.append(f"pipeline still executed a standalone debate stage: {run_stage_names}")
        if (run_dir / "judge-debate.json").exists() or (run_dir / "debate").exists():
            failures.append("pipeline still produced standalone debate artifacts")
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
            or run_config.get("exchange") != "SOR"
            or run_config.get("market") != "UN"
        ):
            failures.append(f"run config did not record strategy policy path/hash: {run_config}")
        order_path_selection = (summary.get("execution") or {}).get("order_path_selection") if isinstance(summary.get("execution"), dict) else {}
        if order_path_selection.get("resolved") != "immediate" or order_path_selection.get("reason") != "auto_regular_session":
            failures.append(f"pipeline did not resolve auto order path to immediate: {order_path_selection}")
        command_log = load_json(run_dir / "pipeline-command-log.json")
        decision_commands = [
            item.get("command")
            for item in command_log.get("commands", [])
            if isinstance(item, dict) and item.get("stage") == "decision-brief"
        ]
        decision_command = decision_commands[-1] if decision_commands else []
        if "--strategy-policy-config" not in decision_command:
            failures.append(f"decision-brief command should receive strategy policy config: {decision_commands}")
        second_spec_commands = [
            item.get("command")
            for item in command_log.get("commands", [])
            if isinstance(item, dict) and item.get("stage") == "second-spec"
        ]
        second_spec_command = second_spec_commands[-1] if second_spec_commands else []
        strategy_config_index = (
            second_spec_command.index("--strategy-policy-config")
            if "--strategy-policy-config" in second_spec_command
            else -1
        )
        if (
            strategy_config_index < 0
            or second_spec_command[strategy_config_index + 1 : strategy_config_index + 2]
            != [str(override_policy.resolve())]
        ):
            failures.append(
                f"second-spec command should receive the same strategy policy config: {second_spec_commands}"
            )
        expected_symbol_news_date_index = decision_command.index("--expected-symbol-news-date") if "--expected-symbol-news-date" in decision_command else -1
        if expected_symbol_news_date_index < 0 or decision_command[expected_symbol_news_date_index + 1 : expected_symbol_news_date_index + 2] != ["2026-06-18"]:
            failures.append(f"decision-brief command should receive the run news date: {decision_commands}")
        if "--news-context-json" not in decision_command:
            failures.append(f"decision-brief command should receive the deduplicated news context: {decision_commands}")
        execution_commands = [
            item.get("command")
            for item in command_log.get("commands", [])
            if isinstance(item, dict) and item.get("stage") == "execution-plan"
        ]
        if not execution_commands or "--decision-brief" in execution_commands[-1]:
            failures.append(f"execution-plan command should rely on the default decision-brief path: {execution_commands}")
        elif execution_commands[-1][-2:] != ["--exchange", "SOR"]:
            failures.append(f"execution-plan command did not preserve SOR routing: {execution_commands}")
        execution_payload = load_json(run_dir / "execution.json")
        if execution_payload.get("exchange") != "SOR":
            failures.append(f"execution artifact did not preserve SOR routing: {execution_payload}")
        execution_by_symbol = {
            symbol_key(item): item for item in execution_payload.get("orders", []) if isinstance(item, dict)
        }
        if as_int(execution_by_symbol.get("005930", {}).get("order_price")) != 70000:
            failures.append(
                "execution-plan did not fall back to decision-brief price for a new holding with missing account current_price"
            )
        if summary["token_usage"]["subagents"]["total_tokens"] != 360:
            failures.append(f"unexpected subagent token total: {summary['token_usage']}")
        if summary["token_usage"]["main"]["total_tokens"] != 17 or summary["token_usage"]["total"]["total_tokens"] != 377:
            failures.append(f"unexpected pipeline token summary with main events: {summary['token_usage']}")
        review_summary = summary.get("review_summary") if isinstance(summary.get("review_summary"), dict) else {}
        # Both symbols are now in scope (one held, one unheld top-ranked) with no score band.
        if review_summary.get("symbol_count") != 2 or not review_summary.get("symbols"):
            failures.append(f"pipeline summary omitted compact review summary: {review_summary}")
        elif review_summary["symbols"][1].get("target_position_value_krw") != 70000:
            failures.append(f"pipeline summary omitted judge target position value: {review_summary}")
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
        if not str(artifacts.get("news_context", "")).endswith("news-context.json"):
            failures.append(f"pipeline summary omitted news context artifact path: {artifacts}")
        if not str(artifacts.get("html_report", "")).endswith("daily-trading-report.html"):
            failures.append(f"pipeline summary omitted HTML report artifact path: {artifacts}")
        run_payload = load_json(run_dir / "run.json")
        if not str(run_payload.get("model_usage", "")).endswith("model-usage.jsonl"):
            failures.append(f"run.json omitted model usage artifact path: {run_payload}")
        evidence_summary = summary.get("evidence_summary") if isinstance(summary.get("evidence_summary"), dict) else {}
        if not isinstance(evidence_summary.get("symbol_news"), dict) or "display_text" not in evidence_summary.get("symbol_news", {}):
            failures.append(f"pipeline summary omitted displayable symbol news evidence status: {evidence_summary}")
        if not isinstance(evidence_summary.get("market_news"), dict) or "display_text" not in evidence_summary.get("market_news", {}):
            failures.append(f"pipeline summary omitted displayable market news evidence status: {evidence_summary}")
        investor_flow_summary = evidence_summary.get("investor_flow") if isinstance(evidence_summary.get("investor_flow"), dict) else {}
        if (
            investor_flow_summary.get("status") != "partial"
            or investor_flow_summary.get("usable_symbol_count") != 1
            or investor_flow_summary.get("missing_usable_symbol_count") != 1
        ):
            failures.append(f"pipeline summary omitted investor flow coverage: {investor_flow_summary}")
        reporting_view = summary.get("reporting_view") if isinstance(summary.get("reporting_view"), dict) else {}
        reporting_account = reporting_view.get("account") if isinstance(reporting_view.get("account"), dict) else {}
        full_account_view = reporting_account.get("full_account") if isinstance(reporting_account.get("full_account"), dict) else {}
        domestic_account_view = (
            reporting_account.get("domestic_trading_account") if isinstance(reporting_account.get("domestic_trading_account"), dict) else {}
        )
        if full_account_view.get("total_asset_amount") != 20000000 or domestic_account_view.get("total_evaluation_amount") != 1500000:
            failures.append(f"reporting_view did not keep full-account and domestic amounts distinct: {reporting_account}")
        if full_account_view.get("total_asset_amount") == domestic_account_view.get("total_evaluation_amount"):
            failures.append("reporting_view full-account and domestic amounts must not collapse to the same figure")
        if full_account_view.get("source_api") != "inquire_account_balance" or not full_account_view.get("observed_at"):
            failures.append(f"reporting_view full_account omitted existing account-asset provenance fields: {full_account_view}")
        if not domestic_account_view.get("snapshot_generated_at") or domestic_account_view.get("source_artifact") != "account-before-order.json":
            failures.append(f"reporting_view domestic account omitted snapshot/source provenance fields: {domestic_account_view}")
        reporting_orders = reporting_view.get("orders") if isinstance(reporting_view.get("orders"), dict) else {}
        active_order_view = reporting_orders.get("active") if isinstance(reporting_orders.get("active"), dict) else {}
        history_view = (
            reporting_orders.get("history_or_reservation_rows") if isinstance(reporting_orders.get("history_or_reservation_rows"), dict) else {}
        )
        current_run_submitted_view = (
            reporting_orders.get("current_run_submitted") if isinstance(reporting_orders.get("current_run_submitted"), dict) else {}
        )
        if active_order_view.get("count") is not None or active_order_view.get("lookup_status") != "not_looked_up":
            failures.append(
                f"reporting_view active order count must stay unknown without a lifecycle-confirmed lookup: {active_order_view}"
            )
        if history_view.get("raw_row_count") != 0:
            failures.append(f"reporting_view history/reservation raw row count omitted: {history_view}")
        if current_run_submitted_view.get("scope") != "current_run_submitted_orders" or current_run_submitted_view.get("count") != 0:
            failures.append(f"reporting_view current-run submitted scope was not truthfully named/counted: {current_run_submitted_view}")
        reporting_domains = reporting_view.get("evidence_domains") if isinstance(reporting_view.get("evidence_domains"), dict) else {}
        reporting_symbol_news = reporting_domains.get("symbol_news") if isinstance(reporting_domains.get("symbol_news"), dict) else {}
        reporting_market_news = reporting_domains.get("market_news") if isinstance(reporting_domains.get("market_news"), dict) else {}
        if reporting_symbol_news.get("blocks_trading") is not False:
            failures.append(f"reporting_view omitted symbol_news non-blocking contract: {reporting_symbol_news}")
        if reporting_market_news.get("blocks_trading") is not False or reporting_market_news.get("scope") != "market_news_context_quality":
            failures.append(f"reporting_view omitted market_news context contract: {reporting_market_news}")
        reporting_investor_flow = reporting_domains.get("investor_flow") if isinstance(reporting_domains.get("investor_flow"), dict) else {}
        if (
            reporting_investor_flow.get("status") != "partial"
            or reporting_investor_flow.get("usable_symbol_count") != 1
            or reporting_investor_flow.get("wanted_symbol_count") != 2
            or reporting_investor_flow.get("blocks_trading") is not False
        ):
            failures.append(f"reporting_view omitted partial investor_flow coverage: {reporting_investor_flow}")
        run_status_view = reporting_view.get("run_status") if isinstance(reporting_view.get("run_status"), dict) else {}
        if run_status_view.get("delivery") != "not_observed_at_summary_build_time":
            failures.append(f"reporting_view claimed an observed delivery status before delivery happens: {run_status_view}")
        if run_status_view.get("pipeline_summary") != summary.get("status"):
            failures.append(f"reporting_view pipeline_summary scope did not track pipeline status: {run_status_view}")
        if "report_generation" in run_status_view:
            failures.append(
                f"reporting_view must not claim report_generation was observed before Markdown/Telegram/HTML are written: {run_status_view}"
            )
        if run_status_view.get("account_collection") != summary.get("account_collection_status"):
            failures.append(f"reporting_view account_collection scope diverged from account collection status: {run_status_view}")
        if run_status_view.get("evidence_collection") != "partial":
            failures.append(f"reporting_view evidence_collection scope did not reflect partial evidence domains: {run_status_view}")
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
            for required_text in ("당일 누적 거래·판단 리포트", "계좌·시장 통합 추이", "회차별 거래·전체 종목 판단"):
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
            if "- 주문가능금액: 900,000원" not in report_text:
                failures.append("portfolio report did not use orderable_cash_amount")
            if "| 005930 | 005930 | 0 | 70,000 | 1 |" not in report_text:
                failures.append("portfolio report omitted judge target position value")
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
    return failures



def step_submit_orders_probe_checks(workspace: Path, run_dir: Path, portfolio_path: Path) -> list[str]:
    """submit_orders=True runs lifecycle preflight before decision-brief and executes orders end to end."""
    failures: list[str] = []
    # Written to disk by step_main_pipeline_run_checks, which always runs first.
    fake_codex = workspace / "fake-codex"
    fake_market_index_snapshot = workspace / "fake-market-index-snapshot.py"
    override_policy = workspace / "override-strategy-policy.yaml"
    main_events = workspace / "main-events.jsonl"
    old_codex_bin = os.environ.get("CODEX_BIN")
    old_reuse = os.environ.get("CODEX_SUBAGENT_REUSE_SUCCESS")
    old_market_index_snapshot = os.environ.get("DAILY_TRADING_MARKET_INDEX_SNAPSHOT_SCRIPT")
    old_strategy_policy = os.environ.get(STRATEGY_POLICY_CONFIG_ENV)
    os.environ["CODEX_BIN"] = str(fake_codex)
    os.environ["CODEX_SUBAGENT_REUSE_SUCCESS"] = "0"
    os.environ["DAILY_TRADING_MARKET_INDEX_SNAPSHOT_SCRIPT"] = str(fake_market_index_snapshot)
    os.environ[STRATEGY_POLICY_CONFIG_ENV] = str(override_policy)
    try:
        fake_execute_orders = workspace / "fake-execute-orders.py"
        fake_execute_orders.write_text(
            """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[sys.argv.index("--output-dir") + 1])
execution_path = output_dir / "execution.json"
account_path = output_dir / "account-before-order.json"
if "preflight" in sys.argv:
    account = json.loads(account_path.read_text(encoding="utf-8"))
    account["active_order_lookup_performed"] = True
    account["active_orders"] = []
    for item in account.get("symbols", []):
        if isinstance(item, dict):
            item["pending_and_reserved_buy_quantity"] = 0
            item["pending_and_reserved_sell_quantity"] = 0
            item["holding_state_status"] = "consistent"
            item["holding_state_reasons"] = []
    lifecycle = {
        "status": "success",
        "lookup_complete": True,
        "active_order_count": 0,
        "previous_submitted_cash_order_count": 0,
        "holding_state_issue_count": 0,
        "holding_state_issues": [],
    }
    account_path.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "order-lifecycle.json").write_text(json.dumps(lifecycle, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(lifecycle, ensure_ascii=False))
    raise SystemExit(0)
execution = json.loads(execution_path.read_text(encoding="utf-8"))
if "terminal-reconcile" in sys.argv:
    filled = 0
    for item in execution.get("orders", []):
        if isinstance(item, dict) and item.get("result") == "submitted" and item.get("order_path") == "immediate":
            item["broker_reconciliation"] = {"status": "filled", "terminal": True, "filled_quantity": item.get("validated_order_quantity", 0)}
            filled += 1
    execution["broker_reconciliation"] = {"status": "success", "submitted_cash_order_count": filled, "filled_order_count": filled}
    execution["status"] = "success"
    execution["errors"] = []
    execution_path.write_text(json.dumps(execution, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(execution, ensure_ascii=False))
    raise SystemExit(0)
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
        if item.get("order_path") == "immediate":
            item["broker_reconciliation"] = {"status": "pending", "terminal": False, "remaining_quantity": item.get("validated_order_quantity", 0)}
execution["status"] = "partial"
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
                symbol_news_cache_path="",
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
        if "order-lifecycle-preflight" not in submit_stages:
            failures.append(f"submit-orders pipeline did not run lifecycle preflight: {submit_stages}")
        elif submit_stages.index("order-lifecycle-preflight") > submit_stages.index("decision-brief"):
            failures.append(f"lifecycle preflight did not run before Judge inputs: {submit_stages}")
        if "order-execution" not in submit_stages:
            failures.append(f"submit-orders pipeline did not run order-execution stage: {submit_stages}")
        if submit_summary.get("status") != "success":
            failures.append(f"submit-orders summary did not reflect fake submitted order: {submit_summary.get('status')}")
        submit_execution = submit_summary.get("execution") if isinstance(submit_summary.get("execution"), dict) else {}
        if submit_execution.get("requires_main_agent_order_execution") is not False:
            failures.append(f"submit-orders summary did not clear execution handoff: {submit_execution}")
        broker = submit_execution.get("broker_reconciliation") if isinstance(submit_execution.get("broker_reconciliation"), dict) else {}
        if broker.get("status") != "success" or broker.get("filled_order_count") != broker.get("submitted_cash_order_count"):
            failures.append(f"terminal reconciliation was not reflected in the final summary: {submit_execution}")
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
    return failures




def run_self_test() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp_name:
        workspace = Path(tmp_name)
        run_dir = workspace / "reports" / "runs" / "pipeline-self-test"
        portfolio_path = write_self_test_fixtures(workspace, run_dir)
        failures.extend(step_cache_coverage_and_evidence_checks(workspace, run_dir))
        failures.extend(step_financial_cache_reuse_and_memory_dir_checks(workspace, portfolio_path))
        failures.extend(step_optional_cache_probe_checks(workspace, portfolio_path))
        failures.extend(step_retry_review_and_rounding_probe_checks(workspace, portfolio_path))
        failures.extend(step_same_day_buy_and_invalid_final_probe_checks(workspace, portfolio_path))
        failures.extend(step_main_pipeline_run_checks(workspace, run_dir, portfolio_path))
        failures.extend(step_submit_orders_probe_checks(workspace, run_dir, portfolio_path))

    payload = {"status": "passed" if not failures else "failed", "failures": failures}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not failures else 1


class RunSelfTestStepsAreIndividuallyDiscoverableTest(unittest.TestCase):
    """Real (non-mocked) execution of every run_self_test step_* helper, so
    each one is reachable from ordinary unittest discovery and not only from
    the mocked wrapper-orchestration test below. Steps have a genuine
    prerequisite order (later steps reuse fixtures/artifacts an earlier step
    wrote to the shared workspace), so setUpClass runs them once in that
    order and each test method asserts on its own step's stored result."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._temp_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._temp_dir.cleanup)
        cls.workspace = Path(cls._temp_dir.name)
        cls.run_dir = cls.workspace / "reports" / "runs" / "pipeline-self-test"
        cls.portfolio_path = write_self_test_fixtures(cls.workspace, cls.run_dir)
        cls.cache_coverage_failures = step_cache_coverage_and_evidence_checks(cls.workspace, cls.run_dir)
        cls.financial_cache_and_memory_dir_failures = step_financial_cache_reuse_and_memory_dir_checks(cls.workspace, cls.portfolio_path)
        cls.optional_cache_probe_failures = step_optional_cache_probe_checks(cls.workspace, cls.portfolio_path)
        cls.retry_and_rounding_failures = step_retry_review_and_rounding_probe_checks(cls.workspace, cls.portfolio_path)
        cls.same_day_and_invalid_final_failures = step_same_day_buy_and_invalid_final_probe_checks(cls.workspace, cls.portfolio_path)
        cls.main_pipeline_run_failures = step_main_pipeline_run_checks(cls.workspace, cls.run_dir, cls.portfolio_path)
        cls.submit_orders_probe_failures = step_submit_orders_probe_checks(cls.workspace, cls.run_dir, cls.portfolio_path)

    def test_step_cache_coverage_and_evidence_checks(self) -> None:
        self.assertEqual(self.cache_coverage_failures, [])

    def test_step_financial_cache_reuse_and_memory_dir_checks(self) -> None:
        self.assertEqual(self.financial_cache_and_memory_dir_failures, [])

    def test_step_optional_cache_probe_checks(self) -> None:
        self.assertEqual(self.optional_cache_probe_failures, [])

    def test_step_retry_review_and_rounding_probe_checks(self) -> None:
        self.assertEqual(self.retry_and_rounding_failures, [])

    def test_step_same_day_buy_and_invalid_final_probe_checks(self) -> None:
        self.assertEqual(self.same_day_and_invalid_final_failures, [])

    def test_step_main_pipeline_run_checks(self) -> None:
        self.assertEqual(self.main_pipeline_run_failures, [])

    def test_step_submit_orders_probe_checks(self) -> None:
        self.assertEqual(self.submit_orders_probe_failures, [])


class RunDailyTradingPipelineSelfTest(unittest.TestCase):
    def test_self_test_suite_runs_every_step_and_reports_success(self) -> None:
        """Wrapper-orchestration check only: each step's real behavior is
        covered by the granular tests below, so this mocks every step
        instead of re-running the whole fake-codex pipeline scenario a
        second time."""
        step_names = [
            "step_cache_coverage_and_evidence_checks",
            "step_financial_cache_reuse_and_memory_dir_checks",
            "step_optional_cache_probe_checks",
            "step_retry_review_and_rounding_probe_checks",
            "step_same_day_buy_and_invalid_final_probe_checks",
            "step_main_pipeline_run_checks",
            "step_submit_orders_probe_checks",
        ]
        patchers = [mock.patch(f"{__name__}.{name}", return_value=[]) for name in step_names]
        mocks = [patcher.start() for patcher in patchers]
        self.addCleanup(lambda: [patcher.stop() for patcher in patchers])

        result = run_self_test()

        self.assertEqual(result, 0)
        for step_mock in mocks:
            step_mock.assert_called_once()

    def test_self_test_suite_reports_failure_when_a_step_fails(self) -> None:
        with mock.patch(f"{__name__}.step_cache_coverage_and_evidence_checks", return_value=["boom"]), mock.patch(
            f"{__name__}.step_financial_cache_reuse_and_memory_dir_checks", return_value=[]
        ), mock.patch(f"{__name__}.step_optional_cache_probe_checks", return_value=[]), mock.patch(
            f"{__name__}.step_retry_review_and_rounding_probe_checks", return_value=[]
        ), mock.patch(f"{__name__}.step_same_day_buy_and_invalid_final_probe_checks", return_value=[]), mock.patch(
            f"{__name__}.step_main_pipeline_run_checks", return_value=[]
        ), mock.patch(
            f"{__name__}.step_submit_orders_probe_checks", return_value=[]
        ):
            result = run_self_test()

        self.assertEqual(result, 1)

    def test_build_reporting_view_ignores_nonzero_raw_history_rows_without_lifecycle_confirmation(self) -> None:
        # This is the original bug: a raw active_orders history/reservation list with several
        # rows must never be reported as a confirmed active-order count when the lifecycle
        # preflight never ran/completed. The raw row count still belongs under the
        # history/reservation reference scope, unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "history-rows-probe"
            run_dir.mkdir(parents=True, exist_ok=True)
            pipeline = Pipeline(
                argparse.Namespace(
                    command="summarize",
                    workspace_dir=str(workspace),
                    output_dir=str(run_dir),
                    run_id="history-rows-probe",
                    started_at="2026-06-18T09:00:00+09:00",
                )
            )
            account = {
                "active_order_lookup_performed": False,
                "active_orders": [
                    {"order_id": "1"},
                    {"order_id": "2"},
                    {"order_id": "3"},
                    {"order_id": "4"},
                    {"order_id": "5"},
                ],
            }
            reporting_view = pipeline.build_reporting_view(
                account_display_summary={},
                account_asset_summary={},
                account=account,
                order_lifecycle_view={"status": "not_run", "lookup_complete": False, "active_order_count": 0},
                current_run_orders=[],
                evidence_summary={},
                account_collection_status="success",
                order_gate_status="not_run",
                execution_status="success",
                pipeline_status="success",
            )
            active_view = reporting_view["orders"]["active"]
            history_view = reporting_view["orders"]["history_or_reservation_rows"]
            self.assertIsNone(active_view["count"])
            self.assertEqual(active_view["lookup_status"], "not_looked_up")
            self.assertEqual(history_view["raw_row_count"], 5)

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
                run_dir / "analyst-review.json",
                {
                    "symbols": [
                        {"symbol_id": "005930", "final_first_score": 7.0},
                        {"symbol_id": "000660", "final_first_score": None},
                        {"symbol_id": "035720", "final_first_score": 5.0},
                        {"symbol_id": "402340", "final_first_score": 6.0},
                        {"symbol_id": "111111", "final_first_score": None},
                    ]
                },
            )
            # 402340 is in review scope but its judge decision is invalid, so it must stay unresolved.
            write_json(
                run_dir / "judge-review-spec.json",
                {"review_scope_reasons": {"005930": "unheld_score_rank", "000660": "held_position", "402340": "active_order"}},
            )
            write_json(
                run_dir / "judge-review.json",
                {
                    "status": "success",
                    "errors": [
                        {
                            "stage": "judge-review",
                            "source": "pipeline",
                            "code": "invalid_final_holding_quantity",
                            "message": "402340: final_holding_quantity must be a non-negative integer",
                            "required": True,
                        }
                    ],
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
                    {"symbol_id": "402340", "current_live_holding_quantity": 0, "symbol_name": "SK스퀘어"},
                ]
            }
            review = pipeline.build_review_summary(account, {"orders": []})
            # Final decisions come from the current->final holding-quantity direction of resolved rows.
            self.assertEqual(review["final_buy_count"], 1)
            self.assertEqual(review["final_sell_count"], 1)
            # The invalid candidate (402340) is not silently folded into 유지: naive scored-minus-directions
            # would report 2 holds, but the delta-derived count is 1 (only 035720).
            self.assertEqual(review["final_hold_count"], 1)
            self.assertEqual(review["unresolved_review_scope_count"], 1)
            self.assertNotIn("402340", {row["symbol_id"] for row in review["symbols"]})
            # Review-scope composition counts remain for diagnostics but are no longer the reported verdict.
            self.assertEqual(review["unheld_review_scope_count"], 1)
            self.assertEqual(review["held_review_scope_count"], 1)
            self.assertEqual(review["active_order_review_scope_count"], 1)
            # Only a valid-scored, out-of-scope symbol counts as "미선정";
            # missing-score rows are retained for audit but are not scored rows.
            self.assertEqual(review["hold_symbol_count"], 1)
            # 402340 is in-scope-but-unresolved (invalid Judge result), never "not shortlisted":
            # it must carry its scope reason and the Judge error code, with a resolved symbol_name.
            unresolved = review["unresolved_review_scope"]
            self.assertEqual(len(unresolved), 1)
            self.assertEqual(unresolved[0]["symbol_id"], "402340")
            self.assertEqual(unresolved[0]["symbol_name"], "SK스퀘어")
            self.assertEqual(unresolved[0]["scope_reason"], "active_order")
            self.assertEqual(unresolved[0]["judge_error_code"], "invalid_final_holding_quantity")

    def test_build_review_summary_canonical_action_enum_counts_and_expected_baseline_fallback(self) -> None:
        # canonical_action (derive_action) is increase|hold|reduce|exit, never buy/sell/hold.
        # Counting against buy/sell silently zeroes every bucket, which is exactly the bug Codex
        # reproduced with a canonical_action="reduce" fixture.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "canonical-enum-probe"
            run_dir.mkdir(parents=True, exist_ok=True)
            pipeline = Pipeline(
                argparse.Namespace(
                    command="summarize",
                    workspace_dir=str(workspace),
                    output_dir=str(run_dir),
                    run_id="canonical-enum-probe",
                    started_at="2026-06-18T09:00:00+09:00",
                )
            )
            write_json(
                run_dir / "judge-review.json",
                {
                    "status": "success",
                    "symbols": [
                        {"symbol_id": "005930", "final_holding_quantity": 5, "canonical_action": "increase"},
                        {"symbol_id": "000660", "final_holding_quantity": 0, "canonical_action": "exit"},
                        {"symbol_id": "035720", "final_holding_quantity": 2, "canonical_action": "reduce"},
                        {"symbol_id": "402340", "final_holding_quantity": 3, "canonical_action": "hold"},
                    ],
                },
            )
            account = {
                "symbols": [
                    {"symbol_id": "005930", "current_live_holding_quantity": 0},
                    {"symbol_id": "000660", "current_live_holding_quantity": 3},
                    # No execution row below for 035720: expected_holding_quantity must fall back to
                    # account current + pending buy - pending sell instead of staying unresolved.
                    {
                        "symbol_id": "035720",
                        "current_live_holding_quantity": 5,
                        "pending_and_reserved_buy_quantity": 1,
                        "pending_and_reserved_sell_quantity": 2,
                    },
                    {"symbol_id": "402340", "current_live_holding_quantity": 3},
                ]
            }
            execution = {
                "orders": [
                    {"symbol_id": "005930", "expected_holding_quantity": 0, "result": "submitted", "direction": "buy"},
                    {"symbol_id": "000660", "expected_holding_quantity": 3, "result": "submitted", "direction": "sell"},
                ]
            }
            review = pipeline.build_review_summary(account, execution)
            self.assertEqual(review["canonical_increase_count"], 1)
            self.assertEqual(review["canonical_reduce_count"], 1)
            self.assertEqual(review["canonical_exit_count"], 1)
            self.assertEqual(review["canonical_hold_count"], 1)
            self.assertEqual(review["canonical_reduce_or_exit_count"], 2)
            rows_by_symbol = {row["symbol_id"]: row for row in review["symbols"]}
            self.assertEqual(rows_by_symbol["005930"]["expected_holding_quantity"], 0)
            self.assertEqual(rows_by_symbol["000660"]["expected_holding_quantity"], 3)
            # 035720 has no execution row: fallback = current(5) + pending_buy(1) - pending_sell(2) = 4.
            self.assertEqual(rows_by_symbol["035720"]["expected_holding_quantity"], 4)

    def test_empty_judge_scope_writes_review_contract_v4(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "empty-review-scope"
            run_dir.mkdir(parents=True, exist_ok=True)
            pipeline = Pipeline(
                argparse.Namespace(
                    command="summarize",
                    workspace_dir=str(workspace),
                    output_dir=str(run_dir),
                    run_id="empty-review-scope",
                    started_at="2026-06-18T09:00:00+09:00",
                )
            )
            write_json(run_dir / "judge-review-spec.json", {"symbol_ids": []})

            pipeline.run_judge_review()

            review = load_json(run_dir / "judge-review.json")
            self.assertEqual(review.get("schema_version"), "4")
            self.assertTrue(review.get("skipped"))
            self.assertEqual(review.get("skip_reason"), "no selected symbols")

    def test_failed_analyst_group_is_partial_and_does_not_abort_held_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "partial-analyst-group"
            run_dir.mkdir(parents=True, exist_ok=True)
            pipeline = Pipeline(
                argparse.Namespace(
                    command="summarize",
                    workspace_dir=str(workspace),
                    output_dir=str(run_dir),
                    run_id="partial-analyst-group",
                    started_at="2026-06-18T09:00:00+09:00",
                    max_workers=2,
                )
            )
            failed_group = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"status": "failed", "failed_count": 2}),
                stderr="",
            )
            with mock.patch.object(pipeline, "run_cmd", return_value=failed_group):
                pipeline.run_analyst_reviews()

            stage = pipeline.stages[-1]
            self.assertEqual(stage.get("stage"), "analyst-review")
            self.assertEqual(stage.get("status"), "partial")

    def test_run_judge_review_accepts_target_without_thesis_definition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            pipeline = self._make_pipeline(workspace, "partial-judge-artifact")
            run_dir = pipeline.output_dir
            write_json(run_dir / "judge-review-spec.json", {"symbol_ids": ["005930"]})
            wrapper = {
                "status": "success",
                "agent_role": "judge",
                "task_name": "second-judge",
                "errors": [],
                "parsed_json": {
                    "stage": "judge-review",
                    "symbols": [
                        {
                            "symbol_id": "005930",
                            "symbol_name": "삼성전자",
                            "target_position_value_krw": 140000,
                            "price": {"current_or_last": 70000},
                            "holding_quantity_context": {"expected_holding_quantity": 1},
                            "relative_attractiveness_rank": 1,
                            "decision_basis": "thesis",
                            "reason_code": "increase_target",
                            "one_line_reason": "increase without a required thesis definition",
                            "opposing_view": self._opposing_view_with_reduce_evidence(
                                "analyst-review:005930:analyst-quality-value"
                            ),
                        }
                    ],
                },
            }
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(wrapper),
                stderr="",
            )

            with mock.patch.object(pipeline, "run_cmd", return_value=completed):
                pipeline.run_judge_review()

            review = load_json(run_dir / "judge-review.json")
            self.assertEqual(review.get("status"), "success")
            self.assertEqual(review["symbols"][0]["target_position_value_krw"], 140000)
            self.assertNotIn("decision_guard", review["symbols"][0])
            self.assertEqual(pipeline.stages[-1].get("stage"), "judge-review")
            self.assertEqual(pipeline.stages[-1].get("status"), "success")

    def test_write_judge_review_drops_every_duplicate_symbol_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_dir = workspace / "reports" / "runs" / "duplicate-judge-symbol"
            run_dir.mkdir(parents=True, exist_ok=True)
            pipeline = Pipeline(
                argparse.Namespace(
                    command="summarize",
                    workspace_dir=str(workspace),
                    output_dir=str(run_dir),
                    run_id="duplicate-judge-symbol",
                    started_at="2026-06-18T09:00:00+09:00",
                )
            )
            write_json(run_dir / "judge-review-spec.json", {"symbol_ids": ["005930"]})
            duplicate = {
                "symbol_id": "005930",
                "symbol_name": "삼성전자",
                "target_position_value_krw": 0,
                "relative_attractiveness_rank": 1,
                "decision_basis": "none",
                "evidence_refs": [],
                "reason_code": "hold",
                "one_line_reason": "duplicate probe",
            }

            pipeline.write_judge_review(
                {
                    "agent_role": "judge",
                    "task_name": "duplicate-probe",
                    "parsed_json": {"symbols": [duplicate, dict(duplicate)]},
                    "errors": [],
                }
            )

            review = load_json(run_dir / "judge-review.json")
            self.assertEqual(review.get("status"), "partial")
            self.assertEqual(review.get("symbols"), [])
            self.assertTrue(
                any(
                    error.get("code") == "duplicate_judge_symbol"
                    for error in review.get("errors", [])
                )
            )

    def _make_pipeline(
        self,
        workspace: Path,
        run_id: str,
        started_at: str = "2026-06-02T09:00:00+09:00",
    ) -> Pipeline:
        run_dir = workspace / "reports" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        pipeline = Pipeline(
            argparse.Namespace(
                command="summarize",
                workspace_dir=str(workspace),
                output_dir=str(run_dir),
                run_id=run_id,
                started_at=started_at,
            )
        )
        return pipeline

    def _loss_context(self) -> dict:
        return {
            "price": {"current_or_last": 70000},
            "financial_summary": {"status": "success", "operating_margin_trend": "compressed"},
            "holding_quantity_context": {"expected_holding_quantity": 10},
            "symbol_strategy_context": {"loss_position": True},
        }

    def _opposing_view_with_reduce_evidence(self, evidence_ref: str) -> dict:
        return {
            "increase_case": {"summary": "increase case self-test", "evidence_refs": []},
            "reduce_case": {"summary": "reduce case self-test", "evidence_refs": [evidence_ref]},
        }

    def test_load_prior_thesis_ignores_future_equal_and_invalid_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runs_dir = workspace / "reports" / "runs"

            def write_run(run_id: str, started_at: str, status: str, thesis: dict) -> None:
                run_dir = runs_dir / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                write_json(
                    run_dir / "judge-review.json",
                    {
                        "run_id": run_id,
                        "started_at": started_at,
                        "status": status,
                        "symbols": [{"symbol_id": "005930", "thesis_definition": thesis}],
                    },
                )

            valid_condition = {
                "core_rationale": "quality moat",
                "invalidation_conditions": [{"condition_id": "cond-a", "description": "description a"}],
            }
            # Future run relative to "current-run" started_at -> ignored.
            write_run("future-run", "2026-06-05T09:00:00+09:00", "success", dict(valid_condition, core_rationale="future"))
            # Exactly equal started_at -> ignored (must be strictly earlier).
            write_run("equal-time-run", "2026-06-02T09:00:00+09:00", "success", dict(valid_condition, core_rationale="equal-time"))
            # Partial/failed status -> ignored even though earlier and otherwise valid.
            write_run("partial-run", "2026-06-01T12:00:00+09:00", "partial", dict(valid_condition, core_rationale="partial"))
            write_run("failed-run", "2026-06-01T11:00:00+09:00", "failed", dict(valid_condition, core_rationale="failed"))
            # Malformed artifact (not even a dict at top level after load) -> ignored without crashing.
            malformed_dir = runs_dir / "malformed-run"
            malformed_dir.mkdir(parents=True, exist_ok=True)
            (malformed_dir / "judge-review.json").write_text("not json", encoding="utf-8")
            # Earlier and valid, but not the most recent eligible one.
            write_run("older-valid-run", "2026-06-01T08:00:00+09:00", "success", dict(valid_condition, core_rationale="older-valid"))
            # The latest earlier successful run with a valid definition.
            write_run("latest-valid-run", "2026-06-01T20:00:00+09:00", "success", dict(valid_condition, core_rationale="latest-valid"))

            pipeline = self._make_pipeline(workspace, "current-run", started_at="2026-06-02T09:00:00+09:00")
            prior = pipeline.load_prior_thesis("005930")
            self.assertIsNotNone(prior)
            self.assertEqual(prior.get("core_rationale"), "latest-valid")

    def test_judge_target_is_not_clamped_by_strategy_labels_or_thesis_assessment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            pipeline = self._make_pipeline(workspace, "direct-target-run")
            context = {
                "price": {"current_or_last": 70000},
                "holding_quantity_context": {"expected_holding_quantity": 10},
            }
            cases = [
                (
                    {
                        "symbol_id": "005930",
                        "target_position_value_krw": 210000,
                        "decision_basis": "none",
                        "reason_code": "reduce_target",
                        "one_line_reason": "direct reduction",
                        "thesis_assessment": {
                            "status": "intact",
                            "matched_invalidation_condition_ids": [],
                        },
                    },
                    210000,
                    "reduce",
                ),
                (
                    {
                        "symbol_id": "005930",
                        "target_position_value_krw": 840000,
                        "decision_basis": "profit_protection",
                        "reason_code": "increase_target",
                        "one_line_reason": "direct increase",
                    },
                    840000,
                    "increase",
                ),
            ]
            for item, expected_target, expected_action in cases:
                normalized, errors = pipeline.derive_judge_final_quantity(item, context)
                self.assertEqual(errors, [])
                self.assertEqual(normalized["target_position_value_krw"], expected_target)
                self.assertEqual(normalized["canonical_action"], expected_action)
                self.assertNotIn("decision_guard", normalized)
                self.assertNotIn("protected_loss_gate", normalized)

    def test_increase_accepts_missing_or_malformed_thesis_definition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            pipeline = self._make_pipeline(workspace, "optional-thesis-run")
            context = {
                "price": {"current_or_last": 70000},
                "holding_quantity_context": {"expected_holding_quantity": 1},
            }
            for thesis_definition in (
                None,
                {},
                {"core_rationale": 123, "invalidation_conditions": []},
            ):
                item = {
                    "symbol_id": "005930",
                    "target_position_value_krw": 210000,
                    "reason_code": "increase_target",
                    "one_line_reason": "increase without a required thesis gate",
                }
                if thesis_definition is not None:
                    item["thesis_definition"] = thesis_definition
                normalized, errors = pipeline.derive_judge_final_quantity(item, context)
                self.assertEqual(errors, [])
                self.assertEqual(normalized["target_position_value_krw"], 210000)
                self.assertEqual(normalized["canonical_action"], "increase")
                self.assertNotIn("thesis_definition", normalized)

    def test_valid_thesis_definition_is_sanitized_as_audit_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            pipeline = self._make_pipeline(workspace, "valid-thesis-audit-run")
            context = {
                "price": {"current_or_last": 70000},
                "holding_quantity_context": {"expected_holding_quantity": 1},
            }
            item = {
                "symbol_id": "005930",
                "target_position_value_krw": 210000,
                "decision_basis": "thesis",
                "reason_code": "increase_target",
                "one_line_reason": "increase with thesis context",
                "thesis_definition": {
                    "core_rationale": "  quality moat and pricing power  ",
                    "invalidation_conditions": [
                        {"condition_id": "Margin Compression!", "description": "  gross margin drops below prior guidance  "},
                        {"condition_id": "Margin Compression!", "description": "duplicate condition_id is deduped"},
                    ],
                },
            }
            normalized, errors = pipeline.derive_judge_final_quantity(item, context)
            self.assertEqual(errors, [])
            self.assertEqual(normalized["target_position_value_krw"], 210000)
            self.assertEqual(
                normalized["thesis_definition"],
                {
                    "defined_at_run_id": "valid-thesis-audit-run",
                    "core_rationale": "quality moat and pricing power",
                    "invalidation_conditions": [
                        {"condition_id": "margin-compression", "description": "gross margin drops below prior guidance"}
                    ],
                },
            )
            self.assertEqual(normalized["prior_thesis_context"], {"available": False})

    def test_load_prior_thesis_treats_naive_started_at_as_kst(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            prior_dir = workspace / "reports" / "runs" / "naive-prior-run"
            prior_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                prior_dir / "judge-review.json",
                {
                    "run_id": "naive-prior-run",
                    # Naive timestamp (no offset): must be treated as KST, matching
                    # run_subagent.prior_thesis_context so both selection paths agree.
                    "started_at": "2026-06-02T01:00:00",
                    "status": "success",
                    "symbols": [
                        {
                            "symbol_id": "005930",
                            "thesis_definition": {
                                "core_rationale": "quality moat",
                                "invalidation_conditions": [{"condition_id": "cond-a", "description": "description a"}],
                            },
                        }
                    ],
                },
            )
            pipeline = self._make_pipeline(workspace, "current-run-naive", started_at="2026-06-02T09:00:00+09:00")
            prior = pipeline.load_prior_thesis("005930")
            self.assertIsNotNone(prior)
            self.assertEqual(prior.get("core_rationale"), "quality moat")

    def test_normalize_thesis_condition_id_preserves_korean_and_rejects_unusable_input(self) -> None:
        self.assertEqual(normalize_thesis_condition_id("한글조건"), "한글조건")
        self.assertEqual(normalize_thesis_condition_id("  한글 조건!! 이름  "), "한글-조건-이름")
        self.assertEqual(normalize_thesis_condition_id("Margin  Compression!!"), "margin-compression")
        self.assertEqual(normalize_thesis_condition_id("--leading-and-trailing--"), "leading-and-trailing")
        self.assertEqual(normalize_thesis_condition_id("already.valid_id-1"), "already.valid_id-1")
        # Non-string input never coerces to a placeholder; it is simply unusable.
        self.assertEqual(normalize_thesis_condition_id(1), "")
        self.assertEqual(normalize_thesis_condition_id(True), "")
        self.assertEqual(normalize_thesis_condition_id(None), "")
        self.assertEqual(normalize_thesis_condition_id({"condition_id": "x"}), "")
        self.assertEqual(normalize_thesis_condition_id(["x"]), "")
        # Whitespace/punctuation-only input normalizes to empty, not "unknown".
        self.assertEqual(normalize_thesis_condition_id("   "), "")
        self.assertEqual(normalize_thesis_condition_id("!!!"), "")
        self.assertEqual(normalize_thesis_condition_id("...___---"), "")
        self.assertEqual(normalize_thesis_condition_id("._condition_."), "condition")
        # Length cap is applied identically to run_subagent's normalizer.
        long_id = "a" * 200
        normalized = normalize_thesis_condition_id(long_id)
        self.assertEqual(len(normalized), 64)
        self.assertEqual(normalized, "a" * 64)

    def test_thesis_definition_is_valid_requires_actual_string_fields(self) -> None:
        # The direct reproduction from the finding: numeric/bool/object/list JSON
        # values coerced with str() must not make a valid definition or valid prior.
        numeric_fields = {
            "core_rationale": 1,
            "invalidation_conditions": [{"condition_id": 1, "description": 1}],
        }
        self.assertFalse(thesis_definition_is_valid(numeric_fields))
        bool_fields = {
            "core_rationale": True,
            "invalidation_conditions": [{"condition_id": True, "description": True}],
        }
        self.assertFalse(thesis_definition_is_valid(bool_fields))
        object_fields = {
            "core_rationale": {"text": "quality moat"},
            "invalidation_conditions": [{"condition_id": {"a": 1}, "description": {"b": 2}}],
        }
        self.assertFalse(thesis_definition_is_valid(object_fields))
        list_fields = {
            "core_rationale": ["quality", "moat"],
            "invalidation_conditions": [{"condition_id": ["a"], "description": ["b"]}],
        }
        self.assertFalse(thesis_definition_is_valid(list_fields))
        # A genuinely valid string-typed definition still passes.
        valid_fields = {
            "core_rationale": "quality moat",
            "invalidation_conditions": [{"condition_id": "cond-a", "description": "description a"}],
        }
        self.assertTrue(thesis_definition_is_valid(valid_fields))

    def test_load_prior_thesis_rejects_prior_with_non_string_thesis_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            prior_dir = workspace / "reports" / "runs" / "numeric-fields-prior-run"
            prior_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                prior_dir / "judge-review.json",
                {
                    "run_id": "numeric-fields-prior-run",
                    "started_at": "2026-06-01T09:00:00+09:00",
                    "status": "success",
                    "symbols": [
                        {
                            "symbol_id": "005930",
                            "thesis_definition": {
                                "core_rationale": 1,
                                "invalidation_conditions": [{"condition_id": 1, "description": 1}],
                            },
                        }
                    ],
                },
            )
            pipeline = self._make_pipeline(workspace, "numeric-fields-current-run")
            self.assertIsNone(pipeline.load_prior_thesis("005930"))


    def test_derive_judge_final_quantity_preserves_korean_condition_id_and_dedupes_after_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            pipeline = self._make_pipeline(workspace, "korean-condition-id-run")
            context = {
                "price": {"current_or_last": 70000},
                "holding_quantity_context": {"expected_holding_quantity": 1},
                "today_trade_timeline_context": {"collection_status": "complete", "has_same_day_buy": False},
            }
            item = {
                "symbol_id": "005930",
                "target_position_value_krw": 210000,
                "decision_basis": "thesis",
                "reason_code": "increase_target",
                "one_line_reason": "increase with Korean condition_id",
                "thesis_definition": {
                    "core_rationale": "품질과 가격 결정력",
                    "invalidation_conditions": [
                        {"condition_id": "한글조건", "description": "설명 1"},
                        {"condition_id": "  한글조건  ", "description": "duplicate after normalization is dropped"},
                        {"condition_id": "HAN-GEUL-CONDITION", "description": "distinct ascii id kept"},
                    ],
                },
            }
            normalized, errors = pipeline.derive_judge_final_quantity(item, context)
            self.assertEqual(errors, [])
            self.assertIsNotNone(normalized)
            self.assertEqual(
                normalized["thesis_definition"]["invalidation_conditions"],
                [
                    {"condition_id": "한글조건", "description": "설명 1"},
                    {"condition_id": "han-geul-condition", "description": "distinct ascii id kept"},
                ],
            )

    def test_load_prior_thesis_breaks_ties_by_source_run_id_when_started_at_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runs_dir = workspace / "reports" / "runs"
            same_started_at = "2026-06-01T09:00:00+09:00"

            def write_run(run_id: str, core_rationale: str) -> None:
                run_dir = runs_dir / run_id
                run_dir.mkdir(parents=True, exist_ok=True)
                write_json(
                    run_dir / "judge-review.json",
                    {
                        "run_id": run_id,
                        "started_at": same_started_at,
                        "status": "success",
                        "symbols": [
                            {
                                "symbol_id": "005930",
                                "thesis_definition": {
                                    "core_rationale": core_rationale,
                                    "invalidation_conditions": [{"condition_id": "cond-a", "description": "description a"}],
                                },
                            }
                        ],
                    },
                )

            # Two eligible earlier runs share the exact same started_at; only the
            # source run_id can break the tie deterministically.
            write_run("tie-run-aaa", "from aaa")
            write_run("tie-run-zzz", "from zzz")

            pipeline = self._make_pipeline(workspace, "tie-current-run", started_at="2026-06-02T09:00:00+09:00")
            prior = pipeline.load_prior_thesis("005930")
            self.assertIsNotNone(prior)
            # run_subagent.prior_thesis_context uses the identical (started_at, run_id)
            # tie-break, so both paths deterministically pick the lexicographically
            # greatest run_id ("tie-run-zzz") regardless of filesystem iteration order.
            self.assertEqual(prior.get("core_rationale"), "from zzz")

    # --- Regression coverage for the Codex-verification findings below ---

    def _thesis_prior(self, workspace: Path, symbol_id: str, run_id: str = "prior-run") -> None:
        prior_dir = workspace / "reports" / "runs" / run_id
        prior_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            prior_dir / "judge-review.json",
            {
                "run_id": run_id,
                "started_at": "2026-06-01T09:00:00+09:00",
                "status": "success",
                "symbols": [
                    {
                        "symbol_id": symbol_id,
                        "thesis_definition": {
                            "core_rationale": "quality moat",
                            "invalidation_conditions": [{"condition_id": "margin-compression", "description": "gross margin drops"}],
                        },
                    }
                ],
            },
        )


    def test_sub_share_target_change_is_canonical_no_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            pipeline = self._make_pipeline(workspace, "sub-share-no-change-run")
            normalized, errors = pipeline.derive_judge_final_quantity(
                {
                    "symbol_id": "005930",
                    "target_position_value_krw": 700_001,
                    "decision_basis": "thesis",
                    "reason_code": "tiny_increase",
                    "one_line_reason": "sub-share target",
                    "thesis_definition": {
                        "core_rationale": "replacement thesis must not persist",
                        "invalidation_conditions": [
                            {
                                "condition_id": "replacement-condition",
                                "description": "sub-share increase did not execute",
                            }
                        ],
                    },
                },
                {
                    "price": {"current_or_last": 70_000},
                    "holding_quantity_context": {"expected_holding_quantity": 10},
                    "symbol_strategy_context": {"loss_position": True},
                    "today_trade_timeline_context": {
                        "collection_status": "complete",
                        "has_same_day_buy": False,
                    },
                },
            )
            self.assertEqual(errors, [])
            self.assertIsNotNone(normalized)
            self.assertEqual(normalized["target_position_value_krw"], 700_000)
            self.assertEqual(normalized["decision_basis"], "thesis")
            self.assertEqual(normalized["canonical_action"], "hold")
            self.assertNotIn("decision_guard", normalized)
            self.assertIn("thesis_definition", normalized)
