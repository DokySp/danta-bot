"""Deterministic KIS market-news collector."""

from __future__ import annotations

import hashlib
import html
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from ..symbol_news import cli as symbol_news
from .storage import MarketNewsStore, now_iso


KST = ZoneInfo("Asia/Seoul")
KIS_PROVIDER = "kis_open_api"
SOURCE_IDS = ("domestic", "global")
MAX_PAGES = 10
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def resolve_db_path(workspace_dir: Path, value: str | Path | None = None) -> Path:
    text = str(value or "").strip()
    if text:
        path = Path(text).expanduser()
        return path if path.is_absolute() else workspace_dir / path
    memory_root = os.getenv("DAILY_TRADING_MEMORY_DIR", "").strip()
    root = Path(memory_root).expanduser() if memory_root else workspace_dir / "memory"
    return root / "market-news" / "market-news.sqlite3"


def utc_datetime(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).replace(microsecond=0)


def plain_title(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
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


def kis_datetime(date_value: Any, time_value: Any) -> str:
    date_digits = "".join(character for character in str(date_value or "") if character.isdigit())
    time_digits = "".join(character for character in str(time_value or "") if character.isdigit()).ljust(6, "0")[:6]
    if len(date_digits) != 8:
        return ""
    try:
        return (
            datetime.strptime(date_digits + time_digits, "%Y%m%d%H%M%S")
            .replace(tzinfo=KST)
            .astimezone(timezone.utc)
            .isoformat()
        )
    except ValueError:
        return ""


def parse_kis_rows(rows: list[dict[str, Any]], *, source_id: str, collected_at: str) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    for row in rows:
        title = plain_title(
            row.get("hts_pbnt_titl_cntt") if source_id == "domestic" else row.get("title")
        )
        if not title:
            continue
        provider_id = str(
            (row.get("cntt_usiq_srno") if source_id == "domestic" else row.get("news_key")) or ""
        ).strip()
        source = str((row.get("dorg") if source_id == "domestic" else row.get("source")) or "").strip()
        classification = str(
            (
                row.get("news_lrdv_code")
                if source_id == "domestic"
                else row.get("class_name") or row.get("class_cd")
            )
            or ""
        ).strip()
        articles.append(
            {
                "provider_article_id": f"{source_id}:{provider_id}" if provider_id else f"{source_id}:{title_hash(title)}",
                "canonical_url": "",
                "title_hash": title_hash(title),
                "title": title,
                "url": "",
                "domain": source,
                "source_country": "KR" if source_id == "domestic" else str(row.get("nation_cd") or "").strip(),
                "source_language": "ko" if source_id == "domestic" else "",
                "published_at": kis_datetime(row.get("data_dt"), row.get("data_tm")),
                "collected_at": collected_at,
                "classification": classification or source_id,
            }
        )
    return articles


def response_rows(body: dict[str, Any], source_id: str) -> list[dict[str, Any]]:
    value = body.get("output") if source_id == "domestic" else body.get("outblock1")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return [value] if isinstance(value, dict) else []


def fetch_kis_source(
    source_id: str,
    *,
    app_key: str,
    app_secret: str,
    token: str,
) -> tuple[list[dict[str, Any]], int]:
    if source_id == "domestic":
        path = "/uapi/domestic-stock/v1/quotations/news-title"
        tr_id = "FHKST01011800"
        params = {
            "FID_NEWS_OFER_ENTP_CODE": "",
            "FID_COND_MRKT_CLS_CODE": "",
            "FID_INPUT_ISCD": "",
            "FID_TITL_CNTT": "",
            "FID_INPUT_DATE_1": "",
            "FID_INPUT_HOUR_1": "",
            "FID_RANK_SORT_CLS_CODE": "",
            "FID_INPUT_SRNO": "",
        }
    elif source_id == "global":
        path = "/uapi/overseas-price/v1/quotations/news-title"
        tr_id = "HHPSTH60100C1"
        params = {
            "INFO_GB": "",
            "CLASS_CD": "",
            "NATION_CD": "",
            "EXCHANGE_CD": "",
            "SYMB": "",
            "DATA_DT": "",
            "DATA_TM": "",
            "CTS": "",
        }
    else:
        raise ValueError(f"unsupported market-news source: {source_id}")

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P",
    }
    rows: list[dict[str, Any]] = []
    request_count = 0
    tr_cont = ""
    for _page in range(MAX_PAGES):
        page_headers = dict(headers)
        if tr_cont:
            page_headers["tr_cont"] = tr_cont
        body, response_headers = symbol_news.retry_json(
            "GET", path, headers=page_headers, params=params, retries=3
        )
        request_count += 1
        if not symbol_news.response_success(body):
            message = body.get("msg1") or body.get("msg_cd") or body.get("rt_cd") or "KIS API failed"
            raise RuntimeError(str(message))
        rows.extend(response_rows(body, source_id))
        if response_headers.get("tr_cont", "") not in {"M", "F"}:
            break
        tr_cont = "N"
        time.sleep(0.1)
    return rows, request_count


