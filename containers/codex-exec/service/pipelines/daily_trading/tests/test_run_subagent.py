#!/usr/bin/env python3
"""Tests for the daily-trading sub-agent launcher."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from typing import Any

from ..scripts.run_subagent import (
    SUBAGENT_MODEL_CONFIG_ENV,
    build_prompt,
    compact_prompt,
    compact_review_payload_errors,
    launcher_model_effort,
    load_json,
    load_subagent_model_config,
    normalize_compact_review_payload,
    run_group,
    run_one,
    validate_spec,
    write_json,
    write_review_input_slices,
)


def assert_unsupported_stage_rejected() -> None:
    try:
        launcher_model_effort("unsupported-stage", "unsupported-role")
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported daily-trading sub-agent stage/role was accepted")
    try:
        launcher_model_effort("unsupported-stage", "judge")
    except ValueError:
        return
    raise AssertionError("unsupported daily-trading sub-agent stage/role was accepted")


def assert_model_effort(stage: str, agent_role: str, *, model: str, effort: str) -> None:
    actual_model, actual_effort = launcher_model_effort(stage, agent_role)
    if (actual_model, actual_effort) != (model, effort):
        raise AssertionError(
            f"expected {model}/{effort} for {stage}/{agent_role}, got {actual_model}/{actual_effort}"
        )


def assert_all_supported_stages_use_expected_models() -> None:
    model_config = load_subagent_model_config()
    cases = [
        (
            "financial-collection",
            "financial",
            model_config["collection"]["model"],
            model_config["collection"]["model_reasoning_effort"],
        ),
        (
            "news-collection",
            "news",
            model_config["collection"]["model"],
            model_config["collection"]["model_reasoning_effort"],
        ),
        (
            "analyst-review",
            "analyst-momentum-news",
            model_config["analyst_review"]["model"],
            model_config["analyst_review"]["model_reasoning_effort"],
        ),
        (
            "judge-review",
            "judge",
            model_config["judge_review"]["model"],
            model_config["judge_review"]["model_reasoning_effort"],
        ),
    ]
    for stage, role, model, effort in cases:
        assert_model_effort(stage, role, model=model, effort=effort)


def fake_codex_script(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

argv_path = Path(os.environ["FAKE_CODEX_ARGV_LOG"])
with argv_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")

output_path = None
for index, arg in enumerate(sys.argv):
    if arg == "-o" and index + 1 < len(sys.argv):
        output_path = Path(sys.argv[index + 1])
        break
if output_path is None:
    print("missing -o", file=sys.stderr)
    sys.exit(2)
output_path.parent.mkdir(parents=True, exist_ok=True)
task_name = output_path.name.removesuffix(".raw.txt")
empty_tasks = {item.strip() for item in os.environ.get("FAKE_CODEX_EMPTY_TASKS", "").split(",") if item.strip()}
if task_name in empty_tasks:
    output_path.write_text("", encoding="utf-8")
    sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
if os.environ.get("FAKE_CODEX_INVALID_JSON") == "1":
    output_path.write_text("not json", encoding="utf-8")
else:
    if "financial" in task_name or "news" in task_name:
        domain = "financial" if "financial" in task_name else "news"
        payload = {
            "schema_version": "1",
            "run_id": "self-test",
            "started_at": "2026-06-08T09:00:00+09:00",
            "generated_at": "2026-06-08T09:00:01+09:00",
            "stage": f"{domain}-collection",
            "domain": domain,
            "status": "success",
            "skipped": False,
            "skip_reason": "",
            "attempts": [],
            "errors": [],
            "symbols": [{"symbol_id": "005930", "symbol_name": "삼성전자", "errors": []}],
        }
    else:
        prompt = sys.argv[-1] if sys.argv else ""
        if "stage: analyst-review" in prompt or "stage: judge-review" in prompt:
            stage = "judge-review" if "stage: judge-review" in prompt else "analyst-review"
            if "agent_role: analyst-quality-risk" in prompt:
                first_payload = {
                    "views": {
                        "analyst-quality-value": {
                            "score": 5,
                            "confidence": 5,
                            "reason_code": "hold_neutral",
                            "one_line_reason": "quality self-test",
                            "missing_data": [],
                        },
                        "analyst-risk-allocation": {
                            "score": 5,
                            "confidence": 5,
                            "reason_code": "hold_neutral",
                            "one_line_reason": "risk self-test",
                            "missing_data": [],
                        },
                    }
                }
            elif "agent_role: analyst-momentum-news" in prompt:
                first_payload = {
                    "views": {
                        "analyst-momentum-cycle": {
                            "score": 5,
                            "confidence": 5,
                            "reason_code": "hold_neutral",
                            "one_line_reason": "momentum self-test",
                            "missing_data": [],
                        },
                        "analyst-news-flow": {
                            "score": 5,
                            "confidence": 5,
                            "reason_code": "no_news_excluded",
                            "one_line_reason": "뉴스 정보가 없어 평균에서 제외",
                            "missing_data": ["news_summary"],
                        },
                    }
                }
            else:
                first_payload = {"score": 5, "confidence": 5, "missing_data": []}
            payload = {
                "agent_id": "fake",
                "persona": "fake",
                "stage": stage,
                "human_markdown_path": "",
                "symbols": [
                    {
                        "symbol_id": "005930",
                        "symbol_name": "삼성전자",
                        "reason_code": "hold_neutral",
                        "one_line_reason": "self-test",
                        **(
                            {"target_position_value_krw": 70000, "relative_attractiveness_rank": 1}
                            if stage == "judge-review"
                            else first_payload
                        ),
                    }
                ],
                "errors": [],
            }
        else:
            payload = {"ok": True, "argv": sys.argv[1:]}
    output_path.write_text(json.dumps(payload), encoding="utf-8")
if "--json" in sys.argv:
    if os.environ.get("FAKE_CODEX_DIAGNOSTIC_EVENTS") == "1":
        print(json.dumps({"type": "event_msg", "payload": {"type": "tool_call", "tool_name": "shell", "command": ["cat", "artifact.json"]}}))
        print(json.dumps({"type": "event_msg", "payload": {"type": "tool_result", "tool_name": "shell", "content": "x" * 2048}}))
        print(json.dumps({"type": "event_msg", "payload": {"type": "tool_call", "tool_name": "shell", "command": ["cat", "artifact.json"]}}))
        print(json.dumps({"type": "event_msg", "payload": {"type": "tool_result", "tool_name": "shell", "content": "y" * 1024}}))
        print(json.dumps({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 7, "cached_input_tokens": 3, "output_tokens": 2, "reasoning_output_tokens": 1, "total_tokens": 9}}}))
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 11, "cached_input_tokens": 5, "output_tokens": 4, "reasoning_output_tokens": 2, "total_tokens": 15}}))
        print("diagnostic stderr", file=sys.stderr)
    else:
        print(json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 120
                    }
                }
            },
            "rate_limits": {
                "primary": {"used_percent": 1.0},
                "secondary": {"used_percent": 2.0}
            }
        }))
sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def spec(tmp: Path, *, stage: str, agent_role: str, task_name: str) -> dict[str, Any]:
    return {
        "run_id": "self-test",
        "started_at": "2026-06-08T09:00:00+09:00",
        "stage": stage,
        "agent_role": agent_role,
        "task_name": task_name,
        "prompt": '{"return":"json only"}',
        "workspace_dir": str(tmp),
        "output_dir": str(tmp / "reports" / "runs" / "self-test"),
    }


def compact_spec(tmp: Path, *, stage: str, agent_role: str, task_name: str) -> dict[str, Any]:
    payload = spec(tmp, stage=stage, agent_role=agent_role, task_name=task_name)
    payload.pop("prompt")
    payload["artifact_paths"] = {
        "decision_brief": str(tmp / "reports" / "runs" / "self-test" / "decision-brief.json"),
        "analyst_review": str(tmp / "reports" / "runs" / "self-test" / "analyst-review.json"),
        "persona": f"prompts/{agent_role}.md",
        "review_format": "prompts/analyst-review-format.md",
    }
    payload["symbol_ids"] = ["005930", {"symbol_id": "000660"}, "005930"]
    return payload


def write_sample_review_inputs(tmp: Path) -> None:
    run_dir = tmp / "reports" / "runs" / "self-test"
    write_json(
        run_dir / "execution.json",
        {
            "schema_version": "1",
            "run_id": "self-test",
            "started_at": "2026-06-08T10:00:00+09:00",
            "orders": [
                {
                    "symbol_id": "005930",
                    "direction": "buy",
                    "result": "submitted",
                    "validated_order_quantity": 99,
                }
            ],
        },
    )
    newer_other_run_dir = tmp / "reports" / "runs" / "self-test-newer-other"
    write_json(
        newer_other_run_dir / "execution.json",
        {
            "schema_version": "1",
            "run_id": "self-test-newer-other",
            "started_at": "2026-06-08T09:05:00+09:00",
            "orders": [
                {
                    "symbol_id": "000660",
                    "direction": "buy",
                    "result": "submitted",
                    "validated_order_quantity": 1,
                }
            ],
        },
    )
    previous_run_dir = tmp / "reports" / "runs" / "self-test-prev"
    write_json(
        previous_run_dir / "execution.json",
        {
            "schema_version": "1",
            "run_id": "self-test-prev",
            "started_at": "2026-06-08T09:00:00+09:00",
            "orders": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "direction": "sell",
                    "result": "submitted",
                    "validated_order_quantity": 1,
                    "current_live_holding_quantity": 12,
                    "expected_holding_quantity": 12,
                    "final_holding_quantity": 11,
                    "additional_required_quantity": -1,
                    "order_path": "immediate",
                    "order_api": "order_cash",
                    "reason": "self-test recent sell",
                }
            ],
        },
    )
    older_run_dir = tmp / "reports" / "runs" / "self-test-older"
    write_json(
        older_run_dir / "execution.json",
        {
            "schema_version": "1",
            "run_id": "self-test-older",
            "started_at": "2026-06-08T08:00:00+09:00",
            "orders": [
                {
                    "symbol_id": "005930",
                    "direction": "buy",
                    "result": "submitted",
                    "validated_order_quantity": 1,
                }
            ],
        },
    )
    write_json(
        run_dir / "decision-brief.json",
        {
            "schema_version": "1",
            "brief_type": "decision-brief",
            "market_index_snapshot": {
                "status": "success",
                "indexes": [
                    {
                        "symbol": "NASDAQ",
                        "name": "Nasdaq",
                        "source": "google_finance",
                        "status": "success",
                        "value": 18000.0,
                        "change_percent": 0.3,
                        "observed_at": "2026-06-08T00:00:00+00:00",
                        "market_status": "latest_available",
                    }
                ],
            },
            "strategy_context": {
                "schema_version": "1",
                "regime": "weak_downside",
                "new_exposure_review_bias": "discourage",
                "downside_add_review_bias": "strong_review_required",
                "index_drop_sell_review_bias": "discourage",
                "advisory_semantics": "advisory only",
            },
            "errors": [
                {"symbol_id": "005930", "code": "keep_symbol_error"},
                {"symbol_id": "035420", "code": "drop_symbol_error"},
                {"code": "keep_run_error"},
            ],
            "symbols": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "price": {
                        "current_or_last": 70000,
                        "observed_at": "2026-06-08T09:00:00+09:00",
                        "snapshot_mode": "live",
                        "open": 69000,
                        "high": 71000,
                    },
                    "price_chart_signals": [
                        {"signal": "s1", "strength": 1, "timeframe": "D"},
                        {"signal": "s2", "strength": 2, "timeframe": "W"},
                        {"signal": "s3", "strength": 3, "timeframe": "M"},
                        {"signal": "s4", "strength": 4, "timeframe": "Y"},
                    ],
                    "chart_context": {"daily": [{"close": 70000}], "weekly": [{"close": 69000}]},
                    "financial_summary": [{"metric": "roe", "value": "10"}],
                    "account_exposure": {
                        "current_live_holding_quantity": 10,
                        "pending_and_reserved_buy_quantity": 2,
                        "pending_and_reserved_sell_quantity": 1,
                    },
                    "symbol_strategy_context": {
                        "current_holding": True,
                        "downside_add_review_target": True,
                        "advisory_semantics": "advisory only",
                    },
                    "today_trade_price_context": {
                        "has_same_day_trade": True,
                        "last_direction": "buy",
                        "last_fill_price": 70100,
                        "current_or_last_price": 70000,
                        "move_since_last_fill_pct": -0.14,
                    },
                    "today_trade_timeline_context": {
                        "has_same_day_trade": True,
                        "last_direction": "buy",
                        "net_quantity": 3,
                        "fills": [
                            {"filled_at": "2026-06-08T09:31:00+09:00", "direction": "buy", "quantity": 3, "price": 70100}
                        ],
                    },
                    "orderbook_summary": {"bid_depth": 100},
                    "trade_flow_summary": {"tick_count": 3},
                    "investor_flow_summary": {"foreign_net_buy_quantity": 1000},
                    "news_summary": [
                        {"content": "n1", "url": "u1"},
                        {"content": "n2", "url": "u2"},
                        {"content": "n3", "url": "u3"},
                        {"content": "n4", "url": "u4"},
                    ],
                    "warnings": ["w1", "w2", "w3", "w4"],
                    "custom_detail": {"keep": True},
                },
                {"symbol_id": "000660", "symbol_name": "SK하이닉스"},
                {"symbol_id": "035420", "symbol_name": "NAVER", "custom_detail": {"drop": True}},
            ],
        },
    )
    write_json(
        run_dir / "analyst-review.json",
        {
            "schema_version": "1",
            "stage": "analyst-review",
            "symbols": [
                {
                    "symbol_id": "005930",
                    "score": 7,
                    "agent_scores": [
                        {
                            "agent_role": "analyst-risk-allocation",
                            "score": 7,
                            "confidence": 6,
                            "one_line_reason": "full reason should remain",
                        }
                    ],
                    "custom_review_detail": {"keep": True},
                },
                {"symbol_id": "000660", "score": 8},
                {"symbol_id": "035420", "score": 5},
            ],
        },
    )


def assert_prompt_compaction() -> None:
    raw = "  keep leading instruction  \n\n\nnext line   \r\n\r\nfinal"
    expected = "  keep leading instruction\n\nnext line\n\nfinal"
    actual = compact_prompt(raw)
    if actual != expected:
        raise AssertionError(f"unexpected compact prompt: {actual!r}")


def assert_compact_review_prompt(tmp: Path) -> None:
    prompt = build_prompt(compact_spec(tmp, stage="analyst-review", agent_role="analyst-quality-risk", task_name="first"))
    required_parts = [
        "stage: analyst-review",
        "agent_role: analyst-quality-risk",
        "You may use read-only local shell commands such as cat and jq only for the explicitly listed files.",
        "Do not call KIS, MCP, web, network, account/order APIs, or external data sources.",
        "Do not write files, create Markdown, emit diffs, or wrap output in code fences.",
        "Read only the listed symbol_ids from artifact files; do not load unrelated symbols, raw cache files, secrets, or unlisted paths.",
        "decision_brief:",
        "persona: prompts/analyst-quality-risk.md",
        "review_format: prompts/analyst-review-format.md",
        "symbol_ids: 005930,000660",
        "Return each symbol with a views object keyed by analyst-quality-value, analyst-risk-allocation",
        "Use today_trade_price_context to avoid same-day churn:",
        "Return JSON only",
    ]
    missing = [part for part in required_parts if part not in prompt]
    if missing:
        raise AssertionError(f"compact review prompt missing {missing}: {prompt}")

    second_prompt = build_prompt(
        compact_spec(tmp, stage="judge-review", agent_role="judge", task_name="second")
    )
    second_required_parts = [
        "stage: judge-review",
        "analyst_review:",
        "For judge-review, use the lossless selected-symbol analyst-review slice from analyst_review.",
        "The supplied symbols are pre-selected candidates by score band: sell candidates are held symbols with final_first_score <= 4, buy candidates have final_first_score >= 6.",
        "Direction preconditions are hard constraints: a sell candidate may only be sold (partial or full) or held (target_position_value_krw <= baseline); a buy candidate may only be bought or held (target_position_value_krw >= baseline).",
        "When evidence is insufficient or conflicting, the default decision is hold at the baseline.",
        "final_first_score is the simple mean of the included analyst view scores",
        "For held sell candidates, an intact long-term thesis favors holding despite the low score",
        "Use strategy_context and symbol_strategy_context as advisory inputs for target_position_value_krw, not as order allow/block rules.",
        "Debate sub-agents: spawn the bull/bear debate sub-agents required by the judge persona",
        "Debate procedure: one base round (bull case, bear case, bull rebuttal, bear rebuttal) over all candidates in batch",
        "hard stop after two rounds",
        "if they balance or evidence is insufficient, hold at the baseline",
        "target_position_value_krw",
        "No additional buy",
    ]
    missing = [part for part in second_required_parts if part not in second_prompt]
    if missing:
        raise AssertionError(f"compact judge-review prompt missing {missing}: {second_prompt}")
    banded_spec = compact_spec(tmp, stage="judge-review", agent_role="judge", task_name="second-banded")
    banded_spec["candidate_directions"] = {"005930": "sell", "000660": "buy"}
    banded_spec["score_band_thresholds"] = {"sell_below": 4.0, "buy_above": 6.0}
    banded_spec["portfolio_snapshot"] = [
        {"symbol_id": "005930", "symbol_name": "삼성전자", "final_first_score": 3.5, "current_live_holding_quantity": 5, "valuation_amount": 350000, "pnl_rate": -3.2, "candidate_direction": "sell"}
    ]
    banded_prompt = build_prompt(banded_spec)
    if "candidate_directions: 000660=buy,005930=sell" not in banded_prompt:
        raise AssertionError(f"candidate directions missing from judge prompt: {banded_prompt}")
    if "portfolio_snapshot: " not in banded_prompt or '"final_first_score":3.5' not in banded_prompt.replace(" ", ""):
        raise AssertionError(f"portfolio snapshot missing from judge prompt: {banded_prompt}")


def assert_review_input_slices(tmp: Path) -> None:
    write_sample_review_inputs(tmp)
    first_payload = compact_spec(tmp, stage="analyst-review", agent_role="analyst-momentum-news", task_name="slice-first")
    first_slices = write_review_input_slices(first_payload)
    first_core = load_json(Path(first_slices["decision_brief"]))
    first_symbol = first_core["symbols"][0]
    if first_core.get("slice_output_roles") != ["analyst-momentum-cycle", "analyst-news-flow"]:
        raise AssertionError(f"analyst-review slice did not record output roles: {first_core}")
    if first_symbol.get("chart_context", {}).get("daily", [{}])[0].get("close") != 70000:
        raise AssertionError(f"momentum-news slice dropped chart_context: {first_symbol}")
    if len(first_symbol.get("news_summary", [])) != 4:
        raise AssertionError(f"momentum-news slice dropped news_summary: {first_symbol}")
    if first_core.get("market_index_snapshot", {}).get("indexes", [{}])[0].get("symbol") != "NASDAQ":
        raise AssertionError(f"momentum-news slice dropped market_index_snapshot: {first_core}")
    if first_symbol.get("today_trade_price_context", {}).get("last_fill_price") != 70100:
        raise AssertionError(f"momentum-news slice dropped same-day trade price context: {first_symbol}")
    if "today_trade_timeline_context" in first_symbol:
        raise AssertionError(f"analyst-review slice kept full same-day timeline: {first_symbol}")
    if "financial_summary" in first_symbol or "account_exposure" in first_symbol or "custom_detail" in first_symbol:
        raise AssertionError(f"momentum-news slice kept unrelated fields: {first_symbol}")
    quality_payload = compact_spec(tmp, stage="analyst-review", agent_role="analyst-quality-risk", task_name="slice-quality")
    quality_slices = write_review_input_slices(quality_payload)
    quality_core = load_json(Path(quality_slices["decision_brief"]))
    if quality_core.get("market_index_snapshot", {}).get("indexes", [{}])[0].get("symbol") != "NASDAQ":
        raise AssertionError(f"quality-risk slice dropped market_index_snapshot: {quality_core}")
    if "strategy_context" in quality_core:
        raise AssertionError(f"analyst-review slice kept strategy_context: {quality_core}")
    if "symbol_strategy_context" in (quality_core.get("symbols") or [{}])[0]:
        raise AssertionError(f"analyst-review slice kept symbol_strategy_context: {quality_core}")

    payload = compact_spec(tmp, stage="judge-review", agent_role="judge", task_name="slice-test")
    slices = write_review_input_slices(payload)
    expected_keys = {"decision_brief", "review_core", "analyst_review"}
    if set(slices) != expected_keys:
        raise AssertionError(f"unexpected slice keys: {slices}")
    for key, slice_path_text in slices.items():
        slice_text = Path(slice_path_text).read_text(encoding="utf-8")
        if "\n  " in slice_text:
            raise AssertionError(f"review input slice should be stored as compact JSON: {slice_path_text}")
        slice_payload = load_json(Path(slice_path_text))
        symbols = [item.get("symbol_id") for item in slice_payload.get("symbols", [])]
        if symbols != ["005930", "000660"]:
            raise AssertionError(f"unexpected sliced symbols for {slice_path_text}: {symbols}")
        if key == "review_core" and slice_payload.get("slice_type") != "review-core":
            raise AssertionError(f"review-core slice missing slice_type: {slice_payload}")
        if key == "review_core":
            if slice_payload.get("market_index_snapshot", {}).get("indexes", [{}])[0].get("symbol") != "NASDAQ":
                raise AssertionError(f"judge-review slice dropped market_index_snapshot: {slice_payload}")
            if slice_payload.get("strategy_context", {}).get("regime") != "weak_downside":
                raise AssertionError(f"judge-review slice dropped strategy_context: {slice_payload}")
            error_codes = [item.get("code") for item in slice_payload.get("errors", []) if isinstance(item, dict)]
            if error_codes != ["keep_symbol_error", "keep_run_error"]:
                raise AssertionError(f"review-core did not filter symbol-scoped errors: {slice_payload}")
            first_symbol = slice_payload["symbols"][0]
            if first_symbol.get("price", {}).get("open") != 69000:
                raise AssertionError(f"review-core did not preserve nested price fields: {first_symbol}")
            if len(first_symbol.get("price_chart_signals", [])) != 4:
                raise AssertionError(f"review-core truncated price_chart_signals: {first_symbol}")
            if len(first_symbol.get("news_summary", [])) != 4:
                raise AssertionError(f"review-core truncated news_summary: {first_symbol}")
            if first_symbol.get("warnings") != ["w1", "w2", "w3", "w4"]:
                raise AssertionError(f"review-core truncated warnings: {first_symbol}")
            if first_symbol.get("custom_detail") != {"keep": True}:
                raise AssertionError(f"judge-review review-core dropped custom detail: {first_symbol}")
            if first_symbol.get("symbol_strategy_context", {}).get("downside_add_review_target") is not True:
                raise AssertionError(f"judge-review review-core dropped symbol_strategy_context: {first_symbol}")
            if first_symbol.get("today_trade_timeline_context", {}).get("fills", [{}])[0].get("price") != 70100:
                raise AssertionError(f"judge-review review-core dropped same-day fill timeline: {first_symbol}")
            holding_context = first_symbol.get("holding_quantity_context", {})
            if holding_context.get("expected_holding_quantity") != 11:
                raise AssertionError(f"review-core did not add expected holding context: {first_symbol}")
            if "direction_examples" in holding_context:
                raise AssertionError(f"review-core kept direction examples: {first_symbol}")
            if "target_position_value_krw" not in holding_context.get("target_position_value_semantics", ""):
                raise AssertionError(f"review-core did not add target value semantics: {first_symbol}")
            if "recent_trade_context" not in first_symbol:
                raise AssertionError(f"review-core did not add recent trade context: {first_symbol}")
            recent_trades = first_symbol.get("recent_trade_context", {}).get("recent_submitted_trades", [])
            if len(recent_trades) != 1 or recent_trades[0].get("direction") != "sell":
                raise AssertionError(f"review-core did not preserve recent submitted trade context: {first_symbol}")
            inspected_runs = first_symbol.get("recent_trade_context", {}).get("inspected_run_ids", [])
            if inspected_runs != ["self-test-newer-other", "self-test-prev"]:
                raise AssertionError(f"review-core inspected wrong recent runs: {first_symbol}")
        if key == "analyst_review" and slice_payload.get("slice_type") != "analyst-review-slice":
            raise AssertionError(f"judge-review first slice missing slice_type: {slice_payload}")
        if key == "analyst_review":
            first_symbol = slice_payload["symbols"][0]
            agent_scores = first_symbol.get("agent_scores", [])
            if not agent_scores or agent_scores[0].get("one_line_reason") != "full reason should remain":
                raise AssertionError(f"analyst-review slice dropped agent score reason: {first_symbol}")
            if first_symbol.get("custom_review_detail") != {"keep": True}:
                raise AssertionError(f"analyst-review slice dropped custom detail: {first_symbol}")


def assert_invalid_spec(spec_payload: dict[str, Any], expected: str) -> None:
    try:
        validate_spec(spec_payload)
    except ValueError as exc:
        if expected not in str(exc):
            raise AssertionError(f"expected {expected!r}, got {exc}") from exc
        return
    raise AssertionError(f"invalid spec was accepted; expected {expected!r}")


def assert_argv(argv_log: Path, *, model: str, effort: str) -> None:
    lines = [json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise AssertionError("fake codex argv log is empty")
    argv = lines[-1]
    if "-m" not in argv or argv[argv.index("-m") + 1] != model:
        raise AssertionError(f"expected model {model}, argv={shlex.join(argv)}")
    expected_effort = f'model_reasoning_effort="{effort}"'
    if "-c" not in argv or expected_effort not in argv:
        raise AssertionError(f"expected effort {expected_effort}, argv={shlex.join(argv)}")


def assert_event_diagnostics_wrapper(wrapper: dict[str, Any]) -> None:
    event_path = Path(str(wrapper.get("event_log_path") or ""))
    stderr_path = Path(str(wrapper.get("stderr_path") or ""))
    diagnostics = wrapper.get("event_diagnostics")
    if not isinstance(diagnostics, dict):
        raise AssertionError(f"wrapper missing event diagnostics: {wrapper}")
    if not wrapper.get("event_log_retained") or not event_path.exists():
        raise AssertionError(f"event log was not retained: {wrapper}")
    if not wrapper.get("stderr_retained") or not stderr_path.exists():
        raise AssertionError(f"stderr log was not retained: {wrapper}")
    if diagnostics.get("event_type_counts", {}).get("event_msg:tool_call") != 2:
        raise AssertionError(f"tool call event count missing: {diagnostics}")
    if diagnostics.get("event_type_counts", {}).get("event_msg:tool_result") != 2:
        raise AssertionError(f"tool result event count missing: {diagnostics}")
    if diagnostics.get("event_type_counts", {}).get("turn.completed") != 1:
        raise AssertionError(f"turn.completed event count missing: {diagnostics}")
    if diagnostics.get("event_type_counts", {}).get("token_count") != 1:
        raise AssertionError(f"legacy token_count event count missing: {diagnostics}")
    if diagnostics.get("tool_call_count") != 2 or diagnostics.get("tool_result_count") != 2:
        raise AssertionError(f"tool summary count mismatch: {diagnostics}")
    if int(diagnostics.get("max_tool_result_bytes", 0)) < 2048:
        raise AssertionError(f"max tool result bytes missing: {diagnostics}")
    repeated = diagnostics.get("repeated_tool_fingerprints")
    if not isinstance(repeated, list) or not repeated or repeated[0].get("count") != 2:
        raise AssertionError(f"repeated command fingerprint missing: {diagnostics}")
    usage_events = diagnostics.get("usage_events")
    if not isinstance(usage_events, list) or [item.get("kind") for item in usage_events] != ["token_count", "turn.completed"]:
        raise AssertionError(f"usage event sequence missing: {diagnostics}")
    if wrapper.get("token_usage", {}).get("total_tokens") != 24:
        raise AssertionError(f"mixed usage events were not accumulated as current semantics: {wrapper}")


def run_self_test() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        fake = tmp / "codex"
        argv_log = tmp / "argv.jsonl"
        fake_codex_script(fake)
        old_env = os.environ.copy()
        os.environ["CODEX_BIN"] = str(fake)
        os.environ["FAKE_CODEX_ARGV_LOG"] = str(argv_log)
        os.environ["CODEX_BYPASS_APPROVALS_AND_SANDBOX"] = "1"
        try:
            try:
                assert_all_supported_stages_use_expected_models()
                assert_unsupported_stage_rejected()
                assert_prompt_compaction()
                assert_compact_review_prompt(tmp)
                assert_review_input_slices(tmp)
                missing_brief = compact_spec(
                    tmp, stage="analyst-review", agent_role="analyst-momentum-news", task_name="missing-brief"
                )
                missing_brief["artifact_paths"].pop("decision_brief")
                assert_invalid_spec(missing_brief, "artifact_paths.decision_brief")
                missing_symbols = compact_spec(
                    tmp, stage="analyst-review", agent_role="analyst-momentum-news", task_name="missing-symbols"
                )
                missing_symbols["symbol_ids"] = []
                assert_invalid_spec(missing_symbols, "symbol_ids")
                missing_analyst_review = compact_spec(
                    tmp,
                    stage="judge-review",
                    agent_role="judge",
                    task_name="missing-analyst-review",
                )
                missing_analyst_review["artifact_paths"].pop("analyst_review")
                assert_invalid_spec(missing_analyst_review, "artifact_paths.analyst_review")
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="analyst-review",
                        agent_role="analyst-random",
                        task_name="analyst-random",
                    ),
                    "analyst-review agent_role must be one of",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="judge-review",
                        agent_role="judge-longterm",
                        task_name="judge-longterm",
                    ),
                    "agent_role must be judge",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="judge-review",
                        agent_role="judge-longterm",
                        task_name="judge-longterm-retry1",
                    ),
                    "agent_role must be judge",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="judge-review",
                        agent_role="judge-random",
                        task_name="judge-random",
                    ),
                    "agent_role must be judge",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="judge-review",
                        agent_role="judge",
                        task_name="judge-retry3",
                    ),
                    "at most 2 retries",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="judge-review",
                        agent_role="judge",
                        task_name="judge-attempt3",
                    ),
                    "at most 2 retries",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="judge-review",
                        agent_role="judge",
                        task_name="judge-retry-3",
                    ),
                    "at most 2 retries",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="judge-review",
                        agent_role="judge",
                        task_name="judge-attempt-3",
                    ),
                    "at most 2 retries",
                )
                assert_invalid_spec(
                    compact_spec(
                        tmp,
                        stage="judge-review",
                        agent_role="judge",
                        task_name="judge-retry1-attempt3",
                    ),
                    "at most 2 retries",
                )
                compact_errors = compact_review_payload_errors(
                    {
                        "stage": "analyst-review",
                        "symbols": [
                            {
                                "symbol_id": "005930",
                                "score": 5,
                                "evidence": ["too long for compact review"],
                            }
                        ],
                    },
                    "analyst-review",
                )
                if not compact_errors or compact_errors[0].get("code") != "disallowed_compact_review_key":
                    raise AssertionError(f"compact review disallowed keys were not rejected: {compact_errors}")
                invalid_second_errors = compact_review_payload_errors(
                    {"stage": "judge-review", "portfolio": {}, "symbols": [{}]},
                    "judge-review",
                )
                if not any(error.get("code") == "invalid_compact_review_schema" for error in invalid_second_errors):
                    raise AssertionError(f"invalid compact judge-review schema was accepted: {invalid_second_errors}")
                target_value_errors = compact_review_payload_errors(
                    {
                        "stage": "judge-review",
                        "symbols": [
                            {
                                "symbol_id": "005930",
                                "symbol_name": "삼성전자",
                                "reason_code": "hold_neutral",
                                "one_line_reason": "유지한다.",
                                "target_position_value_krw": 560000,
                                "relative_attractiveness_rank": 1,
                            }
                        ],
                    },
                    "judge-review",
                )
                if target_value_errors:
                    raise AssertionError(f"target_position_value_krw judge-review schema was rejected: {target_value_errors}")
                invalid_target_value_errors = compact_review_payload_errors(
                    {
                        "stage": "judge-review",
                        "symbols": [
                            {
                                "symbol_id": "005930",
                                "symbol_name": "삼성전자",
                                "reason_code": "hold_neutral",
                                "one_line_reason": "유지한다.",
                                "target_position_value_krw": -1,
                                "relative_attractiveness_rank": 1,
                            }
                        ],
                    },
                    "judge-review",
                )
                if not any("target_position_value_krw must be a non-negative number" in str(error.get("message")) for error in invalid_target_value_errors):
                    raise AssertionError(f"invalid target_position_value_krw was not rejected: {invalid_target_value_errors}")
                reduce_to_zero_errors = compact_review_payload_errors(
                    {
                        "stage": "judge-review",
                        "symbols": [
                            {
                                "symbol_id": "005930",
                                "symbol_name": "삼성전자",
                                "reason_code": "exit_position",
                                "one_line_reason": "명시적으로 청산한다.",
                                "target_position_value_krw": 0,
                                "relative_attractiveness_rank": 1,
                            }
                        ],
                    },
                    "judge-review",
                )
                if reduce_to_zero_errors:
                    raise AssertionError(f"reduce-to-zero target_position_value_krw schema was rejected: {reduce_to_zero_errors}")
                top_level_score_payload = normalize_compact_review_payload(
                    {
                        "stage": "analyst-review",
                        "symbols": [
                            {
                                "symbol_id": "005930",
                                "symbol_name": "삼성전자",
                                "score": 6,
                                "confidence": 5,
                                "reason_code": "hold_neutral",
                                "one_line_reason": "top-level score output",
                            }
                        ],
                    },
                    "analyst-review",
                )
                combined_old_shape_errors = compact_review_payload_errors(
                    top_level_score_payload, "analyst-review", "analyst-quality-risk"
                )
                if not any("must include views" in error.get("message", "") for error in combined_old_shape_errors):
                    raise AssertionError(f"combined analyst-review old shape was accepted: {combined_old_shape_errors}")
                raw_with_artifacts = compact_spec(
                    tmp, stage="analyst-review", agent_role="analyst-momentum-news", task_name="raw-with-artifacts"
                )
                raw_with_artifacts["prompt"] = '{"return":"json only"}'
                assert_invalid_spec(raw_with_artifacts, "raw prompt fallback is forbidden")
            except AssertionError as exc:
                failures.append(str(exc))

            model_config = load_subagent_model_config()
            cases = [
                (
                    spec(tmp, stage="financial-collection", agent_role="financial", task_name="financial"),
                    model_config["collection"]["model"],
                    model_config["collection"]["model_reasoning_effort"],
                ),
                (
                    compact_spec(tmp, stage="analyst-review", agent_role="analyst-quality-risk", task_name="first"),
                    model_config["analyst_review"]["model"],
                    model_config["analyst_review"]["model_reasoning_effort"],
                ),
                (
                    compact_spec(tmp, stage="judge-review", agent_role="judge", task_name="second"),
                    model_config["judge_review"]["model"],
                    model_config["judge_review"]["model_reasoning_effort"],
                ),
            ]
            for test_spec, model, effort in cases:
                wrapper = run_one(test_spec)
                if wrapper["status"] != "success":
                    failures.append(f"{test_spec['task_name']} returned {wrapper['status']}")
                if wrapper.get("token_usage", {}).get("total_tokens") != 120:
                    failures.append(f"{test_spec['task_name']} missing token usage: {wrapper}")
                try:
                    assert_argv(argv_log, model=model, effort=effort)
                except AssertionError as exc:
                    failures.append(str(exc))

            old_event_retention = os.environ.get("CODEX_SUBAGENT_EVENT_RETENTION")
            os.environ["CODEX_SUBAGENT_EVENT_RETENTION"] = "always"
            os.environ["FAKE_CODEX_DIAGNOSTIC_EVENTS"] = "1"
            try:
                diagnostic_wrapper = run_one(
                    compact_spec(
                        tmp,
                        stage="analyst-review",
                        agent_role="analyst-momentum-news",
                        task_name="event-diagnostics",
                    )
                )
                try:
                    assert_event_diagnostics_wrapper(diagnostic_wrapper)
                except AssertionError as exc:
                    failures.append(str(exc))
            finally:
                os.environ.pop("FAKE_CODEX_DIAGNOSTIC_EVENTS", None)
                if old_event_retention is None:
                    os.environ.pop("CODEX_SUBAGENT_EVENT_RETENTION", None)
                else:
                    os.environ["CODEX_SUBAGENT_EVENT_RETENTION"] = old_event_retention

            saved_event_retention = os.environ.pop("CODEX_SUBAGENT_EVENT_RETENTION", None)
            try:
                default_retention_wrapper = run_one(
                    compact_spec(
                        tmp,
                        stage="analyst-review",
                        agent_role="analyst-momentum-news",
                        task_name="event-default-retention",
                    )
                )
            finally:
                if saved_event_retention is not None:
                    os.environ["CODEX_SUBAGENT_EVENT_RETENTION"] = saved_event_retention
            if default_retention_wrapper.get("event_retention") != "anomaly":
                failures.append(f"default event retention mode was not anomaly: {default_retention_wrapper}")
            if default_retention_wrapper.get("event_retention_reason") != "no_anomaly":
                failures.append(f"default anomaly event retention reason was not no_anomaly: {default_retention_wrapper}")
            if default_retention_wrapper.get("event_log_retained") is not False:
                failures.append(f"default anomaly event retention did not prune normal event log: {default_retention_wrapper}")
            if default_retention_wrapper.get("stderr_retained") is not False:
                failures.append(f"default anomaly event retention did not mark stderr pruned: {default_retention_wrapper}")
            if Path(default_retention_wrapper["event_log_path"]).exists():
                failures.append("default anomaly event retention left normal event log on disk")
            if Path(default_retention_wrapper["stderr_path"]).exists():
                failures.append("default anomaly event retention left normal stderr log on disk")

            custom_model_config = tmp / "daily-trading-subagents.yaml"
            custom_model_config.write_text(
                "\n".join(
                    [
                        "collection:",
                        "  model: gpt-5.4-mini",
                        "  model_reasoning_effort: low",
                        "analyst_review:",
                        "  model: custom-model",
                        "  model_reasoning_effort: custom-effort",
                        "judge_review:",
                        "  model: gpt-5.5",
                        "  model_reasoning_effort: medium",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            os.environ[SUBAGENT_MODEL_CONFIG_ENV] = str(custom_model_config)
            custom_wrapper = run_one(
                compact_spec(tmp, stage="analyst-review", agent_role="analyst-momentum-news", task_name="first-custom-model")
            )
            if custom_wrapper["status"] != "success":
                failures.append(f"custom model config returned {custom_wrapper['status']}")
            try:
                assert_argv(argv_log, model="custom-model", effort="custom-effort")
            except AssertionError as exc:
                failures.append(str(exc))
            os.environ.pop(SUBAGENT_MODEL_CONFIG_ENV, None)

            write_sample_review_inputs(tmp)
            compact_wrapper = run_one(
                compact_spec(tmp, stage="analyst-review", agent_role="analyst-momentum-news", task_name="compact-first")
            )
            if compact_wrapper["status"] != "success" or compact_wrapper.get("prompt_mode") != "compact_review":
                failures.append(f"compact review spec returned unexpected wrapper: {compact_wrapper}")
            if not compact_wrapper.get("review_input_paths", {}).get("decision_brief"):
                failures.append(f"compact review spec did not create decision brief slice: {compact_wrapper}")

            write_sample_review_inputs(tmp)
            reuse_spec = compact_spec(
                tmp, stage="analyst-review", agent_role="analyst-momentum-news", task_name="reuse-first"
            )
            first_reuse_wrapper = run_one(reuse_spec)
            argv_before = len(argv_log.read_text(encoding="utf-8").splitlines())
            second_reuse_wrapper = run_one(reuse_spec)
            argv_after = len(argv_log.read_text(encoding="utf-8").splitlines())
            if first_reuse_wrapper.get("status") != "success":
                failures.append(f"reuse setup wrapper failed: {first_reuse_wrapper}")
            if not second_reuse_wrapper.get("reused_existing_wrapper") or argv_after != argv_before:
                failures.append(f"successful wrapper was not reused: {second_reuse_wrapper}")
            group_reuse_before = len(argv_log.read_text(encoding="utf-8").splitlines())
            group_reuse = run_group([reuse_spec], max_workers=1)
            group_reuse_after = len(argv_log.read_text(encoding="utf-8").splitlines())
            if (
                group_reuse.get("status") != "success"
                or group_reuse_after != group_reuse_before
                or not group_reuse.get("wrappers", [{}])[0].get("reused_existing_wrapper")
            ):
                failures.append(f"run-group did not pre-reuse successful wrapper: {group_reuse}")
            changed_brief_path = tmp / "reports" / "runs" / "self-test" / "decision-brief.json"
            changed_brief = load_json(changed_brief_path)
            changed_brief["symbols"][0]["custom_detail"] = {"keep": "changed"}
            write_json(changed_brief_path, changed_brief)
            changed_reuse_before = len(argv_log.read_text(encoding="utf-8").splitlines())
            changed_wrapper = run_one(reuse_spec)
            changed_reuse_after = len(argv_log.read_text(encoding="utf-8").splitlines())
            if changed_wrapper.get("reused_existing_wrapper") or changed_reuse_after != changed_reuse_before + 1:
                failures.append(f"changed artifact content incorrectly reused wrapper: {changed_wrapper}")

            old_raw_retention = os.environ.get("CODEX_SUBAGENT_RAW_RETENTION")
            os.environ["CODEX_SUBAGENT_RAW_RETENTION"] = "failed"
            retained_wrapper = run_one(
                compact_spec(tmp, stage="analyst-review", agent_role="analyst-momentum-news", task_name="raw-retention")
            )
            if retained_wrapper.get("raw_output_retained") is not False:
                failures.append(f"successful raw output was not pruned with failed retention: {retained_wrapper}")
            if Path(retained_wrapper["raw_output_path"]).exists():
                failures.append("raw output path still exists after successful failed-retention run")
            if old_raw_retention is None:
                os.environ.pop("CODEX_SUBAGENT_RAW_RETENTION", None)
            else:
                os.environ["CODEX_SUBAGENT_RAW_RETENTION"] = old_raw_retention

            text_spec = spec(tmp, stage="news-collection", agent_role="news", task_name="text-news")
            os.environ["FAKE_CODEX_INVALID_JSON"] = "1"
            wrapper = run_one(text_spec)
            if wrapper["status"] != "success" or wrapper["parsed_json"] is not None or wrapper.get("parsed_text") != "not json":
                failures.append("text collection output was not accepted without JSON parsing")
            os.environ.pop("FAKE_CODEX_INVALID_JSON", None)

            group = run_group(
                [
                    spec(tmp, stage="financial-collection", agent_role="financial", task_name="g-financial"),
                    spec(tmp, stage="news-collection", agent_role="news", task_name="g-news"),
                ],
                max_workers=3,
            )
            if group["status"] != "success" or group["count"] != 2:
                failures.append(f"run-group returned unexpected result: {group}")
            wrapper_count = len(list((Path(group["wrappers"][0]["raw_output_path"]).parent).glob("g-*.wrapper.json")))
            if wrapper_count != 2:
                failures.append(f"expected 2 group wrapper files, got {wrapper_count}")

            os.environ["FAKE_CODEX_EMPTY_TASKS"] = "optional-news"
            optional_group = run_group(
                [
                    spec(tmp, stage="news-collection", agent_role="news", task_name="optional-news"),
                ],
                max_workers=2,
            )
            if (
                optional_group["status"] != "partial"
                or optional_group["failed_count"] != 1
                or optional_group["required_failed_count"] != 0
                or optional_group["optional_failed_count"] != 1
            ):
                failures.append(f"optional text failure did not produce partial group: {optional_group}")

            os.environ["FAKE_CODEX_EMPTY_TASKS"] = "required-first"
            required_group = run_group(
                [
                    compact_spec(tmp, stage="analyst-review", agent_role="analyst-quality-risk", task_name="required-first"),
                    spec(tmp, stage="news-collection", agent_role="news", task_name="required-news"),
                ],
                max_workers=2,
            )
            if (
                required_group["status"] != "failed"
                or required_group["failed_count"] != 1
                or required_group["required_failed_count"] != 1
                or required_group["optional_failed_count"] != 0
            ):
                failures.append(f"required failure did not produce failed group: {required_group}")
            os.environ.pop("FAKE_CODEX_EMPTY_TASKS", None)
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    status = "failed" if failures else "passed"
    print(json.dumps({"status": status, "failures": failures}, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failures else 0


class RunSubagentSelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        self.assertEqual(run_self_test(), 0)
