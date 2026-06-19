#!/usr/bin/env python3
"""Collect daily-trading main evidence through direct KIS REST calls."""

from __future__ import annotations

import argparse
import json
import os
import re
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
TOKEN_PATH = "/oauth2/tokenP"

ENDPOINTS = {
    "search_stock_info": {
        "path": "/uapi/domestic-stock/v1/quotations/search-stock-info",
        "tr_id": "CTPF1002R",
    },
    "inquire_price": {
        "path": "/uapi/domestic-stock/v1/quotations/inquire-price",
        "tr_id": "FHKST01010100",
    },
    "inquire_balance": {
        "path": "/uapi/domestic-stock/v1/trading/inquire-balance",
        "tr_id_real": "TTTC8434R",
        "tr_id_demo": "VTTC8434R",
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


def token_cache_path(env_dv: str) -> Path:
    configured = os.environ.get("DAILY_TRADING_TOKEN_CACHE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "codex" / "daily-trading" / f"kis-token-{env_dv}.json"


def cached_token(env_dv: str) -> str | None:
    path = token_cache_path(env_dv)
    if not path.exists():
        return None
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    token = str(payload.get("access_token", "")).strip()
    expires_at = parse_expiry(payload.get("expires_at"))
    if not token or expires_at is None:
        return None
    if datetime.now(timezone.utc) + timedelta(minutes=30) >= expires_at:
        return None
    return token


def fetch_token(app_key: str, app_secret: str, env_dv: str, retries: int) -> tuple[str, str, str]:
    cached = cached_token(env_dv)
    if cached:
        return cached, "existing_token", ""
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
    write_json(token_cache_path(env_dv), {"access_token": token, "expires_at": expires_at.isoformat()})
    return token, "new_token", expires_at.astimezone(KST).isoformat(timespec="seconds")


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


def build_price_row(symbol: str, info: dict[str, Any], price: dict[str, Any], *, observed_at: str, env_dv: str, market: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    symbol_name = text_first(info, ("prdt_abrv_name", "prdt_name", "prdt_name120")) or text_first(price, ("hts_kor_isnm", "bstp_kor_isnm")) or symbol
    current_price = parse_int(first_present(price, ("stck_prpr", "thdt_clpr", "stck_prdy_clpr")))
    required_missing: list[str] = []
    if not symbol_name or symbol_name == symbol:
        required_missing.append("symbol_name")
    if current_price is None:
        required_missing.append("price.current_or_last")
    if not observed_at:
        required_missing.append("price.observed_at")

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

    sources = [
        {"api": "direct_kis.search_stock_info", "env_dv": env_dv, "market": market},
        {"api": "direct_kis.inquire_price", "env_dv": env_dv, "market": market},
    ]
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
    if endpoint_name == "inquire_balance":
        tr_id = endpoint["tr_id_demo"] if env_dv == "demo" else endpoint["tr_id_real"]
    headers = base_headers(app_key, app_secret, token, str(tr_id))
    if tr_cont:
        headers["tr_cont"] = tr_cont
    body, response_headers = retry_json("GET", endpoint["path"], headers=headers, params=params, retries=retries)
    if not response_success(body):
        message = str(body.get("msg1") or body.get("msg_cd") or body.get("rt_cd") or "KIS API failed")
        raise RuntimeError(message)
    return body, response_headers


def collect_price_chart(symbols: list[str], *, run_id: str, started_at: str, env_dv: str, market: str, app_key: str, app_secret: str, token: str, retries: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    artifact_errors: list[dict[str, Any]] = []
    for symbol in symbols:
        symbol_errors: list[dict[str, Any]] = []
        info: dict[str, Any] = {}
        price: dict[str, Any] = {}
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
        row = build_price_row(symbol, info, price, observed_at=observed_at, env_dv=env_dv, market=market, errors=symbol_errors)
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
    output_dir: Path,
    token_status: str,
    token_expires_at: str,
) -> dict[str, Any]:
    children = [price_artifact, account_artifact]
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
            "collection_summary": str(output_dir / "collection-summary.json"),
        },
        "counts": {
            "input_symbols": len(symbols),
            "price_symbols": len(price_artifact.get("symbols", [])),
            "account_symbols": len(account_artifact.get("symbols", [])),
            "price_errors": len(price_artifact.get("errors", [])),
            "account_errors": len(account_artifact.get("errors", [])),
        },
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
        write_json(output_dir / "price-chart.json", price_artifact)
        write_json(output_dir / "account-before-order.json", account_artifact)
        summary = build_collection_summary(
            run_id=args.run_id,
            started_at=started_at,
            env_dv=env_dv,
            symbols=symbols,
            price_artifact=price_artifact,
            account_artifact=account_artifact,
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
    write_json(account_path, account_artifact)
    children.append(account_artifact)

    summary = build_collection_summary(
        run_id=args.run_id,
        started_at=started_at,
        env_dv=env_dv,
        symbols=symbols,
        price_artifact=price_artifact,
        account_artifact=account_artifact,
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
    info = {"prdt_abrv_name": "ACE GOLD ETF", "scty_grp_id_cd": "EF", "etf_dvsn_cd": "02"}
    price = {"stck_prpr": "18590", "prdy_ctrt": "1.23", "acml_vol": "1000"}
    row = build_price_row("411060", info, price, observed_at="2026-06-18T09:00:00+09:00", env_dv="real", market="J", errors=[])
    assert row["product_type"] == "etf"
    assert row["price"]["current_or_last"] == 18590
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