def collect_market_news(
    *,
    db_path: Path,
    current_time: datetime | None = None,
    fetcher: Callable[[str], tuple[list[dict[str, Any]], int]] | None = None,
) -> dict[str, Any]:
    store = MarketNewsStore(db_path)
    collected_at = utc_datetime(current_time).isoformat()
    started_at = now_iso()
    results: list[dict[str, Any]] = []

    with store.acquire_run_lock() as acquired:
        if not acquired:
            return {
                "status": "skipped_locked",
                "started_at": started_at,
                "finished_at": now_iso(),
                "db_path": str(db_path),
                "sources": [],
                "errors": [],
            }

        auth_error: BaseException | None = None
        live_args: dict[str, str] = {}
        if fetcher is None:
            try:
                app_key = symbol_news.require_env("KIS_APP_KEY")
                app_secret = symbol_news.require_env("KIS_APP_SECRET")
                live_args = {
                    "app_key": app_key,
                    "app_secret": app_secret,
                    "token": symbol_news.fetch_token(app_key, app_secret, 3),
                }
            except (Exception, SystemExit) as exc:  # token/env failures apply to both sources
                auth_error = exc

        for source_id in SOURCE_IDS:
            source_started_at = now_iso()
            try:
                if auth_error is not None:
                    raise RuntimeError(str(auth_error))
                rows, request_count = (
                    fetcher(source_id) if fetcher is not None else fetch_kis_source(source_id, **live_args)
                )
                articles = parse_kis_rows(rows, source_id=source_id, collected_at=collected_at)
                upsert = store.upsert_articles(source_id, KIS_PROVIDER, articles)
                status = "success"
                error = ""
            except Exception as exc:  # one source must not discard the other
                request_count = 0
                articles = []
                upsert = None
                status = "failed"
                error = str(exc)[:500]
            result = {
                "source_id": source_id,
                "status": status,
                "window_start": collected_at,
                "window_end": collected_at,
                "fetched_count": len(articles),
                "inserted_count": upsert.inserted_count if upsert else 0,
                "duplicate_count": upsert.duplicate_count if upsert else 0,
                "request_count": request_count,
                "error": error,
            }
            store.record_run(
                source_id=source_id,
                started_at=source_started_at,
                finished_at=now_iso(),
                status=status,
                window_start=collected_at,
                window_end=collected_at,
                fetched_count=result["fetched_count"],
                inserted_count=result["inserted_count"],
                duplicate_count=result["duplicate_count"],
                error=error,
            )
            results.append(result)

    success_count = sum(item["status"] == "success" for item in results)
    status = "success" if success_count == len(SOURCE_IDS) else "partial" if success_count else "failed"
    return {
        "status": status,
        "started_at": started_at,
        "finished_at": now_iso(),
        "db_path": str(db_path),
        "sources": results,
        "fetched_count": sum(item["fetched_count"] for item in results),
        "inserted_count": sum(item["inserted_count"] for item in results),
        "duplicate_count": sum(item["duplicate_count"] for item in results),
        "request_count": sum(item["request_count"] for item in results),
        "errors": [f"{item['source_id']}: {item['error']}" for item in results if item["error"]],
        "provider": KIS_PROVIDER,
    }
