"""Deterministic GDELT market-news collector."""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import yaml

from .storage import MarketNewsStore, now_iso, parse_iso


DEFAULT_CONFIG_PATH = Path("/app/config/market-news.yaml")
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 1800


class GdeltRateLimitedError(RuntimeError):
    """Raised on the first GDELT HTTP 429; never retried within fetch_gdelt."""

    def __init__(self, retry_after_seconds: float | None = None) -> None:
        super().__init__("GDELT rate limited (HTTP 429)")
        self.retry_after_seconds = retry_after_seconds
        self.request_count = 0


def parse_retry_after(value: Any, *, now: datetime | None = None) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        seconds = None
    if seconds is not None:
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delta = (parsed - precise_utc_datetime(now)).total_seconds()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return max(0.0, delta)


def load_config(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"market-news config must be a mapping: {path}")
    request = payload.get("request")
    sources = payload.get("sources")
    if not isinstance(request, dict) or not isinstance(sources, list) or not sources:
        raise ValueError("market-news config requires request and non-empty sources")
    source_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or not str(source.get("id") or "").strip() or not str(source.get("query") or "").strip():
            raise ValueError("every market-news source requires id and query")
        source_id = str(source.get("id") or "").strip()
        if source_id in source_ids:
            raise ValueError(f"duplicate market-news source id: {source_id}")
        source_ids.add(source_id)
    return payload


def resolve_config_path(value: str | Path | None = None) -> Path:
    text = str(value or os.getenv("MARKET_NEWS_CONFIG_FILE", "")).strip()
    return Path(text).expanduser() if text else DEFAULT_CONFIG_PATH


def resolve_db_path(workspace_dir: Path, value: str | Path | None = None) -> Path:
    text = str(value or os.getenv("MARKET_NEWS_DB_PATH", "")).strip()
    if text:
        path = Path(text).expanduser()
        return path if path.is_absolute() else workspace_dir / path
    memory_root = str(os.getenv("DAILY_TRADING_MEMORY_DIR", "")).strip()
    root = Path(memory_root).expanduser() if memory_root else workspace_dir / "memory"
    return root / "market-news" / "market-news.sqlite3"


def utc_datetime(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).replace(microsecond=0)


def precise_utc_datetime(value: datetime | None = None) -> datetime:
    """UTC normalizer for operational cooldown timing; preserves microseconds unlike utc_datetime."""
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def gdelt_datetime(value: datetime) -> str:
    return utc_datetime(value).strftime("%Y%m%d%H%M%S")


def parse_gdelt_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    parsed = parse_iso(text)
    return parsed.replace(microsecond=0).isoformat() if parsed else ""


def plain_title(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())[:500]


def normalized_title(value: Any) -> str:
    text = plain_title(value).casefold()
    return "".join(character for character in text if character.isalnum())


def title_hash(value: Any) -> str:
    return hashlib.sha256(normalized_title(value).encode("utf-8")).hexdigest()


def canonicalize_url(value: Any) -> str:
    text = html.unescape(str(value or "")).strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in TRACKING_QUERY_KEYS
        and not any(key.casefold().startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, urlencode(query), ""))


def parse_gdelt_articles(payload: Any, *, source_id: str, collected_at: str) -> list[dict[str, Any]]:
    rows = payload.get("articles") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("GDELT response does not contain an articles list")
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = plain_title(row.get("title"))
        if not title or not normalized_title(title):
            continue
        url = str(row.get("url") or "").strip()
        canonical_url = canonicalize_url(url)
        result.append(
            {
                "provider_article_id": canonical_url or title_hash(title),
                "canonical_url": canonical_url,
                "title_hash": title_hash(title),
                "title": title,
                "url": url,
                "domain": str(row.get("domain") or urlsplit(url).netloc or "").strip().casefold(),
                "source_country": str(row.get("sourcecountry") or "").strip(),
                "source_language": str(row.get("language") or "").strip(),
                "published_at": parse_gdelt_datetime(row.get("seendate") or row.get("date")),
                "collected_at": collected_at,
                "classification": source_id,
            }
        )
    return result


