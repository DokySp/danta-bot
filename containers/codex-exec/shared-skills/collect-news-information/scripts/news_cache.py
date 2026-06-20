#!/usr/bin/env python3
"""Collect and retrieve KIS news YAML caches."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import yaml


KST = ZoneInfo("Asia/Seoul")
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
NEWS_PATH = "/uapi/domestic-stock/v1/quotations/news-title"
NEWS_TR_ID = "FHKST01011800"
MISSING_CACHE_MESSAGE = "해당 날짜 뉴스 캐시가 아직 생성되지 않았습니다."


class QuotedString(str):
    """YAML scalar that must be emitted with quotes."""


class NewsYamlDumper(yaml.SafeDumper):
    pass


def quoted_string_representer(dumper: yaml.Dumper, value: QuotedString) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(value), style='"')


NewsYamlDumper.add_representer(QuotedString, quoted_string_representer)


def find_repo_root(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def memory_root() -> Path:
    configured = os.environ.get("DAILY_TRADING_MEMORY_DIR")
    if configured:
        return Path(configured).expanduser()
    repo_root = find_repo_root()
    if repo_root is not None:
        return repo_root / "memory"
    return Path.cwd() / "memory"


def cache_dir() -> Path:
    configured = os.environ.get("COLLECT_NEWS_INFORMATION_MEMORY_DIR")
    if configured:
        return Path(configured).expanduser()
    return memory_root() / "collect-news-information"


def news_cache_path(date_hyphen: str) -> Path:
    return cache_dir() / f"news-{date_hyphen}.yaml"


def today_kst() -> str:
    return datetime.now(KST).date().isoformat()


def normalize_date(value: str | None) -> str:
    raw = (value or today_kst()).strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise SystemExit(f"date must be YYYY-MM-DD or YYYYMMDD: {raw!r}") from exc


def api_date(date_hyphen: str) -> str:
    return date_hyphen.replace("-", "")


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def read_yaml(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_yaml(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        yaml.dump(canonical_cache(payload), handle, Dumper=NewsYamlDumper, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def request_json(method: str, path: str, *, headers: dict[str, str], payload: Any = None, params: dict[str, str] | None = None, timeout: int = 20) -> tuple[dict[str, Any], dict[str, str]]:
    url = KIS_BASE_URL + path
    if params:
        url = url + "?" + urlencode(params)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        response_headers = {key.lower(): value for key, value in response.headers.items()}
    if not body.strip():
        return {}, response_headers
    return json.loads(body), response_headers


def retry_json(method: str, path: str, *, headers: dict[str, str], payload: Any = None, params: dict[str, str] | None = None, retries: int = 10) -> tuple[dict[str, Any], dict[str, str]]:
    delays = [1, 2, 4, 8, 16, 30, 30, 30, 30, 30]
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return request_json(method, path, headers=headers, payload=payload, params=params)
        except HTTPError as exc:
            last_error = exc
            if exc.code in {400, 401, 403, 404}:
                raise
        except (TimeoutError, URLError, OSError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(delays[min(attempt, len(delays) - 1)])
    raise RuntimeError(f"KIS request failed after retries: {last_error}")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip().strip('"')
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def parse_expiry(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=KST).astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return None


def kis_token_module_candidates() -> list[Path]:
    configured = os.environ.get("KIS_TOKEN_HELPER_PATH", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            Path("/app/skills/kis-token/scripts/kis_token.py"),
            Path("/codex-home/skills/kis-token/scripts/kis_token.py"),
            Path("/workspace/containers/codex-exec/shared-skills/kis-token/scripts/kis_token.py"),
        ]
    )
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidates.append(parent / "kis-token" / "scripts" / "kis_token.py")
        candidates.append(parent / "shared-skills" / "kis-token" / "scripts" / "kis_token.py")
    return candidates


def load_kis_token_module() -> Any:
    for path in kis_token_module_candidates():
        if path.exists():
            spec = importlib.util.spec_from_file_location("codex_kis_token", path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
    raise RuntimeError("shared kis-token helper not found")


def fetch_token(app_key: str, app_secret: str, retries: int) -> str:
    return load_kis_token_module().get_token(app_key, app_secret, env_dv="real", retries=retries).token


def value_from(item: dict[str, Any], *keys: str) -> str:
    lowered = {str(key).lower(): value for key, value in item.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return str(value).strip()
    return ""


def output_rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    output = body.get("output")
    if output is None:
        output = body.get("output1")
    if isinstance(output, list):
        return [item for item in output if isinstance(item, dict)]
    if isinstance(output, dict):
        return [output]
    return []


def response_success(body: dict[str, Any]) -> bool:
    rt_cd = str(body.get("rt_cd", "0"))
    return rt_cd in {"0", ""}


def news_params(symbol_id: str, date_hyphen: str, title: str = "", srno: str = "1") -> dict[str, str]:
    return {
        "FID_NEWS_OFER_ENTP_CODE": "2",
        "FID_COND_MRKT_CLS_CODE": "00",
        "FID_INPUT_ISCD": symbol_id,
        "FID_TITL_CNTT": title,
        "FID_INPUT_DATE_1": api_date(date_hyphen),
        "FID_INPUT_HOUR_1": "000000",
        "FID_RANK_SORT_CLS_CODE": "01",
        "FID_INPUT_SRNO": srno,
    }


def collect_symbol_news(symbol_id: str, date_hyphen: str, app_key: str, app_secret: str, token: str, retries: int, max_pages: int) -> tuple[list[dict[str, Any]], list[str]]:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": NEWS_TR_ID,
        "custtype": "P",
    }
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    tr_cont = ""
    for page in range(max_pages):
        page_headers = dict(headers)
        if tr_cont:
            page_headers["tr_cont"] = tr_cont
        try:
            body, response_headers = retry_json(
                "GET",
                NEWS_PATH,
                headers=page_headers,
                params=news_params(symbol_id, date_hyphen),
                retries=retries,
            )
        except Exception as exc:  # noqa: BLE001 - non-sensitive collection error
            errors.append(str(exc))
            break
        if not response_success(body):
            msg = body.get("msg1") or body.get("msg_cd") or body.get("rt_cd") or "api_failure"
            errors.append(str(msg))
            break
        rows.extend(output_rows(body))
        next_cont = response_headers.get("tr_cont", "")
        if next_cont != "M":
            break
        tr_cont = "N"
        if page + 1 >= max_pages:
            errors.append("max_pages_reached")
    return rows, errors


def parse_symbol(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        raise SystemExit("--symbol must not be empty")
    if ":" in raw:
        symbol_id, symbol_name = raw.split(":", 1)
    elif "," in raw:
        symbol_id, symbol_name = raw.split(",", 1)
    else:
        symbol_id, symbol_name = raw, raw
    symbol_id = symbol_id.strip()
    symbol_name = symbol_name.strip() or symbol_id
    if not symbol_id:
        raise SystemExit(f"invalid symbol: {value!r}")
    return symbol_id, symbol_name


def parse_symbols_list(value: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for item in value.replace("\n", ",").split(","):
        item = item.strip()
        if item:
            result.append(parse_symbol(item))
    return result


def load_symbols(args: argparse.Namespace) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for item in args.symbols or []:
        result.extend(parse_symbols_list(item))
    for item in args.symbol or []:
        result.append(parse_symbol(item))
    if args.symbols_file:
        path = Path(args.symbols_file)
        for line in path.read_text(encoding="utf-8").splitlines():
            for part in line.split(","):
                part = part.strip()
                if part:
                    result.append(parse_symbol(part))
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for symbol_id, symbol_name in result:
        if symbol_id in seen:
            continue
        seen.add(symbol_id)
        unique.append((symbol_id, symbol_name))
    if not unique:
        raise SystemExit("at least one --symbols, --symbol, or --symbols-file entry is required")
    return unique


def sentiment_for(text: str) -> str:
    positive = ("상승", "호재", "수주", "실적 개선", "흑자", "증가", "강세", "목표가 상향", "매수", "계약")
    negative = ("하락", "악재", "적자", "감소", "약세", "목표가 하향", "매도", "소송", "리콜", "손실")
    has_pos = any(word in text for word in positive)
    has_neg = any(word in text for word in negative)
    if has_pos and has_neg:
        return "mixed"
    if has_pos:
        return "positive"
    if has_neg:
        return "negative"
    return "neutral"


def normalize_sentiment(value: Any) -> str:
    mapping = {
        "긍정": "positive",
        "중립": "neutral",
        "부정": "negative",
        "혼합": "mixed",
        "positive": "positive",
        "neutral": "neutral",
        "negative": "negative",
        "mixed": "mixed",
    }
    return mapping.get(str(value).strip(), "neutral")


def normalize_symbol_key(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits and digits == text:
        return digits.zfill(6)
    return text


def canonical_article(raw_article: Any) -> dict[str, str]:
    if not isinstance(raw_article, dict):
        raw_article = {}
    return {
        "article_date": str(raw_article.get("article_date") or ""),
        "sentiment": normalize_sentiment(raw_article.get("sentiment")),
        "content": str(raw_article.get("content") or ""),
    }


def canonical_symbol_payload(raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    payload: dict[str, Any] = {}
    symbol_name = raw_payload.get("symbol_name")
    if symbol_name not in (None, ""):
        payload["symbol_name"] = str(symbol_name)
    raw_articles = raw_payload.get("articles")
    articles = raw_articles if isinstance(raw_articles, list) else []
    payload["articles"] = [canonical_article(article) for article in articles]
    return payload


def canonical_cache(raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    raw_symbols = raw_payload.get("symbols")
    symbols: dict[QuotedString, dict[str, Any]] = {}
    if isinstance(raw_symbols, dict):
        for raw_symbol_id, raw_symbol_payload in sorted(raw_symbols.items(), key=lambda item: normalize_symbol_key(item[0])):
            symbol_id = normalize_symbol_key(raw_symbol_id)
            if not symbol_id:
                continue
            symbols[QuotedString(symbol_id)] = canonical_symbol_payload(raw_symbol_payload)
    return {
        "date": str(raw_payload.get("date") or ""),
        "source": str(raw_payload.get("source") or "kis_open_api"),
        "symbols": symbols,
    }


def row_date(item: dict[str, Any]) -> str:
    date = value_from(item, "data_dt", "news_dt", "date", "dt", "cntt_dt", "stck_bsop_date")
    time_value = value_from(item, "data_tm", "news_tm", "time", "tm", "cntt_tm")
    if not date:
        return ""
    clean_date = "".join(ch for ch in date if ch.isdigit())
    if len(clean_date) == 8:
        if time_value:
            clean_time = "".join(ch for ch in time_value if ch.isdigit()).ljust(6, "0")[:6]
            return f"{clean_date[0:4]}-{clean_date[4:6]}-{clean_date[6:8]}T{clean_time[0:2]}:{clean_time[2:4]}:{clean_time[4:6]}+09:00"
        return f"{clean_date[0:4]}-{clean_date[4:6]}-{clean_date[6:8]}"
    return date


def row_date_digits(item: dict[str, Any]) -> str:
    date = value_from(item, "data_dt", "news_dt", "date", "dt", "cntt_dt", "stck_bsop_date")
    return "".join(ch for ch in date if ch.isdigit())[:8]


def row_title(item: dict[str, Any]) -> str:
    return value_from(
        item,
        "hts_pbnt_titl_cntt",
        "hts_titl_cntt",
        "title",
        "news_titl",
        "titl",
        "cntt",
    )


def row_matches_symbol(item: dict[str, Any], symbol_id: str, symbol_name: str) -> bool:
    for key, value in item.items():
        key_text = str(key).lower()
        value_text = str(value)
        if key_text.startswith("iscd") and value_text == symbol_id:
            return True
    title = row_title(item)
    return bool(symbol_name and symbol_name != symbol_id and symbol_name in title)


def kis_symbol_name(rows: list[dict[str, Any]], symbol_id: str) -> str | None:
    for item in rows:
        lowered = {str(key).lower(): value for key, value in item.items()}
        for key, value in lowered.items():
            if not key.startswith("iscd") or str(value).strip() != symbol_id:
                continue
            suffix = key[4:]
            name = lowered.get(f"kor_isnm{suffix}")
            if name not in (None, ""):
                return str(name).strip()
    return None


def row_text(item: dict[str, Any]) -> str:
    candidates = [
        value_from(item, "hts_pbnt_titl_cntt", "hts_titl_cntt", "title", "news_titl", "titl", "cntt", "body", "news_cntt"),
        " | ".join(f"{key}={value}" for key, value in item.items() if value not in (None, "")),
    ]
    for candidate in candidates:
        if candidate.strip():
            text = " ".join(candidate.split())
            return text[:500]
    return "뉴스 내용 없음"


def article_payload(item: dict[str, Any]) -> dict[str, Any]:
    text = row_text(item)
    return {
        "article_date": row_date(item),
        "sentiment": sentiment_for(text),
        "content": text,
    }


def no_article_payload(date_hyphen: str) -> dict[str, Any]:
    return {
        "article_date": "",
        "sentiment": "neutral",
        "content": f"{date_hyphen} 기준 수집된 뉴스가 없습니다.",
    }


def symbol_payload(date_hyphen: str, symbol_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if rows:
        articles = [article_payload(item) for item in rows]
    else:
        articles = [no_article_payload(date_hyphen)]
    payload: dict[str, Any] = {}
    name = kis_symbol_name(rows, symbol_id)
    if name:
        payload["symbol_name"] = name
    payload["articles"] = articles
    return payload


def empty_cache(date_hyphen: str) -> dict[str, Any]:
    return {
        "date": date_hyphen,
        "source": "kis_open_api",
        "symbols": {},
    }


def load_existing_cache(path: Path, date_hyphen: str) -> dict[str, Any]:
    if not path.exists():
        return empty_cache(date_hyphen)
    payload = read_yaml(path)
    if not isinstance(payload, dict):
        raise SystemExit(f"invalid cache YAML: {path}")
    if str(payload.get("date", "")) not in {"", date_hyphen}:
        raise SystemExit(f"cache date mismatch: {path}")
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict):
        payload["symbols"] = {}
    payload["date"] = date_hyphen
    payload.setdefault("source", "kis_open_api")
    payload.pop("updated_at", None)
    payload.pop("title", None)
    payload.pop("symbol_id", None)
    payload.pop("errors", None)
    normalize_existing_symbols(payload["symbols"])
    return payload


def normalize_existing_symbols(symbols: dict[str, Any]) -> None:
    normalized_symbols: dict[str, Any] = {}
    for raw_symbol_id, raw_payload in list(symbols.items()):
        symbol_id = normalize_symbol_key(raw_symbol_id)
        if not symbol_id:
            continue
        if not isinstance(raw_payload, dict):
            normalized_symbols[symbol_id] = {"articles": []}
            continue
        normalized: dict[str, Any] = {}
        articles = raw_payload.get("articles")
        normalized_articles = []
        if isinstance(articles, list):
            for item in articles:
                if not isinstance(item, dict):
                    continue
                normalized_articles.append(
                    {
                        "article_date": str(item.get("article_date") or ""),
                        "sentiment": normalize_sentiment(item.get("sentiment")),
                        "content": str(item.get("content") or ""),
                    }
                )
        normalized["articles"] = normalized_articles
        normalized_symbols[symbol_id] = normalized
    symbols.clear()
    symbols.update(normalized_symbols)


def merge_cache(date_hyphen: str, path: Path, symbol_rows: list[tuple[str, str, list[dict[str, Any]], list[str]]]) -> dict[str, Any]:
    payload = load_existing_cache(path, date_hyphen)
    symbols = payload.setdefault("symbols", {})
    for symbol_id, _symbol_name, rows, _errors in symbol_rows:
        normalized_symbol_id = normalize_symbol_key(symbol_id)
        symbols[normalized_symbol_id] = symbol_payload(date_hyphen, normalized_symbol_id, rows)
    return payload


def filter_rows(rows: list[dict[str, Any]], symbol_id: str, symbol_name: str, date_hyphen: str) -> list[dict[str, Any]]:
    target_date = api_date(date_hyphen)
    dated = [item for item in rows if row_date_digits(item) == target_date]
    matched = [item for item in dated if row_matches_symbol(item, symbol_id, symbol_name)]
    return matched or dated


def command_get(args: argparse.Namespace) -> int:
    date_hyphen = normalize_date(args.date)
    path = news_cache_path(date_hyphen)
    if not path.exists():
        print(MISSING_CACHE_MESSAGE)
        return 1
    print(path)
    return 0


def command_collect(args: argparse.Namespace) -> int:
    date_hyphen = normalize_date(args.date)
    symbols = load_symbols(args)
    app_key = require_env("KIS_APP_KEY")
    app_secret = require_env("KIS_APP_SECRET")
    token = fetch_token(app_key, app_secret, args.retries)
    collected = []
    for symbol_id, symbol_name in symbols:
        rows, errors = collect_symbol_news(
            symbol_id,
            date_hyphen,
            app_key,
            app_secret,
            token,
            args.retries,
            args.max_pages,
        )
        collected.append((symbol_id, symbol_name, filter_rows(rows, symbol_id, symbol_name, date_hyphen), errors))
    path = news_cache_path(date_hyphen)
    output = merge_cache(date_hyphen, path, collected)
    write_yaml(path, output)
    print(path)
    return 0


def command_self_test(_args: argparse.Namespace) -> int:
    date_hyphen = normalize_date("20260610")
    assert date_hyphen == "2026-06-10"
    assert news_cache_path(date_hyphen).name == "news-2026-06-10.yaml"
    item = {
        "hts_titl_cntt": "삼성전자 수주 증가",
        "data_dt": "20260610",
        "data_tm": "093000",
        "iscd1": "005930",
        "kor_isnm1": "삼성전자",
    }
    assert row_date(item) == "2026-06-10T09:30:00+09:00"
    assert sentiment_for(row_text(item)) == "positive"
    cache = merge_cache(date_hyphen, Path("/tmp/nonexistent-news-cache.yaml"), [("005930", "삼성전자", [item], [])])
    assert list(cache["symbols"]["005930"].keys()) == ["symbol_name", "articles"]
    assert set(cache["symbols"]["005930"]["articles"][0]) == {"article_date", "sentiment", "content"}
    assert cache["symbols"]["005930"]["articles"][0]["article_date"] == "2026-06-10T09:30:00+09:00"
    assert cache["symbols"]["005930"]["articles"][0]["sentiment"] == "positive"
    assert cache["symbols"]["005930"]["symbol_name"] == "삼성전자"
    assert "symbol_id" not in cache["symbols"]["005930"]
    assert "updated_at" not in cache["symbols"]["005930"]
    assert "errors" not in cache["symbols"]["005930"]
    assert "updated_at" not in cache
    existing = {
        "date": date_hyphen,
        "source": "kis_open_api",
        "updated_at": "old",
        "title": "old",
        "symbol_id": "old",
        "errors": ["old"],
        "symbols": {
            660: {
                "symbol_id": "000660",
                "symbol_name": "000660",
                "updated_at": "old",
                "articles": [{"title": "기사", "article_date": None, "sentiment": "중립", "content": "old"}],
                "errors": ["old"],
            },
            "005930": {"symbol_name": "OLD", "articles": []},
        },
    }
    temp = Path(os.environ.get("TMPDIR", "/tmp")) / "collect-news-information-self-test.yaml"
    write_yaml(temp, existing)
    merged = merge_cache(date_hyphen, temp, [("005930", "삼성전자", [item], [])])
    assert "000660" in merged["symbols"]
    assert "005930" in merged["symbols"]
    assert merged["symbols"]["005930"]["symbol_name"] == "삼성전자"
    assert "symbol_name" not in merged["symbols"]["000660"]
    assert set(merged["symbols"]["000660"]["articles"][0]) == {"article_date", "sentiment", "content"}
    assert merged["symbols"]["000660"]["articles"][0]["article_date"] == ""
    assert merged["symbols"]["000660"]["articles"][0]["sentiment"] == "neutral"
    assert "updated_at" not in merged
    assert "title" not in merged
    assert "symbol_id" not in merged
    assert "errors" not in merged
    assert "symbol_id" not in merged["symbols"]["000660"]
    assert "updated_at" not in merged["symbols"]["000660"]
    assert "errors" not in merged["symbols"]["000660"]
    canonical = canonical_cache(merged)
    assert list(canonical.keys()) == ["date", "source", "symbols"]
    assert all(isinstance(symbol_id, QuotedString) for symbol_id in canonical["symbols"])
    assert list(canonical["symbols"]["005930"].keys()) == ["symbol_name", "articles"]
    assert list(canonical["symbols"]["000660"].keys()) == ["articles"]
    write_yaml(temp, merged)
    written = temp.read_text(encoding="utf-8")
    assert '  "000660":' in written
    assert '  "005930":' in written
    symbol_block = written[written.index('  "005930":') :]
    assert symbol_block.index("    symbol_name: 삼성전자") < symbol_block.index("    articles:")
    temp.unlink(missing_ok=True)
    namespace = argparse.Namespace(symbols=["005930,000660"], symbol=None, symbols_file=None)
    assert load_symbols(namespace) == [("005930", "005930"), ("000660", "000660")]
    print("self-test ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and retrieve KIS news YAML caches.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    get_parser = subparsers.add_parser("get", help="Return the date cache path.")
    get_parser.add_argument("--date", help="Target date in YYYY-MM-DD or YYYYMMDD. Defaults to today in Asia/Seoul.")
    get_parser.set_defaults(func=command_get)

    collect_parser = subparsers.add_parser("collect", help="Collect news and write the date cache.")
    collect_parser.add_argument("--date", help="Target date in YYYY-MM-DD or YYYYMMDD. Defaults to today in Asia/Seoul.")
    collect_parser.add_argument("--symbols", action="append", help="Comma-separated symbol code list. Can be repeated.")
    collect_parser.add_argument("--symbol", action="append", help="Symbol as CODE or CODE:NAME. Can be repeated.")
    collect_parser.add_argument("--symbols-file", help="Comma/newline separated symbol list.")
    collect_parser.add_argument("--retries", type=int, default=3, help="Retry count per KIS request.")
    collect_parser.add_argument("--max-pages", type=int, default=1, help="Maximum KIS continuation pages per symbol.")
    collect_parser.set_defaults(func=command_collect)

    self_test_parser = subparsers.add_parser("self-test", help="Run local deterministic tests.")
    self_test_parser.set_defaults(func=command_self_test)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
