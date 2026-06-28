from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


GOOGLE_FINANCE_URLS = {
    "SP500": "https://www.google.com/finance/quote/.INX:INDEXSP",
    "NASDAQ": "https://www.google.com/finance/quote/.IXIC:INDEXNASDAQ",
    "DOW": "https://www.google.com/finance/quote/.DJI:INDEXDJX",
}
DISPLAY_NAMES = {
    "SP500": "S&P 500",
    "NASDAQ": "Nasdaq",
    "DOW": "Dow",
    "KOSPI": "KOSPI",
    "KOSDAQ": "KOSDAQ",
}
DEFAULT_INDEXES = ("SP500", "NASDAQ", "DOW", "KOSPI", "KOSDAQ")


@dataclass(frozen=True)
class IndexQuote:
    symbol: str
    name: str
    source: str
    status: str
    value: float | None
    change_percent: float | None
    observed_at: str
    market_status: str
    error: str = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text:
        return None
    text = text.replace("\u2212", "-")
    try:
        return float(text)
    except ValueError:
        return None


def first_match(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.S)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def google_observed_at(raw: str) -> str:
    timestamp = parse_float(raw)
    if timestamp is None:
        return now_iso()
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(microsecond=0).isoformat()
    except (OverflowError, OSError, ValueError):
        return now_iso()


def fetch_google_finance_index(symbol: str) -> IndexQuote:
    normalized = symbol.upper()
    url = GOOGLE_FINANCE_URLS[normalized]
    request = Request(url, headers={"User-Agent": "codex-exec/market-index-snapshot"})
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"Google Finance quote failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Google Finance quote failed: {exc}") from exc

    value = parse_float(
        first_match(
            body,
            (
                r'data-last-price="([^"]+)"',
                r'"last_price":\s*"([^"]+)"',
                r'\bYMlKec fxKbKc">([^<]+)<',
            ),
        )
    )
    if value is None:
        raise RuntimeError(f"Google Finance quote did not include a numeric price for {normalized}")
    change_percent = parse_float(
        first_match(
            body,
            (
                r'data-last-price-change-percent="([^"]+)"',
                r'"last_price_change_percent":\s*"([^"]+)"',
                r'\(([-+\u2212]?[0-9.,]+%)\)',
            ),
        )
    )
    observed_at = google_observed_at(
        first_match(
            body,
            (
                r'data-last-normal-market-timestamp="([^"]+)"',
                r'"last_normal_market_timestamp":\s*"([^"]+)"',
            ),
        )
    )
    return IndexQuote(
        symbol=normalized,
        name=DISPLAY_NAMES[normalized],
        source="google_finance",
        status="success",
        value=value,
        change_percent=change_percent,
        observed_at=observed_at,
        market_status="latest_available",
    )


def fetch_kis_index(symbol: str) -> IndexQuote:
    from price_monitoring.price_monitoring_quotes import fetch_kis_domestic_index

    quote = fetch_kis_domestic_index(symbol, config=None)
    normalized = symbol.upper()
    return IndexQuote(
        symbol=normalized,
        name=quote.name or DISPLAY_NAMES.get(normalized, normalized),
        source="kis_domestic_index",
        status="success",
        value=quote.value,
        change_percent=quote.session_change_percent,
        observed_at=quote.observed_at,
        market_status=quote.market_status or "",
    )


def unavailable_index(symbol: str, source: str, exc: Exception) -> IndexQuote:
    normalized = symbol.upper()
    return IndexQuote(
        symbol=normalized,
        name=DISPLAY_NAMES.get(normalized, normalized),
        source=source,
        status="unavailable",
        value=None,
        change_percent=None,
        observed_at=now_iso(),
        market_status="unavailable",
        error=str(exc)[:300],
    )


def collect_index(symbol: str) -> IndexQuote:
    normalized = symbol.upper()
    if normalized in GOOGLE_FINANCE_URLS:
        try:
            return fetch_google_finance_index(normalized)
        except Exception as exc:  # noqa: BLE001 - unavailable quote is non-blocking context
            return unavailable_index(normalized, "google_finance", exc)
    if normalized in {"KOSPI", "KOSDAQ"}:
        try:
            return fetch_kis_index(normalized)
        except Exception as exc:  # noqa: BLE001 - unavailable quote is non-blocking context
            return unavailable_index(normalized, "kis_domestic_index", exc)
    return unavailable_index(normalized, "unsupported", RuntimeError("unsupported index"))


def collect_market_index_snapshot(
    *,
    run_id: str,
    started_at: str,
    indexes: tuple[str, ...] = DEFAULT_INDEXES,
) -> dict[str, Any]:
    collected = [collect_index(symbol) for symbol in indexes]
    success_count = sum(1 for item in collected if item.status == "success")
    status = "success" if success_count == len(collected) else "partial" if success_count else "failed"
    errors = [
        {"symbol": item.symbol, "source": item.source, "message": item.error}
        for item in collected
        if item.status != "success" and item.error
    ]
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_iso(),
        "status": status,
        "indexes": [asdict(item) for item in collected],
        "warnings": [] if status == "success" else ["one_or_more_market_indexes_unavailable"],
        "errors": errors,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def status_label(change_percent: float | None) -> str:
    if change_percent is None:
        return "확인불가"
    if change_percent > 0.1:
        return "상승"
    if change_percent < -0.1:
        return "하강"
    return "보합"


def render_markdown(payload: dict[str, Any]) -> str:
    names = {"SP500": "S&P 500", "NASDAQ": "Nasdaq", "DOW": "Dow", "KOSPI": "코스피", "KOSDAQ": "코스닥"}
    lines: list[str] = []
    for item in payload.get("indexes", []):
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "")
        display = names.get(symbol, str(item.get("name") or symbol))
        change = item.get("change_percent")
        label = status_label(parse_float(change))
        percent = "확인불가" if change is None else f"{float(change):+.2f}%"
        if item.get("status") != "success":
            label = "확인불가"
            percent = "확인불가"
        source = item.get("source") or ""
        observed = item.get("observed_at") or ""
        lines.extend(
            [
                f"{display}:",
                f"- 상태: {label}",
                f"- 등락률: {percent}",
                f"- 의견: {source} 기준 최신 관측값입니다. 관측시각: {observed}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"

