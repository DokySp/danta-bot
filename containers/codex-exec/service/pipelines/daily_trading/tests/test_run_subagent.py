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
    MODEL_USAGE_FILENAME,
    RUNTIME_CONFIG_ENV,
    build_prompt,
    compact_prompt,
    compact_review_payload_errors,
    debate_final_decision_issues,
    launcher_model_effort,
    load_json,
    load_subagent_model_config,
    mcp_degraded_dependencies,
    normalize_compact_review_payload,
    normalize_thesis_condition_id,
    prior_thesis_context,
    recent_submitted_trade_context,
    run_group,
    run_one,
    thesis_definition_is_valid,
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
        (
            "judge-debate",
            "debate-bull",
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
        if "stage: judge-debate" in prompt:
            phase = next((value for value in ("opening", "rebuttal-1") if f"debate_phase: {value}" in prompt), "opening")
            side = "bull" if "debate_side: bull" in prompt else "bear"
            opponent = "bear" if side == "bull" else "bull"
            symbol_line = next((line for line in prompt.splitlines() if line.startswith("symbol_ids: ")), "symbol_ids: 005930")
            symbols = [value.strip() for value in symbol_line.split(":", 1)[1].split(",") if value.strip()]
            kind = {"opening": "claim", "rebuttal-1": "rebuttal"}[phase]
            payload = {
                "stage": "judge-debate",
                "phase": phase,
                "side": side,
                "symbols": [
                    {
                        "symbol_id": symbol,
                        "symbol_name": symbol,
                        "arguments": [
                            {
                                "argument_id": f"{symbol}-{side}-{phase}-1",
                                "kind": kind,
                                "targets": [] if phase == "opening" else [f"{symbol}-{opponent}-opening-1"],
                                "statement": f"{side} {phase} self-test",
                                "evidence_refs": [f"decision-brief:{symbol}:price"],
                            }
                        ],
                        "concessions": [],
                        "unresolved_conflicts": [],
                        "final_position": "" if phase == "opening" else f"{side} final position",
                        **(
                            {"recommended_action": "hold", "target_holding_quantity": 0}
                            if phase != "opening"
                            else {}
                        ),
                    }
                    for symbol in symbols
                ],
                "errors": [],
            }
        elif "stage: analyst-review" in prompt or "stage: judge-review" in prompt:
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
    prompt = sys.argv[-1] if sys.argv else ""
    if "agent_role: debate-bull" in prompt:
        thread_id = "00000000-0000-4000-8000-000000000001"
    elif "agent_role: debate-bear" in prompt:
        thread_id = "00000000-0000-4000-8000-000000000002"
    else:
        thread_id = "00000000-0000-4000-8000-000000000099"
    print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
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
if os.environ.get("FAKE_CODEX_MCP_INIT_ERROR"):
    print(os.environ["FAKE_CODEX_MCP_INIT_ERROR"], file=sys.stderr)
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
    if stage == "judge-review":
        payload["artifact_paths"]["debate_artifact"] = str(
            tmp / "reports" / "runs" / "self-test" / "judge-debate.json"
        )
    payload["symbol_ids"] = ["005930", {"symbol_id": "000660"}, "005930"]
    return payload


def compact_debate_spec(
    tmp: Path,
    *,
    side: str,
    phase: str,
    task_name: str,
    session_id: str = "",
) -> dict[str, Any]:
    run_dir = tmp / "reports" / "runs" / "self-test"
    payload = {
        "run_id": "self-test",
        "started_at": "2026-06-08T09:00:00+09:00",
        "stage": "judge-debate",
        "agent_role": f"debate-{side}",
        "task_name": task_name,
        "debate_phase": phase,
        "workspace_dir": str(tmp),
        "output_dir": str(run_dir),
        "artifact_paths": {
            "persona": f"prompts/debate-{side}.md",
            "debate_format": "prompts/debate-format.md",
        },
        "symbol_ids": ["005930", "000660"],
        "candidate_directions": {"005930": "buy", "000660": "sell"},
    }
    if phase == "opening":
        payload["artifact_paths"].update(
            {
                "decision_brief": str(run_dir / "decision-brief.json"),
                "analyst_review": str(run_dir / "analyst-review.json"),
            }
        )
    else:
        opponent = "bear" if side == "bull" else "bull"
        payload["resume_session_id"] = session_id
        payload["artifact_paths"].update(
            {
                "opponent_opening": str(run_dir / "debate" / f"opening-{opponent}-compact.json"),
            }
        )
    return payload


def write_sample_review_inputs(tmp: Path) -> None:
    run_dir = tmp / "reports" / "runs" / "self-test"
    write_json(
        run_dir / "judge-debate.json",
        {
            "schema_version": "1",
            "run_id": "self-test",
            "stage": "judge-debate",
            "status": "success",
            "phases": [],
            "errors": [],
        },
    )
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
                    "product_type": "stock",
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
                        "collection_status": "complete",
                        "has_same_day_trade": True,
                        "has_same_day_buy": True,
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
                            "agent_role": "analyst-news-flow",
                            "score": 5,
                            "reason_code": "no_news_excluded",
                            "one_line_reason": "뉴스 정보가 없어 평균에서 제외",
                            "excluded_from_aggregation": True,
                        },
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
        "Every score must be a JSON integer from 0 to 10",
        "Missing optional-domain coverage alone must not pull an included view toward 5",
        "Return JSON only",
    ]
    missing = [part for part in required_parts if part not in prompt]
    if missing:
        raise AssertionError(f"compact review prompt missing {missing}: {prompt}")
    if "Optional evidence marked missing, failed, empty, unavailable" in prompt:
        raise AssertionError(f"judge-only optional-evidence policy leaked into analyst-review prompt: {prompt}")

    second_prompt = build_prompt(
        compact_spec(tmp, stage="judge-review", agent_role="judge", task_name="second")
    )
    second_required_parts = [
        "stage: judge-review",
        "analyst_review:",
        "For judge-review, use the selected-symbol analyst-review slice from analyst_review; agent_scores excluded from aggregation are intentionally omitted from this judgment input.",
        "The supplied symbols are pre-selected candidates by score band: sell candidates are held symbols with final_first_score <= 4, buy candidates have final_first_score >= 6.",
        "Direction preconditions are hard constraints: a sell candidate may only be sold (partial or full) or held (target_position_value_krw <= baseline); a buy candidate may only be bought or held (target_position_value_krw >= baseline).",
        "When the supplied usable evidence itself is insufficient or conflicting, the default decision is hold at the baseline.",
        "final_first_score is the simple mean of the included analyst view scores",
        "For held sell candidates, an intact long-term thesis favors holding despite the low score",
        "Use strategy_context and symbol_strategy_context as advisory inputs for target_position_value_krw, not as order allow/block rules.",
        "debate_artifact:",
        "The Python pipeline already completed bull/bear opening and rebuttal-1 final arguments.",
        "Do not spawn or resume debate agents and do not request another round.",
        "If debate_artifact status is incomplete, or if the completed debate remains balanced or directionally insufficient, hold at the baseline.",
        "Optional evidence marked missing, failed, empty, unavailable, or excluded_from_aggregation is non-directional",
        "Do not infer safety, risk, favorable news, thesis integrity, or thesis damage from the absence of optional evidence.",
        "Do not use optional-domain coverage counts or completeness to decide evidence sufficiency",
        "target_position_value_krw",
        "No additional buy",
        "same-day buy history is unknown",
        "recent trade history is unknown",
    ]
    missing = [part for part in second_required_parts if part not in second_prompt]
    if missing:
        raise AssertionError(f"compact judge-review prompt missing {missing}: {second_prompt}")
    if "spawn the bull/bear" in second_prompt or "hard stop after two rounds" in second_prompt:
        raise AssertionError(f"judge prompt still owns debate orchestration: {second_prompt}")

    opening_prompt = build_prompt(
        compact_debate_spec(
            tmp,
            side="bull",
            phase="opening",
            task_name="debate-bull-opening",
        )
    )
    opening_required = [
        "stage: judge-debate",
        "debate_phase: opening",
        "debate_side: bull",
        "persona: prompts/debate-bull.md",
        "debate_format: prompts/debate-format.md",
        "Opening: independently present the strongest supported case",
        "Every argument.kind must be claim",
    ]
    missing = [part for part in opening_required if part not in opening_prompt]
    if missing:
        raise AssertionError(f"compact opening prompt missing {missing}: {opening_prompt}")
    rebuttal_prompt = build_prompt(
        compact_debate_spec(
            tmp,
            side="bull",
            phase="rebuttal-1",
            task_name="debate-bull-rebuttal-1",
            session_id="00000000-0000-4000-8000-000000000001",
        )
    )
    if (
        "resume_session_id: 00000000-0000-4000-8000-000000000001" not in rebuttal_prompt
        or "opponent_opening:" not in rebuttal_prompt
        or "own_previous_turn" in rebuttal_prompt
        or "Every argument.kind must be rebuttal" not in rebuttal_prompt
        or "recommended_action must be buy|hold|sell" not in rebuttal_prompt
        or "target_holding_quantity must be a non-negative integer" not in rebuttal_prompt
    ):
        raise AssertionError(f"compact resumed rebuttal prompt missing session contract: {rebuttal_prompt}")
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
    if (quality_core.get("symbols") or [{}])[0].get("product_type") != "stock":
        raise AssertionError(f"quality-risk slice dropped product_type needed for financial/ETF policy: {quality_core}")
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
            recent_context = first_symbol.get("recent_trade_context", {})
            if recent_context.get("coverage_status") != "complete" or recent_context.get("inspected_run_count") != 2:
                raise AssertionError(f"review-core did not mark complete recent-trade coverage: {first_symbol}")
        if key == "analyst_review" and slice_payload.get("slice_type") != "analyst-review-slice":
            raise AssertionError(f"judge-review first slice missing slice_type: {slice_payload}")
        if key == "analyst_review":
            first_symbol = slice_payload["symbols"][0]
            agent_scores = first_symbol.get("agent_scores", [])
            if len(agent_scores) != 1 or agent_scores[0].get("one_line_reason") != "full reason should remain":
                raise AssertionError(f"analyst-review slice did not keep only included agent score reasons: {first_symbol}")
            if any(score.get("excluded_from_aggregation") for score in agent_scores if isinstance(score, dict)):
                raise AssertionError(f"analyst-review slice kept excluded agent scores: {first_symbol}")
            if first_symbol.get("custom_review_detail") != {"keep": True}:
                raise AssertionError(f"analyst-review slice dropped custom detail: {first_symbol}")
            source_payload = load_json(tmp / "reports" / "runs" / "self-test" / "analyst-review.json")
            source_scores = source_payload["symbols"][0].get("agent_scores", [])
            if len(source_scores) != 2 or not source_scores[0].get("excluded_from_aggregation"):
                raise AssertionError(f"judge slice filtering mutated canonical analyst-review: {source_payload}")

    coverage_probe = tmp / "coverage-probe"
    current_run = coverage_probe / "current"
    current_run.mkdir(parents=True, exist_ok=True)
    write_json(
        coverage_probe / "previous" / "execution.json",
        {
            "run_id": "coverage-previous",
            "started_at": "2026-06-07T09:00:00+09:00",
            "orders": [],
        },
    )
    partial_context = recent_submitted_trade_context(current_run, "005930", run_limit=2)
    if partial_context.get("coverage_status") != "partial" or partial_context.get("inspected_run_count") != 1:
        raise AssertionError(f"one of two requested prior runs should produce partial coverage: {partial_context}")
    if partial_context.get("recent_submitted_trades") or "only when coverage_status=complete" not in str(partial_context.get("policy")):
        raise AssertionError(f"partial empty recent-trade history should remain explicitly unknown: {partial_context}")
    write_json(coverage_probe / "invalid-orders" / "execution.json", {"run_id": "invalid-orders"})
    malformed_path = coverage_probe / "malformed" / "execution.json"
    malformed_path.parent.mkdir(parents=True, exist_ok=True)
    malformed_path.write_text("{invalid", encoding="utf-8")
    invalid_context = recent_submitted_trade_context(current_run, "005930", run_limit=2)
    if invalid_context.get("coverage_status") != "partial" or invalid_context.get("invalid_execution_count") != 2:
        raise AssertionError(f"invalid execution artifacts should prevent complete recent-trade coverage: {invalid_context}")

    complete_probe = tmp / "complete-coverage-probe"
    complete_current = complete_probe / "current"
    complete_current.mkdir(parents=True, exist_ok=True)
    for index in range(2):
        write_json(
            complete_probe / f"previous-{index}" / "execution.json",
            {
                "run_id": f"complete-previous-{index}",
                "started_at": f"2026-06-0{index + 6}T09:00:00+09:00",
                "orders": [],
            },
        )
    complete_context = recent_submitted_trade_context(complete_current, "005930", run_limit=2)
    if complete_context.get("coverage_status") != "complete" or complete_context.get("recent_submitted_trades"):
        raise AssertionError(f"two valid empty executions should confirm complete empty recent-trade history: {complete_context}")

    unavailable_probe = tmp / "unavailable-coverage-probe" / "current"
    unavailable_probe.mkdir(parents=True, exist_ok=True)
    unavailable_context = recent_submitted_trade_context(unavailable_probe, "005930", run_limit=2)
    if unavailable_context.get("coverage_status") != "unavailable" or unavailable_context.get("inspected_run_count") != 0:
        raise AssertionError(f"zero prior executions should produce unavailable recent-trade coverage: {unavailable_context}")


