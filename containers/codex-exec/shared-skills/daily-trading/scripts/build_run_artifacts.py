#!/usr/bin/env python3
"""Build deterministic daily-trading run artifacts.

This helper keeps data shaping, spec generation, verdict merging, execution
planning, and token accounting out of the Main agent prompt path.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FIRST_VERDICT_ROLES = (
    "analyst-quality-value",
    "analyst-momentum-cycle",
    "analyst-risk-allocation",
)
TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def skill_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_yaml(path: Path | None) -> Any:
    if not path:
        return None
    try:
        import yaml  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def read_json_arg(value: str | None) -> Any:
    if not value:
        return {}
    if value == "-":
        return json.load(sys.stdin)
    return load_json(Path(value))


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "unknown"


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return int(number)
    return number


def clamp_score(value: Any, default: int = 5) -> int:
    return max(0, min(10, as_int(value, default)))


def round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def normalize_symbol_ids(raw: Any) -> list[str]:
    values = raw
    if isinstance(raw, dict):
        values = raw.get("universe") or raw.get("symbols") or raw.get("symbol_ids") or []
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        if isinstance(item, dict):
            value = item.get("symbol_id") or item.get("symbol") or item.get("code")
        else:
            value = item
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def common_envelope(run_id: str, started_at: str, stage: str, status: str = "success") -> dict[str, Any]:
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_iso(),
        "stage": stage,
        "status": status,
        "skipped": False,
        "skip_reason": "",
        "errors": [],
        "symbols": [],
    }


def symbol_key(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("symbol_id") or item.get("symbol") or item.get("code") or "").strip()


def indexed_symbols(items: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        key = symbol_key(item)
        if key:
            result[key] = item
    return result


def compact_account_exposure(item: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {
            "current_live_holding_quantity": 0,
            "valuation_amount": 0,
            "pnl_amount": 0,
            "pnl_rate": None,
        }
    return {
        "current_live_holding_quantity": as_int(item.get("current_live_holding_quantity")),
        "valuation_amount": as_number(item.get("valuation_amount")) or 0,
        "pnl_amount": as_number(item.get("pnl_amount")) or 0,
        "pnl_rate": as_number(item.get("pnl_rate")),
    }


def account_summary(account: dict[str, Any]) -> dict[str, Any]:
    summary = account.get("account_summary")
    if not isinstance(summary, dict):
        return {}
    keys = (
        "cash_amount",
        "securities_valuation_amount",
        "today_buy_amount",
        "today_sell_amount",
        "total_evaluation_amount",
        "total_pnl_amount",
    )
    return {key: summary.get(key) for key in keys}


def shorten(value: Any, limit: int = 160) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def unwrap_financial_symbol_payload(symbol_payload: Any) -> dict[str, Any] | None:
    if not isinstance(symbol_payload, dict):
        return None
    if "주식현재가 시세" in symbol_payload or "국내주식 종목추정실적" in symbol_payload:
        return symbol_payload
    for nested in symbol_payload.values():
        if isinstance(nested, dict) and ("주식현재가 시세" in nested or "국내주식 종목추정실적" in nested):
            return nested
    return symbol_payload


def is_no_news_content(value: Any) -> bool:
    text = str(value or "").strip()
    return not text or "수집된 뉴스가 없습니다" in text


def financial_summary_for(cache: Any, symbol_id: str, cache_path: str) -> dict[str, Any]:
    summary = {
        "cache_path": cache_path or "",
        "cache_status": "supplied" if cache_path else "missing",
        "items": [],
    }
    if not cache_path:
        return summary
    if not isinstance(cache, dict):
        summary["cache_status"] = "supplied_unparsed"
        return summary
    symbols = cache.get("symbols") if isinstance(cache.get("symbols"), dict) else {}
    if symbol_id not in symbols:
        summary["cache_status"] = "missing_symbol"
        return summary
    symbol_payload = unwrap_financial_symbol_payload(symbols.get(symbol_id) or {})
    if symbol_payload is None:
        summary["cache_status"] = "missing_symbol"
        return summary
    price_rows = ((symbol_payload.get("주식현재가 시세") or {}).get("응답") or [])
    price = price_rows[0] if price_rows and isinstance(price_rows[0], dict) else {}
    opinion_rows = ((symbol_payload.get("국내주식 종목추정실적") or {}).get("종목 및 최신 투자의견 요약") or [])
    opinion = opinion_rows[0] if opinion_rows and isinstance(opinion_rows[0], dict) else {}
    quote_parts = []
    for label, key in (("현재가", "현재가"), ("등락률", "전일 대비율"), ("PER", "주가수익비율(PER)"), ("PBR", "주가순자산비율(PBR)")):
        if price.get(key) not in (None, ""):
            suffix = "%" if key == "전일 대비율" else ""
            quote_parts.append(f"{label} {price.get(key)}{suffix}")
    if quote_parts:
        summary["items"].append(", ".join(quote_parts))
    if opinion.get("추천의견"):
        summary["items"].append(f"최신 투자의견 {opinion.get('추천의견')}")
    if price.get("업종명"):
        summary["items"].append(f"업종 {price.get('업종명')}")
    summary["items"] = summary["items"][:3]
    if not summary["items"]:
        summary["cache_status"] = "supplied_empty"
    return summary


def etf_summary_for(cache: Any, symbol_id: str, cache_path: str) -> dict[str, Any]:
    summary = {
        "cache_path": cache_path or "",
        "cache_status": "supplied" if cache_path else "missing",
        "items": [],
    }
    if not cache_path:
        return summary
    if not isinstance(cache, dict):
        summary["cache_status"] = "supplied_unparsed"
        return summary
    symbols = cache.get("symbols") if isinstance(cache.get("symbols"), dict) else {}
    if symbol_id not in symbols:
        summary["cache_status"] = "missing_symbol"
        return summary
    symbol_payload = unwrap_financial_symbol_payload(symbols.get(symbol_id) or {})
    if not isinstance(symbol_payload, dict):
        summary["cache_status"] = "missing_symbol"
        return summary
    etf_price_rows = ((symbol_payload.get("ETF/ETN 현재가") or {}).get("응답") or [])
    etf_price = etf_price_rows[0] if etf_price_rows and isinstance(etf_price_rows[0], dict) else {}
    nav_payload = symbol_payload.get("NAV 비교추이(종목)") or {}
    nav_summary_rows = nav_payload.get("NAV 비교 요약") or nav_payload.get("응답 1") or nav_payload.get("output1") or []
    nav_trend_rows = nav_payload.get("NAV 비교 추이") or nav_payload.get("응답 2") or nav_payload.get("output2") or []
    nav_summary = nav_summary_rows[0] if isinstance(nav_summary_rows, list) and nav_summary_rows and isinstance(nav_summary_rows[0], dict) else {}
    nav_trend = nav_trend_rows[0] if isinstance(nav_trend_rows, list) and nav_trend_rows and isinstance(nav_trend_rows[0], dict) else {}
    parts = []
    for label, keys in (
        ("NAV", ("NAV", "nav")),
        ("괴리율", ("괴리율", "dprt")),
        ("추적오차", ("추적오차", "ETF 추적수익률 차이", "etf_chas_erng_rt_dbnb")),
        ("거래량", ("누적 거래량", "acml_vol")),
    ):
        value = first_present(etf_price, keys) or first_present(nav_summary, keys) or first_present(nav_trend, keys)
        if value not in (None, ""):
            parts.append(f"{label} {value}")
    if parts:
        summary["items"].append(", ".join(parts[:4]))
    for label, keys in (("NAV 전일대비율", ("NAV 전일 대비율", "nav_prdy_ctrt")), ("전일대비율", ("전일 대비율", "prdy_ctrt"))):
        value = first_present(nav_summary, keys) or first_present(nav_trend, keys) or first_present(etf_price, keys)
        if value not in (None, ""):
            summary["items"].append(f"{label} {value}")
    summary["items"] = summary["items"][:3]
    if not summary["items"]:
        summary["cache_status"] = "supplied_empty"
    return summary


def news_summary_for(cache: Any, symbol_id: str, cache_path: str) -> list[dict[str, Any]]:
    if not cache_path or not isinstance(cache, dict):
        return []
    symbols = cache.get("symbols") if isinstance(cache.get("symbols"), dict) else cache
    entries = symbols.get(symbol_id) if isinstance(symbols, dict) else None
    if isinstance(entries, dict):
        entries = entries.get("items") or entries.get("news") or entries.get("articles") or []
    if not isinstance(entries, list):
        return []
    result: list[dict[str, Any]] = []
    for item in entries[:3]:
        if not isinstance(item, dict):
            continue
        content = shorten(item.get("content") or item.get("text") or item.get("title") or "")
        if is_no_news_content(content):
            continue
        result.append(
            {
                "article_date": item.get("article_date") or item.get("date") or "",
                "sentiment": item.get("sentiment") or "neutral",
                "content": content,
            }
        )
    return result


def compact_chart_context(item: dict[str, Any]) -> dict[str, Any]:
    charts = item.get("charts") if isinstance(item.get("charts"), dict) else {}
    result: dict[str, Any] = {}
    for key in ("daily", "weekly", "monthly"):
        rows = charts.get(key) if isinstance(charts, dict) else []
        if not isinstance(rows, list):
            rows = []
        compact_rows = [row for row in rows if isinstance(row, dict)][:20]
        if compact_rows:
            result[key] = compact_rows
    intraday = item.get("intraday") if isinstance(item.get("intraday"), list) else []
    compact_intraday = [row for row in intraday if isinstance(row, dict)][:10]
    if compact_intraday:
        result["intraday"] = compact_intraday
    return result


def compact_optional_dict(item: dict[str, Any], key: str) -> dict[str, Any]:
    value = item.get(key)
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items() if v not in (None, "", [], {})}


def build_decision_brief(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    portfolio = read_json_arg(args.portfolio_json)
    price_chart = load_json(Path(args.price_chart or output_dir / "price-chart.json"))
    account = load_json(Path(args.account_before_order or output_dir / "account-before-order.json"))
    run_id = args.run_id or price_chart.get("run_id") or account.get("run_id") or output_dir.name
    started_at = args.started_at or price_chart.get("started_at") or account.get("started_at") or ""

    account_by_symbol = indexed_symbols(account.get("symbols"))
    source_artifacts = ["price-chart.json", "account-before-order.json", "check-portfolio JSON"]
    if args.financial_cache_path:
        source_artifacts.append(args.financial_cache_path)
    if args.news_cache_path:
        source_artifacts.append(args.news_cache_path)

    financial_cache = load_yaml(Path(args.financial_cache_path)) if args.financial_cache_path else None
    news_cache = load_yaml(Path(args.news_cache_path)) if args.news_cache_path else None

    artifact = common_envelope(run_id, started_at, "decision-brief")
    artifact.update(
        {
            "brief_type": "decision-brief",
            "source_artifacts": source_artifacts,
            "portfolio": {
                "recommanded": portfolio.get("recommanded", []),
                "specified": portfolio.get("specified", []),
                "holding": portfolio.get("holding", []),
                "universe": portfolio.get("universe", []),
            },
            "account_exposure_summary": account_summary(account),
        }
    )

    if account.get("active_order_lookup_performed") is False or account.get("order_available_lookup_performed") is False:
        artifact["errors"].append(
            {
                "stage": "account-before-order",
                "source": "collect_main_evidence",
                "code": "order_gate_fields_missing",
                "message": "active_order_lookup_performed/order_available_lookup_performed are false; order submission requires refresh before execution",
                "required": False,
            }
        )

    for item in price_chart.get("symbols", []):
        if not isinstance(item, dict):
            continue
        symbol_id = symbol_key(item)
        account_item = account_by_symbol.get(symbol_id)
        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        required_missing = list(item.get("required_missing") or [])
        errors = list(item.get("errors") or [])
        usable_price = price.get("current_or_last") is not None and bool(price.get("observed_at"))
        eligible = bool(item.get("eligible_for_verdict", True)) and usable_price and not required_missing
        if not usable_price and "price.current_or_last/observed_at" not in required_missing:
            required_missing.append("price.current_or_last/observed_at")
        financial_summary = financial_summary_for(financial_cache, symbol_id, args.financial_cache_path)
        etf_summary = etf_summary_for(financial_cache, symbol_id, args.financial_cache_path) if str(item.get("product_type") or "").lower() in {"etf", "etn"} else {}
        symbol = {
            "symbol_id": symbol_id,
            "symbol_name": item.get("symbol_name") or (account_item or {}).get("symbol_name") or symbol_id,
            "product_type": item.get("product_type") or "stock",
            "eligible_for_verdict": eligible,
            "evidence_mode": "full" if financial_summary["cache_status"] == "supplied" else "price_only",
            "exclusion_reasons": [] if eligible else required_missing,
            "price": {
                "current_or_last": price.get("current_or_last"),
                "observed_at": price.get("observed_at") or "",
                "snapshot_mode": price.get("snapshot_mode") or "",
            },
            "price_chart_signals": list(item.get("local_signals") or [])[:12],
            "chart_context": compact_chart_context(item),
            "orderbook_summary": compact_optional_dict(item, "orderbook_summary"),
            "trade_flow_summary": compact_optional_dict(item, "trade_flow_summary"),
            "investor_flow_summary": compact_optional_dict(item, "investor_flow_summary"),
            "financial_summary": financial_summary,
            "etf_summary": etf_summary,
            "news_summary": news_summary_for(news_cache, symbol_id, args.news_cache_path),
            "account_exposure": compact_account_exposure(account_item),
            "required_missing": required_missing,
            "warnings": list(item.get("warnings") or []),
            "errors": errors,
        }
        artifact["symbols"].append(symbol)

    if artifact["errors"] or any(not item.get("eligible_for_verdict") for item in artifact["symbols"]):
        artifact["status"] = "partial"
    write_json(Path(args.output), artifact)
    return artifact


def eligible_symbol_ids(decision_brief: dict[str, Any]) -> list[str]:
    return [
        symbol_key(item)
        for item in decision_brief.get("symbols", [])
        if isinstance(item, dict) and item.get("eligible_for_verdict") and symbol_key(item)
    ]


def artifact_path(path: str | Path, absolute: bool) -> str:
    path_obj = Path(path)
    if absolute:
        return str(path_obj.resolve())
    return str(path_obj)


def build_first_specs(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    decision_brief = load_json(Path(args.decision_brief or output_dir / "decision-brief.json"))
    symbol_ids = normalize_symbol_ids(args.symbol_ids.split(",") if args.symbol_ids else eligible_symbol_ids(decision_brief))
    run_id = args.run_id or decision_brief.get("run_id") or output_dir.name
    started_at = args.started_at or decision_brief.get("started_at") or ""
    workspace_dir = str(Path(args.workspace_dir).resolve())
    daily_skill_dir = Path(args.skill_dir).resolve()
    absolute_paths = not args.relative_paths

    specs = []
    for role in FIRST_VERDICT_ROLES:
        specs.append(
            {
                "run_id": run_id,
                "started_at": started_at,
                "stage": "first-verdict",
                "agent_role": role,
                "task_name": f"first-{role}",
                "workspace_dir": workspace_dir,
                "output_dir": str(output_dir),
                "artifact_paths": {
                    "decision_brief": artifact_path(args.decision_brief or output_dir / "decision-brief.json", absolute_paths),
                    "persona": artifact_path(daily_skill_dir / "references" / "personas" / f"{role}.md", absolute_paths),
                    "verdict_format": artifact_path(daily_skill_dir / "references" / "rules" / "verdict-format.md", absolute_paths),
                },
                "symbol_ids": symbol_ids,
            }
        )
    payload = {"specs": specs}
    write_json(Path(args.output), payload)
    return payload


def normalize_verdict_payload(payload: Any, stage: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    normalized = dict(payload)
    if not isinstance(normalized.get("symbols"), list):
        for key in ("verdicts", "results", "items"):
            if isinstance(normalized.get(key), list):
                normalized["symbols"] = normalized[key]
                break
    if stage == "first-verdict" and isinstance(normalized.get("symbols"), list):
        symbols = []
        for item in normalized["symbols"]:
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            copied.setdefault("missing_data", [])
            copied["reason_code"] = safe_name(str(copied.get("reason_code") or "hold_neutral")).lower()
            symbols.append(copied)
        normalized["symbols"] = symbols
    return normalized


def confidence_adjusted_score(score: int, confidence: int) -> float:
    return 5 + ((score - 5) * (confidence / 10))


def first_sidecar_path(output_dir: Path, role: str, task_name: str) -> Path:
    return output_dir / "verdicts" / f"first-verdict--{safe_name(role)}--{safe_name(task_name)}.md"


def second_sidecar_path(output_dir: Path, role: str, task_name: str) -> Path:
    return output_dir / "verdicts" / f"second-verdict--{safe_name(role)}--{safe_name(task_name)}.md"


def write_first_sidecar(path: Path, symbols: list[dict[str, Any]]) -> None:
    lines = [
        "| 종목 | 점수 | confidence(확신도) | 의견(판단) |",
        "|---|---:|---:|---|",
    ]
    for item in symbols:
        symbol_name = f"{item.get('symbol_id', '')} {item.get('symbol_name', '')}".strip()
        lines.append(
            f"| {symbol_name} | {as_int(item.get('score'))} | {as_int(item.get('confidence'))} | {item.get('one_line_reason', '')} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_second_sidecar(path: Path, symbols: list[dict[str, Any]]) -> None:
    lines = [
        "| 종목 | 목표수량 | 상대매력도 | 판단코드 | 의견(판단) |",
        "|---|---:|---:|---|---|",
    ]
    for item in symbols:
        symbol_name = f"{item.get('symbol_id', '')} {item.get('symbol_name', '')}".strip()
        lines.append(
            f"| {symbol_name} | {as_int(item.get('target_holding_quantity'))} | {as_int(item.get('relative_attractiveness_rank'))} | {item.get('reason_code', '')} | {item.get('one_line_reason', '')} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_success_first_wrappers(subagent_dir: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    by_role: dict[str, dict[str, Any]] = {}
    failures_by_role: dict[str, list[str]] = {}
    errors: list[dict[str, Any]] = []
    for wrapper_path in sorted(subagent_dir.glob("*.wrapper.json")):
        wrapper = load_json(wrapper_path)
        if wrapper.get("stage") != "first-verdict":
            continue
        role = str(wrapper.get("agent_role") or "")
        if wrapper.get("status") != "success":
            failures_by_role.setdefault(role or wrapper_path.name, []).append(wrapper_path.name)
            continue
        previous = by_role.get(role)
        if previous is None or str(wrapper.get("ended_at", "")) >= str(previous.get("ended_at", "")):
            by_role[role] = wrapper
    for role in FIRST_VERDICT_ROLES:
        if role in by_role:
            continue
        for source in failures_by_role.get(role, []):
            errors.append(
                {
                    "stage": "first-verdict",
                    "source": source,
                    "code": "wrapper_failed",
                    "message": f"{role} wrapper has no successful replacement",
                    "required": True,
                }
            )
    return by_role, errors


def build_verdict_first(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    decision_brief = load_json(Path(args.decision_brief or output_dir / "decision-brief.json"))
    symbols_by_id = indexed_symbols(decision_brief.get("symbols"))
    symbol_ids = normalize_symbol_ids(args.symbol_ids.split(",") if args.symbol_ids else eligible_symbol_ids(decision_brief))
    wrappers_by_role, errors = load_success_first_wrappers(output_dir / "subagents")

    agent_scores: dict[str, list[dict[str, Any]]] = {symbol_id: [] for symbol_id in symbol_ids}
    for role, wrapper in wrappers_by_role.items():
        payload = normalize_verdict_payload(wrapper.get("parsed_json"), "first-verdict")
        if payload is None:
            errors.append(
                {
                    "stage": "first-verdict",
                    "source": wrapper.get("task_name") or role,
                    "code": "missing_parsed_json",
                    "message": "successful wrapper has no parsed_json object",
                    "required": True,
                }
            )
            continue
        symbols = [item for item in payload.get("symbols", []) if isinstance(item, dict)]
        write_first_sidecar(first_sidecar_path(output_dir, role, str(wrapper.get("task_name") or role)), symbols)
        for item in symbols:
            symbol_id = symbol_key(item)
            if symbol_id not in agent_scores:
                continue
            score = clamp_score(item.get("score"), 5)
            confidence = clamp_score(item.get("confidence"), 5)
            agent_scores[symbol_id].append(
                {
                    "agent_role": role,
                    "score": score,
                    "confidence": confidence,
                    "confidence_adjusted_score": confidence_adjusted_score(score, confidence),
                    "reason_code": safe_name(str(item.get("reason_code") or "hold_neutral")).lower(),
                    "one_line_reason": item.get("one_line_reason") or "",
                    "missing_data": item.get("missing_data") if isinstance(item.get("missing_data"), list) else [],
                }
            )

    artifact = common_envelope(decision_brief.get("run_id") or output_dir.name, decision_brief.get("started_at") or "", "verdict-first")
    artifact["errors"] = errors
    for symbol_id in symbol_ids:
        scores = agent_scores.get(symbol_id, [])
        seen_roles = {str(item.get("agent_role") or "") for item in scores}
        missing_roles = [role for role in FIRST_VERDICT_ROLES if role not in seen_roles]
        for role in missing_roles:
            artifact["errors"].append(
                {
                    "stage": "first-verdict",
                    "symbol_id": symbol_id,
                    "source": "merge-first",
                    "code": "missing_agent_score",
                    "message": f"{role} did not return a valid score for symbol",
                    "required": True,
                }
            )
        if not scores:
            continue
        mean_score = sum(item["score"] for item in scores) / len(scores)
        mean_adjusted = sum(item["confidence_adjusted_score"] for item in scores) / len(scores)
        brief_symbol = symbols_by_id.get(symbol_id, {})
        artifact["symbols"].append(
            {
                "symbol_id": symbol_id,
                "symbol_name": brief_symbol.get("symbol_name") or symbol_id,
                "agent_scores": scores,
                "mean_score": mean_score,
                "mean_confidence_adjusted_score": mean_adjusted,
                "final_first_score": round_half_up(mean_adjusted),
            }
        )
    artifact["status"] = "partial" if artifact["errors"] else "success"
    write_json(Path(args.output), artifact)
    return artifact


def build_second_spec(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    decision_brief = load_json(Path(args.decision_brief or output_dir / "decision-brief.json"))
    verdict_first = load_json(Path(args.verdict_first or output_dir / "verdict-first.json"))
    portfolio = read_json_arg(args.portfolio_json)
    eligible = set(eligible_symbol_ids(decision_brief))

    selected: list[str] = []
    for item in verdict_first.get("symbols", []):
        symbol_id = symbol_key(item)
        if symbol_id in eligible and as_int(item.get("final_first_score")) >= args.min_score:
            selected.append(symbol_id)
    for symbol_id in normalize_symbol_ids(portfolio.get("holding", [])):
        if symbol_id in eligible and symbol_id not in selected:
            selected.append(symbol_id)

    (output_dir / "second-verdict-symbols.txt").write_text("\n".join(selected) + ("\n" if selected else ""), encoding="utf-8")

    daily_skill_dir = Path(args.skill_dir).resolve()
    absolute_paths = not args.relative_paths
    payload = {
        "run_id": args.run_id or decision_brief.get("run_id") or output_dir.name,
        "started_at": args.started_at or decision_brief.get("started_at") or "",
        "stage": "second-verdict",
        "agent_role": "judge-final",
        "task_name": "second-judge-final",
        "workspace_dir": str(Path(args.workspace_dir).resolve()),
        "output_dir": str(output_dir),
        "artifact_paths": {
            "decision_brief": artifact_path(args.decision_brief or output_dir / "decision-brief.json", absolute_paths),
            "verdict_first": artifact_path(args.verdict_first or output_dir / "verdict-first.json", absolute_paths),
            "persona": artifact_path(daily_skill_dir / "references" / "personas" / "judge-final.md", absolute_paths),
            "verdict_format": artifact_path(daily_skill_dir / "references" / "rules" / "verdict-format.md", absolute_paths),
        },
        "symbol_ids": selected,
    }
    write_json(Path(args.output), payload)
    return payload


def active_quantities(account: dict[str, Any]) -> dict[str, dict[str, int]]:
    quantities: dict[str, dict[str, int]] = {}
    for item in account.get("active_orders", []):
        if not isinstance(item, dict) or item.get("active_status") != "active":
            continue
        symbol_id = symbol_key(item)
        if not symbol_id:
            continue
        bucket = quantities.setdefault(symbol_id, {"buy": 0, "sell": 0})
        direction = str(item.get("direction") or "")
        if direction in bucket:
            bucket[direction] += as_int(item.get("remaining_quantity"))
    return quantities


def build_execution_plan(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    verdict_second = load_json(Path(args.verdict_second or output_dir / "verdict-second.json"))
    account = load_json(Path(args.account_before_order or output_dir / "account-before-order.json"))
    decision_brief_path = Path(args.decision_brief) if args.decision_brief else output_dir / "decision-brief.json"
    decision_brief = load_json(decision_brief_path)
    account_by_symbol = indexed_symbols(account.get("symbols"))
    brief_by_symbol = indexed_symbols(decision_brief.get("symbols"))
    active = active_quantities(account)
    order_path = str(getattr(args, "order_path", "reservation") or "reservation")
    if order_path not in {"reservation", "immediate"}:
        raise ValueError(f"unsupported order_path: {order_path}")
    order_api = "order_cash" if order_path == "immediate" else "order_resv"

    run_id = args.run_id or verdict_second.get("run_id") or account.get("run_id") or output_dir.name
    started_at = args.started_at or verdict_second.get("started_at") or account.get("started_at") or ""
    artifact = common_envelope(run_id, started_at, "execution")
    artifact.update(
        {
            "request_type": args.request_type,
            "requires_main_agent_order_execution": False,
            "required_main_agent_actions": [],
            "latest_available_cash": None
            if account.get("order_available_lookup_performed") is not True
            else (account.get("account_summary") or {}).get("cash_amount"),
            "order_adjustments": [],
            "orders": [],
        }
    )

    gate_missing = account.get("active_order_lookup_performed") is not True or account.get("order_available_lookup_performed") is not True
    blocked_any = False
    refreshable_gate_blocked = False
    for item in verdict_second.get("symbols", []):
        if not isinstance(item, dict):
            continue
        symbol_id = symbol_key(item)
        account_item = account_by_symbol.get(symbol_id, {})
        brief_item = brief_by_symbol.get(symbol_id, {})
        active_item = active.get(symbol_id, {"buy": 0, "sell": 0})
        current_qty = as_int(account_item.get("current_live_holding_quantity"))
        buy_qty = active_item.get("buy", 0)
        sell_qty = active_item.get("sell", 0)
        expected_qty = current_qty + buy_qty - sell_qty
        target_qty = max(0, as_int(item.get("target_holding_quantity")))
        delta = target_qty - expected_qty
        order_price = (
            as_number(account_item.get("current_price"))
            or as_number((brief_item.get("price") or {}).get("current_or_last"))
            or 0
        )
        if delta > 0:
            direction = "buy"
        elif delta < 0:
            direction = "sell"
        else:
            direction = "none"

        result = "skipped"
        reason = "target_equals_expected_holding_quantity"
        if direction != "none":
            if args.request_type in {"demo-submit", "real-submit", "prepare"} and gate_missing:
                result = "blocked"
                reason = "active_order_or_order_available_gate_missing"
                blocked_any = True
                refreshable_gate_blocked = True
            elif args.request_type in {"demo-submit", "real-submit", "prepare"}:
                result = "skipped"
                reason = "ready_for_main_agent_submission"
            elif args.request_type == "analysis":
                result = "skipped"
                reason = "analysis_only_no_order_submission"
            else:
                result = "blocked"
                reason = "unsupported_request_type_for_deterministic_execution_plan"
                blocked_any = True

        artifact["orders"].append(
            {
                "symbol_id": symbol_id,
                "symbol_name": item.get("symbol_name") or account_item.get("symbol_name") or symbol_id,
                "direction": direction,
                "current_live_holding_quantity": current_qty,
                "pending_and_reserved_buy_quantity": buy_qty,
                "pending_and_reserved_sell_quantity": sell_qty,
                "expected_holding_quantity": expected_qty,
                "target_holding_quantity": target_qty,
                "additional_required_quantity": delta,
                "validated_order_quantity": abs(delta),
                "order_price": order_price,
                "order_path": order_path,
                "order_api": order_api,
                "result": result,
                "reason": reason,
                "order_or_reservation_id": "",
                "attempts": [],
            }
        )
    artifact["symbols"] = [item["symbol_id"] for item in artifact["orders"]]
    if args.request_type in {"demo-submit", "real-submit"} and any(item.get("direction") != "none" for item in artifact["orders"]):
        artifact["requires_main_agent_order_execution"] = True
        if refreshable_gate_blocked:
            artifact["required_main_agent_actions"] = [
                "refresh_active_order_lookup",
                "refresh_order_available_lookup",
                "continue_order_execution",
            ]
        elif not blocked_any:
            artifact["required_main_agent_actions"] = ["continue_order_execution"]
    if blocked_any:
        artifact["status"] = "partial"
        artifact["errors"].append(
            {
                "stage": "execution",
                "source": "build_run_artifacts",
                "code": "order_submission_blocked",
                "message": "Real/demo order candidates require latest active-order/order-available gates before Main-agent order execution; no order API submitted by the deterministic execution plan.",
                "required": True,
                "refreshable_by_main_agent": refreshable_gate_blocked,
            }
        )
    write_json(Path(args.output), artifact)
    return artifact


def zero_token_usage() -> dict[str, int]:
    return {field: 0 for field in TOKEN_USAGE_FIELDS}


def token_usage_from(raw: Any) -> dict[str, int]:
    usage = zero_token_usage()
    if not isinstance(raw, dict):
        return usage
    for field in TOKEN_USAGE_FIELDS:
        usage[field] = as_int(raw.get(field))
    if usage["total_tokens"] <= 0:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def add_token_usage(total: dict[str, int], usage: dict[str, int]) -> None:
    for field in TOKEN_USAGE_FIELDS:
        total[field] = as_int(total.get(field)) + as_int(usage.get(field))


def parse_main_events(path: Path | None) -> tuple[dict[str, int], int]:
    usage = zero_token_usage()
    event_count = 0
    if not path:
        return usage, event_count
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("type") == "token_count":
            info = item.get("info") if isinstance(item.get("info"), dict) else {}
            add_token_usage(usage, token_usage_from(info.get("last_token_usage")))
            event_count += 1
            continue
        if item.get("type") == "turn.completed" and isinstance(item.get("usage"), dict):
            add_token_usage(usage, token_usage_from(item.get("usage")))
            event_count += 1
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if item.get("type") == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            add_token_usage(usage, token_usage_from(info.get("last_token_usage")))
            event_count += 1
    return usage, event_count


def build_token_summary(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir)
    main_usage, main_event_count = parse_main_events(Path(args.main_events) if args.main_events else None)
    subagent_usage = zero_token_usage()
    wrapper_count = 0
    wrappers_with_usage = 0
    for wrapper_path in sorted((run_dir / "subagents").glob("*.wrapper.json")):
        wrapper_count += 1
        wrapper = load_json(wrapper_path)
        usage = token_usage_from(wrapper.get("token_usage"))
        if usage["total_tokens"] > 0 or wrapper.get("token_usage_event_count"):
            wrappers_with_usage += 1
        add_token_usage(subagent_usage, usage)
    total_usage = zero_token_usage()
    add_token_usage(total_usage, main_usage)
    add_token_usage(total_usage, subagent_usage)
    payload = {
        "schema_version": "1",
        "run_dir": str(run_dir),
        "main": {"token_usage": main_usage, "token_usage_event_count": main_event_count},
        "subagents": {
            "token_usage": subagent_usage,
            "wrapper_count": wrapper_count,
            "wrappers_with_usage": wrappers_with_usage,
        },
        "total": {"token_usage": total_usage},
    }
    write_json(Path(args.output), payload)
    return payload


def run_self_test() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        run_dir = tmp / "reports" / "runs" / "daily-trading-test"
        portfolio = {
            "recommanded": [],
            "specified": ["005930", "000660"],
            "holding": ["005930"],
            "universe": ["005930", "000660"],
        }
        write_json(tmp / "portfolio.json", portfolio)
        write_json(
            run_dir / "price-chart.json",
            {
                "run_id": "daily-trading-test",
                "started_at": "2026-06-18 09:00:00 KST",
                "symbols": [
                    {
                        "symbol_id": "005930",
                        "symbol_name": "삼성전자",
                        "product_type": "stock",
                        "eligible_for_verdict": True,
                        "price": {"current_or_last": 70000, "observed_at": "2026-06-18T09:00:00+09:00", "snapshot_mode": "live"},
                        "local_signals": [
                            {"name": "day_change_pct", "value": 1.2},
                            {"name": "daily_pct_vs_ma20", "value": 3.4},
                        ],
                        "charts": {
                            "daily": [{"date": "20260618", "open": 69000, "high": 71000, "low": 68000, "close": 70000, "volume": 1000}],
                            "weekly": [{"date": "20260614", "close": 69500, "volume": 5000}],
                            "monthly": [],
                        },
                        "intraday": [{"time": "093000", "price": 70000, "volume": 100}],
                        "orderbook_summary": {"best_bid": 69900, "best_ask": 70000, "spread_pct": 0.143},
                        "trade_flow_summary": {"tick_count": 3, "recent_price_change_pct": 0.2},
                        "investor_flow_summary": {"foreign_net_buy_quantity": 1000},
                        "required_missing": [],
                        "errors": [],
                    },
                    {
                        "symbol_id": "000660",
                        "symbol_name": "SK하이닉스",
                        "product_type": "stock",
                        "eligible_for_verdict": True,
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
                "run_id": "daily-trading-test",
                "started_at": "2026-06-18 09:00:00 KST",
                "active_order_lookup_performed": False,
                "order_available_lookup_performed": False,
                "account_summary": {"cash_amount": 1000000, "total_evaluation_amount": 1500000},
                "active_orders": [],
                "symbols": [
                    {"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 1, "current_price": 70000},
                    {"symbol_id": "000660", "symbol_name": "SK하이닉스", "current_live_holding_quantity": 0, "current_price": 200000},
                ],
            },
        )
        financial_cache_path = tmp / "memory" / "collect-financial-information" / "financial-2026-06-18.yaml"
        financial_cache_path.parent.mkdir(parents=True, exist_ok=True)
        financial_cache_path.write_text(
            '''date: "2026-06-18"
source: kis_open_api
symbols:
  "005930":
    삼성전자:
      주식현재가 시세:
        응답:
          - 현재가: "70000"
            전일 대비율: "1.2"
            주가수익비율(PER): "12.3"
            업종명: "전기전자"
''',
            encoding="utf-8",
        )
        news_cache_path = tmp / "memory" / "collect-news-information" / "news-2026-06-18.yaml"
        news_cache_path.parent.mkdir(parents=True, exist_ok=True)
        news_cache_path.write_text(
            '''date: "2026-06-18"
source: kis_open_api
symbols:
  "005930":
    articles:
      - article_date: ""
        sentiment: neutral
        content: "2026-06-18 기준 수집된 뉴스가 없습니다."
''',
            encoding="utf-8",
        )
        try:
            brief = build_decision_brief(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "decision-brief.json",
                    portfolio_json=str(tmp / "portfolio.json"),
                    price_chart=None,
                    account_before_order=None,
                    run_id=None,
                    started_at=None,
                    financial_cache_path=str(financial_cache_path),
                    news_cache_path=str(news_cache_path),
                )
            )
            if brief["status"] != "partial" or len(brief["symbols"]) != 2:
                failures.append(f"unexpected decision brief: {brief}")
            by_symbol = {item.get("symbol_id"): item for item in brief["symbols"]}
            if by_symbol["005930"].get("evidence_mode") != "full":
                failures.append(f"financial-covered symbol should be full: {by_symbol['005930']}")
            if by_symbol["000660"].get("evidence_mode") != "price_only":
                failures.append(f"financial-missing symbol should remain price_only: {by_symbol['000660']}")
            if by_symbol["000660"].get("financial_summary", {}).get("cache_status") != "missing_symbol":
                failures.append(f"financial-missing symbol should be marked missing_symbol: {by_symbol['000660']}")
            if by_symbol["005930"].get("news_summary"):
                failures.append(f"no-news placeholder should not be included: {by_symbol['005930']}")
            if not by_symbol["005930"].get("chart_context", {}).get("daily"):
                failures.append(f"chart context should be preserved: {by_symbol['005930']}")
            if by_symbol["005930"].get("orderbook_summary", {}).get("best_bid") != 69900:
                failures.append(f"orderbook summary should be preserved: {by_symbol['005930']}")
            if by_symbol["005930"].get("trade_flow_summary", {}).get("tick_count") != 3:
                failures.append(f"trade flow summary should be preserved: {by_symbol['005930']}")
            if by_symbol["005930"].get("investor_flow_summary", {}).get("foreign_net_buy_quantity") != 1000:
                failures.append(f"investor flow summary should be preserved: {by_symbol['005930']}")
            first_specs = build_first_specs(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "first-verdict-specs.json",
                    decision_brief=str(run_dir / "decision-brief.json"),
                    run_id=None,
                    started_at=None,
                    workspace_dir=tmp,
                    skill_dir=skill_dir(),
                    relative_paths=False,
                    symbol_ids="",
                )
            )
            if len(first_specs["specs"]) != 3 or not Path(first_specs["specs"][0]["artifact_paths"]["persona"]).is_absolute():
                failures.append(f"unexpected first specs: {first_specs}")
            subagent_dir = run_dir / "subagents"
            for role in FIRST_VERDICT_ROLES:
                write_json(
                    subagent_dir / f"first-{role}.wrapper.json",
                    {
                        "stage": "first-verdict",
                        "agent_role": role,
                        "task_name": f"first-{role}",
                        "status": "success",
                        "ended_at": "2026-06-18T00:00:00+00:00",
                        "parsed_json": {
                            "stage": "first-verdict",
                            "symbols": [
                                {
                                    "symbol_id": "005930",
                                    "symbol_name": "삼성전자",
                                    "score": 7,
                                    "confidence": 5,
                                    "reason_code": "buy_candidate",
                                    "one_line_reason": "test",
                                },
                                {
                                    "symbol_id": "000660",
                                    "symbol_name": "SK하이닉스",
                                    "score": 5,
                                    "confidence": 5,
                                    "reason_code": "hold_neutral",
                                    "one_line_reason": "test",
                                },
                            ],
                        },
                    },
                )
            verdict_first = build_verdict_first(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "verdict-first.json",
                    decision_brief=str(run_dir / "decision-brief.json"),
                    symbol_ids="",
                )
            )
            if verdict_first["symbols"][0]["final_first_score"] != 6:
                failures.append(f"unexpected first verdict score: {verdict_first}")
            missing_wrapper_path = subagent_dir / "first-analyst-risk-allocation.wrapper.json"
            missing_wrapper = load_json(missing_wrapper_path)
            missing_wrapper["parsed_json"]["symbols"] = missing_wrapper["parsed_json"]["symbols"][:1]
            write_json(missing_wrapper_path, missing_wrapper)
            missing_verdict_first = build_verdict_first(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "verdict-first-missing.json",
                    decision_brief=str(run_dir / "decision-brief.json"),
                    symbol_ids="",
                )
            )
            if missing_verdict_first["status"] != "partial" or not any(
                error.get("code") == "missing_agent_score" for error in missing_verdict_first["errors"]
            ):
                failures.append(f"missing persona score did not produce partial verdict: {missing_verdict_first}")
            second_spec = build_second_spec(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "second-verdict-spec.json",
                    decision_brief=str(run_dir / "decision-brief.json"),
                    verdict_first=str(run_dir / "verdict-first.json"),
                    portfolio_json=str(tmp / "portfolio.json"),
                    run_id=None,
                    started_at=None,
                    workspace_dir=tmp,
                    skill_dir=skill_dir(),
                    relative_paths=False,
                    min_score=7,
                )
            )
            if second_spec["symbol_ids"] != ["005930"]:
                failures.append(f"unexpected second spec symbols: {second_spec}")
            write_json(
                run_dir / "verdict-second.json",
                {
                    "run_id": "daily-trading-test",
                    "started_at": "2026-06-18 09:00:00 KST",
                    "symbols": [
                        {
                            "symbol_id": "005930",
                            "symbol_name": "삼성전자",
                            "target_holding_quantity": 2,
                            "relative_attractiveness_rank": 1,
                            "reason_code": "add",
                            "one_line_reason": "test",
                        }
                    ],
                },
            )
            execution = build_execution_plan(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "execution.json",
                    verdict_second=str(run_dir / "verdict-second.json"),
                    account_before_order=str(run_dir / "account-before-order.json"),
                    decision_brief=str(run_dir / "decision-brief.json"),
                    run_id=None,
                    started_at=None,
                    request_type="real-submit",
                )
            )
            if execution["orders"][0]["result"] != "blocked":
                failures.append(f"unexpected execution plan: {execution}")
            if execution["orders"][0].get("order_path") != "reservation" or execution["orders"][0].get("order_api") != "order_resv":
                failures.append(f"execution plan did not emit reservation order path/API: {execution['orders'][0]}")
            account_missing_gates = load_json(run_dir / "account-before-order.json")
            account_missing_gates.pop("active_order_lookup_performed", None)
            account_missing_gates.pop("order_available_lookup_performed", None)
            write_json(run_dir / "account-before-order-missing-gates.json", account_missing_gates)
            missing_gate_execution = build_execution_plan(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "execution-missing-gates.json",
                    verdict_second=str(run_dir / "verdict-second.json"),
                    account_before_order=str(run_dir / "account-before-order-missing-gates.json"),
                    decision_brief=str(run_dir / "decision-brief.json"),
                    run_id=None,
                    started_at=None,
                    request_type="real-submit",
                )
            )
            if missing_gate_execution["orders"][0]["result"] != "blocked":
                failures.append(f"missing account gate fields did not block execution: {missing_gate_execution}")
            if missing_gate_execution["orders"][0]["reason"] != "active_order_or_order_available_gate_missing":
                failures.append(f"missing account gate used unexpected reason: {missing_gate_execution['orders'][0]}")
            if "explicit limit price" in (missing_gate_execution.get("errors") or [{}])[0].get("message", ""):
                failures.append(f"missing account gate error still requires explicit limit price: {missing_gate_execution['errors']}")
            account_ready = load_json(run_dir / "account-before-order.json")
            account_ready["active_order_lookup_performed"] = True
            account_ready["order_available_lookup_performed"] = True
            write_json(run_dir / "account-before-order-ready.json", account_ready)
            ready_execution = build_execution_plan(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "execution-ready.json",
                    verdict_second=str(run_dir / "verdict-second.json"),
                    account_before_order=str(run_dir / "account-before-order-ready.json"),
                    decision_brief=str(run_dir / "decision-brief.json"),
                    run_id=None,
                    started_at=None,
                    request_type="real-submit",
                )
            )
            if ready_execution["orders"][0]["result"] != "skipped" or ready_execution["orders"][0]["reason"] != "ready_for_main_agent_submission":
                failures.append(f"gate-ready execution plan was not marked ready: {ready_execution}")
            account_default_brief = load_json(run_dir / "account-before-order-ready.json")
            account_default_brief["symbols"][0]["current_price"] = None
            write_json(run_dir / "account-before-order-default-brief.json", account_default_brief)
            default_brief_execution = build_execution_plan(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "execution-default-brief.json",
                    verdict_second=str(run_dir / "verdict-second.json"),
                    account_before_order=str(run_dir / "account-before-order-default-brief.json"),
                    decision_brief="",
                    run_id=None,
                    started_at=None,
                    request_type="real-submit",
                )
            )
            if default_brief_execution["orders"][0].get("order_price") != 70000:
                failures.append(
                    f"default decision-brief price fallback did not populate order_price: {default_brief_execution}"
                )
            write_json(subagent_dir / "token.wrapper.json", {"token_usage": {"input_tokens": 2, "output_tokens": 3}})
            events = tmp / "events.jsonl"
            events.write_text(
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
                + "\n"
                + json.dumps({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1, "output_tokens": 1}}})
                + "\n",
                encoding="utf-8",
            )
            token_summary = build_token_summary(
                argparse.Namespace(run_dir=run_dir, main_events=str(events), output=run_dir / "token-summary.json")
            )
            if token_summary["total"]["token_usage"]["total_tokens"] != 22:
                failures.append(f"unexpected token summary: {token_summary}")
        except Exception as exc:  # noqa: BLE001 - self-test reports all issues
            failures.append(str(exc))

    status = "failed" if failures else "passed"
    print(json.dumps({"status": status, "failures": failures}, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build deterministic daily-trading run artifacts.")
    subparsers = parser.add_subparsers(dest="command")

    decision = subparsers.add_parser("decision-brief", help="Build decision-brief.json.")
    decision.add_argument("--output-dir", type=Path, required=True)
    decision.add_argument("--portfolio-json", required=True, help="Path to check-portfolio JSON, or '-' for stdin.")
    decision.add_argument("--price-chart")
    decision.add_argument("--account-before-order")
    decision.add_argument("--financial-cache-path", default="")
    decision.add_argument("--news-cache-path", default="")
    decision.add_argument("--run-id")
    decision.add_argument("--started-at")
    decision.add_argument("--output", type=Path, default=None)

    first_specs = subparsers.add_parser("first-specs", help="Build first-verdict-specs.json.")
    first_specs.add_argument("--output-dir", type=Path, required=True)
    first_specs.add_argument("--decision-brief")
    first_specs.add_argument("--workspace-dir", default=".")
    first_specs.add_argument("--skill-dir", default=str(skill_dir()))
    first_specs.add_argument("--symbol-ids", default="")
    first_specs.add_argument("--run-id")
    first_specs.add_argument("--started-at")
    first_specs.add_argument("--relative-paths", action="store_true")
    first_specs.add_argument("--output", type=Path, default=None)

    merge_first = subparsers.add_parser("merge-first", help="Merge first-verdict wrappers into verdict-first.json.")
    merge_first.add_argument("--output-dir", type=Path, required=True)
    merge_first.add_argument("--decision-brief")
    merge_first.add_argument("--symbol-ids", default="")
    merge_first.add_argument("--output", type=Path, default=None)

    second_spec = subparsers.add_parser("second-spec", help="Build second-verdict symbols and spec.")
    second_spec.add_argument("--output-dir", type=Path, required=True)
    second_spec.add_argument("--portfolio-json", required=True)
    second_spec.add_argument("--decision-brief")
    second_spec.add_argument("--verdict-first")
    second_spec.add_argument("--workspace-dir", default=".")
    second_spec.add_argument("--skill-dir", default=str(skill_dir()))
    second_spec.add_argument("--min-score", type=int, default=7)
    second_spec.add_argument("--run-id")
    second_spec.add_argument("--started-at")
    second_spec.add_argument("--relative-paths", action="store_true")
    second_spec.add_argument("--output", type=Path, default=None)

    execution = subparsers.add_parser("execution-plan", help="Build non-submitting execution.json plan.")
    execution.add_argument("--output-dir", type=Path, required=True)
    execution.add_argument("--verdict-second")
    execution.add_argument("--account-before-order")
    execution.add_argument("--decision-brief", help="Path to decision-brief.json. Defaults to <output-dir>/decision-brief.json.")
    execution.add_argument("--request-type", choices=["analysis", "prepare", "demo-submit", "real-submit"], default="analysis")
    execution.add_argument("--order-path", choices=["reservation", "immediate"], default="reservation")
    execution.add_argument("--run-id")
    execution.add_argument("--started-at")
    execution.add_argument("--output", type=Path, default=None)

    token = subparsers.add_parser("token-summary", help="Build token summary from main events and wrappers.")
    token.add_argument("--run-dir", type=Path, required=True)
    token.add_argument("--main-events")
    token.add_argument("--output", type=Path, default=None)

    subparsers.add_parser("self-test", help="Run helper self-tests.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "decision-brief":
        if args.output is None:
            args.output = args.output_dir / "decision-brief.json"
        print(json.dumps(build_decision_brief(args), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "first-specs":
        if args.output is None:
            args.output = args.output_dir / "first-verdict-specs.json"
        print(json.dumps(build_first_specs(args), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "merge-first":
        if args.output is None:
            args.output = args.output_dir / "verdict-first.json"
        print(json.dumps(build_verdict_first(args), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "second-spec":
        if args.output is None:
            args.output = args.output_dir / "second-verdict-spec.json"
        print(json.dumps(build_second_spec(args), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "execution-plan":
        if args.output is None:
            args.output = args.output_dir / "execution.json"
        print(json.dumps(build_execution_plan(args), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "token-summary":
        if args.output is None:
            args.output = args.run_dir / "token-summary.json"
        print(json.dumps(build_token_summary(args), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "self-test":
        return run_self_test()
    raise SystemExit("a subcommand is required")


if __name__ == "__main__":
    raise SystemExit(main())
