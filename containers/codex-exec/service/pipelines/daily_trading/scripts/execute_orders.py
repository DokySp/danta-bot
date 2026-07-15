#!/usr/bin/env python3
"""Execute daily-trading orders from execution.json.

Refresh orderability gates, reconcile existing active orders, and submit,
cancel, or correct orders only when --submit is explicitly present.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import sys
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

ENDPOINTS = {
    "inquire_psbl_order": ("/uapi/domestic-stock/v1/trading/inquire-psbl-order", "GET", "TTTC8908R", "VTTC8908R"),
    "inquire_psbl_sell": ("/uapi/domestic-stock/v1/trading/inquire-psbl-sell", "GET", "TTTC8408R", "VTTC8408R"),
    "inquire_psbl_rvsecncl": ("/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl", "GET", "TTTC0084R", "VTTC0084R"),
    "inquire_daily_ccld": ("/uapi/domestic-stock/v1/trading/inquire-daily-ccld", "GET", "TTTC0081R", "VTTC0081R"),
    "order_resv": ("/uapi/domestic-stock/v1/trading/order-resv", "POST", "CTSC0008U", "VTSC0008U"),
    "order_resv_ccnl": ("/uapi/domestic-stock/v1/trading/order-resv-ccnl", "GET", "CTSC0004R", "VTSC0004R"),
}

BROKER_RECONCILIATION_POLL_DELAYS = (0.5, 1.0, 2.0)
BROKER_TERMINAL_STATUSES = {
    "filled",
    "rejected",
    "partially_filled_rejected",
    "canceled",
    "partially_filled_canceled",
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
}


def default_reservation_orgno() -> str:
    return os.environ.get("KIS_RSVN_ORD_ORGNO", "001").strip() or "001"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or value in (None, ""):
        return default
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def non_negative_int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            parsed = int(text)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def as_price(value: Any) -> int:
    return as_int(value)


def krx_tick_size(price: int) -> int:
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def normalize_limit_price(price: Any, side: str) -> int:
    value = as_price(price)
    if value <= 0:
        return value
    unit = krx_tick_size(value)
    remainder = value % unit
    if remainder == 0:
        return value
    if side == "buy":
        return value + (unit - remainder)
    if side == "sell":
        return value - remainder
    return value


def first(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def symbol_key(value: Any) -> str:
    if isinstance(value, dict):
        value = first(
            value,
            (
                "symbol_id",
                "symbol",
                "code",
                "pdno",
                "pd_no",
                "prdt_no",
                "shtn_pdno",
                "isu_cd",
                "PDNO",
                "PD_NO",
                "PRDT_NO",
                "SHTN_PDNO",
                "ISU_CD",
            ),
        )
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits and digits == text else text


PORTFOLIO_EXCEPT_ENV_VAR = "PORTFOLIO_EXCEPT_FILE"


def portfolio_except_file_candidates() -> list[Path]:
    configured = os.environ.get(PORTFOLIO_EXCEPT_ENV_VAR, "").strip()
    if configured:
        return [Path(configured).expanduser()]
    return [
        Path("/app/config/portfolio-except.txt"),
        Path("/workspace/containers/codex-exec/profiles/base/config/portfolio-except.txt"),
        Path("containers/codex-exec/profiles/base/config/portfolio-except.txt"),
    ]


def load_portfolio_except_symbols() -> set[str]:
    for path in portfolio_except_file_candidates():
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        symbols: set[str] = set()
        for line in text.splitlines():
            line = line.split("#", 1)[0]
            for entry in line.split(","):
                parts = entry.split()
                if parts:
                    symbols.add(symbol_key(parts[0]))
        return symbols
    return set()


# Score-band gate: new submissions require the analyst-review score band that
# selected the candidate (sell at or below SELL_MAX_SCORE, buy at or above
# BUY_MIN_SCORE). Cancels and corrections of existing active orders are exempt.
SELL_MAX_SCORE = 4.0
BUY_MIN_SCORE = 6.0


def score_band_block_reason(order: dict[str, Any], side: str) -> str:
    if side not in {"buy", "sell"}:
        return ""
    raw = order.get("final_first_score")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return "score_band_value_missing"
    if side == "sell" and raw > SELL_MAX_SCORE:
        return "sell_blocked_score_band"
    if side == "buy" and raw < BUY_MIN_SCORE:
        return "buy_blocked_score_band"
    return ""


def env_dv(raw: str | None) -> str:
    value = (raw or os.environ.get("CODEX_MCP_TRADING_ENV") or "acct").strip().lower()
    if value in {"paper", "demo", "mock"}:
        return "demo"
    if value in {"acct", "real"}:
        return "real"
    raise RuntimeError(f"unsupported trading env: {value}")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip().strip('"')
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def credentials(env: str) -> tuple[str, str]:
    if env == "demo":
        return require_env("KIS_PAPER_APP_KEY"), require_env("KIS_PAPER_APP_SECRET")
    return require_env("KIS_APP_KEY"), require_env("KIS_APP_SECRET")


def account_parts(env: str) -> tuple[str, str]:
    account = require_env("KIS_PAPER_STOCK" if env == "demo" else "KIS_ACCT_STOCK")
    compact = re.sub(r"[^0-9]", "", account)
    if len(compact) >= 10:
        return compact[:-2], compact[-2:]
    if len(compact) == 8:
        return compact, os.environ.get("KIS_PROD_TYPE", "01").strip('"') or "01"
    raise RuntimeError("KIS stock account must be 8 digits, or account+product code digits")


def token_helper_candidates() -> list[Path]:
    candidates = [
        Path("/app/skills/kis-token/scripts/kis_token.py"),
        Path("/codex-home/skills/kis-token/scripts/kis_token.py"),
        Path("/workspace/containers/codex-exec/shared-skills/kis-token/scripts/kis_token.py"),
    ]
    configured = os.environ.get("KIS_TOKEN_HELPER_PATH", "").strip()
    if configured:
        candidates.insert(0, Path(configured).expanduser())
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidates.append(parent / "kis-token" / "scripts" / "kis_token.py")
        candidates.append(parent / "shared-skills" / "kis-token" / "scripts" / "kis_token.py")
    return candidates


def load_token_helper() -> Any:
    for path in token_helper_candidates():
        if path.exists():
            spec = importlib.util.spec_from_file_location("codex_kis_token", path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
    raise RuntimeError("shared kis-token helper not found")


def redact(exc: BaseException | str) -> str:
    text = str(exc)
    for key in SENSITIVE_KEYS:
        text = re.sub(rf"(?i){re.escape(key)}[=:]\S+", f"{key}=<redacted>", text)
    return text[:500]


def request_json(method: str, path: str, headers: dict[str, str], *, params: dict[str, str] | None = None, payload: dict[str, Any] | None = None, timeout: int = 20) -> tuple[dict[str, Any], dict[str, str]]:
    url = KIS_BASE_URL + path
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        response_headers = {key.lower(): value for key, value in response.headers.items()}
    return (json.loads(body) if body.strip() else {}, response_headers)


def retry_json(method: str, path: str, headers: dict[str, str], *, params: dict[str, str] | None = None, payload: dict[str, Any] | None = None, retries: int = 2) -> tuple[dict[str, Any], dict[str, str]]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return request_json(method, path, headers, params=params, payload=payload)
        except HTTPError as exc:
            last_error = exc
            if exc.code in {400, 401, 403, 404}:
                raise
        except (TimeoutError, URLError, OSError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep([1, 2, 4][min(attempt, 2)])
    raise RuntimeError(f"KIS request failed after retries: {last_error}")


class Kis:
    def __init__(self, env: str, retries: int) -> None:
        self.env = env
        self.retries = retries
        self.app_key, self.app_secret = credentials(env)
        token_result = load_token_helper().get_token(self.app_key, self.app_secret, env_dv=env, retries=retries)
        self.token = token_result.token
        self.cano, self.product = account_parts(env)

    def headers(self, tr_id: str, payload: dict[str, Any] | None = None) -> dict[str, str]:
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if payload is not None:
            hash_headers = {
                "content-type": "application/json; charset=utf-8",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            }
            body, _ = retry_json("POST", "/uapi/hashkey", hash_headers, payload=payload, retries=self.retries)
            headers["hashkey"] = str(body.get("HASH") or body.get("hash") or "")
        return headers

    def call(self, name: str, *, params: dict[str, str] | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        path, method, tr_real, tr_demo = ENDPOINTS[name]
        tr_id = tr_demo if self.env == "demo" else tr_real
        # Do not replay order-changing POSTs after ambiguous transport failures.
        request_retries = 0 if method == "POST" else self.retries
        body, _ = retry_json(method, path, self.headers(tr_id, payload), params=params, payload=payload, retries=request_retries)
        if str(body.get("rt_cd", "0")) not in {"0", ""}:
            raise RuntimeError(str(body.get("msg1") or body.get("msg_cd") or body.get("rt_cd") or "KIS API failed"))
        return body


def rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in ("output", "output1", "output2"):
        value = body.get(key)
        if isinstance(value, dict):
            result.append(value)
        elif isinstance(value, list):
            result.extend(item for item in value if isinstance(item, dict))
    return result


def direction(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"01", "sell", "sll"} or "매도" in text:
        return "sell"
    if text in {"02", "buy"} or "매수" in text:
        return "buy"
    return ""


def direction_code(value: str) -> str:
    return "01" if value == "sell" else "02"


def order_path_api(order_path: str) -> tuple[str, str]:
    if order_path == "immediate":
        return "immediate", "order_cash"
    return "reservation", "order_resv"


def normalize_reservation(row: dict[str, Any]) -> dict[str, Any]:
    symbol = symbol_key(row)
    order_id = str(
        first(row, ("rsvn_ord_seq", "rsvn_ord_no", "odno", "ord_no", "RSVN_ORD_SEQ", "RSVN_ORD_NO", "ODNO", "ORD_NO"))
        or ""
    ).strip()
    orgno = str(
        first(
            row,
            (
                "rsvn_ord_orgno",
                "rsvn_ord_org_no",
                "rsvn_ord_org_no_cd",
                "ord_orgno",
                "ord_gno_brno",
                "RSVN_ORD_ORGNO",
                "RSVN_ORD_ORG_NO",
                "RSVN_ORD_ORG_NO_CD",
                "ORD_ORGNO",
                "ORD_GNO_BRNO",
            ),
        )
        or ""
    ).strip()
    reserved_quantity = as_int(
        first(
            row,
            (
                "rmn_qty",
                "ord_uncc_qty",
                "uncc_qty",
                "ord_rsvn_qty",
                "rsvn_ord_qty",
                "ord_qty",
                "RMN_QTY",
                "ORD_UNCC_QTY",
                "UNCC_QTY",
                "ORD_RSVN_QTY",
                "RSVN_ORD_QTY",
                "ORD_QTY",
            ),
        )
    )
    filled_quantity = as_int(first(row, ("tot_ccld_qty", "TOT_CCLD_QTY")))
    remaining = max(0, reserved_quantity - filled_quantity) if filled_quantity else reserved_quantity
    status_text = " ".join(
        str(row.get(key, ""))
        for key in (
            "rsvn_ord_stat_name",
            "rsvn_ord_stat_cd",
            "prcs_stat_name",
            "prcs_rslt",
            "cncl_yn",
            "RSVN_ORD_STAT_NAME",
            "RSVN_ORD_STAT_CD",
            "PRCS_STAT_NAME",
            "PRCS_RSLT",
            "CNCL_YN",
        )
    )
    processed_time = str(first(row, ("ord_tmd", "ORD_TMD")) or "").strip()
    inactive = any(marker in status_text for marker in ("취소", "완료", "거부", "거절", "만료", "실효", "미처리"))
    if "처리" in status_text and processed_time:
        inactive = True
    return {
        "symbol_id": symbol,
        "symbol_name": str(first(row, ("prdt_name", "prdt_abrv_name", "hts_kor_isnm", "kor_item_shtn_name", "PRDT_NAME", "PRDT_ABRV_NAME", "HTS_KOR_ISNM", "KOR_ITEM_SHTN_NAME")) or symbol),
        "order_id": order_id,
        "order_kind": "reservation",
        "direction": direction(first(row, ("sll_buy_dvsn_cd", "sll_buy_dvsn_name", "SLL_BUY_DVSN_CD", "SLL_BUY_DVSN_NAME"))),
        "remaining_quantity": remaining,
        "order_price": as_int(first(row, ("ord_unpr", "ord_rsvn_unpr", "rsvn_ord_unpr", "ord_prc", "ORD_UNPR", "ORD_RSVN_UNPR", "RSVN_ORD_UNPR", "ORD_PRC"))),
        "order_api": "order_resv",
        "order_path": "reservation",
        "execution_environment": "",
        "observed_at": now_iso(),
        "active_status": "inactive" if inactive or remaining <= 0 else "active",
        "rsvn_ord_seq": order_id,
        "rsvn_ord_orgno": (orgno or default_reservation_orgno()) if order_id else "",
        "rsvn_ord_ord_dt": str(first(row, ("rsvn_ord_ord_dt", "ord_dt", "RSVN_ORD_ORD_DT", "ORD_DT")) or "").strip(),
    }


def normalize_pending_order(row: dict[str, Any]) -> dict[str, Any]:
    symbol = symbol_key(row)
    order_id = str(first(row, ("odno", "ord_no", "orgn_odno", "ODNO", "ORD_NO", "ORGN_ODNO")) or "").strip()
    remaining = as_int(first(row, ("ord_uncc_qty", "uncc_qty", "rmn_qty", "ord_qty", "ORD_UNCC_QTY", "UNCC_QTY", "RMN_QTY", "ORD_QTY")))
    status_text = " ".join(str(row.get(key, "")) for key in ("ord_stat_name", "ord_stat_cd", "cncl_yn", "ORD_STAT_NAME", "ORD_STAT_CD", "CNCL_YN"))
    inactive = any(marker in status_text for marker in ("취소", "완료", "체결", "거부", "거절", "만료", "실효"))
    return {
        "symbol_id": symbol,
        "symbol_name": str(first(row, ("prdt_name", "prdt_abrv_name", "hts_kor_isnm", "PRDT_NAME", "PRDT_ABRV_NAME", "HTS_KOR_ISNM")) or symbol),
        "order_id": order_id,
        "order_kind": "pending",
        "direction": direction(first(row, ("sll_buy_dvsn_cd", "sll_buy_dvsn_name", "SLL_BUY_DVSN_CD", "SLL_BUY_DVSN_NAME"))),
        "remaining_quantity": remaining,
        "order_price": as_int(first(row, ("ord_unpr", "ord_prc", "ORD_UNPR", "ORD_PRC"))),
        "order_api": "order_cash",
        "order_path": "immediate",
        "execution_environment": "",
        "observed_at": now_iso(),
        "active_status": "inactive" if inactive or remaining <= 0 else "active",
        "krx_fwdg_ord_orgno": str(first(row, ("krx_fwdg_ord_orgno", "KRX_FWDG_ORD_ORGNO", "ord_gno_brno", "ORD_GNO_BRNO")) or "").strip(),
        "orgn_odno": order_id,
        "ord_dvsn": str(first(row, ("ord_dvsn", "ORD_DVSN")) or "00").strip() or "00",
        "excg_id_dvsn_cd": str(first(row, ("excg_id_dvsn_cd", "EXCG_ID_DVSN_CD")) or "KRX").strip() or "KRX",
    }


def active_quantities(active_orders: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for item in active_orders:
        if item.get("active_status") != "active":
            continue
        symbol = symbol_key(item)
        side = str(item.get("direction") or "")
        if symbol and side in {"buy", "sell"}:
            result.setdefault(symbol, {"buy": 0, "sell": 0})[side] += as_int(item.get("remaining_quantity"))
    return result


def fetch_reservations(kis: Kis, start_date: str, end_date: str) -> list[dict[str, Any]]:
    body = kis.call(
        "order_resv_ccnl",
        params={
            "RSVN_ORD_ORD_DT": start_date,
            "RSVN_ORD_END_DT": end_date,
            "TMNL_MDIA_KIND_CD": "00",
            "CANO": kis.cano,
            "ACNT_PRDT_CD": kis.product,
            "PRCS_DVSN_CD": "0",
            "CNCL_YN": "Y",
            "RSVN_ORD_SEQ": "",
            "PDNO": "",
            "SLL_BUY_DVSN_CD": "",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        },
    )
    normalized = [normalize_reservation(row) for row in rows(body)]
    for item in normalized:
        item["execution_environment"] = kis.env
    return normalized


def fetch_pending_orders(kis: Kis) -> list[dict[str, Any]]:
    body = kis.call(
        "inquire_psbl_rvsecncl",
        params={
            "CANO": kis.cano,
            "ACNT_PRDT_CD": kis.product,
            "INQR_DVSN_1": "0",
            "INQR_DVSN_2": "0",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        },
    )
    normalized = [normalize_pending_order(row) for row in rows(body)]
    for item in normalized:
        item["execution_environment"] = kis.env
    return normalized


def broker_order_id(row: dict[str, Any]) -> str:
    return str(first(row, ("odno", "ord_no", "ODNO", "ORD_NO")) or "").strip()


def normalize_broker_reconciliation(
    row: dict[str, Any] | None,
    *,
    requested_quantity: int,
    observed_at: str,
) -> dict[str, Any]:
    if row is None:
        return {
            "status": "unconfirmed",
            "terminal": False,
            "ordered_quantity": requested_quantity,
            "filled_quantity": 0,
            "rejected_quantity": 0,
            "canceled_quantity": 0,
            "remaining_quantity": requested_quantity,
            "filled_price": 0,
            "observed_at": observed_at,
            "source_api": "inquire_daily_ccld",
        }

    ordered = as_int(first(row, ("ord_qty", "ORD_QTY")), requested_quantity) or requested_quantity
    filled = as_int(first(row, ("tot_ccld_qty", "TOT_CCLD_QTY")))
    rejected = as_int(first(row, ("rjct_qty", "RJCT_QTY")))
    canceled = as_int(first(row, ("cnc_cfrm_qty", "CNC_CFRM_QTY")))
    remaining = as_int(first(row, ("rmn_qty", "RMN_QTY")))
    filled_price = as_int(first(row, ("avg_prvs", "AVG_PRVS", "avg_ccld_prc", "AVG_CCLD_PRC")))

    if rejected > 0 and filled > 0:
        status = "partially_filled_rejected"
    elif rejected > 0:
        status = "rejected"
    elif canceled > 0 and filled > 0:
        status = "partially_filled_canceled"
    elif canceled > 0:
        status = "canceled"
    elif ordered > 0 and filled >= ordered:
        status = "filled"
    elif filled > 0:
        status = "partially_filled"
    elif remaining > 0:
        status = "pending"
    else:
        status = "accepted"

    return {
        "status": status,
        "terminal": status in BROKER_TERMINAL_STATUSES,
        "ordered_quantity": ordered,
        "filled_quantity": filled,
        "rejected_quantity": rejected,
        "canceled_quantity": canceled,
        "remaining_quantity": remaining,
        "filled_price": filled_price,
        "observed_at": observed_at,
        "source_api": "inquire_daily_ccld",
    }


def execution_order_day(execution: dict[str, Any]) -> str:
    started_at = str(execution.get("started_at") or "").strip()
    if started_at:
        try:
            return datetime.fromisoformat(started_at).astimezone(KST).strftime("%Y%m%d")
        except ValueError:
            pass
    return datetime.now(KST).strftime("%Y%m%d")


def fetch_cash_order_status_rows(kis: Kis, day: str, order_id: str = "") -> list[dict[str, Any]]:
    body = kis.call(
        "inquire_daily_ccld",
        params={
            "CANO": kis.cano,
            "ACNT_PRDT_CD": kis.product,
            "INQR_STRT_DT": day,
            "INQR_END_DT": day,
            "SLL_BUY_DVSN_CD": "00",
            "INQR_DVSN": "00",
            "PDNO": "",
            "CCLD_DVSN": "00",
            "ORD_GNO_BRNO": "",
            "ODNO": order_id,
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        },
    )
    return [row for row in rows(body) if broker_order_id(row)]


def reconcile_submitted_cash_orders(
    kis: Kis,
    execution: dict[str, Any],
    *,
    poll_delays: tuple[float, ...] = BROKER_RECONCILIATION_POLL_DELAYS,
) -> dict[str, Any]:
    targets = [
        order
        for order in execution.get("orders", [])
        if isinstance(order, dict)
        and order.get("result") == "submitted"
        and order.get("order_path") == "immediate"
        and order.get("direction") in {"buy", "sell"}
    ]
    if not targets:
        summary = {
            "status": "skipped",
            "observed_at": now_iso(),
            "source_api": "inquire_daily_ccld",
            "submitted_cash_order_count": 0,
            "filled_order_count": 0,
            "partially_filled_order_count": 0,
            "pending_order_count": 0,
            "rejected_order_count": 0,
            "canceled_order_count": 0,
            "unconfirmed_order_count": 0,
            "lookup_errors": [],
        }
        execution["broker_reconciliation"] = summary
        return summary

    target_ids = {
        str(order.get("order_or_reservation_id") or "").strip()
        for order in targets
        if str(order.get("order_or_reservation_id") or "").strip()
    }
    latest_by_order_id: dict[str, dict[str, Any]] = {}
    lookup_errors: list[str] = []
    day = execution_order_day(execution)
    observed_at = now_iso()

    if target_ids:
        delays = poll_delays or (0.0,)
        for poll_index, delay in enumerate(delays):
            if delay > 0:
                time.sleep(delay)
            observed_at = now_iso()
            try:
                status_rows = fetch_cash_order_status_rows(kis, day)
            except Exception as exc:  # noqa: BLE001 - later polls may recover
                lookup_errors.append(redact(exc))
                continue
            for row in status_rows:
                order_id = broker_order_id(row)
                if order_id in target_ids:
                    latest_by_order_id[order_id] = row
            if poll_index == len(delays) - 1:
                for missing_order_id in sorted(target_ids - set(latest_by_order_id)):
                    try:
                        targeted_rows = fetch_cash_order_status_rows(kis, day, missing_order_id)
                    except Exception as exc:  # noqa: BLE001 - preserve partial status below
                        lookup_errors.append(redact(exc))
                        continue
                    matching_row = next(
                        (row for row in targeted_rows if broker_order_id(row) == missing_order_id),
                        None,
                    )
                    if matching_row is not None:
                        latest_by_order_id[missing_order_id] = matching_row
            if all(
                normalize_broker_reconciliation(
                    latest_by_order_id.get(order_id),
                    requested_quantity=as_int(order.get("validated_order_quantity")),
                    observed_at=observed_at,
                ).get("terminal")
                for order in targets
                if (order_id := str(order.get("order_or_reservation_id") or "").strip())
            ) and len(target_ids) == len(targets):
                break

    statuses: list[str] = []
    for order in targets:
        order_id = str(order.get("order_or_reservation_id") or "").strip()
        reconciliation = normalize_broker_reconciliation(
            latest_by_order_id.get(order_id) if order_id else None,
            requested_quantity=as_int(order.get("validated_order_quantity")),
            observed_at=observed_at,
        )
        order["broker_reconciliation"] = reconciliation
        statuses.append(str(reconciliation.get("status") or "unconfirmed"))

    filled_count = statuses.count("filled")
    # Aggregate buckets are mutually exclusive so one submitted order is counted once.
    partially_filled_count = statuses.count("partially_filled")
    pending_count = statuses.count("pending") + statuses.count("accepted")
    rejected_count = statuses.count("rejected") + statuses.count("partially_filled_rejected")
    canceled_count = statuses.count("canceled") + statuses.count("partially_filled_canceled")
    unconfirmed_count = statuses.count("unconfirmed")
    summary_status = "success" if filled_count == len(targets) else "partial"
    summary = {
        "status": summary_status,
        "observed_at": observed_at,
        "source_api": "inquire_daily_ccld",
        "submitted_cash_order_count": len(targets),
        "filled_order_count": filled_count,
        "partially_filled_order_count": partially_filled_count,
        "pending_order_count": pending_count,
        "rejected_order_count": rejected_count,
        "canceled_order_count": canceled_count,
        "unconfirmed_order_count": unconfirmed_count,
        "lookup_errors": lookup_errors[-3:],
    }
    execution["broker_reconciliation"] = summary
    execution["errors"] = [
        item
        for item in execution.get("errors", [])
        if isinstance(item, dict) and not str(item.get("code") or "").startswith("broker_order_")
    ]
    if rejected_count:
        execution["errors"].append(error("broker_order_rejected", f"{rejected_count} submitted cash order(s) rejected by KIS"))
    if canceled_count:
        execution["errors"].append(error("broker_order_canceled", f"{canceled_count} submitted cash order(s) canceled before fill"))
    if unconfirmed_count:
        execution["errors"].append(
            error(
                "broker_order_status_unconfirmed",
                f"broker status unconfirmed for {unconfirmed_count} submitted cash order(s)",
            )
        )
    if summary_status != "success" and execution.get("status") == "success":
        execution["status"] = "partial"
    return summary


def normalize_execution_order_prices(execution: dict[str, Any]) -> None:
    for item in execution.get("orders", []):
        if not isinstance(item, dict):
            continue
        side = str(item.get("direction") or "")
        final_quantity = as_int(item.get("final_holding_quantity"))
        expected = as_int(item.get("expected_holding_quantity"), as_int(item.get("current_live_holding_quantity")))
        if side not in {"buy", "sell"}:
            delta = final_quantity - expected
            side = "buy" if delta > 0 else "sell" if delta < 0 else ""
        normalized = normalize_limit_price(item.get("order_price"), side)
        original = as_price(item.get("order_price"))
        if normalized > 0 and original != normalized:
            item["order_price"] = normalized
            item["order_price_adjustment"] = {
                "from": original,
                "to": normalized,
                "reason": "krx_tick_size",
            }


def buy_capacity(kis: Kis, symbol: str, price: int) -> dict[str, int]:
    body = kis.call(
        "inquire_psbl_order",
        params={
            "CANO": kis.cano,
            "ACNT_PRDT_CD": kis.product,
            "PDNO": symbol,
            "ORD_UNPR": str(price),
            "ORD_DVSN": "00",
            "CMA_EVLU_AMT_ICLD_YN": "Y",
            "OVRS_ICLD_YN": "N",
        },
    )
    row = (rows(body) or [{}])[0]
    return {
        "max_buy_amt": as_int(first(row, ("max_buy_amt", "nrcvb_buy_amt", "ord_psbl_cash", "ord_psbl_amt", "MAX_BUY_AMT", "NRCVB_BUY_AMT", "ORD_PSBL_CASH", "ORD_PSBL_AMT"))),
        "max_buy_qty": as_int(first(row, ("max_buy_qty", "nrcvb_buy_qty", "ord_psbl_qty", "MAX_BUY_QTY", "NRCVB_BUY_QTY", "ORD_PSBL_QTY"))),
    }


def sell_capacity(kis: Kis, symbol: str) -> dict[str, int]:
    body = kis.call(
        "inquire_psbl_sell",
        params={
            "CANO": kis.cano,
            "ACNT_PRDT_CD": kis.product,
            "PDNO": symbol,
        },
    )
    row = (rows(body) or [{}])[0]
    return {
        "max_sell_qty": as_int(first(row, ("ord_psbl_qty", "sell_psbl_qty", "slpsblqty", "ORD_PSBL_QTY", "SELL_PSBL_QTY", "SLPSBLQTY"))),
    }


def submit_reservation(kis: Kis, order: dict[str, Any]) -> str:
    payload = {
        "CANO": kis.cano,
        "ACNT_PRDT_CD": kis.product,
        "PDNO": order["symbol_id"],
        "ORD_QTY": str(as_int(order.get("validated_order_quantity"))),
        "ORD_UNPR": str(as_price(order.get("order_price"))),
        "SLL_BUY_DVSN_CD": direction_code(str(order.get("direction") or "")),
        "ORD_DVSN_CD": "00",
        "ORD_OBJT_CBLC_DVSN_CD": "10",
    }
    body = kis.call("order_resv", payload=payload)
    row = (rows(body) or [{}])[0]
    return str(first(row, ("RSVN_ORD_SEQ", "rsvn_ord_seq", "ODNO", "odno")) or "").strip()


def cash_order_tr_id(env: str, side: str) -> str:
    if env == "demo":
        return "VTTC0011U" if side == "sell" else "VTTC0012U"
    return "TTTC0011U" if side == "sell" else "TTTC0012U"


def submit_cash(kis: Kis, order: dict[str, Any]) -> str:
    side = str(order.get("direction") or "")
    payload = {
        "CANO": kis.cano,
        "ACNT_PRDT_CD": kis.product,
        "PDNO": order["symbol_id"],
        "ORD_DVSN": str(order.get("ord_dvsn") or "00"),
        "ORD_QTY": str(as_int(order.get("validated_order_quantity"))),
        "ORD_UNPR": str(as_price(order.get("order_price"))),
        "EXCG_ID_DVSN_CD": str(order.get("excg_id_dvsn_cd") or "KRX"),
        "SLL_TYPE": "01" if side == "sell" else "",
        "CNDT_PRIC": str(order.get("cndt_pric") or ""),
    }
    body, _ = retry_json("POST", "/uapi/domestic-stock/v1/trading/order-cash", kis.headers(cash_order_tr_id(kis.env, side), payload), payload=payload, retries=0)
    if str(body.get("rt_cd", "0")) not in {"0", ""}:
        raise RuntimeError(str(body.get("msg1") or body.get("msg_cd") or body.get("rt_cd") or "KIS order_cash failed"))
    row = (rows(body) or [{}])[0]
    return str(first(row, ("ODNO", "odno", "ORD_NO", "ord_no")) or "").strip()


def adjust_reservation(kis: Kis, active: dict[str, Any], desired: dict[str, Any] | None) -> str:
    ord_type = "modify" if desired else "cancel"
    payload = {
        "CANO": kis.cano,
        "ACNT_PRDT_CD": kis.product,
        "RSVN_ORD_SEQ": str(active.get("rsvn_ord_seq") or active.get("order_id") or ""),
        "RSVN_ORD_ORGNO": str(active.get("rsvn_ord_orgno") or ""),
        "RSVN_ORD_ORD_DT": str(active.get("rsvn_ord_ord_dt") or ""),
        "ORD_TYPE": ord_type,
    }
    missing = [key for key in ("RSVN_ORD_SEQ", "RSVN_ORD_ORGNO", "RSVN_ORD_ORD_DT") if not payload.get(key)]
    if missing:
        raise RuntimeError(f"missing reservation adjustment identifiers: {','.join(missing)}")
    tr_id = "CTSC0013U" if ord_type == "modify" else "CTSC0009U"
    if desired:
        payload.update(
            {
                "PDNO": desired["symbol_id"],
                "ORD_QTY": str(as_int(desired.get("validated_order_quantity"))),
                "ORD_UNPR": str(as_price(desired.get("order_price"))),
                "SLL_BUY_DVSN_CD": direction_code(str(desired.get("direction") or "")),
                "ORD_DVSN_CD": str(desired.get("ord_dvsn") or "00"),
                "ORD_OBJT_CBLC_DVSN_CD": "10",
            }
        )
    body, _ = retry_json("POST", "/uapi/domestic-stock/v1/trading/order-resv-rvsecncl", kis.headers(tr_id, payload), payload=payload, retries=0)
    if str(body.get("rt_cd", "0")) not in {"0", ""}:
        raise RuntimeError(str(body.get("msg1") or body.get("msg_cd") or body.get("rt_cd") or "KIS order_resv_rvsecncl failed"))
    row = (rows(body) or [{}])[0]
    return str(first(row, ("RSVN_ORD_SEQ", "rsvn_ord_seq", "ODNO", "odno")) or active.get("order_id") or "").strip()


def adjust_cash_order(kis: Kis, active: dict[str, Any], desired: dict[str, Any] | None) -> str:
    payload = {
        "CANO": kis.cano,
        "ACNT_PRDT_CD": kis.product,
        "KRX_FWDG_ORD_ORGNO": str(active.get("krx_fwdg_ord_orgno") or ""),
        "ORGN_ODNO": str(active.get("orgn_odno") or active.get("order_id") or ""),
        "ORD_DVSN": str((desired or active).get("ord_dvsn") or "00"),
        "RVSE_CNCL_DVSN_CD": "01" if desired else "02",
        "ORD_QTY": str(as_int((desired or active).get("validated_order_quantity") or active.get("remaining_quantity"))),
        "ORD_UNPR": str(as_price((desired or active).get("order_price"))),
        "QTY_ALL_ORD_YN": "N" if desired else "Y",
        "EXCG_ID_DVSN_CD": str((desired or active).get("excg_id_dvsn_cd") or active.get("excg_id_dvsn_cd") or "KRX"),
    }
    missing = [key for key in ("KRX_FWDG_ORD_ORGNO", "ORGN_ODNO") if not payload.get(key)]
    if missing:
        raise RuntimeError(f"missing cash adjustment identifiers: {','.join(missing)}")
    body, _ = retry_json("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl", kis.headers("VTTC0013U" if kis.env == "demo" else "TTTC0013U", payload), payload=payload, retries=0)
    if str(body.get("rt_cd", "0")) not in {"0", ""}:
        raise RuntimeError(str(body.get("msg1") or body.get("msg_cd") or body.get("rt_cd") or "KIS order_rvsecncl failed"))
    row = (rows(body) or [{}])[0]
    return str(first(row, ("ODNO", "odno", "ORD_NO", "ord_no")) or active.get("order_id") or "").strip()


def submit_order(kis: Kis, order: dict[str, Any]) -> str:
    return submit_cash(kis, order) if order.get("order_path") == "immediate" else submit_reservation(kis, order)


def attempt(api_name: str, result: str, message: str, error_code: str = "") -> dict[str, Any]:
    return {"api_name": api_name, "attempt": 1, "delay_seconds": 0, "error_code": error_code, "message": message, "result": result}


def reduce_order_quantity(order: dict[str, Any], *, from_qty: int, to_qty: int, reason: str, gate: str, limit: int) -> None:
    original_qty = as_int(order.get("requested_order_quantity"), from_qty)
    order.setdefault("requested_order_quantity", from_qty)
    order.setdefault("requested_additional_required_quantity", as_int(order.get("additional_required_quantity")))
    order["validated_order_quantity"] = to_qty
    if order.get("direction") == "sell":
        order["additional_required_quantity"] = -to_qty
    elif order.get("direction") == "buy":
        order["additional_required_quantity"] = to_qty
    order["quantity_adjustment"] = {
        "from": original_qty,
        "to": to_qty,
        "reason": reason,
        "limit": limit,
    }
    order["attempts"].append(attempt(gate, "adjusted", f"quantity reduced from {from_qty} to {to_qty}", "quantity_adjustment"))


def block_order(order: dict[str, Any], *, reason: str, gate: str, message: str, error_code: str) -> None:
    order.setdefault("requested_order_quantity", as_int(order.get("validated_order_quantity")))
    order.setdefault("requested_additional_required_quantity", as_int(order.get("additional_required_quantity")))
    order["result"] = "blocked"
    order["reason"] = reason
    order["attempts"].append(attempt(gate, "blocked", message, error_code))


def buy_cash_limit(capacities: dict[str, dict[str, int]]) -> int | None:
    positive = [as_int(item.get("max_buy_amt")) for item in capacities.values() if as_int(item.get("max_buy_amt")) > 0]
    if positive:
        return min(positive)
    return None


def apply_quantity_gates(
    order: dict[str, Any],
    *,
    symbol: str,
    side: str,
    qty: int,
    price: int,
    current: int,
    active_sell_quantity: int,
    capacities: dict[str, dict[str, int]],
    sell_capacities: dict[str, dict[str, int]],
    used_cash: int,
    cash_limit: int | None,
    local_sell_gate: bool,
) -> tuple[int, int, bool]:
    if local_sell_gate and side == "sell":
        available_sell = max(0, current - active_sell_quantity)
        if qty > available_sell:
            if available_sell <= 0:
                block_order(
                    order,
                    reason="sell_quantity_exceeds_available_holding",
                    gate="local_sell_gate",
                    message=f"available_sell={available_sell}",
                    error_code="sell_gate",
                )
                return qty, 0, True
            reduce_order_quantity(
                order,
                from_qty=qty,
                to_qty=available_sell,
                reason="sell_quantity_reduced_to_available_holding",
                gate="local_sell_gate",
                limit=available_sell,
            )
            qty = available_sell
    if side == "sell":
        sell_cap = sell_capacities.get(symbol)
        if isinstance(sell_cap, dict) and "max_sell_qty" in sell_cap:
            max_sell_qty = as_int(sell_cap.get("max_sell_qty"))
            if qty > max_sell_qty:
                if max_sell_qty <= 0:
                    block_order(
                        order,
                        reason="sell_quantity_exceeds_order_available_quantity",
                        gate="inquire_psbl_sell",
                        message=f"max_sell_qty={max_sell_qty}",
                        error_code="sell_gate",
                    )
                    return qty, 0, True
                reduce_order_quantity(
                    order,
                    from_qty=qty,
                    to_qty=max_sell_qty,
                    reason="sell_quantity_reduced_to_order_available_quantity",
                    gate="inquire_psbl_sell",
                    limit=max_sell_qty,
                )
                qty = max_sell_qty
    required_cash = 0
    if side == "buy":
        cap = capacities.get(symbol)
        if isinstance(cap, dict) and "max_buy_qty" in cap:
            max_qty = as_int(cap.get("max_buy_qty"))
            if qty > max_qty:
                if max_qty <= 0:
                    block_order(
                        order,
                        reason="buy_quantity_exceeds_order_available_quantity",
                        gate="inquire_psbl_order",
                        message=f"max_buy_qty={max_qty}",
                        error_code="cash_gate",
                    )
                    return qty, 0, True
                reduce_order_quantity(
                    order,
                    from_qty=qty,
                    to_qty=max_qty,
                    reason="buy_quantity_reduced_to_order_available_quantity",
                    gate="inquire_psbl_order",
                    limit=max_qty,
                )
                qty = max_qty
        if not isinstance(cap, dict) or as_int(cap.get("max_buy_amt")) <= 0:
            block_order(
                order,
                reason="buy_cash_limit_missing",
                gate="inquire_psbl_order",
                message="max_buy_amt unavailable from order-available lookup",
                error_code="cash_gate",
            )
            return qty, 0, True
        required_cash = qty * price
        if cash_limit is None:
            block_order(
                order,
                reason="buy_cash_limit_missing",
                gate="inquire_psbl_order",
                message="max_buy_amt unavailable from order-available lookup",
                error_code="cash_gate",
            )
            return qty, 0, True
        if cash_limit and used_cash + required_cash > cash_limit:
            remaining_cash = max(0, cash_limit - used_cash)
            affordable_qty = remaining_cash // price if price > 0 else 0
            if affordable_qty <= 0:
                block_order(
                    order,
                    reason="buy_cash_gate_reduced_reverse_rank",
                    gate="inquire_psbl_order",
                    message=f"buy candidates exceeded latest buy cash limit {cash_limit}",
                    error_code="cash_gate",
                )
                return qty, 0, True
            reduce_order_quantity(
                order,
                from_qty=qty,
                to_qty=affordable_qty,
                reason="buy_quantity_reduced_to_remaining_cash",
                gate="inquire_psbl_order",
                limit=remaining_cash,
            )
            qty = affordable_qty
            required_cash = qty * price
    return qty, required_cash, False


def error(code: str, message: str) -> dict[str, Any]:
    return {"stage": "order-execution", "source": "execute_orders", "code": code, "message": message, "required": True}


def adjustment_row(active: dict[str, Any], *, action: str, reason: str, result: str) -> dict[str, Any]:
    order_api = active.get("order_api", "")
    order_path = active.get("order_path", "")
    direction_value = active.get("direction", "")
    remaining_quantity = as_int(active.get("remaining_quantity"))
    order_price = as_int(active.get("order_price"))
    active_status = active.get("active_status", "")
    return {
        "symbol_id": symbol_key(active),
        "symbol_name": active.get("symbol_name") or symbol_key(active),
        "existing_order_id": active.get("order_id", ""),
        "existing_order_kind": active.get("order_kind", ""),
        "existing_execution_environment": active.get("execution_environment", ""),
        "existing_direction": direction_value,
        "existing_remaining_quantity": remaining_quantity,
        "existing_order_price": order_price,
        "existing_order_api": order_api,
        "existing_order_path": order_path,
        "existing_active_status": active_status,
        "direction": direction_value,
        "remaining_quantity": remaining_quantity,
        "order_price": order_price,
        "order_api": order_api,
        "order_path": order_path,
        "active_status": active_status,
        "action": action,
        "reason": reason,
        "result": result,
        "adjustment_api_name": "order_resv_rvsecncl" if order_path == "reservation" else "order_rvsecncl" if order_path == "immediate" else "",
        "confirmed_status": "confirmed" if action == "keep" else "unconfirmed",
        "confirmation_status": "confirmed" if action == "keep" else "unconfirmed",
        "confirmed_at": "",
        "confirmation_artifact": "account-before-order.json",
        "replacement_required": action in {"cancel", "replace"},
        "replacement_order_id": "",
        "attempts": [],
    }


def mismatched_active_orders(active_orders: list[dict[str, Any]], side: str, qty: int, price: int, order_path: str, order_api: str) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for item in active_orders:
        if item.get("order_path") != order_path or item.get("order_api") != order_api:
            mismatches.append(item)
            continue
        if item.get("direction") != side:
            mismatches.append(item)
            continue
        if as_int(item.get("remaining_quantity")) != qty:
            mismatches.append(item)
            continue
        if as_int(item.get("order_price")) != price:
            mismatches.append(item)
            continue
    return mismatches


def matching_single_order(active_orders: list[dict[str, Any]], side: str, qty: int, price: int, order_path: str, order_api: str) -> dict[str, Any] | None:
    if len(active_orders) != 1:
        return None
    item = active_orders[0]
    if active_order_missing_fields(item):
        return None
    if mismatched_active_orders([item], side, qty, price, order_path, order_api):
        return None
    return item


def active_order_missing_fields(active: dict[str, Any]) -> list[str]:
    required = (
        "symbol_id",
        "symbol_name",
        "order_id",
        "order_kind",
        "direction",
        "remaining_quantity",
        "order_price",
        "order_api",
        "order_path",
        "execution_environment",
        "active_status",
        "observed_at",
    )
    missing = [key for key in required if active.get(key) in (None, "")]
    if as_int(active.get("remaining_quantity")) <= 0:
        missing.append("remaining_quantity")
    if as_int(active.get("order_price")) <= 0:
        missing.append("order_price")
    return sorted(set(missing))


def can_correct(active: dict[str, Any], side: str, order_path: str, order_api: str) -> bool:
    if active_order_missing_fields(active):
        return False
    return active.get("direction") == side and active.get("order_path") == order_path and active.get("order_api") == order_api


def adjust_active_order(kis: Kis | None, active: dict[str, Any], desired: dict[str, Any] | None) -> tuple[str, str, str]:
    if kis is None:
        return "", "blocked", "KIS client unavailable for active-order adjustment"
    if active.get("order_path") == "reservation":
        request_id = adjust_reservation(kis, active, desired)
        return request_id, "correct" if desired else "cancel", "reservation adjustment accepted"
    if active.get("order_path") == "immediate":
        request_id = adjust_cash_order(kis, active, desired)
        return request_id, "correct" if desired else "cancel", "cash order adjustment accepted"
    raise RuntimeError("unsupported active order path for adjustment")


def default_date_range() -> tuple[str, str]:
    today = datetime.now(KST)
    return (today - timedelta(days=30)).strftime("%Y%m%d"), (today + timedelta(days=30)).strftime("%Y%m%d")


def refresh_gates(args: argparse.Namespace, account: dict[str, Any], execution: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]], dict[str, dict[str, int]], list[dict[str, Any]], Kis | None]:
    if args.offline:
        active = [item for item in account.get("active_orders", []) if isinstance(item, dict)]
        return active, {}, {}, [], None
    kis = Kis(env_dv(args.env or account.get("execution_environment")), args.retries)
    start_date, end_date = args.reservation_start_date, args.reservation_end_date
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    active = fetch_reservations(kis, start_date, end_date) + fetch_pending_orders(kis)
    capacities: dict[str, dict[str, int]] = {}
    sell_capacities: dict[str, dict[str, int]] = {}
    errors: list[dict[str, Any]] = []
    for item in execution.get("orders", []):
        if not isinstance(item, dict):
            continue
        symbol = symbol_key(item)
        price = as_price(item.get("order_price"))
        if not symbol:
            continue
        if item.get("direction") == "buy" and price > 0:
            try:
                capacities[symbol] = buy_capacity(kis, symbol, price)
            except Exception as exc:  # noqa: BLE001
                errors.append(error("order_available_lookup_failed", f"{symbol}: {redact(exc)}"))
        if item.get("direction") == "sell":
            try:
                sell_capacities[symbol] = sell_capacity(kis, symbol)
            except Exception as exc:  # noqa: BLE001
                errors.append(error("sell_available_lookup_failed", f"{symbol}: {redact(exc)}"))
    return active, capacities, sell_capacities, errors, kis


def reconcile(account: dict[str, Any], execution: dict[str, Any], active: list[dict[str, Any]], capacities: dict[str, dict[str, int]], sell_capacities: dict[str, dict[str, int]], *, submit: bool, kis: Kis | None) -> None:
    portfolio_except = load_portfolio_except_symbols()
    active_qty = active_quantities(active)
    active_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for item in active:
        if item.get("active_status") == "active":
            active_by_symbol.setdefault(symbol_key(item), []).append(item)
    account_by_symbol = {symbol_key(item): item for item in account.get("symbols", []) if isinstance(item, dict)}
    cash_limit = buy_cash_limit(capacities)
    used_cash = 0
    order_adjustments: list[dict[str, Any]] = []
    submitted = 0
    blocked = 0

    for order in execution.get("orders", []):
        if not isinstance(order, dict):
            continue
        symbol = symbol_key(order)
        account_item = account_by_symbol.get(symbol, {})
        active_item = active_qty.get(symbol, {"buy": 0, "sell": 0})
        current = as_int(account_item.get("current_live_holding_quantity"), as_int(order.get("current_live_holding_quantity")))
        expected = current + active_item["buy"] - active_item["sell"]
        if symbol in portfolio_except:
            order.update(
                {
                    "symbol_id": symbol,
                    "direction": "none",
                    "current_live_holding_quantity": current,
                    "pending_and_reserved_buy_quantity": active_item["buy"],
                    "pending_and_reserved_sell_quantity": active_item["sell"],
                    "expected_holding_quantity": expected,
                    "additional_required_quantity": 0,
                    "validated_order_quantity": 0,
                    "result": "blocked",
                    "reason": "symbol_in_portfolio_except_list",
                    "attempts": order.get("attempts") if isinstance(order.get("attempts"), list) else [],
                }
            )
            order["attempts"].append(attempt("portfolio-except-list", "blocked", "symbol is listed in the portfolio-except exclusion list", "symbol_in_portfolio_except_list"))
            blocked += 1
            continue
        final_quantity = non_negative_int_value(order.get("final_holding_quantity"))
        if final_quantity is None:
            order.update(
                {
                    "symbol_id": symbol,
                    "direction": "none",
                    "current_live_holding_quantity": current,
                    "pending_and_reserved_buy_quantity": active_item["buy"],
                    "pending_and_reserved_sell_quantity": active_item["sell"],
                    "expected_holding_quantity": expected,
                    "additional_required_quantity": 0,
                    "validated_order_quantity": 0,
                    "result": "blocked",
                    "reason": "invalid_final_holding_quantity",
                    "attempts": order.get("attempts") if isinstance(order.get("attempts"), list) else [],
                }
            )
            order["attempts"].append(attempt("schema", "blocked", "final_holding_quantity must be a non-negative integer", "invalid_final_holding_quantity"))
            blocked += 1
            continue
        delta = final_quantity - expected
        side = "buy" if delta > 0 else "sell" if delta < 0 else "none"
        order.update(
            {
                "symbol_id": symbol,
                "direction": side,
                "current_live_holding_quantity": current,
                "pending_and_reserved_buy_quantity": active_item["buy"],
                "pending_and_reserved_sell_quantity": active_item["sell"],
                "expected_holding_quantity": expected,
                "additional_required_quantity": delta,
                "validated_order_quantity": abs(delta),
                "attempts": order.get("attempts") if isinstance(order.get("attempts"), list) else [],
            }
        )

        matching_active = active_by_symbol.get(symbol, [])
        qty = as_int(order.get("validated_order_quantity"))
        order_path = str(order.get("order_path") or "reservation")
        order_path, order_api = order_path_api(order_path)
        order["order_path"] = order_path
        order["order_api"] = order_api
        price = normalize_limit_price(order.get("order_price"), side)
        original_price = as_price(order.get("order_price"))
        if price > 0 and original_price != price:
            order["order_price"] = price
            order["order_price_adjustment"] = {
                "from": original_price,
                "to": price,
                "reason": "krx_tick_size",
            }
        desired_delta = final_quantity - current
        desired_side = "buy" if desired_delta > 0 else "sell" if desired_delta < 0 else ""
        desired_qty = abs(desired_delta)
        if matching_active:
            if len(matching_active) > 1:
                order["result"] = "blocked"
                order["reason"] = "multiple_active_orders_require_manual_review"
                order["attempts"].append(attempt("active_order_reconcile", "blocked", f"{len(matching_active)} active orders for {symbol}", "ambiguous_active_orders"))
                for active_order in matching_active:
                    order_adjustments.append(adjustment_row(active_order, action="block", reason="multiple_active_orders_require_manual_review", result="blocked"))
                blocked += 1
                continue

            missing_active_fields = active_order_missing_fields(matching_active[0])
            if missing_active_fields:
                order["result"] = "blocked"
                order["reason"] = "active_order_required_fields_missing"
                order["order_or_reservation_id"] = matching_active[0].get("order_id", "")
                order["attempts"].append(attempt("active_order_reconcile", "blocked", f"missing={','.join(missing_active_fields)}", "active_order_schema"))
                order_adjustments.append(adjustment_row(matching_active[0], action="block", reason="active_order_required_fields_missing", result="blocked"))
                blocked += 1
                continue

            kept = matching_single_order(matching_active, desired_side, desired_qty, price, order_path, order_api) if desired_side else None
            if kept:
                order["result"] = "skipped"
                order["reason"] = "existing_matching_reservation_kept" if order_path == "reservation" else "existing_matching_order_kept"
                order["direction"] = "none"
                order["additional_required_quantity"] = 0
                order["validated_order_quantity"] = 0
                order["order_or_reservation_id"] = kept.get("order_id", "")
                order_adjustments.append(adjustment_row(kept, action="keep", reason="matches_final_delta", result="skipped"))
                continue

            conflict = matching_active[0]
            if not submit:
                order["result"] = "blocked"
                order["reason"] = "active_order_adjustment_required"
                order["order_or_reservation_id"] = conflict.get("order_id", "")
                order["attempts"].append(attempt(conflict.get("order_api") or "order_rvsecncl", "blocked", "mismatched active order kept unmodified", "blocked"))
                order_adjustments.append(adjustment_row(conflict, action="block", reason="active_order_adjustment_required", result="blocked"))
                blocked += 1
                continue

            conflict_remaining = as_int(conflict.get("remaining_quantity"))
            active_matches_desired_sell = desired_side == "sell" and can_correct(conflict, desired_side, order_path, order_api)
            corrected_active_order_id = ""
            active_adjustment_recorded = False

            if active_matches_desired_sell and conflict_remaining >= desired_qty:
                if desired_qty <= 0 or price <= 0:
                    order["result"] = "blocked"
                    order["reason"] = "invalid_order_quantity_or_price"
                    order["order_or_reservation_id"] = conflict.get("order_id", "")
                    order_adjustments.append(adjustment_row(conflict, action="block", reason="invalid_order_quantity_or_price", result="blocked"))
                    blocked += 1
                    continue
                desired_order = dict(order)
                desired_order["direction"] = desired_side
                desired_order["validated_order_quantity"] = desired_qty
                desired_order["additional_required_quantity"] = desired_delta
                try:
                    request_id, action, message = adjust_active_order(kis, conflict, desired_order)
                except Exception as exc:  # noqa: BLE001
                    order["result"] = "blocked"
                    order["reason"] = "active_order_adjustment_failed"
                    order["order_or_reservation_id"] = conflict.get("order_id", "")
                    order["attempts"].append(attempt(conflict.get("order_api") or "order_rvsecncl", "blocked", redact(exc), "api_error"))
                    order_adjustments.append(adjustment_row(conflict, action="block", reason="active_order_adjustment_failed", result="blocked"))
                    blocked += 1
                    continue
                row = adjustment_row(conflict, action=action, reason="active_order_adjustment_required", result="submitted")
                row["adjustment_request_id"] = request_id
                row["attempts"].append(attempt(conflict.get("order_api") or "order_rvsecncl", "submitted", message))
                order_adjustments.append(row)
                order["attempts"].append(attempt(conflict.get("order_api") or "order_rvsecncl", "submitted", message))
                if action == "correct":
                    order["result"] = "submitted"
                    order["reason"] = "active_order_correction_submitted"
                    order["direction"] = desired_side
                    order["additional_required_quantity"] = desired_delta
                    order["validated_order_quantity"] = desired_qty
                    order["order_or_reservation_id"] = request_id
                    submitted += 1
                    continue
                order["result"] = "blocked"
                order["reason"] = "active_order_adjustment_unavailable"
                order["direction"] = desired_side
                order["additional_required_quantity"] = desired_delta
                order["validated_order_quantity"] = desired_qty
                order["order_or_reservation_id"] = request_id or conflict.get("order_id", "")
                row["replacement_required"] = True
                blocked += 1
                continue

            if active_matches_desired_sell and 0 < conflict_remaining < desired_qty and as_int(conflict.get("order_price")) != price:
                if price <= 0:
                    order["result"] = "blocked"
                    order["reason"] = "invalid_order_quantity_or_price"
                    order["order_or_reservation_id"] = conflict.get("order_id", "")
                    order_adjustments.append(adjustment_row(conflict, action="block", reason="invalid_order_quantity_or_price", result="blocked"))
                    blocked += 1
                    continue
                covered_order = dict(order)
                covered_order["direction"] = desired_side
                covered_order["validated_order_quantity"] = conflict_remaining
                covered_order["additional_required_quantity"] = conflict_remaining if desired_side == "buy" else -conflict_remaining
                try:
                    request_id, action, message = adjust_active_order(kis, conflict, covered_order)
                except Exception as exc:  # noqa: BLE001
                    order["result"] = "blocked"
                    order["reason"] = "active_order_adjustment_failed"
                    order["order_or_reservation_id"] = conflict.get("order_id", "")
                    order["attempts"].append(attempt(conflict.get("order_api") or "order_rvsecncl", "blocked", redact(exc), "api_error"))
                    order_adjustments.append(adjustment_row(conflict, action="block", reason="active_order_adjustment_failed", result="blocked"))
                    blocked += 1
                    continue
                if action != "correct":
                    order["result"] = "blocked"
                    order["reason"] = "active_order_adjustment_unavailable"
                    order["order_or_reservation_id"] = request_id or conflict.get("order_id", "")
                    order_adjustments.append(adjustment_row(conflict, action=action, reason="active_order_adjustment_required", result="blocked"))
                    blocked += 1
                    continue
                row = adjustment_row(conflict, action=action, reason="active_order_adjustment_required", result="submitted")
                row["adjustment_request_id"] = request_id
                row["attempts"].append(attempt(conflict.get("order_api") or "order_rvsecncl", "submitted", message))
                order_adjustments.append(row)
                order["attempts"].append(attempt(conflict.get("order_api") or "order_rvsecncl", "submitted", message))
                corrected_active_order_id = request_id
                active_adjustment_recorded = True

            if (
                side != "none"
                and conflict.get("direction") == side
                and conflict.get("order_path") == order_path
                and conflict.get("order_api") == order_api
            ):
                band_reason = score_band_block_reason(order, side)
                if band_reason:
                    order["result"] = "blocked"
                    order["reason"] = band_reason
                    order["attempts"].append(attempt("score-band-gate", "blocked", f"final_first_score={order.get('final_first_score')} does not permit new {side} submission", band_reason))
                    if not active_adjustment_recorded:
                        order_adjustments.append(adjustment_row(conflict, action="keep", reason="same_direction_active_order_kept", result="skipped"))
                    blocked += 1
                    continue
                if qty <= 0 or price <= 0:
                    order["result"] = "blocked"
                    order["reason"] = "invalid_order_quantity_or_price"
                    order_adjustments.append(adjustment_row(conflict, action="keep", reason="same_direction_active_order_kept", result="skipped"))
                    blocked += 1
                    continue
                qty, required_cash, quantity_blocked = apply_quantity_gates(
                    order,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    price=price,
                    current=current,
                    active_sell_quantity=active_item["sell"],
                    capacities=capacities,
                    sell_capacities=sell_capacities,
                    used_cash=used_cash,
                    cash_limit=cash_limit,
                    local_sell_gate=True,
                )
                if not active_adjustment_recorded:
                    order_adjustments.append(adjustment_row(conflict, action="keep", reason="same_direction_active_order_kept", result="skipped"))
                if quantity_blocked:
                    order["order_or_reservation_id"] = conflict.get("order_id", "")
                    blocked += 1
                    continue
                order["validated_order_quantity"] = qty
                order["additional_required_quantity"] = qty if side == "buy" else -qty
                if side == "buy":
                    used_cash += required_cash
                try:
                    order_id = submit_order(kis, order) if kis is not None else ""
                except Exception as exc:  # noqa: BLE001
                    order["result"] = "blocked"
                    order["reason"] = "additional_order_submission_failed"
                    order["order_or_reservation_id"] = conflict.get("order_id", "")
                    order["attempts"].append(attempt(order_api, "blocked", redact(exc), "api_error"))
                    if side == "buy":
                        used_cash -= required_cash
                    blocked += 1
                    continue
                if not order_id:
                    order["result"] = "blocked"
                    order["reason"] = "additional_order_submission_uncertain"
                    order["order_or_reservation_id"] = conflict.get("order_id", "")
                    order["attempts"].append(attempt(order_api, "blocked", "additional order accepted without order id", "uncertain_order_id"))
                    if side == "buy":
                        used_cash -= required_cash
                    blocked += 1
                    continue
                order["result"] = "submitted"
                order["reason"] = "active_order_kept_and_additional_order_submitted"
                order["order_or_reservation_id"] = order_id
                if corrected_active_order_id:
                    order["corrected_active_order_id"] = corrected_active_order_id
                else:
                    order["kept_active_order_id"] = conflict.get("order_id", "")
                order["attempts"].append(attempt(order_api, "submitted", f"additional_order_id={order_id}"))
                submitted += 1
                continue

            desired_order = None
            if desired_side:
                if desired_qty <= 0 or price <= 0:
                    order["result"] = "blocked"
                    order["reason"] = "invalid_order_quantity_or_price"
                    order["order_or_reservation_id"] = conflict.get("order_id", "")
                    order_adjustments.append(adjustment_row(conflict, action="block", reason="invalid_order_quantity_or_price", result="blocked"))
                    blocked += 1
                    continue
                desired_order = dict(order)
                desired_order["direction"] = desired_side
                desired_order["validated_order_quantity"] = desired_qty
                desired_order["additional_required_quantity"] = desired_delta
                desired_qty, required_cash, quantity_blocked = apply_quantity_gates(
                    desired_order,
                    symbol=symbol,
                    side=desired_side,
                    qty=desired_qty,
                    price=price,
                    current=current,
                    active_sell_quantity=active_item["sell"],
                    capacities=capacities,
                    sell_capacities=sell_capacities,
                    used_cash=used_cash,
                    cash_limit=cash_limit,
                    local_sell_gate=False,
                )
                if quantity_blocked:
                    order.update(
                        {
                            key: value
                            for key, value in desired_order.items()
                            if key
                            in {
                                "result",
                                "reason",
                                "direction",
                                "requested_order_quantity",
                                "requested_additional_required_quantity",
                                "quantity_adjustment",
                                "validated_order_quantity",
                                "additional_required_quantity",
                                "attempts",
                            }
                        }
                    )
                    order["order_or_reservation_id"] = conflict.get("order_id", "")
                    order_adjustments.append(adjustment_row(conflict, action="block", reason=order.get("reason") or "quantity_gate_blocked", result="blocked"))
                    blocked += 1
                    continue
                desired_delta = desired_qty if desired_side == "buy" else -desired_qty
                desired_order["validated_order_quantity"] = desired_qty
                desired_order["additional_required_quantity"] = desired_delta
                reduced_kept = matching_single_order([conflict], desired_side, desired_qty, price, order_path, order_api)
                if reduced_kept:
                    order["result"] = "skipped"
                    order["reason"] = "existing_matching_reservation_kept" if order_path == "reservation" else "existing_matching_order_kept"
                    order["direction"] = desired_side
                    order["additional_required_quantity"] = desired_delta
                    order["validated_order_quantity"] = desired_qty
                    if "requested_order_quantity" in desired_order:
                        order["requested_order_quantity"] = desired_order.get("requested_order_quantity")
                        order["requested_additional_required_quantity"] = desired_order.get("requested_additional_required_quantity")
                        order["quantity_adjustment"] = desired_order.get("quantity_adjustment")
                    order["order_or_reservation_id"] = reduced_kept.get("order_id", "")
                    order_adjustments.append(adjustment_row(reduced_kept, action="keep", reason="matches_reduced_final_delta", result="skipped"))
                    continue
            try:
                request_id, action, message = adjust_active_order(
                    kis,
                    conflict,
                    desired_order if desired_order and can_correct(conflict, desired_side, order_path, order_api) else None,
                )
            except Exception as exc:  # noqa: BLE001
                order["result"] = "blocked"
                order["reason"] = "active_order_adjustment_failed"
                order["order_or_reservation_id"] = conflict.get("order_id", "")
                order["attempts"].append(attempt(conflict.get("order_api") or "order_rvsecncl", "blocked", redact(exc), "api_error"))
                order_adjustments.append(adjustment_row(conflict, action="block", reason="active_order_adjustment_failed", result="blocked"))
                blocked += 1
                continue

            row = adjustment_row(conflict, action=action, reason="active_order_adjustment_required", result="submitted")
            row["adjustment_request_id"] = request_id
            row["attempts"].append(attempt(conflict.get("order_api") or "order_rvsecncl", "submitted", message))
            order_adjustments.append(row)
            order["attempts"].append(attempt(conflict.get("order_api") or "order_rvsecncl", "submitted", message))
            if desired_order and action == "correct":
                order["result"] = "submitted"
                order["reason"] = "active_order_correction_submitted"
                order["direction"] = desired_side
                order["additional_required_quantity"] = desired_delta
                order["validated_order_quantity"] = desired_qty
                if "requested_order_quantity" in desired_order:
                    order["requested_order_quantity"] = desired_order.get("requested_order_quantity")
                    order["requested_additional_required_quantity"] = desired_order.get("requested_additional_required_quantity")
                    order["quantity_adjustment"] = desired_order.get("quantity_adjustment")
                if desired_side == "buy":
                    used_cash += required_cash
                order["order_or_reservation_id"] = request_id
                submitted += 1
                continue
            if not desired_order:
                order["result"] = "submitted"
                order["reason"] = "active_order_cancel_submitted"
                order["direction"] = "none"
                order["additional_required_quantity"] = 0
                order["validated_order_quantity"] = 0
                order["order_or_reservation_id"] = request_id
                submitted += 1
                continue
            if desired_order and action == "cancel":
                row["replacement_required"] = True
                order["direction"] = desired_side
                order["additional_required_quantity"] = desired_delta
                order["validated_order_quantity"] = desired_qty
                if "requested_order_quantity" in desired_order:
                    order["requested_order_quantity"] = desired_order.get("requested_order_quantity")
                    order["requested_additional_required_quantity"] = desired_order.get("requested_additional_required_quantity")
                    order["quantity_adjustment"] = desired_order.get("quantity_adjustment")
                try:
                    replacement_id = submit_order(kis, desired_order) if kis is not None else ""
                except Exception as exc:  # noqa: BLE001
                    order["result"] = "blocked"
                    order["reason"] = "replacement_order_submission_failed"
                    order["order_or_reservation_id"] = request_id
                    order["attempts"].append(attempt(order_api, "blocked", redact(exc), "api_error"))
                    blocked += 1
                    continue
                if not replacement_id:
                    order["result"] = "blocked"
                    order["reason"] = "replacement_order_submission_uncertain"
                    order["order_or_reservation_id"] = request_id
                    order["attempts"].append(attempt(order_api, "blocked", "replacement order accepted without order id", "uncertain_order_id"))
                    blocked += 1
                    continue
                row["replacement_order_id"] = replacement_id
                order["result"] = "submitted"
                order["reason"] = "active_order_cancel_and_replacement_submitted"
                order["order_or_reservation_id"] = replacement_id
                order["cancel_request_id"] = request_id
                order["attempts"].append(attempt(order_api, "submitted", f"replacement_order_id={replacement_id}" if replacement_id else "replacement order accepted"))
                if desired_side == "buy":
                    used_cash += required_cash
                submitted += 1
                continue
            order["result"] = "blocked"
            order["reason"] = "active_order_adjustment_unavailable"
            order["direction"] = desired_side
            order["additional_required_quantity"] = desired_delta
            order["validated_order_quantity"] = desired_qty
            order["order_or_reservation_id"] = request_id
            row["replacement_required"] = True
            blocked += 1
            continue

        if side == "none":
            order["result"] = "skipped"
            order["reason"] = "final_equals_expected_holding_quantity"
            continue

        band_reason = score_band_block_reason(order, side)
        if band_reason:
            order["result"] = "blocked"
            order["reason"] = band_reason
            order["attempts"].append(attempt("score-band-gate", "blocked", f"final_first_score={order.get('final_first_score')} does not permit new {side} submission", band_reason))
            blocked += 1
            continue

        if qty <= 0 or price <= 0:
            order["result"] = "blocked"
            order["reason"] = "invalid_order_quantity_or_price"
            blocked += 1
            continue
        qty, required_cash, quantity_blocked = apply_quantity_gates(
            order,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            current=current,
            active_sell_quantity=active_item["sell"],
            capacities=capacities,
            sell_capacities=sell_capacities,
            used_cash=used_cash,
            cash_limit=cash_limit,
            local_sell_gate=True,
        )
        if quantity_blocked:
            blocked += 1
            continue
        if side == "buy":
            used_cash += required_cash
        if not submit:
            order["result"] = "skipped"
            order["reason"] = "validated_dry_run_not_submitted"
            continue
        try:
            reservation_id = submit_order(kis, order) if kis is not None else ""
        except Exception as exc:  # noqa: BLE001
            order["result"] = "blocked"
            order["reason"] = "order_submission_failed"
            order["attempts"].append(attempt(order_api, "blocked", redact(exc), "api_error"))
            blocked += 1
            continue
        order["result"] = "submitted"
        order["reason"] = "cash_order_submitted" if order_path == "immediate" else "reservation_order_submitted"
        order["order_or_reservation_id"] = reservation_id
        order["attempts"].append(attempt(order_api, "submitted", f"order_id={reservation_id}" if reservation_id else "order accepted"))
        submitted += 1

    execution["latest_available_cash"] = cash_limit
    execution["order_adjustments"] = order_adjustments
    execution["requires_main_agent_order_execution"] = False
    execution["required_main_agent_actions"] = []
    execution["errors"] = [item for item in execution.get("errors", []) if isinstance(item, dict) and item.get("code") != "order_submission_blocked"]
    execution["status"] = "partial" if blocked else "success"
    if submitted == 0 and blocked and not any(item.get("result") == "skipped" for item in execution.get("orders", [])):
        execution["status"] = "failed"
    execution["generated_at"] = now_iso()


def execute(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    execution_path = Path(args.execution_json or output_dir / "execution.json")
    account_path = Path(args.account_before_order or output_dir / "account-before-order.json")
    execution = load_json(execution_path)
    account = load_json(account_path)
    request_type = str(execution.get("request_type") or "")
    if args.submit and args.offline:
        execution["status"] = "failed"
        execution["errors"] = [item for item in execution.get("errors", []) if isinstance(item, dict)] + [
            error("submit_requires_live_kis_client", "offline mode cannot submit or mark submitted orders")
        ]
        execution["requires_main_agent_order_execution"] = False
        execution["required_main_agent_actions"] = []
        execution["order_execution_mode"] = "submit-blocked"
        write_json(execution_path, execution)
        return execution
    if args.submit and request_type not in {"demo-submit", "real-submit"}:
        execution["status"] = "failed"
        execution["errors"] = [item for item in execution.get("errors", []) if isinstance(item, dict)] + [
            error("submit_requires_explicit_execution_request", f"request_type={request_type or '<missing>'}")
        ]
        execution["requires_main_agent_order_execution"] = False
        execution["required_main_agent_actions"] = []
        execution["order_execution_mode"] = "submit-blocked"
        write_json(execution_path, execution)
        return execution
    normalize_execution_order_prices(execution)
    active, capacities, sell_capacities, gate_errors, kis = refresh_gates(args, account, execution)
    account["active_order_lookup_performed"] = True
    account["order_available_lookup_performed"] = not bool(gate_errors)
    account["active_orders"] = active
    account.setdefault("active_order_checks", {})["order_resv_ccnl"] = f"{len([item for item in active if item.get('active_status') == 'active'])} active"
    account["warnings"] = [item for item in account.get("warnings", []) if item not in {"active_order_lookup_not_performed", "order_available_lookup_not_performed"}]
    if gate_errors:
        execution["status"] = "failed"
        execution["errors"] = [item for item in execution.get("errors", []) if isinstance(item, dict)] + gate_errors
        execution["requires_main_agent_order_execution"] = False
        execution["required_main_agent_actions"] = []
    else:
        reconcile(account, execution, active, capacities, sell_capacities, submit=args.submit, kis=kis)
        if args.submit and kis is not None:
            reconcile_submitted_cash_orders(kis, execution)
    execution["order_execution_mode"] = "submit" if args.submit else "dry-run"
    execution["execution_environment"] = env_dv(args.env or account.get("execution_environment"))
    execution["order_available_checks"] = capacities
    execution["sell_available_checks"] = sell_capacities
    write_json(account_path, account)
    write_json(execution_path, execution)
    write_json(
        output_dir / "order-execution-log.json",
        {
            "schema_version": "1",
            "run_id": execution.get("run_id") or output_dir.name,
            "generated_at": now_iso(),
            "submit": bool(args.submit),
            "execution_status": execution.get("status"),
            "active_order_count": len([item for item in active if item.get("active_status") == "active"]),
            "buy_capacity_symbols": sorted(capacities),
            "sell_capacity_symbols": sorted(sell_capacities),
            "broker_reconciliation": execution.get("broker_reconciliation", {}),
        },
    )
    return execution


def self_test() -> int:
    """Run the extracted test suite through the legacy CLI contract."""
    codex_exec_root = Path(__file__).resolve().parents[4]
    codex_exec_root_text = str(codex_exec_root)
    if codex_exec_root_text not in sys.path:
        sys.path.insert(0, codex_exec_root_text)

    from service.pipelines.daily_trading.tests.test_execute_orders import (
        self_test as run_external_self_test,
    )

    return run_external_self_test()


def probe_api(args: argparse.Namespace) -> int:
    kis = Kis(env_dv(args.env), args.retries)
    start_date, end_date = args.reservation_start_date, args.reservation_end_date
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    result: dict[str, Any] = {
        "status": "success",
        "env": kis.env,
        "read_only": True,
        "reservations": {"status": "not_run"},
        "pending_orders": {"status": "not_run"},
        "buy_capacity": {"status": "not_run"},
        "sell_capacity": {"status": "not_run"},
    }
    try:
        reservations = fetch_reservations(kis, start_date, end_date)
        result["reservations"] = {"status": "success", "count": len(reservations)}
    except Exception as exc:  # noqa: BLE001
        result["reservations"] = {"status": "failed", "error": redact(exc)}
        result["status"] = "partial"
    try:
        pending = fetch_pending_orders(kis)
        result["pending_orders"] = {"status": "success", "count": len(pending)}
    except Exception as exc:  # noqa: BLE001
        result["pending_orders"] = {"status": "failed", "error": redact(exc)}
        result["status"] = "partial"
    symbol = symbol_key(args.symbol)
    if symbol:
        try:
            result["buy_capacity"] = {"status": "success", **buy_capacity(kis, symbol, as_price(args.price))}
        except Exception as exc:  # noqa: BLE001
            result["buy_capacity"] = {"status": "failed", "error": redact(exc)}
            result["status"] = "partial"
        try:
            result["sell_capacity"] = {"status": "success", **sell_capacity(kis, symbol)}
        except Exception as exc:  # noqa: BLE001
            result["sell_capacity"] = {"status": "failed", "error": redact(exc)}
            result["status"] = "partial"
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "success" else 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Execute daily-trading orders.")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--output-dir", required=True)
    run.add_argument("--execution-json", default="")
    run.add_argument("--account-before-order", default="")
    run.add_argument("--env", default=os.environ.get("CODEX_MCP_TRADING_ENV", "acct"))
    run.add_argument("--submit", action="store_true")
    run.add_argument("--offline", action="store_true")
    run.add_argument("--retries", type=int, default=2)
    run.add_argument("--reservation-start-date", default="")
    run.add_argument("--reservation-end-date", default="")
    probe = sub.add_parser("probe-api", help="Run read-only KIS order/account API probes.")
    probe.add_argument("--env", default=os.environ.get("CODEX_MCP_TRADING_ENV", "acct"))
    probe.add_argument("--symbol", default="")
    probe.add_argument("--price", default="1")
    probe.add_argument("--retries", type=int, default=0)
    probe.add_argument("--reservation-start-date", default="")
    probe.add_argument("--reservation-end-date", default="")
    sub.add_parser("self-test")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "self-test":
        return self_test()
    if args.command == "probe-api":
        return probe_api(args)
    payload = execute(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