def request_json(url: str, *, params: dict[str, str], timeout_seconds: int) -> dict[str, Any]:
    request = Request(
        f"{url}?{urlencode(params)}",
        headers={"Accept": "application/json", "User-Agent": "danta-bot-market-news/1"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8", errors="replace")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("GDELT response is not a JSON object")
    return payload


def fetch_gdelt(
    *,
    request_config: dict[str, Any],
    query: str,
    window_start: datetime,
    window_end: datetime,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    retries = max(0, int(request_config.get("retries", 3)))
    backoff = request_config.get("retry_backoff_seconds")
    delays = [float(item) for item in backoff] if isinstance(backoff, list) and backoff else [1.0, 3.0, 8.0]
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "sort": "datedesc",
        "maxrecords": str(min(250, max(1, int(request_config.get("max_records", 250))))),
        "startdatetime": gdelt_datetime(window_start),
        "enddatetime": gdelt_datetime(window_end),
    }
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return request_json(
                str(request_config.get("base_url") or "https://api.gdeltproject.org/api/v2/doc/doc"),
                params=params,
                timeout_seconds=max(1, int(request_config.get("timeout_seconds", 20))),
            )
        except HTTPError as exc:
            if exc.code == 429:
                headers = getattr(exc, "headers", None)
                getter = getattr(headers, "get", None)
                retry_after_value = getter("Retry-After") if callable(getter) else None
                raise GdeltRateLimitedError(parse_retry_after(retry_after_value)) from exc
            last_error = exc
            if exc.code < 500:
                raise
        except (TimeoutError, URLError, OSError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
        if attempt < retries:
            sleep(delays[min(attempt, len(delays) - 1)])
    raise RuntimeError(f"GDELT request failed after {retries + 1} attempts: {last_error}")


def fetch_source_windowed(
    *,
    request_config: dict[str, Any],
    query: str,
    source_id: str,
    window_start: datetime,
    window_end: datetime,
    fetcher: Callable[..., dict[str, Any]],
    sleep: Callable[[float], None],
    min_window_minutes: int,
    max_requests: int,
) -> tuple[list[dict[str, Any]], int, list[tuple[datetime, datetime]]]:
    """Split saturated provider windows so a capped response is never silently complete."""
    max_records = min(250, max(1, int(request_config.get("max_records", 250))))
    minimum_seconds = max(1, min_window_minutes) * 60
    pending = [(window_start, window_end)]
    unresolved: list[tuple[datetime, datetime]] = []
    articles_by_key: dict[str, dict[str, Any]] = {}
    request_count = 0

    while pending:
        if request_count >= max(1, max_requests):
            unresolved.extend(pending)
            break
        current_start, current_end = pending.pop()
        try:
            payload = fetcher(
                request_config=request_config,
                query=query,
                window_start=current_start,
                window_end=current_end,
                sleep=sleep,
            )
        except GdeltRateLimitedError as exc:
            request_count += 1
            exc.request_count = request_count
            raise
        request_count += 1
        parsed = parse_gdelt_articles(payload, source_id=source_id, collected_at=now_iso())
        for article in parsed:
            key = str(article.get("provider_article_id") or article.get("title_hash") or "")
            if key:
                articles_by_key.setdefault(key, article)

        raw_rows = payload.get("articles") if isinstance(payload, dict) else None
        saturated = isinstance(raw_rows, list) and len(raw_rows) >= max_records
        duration_seconds = max(0.0, (current_end - current_start).total_seconds())
        if saturated and duration_seconds > minimum_seconds:
            midpoint = current_start + timedelta(seconds=max(1, int(duration_seconds / 2)))
            if current_start < midpoint < current_end:
                pending.append((current_start, midpoint))
                pending.append((midpoint, current_end))
                continue
        if saturated:
            unresolved.append((current_start, current_end))

    return list(articles_by_key.values()), request_count, unresolved


def collect_market_news(
    *,
    config_path: Path,
    db_path: Path,
    current_time: datetime | None = None,
    fetcher: Callable[..., dict[str, Any]] = fetch_gdelt,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = precise_utc_datetime,
) -> dict[str, Any]:
    config = load_config(config_path)
    request_config = config["request"]
    store = MarketNewsStore(db_path)
    end = utc_datetime(current_time)
    started_at = now_iso()
    overlap = timedelta(minutes=max(0, int(config.get("overlap_minutes", 30))))
    initial_lookback = timedelta(hours=max(1, int(config.get("initial_lookback_hours", 72))))
    max_lookback = timedelta(hours=max(1, int(config.get("max_lookback_hours", 72))))
    lock_timeout = max(1, int(config.get("lock_timeout_seconds", 900)))
    min_window_minutes = max(1, int(config.get("min_request_window_minutes", 15)))
    max_requests = max(1, int(config.get("max_requests_per_source", 16)))
    provider = str(config.get("provider") or "gdelt_doc_2")
    fallback_cooldown_seconds = max(
        1, int(config.get("rate_limit_cooldown_seconds", DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS))
    )
    results: list[dict[str, Any]] = []

    with store.acquire_run_lock(timeout_seconds=lock_timeout) as acquired:
        if not acquired:
            return {
                "status": "skipped_locked",
                "started_at": started_at,
                "finished_at": now_iso(),
                "db_path": str(db_path),
                "sources": [],
                "errors": [],
            }

        provider_state = store.get_provider_state(provider)
        if provider_state and provider_state["status"] == "rate_limited":
            cooldown_until = parse_iso(str(provider_state.get("cooldown_until") or ""))
            if cooldown_until and cooldown_until > precise_utc_datetime(clock()):
                return {
                    "status": "skipped_rate_limited",
                    "started_at": started_at,
                    "finished_at": now_iso(),
                    "db_path": str(db_path),
                    "sources": [],
                    "fetched_count": 0,
                    "inserted_count": 0,
                    "duplicate_count": 0,
                    "request_count": 0,
                    "saturated_window_count": 0,
                    "errors": [],
                    "provider": provider,
                    "rate_limited_until": provider_state.get("cooldown_until"),
                    "alert": False,
                    "recovered": False,
                }

        rate_limited_info: dict[str, Any] | None = None
        for raw_source in config["sources"]:
            source = raw_source if isinstance(raw_source, dict) else {}
            source_id = str(source.get("id") or "").strip()
            cursor = parse_iso(store.get_cursor(source_id) or "")
            start = (cursor - overlap) if cursor else (end - initial_lookback)
            start = max(start, end - max_lookback)
            source_started_at = now_iso()
            try:
                articles, request_count, unresolved_windows = fetch_source_windowed(
                    request_config=request_config,
                    query=str(source.get("query") or ""),
                    source_id=source_id,
                    window_start=start,
                    window_end=end,
                    fetcher=fetcher,
                    sleep=sleep,
                    min_window_minutes=min_window_minutes,
                    max_requests=max_requests,
                )
                upsert = store.upsert_articles(source_id, provider, articles)
                if unresolved_windows:
                    status = "partial"
                    error = (
                        f"provider result limit remained saturated for {len(unresolved_windows)} window(s) "
                        f"after {request_count} request(s); cursor not advanced"
                    )
                else:
                    status = "success"
                    error = ""
                    store.set_cursor(source_id, end.isoformat())
                result = {
                    "source_id": source_id,
                    "status": status,
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "fetched_count": len(articles),
                    "inserted_count": upsert.inserted_count,
                    "duplicate_count": upsert.duplicate_count,
                    "request_count": request_count,
                    "saturated_window_count": len(unresolved_windows),
                    "error": error,
                }
                store.record_run(
                    source_id=source_id,
                    started_at=source_started_at,
                    finished_at=now_iso(),
                    **{key: result[key] for key in (
                        "status", "window_start", "window_end", "fetched_count",
                        "inserted_count", "duplicate_count", "error",
                    )},
                )
                results.append(result)
            except GdeltRateLimitedError as exc:
                request_count = int(getattr(exc, "request_count", 0))
                cooldown_seconds = (
                    exc.retry_after_seconds if exc.retry_after_seconds is not None and exc.retry_after_seconds >= 0
                    else fallback_cooldown_seconds
                )
                rate_limited_at = precise_utc_datetime(clock())
                try:
                    cooldown_until_dt = rate_limited_at + timedelta(seconds=cooldown_seconds)
                except (OverflowError, ValueError):
                    cooldown_until_dt = rate_limited_at + timedelta(seconds=fallback_cooldown_seconds)
                should_alert = store.mark_provider_rate_limited(provider, cooldown_until_dt.isoformat())
                result = {
                    "source_id": source_id,
                    "status": "skipped_rate_limited",
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "fetched_count": 0,
                    "inserted_count": 0,
                    "duplicate_count": 0,
                    "request_count": request_count,
                    "saturated_window_count": 0,
                    "error": f"GDELT rate limited (HTTP 429); cooldown until {cooldown_until_dt.isoformat()}",
                }
                store.record_run(
                    source_id=source_id,
                    started_at=source_started_at,
                    finished_at=now_iso(),
                    **{key: result[key] for key in (
                        "status", "window_start", "window_end", "fetched_count",
                        "inserted_count", "duplicate_count", "error",
                    )},
                )
                results.append(result)
                rate_limited_info = {"cooldown_until": cooldown_until_dt.isoformat(), "alert": should_alert}
                break
            except Exception as exc:  # noqa: BLE001 - one source must not stop the other
                status = "failed"
                error = str(exc)[:500]
                result = {
                    "source_id": source_id,
                    "status": status,
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "fetched_count": 0,
                    "inserted_count": 0,
                    "duplicate_count": 0,
                    "request_count": 0,
                    "saturated_window_count": 0,
                    "error": error,
                }
                store.record_run(
                    source_id=source_id,
                    started_at=source_started_at,
                    finished_at=now_iso(),
                    **{key: result[key] for key in (
                        "status", "window_start", "window_end", "fetched_count",
                        "inserted_count", "duplicate_count", "error",
                    )},
                )
                results.append(result)

        if rate_limited_info is not None:
            genuine_results = [item for item in results if item["status"] != "skipped_rate_limited"]
            if any(item["status"] == "failed" for item in genuine_results):
                overall_status = "failed"
            elif any(item["status"] == "partial" for item in genuine_results):
                overall_status = "partial"
            else:
                overall_status = "skipped_rate_limited"
            genuine_errors = [f"{item['source_id']}: {item['error']}" for item in genuine_results if item["error"]]
            return {
                "status": overall_status,
                "started_at": started_at,
                "finished_at": now_iso(),
                "db_path": str(db_path),
                "sources": results,
                "fetched_count": sum(int(item["fetched_count"]) for item in results),
                "inserted_count": sum(int(item["inserted_count"]) for item in results),
                "duplicate_count": sum(int(item["duplicate_count"]) for item in results),
                "request_count": sum(int(item["request_count"]) for item in results),
                "saturated_window_count": sum(int(item["saturated_window_count"]) for item in results),
                "errors": genuine_errors,
                "provider": provider,
                "rate_limited_until": rate_limited_info["cooldown_until"],
                "alert": rate_limited_info["alert"],
                "recovered": False,
            }

        recovered = False
        if any(item["status"] == "success" for item in results):
            recovered = store.mark_provider_healthy(provider)

    statuses = {str(item["status"]) for item in results}
    status = (
        "success"
        if statuses == {"success"} and results
        else "partial"
        if statuses & {"success", "partial"}
        else "failed"
    )
    return {
        "status": status,
        "started_at": started_at,
        "finished_at": now_iso(),
        "db_path": str(db_path),
        "sources": results,
        "fetched_count": sum(int(item["fetched_count"]) for item in results),
        "inserted_count": sum(int(item["inserted_count"]) for item in results),
        "duplicate_count": sum(int(item["duplicate_count"]) for item in results),
        "request_count": sum(int(item["request_count"]) for item in results),
        "saturated_window_count": sum(int(item["saturated_window_count"]) for item in results),
        "errors": [f"{item['source_id']}: {item['error']}" for item in results if item["error"]],
        "provider": provider,
        "alert": False,
        "recovered": recovered,
    }