def assert_debate_optional_evidence_policy() -> None:
    prompt_dir = Path(__file__).resolve().parents[1] / "prompts"
    shared_required = [
        "optional evidence는 주장·약점·반박에 사용하지 않는다",
        "정보 부재를 위험·안전, 호재·악재, thesis 유지·훼손의 근거로 추론하지 않는다",
        "지원되는 논거 없음",
    ]
    for name in ("debate-bull.md", "debate-bear.md"):
        text = (prompt_dir / name).read_text(encoding="utf-8")
        missing = [part for part in shared_required if part not in text]
        if missing:
            raise AssertionError(f"{name} missing optional-evidence policy {missing}")
    judge_text = (prompt_dir / "judge.md").read_text(encoding="utf-8")
    if "optional evidence 부재는 어느 방향의 논거로도 세지 않는다" not in judge_text:
        raise AssertionError("judge.md missing non-directional optional-evidence policy")
    format_text = (prompt_dir / "judge-review-format.md").read_text(encoding="utf-8")
    if "unavailable news is neutral rather than favorable or adverse" not in format_text:
        raise AssertionError("judge-review-format.md missing unavailable-news neutrality policy")
    if "coverage_status=complete" not in format_text or "same-day buy history is unknown" not in format_text:
        raise AssertionError("judge-review-format.md missing trade-history coverage policy")
    analyst_format_text = (prompt_dir / "analyst-review-format.md").read_text(encoding="utf-8")
    quality_text = (prompt_dir / "analyst-quality-risk.md").read_text(encoding="utf-8")
    if "no_financial_excluded" not in analyst_format_text or "no_financial_excluded" not in quality_text:
        raise AssertionError("quality-value prompts missing no-financial aggregation exclusion policy")


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
                assert_debate_optional_evidence_policy()
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
                missing_debate_artifact = compact_spec(
                    tmp,
                    stage="judge-review",
                    agent_role="judge",
                    task_name="missing-debate-artifact",
                )
                missing_debate_artifact["artifact_paths"].pop("debate_artifact")
                assert_invalid_spec(missing_debate_artifact, "artifact_paths.debate_artifact")
                missing_resume = compact_debate_spec(
                    tmp,
                    side="bull",
                    phase="rebuttal-1",
                    task_name="missing-resume",
                )
                assert_invalid_spec(missing_resume, "resume_session_id")
                missing_opponent_opening = compact_debate_spec(
                    tmp,
                    side="bull",
                    phase="rebuttal-1",
                    task_name="missing-opponent-opening",
                    session_id="00000000-0000-4000-8000-000000000001",
                )
                missing_opponent_opening["artifact_paths"].pop("opponent_opening")
                assert_invalid_spec(missing_opponent_opening, "artifact_paths.opponent_opening")
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
                for invalid_score in ("unknown", 5.0, True, -1, 11):
                    invalid_score_errors = compact_review_payload_errors(
                        {
                            "stage": "analyst-review",
                            "symbols": [
                                {
                                    "symbol_id": "005930",
                                    "symbol_name": "삼성전자",
                                    "views": {
                                        "analyst-quality-value": {
                                            "score": invalid_score,
                                            "reason_code": "hold_neutral",
                                            "one_line_reason": "invalid score probe",
                                        },
                                        "analyst-risk-allocation": {
                                            "score": 6,
                                            "reason_code": "buy_candidate",
                                            "one_line_reason": "valid score probe",
                                        },
                                    },
                                }
                            ],
                        },
                        "analyst-review",
                        "analyst-quality-risk",
                    )
                    if not any("score must be an integer from 0 to 10" in str(error.get("message")) for error in invalid_score_errors):
                        raise AssertionError(f"invalid analyst score was accepted: {invalid_score!r} {invalid_score_errors}")
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

            os.environ["FAKE_CODEX_MCP_INIT_ERROR"] = (
                "2026-07-14T03:00:44Z ERROR rmcp::transport::worker: worker quit with fatal: "
                "unexpected server response: HTTP 500, when send initialized notification"
            )
            try:
                degraded_wrapper = run_one(
                    compact_spec(
                        tmp,
                        stage="analyst-review",
                        agent_role="analyst-momentum-news",
                        task_name="mcp-degraded",
                    )
                )
            finally:
                os.environ.pop("FAKE_CODEX_MCP_INIT_ERROR", None)
            if degraded_wrapper.get("status") != "success":
                failures.append(f"MCP degradation incorrectly failed wrapper: {degraded_wrapper}")
            degraded = degraded_wrapper.get("degraded_dependencies")
            if not isinstance(degraded, list) or not degraded or degraded[0].get("dependency_id") != "mcp:unknown":
                failures.append(f"MCP degradation missing from wrapper: {degraded_wrapper}")
            if degraded_wrapper.get("event_diagnostics", {}).get("degraded_dependency_count") != 1:
                failures.append(f"MCP degraded count missing from diagnostics: {degraded_wrapper}")
            if degraded_wrapper.get("event_retention_reason") != "anomaly":
                failures.append(f"MCP degradation did not retain diagnostics: {degraded_wrapper}")

            custom_model_config = tmp / "codex-runtime.yaml"
            custom_model_config.write_text(
                "\n".join(
                    [
                        "defaults:",
                        "  model: gpt-5.6-sol",
                        "  model_reasoning_effort: medium",
                        "  new_session_prompt: new session",
                        "daily_trading:",
                        "  collection:",
                        "    model: gpt-5.4-mini",
                        "    model_reasoning_effort: low",
                        "  analyst_review:",
                        "    model: custom-model",
                        "    model_reasoning_effort: custom-effort",
                        "  judge_review:",
                        "    model: gpt-5.5",
                        "    model_reasoning_effort: medium",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            os.environ[RUNTIME_CONFIG_ENV] = str(custom_model_config)
            custom_wrapper = run_one(
                compact_spec(tmp, stage="analyst-review", agent_role="analyst-momentum-news", task_name="first-custom-model")
            )
            if custom_wrapper["status"] != "success":
                failures.append(f"custom model config returned {custom_wrapper['status']}")
            try:
                assert_argv(argv_log, model="custom-model", effort="custom-effort")
            except AssertionError as exc:
                failures.append(str(exc))
            model_usage_path = tmp / "reports" / "runs" / "self-test" / MODEL_USAGE_FILENAME
            try:
                model_usage_entries = [
                    json.loads(line)
                    for line in model_usage_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except (OSError, json.JSONDecodeError) as exc:
                failures.append(f"model usage log could not be read: {exc}")
                model_usage_entries = []
            custom_model_entries = [
                item for item in model_usage_entries if item.get("task_name") == "first-custom-model"
            ]
            if len(custom_model_entries) != 1:
                failures.append(f"custom model invocation was not recorded exactly once: {custom_model_entries}")
            elif (
                custom_model_entries[0].get("model") != "custom-model"
                or custom_model_entries[0].get("model_reasoning_effort") != "custom-effort"
                or custom_model_entries[0].get("stage") != "analyst-review"
                or custom_model_entries[0].get("agent_role") != "analyst-momentum-news"
            ):
                failures.append(f"custom model invocation log was incorrect: {custom_model_entries[0]}")
            if (
                custom_wrapper.get("model") != "custom-model"
                or custom_wrapper.get("model_reasoning_effort") != "custom-effort"
                or custom_wrapper.get("model_usage_path") != str(model_usage_path)
            ):
                failures.append(f"custom wrapper omitted explicit model provenance: {custom_wrapper}")
            os.environ.pop(RUNTIME_CONFIG_ENV, None)

            write_sample_review_inputs(tmp)
            opening_specs = [
                compact_debate_spec(
                    tmp,
                    side=side,
                    phase="opening",
                    task_name=f"debate-{side}-opening",
                )
                for side in ("bull", "bear")
            ]
            opening_group = run_group(opening_specs, max_workers=2)
            if opening_group.get("status") != "success" or len(opening_group.get("wrappers") or []) != 2:
                failures.append(f"persistent debate opening group failed: {opening_group}")
            opening_by_side = {
                wrapper.get("agent_role", "").removeprefix("debate-"): wrapper
                for wrapper in opening_group.get("wrappers", [])
            }
            expected_session_ids = {
                "bull": "00000000-0000-4000-8000-000000000001",
                "bear": "00000000-0000-4000-8000-000000000002",
            }
            if {side: wrapper.get("session_id") for side, wrapper in opening_by_side.items()} != expected_session_ids:
                failures.append(f"opening did not capture stable debate session ids: {opening_by_side}")
            for side, wrapper in opening_by_side.items():
                write_json(
                    tmp / "reports" / "runs" / "self-test" / "debate" / f"opening-{side}-compact.json",
                    wrapper["parsed_json"],
                )
            for phase in ("rebuttal-1",):
                phase_specs = []
                for side in ("bull", "bear"):
                    opponent = "bear" if side == "bull" else "bull"
                    phase_spec = compact_debate_spec(
                        tmp,
                        side=side,
                        phase=phase,
                        task_name=f"debate-{side}-{phase}",
                        session_id=expected_session_ids[side],
                    )
                    phase_spec["artifact_paths"]["opponent_opening"] = str(
                        tmp / "reports" / "runs" / "self-test" / "debate" / f"opening-{opponent}-compact.json"
                    )
                    phase_specs.append(phase_spec)
                phase_group = run_group(phase_specs, max_workers=2)
                if phase_group.get("status") != "success":
                    failures.append(f"persistent debate {phase} group failed: {phase_group}")
                    break
                phase_by_side = {
                    wrapper.get("agent_role", "").removeprefix("debate-"): wrapper
                    for wrapper in phase_group.get("wrappers", [])
                }
                if any(
                    wrapper.get("session_id") != expected_session_ids.get(side)
                    or wrapper.get("resume_session_id") != expected_session_ids.get(side)
                    or wrapper.get("event_retention") != "always"
                    or not wrapper.get("event_log_retained")
                    for side, wrapper in phase_by_side.items()
                ):
                    failures.append(f"{phase} did not resume and retain the original sessions: {phase_by_side}")
            debate_argv = [
                json.loads(line)
                for line in argv_log.read_text(encoding="utf-8").splitlines()
                if "stage: judge-debate" in line
            ]
            resume_argv = [argv for argv in debate_argv if "resume" in argv]
            if len(debate_argv) != 4 or len(resume_argv) != 2:
                failures.append(f"fixed debate should use 2 openings and 2 resumed turns: {debate_argv}")

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
            reuse_model_entries = [
                json.loads(line)
                for line in model_usage_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and json.loads(line).get("task_name") == "reuse-first"
            ]
            if len(reuse_model_entries) != 1:
                failures.append(f"reused wrapper recorded a non-executed model invocation: {reuse_model_entries}")
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
            try:
                group_model_entries = [
                    json.loads(line)
                    for line in model_usage_path.read_text(encoding="utf-8").splitlines()
                    if line.strip() and json.loads(line).get("task_name") in {"g-financial", "g-news"}
                ]
            except json.JSONDecodeError as exc:
                failures.append(f"parallel model usage writes produced invalid JSONL: {exc}")
                group_model_entries = []
            if {item.get("task_name") for item in group_model_entries} != {"g-financial", "g-news"}:
                failures.append(f"parallel model invocations were not recorded: {group_model_entries}")
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

    def test_named_mcp_server_is_preserved_in_degraded_dependency(self) -> None:
        dependencies = mcp_degraded_dependencies(
            "MCP server 'kis-trading' failed during initialization: HTTP 503"
        )

        self.assertEqual(len(dependencies), 1)
        self.assertEqual(dependencies[0]["dependency_id"], "mcp:kis-trading")
        self.assertEqual(dependencies[0]["server_identifier_source"], "stderr")
        self.assertEqual(dependencies[0]["http_status"], 503)

    def test_recent_trade_context_uses_current_lifecycle_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            runs_dir = Path(tmp_name) / "runs"
            previous = runs_dir / "previous"
            current = runs_dir / "current"
            previous.mkdir(parents=True)
            current.mkdir(parents=True)
            write_json(
                previous / "execution.json",
                {
                    "run_id": "previous",
                    "started_at": "2026-07-15T14:30:00+09:00",
                    "orders": [
                        {
                            "symbol_id": "042660",
                            "direction": "buy",
                            "result": "submitted",
                            "order_path": "immediate",
                            "order_api": "order_cash",
                            "validated_order_quantity": 1,
                            "order_or_reservation_id": "reject-1",
                            "broker_reconciliation": {"status": "unconfirmed"},
                        }
                    ],
                },
            )
            write_json(
                current / "order-lifecycle.json",
                {
                    "previous_submitted_cash_orders": [
                        {
                            "order_id": "reject-1",
                            "broker_reconciliation": {
                                "status": "rejected",
                                "terminal": True,
                                "filled_quantity": 0,
                                "rejected_quantity": 1,
                                "remaining_quantity": 0,
                            },
                        }
                    ]
                },
            )

            context = recent_submitted_trade_context(current, "042660", run_limit=1)

            trade = context["recent_submitted_trades"][0]
            self.assertEqual(trade["broker_status"], "rejected")
            self.assertTrue(trade["broker_terminal"])
            self.assertEqual(trade["broker_rejected_quantity"], 1)

    def test_prior_thesis_context_selects_latest_earlier_successful_valid_definition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            runs_dir = Path(tmp_name) / "runs"
            current = runs_dir / "current"
            current.mkdir(parents=True)

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

            valid = {
                "core_rationale": "quality moat",
                "invalidation_conditions": [{"condition_id": "cond-a", "description": "description a"}],
            }
            current_started_at = "2026-06-02T09:00:00+09:00"
            write_run("future-run", "2026-06-05T09:00:00+09:00", "success", dict(valid, core_rationale="future"))
            write_run("equal-time-run", current_started_at, "success", dict(valid, core_rationale="equal-time"))
            write_run("partial-run", "2026-06-01T12:00:00+09:00", "partial", dict(valid, core_rationale="partial"))
            write_run("failed-run", "2026-06-01T11:00:00+09:00", "failed", dict(valid, core_rationale="failed"))
            malformed_dir = runs_dir / "malformed-run"
            malformed_dir.mkdir(parents=True, exist_ok=True)
            (malformed_dir / "judge-review.json").write_text("not json", encoding="utf-8")
            write_run("empty-thesis-run", "2026-06-01T13:00:00+09:00", "success", {"core_rationale": "", "invalidation_conditions": []})
            write_run("older-valid-run", "2026-06-01T08:00:00+09:00", "success", dict(valid, core_rationale="older-valid"))
            write_run("latest-valid-run", "2026-06-01T20:00:00+09:00", "success", dict(valid, core_rationale="latest-valid"))

            context = prior_thesis_context(current, "005930", current_started_at)
            self.assertEqual(context["status"], "available")
            self.assertEqual(context["source_run_id"], "latest-valid-run")
            self.assertEqual(context["thesis_definition"]["core_rationale"], "latest-valid")

            # No symbol match at all -> explicit no_prior_thesis, not a crash.
            missing_context = prior_thesis_context(current, "999999", current_started_at)
            self.assertEqual(missing_context["status"], "no_prior_thesis")
            self.assertIsNone(missing_context["thesis_definition"])

    def test_prior_thesis_context_treats_naive_started_at_as_kst_matching_pipeline(self) -> None:
        # Reproduction: current=2026-06-02T09:00:00+09:00, prior started_at is naive
        # "2026-06-02T01:00:00". run_daily_trading_pipeline.parse_kst_datetime treats a
        # naive timestamp as KST (01:00 KST is strictly before 09:00 KST -> eligible).
        # run_subagent.parse_iso_datetime must agree, not treat it as UTC (which would
        # be 01:00 UTC == 10:00 KST, i.e. after current, and wrongly excluded).
        with tempfile.TemporaryDirectory() as tmp_name:
            runs_dir = Path(tmp_name) / "runs"
            current = runs_dir / "current"
            current.mkdir(parents=True)
            prior_dir = runs_dir / "naive-prior-run"
            prior_dir.mkdir(parents=True)
            write_json(
                prior_dir / "judge-review.json",
                {
                    "run_id": "naive-prior-run",
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
            context = prior_thesis_context(current, "005930", "2026-06-02T09:00:00+09:00")
            self.assertEqual(context["status"], "available")
            self.assertEqual(context["source_run_id"], "naive-prior-run")
            self.assertEqual(context["thesis_definition"]["core_rationale"], "quality moat")

    def test_normalize_thesis_condition_id_preserves_korean_and_rejects_unusable_input(self) -> None:
        # Mirrors run_daily_trading_pipeline.normalize_thesis_condition_id exactly.
        self.assertEqual(normalize_thesis_condition_id("한글조건"), "한글조건")
        self.assertEqual(normalize_thesis_condition_id("  한글 조건!! 이름  "), "한글-조건-이름")
        self.assertEqual(normalize_thesis_condition_id("Margin  Compression!!"), "margin-compression")
        self.assertEqual(normalize_thesis_condition_id("--leading-and-trailing--"), "leading-and-trailing")
        self.assertEqual(normalize_thesis_condition_id("already.valid_id-1"), "already.valid_id-1")
        self.assertEqual(normalize_thesis_condition_id(1), "")
        self.assertEqual(normalize_thesis_condition_id(True), "")
        self.assertEqual(normalize_thesis_condition_id(None), "")
        self.assertEqual(normalize_thesis_condition_id({"condition_id": "x"}), "")
        self.assertEqual(normalize_thesis_condition_id(["x"]), "")
        self.assertEqual(normalize_thesis_condition_id("   "), "")
        self.assertEqual(normalize_thesis_condition_id("!!!"), "")
        self.assertEqual(normalize_thesis_condition_id("...___---"), "")
        self.assertEqual(normalize_thesis_condition_id("._condition_."), "condition")
        self.assertEqual(normalize_thesis_condition_id("a" * 200), "a" * 64)

    def test_thesis_definition_is_valid_requires_actual_string_fields(self) -> None:
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
        valid_fields = {
            "core_rationale": "quality moat",
            "invalidation_conditions": [{"condition_id": "cond-a", "description": "description a"}],
        }
        self.assertTrue(thesis_definition_is_valid(valid_fields))

    def test_prior_thesis_context_rejects_prior_with_non_string_thesis_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            runs_dir = Path(tmp_name) / "runs"
            current = runs_dir / "current"
            current.mkdir(parents=True)
            prior_dir = runs_dir / "numeric-fields-prior-run"
            prior_dir.mkdir(parents=True)
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
            context = prior_thesis_context(current, "005930", "2026-06-02T09:00:00+09:00")
            self.assertEqual(context["status"], "no_prior_thesis")

    def test_prior_thesis_context_breaks_ties_by_source_run_id_when_started_at_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            runs_dir = Path(tmp_name) / "runs"
            current = runs_dir / "current"
            current.mkdir(parents=True)
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

            write_run("tie-run-aaa", "from aaa")
            write_run("tie-run-zzz", "from zzz")

            context = prior_thesis_context(current, "005930", "2026-06-02T09:00:00+09:00")
            self.assertEqual(context["status"], "available")
            self.assertEqual(context["source_run_id"], "tie-run-zzz")
            # Same deterministic winner as run_daily_trading_pipeline.load_prior_thesis
            # for the identical fixture (see test_load_prior_thesis_breaks_ties_by_source_run_id_when_started_at_matches).
            self.assertEqual(context["thesis_definition"]["core_rationale"], "from zzz")

    def test_write_review_input_slices_surfaces_prior_thesis_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            write_sample_review_inputs(tmp)
            prior_dir = tmp / "reports" / "runs" / "self-test-prior-thesis"
            prior_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                prior_dir / "judge-review.json",
                {
                    "run_id": "self-test-prior-thesis",
                    "started_at": "2026-06-07T09:00:00+09:00",
                    "status": "success",
                    "symbols": [
                        {
                            "symbol_id": "005930",
                            "thesis_definition": {
                                "core_rationale": "quality moat and pricing power",
                                "invalidation_conditions": [
                                    {"condition_id": "margin-compression", "description": "gross margin drops below prior guidance"}
                                ],
                            },
                        }
                    ],
                },
            )
            payload = compact_spec(tmp, stage="judge-review", agent_role="judge", task_name="prior-thesis-slice")
            slice_paths = write_review_input_slices(payload)
            core = load_json(Path(slice_paths["review_core"]))
            symbol = next(item for item in core["symbols"] if item.get("symbol_id") == "005930")
            prior = symbol.get("prior_thesis_context")
            self.assertIsNotNone(prior)
            self.assertEqual(prior["status"], "available")
            self.assertEqual(prior["source_run_id"], "self-test-prior-thesis")
            self.assertEqual(
                prior["thesis_definition"]["invalidation_conditions"],
                [{"condition_id": "margin-compression", "description": "gross margin drops below prior guidance"}],
            )

            other_symbol = next(item for item in core["symbols"] if item.get("symbol_id") == "000660")
            self.assertEqual(other_symbol.get("prior_thesis_context", {}).get("status"), "no_prior_thesis")

    def test_rebuttal_final_decision_issues_validate_action_and_quantity(self) -> None:
        spec = {
            "debate_phase": "rebuttal-1",
            "symbol_ids": ["005930"],
            "candidate_directions": {"005930": "buy"},
            "portfolio_snapshot": [
                {"symbol_id": "005930", "current_live_holding_quantity": 2}
            ],
        }
        payload = {
            "symbols": [
                {
                    "symbol_id": "005930",
                    "arguments": [{"evidence_refs": ["decision-brief:005930:price"]}],
                    "final_position": "hold",
                    "recommended_action": "hold",
                    "target_holding_quantity": 2,
                }
            ]
        }
        self.assertEqual(debate_final_decision_issues(payload, spec), [])

        payload["symbols"][0]["target_holding_quantity"] = "2"
        self.assertIn(
            "invalid_debate_target_holding_quantity",
            {item["code"] for item in debate_final_decision_issues(payload, spec)},
        )

        payload["symbols"][0]["recommended_action"] = "buy"
        payload["symbols"][0]["target_holding_quantity"] = 1
        payload["symbols"][0]["arguments"][0]["evidence_refs"] = []
        codes = {item["code"] for item in debate_final_decision_issues(payload, spec)}
        self.assertEqual(
            codes,
            {
                "inconsistent_debate_action_quantity",
                "invalid_debate_candidate_direction",
                "missing_debate_evidence_refs",
            },
        )
