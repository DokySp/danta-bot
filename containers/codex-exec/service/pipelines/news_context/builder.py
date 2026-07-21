"""Build one run-local, deduplicated news context without network access."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from ..market_news.collector import canonicalize_url, title_hash
from ..market_news.storage import MarketNewsStore


KST = ZoneInfo("Asia/Seoul")


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(" KST"):
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S KST").replace(tzinfo=KST).astimezone(timezone.utc)
        except ValueError:
            return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def previous_run_started_at(workspace_dir: Path, *, current_run_id: str, current_started_at: datetime) -> datetime | None:
    candidates: list[datetime] = []
    runs_root = workspace_dir / "reports" / "runs"
    if not runs_root.exists():
        return None
    for path in runs_root.glob("*/run.json"):
        if path.parent.name == current_run_id:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            summary = json.loads((path.parent / "pipeline-summary.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, dict)
            or not isinstance(summary, dict)
            or str(summary.get("status") or "").lower() not in {"success", "partial"}
        ):
            continue
        started_at = parse_datetime(payload.get("started_at"))
        if started_at is not None and started_at < current_started_at:
            candidates.append(started_at)
    return max(candidates) if candidates else None


def load_symbol_items(path: Path | None, *, window_start: datetime, window_end: datetime) -> tuple[str, list[dict[str, Any]]]:
    if path is None or not path.exists():
        return "missing", []
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return "failed", []
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, dict):
        return "failed", []
    items: list[dict[str, Any]] = []
    for raw_symbol_id, raw_symbol in symbols.items():
        symbol_id = str(raw_symbol_id or "").zfill(6)
        symbol = raw_symbol if isinstance(raw_symbol, dict) else {}
        articles = symbol.get("articles") if isinstance(symbol.get("articles"), list) else []
        for article in articles:
            if not isinstance(article, dict):
                continue
            title = " ".join(str(article.get("content") or article.get("title") or "").split())[:500]
            if not title or "수집된 뉴스가 없습니다" in title:
                continue
            published_at = parse_datetime(article.get("article_date"))
            if published_at is None or published_at < window_start or published_at > window_end:
                continue
            items.append(
                {
                    "title": title,
                    "published_at": iso(published_at),
                    "collected_at": "",
                    "url": "",
                    "canonical_url": "",
                    "domain": "kis_open_api",
                    "source_ids": ["kis_symbol_news"],
                    "providers": ["kis_open_api"],
                    "classifications": ["symbol_news"],
                    "symbol_ids": [symbol_id],
                    "symbol_names": [str(symbol.get("symbol_name") or "")],
                    "scopes": ["symbol_news"],
                }
            )
    return "supplied" if items else "empty", items


def market_status(latest: dict[str, dict[str, Any]], items: list[dict[str, Any]], database_exists: bool) -> str:
    if not database_exists:
        return "missing"
    statuses = {str(item.get("status") or "") for item in latest.values()}
    if statuses and statuses <= {"success"}:
        return "supplied" if items else "empty"
    if items:
        return "partial"
    if "success" in statuses:
        return "partial"
    if statuses:
        return "failed"
    return "empty"


def item_keys(item: dict[str, Any]) -> list[str]:
    canonical_url = canonicalize_url(item.get("canonical_url") or item.get("url"))
    keys = [f"url:{canonical_url}"] if canonical_url else []
    if str(item.get("title") or "").strip():
        keys.append(f"title:{title_hash(item.get('title'))}")
    return keys


def merge_unique(base: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key in ("source_ids", "providers", "classifications", "symbol_ids", "symbol_names", "scopes"):
        values = [str(value) for value in base.get(key) or [] if str(value)]
        values.extend(str(value) for value in incoming.get(key) or [] if str(value))
        base[key] = sorted(set(values))
    if not base.get("url") and incoming.get("url"):
        base["url"] = incoming["url"]
        base["canonical_url"] = incoming.get("canonical_url") or canonicalize_url(incoming["url"])
    if not base.get("published_at") and incoming.get("published_at"):
        base["published_at"] = incoming["published_at"]
    if not base.get("collected_at") and incoming.get("collected_at"):
        base["collected_at"] = incoming["collected_at"]


def select_market_items(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    max_items = max(1, limit)
    selected_indexes: list[int] = []
    selected_set: set[int] = set()
    source_cursors = {"domestic": 0, "global": 0}
    while len(selected_indexes) < max_items:
        progressed = False
        for source_id in ("domestic", "global"):
            cursor = source_cursors[source_id]
            while cursor < len(items):
                index = cursor
                cursor += 1
                if index in selected_set:
                    continue
                source_ids = {str(value) for value in items[index].get("source_ids") or []}
                if source_id not in source_ids:
                    continue
                selected_indexes.append(index)
                selected_set.add(index)
                progressed = True
                break
            source_cursors[source_id] = cursor
            if len(selected_indexes) >= max_items:
                break
        if not progressed:
            break
    for index in range(len(items)):
        if len(selected_indexes) >= max_items:
            break
        if index not in selected_set:
            selected_indexes.append(index)
            selected_set.add(index)
    return [items[index] for index in sorted(selected_indexes)]


def build_news_context(
    *,
    workspace_dir: Path,
    current_run_id: str,
    current_started_at: str,
    symbol_news_cache_path: Path | None,
    market_news_db_path: Path,
    max_market_items: int = 30,
    max_lookback_hours: int = 72,
) -> dict[str, Any]:
    end = parse_datetime(current_started_at)
    if end is None:
        raise ValueError(f"invalid current_started_at: {current_started_at!r}")
    fallback_start = end - timedelta(hours=max(1, max_lookback_hours))
    previous = previous_run_started_at(workspace_dir, current_run_id=current_run_id, current_started_at=end)
    start = max(previous, fallback_start) if previous is not None else fallback_start

    symbol_status, symbol_items = load_symbol_items(symbol_news_cache_path, window_start=start, window_end=end)
    database_exists = market_news_db_path.exists()
    if database_exists:
        store = MarketNewsStore(market_news_db_path, read_only=True)
        source_limit = max(100, max_market_items * 10)
        candidates = [
            item
            for source_id in ("domestic", "global")
            for item in store.query_between(
                iso(start),
                iso(end),
                limit=source_limit,
                source_id=source_id,
            )
        ]
        raw_market_items: list[dict[str, Any]] = []
        candidate_owner: dict[str, dict[str, Any]] = {}
        for item in candidates:
            keys = item_keys(item)
            existing = next((candidate_owner[key] for key in keys if key in candidate_owner), None)
            if existing is None:
                existing = dict(item)
                raw_market_items.append(existing)
            else:
                merge_unique(existing, item)
            for key in keys:
                candidate_owner[key] = existing
        latest_statuses = store.latest_run_statuses()
    else:
        raw_market_items = []
        latest_statuses = {}
    normalized_market_items = [
        {
            **item,
            "symbol_ids": [],
            "symbol_names": [],
            "scopes": ["market_news"],
        }
        for item in raw_market_items
    ]

    canonical: dict[str, dict[str, Any]] = {}
    section: dict[str, str] = {}
    key_owner: dict[str, str] = {}
    duplicate_count = 0
    for scope, items in (("symbol_news", symbol_items), ("market_news", normalized_market_items)):
        for item in items:
            keys = item_keys(item)
            matched = list(dict.fromkeys(key_owner[key] for key in keys if key in key_owner))
            if matched:
                primary = matched[0]
                for secondary in matched[1:]:
                    merge_unique(canonical[primary], canonical.pop(secondary))
                    if section.get(secondary) == "symbol_news":
                        section[primary] = "symbol_news"
                    section.pop(secondary, None)
                    for alias, owner in list(key_owner.items()):
                        if owner == secondary:
                            key_owner[alias] = primary
                merge_unique(canonical[primary], item)
                duplicate_count += 1
            else:
                primary = f"article:{len(canonical) + 1}"
                canonical[primary] = dict(item)
                section[primary] = scope
            for key in keys:
                key_owner[key] = primary

    def sort_key(item: dict[str, Any]) -> str:
        return str(item.get("published_at") or item.get("collected_at") or "")

    merged_symbol = sorted(
        (item for key, item in canonical.items() if section[key] == "symbol_news"),
        key=sort_key,
        reverse=True,
    )
    merged_market_all = sorted(
        (item for key, item in canonical.items() if section[key] == "market_news"),
        key=sort_key,
        reverse=True,
    )
    merged_market = select_market_items(merged_market_all, max_market_items)
    market_domain_status = market_status(latest_statuses, raw_market_items, database_exists)
    statuses = {symbol_status, market_domain_status}
    overall_status = "success" if statuses <= {"supplied", "empty"} else "partial"
    return {
        "schema_version": "1",
        "status": overall_status,
        "run_id": current_run_id,
        "generated_at": iso(datetime.now(timezone.utc)),
        "window_start": iso(start),
        "window_end": iso(end),
        "window_source": "previous_daily_trading_run" if previous is not None and previous >= fallback_start else "fallback_lookback",
        "max_lookback_hours": max(1, max_lookback_hours),
        "deduplicated_count": duplicate_count,
        "symbol_news": {
            "status": symbol_status,
            "cache_path": str(symbol_news_cache_path or ""),
            "raw_count": len(symbol_items),
            "items": merged_symbol,
        },
        "market_news": {
            "status": market_domain_status,
            "db_path": str(market_news_db_path),
            "raw_count": len(raw_market_items),
            "selected_count": len(merged_market),
            "max_items": max(1, max_market_items),
            "source_statuses": latest_statuses,
            "items": merged_market,
        },
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
