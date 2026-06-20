#!/usr/bin/env python3
"""Shared KIS Open API OAuth token cache for codex-exec helpers."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_PATH = "/oauth2/tokenP"


class TokenResult:
    def __init__(self, *, token: str, status: str, expires_at: datetime) -> None:
        self.token = token
        self.status = status
        self.expires_at = expires_at

    @property
    def expires_at_kst(self) -> str:
        return self.expires_at.astimezone(KST).isoformat(timespec="seconds")


def normalize_env(env_dv: str | None) -> str:
    value = (env_dv or os.environ.get("CODEX_MCP_TRADING_ENV") or "acct").strip().lower()
    if value in {"paper", "demo", "mock"}:
        return "demo"
    if value in {"acct", "real"}:
        return "real"
    raise RuntimeError(f"unsupported KIS token environment: {value}")


def token_cache_dir() -> Path:
    configured = os.environ.get("KIS_TOKEN_CACHE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser() / ".cache" / "kis-token"
    return Path.home() / ".cache" / "codex" / "kis-token"


def token_cache_path(env_dv: str) -> Path:
    return token_cache_dir() / f"kis-token-{normalize_env(env_dv)}.json"


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


def cached_token(env_dv: str) -> TokenResult | None:
    path = token_cache_path(env_dv)
    if not path.exists():
        return None
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    token = str(payload.get("access_token", "")).strip()
    expires_at = parse_expiry(payload.get("expires_at"))
    if not token or expires_at is None:
        return None
    if datetime.now(timezone.utc) + timedelta(minutes=30) >= expires_at:
        return None
    return TokenResult(token=token, status="existing_token", expires_at=expires_at)


def request_json(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    payload: Any = None,
    timeout: int = 20,
) -> tuple[dict[str, Any], dict[str, str]]:
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(KIS_BASE_URL + path, data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        response_headers = {key.lower(): value for key, value in response.headers.items()}
    if not body.strip():
        return {}, response_headers
    return json.loads(body), response_headers


def retry_json(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    payload: Any = None,
    retries: int = 3,
) -> tuple[dict[str, Any], dict[str, str]]:
    delays = [1, 2, 4, 8, 16, 30, 30, 30, 30, 30]
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return request_json(method, path, headers=headers, payload=payload)
        except HTTPError as exc:
            last_error = exc
            if exc.code in {400, 401, 403, 404}:
                raise
        except (TimeoutError, URLError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(delays[min(attempt, len(delays) - 1)])
    raise RuntimeError(f"KIS token request failed after retries: {last_error}")


def get_token(app_key: str, app_secret: str, *, env_dv: str | None = None, retries: int = 3) -> TokenResult:
    normalized_env = normalize_env(env_dv)
    cached = cached_token(normalized_env)
    if cached:
        return cached
    body, _headers = retry_json(
        "POST",
        TOKEN_PATH,
        headers={"content-type": "application/json; charset=utf-8"},
        payload={"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret},
        retries=retries,
    )
    token = str(body.get("access_token", "")).strip()
    if not token:
        raise RuntimeError("KIS token response did not include access_token")
    expires_at = parse_expiry(body.get("access_token_token_expired") or body.get("expires_at"))
    if expires_at is None:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=23)
    write_json(token_cache_path(normalized_env), {"access_token": token, "expires_at": expires_at.isoformat()})
    return TokenResult(token=token, status="new_token", expires_at=expires_at)
