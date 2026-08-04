#!/usr/bin/env python3
"""Build deterministic daily-trading run artifacts.

This helper keeps data shaping, spec generation, review merging, execution
planning, and token accounting out of the Main agent prompt path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ANALYST_REVIEW_ROLES = (
    "analyst-quality-value",
    "analyst-momentum-cycle",
    "analyst-risk-allocation",
    "analyst-news-flow",
)
ANALYST_REVIEW_SPEC_ROLES = (
    "analyst-quality-risk",
    "analyst-momentum-news",
)
COMBINED_ANALYST_REVIEW_ROLES = {
    "analyst-quality-risk": (
        "analyst-quality-value",
        "analyst-risk-allocation",
    ),
    "analyst-momentum-news": (
        "analyst-momentum-cycle",
        "analyst-news-flow",
    ),
}
TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
CHART_RECENT_ROW_LIMITS = {
    "daily": 5,
    "weekly": 4,
    "monthly": 4,
    "intraday": 5,
}
# Bumped when the judge-review contract changes shape. Version 3 removes the
# standalone debate input and requires a bounded opposing_view in Judge output,
# so cached version-2 specs cannot masquerade as the current contract.
REVIEW_CONTRACT_VERSION = 3
STRATEGY_POLICY_CONFIG_ENV = "DAILY_TRADING_STRATEGY_POLICY_CONFIG"
STRATEGY_POLICY_CONFIG_FILENAME = "daily-trading-strategy-policy.yaml"
STRATEGY_ADVISORY_LABELS = {
    "favor",
    "neutral",
    "discourage",
    "observe_first",
    "strong_review_required",
}
STRATEGY_REGIMES = {
    "insufficient_market_data",
    "neutral",
    "risk_on",
    "weak_downside",
    "panic_downside",
}
STRATEGY_BIAS_FIELDS = (
    "new_exposure_review_bias",
    "downside_add_review_bias",
    "index_drop_sell_review_bias",
)
KST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pipeline_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def default_strategy_policy_config_path() -> Path:
    return pipeline_dir().parents[2] / "profiles" / "base" / "config" / STRATEGY_POLICY_CONFIG_FILENAME


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def load_required_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - depends on runtime image
        raise RuntimeError(f"PyYAML is required to read {path}") from exc
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"failed to parse YAML: {path}") from exc


def resolve_strategy_policy_config_path(value: str | Path | None = None) -> Path:
    text = str(value or os.getenv(STRATEGY_POLICY_CONFIG_ENV, "")).strip()
    if text:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            raise FileNotFoundError(f"strategy policy config not found: {path}")
        return path.resolve()
    path = default_strategy_policy_config_path()
    if not path.exists():
        raise FileNotFoundError(f"default strategy policy config not found: {path}")
    return path.resolve()


def finite_float_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(parsed):
        return parsed
    return None


def required_finite_number(payload: dict[str, Any], key: str, source: Path) -> float:
    value = finite_float_value(payload.get(key))
    if value is None:
        raise ValueError(f"strategy policy {key} must be a finite number: {source}")
    return value


def validate_strategy_policy_config(payload: Any, source: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"strategy policy config must be an object: {source}")
    tracked = payload.get("tracked_indexes")
    if not isinstance(tracked, list) or not [str(item).strip() for item in tracked if str(item).strip()]:
        raise ValueError(f"strategy policy tracked_indexes must be a non-empty list: {source}")
    thresholds = payload.get("regime_thresholds")
    if not isinstance(thresholds, dict):
        raise ValueError(f"strategy policy regime_thresholds must be an object: {source}")
    normalized_thresholds = {
        "panic_downside_any_lte_pct": required_finite_number(thresholds, "panic_downside_any_lte_pct", source),
        "weak_downside_any_lte_pct": required_finite_number(thresholds, "weak_downside_any_lte_pct", source),
        "risk_on_all_gte_pct": required_finite_number(thresholds, "risk_on_all_gte_pct", source),
    }
    if normalized_thresholds["panic_downside_any_lte_pct"] > normalized_thresholds["weak_downside_any_lte_pct"]:
        raise ValueError(f"strategy policy panic threshold must be <= weak threshold: {source}")

    labels = payload.get("advisory_labels")
    if not isinstance(labels, dict):
        raise ValueError(f"strategy policy advisory_labels must be an object: {source}")
    normalized_labels = {key: str(labels.get(key) or "").strip() for key in STRATEGY_ADVISORY_LABELS}
    missing_labels = [key for key, value in normalized_labels.items() if not value]
    if missing_labels:
        raise ValueError(f"strategy policy advisory_labels missing: {', '.join(sorted(missing_labels))}")

    regime_bias = payload.get("regime_bias")
    if not isinstance(regime_bias, dict):
        raise ValueError(f"strategy policy regime_bias must be an object: {source}")
    normalized_bias: dict[str, dict[str, str]] = {}
    for regime in STRATEGY_REGIMES:
        entry = regime_bias.get(regime)
        if not isinstance(entry, dict):
            raise ValueError(f"strategy policy regime_bias.{regime} must be an object: {source}")
        normalized_entry: dict[str, str] = {}
        for field in STRATEGY_BIAS_FIELDS:
            label = str(entry.get(field) or "").strip()
            if label not in STRATEGY_ADVISORY_LABELS:
                raise ValueError(f"strategy policy regime_bias.{regime}.{field} has unsupported label: {label}")
            normalized_entry[field] = label
        normalized_entry["advisory_reason"] = str(entry.get("advisory_reason") or "").strip()
        normalized_bias[regime] = normalized_entry

    downside_add = payload.get("downside_add_review")
    if not isinstance(downside_add, dict):
        raise ValueError(f"strategy policy downside_add_review must be an object: {source}")
    downside_target = str(downside_add.get("target") or "").strip()
    if downside_target != "all_current_holdings":
        raise ValueError(f"strategy policy downside_add_review.target must be all_current_holdings: {source}")

    concentration = payload.get("concentration_levels") if isinstance(payload.get("concentration_levels"), dict) else {}
    low = finite_float_value(concentration.get("low_lte_pct"))
    moderate = finite_float_value(concentration.get("moderate_lte_pct"))
    if low is None or moderate is None or low < 0 or moderate < low:
        raise ValueError(f"strategy policy concentration_levels are invalid: {source}")

    top_k = payload.get("unheld_review_top_k")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 0:
        raise ValueError(f"strategy policy unheld_review_top_k must be a non-negative integer: {source}")

    return {
        "schema_version": str(payload.get("schema_version") or "1"),
        "tracked_indexes": [str(item).strip() for item in tracked if str(item).strip()],
        "regime_thresholds": normalized_thresholds,
        "advisory_labels": normalized_labels,
        "regime_bias": normalized_bias,
        "downside_add_review": {"target": downside_target},
        "concentration_levels": {
            "low_lte_pct": low,
            "moderate_lte_pct": moderate,
        },
        "unheld_review_top_k": top_k,
    }


def load_strategy_policy_config(path_value: str | Path | None = None) -> tuple[dict[str, Any], Path]:
    path = resolve_strategy_policy_config_path(path_value)
    return validate_strategy_policy_config(load_required_yaml(path), path), path


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
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


def non_negative_int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            parsed = int(text)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


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


def as_float(value: Any) -> float | None:
    number = as_number(value)
    if number is None:
        return None
    return float(number)


def round_float(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return ((current - previous) / previous) * 100


def review_score_value(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 10 else None


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
            "pending_and_reserved_buy_quantity": 0,
            "pending_and_reserved_sell_quantity": 0,
            "expected_holding_quantity": 0,
            "holding_state_status": "unconfirmed",
            "holding_state_reasons": ["account_symbol_missing"],
            "valuation_amount": 0,
            "pnl_amount": 0,
            "pnl_rate": None,
        }
    current = as_int(item.get("current_live_holding_quantity"))
    pending_buy = as_int(item.get("pending_and_reserved_buy_quantity"))
    pending_sell = as_int(item.get("pending_and_reserved_sell_quantity"))
    return {
        "current_live_holding_quantity": current,
        "pending_and_reserved_buy_quantity": pending_buy,
        "pending_and_reserved_sell_quantity": pending_sell,
        "expected_holding_quantity": current + pending_buy - pending_sell,
        "holding_state_status": item.get("holding_state_status") or "",
        "holding_state_reasons": list(item.get("holding_state_reasons") or []),
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


def compact_market_index_snapshot(path: str | None) -> dict[str, Any]:
    if not path:
        return {"status": "missing", "indexes": [], "warnings": ["market_index_snapshot_not_supplied"], "errors": []}
    payload = load_json(Path(path))
    indexes: list[dict[str, Any]] = []
    for item in payload.get("indexes", []):
        if not isinstance(item, dict):
            continue
        indexes.append(
            {
                "symbol": item.get("symbol") or "",
                "name": item.get("name") or "",
                "source": item.get("source") or "",
                "status": item.get("status") or "",
                "value": as_number(item.get("value")),
                "change_percent": as_number(item.get("change_percent")),
                "observed_at": item.get("observed_at") or "",
                "market_status": item.get("market_status") or "",
            }
        )
    return {
        "schema_version": payload.get("schema_version") or "1",
        "status": payload.get("status") or "unknown",
        "generated_at": payload.get("generated_at") or "",
        "indexes": indexes[:5],
        "warnings": list(payload.get("warnings") or [])[:5],
        "errors": list(payload.get("errors") or [])[:5],
    }


def compact_market_news_context(path: str | None) -> dict[str, Any]:
    if not path:
        return {
            "status": "missing",
            "window_start": "",
            "window_end": "",
            "window_source": "",
            "selected_count": 0,
            "items": [],
        }
    payload = load_json(Path(path))
    market_news = payload.get("market_news") if isinstance(payload.get("market_news"), dict) else {}
    items: list[dict[str, Any]] = []
    for item in market_news.get("items", []) if isinstance(market_news.get("items"), list) else []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[:500]
        if not title:
            continue
        items.append(
            {
                "title": title,
                "published_at": item.get("published_at") or "",
                "collected_at": item.get("collected_at") or "",
                "url": item.get("url") or "",
                "domain": item.get("domain") or "",
                "source_country": item.get("source_country") or "",
                "source_language": item.get("source_language") or "",
                "source_ids": list(item.get("source_ids") or [])[:5],
                "providers": list(item.get("providers") or [])[:5],
                "classifications": list(item.get("classifications") or [])[:5],
            }
        )
    source_statuses: dict[str, Any] = {}
    for source_id, raw_status in (
        market_news.get("source_statuses", {}).items()
        if isinstance(market_news.get("source_statuses"), dict)
        else []
    ):
        status = raw_status if isinstance(raw_status, dict) else {}
        source_statuses[str(source_id)] = {
            "status": status.get("status") or "unknown",
            "window_start": status.get("window_start") or "",
            "window_end": status.get("window_end") or "",
            "error": str(status.get("error") or "")[:300],
        }
    return {
        "schema_version": payload.get("schema_version") or "1",
        "status": market_news.get("status") or "missing",
        "context_status": payload.get("status") or "unknown",
        "window_start": payload.get("window_start") or "",
        "window_end": payload.get("window_end") or "",
        "window_source": payload.get("window_source") or "",
        "deduplicated_count": as_int(payload.get("deduplicated_count")),
        "raw_count": as_int(market_news.get("raw_count")),
        "selected_count": len(items),
        "source_statuses": source_statuses,
        "items": items[:30],
    }


def tracked_index_changes(
    market_index_snapshot: dict[str, Any],
    tracked_indexes: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    wanted = {str(item).strip().upper() for item in tracked_indexes if str(item).strip()}
    found: dict[str, dict[str, Any]] = {}
    for item in market_index_snapshot.get("indexes", []) if isinstance(market_index_snapshot, dict) else []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol not in wanted:
            continue
        change = finite_float_value(item.get("change_percent"))
        if change is None:
            continue
        found[symbol] = {
            "symbol": symbol,
            "change_percent": round_float(change),
            "status": item.get("status") or "",
            "observed_at": item.get("observed_at") or "",
            "market_status": item.get("market_status") or "",
        }
    ordered = [found[str(symbol).strip().upper()] for symbol in tracked_indexes if str(symbol).strip().upper() in found]
    missing = [str(symbol).strip().upper() for symbol in tracked_indexes if str(symbol).strip().upper() not in found]
    return ordered, missing


def strategy_regime(policy: dict[str, Any], tracked_changes: list[dict[str, Any]], missing: list[str]) -> str:
    if not tracked_changes:
        return "insufficient_market_data"
    thresholds = policy.get("regime_thresholds") if isinstance(policy.get("regime_thresholds"), dict) else {}
    panic = float(thresholds.get("panic_downside_any_lte_pct"))
    weak = float(thresholds.get("weak_downside_any_lte_pct"))
    risk_on = float(thresholds.get("risk_on_all_gte_pct"))
    changes = [
        float(item["change_percent"])
        for item in tracked_changes
        if finite_float_value(item.get("change_percent")) is not None
    ]
    if any(change <= panic for change in changes):
        return "panic_downside"
    if any(change <= weak for change in changes):
        return "weak_downside"
    if not missing and len(changes) == len(policy.get("tracked_indexes", [])) and all(change >= risk_on for change in changes):
        return "risk_on"
    return "neutral"


def build_strategy_context(
    policy: dict[str, Any],
    policy_path: Path,
    market_index_snapshot: dict[str, Any],
) -> dict[str, Any]:
    tracked, missing = tracked_index_changes(market_index_snapshot, list(policy.get("tracked_indexes") or []))
    regime = strategy_regime(policy, tracked, missing)
    bias = dict((policy.get("regime_bias") or {}).get(regime) or {})
    return {
        "schema_version": "1",
        "policy_source": {
            "path": str(policy_path),
            "sha256": file_sha256(policy_path),
            "schema_version": policy.get("schema_version") or "1",
        },
        "advisory_semantics": "strategy_context and symbol_strategy_context are advisory inputs for target_position_value_krw judgment, not order allow/block rules.",
        "regime": regime,
        "tracked_indexes": tracked,
        "missing_tracked_indexes": missing,
        "partial_missing_index_policy": "downside regimes use available tracked indexes; risk_on requires all configured tracked indexes to be usable.",
        "new_exposure_review_bias": bias.get("new_exposure_review_bias") or "neutral",
        "downside_add_review_bias": bias.get("downside_add_review_bias") or "neutral",
        "index_drop_sell_review_bias": bias.get("index_drop_sell_review_bias") or "neutral",
        "advisory_reason": bias.get("advisory_reason") or "",
        "advisory_labels": policy.get("advisory_labels") or {},
    }


def concentration_context(
    valuation_amount: Any,
    total_evaluation_amount: Any,
    policy: dict[str, Any],
) -> dict[str, Any]:
    valuation = finite_float_value(valuation_amount)
    total = finite_float_value(total_evaluation_amount)
    if valuation is None or total is None or total <= 0:
        return {}
    pct = (valuation / total) * 100
    levels = policy.get("concentration_levels") if isinstance(policy.get("concentration_levels"), dict) else {}
    low = finite_float_value(levels.get("low_lte_pct"))
    moderate = finite_float_value(levels.get("moderate_lte_pct"))
    if low is None or moderate is None:
        return {"concentration_pct": round_float(pct)}
    if pct <= low:
        level = "low"
    elif pct <= moderate:
        level = "moderate"
    else:
        level = "high"
    return {"concentration_pct": round_float(pct), "concentration_level": level}


def build_symbol_strategy_context(
    policy: dict[str, Any],
    strategy_context: dict[str, Any],
    account_exposure: dict[str, Any],
    account_exposure_summary: dict[str, Any],
) -> dict[str, Any]:
    holding_quantity = as_int(account_exposure.get("current_live_holding_quantity"))
    current_holding = holding_quantity > 0
    downside_regime = strategy_context.get("regime") in {"weak_downside", "panic_downside"}
    downside_target = ((policy.get("downside_add_review") or {}).get("target") == "all_current_holdings")
    context: dict[str, Any] = {
        "current_holding": current_holding,
        "current_live_holding_quantity": holding_quantity,
        "downside_add_review_target": bool(current_holding and downside_regime and downside_target),
        "downside_add_review_scope": (policy.get("downside_add_review") or {}).get("target") or "",
        "advisory_semantics": "review target is advisory context for judge target exposure, not an order allow/block rule.",
    }
    pnl_rate = finite_float_value(account_exposure.get("pnl_rate"))
    if pnl_rate is not None:
        context["pnl_rate"] = round_float(pnl_rate)
        context["loss_position"] = pnl_rate < 0
    context.update(
        concentration_context(
            account_exposure.get("valuation_amount"),
            account_exposure_summary.get("total_evaluation_amount"),
            policy,
        )
    )
    return context


def fills_by_symbol(today_fills: Any) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    fills = today_fills.get("fills") if isinstance(today_fills, dict) else []
    if not isinstance(fills, list):
        return result
    for item in fills:
        if not isinstance(item, dict):
            continue
        symbol_id = symbol_key(item)
        direction = str(item.get("direction") or "").lower()
        quantity = as_int(item.get("filled_quantity"))
        if not symbol_id or direction not in {"buy", "sell"} or quantity <= 0:
            continue
        compact = {
            "filled_at": str(item.get("filled_at") or ""),
            "direction": direction,
            "quantity": quantity,
            "price": as_int(item.get("filled_price")),
            "amount": as_int(item.get("filled_amount")),
            "order_id": str(item.get("order_id") or ""),
            "source_actor": str(item.get("source_actor") or ""),
        }
        result.setdefault(symbol_id, []).append(compact)
    for rows in result.values():
        rows.sort(key=lambda row: (str(row.get("filled_at") or ""), str(row.get("order_id") or "")))
    return result


def weighted_average_price(fills: list[dict[str, Any]], direction: str) -> float | None:
    qty = sum(as_int(item.get("quantity")) for item in fills if item.get("direction") == direction and as_int(item.get("price")) > 0)
    if qty <= 0:
        return None
    amount = sum(as_int(item.get("quantity")) * as_int(item.get("price")) for item in fills if item.get("direction") == direction and as_int(item.get("price")) > 0)
    return round(amount / qty, 2)


def today_trade_collection_context(today_fills: Any, *, artifact_exists: bool, symbol_id: str) -> dict[str, Any]:
    base = {
        "artifact_status": str(today_fills.get("status") or "") if isinstance(today_fills, dict) else "",
        "collection_error_count": len(today_fills.get("errors") or []) if isinstance(today_fills, dict) and isinstance(today_fills.get("errors"), list) else 0,
    }
    if not artifact_exists:
        return dict(base, collection_status="unavailable", collection_reason="today_fills_artifact_missing")
    if not isinstance(today_fills, dict) or today_fills.get("stage") != "today-fills":
        return dict(base, collection_status="unavailable", collection_reason="today_fills_artifact_invalid")
    if today_fills.get("skipped") is True:
        return dict(base, collection_status="unavailable", collection_reason="today_fills_collection_skipped")

    status = str(today_fills.get("status") or "").lower()
    errors = today_fills.get("errors") if isinstance(today_fills.get("errors"), list) else []
    covered_symbols = {
        symbol_key(item)
        for item in today_fills.get("symbols", [])
        if isinstance(item, dict) and symbol_key(item)
    } if isinstance(today_fills.get("symbols"), list) else set()
    if status == "success":
        if errors:
            return dict(base, collection_status="partial", collection_reason="today_fills_collection_incomplete")
        if symbol_id in covered_symbols:
            return dict(base, collection_status="complete", collection_reason="")
        return dict(base, collection_status="unavailable", collection_reason="symbol_not_covered")
    return dict(base, collection_status="unavailable", collection_reason="today_fills_collection_failed")


def today_trade_context(
    fills: list[dict[str, Any]],
    current_price: Any,
    collection_context: dict[str, Any],
) -> dict[str, Any]:
    collection_status = str(collection_context.get("collection_status") or "unavailable")
    if not fills:
        return {
            **collection_context,
            "has_same_day_trade": False if collection_status == "complete" else None,
            "has_same_day_buy": False if collection_status == "complete" else None,
            "fills": [],
            "policy": "has_same_day_trade=false confirms no fills only when collection_status=complete; null means the same-day history is unknown.",
        }
    buy_qty = sum(as_int(item.get("quantity")) for item in fills if item.get("direction") == "buy")
    sell_qty = sum(as_int(item.get("quantity")) for item in fills if item.get("direction") == "sell")
    buy_fills = [item for item in fills if item.get("direction") == "buy"]
    sell_fills = [item for item in fills if item.get("direction") == "sell"]

    def actor_net_quantity(actor: str) -> int:
        signed = 0
        for item in fills:
            if str(item.get("source_actor") or "") != actor:
                continue
            if item.get("direction") == "buy":
                signed += as_int(item.get("quantity"))
            elif item.get("direction") == "sell":
                signed -= as_int(item.get("quantity"))
        return signed

    last = fills[-1]
    first_direction = str(fills[0].get("direction") or "")
    last_direction = str(last.get("direction") or "")
    price = as_number(current_price)
    last_price = as_number(last.get("price"))
    move_since_last = None
    if price is not None and last_price not in (None, 0):
        move_since_last = round(((float(price) - float(last_price)) / float(last_price)) * 100, 2)
    return {
        **collection_context,
        "has_same_day_trade": True,
        "has_same_day_buy": True if buy_qty > 0 else False if collection_status == "complete" else None,
        "fill_count": len(fills),
        "buy_quantity": buy_qty,
        "sell_quantity": sell_qty,
        "buy_fill_count": len(buy_fills),
        "sell_fill_count": len(sell_fills),
        "net_quantity": buy_qty - sell_qty,
        "bot_net_quantity": actor_net_quantity("bot_opnapi"),
        "manual_net_quantity": actor_net_quantity("non_bot_user"),
        "manual_fill_count": len([item for item in fills if str(item.get("source_actor") or "") == "non_bot_user"]),
        "first_direction": first_direction,
        "last_direction": last_direction,
        "last_buy_fill": buy_fills[-1] if buy_fills else {},
        "last_sell_fill": sell_fills[-1] if sell_fills else {},
        "last_fill_at": last.get("filled_at") or "",
        "last_fill_price": as_int(last.get("price")),
        "average_buy_price": weighted_average_price(fills, "buy"),
        "average_sell_price": weighted_average_price(fills, "sell"),
        "current_or_last_price": price,
        "move_since_last_fill_pct": move_since_last,
        "has_intraday_reversal": bool(buy_qty > 0 and sell_qty > 0),
        "fills": fills[:20],
        "policy": "Use confirmed fills as trade-history context; collection_status controls whether missing fill directions are confirmed absent or unknown. Do not treat fills as proof that the current command caused them.",
    }


def shorten(value: Any, limit: int = 160) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def first_present_from(sources: tuple[dict[str, Any], ...], keys: tuple[str, ...]) -> Any:
    for source in sources:
        value = first_present(source, keys)
        if value not in (None, ""):
            return value
    return None


FINANCIAL_SYMBOL_PAYLOAD_MARKER_KEYS = (
    "주식현재가 시세",
    "국내주식 종목추정실적",
    "국내주식 종목투자의견",
    "ETF/ETN 현재가",
    "NAV 비교추이(종목)",
)


def unwrap_financial_symbol_payload(symbol_payload: Any) -> dict[str, Any] | None:
    if not isinstance(symbol_payload, dict):
        return None
    if any(key in symbol_payload for key in FINANCIAL_SYMBOL_PAYLOAD_MARKER_KEYS):
        return symbol_payload
    for nested in symbol_payload.values():
        if isinstance(nested, dict) and any(key in nested for key in FINANCIAL_SYMBOL_PAYLOAD_MARKER_KEYS):
            return nested
    return symbol_payload


def is_no_news_content(value: Any) -> bool:
    text = str(value or "").strip()
    return not text or "수집된 뉴스가 없습니다" in text


def financial_summary_for(cache: Any, symbol_id: str, cache_path: str, fresh_current_price: Any = None) -> dict[str, Any]:
    summary = {
        "cache_path": cache_path or "",
        "cache_status": "supplied" if cache_path else "missing",
        "items": [],
        "quality_value_usable": False,
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
    target_rows = ((symbol_payload.get("국내주식 종목투자의견") or {}).get("응답") or [])
    targets = []
    broker_opinions = []
    for row in target_rows:
        if not isinstance(row, dict):
            continue
        broker_opinion = str(row.get("투자의견") or "").strip()
        if broker_opinion:
            broker_opinions.append(
                {
                    "date": str(row.get("주식 영업일자") or "").strip(),
                    "broker": str(row.get("증권사명") or "").strip(),
                    "opinion": broker_opinion,
                }
            )
        value = as_int(str(row.get("목표가") or "").replace(",", "").strip() or 0)
        if value <= 0:
            continue
        targets.append(
            {
                "value": value,
                "date": str(row.get("주식 영업일자") or "").strip(),
                "broker": str(row.get("증권사명") or "").strip(),
                "opinion": str(row.get("투자의견") or "").strip(),
            }
        )
    targets.sort(key=lambda entry: entry["date"], reverse=True)
    broker_opinions.sort(key=lambda entry: entry["date"], reverse=True)
    quote_parts = []
    for label, key in (("PER", "주가수익비율(PER)"), ("PBR", "주가순자산비율(PBR)")):
        if price.get(key) not in (None, ""):
            quote_parts.append(f"{label} {price.get(key)}")
            summary["quality_value_usable"] = True
    if quote_parts:
        summary["items"].append(", ".join(quote_parts))
    if targets:
        summary["quality_value_usable"] = True
        latest = targets[0]
        latest_source = ", ".join(part for part in (latest["broker"], latest["opinion"], latest["date"]) if part)
        fresh_price = finite_float_value(fresh_current_price)
        current_price = fresh_price if fresh_price is not None and fresh_price > 0 else 0

        def gap_text(reference_value: int) -> str:
            if current_price <= 0 or reference_value <= 0:
                return ""
            return f"현재가대비 괴리율 {(current_price - reference_value) / reference_value * 100:.1f}%"

        if len(targets) == 1:
            single_parts = [part for part in (latest_source, gap_text(latest["value"])) if part]
            item = f"목표가 {latest['value']}"
            if single_parts:
                item += f" ({', '.join(single_parts)})"
        else:
            values = sorted(entry["value"] for entry in targets)
            mid = len(values) // 2
            median_value = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) // 2
            gap = gap_text(median_value)
            dates = [entry["date"] for entry in targets if entry["date"]]
            date_span = f"(발표 {min(dates)}~{max(dates)})" if dates else ""
            item = f"목표가 컨센서스 {len(targets)}건{date_span} 중앙값 {median_value}"
            if gap:
                item += f"({gap})"
            item += f", 범위 {values[0]}~{values[-1]}, 최신 {latest['value']}"
            if latest_source:
                item += f" ({latest_source})"
        summary["items"].append(item)
    recommendation = str(opinion.get("추천의견") or "").strip()
    recommendation_source = ""
    if not recommendation and broker_opinions:
        latest_opinion = broker_opinions[0]
        recommendation = latest_opinion["opinion"]
        recommendation_source = ", ".join(
            part for part in (latest_opinion["broker"], latest_opinion["date"]) if part
        )
    if recommendation:
        item = f"최신 투자의견 {recommendation}"
        if recommendation_source:
            item += f" ({recommendation_source})"
        summary["items"].append(item)
        summary["quality_value_usable"] = True
    if price.get("업종명"):
        summary["items"].append(f"업종 {price.get('업종명')}")
    summary["items"] = summary["items"][:4]
    if not summary["items"]:
        summary["cache_status"] = "supplied_empty"
    return summary


def etf_summary_for(cache: Any, symbol_id: str, cache_path: str) -> dict[str, Any]:
    summary = {
        "cache_path": cache_path or "",
        "cache_status": "supplied" if cache_path else "missing",
        "items": [],
        "quality_value_usable": False,
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
        value = first_present_from((etf_price, nav_summary, nav_trend), keys)
        if value not in (None, ""):
            parts.append(f"{label} {value}")
            if label in {"NAV", "괴리율", "추적오차"}:
                summary["quality_value_usable"] = True
    if parts:
        summary["items"].append(", ".join(parts[:4]))
    for label, keys in (("NAV 전일대비율", ("NAV 전일 대비율", "nav_prdy_ctrt")), ("전일대비율", ("전일 대비율", "prdy_ctrt"))):
        value = first_present_from((nav_summary, nav_trend, etf_price), keys)
        if value not in (None, ""):
            summary["items"].append(f"{label} {value}")
    summary["items"] = summary["items"][:3]
    if not summary["items"]:
        summary["cache_status"] = "supplied_empty"
    return summary


def compact_summary_is_usable(summary: Any) -> bool:
    if not isinstance(summary, dict) or summary.get("cache_status") != "supplied":
        return False
    items = summary.get("items")
    return (
        summary.get("quality_value_usable") is True
        and isinstance(items, list)
        and any(str(item or "").strip() for item in items)
    )


def normalized_calendar_date(value: Any) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())[:8]
    if len(digits) != 8:
        return ""
    try:
        return datetime.strptime(digits, "%Y%m%d").date().isoformat()
    except ValueError:
        return ""


def expected_news_calendar_date(expected_date: Any, started_at: Any) -> str:
    if str(expected_date or "").strip():
        return normalized_calendar_date(expected_date)
    text = str(started_at or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return normalized_calendar_date(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(KST)
    return parsed.date().isoformat()


def symbol_news_summary_for(cache: Any, symbol_id: str, cache_path: str, expected_date: str) -> list[dict[str, Any]]:
    if not cache_path or not isinstance(cache, dict):
        return []
    expected_calendar_date = normalized_calendar_date(expected_date)
    cache_date = normalized_calendar_date(cache.get("date"))
    if not expected_calendar_date or cache_date != expected_calendar_date:
        return []
    symbols = cache.get("symbols") if isinstance(cache.get("symbols"), dict) else cache
    entries = symbols.get(symbol_id) if isinstance(symbols, dict) else None
    if isinstance(entries, dict):
        entries = entries.get("items") or entries.get("articles") or []
    if not isinstance(entries, list):
        return []
    result: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        article_date = item.get("article_date") or item.get("date") or ""
        if normalized_calendar_date(article_date) != expected_calendar_date:
            continue
        content = shorten(item.get("content") or item.get("text") or item.get("title") or "")
        if is_no_news_content(content):
            continue
        result.append(
            {
                "article_date": article_date,
                "content": content,
            }
        )
        if len(result) >= 3:
            break
    return result


def chart_row_value(row: dict[str, Any], key: str) -> float | None:
    return as_float(row.get(key)) if isinstance(row, dict) else None


def compact_ohlcv_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    keys = ("date", "open", "high", "low", "close", "volume", "trading_value")
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        compact = {key: row.get(key) for key in keys if row.get(key) not in (None, "")}
        if compact:
            result.append(compact)
    return result


def compact_intraday_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    keys = ("time", "price", "volume")
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        compact = {key: row.get(key) for key in keys if row.get(key) not in (None, "")}
        if compact:
            result.append(compact)
    return result


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return average(values[:window])


def chart_series_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid_rows = [row for row in rows if isinstance(row, dict)]
    if not valid_rows:
        return {}
    latest = valid_rows[0]
    closes = [value for row in valid_rows if (value := chart_row_value(row, "close")) is not None]
    highs = [value for row in valid_rows if (value := chart_row_value(row, "high")) is not None]
    lows = [value for row in valid_rows if (value := chart_row_value(row, "low")) is not None]
    volumes = [value for row in valid_rows if (value := chart_row_value(row, "volume")) is not None]
    latest_close = chart_row_value(latest, "close")
    previous_close = closes[1] if len(closes) > 1 else None
    range_high = max(highs) if highs else (max(closes) if closes else None)
    range_low = min(lows) if lows else (min(closes) if closes else None)
    range_position_pct = None
    if latest_close is not None and range_high is not None and range_low is not None and range_high != range_low:
        range_position_pct = ((latest_close - range_low) / (range_high - range_low)) * 100
    ma5 = moving_average(closes, 5)
    ma20 = moving_average(closes, 20)
    latest_volume = chart_row_value(latest, "volume")
    avg_volume = average(volumes[1:]) if len(volumes) > 1 else None
    summary = {
        "latest_date": latest.get("date") or "",
        "latest_close": as_number(latest.get("close")),
        "latest_open": as_number(latest.get("open")),
        "latest_high": as_number(latest.get("high")),
        "latest_low": as_number(latest.get("low")),
        "change_1_period_pct": round_float(pct_change(latest_close, previous_close)),
        "change_5_period_pct": round_float(pct_change(latest_close, closes[5] if len(closes) > 5 else None)),
        "change_20_period_pct": round_float(pct_change(latest_close, closes[20] if len(closes) > 20 else None)),
        "range_high": round_float(range_high, 2),
        "range_low": round_float(range_low, 2),
        "range_position_pct": round_float(range_position_pct),
        "ma5": round_float(ma5, 2),
        "ma20": round_float(ma20, 2),
        "distance_ma5_pct": round_float(pct_change(latest_close, ma5)),
        "distance_ma20_pct": round_float(pct_change(latest_close, ma20)),
        "latest_volume": as_number(latest.get("volume")),
        "avg_volume_ex_latest": round_float(avg_volume, 2),
        "volume_vs_avg_pct": round_float(pct_change(latest_volume, avg_volume)),
        "sample_count": len(valid_rows),
    }
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def intraday_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid_rows = [row for row in rows if isinstance(row, dict)]
    if not valid_rows:
        return {}
    latest = valid_rows[0]
    prices = [value for row in valid_rows if (value := chart_row_value(row, "price")) is not None]
    volumes = [value for row in valid_rows if (value := chart_row_value(row, "volume")) is not None]
    latest_price = chart_row_value(latest, "price")
    oldest_price = prices[-1] if prices else None
    summary = {
        "latest_time": latest.get("time") or "",
        "latest_price": as_number(latest.get("price")),
        "change_observed_pct": round_float(pct_change(latest_price, oldest_price)),
        "observed_high": round_float(max(prices), 2) if prices else None,
        "observed_low": round_float(min(prices), 2) if prices else None,
        "latest_volume": as_number(latest.get("volume")),
        "total_observed_volume": round_float(sum(volumes), 2) if volumes else None,
        "sample_count": len(valid_rows),
    }
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def compact_chart_context(item: dict[str, Any]) -> dict[str, Any]:
    charts = item.get("charts") if isinstance(item.get("charts"), dict) else {}
    result: dict[str, Any] = {}
    for key in ("daily", "weekly", "monthly"):
        rows = charts.get(key) if isinstance(charts, dict) else []
        if not isinstance(rows, list):
            rows = []
        compact_rows = [row for row in rows if isinstance(row, dict)]
        if compact_rows:
            result[f"{key}_summary"] = chart_series_summary(compact_rows)
            result[f"recent_{key}"] = compact_ohlcv_rows(compact_rows, CHART_RECENT_ROW_LIMITS[key])
    intraday = item.get("intraday") if isinstance(item.get("intraday"), list) else []
    compact_intraday = [row for row in intraday if isinstance(row, dict)]
    if compact_intraday:
        result["intraday_summary"] = intraday_summary(compact_intraday)
        result["recent_intraday"] = compact_intraday_rows(compact_intraday, CHART_RECENT_ROW_LIMITS["intraday"])
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
    today_fills_path = Path(args.today_fills or output_dir / "today-fills.json")
    today_fills = load_json(today_fills_path) if today_fills_path.exists() else {}
    fills_by_id = fills_by_symbol(today_fills)
    run_id = args.run_id or price_chart.get("run_id") or account.get("run_id") or output_dir.name
    started_at = args.started_at or price_chart.get("started_at") or account.get("started_at") or ""
    expected_symbol_news_date = expected_news_calendar_date(getattr(args, "expected_symbol_news_date", ""), started_at)

    account_by_symbol = indexed_symbols(account.get("symbols"))
    source_artifacts = ["price-chart.json", "account-before-order.json", "check-portfolio JSON"]
    if today_fills_path.exists():
        source_artifacts.append(str(today_fills_path))
    if args.financial_cache_path:
        source_artifacts.append(args.financial_cache_path)
    if args.symbol_news_cache_path:
        source_artifacts.append(args.symbol_news_cache_path)
    news_context_json = getattr(args, "news_context_json", "")
    if news_context_json:
        source_artifacts.append(news_context_json)
    if args.market_index_snapshot_json:
        source_artifacts.append(args.market_index_snapshot_json)

    financial_cache = load_yaml(Path(args.financial_cache_path)) if args.financial_cache_path else None
    symbol_news_cache = load_yaml(Path(args.symbol_news_cache_path)) if args.symbol_news_cache_path else None
    strategy_policy, strategy_policy_path = load_strategy_policy_config(
        getattr(args, "strategy_policy_config", "")
    )
    market_index_snapshot = compact_market_index_snapshot(args.market_index_snapshot_json)
    market_news_context = compact_market_news_context(news_context_json)
    account_exposure_summary = account_summary(account)
    strategy_context = build_strategy_context(strategy_policy, strategy_policy_path, market_index_snapshot)

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
            "market_index_snapshot": market_index_snapshot,
            "market_news_context": market_news_context,
            "account_exposure_summary": account_exposure_summary,
            "strategy_context": strategy_context,
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
        eligible = bool(item.get("eligible_for_review", True)) and usable_price and not required_missing
        if not usable_price and "price.current_or_last/observed_at" not in required_missing:
            required_missing.append("price.current_or_last/observed_at")
        product_type = str(item.get("product_type") or "stock").lower()
        financial_summary = financial_summary_for(
            financial_cache,
            symbol_id,
            args.financial_cache_path,
            price.get("current_or_last") if usable_price else None,
        )
        etf_summary = etf_summary_for(financial_cache, symbol_id, args.financial_cache_path) if product_type in {"etf", "etn"} else {}
        quality_value_summary = etf_summary if product_type in {"etf", "etn"} else financial_summary
        same_day_collection = today_trade_collection_context(
            today_fills,
            artifact_exists=today_fills_path.exists(),
            symbol_id=symbol_id,
        )
        same_day_context = today_trade_context(
            fills_by_id.get(symbol_id, []),
            price.get("current_or_last"),
            same_day_collection,
        )
        account_exposure = compact_account_exposure(account_item)
        symbol = {
            "symbol_id": symbol_id,
            "symbol_name": item.get("symbol_name") or (account_item or {}).get("symbol_name") or symbol_id,
            "product_type": product_type,
            "eligible_for_review": eligible,
            "evidence_mode": "full" if compact_summary_is_usable(quality_value_summary) else "price_only",
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
            "symbol_news_summary": symbol_news_summary_for(symbol_news_cache, symbol_id, args.symbol_news_cache_path, expected_symbol_news_date),
            "account_exposure": account_exposure,
            "symbol_strategy_context": build_symbol_strategy_context(
                strategy_policy,
                strategy_context,
                account_exposure,
                account_exposure_summary,
            ),
            "today_trade_price_context": {key: value for key, value in same_day_context.items() if key != "fills"},
            "today_trade_timeline_context": same_day_context,
            "required_missing": required_missing,
            "warnings": list(item.get("warnings") or []),
            "errors": errors,
        }
        artifact["symbols"].append(symbol)

    if artifact["errors"] or any(not item.get("eligible_for_review") for item in artifact["symbols"]):
        artifact["status"] = "partial"
    write_json(Path(args.output), artifact)
    return artifact


def eligible_symbol_ids(decision_brief: dict[str, Any]) -> list[str]:
    return [
        symbol_key(item)
        for item in decision_brief.get("symbols", [])
        if isinstance(item, dict) and item.get("eligible_for_review") and symbol_key(item)
    ]


def artifact_path(path: str | Path, absolute: bool) -> str:
    path_obj = Path(path)
    if absolute:
        return str(path_obj.resolve())
    return str(path_obj)


def review_extra_instructions(path: str | Path | None, stage_key: str) -> list[str]:
    if not path:
        return []
    payload = load_json(Path(path))
    raw = payload.get("review_extra_instructions") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return []
    items = raw.get(stage_key)
    if not isinstance(items, list):
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def build_first_specs(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    decision_brief = load_json(Path(args.decision_brief or output_dir / "decision-brief.json"))
    symbol_ids = normalize_symbol_ids(args.symbol_ids.split(",") if args.symbol_ids else eligible_symbol_ids(decision_brief))
    run_id = args.run_id or decision_brief.get("run_id") or output_dir.name
    started_at = args.started_at or decision_brief.get("started_at") or ""
    workspace_dir = str(Path(args.workspace_dir).resolve())
    daily_pipeline_dir = Path(args.pipeline_dir).resolve()
    absolute_paths = not args.relative_paths
    extra_instructions = review_extra_instructions(
        getattr(args, "review_extra_instructions_file", ""),
        "analyst_review",
    )

    specs = []
    for role in ANALYST_REVIEW_SPEC_ROLES:
        spec = {
            "run_id": run_id,
            "started_at": started_at,
            "stage": "analyst-review",
            "agent_role": role,
            "task_name": f"first-{role}",
            "workspace_dir": workspace_dir,
            "output_dir": str(output_dir),
            "artifact_paths": {
                "decision_brief": artifact_path(args.decision_brief or output_dir / "decision-brief.json", absolute_paths),
                "persona": artifact_path(daily_pipeline_dir / "prompts" / f"{role}.md", absolute_paths),
                "review_format": artifact_path(daily_pipeline_dir / "prompts" / "analyst-review-format.md", absolute_paths),
            },
            "symbol_ids": symbol_ids,
        }
        if extra_instructions:
            spec["extra_instructions"] = extra_instructions
        specs.append(spec)
    payload = {"specs": specs}
    write_json(Path(args.output), payload)
    return payload


def normalize_review_payload(payload: Any, stage: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    normalized = dict(payload)
    if stage == "analyst-review" and isinstance(normalized.get("symbols"), list):
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


def finite_number(value: Any) -> float | None:
    number = as_number(value)
    if isinstance(number, bool) or number is None:
        return None
    number = float(number)
    if not math.isfinite(number):
        return None
    return number


def first_sidecar_path(output_dir: Path, role: str, task_name: str) -> Path:
    return output_dir / "reviews" / f"analyst-review--{safe_name(role)}--{safe_name(task_name)}.md"


def second_sidecar_path(output_dir: Path, role: str, task_name: str) -> Path:
    return output_dir / "reviews" / f"judge-review--{safe_name(role)}--{safe_name(task_name)}.md"


def write_first_sidecar(path: Path, symbols: list[dict[str, Any]]) -> None:
    lines = [
        "| 종목 | 점수 | 의견(판단) |",
        "|---|---:|---|",
    ]
    for item in symbols:
        symbol_name = f"{item.get('symbol_id', '')} {item.get('symbol_name', '')}".strip()
        lines.append(
            f"| {symbol_name} | {as_int(item.get('score'))} | {item.get('one_line_reason', '')} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_combined_first_sidecar(
    path: Path,
    role: str,
    symbols: list[dict[str, Any]],
    symbols_by_id: dict[str, dict[str, Any]] | None = None,
) -> None:
    lines = [
        "| 관점 | 종목 | 점수 | 의견(판단) |",
        "|---|---|---:|---|",
    ]
    for item in symbols:
        symbol_name = f"{item.get('symbol_id', '')} {item.get('symbol_name', '')}".strip()
        for score in expanded_first_scores(role, item):
            if symbols_by_id is not None:
                score = mark_optional_view_exclusions(score, symbols_by_id.get(symbol_key(item), {}))
            lines.append(
                f"| {score.get('agent_role', '')} | {symbol_name} | {as_int(score.get('score'))} | {score.get('one_line_reason', '')} |"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_second_sidecar(path: Path, symbols: list[dict[str, Any]]) -> None:
    lines = [
        "| 종목 | 목표금액 | 최종수량 | 상대매력도 | 판단코드 | 의견(판단) |",
        "|---|---:|---:|---:|---|---|",
    ]
    for item in symbols:
        symbol_name = f"{item.get('symbol_id', '')} {item.get('symbol_name', '')}".strip()
        lines.append(
            f"| {symbol_name} | {as_int(item.get('target_position_value_krw')):,} | {as_int(item.get('final_holding_quantity'))} | {as_int(item.get('relative_attractiveness_rank'))} | {item.get('reason_code', '')} | {item.get('one_line_reason', '')} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_success_first_wrappers(subagent_dir: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    by_role: dict[str, dict[str, Any]] = {}
    failures_by_role: dict[str, list[str]] = {}
    errors: list[dict[str, Any]] = []
    for wrapper_path in sorted(subagent_dir.glob("*.wrapper.json")):
        wrapper = load_json(wrapper_path)
        if wrapper.get("stage") != "analyst-review":
            continue
        role = str(wrapper.get("agent_role") or "")
        if wrapper.get("status") != "success":
            failures_by_role.setdefault(role or wrapper_path.name, []).append(wrapper_path.name)
            continue
        previous = by_role.get(role)
        if previous is None or str(wrapper.get("ended_at", "")) >= str(previous.get("ended_at", "")):
            by_role[role] = wrapper
    for role in ANALYST_REVIEW_SPEC_ROLES:
        if role in by_role:
            continue
        for source in failures_by_role.get(role, []):
            errors.append(
                {
                    "stage": "analyst-review",
                    "source": source,
                    "code": "wrapper_failed",
                    "message": f"{role} wrapper has no successful replacement",
                    "required": True,
                }
            )
    return by_role, errors


def view_payload(item: dict[str, Any], role: str) -> dict[str, Any] | None:
    views = item.get("views")
    if isinstance(views, dict) and isinstance(views.get(role), dict):
        return dict(views[role])
    agent_scores = item.get("agent_scores")
    if isinstance(agent_scores, list):
        for score in agent_scores:
            if isinstance(score, dict) and str(score.get("agent_role") or "") == role:
                return dict(score)
    return None


def expanded_first_scores(role: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    expanded_roles = COMBINED_ANALYST_REVIEW_ROLES.get(role)
    if not expanded_roles:
        score = dict(item)
        score["agent_role"] = role
        return [score]

    scores: list[dict[str, Any]] = []
    for expanded_role in expanded_roles:
        score = view_payload(item, expanded_role)
        if score is None:
            continue
        score.setdefault("symbol_id", item.get("symbol_id"))
        score.setdefault("symbol_name", item.get("symbol_name"))
        score["agent_role"] = expanded_role
        scores.append(score)
    return scores


def has_usable_symbol_news_summary(symbol: dict[str, Any]) -> bool:
    symbol_news_summary = symbol.get("symbol_news_summary")
    return isinstance(symbol_news_summary, list) and any(isinstance(item, dict) for item in symbol_news_summary)


def mark_news_flow_exclusions(score_item: dict[str, Any], brief_symbol: dict[str, Any]) -> dict[str, Any]:
    if str(score_item.get("agent_role") or "") != "analyst-news-flow":
        return score_item
    if has_usable_symbol_news_summary(brief_symbol):
        normalized = dict(score_item)
        normalized["excluded_from_aggregation"] = review_score_value(score_item.get("score")) == 5
        return normalized
    normalized = dict(score_item)
    normalized["score"] = 5
    normalized["reason_code"] = "no_news_excluded"
    normalized["one_line_reason"] = "뉴스 정보가 없어 평균에서 제외"
    normalized["excluded_from_aggregation"] = True
    missing_data = normalized.get("missing_data")
    if not isinstance(missing_data, list):
        missing_data = []
    if "symbol_news_summary" not in missing_data:
        missing_data = [*missing_data, "symbol_news_summary"]
    normalized["missing_data"] = missing_data
    return normalized


def has_usable_quality_value_summary(symbol: dict[str, Any]) -> bool:
    product_type = str(symbol.get("product_type") or "stock").lower()
    summary_key = "etf_summary" if product_type in {"etf", "etn"} else "financial_summary"
    return compact_summary_is_usable(symbol.get(summary_key))


def mark_quality_value_excluded_without_financial(score_item: dict[str, Any], brief_symbol: dict[str, Any]) -> dict[str, Any]:
    if str(score_item.get("agent_role") or "") != "analyst-quality-value" or has_usable_quality_value_summary(brief_symbol):
        return score_item
    product_type = str(brief_symbol.get("product_type") or "stock").lower()
    summary_key = "etf_summary" if product_type in {"etf", "etn"} else "financial_summary"
    normalized = dict(score_item)
    normalized["score"] = 5
    normalized["reason_code"] = "no_financial_excluded"
    normalized["one_line_reason"] = "ETF 핵심 정보가 없어 평균에서 제외" if summary_key == "etf_summary" else "재무 정보가 없어 평균에서 제외"
    normalized["excluded_from_aggregation"] = True
    missing_data = normalized.get("missing_data")
    if not isinstance(missing_data, list):
        missing_data = []
    if summary_key not in missing_data:
        missing_data = [*missing_data, summary_key]
    normalized["missing_data"] = missing_data
    return normalized


def mark_optional_view_exclusions(score_item: dict[str, Any], brief_symbol: dict[str, Any]) -> dict[str, Any]:
    score_item = mark_news_flow_exclusions(score_item, brief_symbol)
    return mark_quality_value_excluded_without_financial(score_item, brief_symbol)


def build_analyst_review(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    decision_brief = load_json(Path(args.decision_brief or output_dir / "decision-brief.json"))
    symbols_by_id = indexed_symbols(decision_brief.get("symbols"))
    symbol_ids = normalize_symbol_ids(args.symbol_ids.split(",") if args.symbol_ids else eligible_symbol_ids(decision_brief))
    wrappers_by_role, errors = load_success_first_wrappers(output_dir / "subagents")

    agent_scores: dict[str, list[dict[str, Any]]] = {symbol_id: [] for symbol_id in symbol_ids}
    for role, wrapper in wrappers_by_role.items():
        payload = normalize_review_payload(wrapper.get("parsed_json"), "analyst-review")
        if payload is None:
            errors.append(
                {
                    "stage": "analyst-review",
                    "source": wrapper.get("task_name") or role,
                    "code": "missing_parsed_json",
                    "message": "successful wrapper has no parsed_json object",
                    "required": True,
                }
            )
            continue
        symbols = [item for item in payload.get("symbols", []) if isinstance(item, dict)]
        sidecar = first_sidecar_path(output_dir, role, str(wrapper.get("task_name") or role))
        if role in COMBINED_ANALYST_REVIEW_ROLES:
            write_combined_first_sidecar(sidecar, role, symbols, symbols_by_id)
        else:
            write_first_sidecar(sidecar, symbols)
        for item in symbols:
            symbol_id = symbol_key(item)
            if symbol_id not in agent_scores:
                continue
            for score_item in expanded_first_scores(role, item):
                expanded_role = str(score_item.get("agent_role") or role)
                score = review_score_value(score_item.get("score"))
                if score is None:
                    errors.append(
                        {
                            "stage": "analyst-review",
                            "symbol_id": symbol_id,
                            "source": wrapper.get("task_name") or role,
                            "code": "invalid_agent_score",
                            "message": f"{expanded_role} score must be an integer from 0 to 10",
                            "required": True,
                        }
                    )
                    continue
                score_item = mark_optional_view_exclusions(score_item, symbols_by_id.get(symbol_id, {}))
                score = review_score_value(score_item.get("score"))
                if score is None:
                    continue
                excluded_from_aggregation = bool(score_item.get("excluded_from_aggregation"))
                agent_scores[symbol_id].append(
                    {
                        "agent_role": expanded_role,
                        "source_agent_role": role,
                        "score": score,
                        "reason_code": safe_name(str(score_item.get("reason_code") or "hold_neutral")).lower(),
                        "one_line_reason": score_item.get("one_line_reason") or "",
                        "missing_data": score_item.get("missing_data") if isinstance(score_item.get("missing_data"), list) else [],
                        "excluded_from_aggregation": excluded_from_aggregation,
                    }
                )

    artifact = common_envelope(decision_brief.get("run_id") or output_dir.name, decision_brief.get("started_at") or "", "analyst-review")
    artifact["errors"] = errors
    for symbol_id in symbol_ids:
        scores = agent_scores.get(symbol_id, [])
        seen_roles = {str(item.get("agent_role") or "") for item in scores}
        missing_roles = [role for role in ANALYST_REVIEW_ROLES if role not in seen_roles]
        for role in missing_roles:
            artifact["errors"].append(
                {
                    "stage": "analyst-review",
                    "symbol_id": symbol_id,
                    "source": "merge-first",
                    "code": "missing_agent_score",
                    "message": f"{role} did not return a valid score for symbol",
                    "required": True,
                }
            )
        aggregation_scores = [item for item in scores if not item.get("excluded_from_aggregation")]
        brief_symbol = symbols_by_id.get(symbol_id, {})
        mean_score = (
            sum(item["score"] for item in aggregation_scores) / len(aggregation_scores)
            if aggregation_scores
            else None
        )
        artifact["symbols"].append(
            {
                "symbol_id": symbol_id,
                "symbol_name": brief_symbol.get("symbol_name") or symbol_id,
                "agent_scores": scores,
                "mean_score": mean_score,
                "aggregation_score_count": len(aggregation_scores),
                "final_first_score": mean_score,
            }
        )
    artifact["status"] = "partial" if artifact["errors"] else "success"
    write_json(Path(args.output), artifact)
    return artifact


def build_second_spec(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    decision_brief = load_json(Path(args.decision_brief or output_dir / "decision-brief.json"))
    analyst_review = load_json(Path(args.analyst_review or output_dir / "analyst-review.json"))
    account_path = output_dir / "account-before-order.json"
    account = load_json(account_path) if account_path.is_file() else {}
    portfolio = read_json_arg(args.portfolio_json)
    strategy_policy, _ = load_strategy_policy_config(getattr(args, "strategy_policy_config", ""))
    unheld_review_top_k = int(strategy_policy["unheld_review_top_k"])
    eligible = set(eligible_symbol_ids(decision_brief))
    holding_set = {symbol_id for symbol_id in normalize_symbol_ids(portfolio.get("holding", [])) if symbol_id in eligible}
    active_order_set = (
        {
            symbol_id
            for symbol_id, quantities in active_quantities(account).items()
            if symbol_id in eligible and (quantities["buy"] or quantities["sell"])
        }
        if account.get("active_order_lookup_performed") is True
        else set()
    )
    brief_by_symbol = indexed_symbols(decision_brief.get("symbols"))

    # No score band and no assigned candidate direction: every eligible held or
    # active-order symbol is always in scope, and up to unheld_review_top_k other
    # unheld symbols with valid scores are added by deterministic rank.
    scores_by_symbol: dict[str, float] = {}
    for item in analyst_review.get("symbols", []):
        symbol_id = symbol_key(item)
        final_first_score = finite_number(item.get("final_first_score"))
        if not symbol_id or symbol_id not in eligible or final_first_score is None:
            continue
        scores_by_symbol[symbol_id] = final_first_score

    review_scope_reasons: dict[str, str] = {}
    for symbol_id in sorted(holding_set):
        review_scope_reasons[symbol_id] = "held_position"
    for symbol_id in sorted(active_order_set - holding_set):
        review_scope_reasons[symbol_id] = "active_order"
    for symbol_id in sorted(
        symbol_id
        for symbol_id, item in brief_by_symbol.items()
        if symbol_id in eligible and has_usable_symbol_news_summary(item) and symbol_id not in review_scope_reasons
    ):
        review_scope_reasons[symbol_id] = "symbol_news"

    unheld_ranked = sorted(
        (symbol_id for symbol_id in scores_by_symbol if symbol_id not in review_scope_reasons),
        key=lambda symbol_id: (-scores_by_symbol[symbol_id], symbol_id),
    )
    for symbol_id in unheld_ranked[:unheld_review_top_k]:
        review_scope_reasons[symbol_id] = "unheld_score_rank"

    selected = sorted(review_scope_reasons)

    portfolio_snapshot: list[dict[str, Any]] = []
    for symbol_id in sorted(holding_set):
        brief_item = brief_by_symbol.get(symbol_id, {})
        exposure = brief_item.get("account_exposure") if isinstance(brief_item.get("account_exposure"), dict) else {}
        portfolio_snapshot.append(
            {
                "symbol_id": symbol_id,
                "symbol_name": brief_item.get("symbol_name") or symbol_id,
                "final_first_score": scores_by_symbol.get(symbol_id),
                "current_live_holding_quantity": as_int(exposure.get("current_live_holding_quantity")),
                "valuation_amount": as_int(exposure.get("valuation_amount")),
                "pnl_rate": as_number(exposure.get("pnl_rate")),
            }
        )

    (output_dir / "judge-review-symbols.txt").write_text("\n".join(selected) + ("\n" if selected else ""), encoding="utf-8")

    daily_pipeline_dir = Path(args.pipeline_dir).resolve()
    absolute_paths = not args.relative_paths
    extra_instructions = review_extra_instructions(
        getattr(args, "review_extra_instructions_file", ""),
        "judge_review",
    )
    payload = {
        "run_id": args.run_id or decision_brief.get("run_id") or output_dir.name,
        "started_at": args.started_at or decision_brief.get("started_at") or "",
        "stage": "judge-review",
        "review_contract_version": REVIEW_CONTRACT_VERSION,
        "agent_role": "judge",
        "task_name": "second-judge",
        "workspace_dir": str(Path(args.workspace_dir).resolve()),
        "output_dir": str(output_dir),
        "artifact_paths": {
            "decision_brief": artifact_path(args.decision_brief or output_dir / "decision-brief.json", absolute_paths),
            "analyst_review": artifact_path(args.analyst_review or output_dir / "analyst-review.json", absolute_paths),
            "persona": artifact_path(daily_pipeline_dir / "prompts" / "judge.md", absolute_paths),
            "review_format": artifact_path(daily_pipeline_dir / "prompts" / "judge-review-format.md", absolute_paths),
        },
        "symbol_ids": selected,
        "review_scope_reasons": review_scope_reasons,
        "portfolio_snapshot": portfolio_snapshot,
    }
    if extra_instructions:
        payload["extra_instructions"] = extra_instructions
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
    judge_review = load_json(Path(args.judge_review or output_dir / "judge-review.json"))
    account = load_json(Path(args.account_before_order or output_dir / "account-before-order.json"))
    decision_brief_path = Path(args.decision_brief) if args.decision_brief else output_dir / "decision-brief.json"
    decision_brief = load_json(decision_brief_path)
    analyst_review_path = Path(getattr(args, "analyst_review", None) or output_dir / "analyst-review.json")
    analyst_review = load_json(analyst_review_path) if analyst_review_path.is_file() else {}
    scores_by_symbol: dict[str, float] = {}
    for review_item in analyst_review.get("symbols", []) if isinstance(analyst_review, dict) else []:
        review_symbol_id = symbol_key(review_item)
        review_score = finite_number(review_item.get("final_first_score"))
        if review_symbol_id and review_score is not None:
            scores_by_symbol[review_symbol_id] = review_score
    account_by_symbol = indexed_symbols(account.get("symbols"))
    brief_by_symbol = indexed_symbols(decision_brief.get("symbols"))
    active = active_quantities(account)
    order_path = str(getattr(args, "order_path", "reservation") or "reservation")
    if order_path not in {"reservation", "immediate"}:
        raise ValueError(f"unsupported order_path: {order_path}")
    order_api = "order_cash" if order_path == "immediate" else "order_resv"

    run_id = args.run_id or judge_review.get("run_id") or account.get("run_id") or output_dir.name
    started_at = args.started_at or judge_review.get("started_at") or account.get("started_at") or ""
    artifact = common_envelope(run_id, started_at, "execution")
    artifact.update(
        {
            # Version 3 carries the Judge's direct target without a separate
            # strategy-authorization guard; broker/order checks still run later.
            "schema_version": "3",
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

    blocked_any = False
    invalid_final_quantity = False
    refreshable_gate_blocked = False
    judge_symbol_ids: set[str] = set()
    judge_items = [
        item
        for item in judge_review.get("symbols", [])
        if isinstance(item, dict) and symbol_key(item)
    ]
    judge_symbol_counts: dict[str, int] = {}
    for item in judge_items:
        item_symbol_id = symbol_key(item)
        judge_symbol_counts[item_symbol_id] = judge_symbol_counts.get(item_symbol_id, 0) + 1
    duplicate_judge_symbol_ids = {
        symbol_id for symbol_id, count in judge_symbol_counts.items() if count > 1
    }
    for symbol_id in sorted(duplicate_judge_symbol_ids):
        artifact["errors"].append(
            {
                "stage": "execution",
                "source": "build_run_artifacts",
                "code": "duplicate_judge_symbol",
                "message": f"{symbol_id}: duplicate judge rows were rejected before order planning",
                "required": True,
            }
        )
    if duplicate_judge_symbol_ids:
        blocked_any = True
    for item in judge_items:
        symbol_id = symbol_key(item)
        if symbol_id in duplicate_judge_symbol_ids:
            continue
        judge_symbol_ids.add(symbol_id)
        account_item = account_by_symbol.get(symbol_id, {})
        brief_item = brief_by_symbol.get(symbol_id, {})
        active_item = active.get(symbol_id, {"buy": 0, "sell": 0})
        current_qty = as_int(account_item.get("current_live_holding_quantity"))
        buy_qty = active_item.get("buy", 0)
        sell_qty = active_item.get("sell", 0)
        expected_qty = current_qty + buy_qty - sell_qty
        final_qty = non_negative_int_value(item.get("final_holding_quantity"))
        if final_qty is None:
            artifact["errors"].append(
                {
                    "stage": "execution",
                    "source": "build_run_artifacts",
                    "code": "invalid_final_holding_quantity",
                    "message": f"{symbol_id}: final_holding_quantity must be a non-negative integer",
                    "required": True,
                }
            )
            invalid_final_quantity = True
            continue
        delta = final_qty - expected_qty
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
        reason = "final_equals_expected_holding_quantity"
        holding_state_status = str(account_item.get("holding_state_status") or "").strip()
        active_order_reconciliation_required = bool(buy_qty or sell_qty) and delta != 0
        active_cancel_only = active_order_reconciliation_required and final_qty == current_qty
        reconciliation_only = False
        judge_final_qty = final_qty
        if holding_state_status in {"inconsistent", "unconfirmed"}:
            if buy_qty or sell_qty:
                final_qty = current_qty
                delta = final_qty - expected_qty
                direction = "buy" if delta > 0 else "sell" if delta < 0 else "none"
                active_order_reconciliation_required = True
                active_cancel_only = True
                reconciliation_only = True
                reason = "unverified_holding_requires_active_order_cancellation"
            else:
                direction = "none"
                result = "blocked"
                reason = "holding_state_not_verified"
                blocked_any = True
        elif direction != "none":
            row_gate_missing = account.get("active_order_lookup_performed") is not True or (
                account.get("order_available_lookup_performed") is not True and not active_cancel_only
            )
            if args.request_type in {"demo-submit", "real-submit", "prepare"} and row_gate_missing:
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
                "final_first_score": scores_by_symbol.get(symbol_id),
                "requested_target_position_value_krw": item.get("requested_target_position_value_krw"),
                "target_position_value_krw": item.get("target_position_value_krw"),
                "requested_action": item.get("requested_action") or "hold",
                "canonical_action": item.get("canonical_action") or "hold",
                "decision_basis": item.get("decision_basis") or "none",
                "holding_state_status": holding_state_status,
                "holding_state_reasons": list(account_item.get("holding_state_reasons") or []),
                "current_live_holding_quantity": current_qty,
                "pending_and_reserved_buy_quantity": buy_qty,
                "pending_and_reserved_sell_quantity": sell_qty,
                "expected_holding_quantity": expected_qty,
                "judge_final_holding_quantity": judge_final_qty,
                "final_holding_quantity": final_qty,
                "additional_required_quantity": delta,
                "validated_order_quantity": abs(delta),
                "order_price": order_price,
                "order_path": order_path,
                "order_api": order_api,
                "active_order_reconciliation_required": active_order_reconciliation_required,
                "active_cancel_only": active_cancel_only,
                "reconciliation_only": reconciliation_only,
                "result": result,
                "reason": reason,
                "order_or_reservation_id": "",
                "attempts": [],
            }
        )

    for symbol_id, active_item in active.items():
        if symbol_id in judge_symbol_ids or not (active_item.get("buy", 0) or active_item.get("sell", 0)):
            continue
        account_item = account_by_symbol.get(symbol_id, {})
        active_rows = [
            row
            for row in account.get("active_orders", [])
            if isinstance(row, dict) and symbol_key(row) == symbol_id and row.get("active_status") == "active"
        ]
        current_qty = as_int(account_item.get("current_live_holding_quantity"))
        buy_qty = active_item.get("buy", 0)
        sell_qty = active_item.get("sell", 0)
        expected_qty = current_qty + buy_qty - sell_qty
        delta = current_qty - expected_qty
        direction = "buy" if delta > 0 else "sell" if delta < 0 else "none"
        holding_state_status = str(account_item.get("holding_state_status") or "").strip()
        order_price = (
            as_number(account_item.get("current_price"))
            or next((as_number(row.get("order_price")) for row in active_rows if as_number(row.get("order_price"))), 0)
            or 0
        )
        active_path = str(next((row.get("order_path") for row in active_rows if row.get("order_path")), order_path))
        active_api = str(next((row.get("order_api") for row in active_rows if row.get("order_api")), order_api))
        artifact["orders"].append(
            {
                "symbol_id": symbol_id,
                "symbol_name": (
                    account_item.get("symbol_name")
                    if account_item.get("symbol_name") != symbol_id
                    else None
                ) or next(
                    (row.get("symbol_name") for row in active_rows if row.get("symbol_name")),
                    symbol_id,
                ),
                "direction": direction,
                "final_first_score": None,
                "holding_state_status": holding_state_status,
                "holding_state_reasons": list(account_item.get("holding_state_reasons") or []),
                "current_live_holding_quantity": current_qty,
                "pending_and_reserved_buy_quantity": buy_qty,
                "pending_and_reserved_sell_quantity": sell_qty,
                "expected_holding_quantity": expected_qty,
                "final_holding_quantity": current_qty,
                "additional_required_quantity": delta,
                "validated_order_quantity": abs(delta),
                "order_price": order_price,
                "order_path": active_path,
                "order_api": active_api,
                "active_order_reconciliation_required": True,
                "active_cancel_only": True,
                "reconciliation_only": True,
                "result": "skipped",
                "reason": (
                    "unverified_holding_requires_active_order_cancellation"
                    if holding_state_status in {"inconsistent", "unconfirmed"}
                    else "stale_active_order_requires_cancellation"
                ),
                "order_or_reservation_id": "",
                "attempts": [],
            }
        )
    artifact["symbols"] = [item["symbol_id"] for item in artifact["orders"]]
    if args.request_type in {"demo-submit", "real-submit"} and any(
        item.get("direction") != "none" or item.get("active_order_reconciliation_required") is True
        for item in artifact["orders"]
    ):
        artifact["requires_main_agent_order_execution"] = True
        if refreshable_gate_blocked:
            artifact["required_main_agent_actions"] = [
                "refresh_active_order_lookup",
                "refresh_order_available_lookup",
                "continue_order_execution",
            ]
        elif not blocked_any:
            artifact["required_main_agent_actions"] = ["continue_order_execution"]
    if invalid_final_quantity or blocked_any:
        artifact["status"] = "partial"
    if blocked_any:
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
    """Run the extracted test suite through the legacy CLI contract."""
    codex_exec_root = pipeline_dir().parents[2]
    codex_exec_root_text = str(codex_exec_root)
    if codex_exec_root_text not in sys.path:
        sys.path.insert(0, codex_exec_root_text)

    from service.pipelines.daily_trading.tests.test_build_run_artifacts import (
        run_self_test as run_external_self_test,
    )

    return run_external_self_test()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build deterministic daily-trading run artifacts.")
    subparsers = parser.add_subparsers(dest="command")

    decision = subparsers.add_parser("decision-brief", help="Build decision-brief.json.")
    decision.add_argument("--output-dir", type=Path, required=True)
    decision.add_argument("--portfolio-json", required=True, help="Path to check-portfolio JSON, or '-' for stdin.")
    decision.add_argument("--price-chart")
    decision.add_argument("--account-before-order")
    decision.add_argument("--today-fills")
    decision.add_argument("--financial-cache-path", default="")
    decision.add_argument("--symbol-news-cache-path", default="")
    decision.add_argument("--news-context-json", default="")
    decision.add_argument("--expected-symbol-news-date", default="")
    decision.add_argument("--market-index-snapshot-json", default="")
    decision.add_argument("--strategy-policy-config", default="")
    decision.add_argument("--run-id")
    decision.add_argument("--started-at")
    decision.add_argument("--output", type=Path, default=None)

    first_specs = subparsers.add_parser("first-specs", help="Build analyst-review-specs.json.")
    first_specs.add_argument("--output-dir", type=Path, required=True)
    first_specs.add_argument("--decision-brief")
    first_specs.add_argument("--workspace-dir", default=".")
    first_specs.add_argument("--pipeline-dir", default=str(pipeline_dir()))
    first_specs.add_argument("--symbol-ids", default="")
    first_specs.add_argument("--run-id")
    first_specs.add_argument("--started-at")
    first_specs.add_argument("--relative-paths", action="store_true")
    first_specs.add_argument("--review-extra-instructions-file", default="")
    first_specs.add_argument("--output", type=Path, default=None)

    merge_first = subparsers.add_parser("merge-first", help="Merge analyst-review wrappers into analyst-review.json.")
    merge_first.add_argument("--output-dir", type=Path, required=True)
    merge_first.add_argument("--decision-brief")
    merge_first.add_argument("--symbol-ids", default="")
    merge_first.add_argument("--output", type=Path, default=None)

    second_spec = subparsers.add_parser("second-spec", help="Build judge-review symbols and spec.")
    second_spec.add_argument("--output-dir", type=Path, required=True)
    second_spec.add_argument("--portfolio-json", required=True)
    second_spec.add_argument("--decision-brief")
    second_spec.add_argument("--analyst-review")
    second_spec.add_argument("--workspace-dir", default=".")
    second_spec.add_argument("--pipeline-dir", default=str(pipeline_dir()))
    second_spec.add_argument("--strategy-policy-config", default="")
    second_spec.add_argument("--run-id")
    second_spec.add_argument("--started-at")
    second_spec.add_argument("--relative-paths", action="store_true")
    second_spec.add_argument("--review-extra-instructions-file", default="")
    second_spec.add_argument("--output", type=Path, default=None)

    execution = subparsers.add_parser("execution-plan", help="Build non-submitting execution.json plan.")
    execution.add_argument("--output-dir", type=Path, required=True)
    execution.add_argument("--judge-review")
    execution.add_argument("--account-before-order")
    execution.add_argument("--decision-brief", help="Path to decision-brief.json. Defaults to <output-dir>/decision-brief.json.")
    execution.add_argument("--analyst-review", help="Path to analyst-review.json. Defaults to <output-dir>/analyst-review.json.")
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
            args.output = args.output_dir / "analyst-review-specs.json"
        print(json.dumps(build_first_specs(args), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "merge-first":
        if args.output is None:
            args.output = args.output_dir / "analyst-review.json"
        print(json.dumps(build_analyst_review(args), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "second-spec":
        if args.output is None:
            args.output = args.output_dir / "judge-review-spec.json"
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
