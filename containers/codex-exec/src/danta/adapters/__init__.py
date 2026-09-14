"""Explicit external boundaries. Importing adapters never reads credentials."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class AdapterError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)  # Do not leak URLs, account ids, tokens, or provider bodies.


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self):
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeError) as exc:
            raise AdapterError("MALFORMED_RESPONSE") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_transport(*, allowed_origins: set[str], network_enabled: bool = False) -> Callable:
    """Exact origin allowlist; redirects never forward secrets to another origin."""
    opener = build_opener(_NoRedirect, ProxyHandler({}))

    def request(method, url, headers=None, body=None, timeout=15):
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if not network_enabled:
            raise AdapterError("OFFLINE_NETWORK_BLOCKED")
        if origin not in allowed_origins or parsed.username or parsed.password:
            raise AdapterError("ORIGIN_NOT_ALLOWED")
        if method not in {"GET", "POST"}:
            raise AdapterError("METHOD_NOT_ALLOWED")
        try:
            with opener.open(Request(url, data=body, headers=headers or {}, method=method), timeout=timeout) as response:
                data = response.read(32 * 1024 * 1024 + 1)
                if len(data) > 32 * 1024 * 1024:
                    raise AdapterError("RESPONSE_TOO_LARGE")
                return HttpResponse(response.status, data, {k.lower(): v for k, v in response.headers.items()})
        except HTTPError as exc:
            return HttpResponse(exc.code, b"{}", {})
        except OSError:
            raise AdapterError("TRANSPORT_FAILED") from None

    return request


@dataclass(frozen=True)
class FetchResult:
    records: tuple[dict, ...]
    quality: str
    retrieved_at: datetime
    next_cursor: object = None
    metadata: dict = field(default_factory=dict)


def utcnow():
    return datetime.now(timezone.utc)


def require_http_ok(response: HttpResponse):
    if response.status in {401, 403}:
        raise AdapterError("AUTH_FAILED")
    if response.status == 429:
        raise AdapterError("RATE_LIMITED")
    if response.status >= 500:
        raise AdapterError("TRANSIENT_FAILURE")
    if response.status != 200:
        raise AdapterError("HTTP_FAILURE")
