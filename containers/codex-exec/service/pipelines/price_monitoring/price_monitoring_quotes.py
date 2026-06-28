import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from time import sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .price_monitoring_models import Quote
from .price_monitoring_util import parse_float


NAVER_INDEX_URL = "https://polling.finance.naver.com/api/realtime/domestic/index/{symbol}"
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
KIS_INDEX_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-index-price"
KIS_INDEX_PRICE_TR_ID = "FHPUP02100000"
KIS_INDEX_CODES = {
    "KOSPI": "0001",
    "KOSDAQ": "1001",
    "KOSPI200": "2001",
}


def fetch_quote(trigger: Any, config: Any) -> Quote:
    if trigger.source == "kis_domestic_index":
        return fetch_kis_domestic_index(trigger.symbol, config)
    if trigger.source == "naver_domestic_index":
        return fetch_naver_domestic_index(trigger.symbol)
    raise ValueError(f"{trigger.trigger_id}: unsupported source: {trigger.source}")


def fetch_kis_domestic_index(symbol: str, config: Any) -> Quote:
    app_key, app_secret = kis_credentials()
    token = fetch_kis_token(app_key, app_secret, config)
    index_code = KIS_INDEX_CODES.get(symbol.upper(), symbol)
    body = kis_request_json(
        "GET",
        KIS_INDEX_PRICE_PATH,
        headers={
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": KIS_INDEX_PRICE_TR_ID,
            "custtype": "P",
        },
        params={
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": index_code,
        },
    )
    if not kis_response_success(body):
        message = body.get("msg1") or body.get("msg_cd") or body.get("rt_cd") or "api_failure"
        raise RuntimeError(f"KIS index quote failed: {message}")

    row = first_output_row(body)
    value = first_float(
        row,
        (
            "bstp_nmix_prpr",
            "bstp_nmix_prpr_prc",
            "stck_prpr",
            "close",
        ),
    )
    if value is None:
        raise RuntimeError(f"KIS index quote did not include a numeric price for {symbol}")
    observed_at = kis_observed_at(row)
    return Quote(
        symbol=symbol.upper(),
        name=str(row.get("bstp_kor_isnm") or row.get("hts_kor_isnm") or symbol.upper()),
        value=value,
        observed_at=observed_at,
        market_status=str(row.get("mrkt_trtm_cls_name") or row.get("market_status") or "")
        or None,
        session_change_percent=first_float(
            row,
            (
                "bstp_nmix_prdy_ctrt",
                "bstp_nmix_prdy_ctrt_rate",
                "prdy_ctrt",
                "fluctuationsRatio",
            ),
        ),
    )


def kis_credentials() -> tuple[str, str]:
    app_key = os.environ.get("KIS_APP_KEY", "").strip().strip('"')
    app_secret = os.environ.get("KIS_APP_SECRET", "").strip().strip('"')
    if not app_key:
        raise RuntimeError("KIS_APP_KEY is required for kis_domestic_index price triggers")
    if not app_secret:
        raise RuntimeError("KIS_APP_SECRET is required for kis_domestic_index price triggers")
    return app_key, app_secret


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
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                return module
    raise RuntimeError("shared kis-token helper not found")


def fetch_kis_token(app_key: str, app_secret: str, config: Any) -> str:
    return load_kis_token_module().get_token(app_key, app_secret, env_dv="real").token


def kis_request_json(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    payload: Any = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = KIS_BASE_URL + path
    if params:
        url = url + "?" + urlencode(params)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    delays = [1, 2, 4]
    last_error: Exception | None = None
    for attempt in range(len(delays) + 1):
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8")
            if not body.strip():
                return {}
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                raise RuntimeError("KIS response must be a JSON object")
            return parsed
        except HTTPError as exc:
            last_error = exc
            if exc.code in {400, 401, 403, 404}:
                raw = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"KIS request failed: HTTP {exc.code}: {raw}") from exc
        except (TimeoutError, URLError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < len(delays):
            sleep(delays[attempt])
    raise RuntimeError(f"KIS request failed after retries: {last_error}")


def kis_response_success(body: dict[str, Any]) -> bool:
    rt_cd = str(body.get("rt_cd", "0"))
    return rt_cd in {"0", ""}


def first_output_row(body: dict[str, Any]) -> dict[str, Any]:
    output = body.get("output")
    if isinstance(output, dict):
        return output
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict):
                return item
    raise RuntimeError("KIS index quote returned no output row")


def first_float(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = parse_float(row.get(key))
        if value is not None:
            return value
    return None


def kis_observed_at(row: dict[str, Any]) -> str:
    date = str(row.get("stck_bsop_date") or row.get("bsop_date") or "").strip()
    time_text = str(row.get("stck_cntg_hour") or row.get("cntg_hour") or "").strip()
    if len(date) == 8 and date.isdigit():
        if len(time_text) >= 6 and time_text[:6].isdigit():
            return (
                f"{date[0:4]}-{date[4:6]}-{date[6:8]}T"
                f"{time_text[0:2]}:{time_text[2:4]}:{time_text[4:6]}+09:00"
            )
        return f"{date[0:4]}-{date[4:6]}-{date[6:8]}"
    return datetime.now().astimezone().isoformat(timespec="seconds")


def fetch_naver_domestic_index(symbol: str) -> Quote:
    url = NAVER_INDEX_URL.format(symbol=symbol)
    request = Request(url, headers={"User-Agent": "codex-exec/price-trigger"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"naver index quote failed: HTTP {exc.code}: {raw}") from exc
    except URLError as exc:
        raise RuntimeError(f"naver index quote failed: {exc}") from exc

    datas = payload.get("datas")
    if not isinstance(datas, list) or not datas:
        raise RuntimeError(f"naver index quote returned no data for {symbol}")
    item = datas[0]
    if not isinstance(item, dict):
        raise RuntimeError(f"naver index quote returned malformed data for {symbol}")

    value = parse_float(item.get("closePriceRaw") or item.get("closePrice"))
    if value is None:
        raise RuntimeError(f"naver index quote did not include a numeric price for {symbol}")
    observed_at = str(item.get("localTradedAt") or payload.get("time") or datetime.now().isoformat())
    return Quote(
        symbol=str(item.get("symbolCode") or symbol),
        name=str(item.get("stockName") or symbol),
        value=value,
        observed_at=observed_at,
        market_status=str(item.get("marketStatus")) if item.get("marketStatus") else None,
        session_change_percent=parse_float(
            item.get("fluctuationsRatioRaw") or item.get("fluctuationsRatio")
        ),
    )
