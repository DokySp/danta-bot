#!/usr/bin/env python3
"""Collect daily-trading main evidence through direct KIS REST calls."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
ACCOUNT_ASSET_HISTORY_RELATIVE_PATH = Path("memory") / "account-assets" / "account-assets.jsonl"
ACCOUNT_ASSET_ALLOWLIST = (
    "tot_asst_amt",
    "tot_dncl_amt",
    "evlu_amt_smtl",
    "pchs_amt_smtl",
    "evlu_pfls_amt_smtl",
    "ovrs_stck_evlu_amt1",
)

ENDPOINTS = {
    "search_stock_info": {
        "path": "/uapi/domestic-stock/v1/quotations/search-stock-info",
        "tr_id": "CTPF1002R",
    },
    "inquire_price": {
        "path": "/uapi/domestic-stock/v1/quotations/inquire-price",
        "tr_id": "FHKST01010100",
    },
    "inquire_daily_itemchartprice": {
        "path": "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        "tr_id": "FHKST03010100",
    },
    "inquire_time_itemchartprice": {
        "path": "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
        "tr_id": "FHKST03010200",
    },
    "inquire_asking_price_exp_ccn": {
        "path": "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        "tr_id": "FHKST01010200",
    },
    "inquire_ccnl": {
        "path": "/uapi/domestic-stock/v1/quotations/inquire-ccnl",
        "tr_id": "FHKST01010300",
    },
    "inquire_investor": {
        "path": "/uapi/domestic-stock/v1/quotations/inquire-investor",
        "tr_id": "FHKST01010900",
    },
    "inquire_balance": {
        "path": "/uapi/domestic-stock/v1/trading/inquire-balance",
        "tr_id_real": "TTTC8434R",
        "tr_id_demo": "VTTC8434R",
    },
    "inquire_daily_ccld": {
        "path": "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
        "tr_id_real": "TTTC8001R",
        "tr_id_demo": "VTTC8001R",
    },
}

SENSITIVE_KEYS = {
    "authorization",
    "appkey",
    "appsecret",
    "access_token",
    "token",
    "cano",
    "acnt_prdt_cd",
    "account",
    "account_number",
    "htsid",
    "my_htsid",
}


def now_kst_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


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


def request_json(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    payload: Any = None,
    params: dict[str, str] | None = None,
    timeout: int = 20,
) -> tuple[dict[str, Any], dict[str, str]]:
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


def retry_json(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    payload: Any = None,
    params: dict[str, str] | None = None,
    retries: int = 3,
) -> tuple[dict[str, Any], dict[str, str]]:
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
        raise RuntimeError(f"{name} is required")
    return value


def normalize_trading_env(raw: str | None) -> str:
    value = (raw or os.environ.get("CODEX_MCP_TRADING_ENV") or "acct").strip().lower()
    if value in {"paper", "demo", "mock"}:
        return "demo"
    if value in {"acct", "real"}:
        return "real"
    raise RuntimeError(f"unsupported trading env: {value}")


def kis_credentials(env_dv: str) -> tuple[str, str]:
    if env_dv == "demo":
        return require_env("KIS_PAPER_APP_KEY"), require_env("KIS_PAPER_APP_SECRET")
    return require_env("KIS_APP_KEY"), require_env("KIS_APP_SECRET")


def account_parts(env_dv: str) -> tuple[str, str]:
    account = require_env("KIS_PAPER_STOCK" if env_dv == "demo" else "KIS_ACCT_STOCK")
    product = os.environ.get("KIS_PROD_TYPE", "").strip().strip('"') or "01"
    compact = re.sub(r"[^0-9]", "", account)
    if len(compact) >= 10:
        return compact[:-2], compact[-2:]
    if len(compact) == 8:
        return compact, product
    raise RuntimeError("KIS stock account must be 8 digits, or account+product code digits")


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


def fetch_token(app_key: str, app_secret: str, env_dv: str, retries: int) -> tuple[str, str, str]:
    result = load_kis_token_module().get_token(app_key, app_secret, env_dv=env_dv, retries=retries)
    return result.token, result.status, "" if result.status == "existing_token" else result.expires_at_kst


def response_success(body: dict[str, Any]) -> bool:
    rt_cd = str(body.get("rt_cd", "0"))
    return rt_cd in {"0", ""}


def safe_error(exc: BaseException | str, *, code: str = "api_error", stage: str = "", symbol_id: str = "", source: str = "", required: bool = True) -> dict[str, Any]:
    text = str(exc)
    for key in SENSITIVE_KEYS:
        text = re.sub(rf"(?i){re.escape(key)}[=:]\S+", f"{key}=<redacted>", text)
    return {
        "stage": stage,
        "symbol_id": symbol_id,
        "source": source,
        "code": code,
        "message": text[:400],
        "required": required,
    }


def normalize_output(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def output_first(body: dict[str, Any], key: str = "output") -> dict[str, Any]:
    rows = normalize_output(body.get(key))
    return rows[0] if rows else {}


def normalize_symbol_key(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits and digits == text:
        return digits.zfill(6)
    return text


def parse_symbols(value: str) -> list[str]:
    symbols: list[str] = []
    for item in value.replace("\n", ",").split(","):
        symbol = normalize_symbol_key(item.strip())
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise RuntimeError("--symbols must include at least one symbol")
    return symbols


def parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).replace(",", "").strip()
    if text in {"", "-"}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).replace(",", "").strip()
    if text in {"", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def text_first(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    value = first_present(row, keys)
    return str(value).strip() if value not in (None, "") else ""


def is_etf_or_etn(info: dict[str, Any], price: dict[str, Any]) -> bool:
    group = str(info.get("scty_grp_id_cd") or "").strip().upper()
    etf_code = str(info.get("etf_dvsn_cd") or "").strip()
    product_name = text_first(info, ("prdt_abrv_name", "prdt_name", "prdt_name120")).upper()
    market_name = text_first(price, ("rprs_mrkt_kor_name",)).upper()
    return group == "EF" or etf_code not in {"", "0", "00"} or "ETF" in product_name or "ETN" in product_name or "ETF" in market_name


def product_type(info: dict[str, Any], price: dict[str, Any]) -> str:
    name = text_first(info, ("prdt_abrv_name", "prdt_name", "prdt_name120")).upper()
    if "ETN" in name:
        return "etn"
    if is_etf_or_etn(info, price):
        return "etf"
    if info or price:
        return "stock"
    return "unresolved"


def price_signal(name: str, value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    return {"name": name, "value": value}


def compact_ohlcv_bar(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": text_first(row, ("stck_bsop_date", "bsop_date", "date")),
        "open": parse_int(first_present(row, ("stck_oprc", "oprc", "open"))),
        "high": parse_int(first_present(row, ("stck_hgpr", "hgpr", "high"))),
        "low": parse_int(first_present(row, ("stck_lwpr", "lwpr", "low"))),
        "close": parse_int(first_present(row, ("stck_clpr", "clpr", "close", "stck_prpr"))),
        "volume": parse_int(first_present(row, ("acml_vol", "cntg_vol", "vol"))),
        "trading_value": parse_int(first_present(row, ("acml_tr_pbmn", "tr_pbmn"))),
    }


def compact_intraday_bar(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": text_first(row, ("stck_cntg_hour", "cntg_hour", "time")),
        "price": parse_int(first_present(row, ("stck_prpr", "stck_clpr", "price"))),
        "volume": parse_int(first_present(row, ("cntg_vol", "acml_vol", "vol"))),
    }


def output_rows_from_body(body: dict[str, Any], preferred_key: str) -> list[dict[str, Any]]:
    rows = normalize_output(body.get(preferred_key))
    if rows:
        return rows
    for key in ("output2", "output1", "output"):
        rows = normalize_output(body.get(key))
        if rows:
            return rows
    return []


def trim_compact_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return [row for row in rows if any(value not in (None, "") for value in row.values())][:limit]


def pct_change(start: int | float | None, end: int | float | None) -> float | None:
    if start in (None, 0) or end is None:
        return None
    return round(((float(end) - float(start)) / float(start)) * 100, 2)


def avg(values: list[int | float | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def chart_signal_summary(charts: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for timeframe, rows in charts.items():
        if not rows:
            continue
        chronological = list(reversed(rows))
        closes = [row.get("close") for row in chronological if row.get("close") is not None]
        volumes = [row.get("volume") for row in chronological if row.get("volume") is not None]
        latest_close = closes[-1] if closes else None
        if len(closes) >= 2:
            signals.append({"name": f"{timeframe}_change_pct", "value": pct_change(closes[0], latest_close)})
        if len(closes) >= 20:
            ma20 = avg(closes[-20:])
            signals.append({"name": f"{timeframe}_pct_vs_ma20", "value": pct_change(ma20, latest_close)})
        if len(closes) >= 60:
            ma60 = avg(closes[-60:])
            signals.append({"name": f"{timeframe}_pct_vs_ma60", "value": pct_change(ma60, latest_close)})
        if len(volumes) >= 20 and volumes[-1] is not None:
            avg_volume = avg(volumes[-20:])
            signals.append({"name": f"{timeframe}_volume_vs_20_avg_pct", "value": pct_change(avg_volume, volumes[-1])})
    return [signal for signal in signals if signal.get("value") is not None][:8]


def summarize_orderbook(row: dict[str, Any], expected_row: dict[str, Any] | None = None) -> dict[str, Any]:
    expected_row = expected_row or {}
    best_ask = parse_int(first_present(row, ("askp1", "askp")))
    best_bid = parse_int(first_present(row, ("bidp1", "bidp")))
    ask_qty = parse_int(first_present(row, ("askp_rsqn1", "askp_rsqn")))
    bid_qty = parse_int(first_present(row, ("bidp_rsqn1", "bidp_rsqn")))
    total_ask_qty = parse_int(first_present(row, ("total_askp_rsqn", "askp_rsqn_tots")))
    total_bid_qty = parse_int(first_present(row, ("total_bidp_rsqn", "bidp_rsqn_tots")))
    mid = ((best_ask or 0) + (best_bid or 0)) / 2 if best_ask and best_bid else None
    spread_pct = round(((best_ask - best_bid) / mid) * 100, 3) if mid else None
    total_depth = (total_ask_qty or 0) + (total_bid_qty or 0)
    imbalance = round(((total_bid_qty or 0) - (total_ask_qty or 0)) / total_depth, 4) if total_depth else None
    return {
        "best_ask": best_ask,
        "best_bid": best_bid,
        "ask_quantity_1": ask_qty,
        "bid_quantity_1": bid_qty,
        "total_ask_quantity": total_ask_qty,
        "total_bid_quantity": total_bid_qty,
        "spread_pct": spread_pct,
        "depth_imbalance": imbalance,
        "expected_price": parse_int(first_present(expected_row, ("antc_cnpr", "stck_prpr")) or first_present(row, ("antc_cnpr", "stck_prpr"))),
        "expected_volume": parse_int(first_present(expected_row, ("antc_vol",)) or first_present(row, ("antc_vol",))),
        "vi_status": text_first(expected_row, ("vi_cls_code", "vi_stnd_prc")) or text_first(row, ("vi_cls_code", "vi_stnd_prc")),
    }


def summarize_trade_flow(rows: list[dict[str, Any]]) -> dict[str, Any]:
    compact = trim_compact_rows(
        [
            {
                "time": text_first(row, ("stck_cntg_hour", "cntg_hour")),
                "price": parse_int(first_present(row, ("stck_prpr", "cnpr"))),
                "volume": parse_int(first_present(row, ("cntg_vol", "acml_vol"))),
                "change_pct": parse_float(first_present(row, ("prdy_ctrt",))),
            }
            for row in rows
        ],
        10,
    )
    prices = [row.get("price") for row in compact if row.get("price") is not None]
    return {
        "recent": compact[:5],
        "tick_count": len(compact),
        "latest_price": prices[0] if prices else None,
        "oldest_price": prices[-1] if prices else None,
        "recent_price_change_pct": pct_change(prices[-1], prices[0]) if len(prices) >= 2 else None,
    }


def summarize_investor_flow(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "foreign_net_buy_quantity": parse_int(first_present(row, ("frgn_ntby_qty", "frgn_ntby_vol"))),
        "institution_net_buy_quantity": parse_int(first_present(row, ("orgn_ntby_qty", "orgn_ntby_vol"))),
        "foreign_net_buy_value": parse_int(first_present(row, ("frgn_ntby_tr_pbmn", "frgn_ntby_tr_pbmn"))),
        "institution_net_buy_value": parse_int(first_present(row, ("orgn_ntby_tr_pbmn", "orgn_ntby_tr_pbmn"))),
        "foreign_buy_volume": parse_int(first_present(row, ("frgn_shnu_vol",))),
        "foreign_sell_volume": parse_int(first_present(row, ("frgn_seln_vol",))),
        "institution_buy_volume": parse_int(first_present(row, ("orgn_shnu_vol",))),
        "institution_sell_volume": parse_int(first_present(row, ("orgn_seln_vol",))),
    }


def build_price_row(
    symbol: str,
    info: dict[str, Any],
    price: dict[str, Any],
    *,
    observed_at: str,
    env_dv: str,
    market: str,
    errors: list[dict[str, Any]],
    charts: dict[str, list[dict[str, Any]]] | None = None,
    intraday: list[dict[str, Any]] | None = None,
    orderbook: dict[str, Any] | None = None,
    trade_flow: dict[str, Any] | None = None,
    investor_flow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol_name = text_first(info, ("prdt_abrv_name", "prdt_name", "prdt_name120")) or text_first(price, ("hts_kor_isnm", "bstp_kor_isnm")) or symbol
    current_price = parse_int(first_present(price, ("stck_prpr", "thdt_clpr", "stck_prdy_clpr")))
    required_missing: list[str] = []
    if not symbol_name or symbol_name == symbol:
        required_missing.append("symbol_name")
    if current_price is None:
        required_missing.append("price.current_or_last")
    if not observed_at:
        required_missing.append("price.observed_at")

    charts = charts or {"daily": [], "weekly": [], "monthly": []}
    intraday = intraday or []
    signals = [
        price_signal("day_change_pct", parse_float(price.get("prdy_ctrt"))),
        price_signal("volume", parse_int(price.get("acml_vol"))),
        price_signal("trading_value", parse_int(price.get("acml_tr_pbmn"))),
        price_signal("sector", text_first(price, ("bstp_kor_isnm",))),
        price_signal("per", parse_float(price.get("per"))),
        price_signal("pbr", parse_float(price.get("pbr"))),
        price_signal("pct_from_52w_high", parse_float(price.get("w52_hgpr_vrss_prpr_ctrt"))),
        price_signal("pct_from_52w_low", parse_float(price.get("w52_lwpr_vrss_prpr_ctrt"))),
        price_signal("pct_from_250d_high", parse_float(price.get("d250_hgpr_vrss_prpr_rate"))),
        price_signal("pct_from_250d_low", parse_float(price.get("d250_lwpr_vrss_prpr_rate"))),
    ]
    risk_flags = {
        key: str(price.get(key, "")).strip()
        for key in ("temp_stop_yn", "trht_yn", "invt_caful_yn", "short_over_yn", "sltr_yn", "mrkt_warn_cls_code", "mang_issu_cls_code")
        if str(price.get(key, "")).strip() not in {"", "N", "0", "00"}
    }
    if risk_flags:
        signals.append(price_signal("risk_flags", risk_flags))
    signals.extend(chart_signal_summary(charts))

    sources = [
        {"api": "direct_kis.search_stock_info", "env_dv": env_dv, "market": market},
        {"api": "direct_kis.inquire_price", "env_dv": env_dv, "market": market},
    ]
    if any(charts.values()):
        sources.append({"api": "direct_kis.inquire_daily_itemchartprice", "env_dv": env_dv, "market": market})
    if intraday:
        sources.append({"api": "direct_kis.inquire_time_itemchartprice", "env_dv": env_dv, "market": market})
    if orderbook:
        sources.append({"api": "direct_kis.inquire_asking_price_exp_ccn", "env_dv": env_dv, "market": market})
    if trade_flow:
        sources.append({"api": "direct_kis.inquire_ccnl", "env_dv": env_dv, "market": market})
    if investor_flow:
        sources.append({"api": "direct_kis.inquire_investor", "env_dv": env_dv, "market": market})
    return {
        "schema_version": "1",
        "symbol_id": symbol,
        "symbol_name": symbol_name,
        "product_type": product_type(info, price),
        "price": {
            "current_or_last": current_price,
            "observed_at": observed_at,
            "snapshot_mode": "live",
        },
        "eligible_for_verdict": not required_missing and not any(error.get("required") for error in errors),
        "required_missing": required_missing,
        "local_signals": [signal for signal in signals if signal is not None],
        "charts": charts,
        "intraday": intraday,
        "orderbook_summary": orderbook or {},
        "trade_flow_summary": trade_flow or {},
        "investor_flow_summary": investor_flow or {},
        "sources": sources,
        "errors": errors,
    }


def base_headers(app_key: str, app_secret: str, token: str, tr_id: str) -> dict[str, str]:
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P",
    }


def call_endpoint(endpoint_name: str, params: dict[str, str], app_key: str, app_secret: str, token: str, retries: int, *, env_dv: str = "real", tr_cont: str = "") -> tuple[dict[str, Any], dict[str, str]]:
    endpoint = ENDPOINTS[endpoint_name]
    tr_id = endpoint.get("tr_id")
    if endpoint_name in {"inquire_balance", "inquire_daily_ccld"}:
        tr_id = endpoint["tr_id_demo"] if env_dv == "demo" else endpoint["tr_id_real"]
    headers = base_headers(app_key, app_secret, token, str(tr_id))
    if tr_cont:
        headers["tr_cont"] = tr_cont
    body, response_headers = retry_json("GET", endpoint["path"], headers=headers, params=params, retries=retries)
    if not response_success(body):
        message = str(body.get("msg1") or body.get("msg_cd") or body.get("rt_cd") or "KIS API failed")
        raise RuntimeError(message)
    return body, response_headers


def yyyymmdd(value: datetime) -> str:
    return value.astimezone(KST).strftime("%Y%m%d")


def collect_period_chart(
    symbol: str,
    period_code: str,
    *,
    market: str,
    app_key: str,
    app_secret: str,
    token: str,
    retries: int,
    env_dv: str,
    end_at: datetime,
) -> list[dict[str, Any]]:
    days = {"D": 140, "W": 500, "M": 1500}.get(period_code, 140)
    start_at = end_at - timedelta(days=days)
    body, _headers = call_endpoint(
        "inquire_daily_itemchartprice",
        {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": yyyymmdd(start_at),
            "FID_INPUT_DATE_2": yyyymmdd(end_at),
            "FID_PERIOD_DIV_CODE": period_code,
            "FID_ORG_ADJ_PRC": "0",
        },
        app_key,
        app_secret,
        token,
        retries,
        env_dv=env_dv,
    )
    rows = [compact_ohlcv_bar(row) for row in output_rows_from_body(body, "output2")]
    limits = {"D": 60, "W": 52, "M": 36}
    return trim_compact_rows(rows, limits.get(period_code, 60))


def collect_intraday_chart(
    symbol: str,
    *,
    market: str,
    app_key: str,
    app_secret: str,
    token: str,
    retries: int,
    env_dv: str,
    end_at: datetime,
) -> list[dict[str, Any]]:
    body, _headers = call_endpoint(
        "inquire_time_itemchartprice",
        {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": end_at.astimezone(KST).strftime("%H%M%S"),
            "FID_PW_DATA_INCU_YN": "Y",
            "FID_ETC_CLS_CODE": "",
        },
        app_key,
        app_secret,
        token,
        retries,
        env_dv=env_dv,
    )
    rows = [compact_intraday_bar(row) for row in output_rows_from_body(body, "output2")]
    return trim_compact_rows(rows, 30)


def collect_orderbook_summary(symbol: str, *, market: str, app_key: str, app_secret: str, token: str, retries: int, env_dv: str) -> dict[str, Any]:
    body, _headers = call_endpoint(
        "inquire_asking_price_exp_ccn",
        {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": symbol},
        app_key,
        app_secret,
        token,
        retries,
        env_dv=env_dv,
    )
    orderbook_rows = normalize_output(body.get("output1"))
    expected_rows = normalize_output(body.get("output2"))
    return summarize_orderbook(orderbook_rows[0], expected_rows[0] if expected_rows else None) if orderbook_rows else {}


def collect_trade_flow_summary(symbol: str, *, market: str, app_key: str, app_secret: str, token: str, retries: int, env_dv: str) -> dict[str, Any]:
    body, _headers = call_endpoint(
        "inquire_ccnl",
        {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": symbol},
        app_key,
        app_secret,
        token,
        retries,
        env_dv=env_dv,
    )
    return summarize_trade_flow(output_rows_from_body(body, "output"))


def collect_investor_flow_summary(symbol: str, *, market: str, app_key: str, app_secret: str, token: str, retries: int, env_dv: str) -> dict[str, Any]:
    body, _headers = call_endpoint(
        "inquire_investor",
        {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": symbol},
        app_key,
        app_secret,
        token,
        retries,
        env_dv=env_dv,
    )
    rows = output_rows_from_body(body, "output")
    return summarize_investor_flow(rows[0]) if rows else {}


def collect_extended_market_evidence(
    symbol: str,
    *,
    market: str,
    app_key: str,
    app_secret: str,
    token: str,
    retries: int,
    env_dv: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    charts = {"daily": [], "weekly": [], "monthly": []}
    intraday: list[dict[str, Any]] = []
    orderbook: dict[str, Any] = {}
    trade_flow: dict[str, Any] = {}
    investor_flow: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    end_at = datetime.now(KST)

    for period_code, key in (("D", "daily"), ("W", "weekly"), ("M", "monthly")):
        try:
            charts[key] = collect_period_chart(
                symbol,
                period_code,
                market=market,
                app_key=app_key,
                app_secret=app_secret,
                token=token,
                retries=retries,
                env_dv=env_dv,
                end_at=end_at,
            )
        except Exception as exc:  # noqa: BLE001 - preserve price-based verdict eligibility
            errors.append(safe_error(exc, code=f"{key}_chart_failed", stage="price-chart", symbol_id=symbol, source="direct_kis.inquire_daily_itemchartprice", required=False))

    for source, code, collector in (
        ("direct_kis.inquire_time_itemchartprice", "intraday_chart_failed", lambda: collect_intraday_chart(symbol, market=market, app_key=app_key, app_secret=app_secret, token=token, retries=retries, env_dv=env_dv, end_at=end_at)),
        ("direct_kis.inquire_asking_price_exp_ccn", "orderbook_failed", lambda: collect_orderbook_summary(symbol, market=market, app_key=app_key, app_secret=app_secret, token=token, retries=retries, env_dv=env_dv)),
        ("direct_kis.inquire_ccnl", "trade_flow_failed", lambda: collect_trade_flow_summary(symbol, market=market, app_key=app_key, app_secret=app_secret, token=token, retries=retries, env_dv=env_dv)),
        ("direct_kis.inquire_investor", "investor_flow_failed", lambda: collect_investor_flow_summary(symbol, market=market, app_key=app_key, app_secret=app_secret, token=token, retries=retries, env_dv=env_dv)),
    ):
        try:
            value = collector()
            if code == "intraday_chart_failed":
                intraday = value
            elif code == "orderbook_failed":
                orderbook = value
            elif code == "trade_flow_failed":
                trade_flow = value
            elif code == "investor_flow_failed":
                investor_flow = value
        except Exception as exc:  # noqa: BLE001 - extended evidence is non-blocking
            errors.append(safe_error(exc, code=code, stage="price-chart", symbol_id=symbol, source=source, required=False))

    return charts, intraday, orderbook, trade_flow, investor_flow, errors


def collect_price_chart(symbols: list[str], *, run_id: str, started_at: str, env_dv: str, market: str, app_key: str, app_secret: str, token: str, retries: int, include_extended: bool = True) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    artifact_errors: list[dict[str, Any]] = []
    for symbol in symbols:
        symbol_errors: list[dict[str, Any]] = []
        info: dict[str, Any] = {}
        price: dict[str, Any] = {}
        charts: dict[str, list[dict[str, Any]]] = {"daily": [], "weekly": [], "monthly": []}
        intraday: list[dict[str, Any]] = []
        orderbook: dict[str, Any] = {}
        trade_flow: dict[str, Any] = {}
        investor_flow: dict[str, Any] = {}
        observed_at = now_kst_iso()
        try:
            body, _headers = call_endpoint(
                "search_stock_info",
                {"PRDT_TYPE_CD": "300", "PDNO": symbol},
                app_key,
                app_secret,
                token,
                retries,
                env_dv=env_dv,
            )
            info = output_first(body, "output")
        except Exception as exc:  # noqa: BLE001 - preserve partial symbol evidence
            symbol_errors.append(safe_error(exc, code="search_stock_info_failed", stage="price-chart", symbol_id=symbol, source="direct_kis.search_stock_info", required=False))
        try:
            body, _headers = call_endpoint(
                "inquire_price",
                {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": symbol},
                app_key,
                app_secret,
                token,
                retries,
                env_dv=env_dv,
            )
            price = output_first(body, "output")
            observed_at = now_kst_iso()
        except Exception as exc:  # noqa: BLE001 - required price failure becomes a row error
            symbol_errors.append(safe_error(exc, code="inquire_price_failed", stage="price-chart", symbol_id=symbol, source="direct_kis.inquire_price", required=True))
        if include_extended:
            charts, intraday, orderbook, trade_flow, investor_flow, extended_errors = collect_extended_market_evidence(
                symbol,
                market=market,
                app_key=app_key,
                app_secret=app_secret,
                token=token,
                retries=retries,
                env_dv=env_dv,
            )
            symbol_errors.extend(extended_errors)
        row = build_price_row(
            symbol,
            info,
            price,
            observed_at=observed_at,
            env_dv=env_dv,
            market=market,
            errors=symbol_errors,
            charts=charts,
            intraday=intraday,
            orderbook=orderbook,
            trade_flow=trade_flow,
            investor_flow=investor_flow,
        )
        rows.append(row)
        artifact_errors.extend(symbol_errors)

    status = "success"
    if any(not row["eligible_for_verdict"] for row in rows):
        status = "partial" if any(row["eligible_for_verdict"] for row in rows) else "failed"
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_kst_iso(),
        "stage": "price-chart",
        "status": status,
        "skipped": False,
        "skip_reason": "",
        "errors": artifact_errors,
        "symbols": rows,
    }


def failed_price_artifact(symbols: list[str], *, run_id: str, started_at: str, error: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for symbol in symbols:
        symbol_error = dict(error)
        symbol_error["stage"] = "price-chart"
        symbol_error["symbol_id"] = symbol
        rows.append(
            {
                "schema_version": "1",
                "symbol_id": symbol,
                "symbol_name": symbol,
                "product_type": "unresolved",
                "price": {"current_or_last": None, "observed_at": "", "snapshot_mode": ""},
                "eligible_for_verdict": False,
                "required_missing": ["symbol_name", "price.current_or_last", "price.observed_at"],
                "local_signals": [],
                "sources": [],
                "errors": [symbol_error],
            }
        )
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_kst_iso(),
        "stage": "price-chart",
        "status": "failed",
        "skipped": False,
        "skip_reason": "",
        "errors": [error],
        "symbols": rows,
    }


def balance_params(cano: str, product_code: str, ctx_fk100: str = "", ctx_nk100: str = "") -> dict[str, str]:
    return {
        "CANO": cano,
        "ACNT_PRDT_CD": product_code,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "00",
        "CTX_AREA_FK100": ctx_fk100,
        "CTX_AREA_NK100": ctx_nk100,
    }


def output_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("output1")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    if isinstance(rows, dict):
        return [rows]
    return []


def output_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("output2")
    if isinstance(summary, list) and summary and isinstance(summary[0], dict):
        return summary[0]
    if isinstance(summary, dict):
        return summary
    return {}


def continuation_context(payload: dict[str, Any]) -> tuple[str, str]:
    ctx_fk100 = str(payload.get("ctx_area_fk100") or payload.get("CTX_AREA_FK100") or "").strip()
    ctx_nk100 = str(payload.get("ctx_area_nk100") or payload.get("CTX_AREA_NK100") or "").strip()
    if ctx_fk100 or ctx_nk100:
        return ctx_fk100, ctx_nk100
    summary = output_summary(payload)
    return (
        str(summary.get("ctx_area_fk100") or summary.get("CTX_AREA_FK100") or "").strip(),
        str(summary.get("ctx_area_nk100") or summary.get("CTX_AREA_NK100") or "").strip(),
    )


def fetch_account_balance(*, env_dv: str, app_key: str, app_secret: str, token: str, retries: int, max_pages: int) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    cano, product_code = account_parts(env_dv)
    rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    tr_cont = ""
    ctx_fk100 = ""
    ctx_nk100 = ""
    for _page in range(max_pages):
        try:
            body, response_headers = call_endpoint(
                "inquire_balance",
                balance_params(cano, product_code, ctx_fk100, ctx_nk100),
                app_key,
                app_secret,
                token,
                retries,
                env_dv=env_dv,
                tr_cont=tr_cont,
            )
        except Exception as exc:  # noqa: BLE001 - account gate handles failure
            errors.append(safe_error(exc, code="inquire_balance_failed", stage="account-before-order", source="direct_kis.inquire_balance", required=True))
            break
        rows.extend(output_rows(body))
        if not summary:
            summary = output_summary(body)
        ctx_fk100, ctx_nk100 = continuation_context(body)
        next_tr_cont = response_headers.get("tr_cont", "").strip()
        if next_tr_cont not in {"F", "M"}:
            break
        tr_cont = "N"
        time.sleep(0.2)
    return rows, summary, errors


def holding_symbol(row: dict[str, Any]) -> str:
    value = text_first(row, ("pdno", "PDNO", "prdt_code", "shtn_pdno", "item_code"))
    return value


def holding_quantity(row: dict[str, Any]) -> int:
    return parse_int(first_present(row, ("hldg_qty", "hold_qty", "qty"))) or 0


def normalize_holding(row: dict[str, Any], *, observed_at: str) -> dict[str, Any]:
    symbol = holding_symbol(row)
    return {
        "symbol_id": symbol,
        "symbol_name": text_first(row, ("prdt_name", "prdt_abrv_name", "hts_kor_isnm")) or symbol,
        "current_live_holding_quantity": holding_quantity(row),
        "ord_psbl_qty": parse_int(first_present(row, ("ord_psbl_qty", "sell_psbl_qty", "slpsblqty"))) or 0,
        "current_price": parse_int(first_present(row, ("prpr", "stck_prpr", "now_pric", "pchs_avg_pric"))),
        "valuation_amount": parse_int(first_present(row, ("evlu_amt", "evlu_pfls_amt_smtl", "scts_evlu_amt"))),
        "pnl_amount": parse_int(first_present(row, ("evlu_pfls_amt", "evlu_pfls_smtl_amt"))),
        "pnl_rate": parse_float(first_present(row, ("evlu_pfls_rt", "evlu_erng_rt", "pfls_rt"))),
        "today_buy_quantity": parse_int(first_present(row, ("thdt_buyqty", "thdt_buy_qty", "tdy_buy_qty"))) or 0,
        "today_sell_quantity": parse_int(first_present(row, ("thdt_sll_qty", "thdt_sllqty", "tdy_sell_qty"))) or 0,
        "observed_at": observed_at,
    }


def build_account_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "cash_amount": parse_int(first_present(summary, ("dnca_tot_amt", "prvs_rcdl_excc_amt", "ord_psbl_cash"))),
        "total_evaluation_amount": parse_int(first_present(summary, ("tot_evlu_amt", "nass_amt", "tot_asst_amt"))),
        "securities_valuation_amount": parse_int(first_present(summary, ("scts_evlu_amt", "tot_stln_slng_chgs"))),
        "total_pnl_amount": parse_int(first_present(summary, ("evlu_pfls_smtl_amt", "evlu_pfls_amt_smtl"))),
        "today_buy_amount": parse_int(first_present(summary, ("thdt_buy_amt", "thdt_buy_amt_smtl"))),
        "today_sell_amount": parse_int(first_present(summary, ("thdt_sll_amt", "thdt_sll_amt_smtl"))),
    }


def normalize_fill(row: dict[str, Any], *, env_dv: str, observed_at: str) -> dict[str, Any]:
    symbol = normalize_symbol_key(first_present(row, ("pdno", "PDNO", "shtn_pdno", "SHTN_PDNO", "prdt_no", "PRDT_NO")))
    side = str(first_present(row, ("sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD", "sll_buy_dvsn_name", "SLL_BUY_DVSN_NAME")) or "").strip()
    side_text = side.lower()
    if side in {"01"} or "매도" in side_text or side_text in {"sell", "sll"}:
        direction = "sell"
    elif side in {"02"} or "매수" in side_text or side_text in {"buy"}:
        direction = "buy"
    else:
        direction = ""
    filled_quantity = parse_int(first_present(row, ("tot_ccld_qty", "TOT_CCLD_QTY", "ccld_qty", "CCLD_QTY", "ord_qty", "ORD_QTY"))) or 0
    filled_price = parse_int(first_present(row, ("avg_prvs", "AVG_PRVS", "avg_ccld_prc", "AVG_CCLD_PRC", "ccld_unpr", "CCLD_UNPR", "ord_unpr", "ORD_UNPR"))) or 0
    filled_amount = parse_int(first_present(row, ("tot_ccld_amt", "TOT_CCLD_AMT", "ccld_amt", "CCLD_AMT"))) or (filled_quantity * filled_price if filled_quantity and filled_price else 0)
    filled_at = str(first_present(row, ("ord_tmd", "ORD_TMD", "ccld_dtime", "CCLD_DTIME", "ccld_tmd", "CCLD_TMD")) or "").strip()
    order_date = str(first_present(row, ("ord_dt", "ORD_DT", "ord_date", "ORD_DATE")) or "").strip()
    if order_date and filled_at and len(filled_at) == 6 and filled_at.isdigit():
        filled_at = f"{order_date[0:4]}-{order_date[4:6]}-{order_date[6:8]}T{filled_at[0:2]}:{filled_at[2:4]}:{filled_at[4:6]}+09:00"
    return {
        "symbol_id": symbol,
        "symbol_name": text_first(row, ("prdt_name", "PRDT_NAME", "prdt_abrv_name", "PRDT_ABRV_NAME", "hts_kor_isnm", "HTS_KOR_ISNM")) or symbol,
        "direction": direction,
        "filled_at": filled_at,
        "filled_quantity": filled_quantity,
        "filled_price": filled_price,
        "filled_amount": filled_amount,
        "order_id": str(first_present(row, ("odno", "ODNO", "ord_no", "ORD_NO")) or "").strip(),
        "order_date": order_date,
        "execution_environment": env_dv,
        "observed_at": observed_at,
    }


def collect_today_fills_artifact(
    symbols: list[str],
    *,
    run_id: str,
    started_at: str,
    env_dv: str,
    app_key: str,
    app_secret: str,
    token: str,
    retries: int,
) -> dict[str, Any]:
    cano, product = account_parts(env_dv)
    day = datetime.fromisoformat(started_at).astimezone(KST).strftime("%Y%m%d") if started_at else datetime.now(KST).strftime("%Y%m%d")
    raw_rows: list[dict[str, Any]] = []
    ctx_fk100 = ""
    ctx_nk100 = ""
    tr_cont = ""
    for _page in range(20):
        body, response_headers = call_endpoint(
            "inquire_daily_ccld",
            {
                "CANO": cano,
                "ACNT_PRDT_CD": product,
                "INQR_STRT_DT": day,
                "INQR_END_DT": day,
                "SLL_BUY_DVSN_CD": "00",
                "INQR_DVSN": "00",
                "PDNO": "",
                "CCLD_DVSN": "01",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": ctx_fk100,
                "CTX_AREA_NK100": ctx_nk100,
            },
            app_key,
            app_secret,
            token,
            retries,
            env_dv=env_dv,
            tr_cont=tr_cont,
        )
        raw_rows.extend(output_rows_from_body(body, "output1"))
        ctx_fk100, ctx_nk100 = continuation_context(body)
        next_tr_cont = response_headers.get("tr_cont", "").strip()
        if next_tr_cont not in {"F", "M"}:
            break
        tr_cont = "N"
        time.sleep(0.2)
    wanted = {normalize_symbol_key(symbol) for symbol in symbols}
    observed_at = now_kst_iso()
    fills = [
        fill
        for fill in (normalize_fill(row, env_dv=env_dv, observed_at=observed_at) for row in raw_rows)
        if fill.get("symbol_id") in wanted and fill.get("direction") in {"buy", "sell"} and fill.get("filled_quantity", 0) > 0
    ]
    fills.sort(key=lambda item: (str(item.get("filled_at") or ""), str(item.get("order_id") or "")))
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_kst_iso(),
        "stage": "today-fills",
        "status": "success",
        "skipped": False,
        "skip_reason": "",
        "execution_environment": env_dv,
        "source_api": "direct_kis.inquire_daily_ccld",
        "symbols": [{"symbol_id": symbol} for symbol in symbols],
        "fills": fills,
        "errors": [],
    }


def skipped_today_fills_artifact(symbols: list[str], *, run_id: str, started_at: str, env_dv: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_kst_iso(),
        "stage": "today-fills",
        "status": "success",
        "skipped": True,
        "skip_reason": reason,
        "execution_environment": env_dv,
        "source_api": "direct_kis.inquire_daily_ccld",
        "symbols": [{"symbol_id": symbol} for symbol in symbols],
        "fills": [],
        "errors": [],
    }


def failed_today_fills_artifact(symbols: list[str], *, run_id: str, started_at: str, env_dv: str, error: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_kst_iso(),
        "stage": "today-fills",
        "status": "partial",
        "skipped": False,
        "skip_reason": "",
        "execution_environment": env_dv,
        "source_api": "direct_kis.inquire_daily_ccld",
        "symbols": [{"symbol_id": symbol} for symbol in symbols],
        "fills": [],
        "errors": [error],
    }


def account_asset_history_path(output_dir: Path) -> Path:
    for parent in output_dir.resolve().parents:
        if parent.name == "reports":
            return parent.parent / ACCOUNT_ASSET_HISTORY_RELATIVE_PATH
    return output_dir.resolve().parent / ACCOUNT_ASSET_HISTORY_RELATIVE_PATH


def account_asset_summary_from_row(row: dict[str, Any]) -> dict[str, Any]:
    purchase = parse_int(first_present(row, ("pchs_amt_smtl",)))
    pnl = parse_int(first_present(row, ("evlu_pfls_amt_smtl",)))
    return {
        "source_api": "inquire_balance",
        "observed_at": row.get("observed_at"),
        "total_asset_amount": parse_int(first_present(row, ("tot_asst_amt",))),
        "cash_deposit_amount": parse_int(first_present(row, ("tot_dncl_amt",))),
        "evaluated_asset_amount": parse_int(first_present(row, ("evlu_amt_smtl",))),
        "purchase_amount": purchase,
        "evaluation_pnl_amount": pnl,
        "evaluation_pnl_rate": (pnl / purchase) if purchase and pnl is not None else None,
        "overseas_stock_evaluation_amount": parse_int(first_present(row, ("ovrs_stck_evlu_amt1",))),
    }


def account_asset_history_row(snapshot: dict[str, Any]) -> dict[str, Any]:
    row = {
        "schema_version": snapshot.get("schema_version"),
        "run_id": snapshot.get("run_id"),
        "started_at": snapshot.get("started_at"),
        "observed_at": snapshot.get("observed_at"),
        "generated_at": snapshot.get("generated_at"),
        "status": snapshot.get("status"),
        "source_api": snapshot.get("source_api"),
        "execution_environment": snapshot.get("execution_environment"),
    }
    for key in ACCOUNT_ASSET_ALLOWLIST:
        row[key] = snapshot.get(key)
    return row


def append_account_asset_history(history_path: Path, snapshot: dict[str, Any]) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = history_path.with_suffix(history_path.suffix + ".lock")
    with open(lock_path, "a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            with open(history_path, "a", encoding="utf-8") as history_handle:
                json.dump(account_asset_history_row(snapshot), history_handle, ensure_ascii=False, sort_keys=True)
                history_handle.write("\n")
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def collect_account_asset_snapshot(
    *,
    run_id: str,
    started_at: str,
    env_dv: str,
    app_key: str,
    app_secret: str,
    token: str,
    retries: int,
) -> dict[str, Any]:
    observed_at = now_kst_iso()
    cano, product_code = account_parts(env_dv)
    body, _headers = call_endpoint(
        "inquire_balance",
        balance_params(cano, product_code),
        app_key,
        app_secret,
        token,
        retries,
        env_dv=env_dv,
    )
    summary = output_summary(body)
    snapshot: dict[str, Any] = {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_kst_iso(),
        "observed_at": observed_at,
        "stage": "account-asset-snapshot",
        "status": "success",
        "skipped": False,
        "skip_reason": "",
        "source_api": "inquire_balance",
        "execution_environment": env_dv,
        "errors": [],
    }
    for key in ACCOUNT_ASSET_ALLOWLIST:
        snapshot[key] = parse_int(first_present(summary, (key,)))
    snapshot["account_asset_summary"] = account_asset_summary_from_row(snapshot)
    if snapshot["tot_asst_amt"] is None:
        snapshot["status"] = "failed"
        snapshot["errors"] = [
            safe_error(
                "missing tot_asst_amt in allowlisted account asset response",
                code="missing_total_asset_amount",
                stage="account-asset-snapshot",
                source="direct_kis.inquire_balance",
                required=False,
            )
        ]
    return snapshot


def skipped_account_asset_snapshot(*, run_id: str, started_at: str, env_dv: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_kst_iso(),
        "observed_at": "",
        "stage": "account-asset-snapshot",
        "status": "success",
        "skipped": True,
        "skip_reason": reason,
        "source_api": "inquire_balance",
        "execution_environment": env_dv,
        "errors": [],
        "account_asset_summary": {},
    }


def failed_account_asset_snapshot(*, run_id: str, started_at: str, env_dv: str, error: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_kst_iso(),
        "observed_at": "",
        "stage": "account-asset-snapshot",
        "status": "failed",
        "skipped": False,
        "skip_reason": "",
        "source_api": "inquire_balance",
        "execution_environment": env_dv,
        "errors": [error],
        "account_asset_summary": {},
    }


def collect_account_artifact(symbols: list[str], *, run_id: str, started_at: str, env_dv: str, app_key: str, app_secret: str, token: str, retries: int, max_pages: int, request_type: str) -> dict[str, Any]:
    observed_at = now_kst_iso()
    rows, summary, errors = fetch_account_balance(env_dv=env_dv, app_key=app_key, app_secret=app_secret, token=token, retries=retries, max_pages=max_pages)
    holdings_by_symbol: dict[str, dict[str, Any]] = {}
    non_universe: list[dict[str, Any]] = []
    universe = set(symbols)
    for row in rows:
        normalized = normalize_holding(row, observed_at=observed_at)
        symbol = normalized["symbol_id"]
        if not symbol or normalized["current_live_holding_quantity"] <= 0:
            continue
        if symbol in universe:
            holdings_by_symbol[symbol] = normalized
        else:
            non_universe.append(normalized)

    symbol_rows = []
    for symbol in symbols:
        symbol_rows.append(
            holdings_by_symbol.get(
                symbol,
                {
                    "symbol_id": symbol,
                    "symbol_name": symbol,
                    "current_live_holding_quantity": 0,
                    "ord_psbl_qty": 0,
                    "current_price": None,
                    "valuation_amount": 0,
                    "pnl_amount": 0,
                    "pnl_rate": None,
                    "today_buy_quantity": 0,
                    "today_sell_quantity": 0,
                    "observed_at": observed_at,
                },
            )
        )

    warnings = [
        "active_order_lookup_not_performed",
        "order_available_lookup_not_performed",
    ]
    status = "failed" if errors else "partial"
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_kst_iso(),
        "stage": "account-before-order",
        "status": status,
        "skipped": False,
        "skip_reason": "",
        "request_type": request_type,
        "execution_environment": env_dv,
        "account_summary": build_account_summary(summary),
        "active_order_lookup_performed": False,
        "order_available_lookup_performed": False,
        "warnings": warnings,
        "active_orders": [],
        "non_universe_account_positions": non_universe,
        "errors": errors,
        "symbols": symbol_rows,
    }


def skipped_account_artifact(symbols: list[str], *, run_id: str, started_at: str, env_dv: str, request_type: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_kst_iso(),
        "stage": "account-before-order",
        "status": "success",
        "skipped": True,
        "skip_reason": reason,
        "request_type": request_type,
        "execution_environment": env_dv,
        "account_summary": {},
        "active_order_lookup_performed": False,
        "order_available_lookup_performed": False,
        "warnings": [],
        "active_orders": [],
        "non_universe_account_positions": [],
        "errors": [],
        "symbols": [{"symbol_id": symbol} for symbol in symbols],
    }


def failed_account_artifact(symbols: list[str], *, run_id: str, started_at: str, env_dv: str, request_type: str, error: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_kst_iso(),
        "stage": "account-before-order",
        "status": "failed",
        "skipped": False,
        "skip_reason": "",
        "request_type": request_type,
        "execution_environment": env_dv,
        "account_summary": {},
        "active_order_lookup_performed": False,
        "order_available_lookup_performed": False,
        "warnings": [],
        "active_orders": [],
        "non_universe_account_positions": [],
        "errors": [error],
        "symbols": [{"symbol_id": symbol} for symbol in symbols],
    }


def status_from_children(children: list[dict[str, Any]]) -> str:
    statuses = [child.get("status") for child in children if not child.get("skipped")]
    if not statuses:
        return "success"
    if any(status == "failed" for status in statuses):
        return "partial" if any(status in {"success", "partial"} for status in statuses) else "failed"
    if any(status == "partial" for status in statuses):
        return "partial"
    return "success"


def build_collection_summary(
    *,
    run_id: str,
    started_at: str,
    env_dv: str,
    symbols: list[str],
    price_artifact: dict[str, Any],
    account_artifact: dict[str, Any],
    account_asset_snapshot: dict[str, Any],
    today_fills_artifact: dict[str, Any],
    output_dir: Path,
    token_status: str,
    token_expires_at: str,
) -> dict[str, Any]:
    children = [price_artifact, account_artifact]
    account_asset_errors = account_asset_snapshot.get("errors", []) if isinstance(account_asset_snapshot, dict) else []
    return {
        "schema_version": "1",
        "run_id": run_id,
        "started_at": started_at,
        "generated_at": now_kst_iso(),
        "stage": "main-evidence-collection",
        "status": status_from_children(children),
        "skipped": False,
        "skip_reason": "",
        "environment": env_dv,
        "token_status": token_status,
        "token_expires_at": token_expires_at,
        "paths": {
            "price_chart": str(output_dir / "price-chart.json"),
            "account_before_order": str(output_dir / "account-before-order.json"),
            "account_asset_snapshot": str(output_dir / "account-asset-snapshot.json"),
            "today_fills": str(output_dir / "today-fills.json"),
            "collection_summary": str(output_dir / "collection-summary.json"),
        },
        "counts": {
            "input_symbols": len(symbols),
            "price_symbols": len(price_artifact.get("symbols", [])),
            "account_symbols": len(account_artifact.get("symbols", [])),
            "today_fill_count": len(today_fills_artifact.get("fills", [])),
            "price_errors": len(price_artifact.get("errors", [])),
            "account_errors": len(account_artifact.get("errors", [])),
            "account_asset_errors": len(account_asset_errors),
            "today_fills_errors": len(today_fills_artifact.get("errors", [])),
        },
        "optional_stages": [
            {
                "stage": "account-asset-snapshot",
                "status": account_asset_snapshot.get("status"),
                "skipped": bool(account_asset_snapshot.get("skipped")),
                "errors": account_asset_errors,
            },
            {
                "stage": "today-fills",
                "status": today_fills_artifact.get("status"),
                "skipped": bool(today_fills_artifact.get("skipped")),
                "errors": today_fills_artifact.get("errors", []) if isinstance(today_fills_artifact.get("errors"), list) else [],
            }
        ],
        "warnings": account_artifact.get("warnings", []),
        "errors": price_artifact.get("errors", []) + account_artifact.get("errors", []),
        "symbols": [{"symbol_id": symbol} for symbol in symbols],
    }


def command_collect(args: argparse.Namespace) -> int:
    symbols = parse_symbols(args.symbols)
    env_dv = normalize_trading_env(args.env)
    started_at = args.started_at or now_kst_iso()
    output_dir = Path(args.output_dir).expanduser()
    try:
        app_key, app_secret = kis_credentials(env_dv)
        token, token_status, token_expires_at = fetch_token(app_key, app_secret, env_dv, args.retries)
    except Exception as exc:  # noqa: BLE001 - write sanitized auth failure artifacts
        error = safe_error(exc, code="auth_failed", stage="main-evidence-collection", source="direct_kis.auth", required=True)
        price_artifact = failed_price_artifact(symbols, run_id=args.run_id, started_at=started_at, error=error)
        account_artifact = failed_account_artifact(symbols, run_id=args.run_id, started_at=started_at, env_dv=env_dv, request_type=args.request_type, error=error)
        account_asset_snapshot = failed_account_asset_snapshot(
            run_id=args.run_id,
            started_at=started_at,
            env_dv=env_dv,
            error=safe_error(exc, code="auth_failed", stage="account-asset-snapshot", source="direct_kis.auth", required=False),
        )
        today_fills_artifact = failed_today_fills_artifact(
            symbols,
            run_id=args.run_id,
            started_at=started_at,
            env_dv=env_dv,
            error=safe_error(exc, code="auth_failed", stage="today-fills", source="direct_kis.auth", required=False),
        )
        write_json(output_dir / "price-chart.json", price_artifact)
        write_json(output_dir / "account-before-order.json", account_artifact)
        write_json(output_dir / "account-asset-snapshot.json", account_asset_snapshot)
        write_json(output_dir / "today-fills.json", today_fills_artifact)
        summary = build_collection_summary(
            run_id=args.run_id,
            started_at=started_at,
            env_dv=env_dv,
            symbols=symbols,
            price_artifact=price_artifact,
            account_artifact=account_artifact,
            account_asset_snapshot=account_asset_snapshot,
            today_fills_artifact=today_fills_artifact,
            output_dir=output_dir,
            token_status="failed",
            token_expires_at="",
        )
        write_json(output_dir / "collection-summary.json", summary)
        print(json.dumps({"status": summary["status"], "paths": summary["paths"], "counts": summary["counts"], "warnings": summary["warnings"], "errors": summary["errors"]}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1

    price_artifact = collect_price_chart(
        symbols,
        run_id=args.run_id,
        started_at=started_at,
        env_dv=env_dv,
        market=args.market,
        app_key=app_key,
        app_secret=app_secret,
        token=token,
        retries=args.retries,
        include_extended=not args.skip_extended_market_evidence,
    )
    price_path = output_dir / "price-chart.json"
    write_json(price_path, price_artifact)

    children = [price_artifact]
    account_path = output_dir / "account-before-order.json"
    if args.skip_account:
        account_artifact = skipped_account_artifact(
            symbols,
            run_id=args.run_id,
            started_at=started_at,
            env_dv=env_dv,
            request_type=args.request_type,
            reason="skip-account option",
        )
        account_asset_snapshot = skipped_account_asset_snapshot(
            run_id=args.run_id,
            started_at=started_at,
            env_dv=env_dv,
            reason="skip-account option",
        )
        today_fills_artifact = skipped_today_fills_artifact(
            symbols,
            run_id=args.run_id,
            started_at=started_at,
            env_dv=env_dv,
            reason="skip-account option",
        )
    else:
        account_artifact = collect_account_artifact(
            symbols,
            run_id=args.run_id,
            started_at=started_at,
            env_dv=env_dv,
            app_key=app_key,
            app_secret=app_secret,
            token=token,
            retries=args.retries,
            max_pages=args.max_account_pages,
            request_type=args.request_type,
        )
        try:
            account_asset_snapshot = collect_account_asset_snapshot(
                run_id=args.run_id,
                started_at=started_at,
                env_dv=env_dv,
                app_key=app_key,
                app_secret=app_secret,
                token=token,
                retries=args.retries,
            )
        except Exception as exc:  # noqa: BLE001 - optional account asset stage is non-blocking
            account_asset_snapshot = failed_account_asset_snapshot(
                run_id=args.run_id,
                started_at=started_at,
                env_dv=env_dv,
                error=safe_error(exc, code="account_asset_snapshot_failed", stage="account-asset-snapshot", source="direct_kis.inquire_balance", required=False),
            )
        if account_asset_snapshot.get("status") == "success" and not account_asset_snapshot.get("skipped"):
            try:
                append_account_asset_history(account_asset_history_path(output_dir), account_asset_snapshot)
            except Exception as exc:  # noqa: BLE001 - history persistence must not fail required collection
                account_asset_snapshot["status"] = "partial"
                account_asset_snapshot.setdefault("errors", []).append(
                    safe_error(exc, code="account_asset_history_append_failed", stage="account-asset-snapshot", source=str(account_asset_history_path(output_dir)), required=False)
                )
        try:
            today_fills_artifact = collect_today_fills_artifact(
                symbols,
                run_id=args.run_id,
                started_at=started_at,
                env_dv=env_dv,
                app_key=app_key,
                app_secret=app_secret,
                token=token,
                retries=args.retries,
            )
        except Exception as exc:  # noqa: BLE001 - fill timeline is useful but non-blocking
            today_fills_artifact = failed_today_fills_artifact(
                symbols,
                run_id=args.run_id,
                started_at=started_at,
                env_dv=env_dv,
                error=safe_error(exc, code="today_fills_failed", stage="today-fills", source="direct_kis.inquire_daily_ccld", required=False),
            )
    write_json(account_path, account_artifact)
    write_json(output_dir / "account-asset-snapshot.json", account_asset_snapshot)
    write_json(output_dir / "today-fills.json", today_fills_artifact)
    children.append(account_artifact)

    summary = build_collection_summary(
        run_id=args.run_id,
        started_at=started_at,
        env_dv=env_dv,
        symbols=symbols,
        price_artifact=price_artifact,
        account_artifact=account_artifact,
        account_asset_snapshot=account_asset_snapshot,
        today_fills_artifact=today_fills_artifact,
        output_dir=output_dir,
        token_status=token_status,
        token_expires_at=token_expires_at,
    )
    summary_path = output_dir / "collection-summary.json"
    write_json(summary_path, summary)
    print(json.dumps({"status": summary["status"], "paths": summary["paths"], "counts": summary["counts"], "warnings": summary["warnings"]}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"] in {"success", "partial"} else 1


def command_self_test(_args: argparse.Namespace) -> int:
    assert normalize_trading_env("acct") == "real"
    assert normalize_trading_env("paper") == "demo"
    assert parse_symbols("5930,000660,000660") == ["005930", "000660"]
    assert parse_int("1,234.00") == 1234
    assert parse_float("-1.25") == -1.25
    orderbook = summarize_orderbook(
        {"askp1": "1010", "bidp1": "1000", "total_askp_rsqn": "20", "total_bidp_rsqn": "30"},
        {"antc_cnpr": "1005", "antc_vol": "12", "vi_cls_code": "N"},
    )
    assert orderbook["expected_price"] == 1005
    assert orderbook["expected_volume"] == 12
    info = {"prdt_abrv_name": "ACE GOLD ETF", "scty_grp_id_cd": "EF", "etf_dvsn_cd": "02"}
    price = {"stck_prpr": "18590", "prdy_ctrt": "1.23", "acml_vol": "1000"}
    row = build_price_row(
        "411060",
        info,
        price,
        observed_at="2026-06-18T09:00:00+09:00",
        env_dv="real",
        market="J",
        errors=[],
        charts={
            "daily": [
                {"date": "20260617", "close": 18000, "volume": 100},
                {"date": "20260618", "close": 18590, "volume": 150},
            ],
            "weekly": [],
            "monthly": [],
        },
        orderbook={"best_ask": 18600, "best_bid": 18590, "spread_pct": 0.054, "expected_price": 18595},
        investor_flow={"foreign_net_buy_quantity": 1000},
    )
    assert row["product_type"] == "etf"
    assert row["price"]["current_or_last"] == 18590
    assert row["charts"]["daily"][0]["date"] == "20260617"
    assert any(signal["name"] == "daily_change_pct" for signal in row["local_signals"])
    assert row["orderbook_summary"]["best_bid"] == 18590
    assert row["orderbook_summary"]["expected_price"] == 18595
    assert row["investor_flow_summary"]["foreign_net_buy_quantity"] == 1000
    assert row["eligible_for_verdict"]
    sample_account = normalize_holding(
        {
            "pdno": "0183J0",
            "prdt_name": "Samsung Electronics",
            "hldg_qty": "3",
            "ord_psbl_qty": "2",
            "prpr": "70000",
            "evlu_amt": "210000",
            "evlu_pfls_amt": "1000",
            "evlu_pfls_rt": "0.48",
        },
        observed_at="2026-06-18T09:00:00+09:00",
    )
    assert sample_account["symbol_id"] == "0183J0"
    assert sample_account["current_live_holding_quantity"] == 3
    assert build_account_summary({"dnca_tot_amt": "1000", "tot_evlu_amt": "2000"})["cash_amount"] == 1000
    fill = normalize_fill(
        {
            "pdno": "005930",
            "prdt_name": "Samsung Electronics",
            "sll_buy_dvsn_cd": "02",
            "tot_ccld_qty": "3",
            "avg_prvs": "70100",
            "ord_dt": "20260618",
            "ord_tmd": "094200",
            "odno": "1",
        },
        env_dv="real",
        observed_at="2026-06-18T09:43:00+09:00",
    )
    assert fill["symbol_id"] == "005930"
    assert fill["direction"] == "buy"
    assert fill["filled_price"] == 70100
    assert fill["filled_at"] == "2026-06-18T09:42:00+09:00"
    asset_summary = account_asset_summary_from_row(
        {
            "tot_asst_amt": "20000000",
            "tot_dncl_amt": "1000000",
            "evlu_amt_smtl": "19000000",
            "pchs_amt_smtl": "18000000",
            "evlu_pfls_amt_smtl": "1000000",
            "ovrs_stck_evlu_amt1": "0",
        }
    )
    assert asset_summary["total_asset_amount"] == 20000000
    assert asset_summary["evaluation_pnl_rate"] == 1000000 / 18000000
    skipped_asset = skipped_account_asset_snapshot(run_id="self-test", started_at="2026-06-18T09:00:00+09:00", env_dv="real", reason="skip-account option")
    assert skipped_asset["skipped"] and skipped_asset["status"] == "success"
    summary = build_collection_summary(
        run_id="self-test",
        started_at="2026-06-18T09:00:00+09:00",
        env_dv="real",
        symbols=["005930"],
        price_artifact={"status": "success", "symbols": [{"symbol_id": "005930"}], "errors": []},
        account_artifact={"status": "success", "symbols": [{"symbol_id": "005930"}], "warnings": [], "errors": []},
        account_asset_snapshot={
            "stage": "account-asset-snapshot",
            "status": "failed",
            "skipped": False,
            "errors": [safe_error("optional probe", stage="account-asset-snapshot", required=False)],
        },
        today_fills_artifact={
            "stage": "today-fills",
            "status": "success",
            "skipped": False,
            "fills": [fill],
            "errors": [],
        },
        output_dir=Path("/tmp/daily-trading-self-test"),
        token_status="existing_token",
        token_expires_at="",
    )
    assert summary["status"] == "success"
    assert summary["errors"] == []
    assert summary["counts"]["account_asset_errors"] == 1
    assert summary["counts"]["today_fill_count"] == 1
    assert summary["optional_stages"][0]["stage"] == "account-asset-snapshot"
    assert summary["optional_stages"][1]["stage"] == "today-fills"
    with tempfile.TemporaryDirectory() as tmp_name:
        history_path = Path(tmp_name) / "memory" / "account-assets" / "account-assets.jsonl"
        snapshot = {
            "schema_version": "1",
            "run_id": "self-test",
            "started_at": "2026-06-18T09:00:00+09:00",
            "observed_at": "2026-06-18T09:01:00+09:00",
            "generated_at": "2026-06-18T09:01:00+09:00",
            "status": "success",
            "source_api": "inquire_balance",
            "execution_environment": "real",
            "tot_asst_amt": 20000000,
            "tot_dncl_amt": 1000000,
            "evlu_amt_smtl": 19000000,
            "pchs_amt_smtl": 18000000,
            "evlu_pfls_amt_smtl": 1000000,
            "ovrs_stck_evlu_amt1": 0,
            "account_asset_summary": asset_summary,
            "raw_secret_probe": "must_not_be_written",
        }
        append_account_asset_history(history_path, snapshot)
        row = json.loads(history_path.read_text(encoding="utf-8").strip())
        assert row["tot_asst_amt"] == 20000000
        assert "account_asset_summary" not in row
        assert "raw_secret_probe" not in row
    print("self-test ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect daily-trading price and account evidence through direct KIS REST.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="Collect price-chart and account-before-order artifacts.")
    collect_parser.add_argument("--run-id", required=True)
    collect_parser.add_argument("--started-at", default="")
    collect_parser.add_argument("--symbols", required=True, help="Comma or newline separated symbol ids.")
    collect_parser.add_argument("--output-dir", required=True)
    collect_parser.add_argument("--env", default="", help="acct/real or paper/demo. Defaults to CODEX_MCP_TRADING_ENV/acct.")
    collect_parser.add_argument("--market", default="J")
    collect_parser.add_argument("--request-type", default="analysis", choices=["analysis", "prepare", "demo-submit", "real-submit"])
    collect_parser.add_argument("--skip-account", action="store_true")
    collect_parser.add_argument("--skip-extended-market-evidence", action="store_true", help="Collect only identity/current price/account artifacts.")
    collect_parser.add_argument("--retries", type=int, default=3)
    collect_parser.add_argument("--max-account-pages", type=int, default=20)
    collect_parser.set_defaults(func=command_collect)

    self_test_parser = subparsers.add_parser("self-test", help="Run local parser tests.")
    self_test_parser.set_defaults(func=command_self_test)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - top-level sanitized JSON failure
        print(json.dumps({"status": "failed", "error": safe_error(exc, code="runtime_error")}, ensure_ascii=False, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
