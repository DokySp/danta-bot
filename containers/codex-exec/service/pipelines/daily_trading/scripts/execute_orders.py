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
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"

ENDPOINTS = {
    "inquire_price": ("/uapi/domestic-stock/v1/quotations/inquire-price", "GET", "FHKST01010100", "FHKST01010100"),
    "inquire_balance": ("/uapi/domestic-stock/v1/trading/inquire-balance", "GET", "TTTC8434R", "VTTC8434R"),
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


# Decision-guard gate: every genuinely new buy/sell submission (including
# same-direction incremental exposure after active-order handling) requires a
# matching decision_guard.status=="allowed" from the pipeline's policy guard.
# Lifecycle-only cancellation/correction/reconciliation paths never call this.
def decision_guard_block_reason(order: dict[str, Any], side: str) -> str:
    """Fail closed unless the guard is allowed AND its canonical_action/basis actually
    match this submission's side/decision_basis -- a forged, missing, or stale guard
    (e.g. copied from a different decision) must never authorize an order."""
    if side not in {"buy", "sell"}:
        return ""
    guard = order.get("decision_guard") if isinstance(order.get("decision_guard"), dict) else {}
    status = str(guard.get("status") or "")
    if status != "allowed":
        return "decision_guard_not_allowed"
    canonical_action = str(guard.get("canonical_action") or "")
    if side == "buy" and canonical_action != "increase":
        return "decision_guard_action_mismatch"
    if side == "sell" and canonical_action not in {"reduce", "exit"}:
        return "decision_guard_action_mismatch"
    decision_basis = str(order.get("decision_basis") or "")
    guard_basis = str(guard.get("basis") or "")
    if not decision_basis or decision_basis != guard_basis:
        return "decision_guard_basis_mismatch"
    return ""


def fresh_balance_params(
    cano: str,
    product_code: str,
    ctx_fk100: str = "",
    ctx_nk100: str = "",
) -> dict[str, str]:
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


def fetch_fresh_domestic_balance(
    kis: "Kis | None",
    *,
    max_pages: int = 20,
) -> dict[str, Any] | None:
    """One read-only KIS domestic inquire-balance snapshot per executor run.

    Kept entirely separate from account-before-order.json/lifecycle state (this
    is a fresh pre-submit recheck source, not a replacement for the pipeline's
    account artifact). Returns None on any fetch/parse failure, missing total
    evaluation amount, or non-dict per-symbol rows -- callers must fail closed
    for profit_protection/concentration_rebalance new submissions rather than
    fall back to a stale snapshot.
    """
    if kis is None:
        return None
    call_with_headers = getattr(kis, "call_with_headers", None)
    ctx_fk100 = ""
    ctx_nk100 = ""
    tr_cont = ""
    rows: list[dict[str, Any]] = []
    summary_row: dict[str, Any] = {}
    for _page in range(max_pages):
        try:
            params = fresh_balance_params(kis.cano, kis.product, ctx_fk100, ctx_nk100)
            if callable(call_with_headers):
                body, response_headers = call_with_headers(
                    "inquire_balance",
                    params=params,
                    tr_cont=tr_cont,
                )
            else:
                body = kis.call("inquire_balance", params=params)
                response_headers = {}
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(body, dict):
            return None
        output1 = body.get("output1")
        if not isinstance(output1, list) or not all(isinstance(row, dict) for row in output1):
            return None
        rows.extend(output1)
        output2 = body.get("output2")
        page_summary = (
            output2[0]
            if isinstance(output2, list) and output2 and isinstance(output2[0], dict)
            else output2
            if isinstance(output2, dict)
            else {}
        )
        if not summary_row and page_summary:
            summary_row = page_summary
        next_tr_cont = str(response_headers.get("tr_cont") or "").strip()
        if next_tr_cont not in {"F", "M"}:
            break
        ctx_fk100 = str(
            body.get("ctx_area_fk100")
            or body.get("CTX_AREA_FK100")
            or page_summary.get("ctx_area_fk100")
            or page_summary.get("CTX_AREA_FK100")
            or ""
        ).strip()
        ctx_nk100 = str(
            body.get("ctx_area_nk100")
            or body.get("CTX_AREA_NK100")
            or page_summary.get("ctx_area_nk100")
            or page_summary.get("CTX_AREA_NK100")
            or ""
        ).strip()
        if not (ctx_fk100 or ctx_nk100):
            return None
        tr_cont = "N"
    else:
        return None

    symbols: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = symbol_key(row)
        if not symbol:
            continue
        symbols[symbol] = {
            "quantity": as_int(first(row, ("hldg_qty", "HLDG_QTY"))),
            "average_purchase_price": as_number(first(row, ("pchs_avg_pric", "PCHS_AVG_PRIC"))),
            "valuation_amount": as_number(first(row, ("evlu_amt", "EVLU_AMT"))),
        }
    total_evaluation_amount = as_number(first(summary_row, ("tot_evlu_amt", "TOT_EVLU_AMT")))
    if total_evaluation_amount is None or total_evaluation_amount <= 0:
        return None
    return {"symbols": symbols, "total_evaluation_amount": total_evaluation_amount}


def verify_concentration_rebalance(fresh_balance: dict[str, Any] | None, symbol: str, order: dict[str, Any]) -> bool:
    """Fail-closed recheck against a fresh pre-submit balance snapshot.

    Verifies the symbol is still above the approved cap (guard.cap_pct, stashed
    on decision_guard by the pipeline) and that this order's validated
    reduction, together with any already-pending sell, does not take the
    post-trade valuation below the cap floor. The valuation uses the fresh
    balance row's implied unit value, never a stale execution-plan order price.
    Unavailable/incomplete fresh data, or a
    concentration that has already fallen back within the cap, blocks rather
    than trusting the earlier guard.
    """
    if not isinstance(fresh_balance, dict):
        return False
    total_evaluation_amount = as_number(fresh_balance.get("total_evaluation_amount"))
    symbols = fresh_balance.get("symbols") if isinstance(fresh_balance.get("symbols"), dict) else {}
    entry = symbols.get(symbol)
    if total_evaluation_amount is None or total_evaluation_amount <= 0 or not isinstance(entry, dict):
        return False
    valuation_amount = as_number(entry.get("valuation_amount"))
    fresh_qty = as_int(entry.get("quantity"))
    if valuation_amount is None:
        return False
    guard = order.get("decision_guard") if isinstance(order.get("decision_guard"), dict) else {}
    cap_pct = as_number(guard.get("cap_pct"))
    if cap_pct is None or fresh_qty <= 0:
        return False
    current_pct = (valuation_amount / total_evaluation_amount) * 100
    if current_pct <= cap_pct:
        return False
    validated_qty = as_int(order.get("validated_order_quantity"))
    pending_sell_qty = as_int(order.get("pending_and_reserved_sell_quantity"))
    post_trade_qty = fresh_qty - pending_sell_qty - validated_qty
    if validated_qty <= 0 or post_trade_qty <= 0:
        return False
    # Revalue the post-trade quantity with the same fresh snapshot's implied
    # per-share value. Using execution.order_price here could be stale and let a
    # high old price understate the cap-floor quantity.
    fresh_unit_value = valuation_amount / fresh_qty
    cap_value = cap_pct / 100 * total_evaluation_amount
    return post_trade_qty * fresh_unit_value >= cap_value


def verify_profit_protection_pnl(kis: "Kis | None", fresh_balance: dict[str, Any] | None, symbol: str) -> bool:
    """Immediate positive-PnL recheck for a profit_protection sell, right before submission.

    Uses the freshest available account cost (average_purchase_price from this
    run's fresh pre-submit balance snapshot, not account-before-order.json)
    plus a fresh KIS current-price lookup. Inability to verify (no kis client,
    missing fresh balance, missing cost, or a failed price lookup) blocks
    rather than assuming the earlier positive PnL still holds.
    """
    if kis is None or not isinstance(fresh_balance, dict):
        return False
    symbols = fresh_balance.get("symbols") if isinstance(fresh_balance.get("symbols"), dict) else {}
    entry = symbols.get(symbol)
    if not isinstance(entry, dict):
        return False
    average_price = as_number(entry.get("average_purchase_price"))
    if average_price is None or average_price <= 0:
        return False
    try:
        current = kis.current_price(symbol)
    except Exception:  # noqa: BLE001
        return False
    if current is None or current <= 0:
        return False
    return current > average_price


def fresh_balance_quantity(fresh_balance: dict[str, Any] | None, symbol: str) -> int | None:
    if not isinstance(fresh_balance, dict):
        return None
    symbols = fresh_balance.get("symbols") if isinstance(fresh_balance.get("symbols"), dict) else {}
    entry = symbols.get(symbol)
    if not isinstance(entry, dict):
        return None
    return non_negative_int_value(entry.get("quantity"))


def verify_fresh_reduction_bounds(
    fresh_balance: dict[str, Any] | None,
    symbol: str,
    order: dict[str, Any],
) -> bool:
    """Revalidate the approved partial-reduction bound against fresh holdings.

    Existing pending sells and the new sell are one combined reduction from the
    fresh holding. Missing bounds/data, a full exit, or a combined reduction
    above the guard's max_reduction_pct all fail closed.
    """
    fresh_qty = fresh_balance_quantity(fresh_balance, symbol)
    guard = order.get("decision_guard") if isinstance(order.get("decision_guard"), dict) else {}
    max_reduction_pct = as_number(guard.get("max_reduction_pct"))
    pending_sell_qty = as_int(order.get("pending_and_reserved_sell_quantity"))
    new_sell_qty = as_int(order.get("validated_order_quantity"))
    if (
        fresh_qty is None
        or fresh_qty <= 0
        or max_reduction_pct is None
        or not (0 < max_reduction_pct <= 100)
        or pending_sell_qty < 0
        or new_sell_qty <= 0
    ):
        return False
    combined_reduction = pending_sell_qty + new_sell_qty
    max_pct_reduction = int(
        (
            Decimal(fresh_qty)
            * Decimal(str(max_reduction_pct))
            / Decimal(100)
        ).to_integral_value(rounding=ROUND_DOWN)
    )
    max_allowed_reduction = min(max_pct_reduction, fresh_qty - 1)
    return 0 < combined_reduction <= max_allowed_reduction


def as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


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

    def call_with_headers(
        self,
        name: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        tr_cont: str = "",
    ) -> tuple[dict[str, Any], dict[str, str]]:
        path, method, tr_real, tr_demo = ENDPOINTS[name]
        tr_id = tr_demo if self.env == "demo" else tr_real
        # Do not replay order-changing POSTs after ambiguous transport failures.
        request_retries = 0 if method == "POST" else self.retries
        headers = self.headers(tr_id, payload)
        if tr_cont:
            headers["tr_cont"] = tr_cont
        body, response_headers = retry_json(method, path, headers, params=params, payload=payload, retries=request_retries)
        if str(body.get("rt_cd", "0")) not in {"0", ""}:
            raise RuntimeError(str(body.get("msg1") or body.get("msg_cd") or body.get("rt_cd") or "KIS API failed"))
        return body, response_headers

    def call(self, name: str, *, params: dict[str, str] | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body, _ = self.call_with_headers(name, params=params, payload=payload)
        return body

    def current_price(self, symbol: str) -> int | None:
        body = self.call("inquire_price", params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
        output = body.get("output") if isinstance(body.get("output"), dict) else {}
        price = as_int(output.get("stck_prpr"), 0)
        return price if price > 0 else None


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
    reservation_id = str(
        first(row, ("rsvn_ord_seq", "rsvn_ord_no", "RSVN_ORD_SEQ", "RSVN_ORD_NO")) or ""
    ).strip()
    resulting_order_id = str(first(row, ("odno", "ord_no", "ODNO", "ORD_NO")) or "").strip()
    # Reservation identity (used for correction/cancellation) still wins when both are present.
    order_id = reservation_id or resulting_order_id
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
        "rsvn_ord_seq": reservation_id,
        "rsvn_ord_orgno": (orgno or default_reservation_orgno()) if order_id else "",
        "rsvn_ord_ord_dt": str(first(row, ("rsvn_ord_ord_dt", "ord_dt", "RSVN_ORD_ORD_DT", "ORD_DT")) or "").strip(),
        "odno": resulting_order_id,
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


def fetch_reservations(kis: Kis, start_date: str, end_date: str, *, max_pages: int = 20) -> list[dict[str, Any]]:
    call_with_headers = getattr(kis, "call_with_headers", None)
    ctx_fk200 = ""
    ctx_nk200 = ""
    tr_cont = ""
    raw_rows: list[dict[str, Any]] = []
    for _page in range(max_pages):
        params = {
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
            "CTX_AREA_FK200": ctx_fk200,
            "CTX_AREA_NK200": ctx_nk200,
        }
        if call_with_headers is not None:
            body, response_headers = call_with_headers("order_resv_ccnl", params=params, tr_cont=tr_cont)
        else:
            body = kis.call("order_resv_ccnl", params=params)
            response_headers = {}
        raw_rows.extend(rows(body))
        ctx_fk200 = str(body.get("ctx_area_fk200") or body.get("CTX_AREA_FK200") or "").strip()
        ctx_nk200 = str(body.get("ctx_area_nk200") or body.get("CTX_AREA_NK200") or "").strip()
        next_tr_cont = str(response_headers.get("tr_cont") or "").strip()
        has_more = next_tr_cont in {"F", "M"} and bool(ctx_fk200 or ctx_nk200)
        if not has_more:
            break
        tr_cont = "N"
    else:
        raise RuntimeError(f"KIS order_resv_ccnl pagination exceeded max_pages={max_pages} with more data remaining")
    normalized = [normalize_reservation(row) for row in raw_rows]
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
    canceled = as_int(
        first(
            row,
            (
                "cncl_cfrm_qty",
                "CNCL_CFRM_QTY",
                # Retain the old alias for previously captured fixtures.
                "cnc_cfrm_qty",
                "CNC_CFRM_QTY",
            ),
        )
    )
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


def previous_submitted_cash_orders(output_dir: Path, day: str) -> list[dict[str, Any]]:
    """Return unique same-day cash submissions from earlier pipeline runs."""
    runs_dir = output_dir.parent
    if not runs_dir.is_dir():
        return []
    result: list[dict[str, Any]] = []
    seen_order_ids: set[str] = set()
    candidates: list[tuple[str, Path, dict[str, Any]]] = []
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        execution_path = run_dir / "execution.json"
        if not execution_path.is_file():
            continue
        try:
            execution = load_json(execution_path)
        except (OSError, ValueError):
            continue
        if not isinstance(execution, dict) or execution_order_day(execution) != day:
            continue
        candidates.append((str(execution.get("started_at") or run_dir.name), run_dir, execution))
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)

    for _, run_dir, execution in candidates:
        for row_index, order in enumerate(execution.get("orders", [])):
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("order_or_reservation_id") or "").strip()
            if (
                not order_id
                or order_id in seen_order_ids
                or order.get("order_path") != "immediate"
                or str(order.get("result") or "").lower() != "submitted"
                or str(order.get("direction") or "").lower() not in {"buy", "sell"}
            ):
                continue
            seen_order_ids.add(order_id)
            result.append(
                {
                    "run_id": execution.get("run_id") or run_dir.name,
                    "started_at": execution.get("started_at") or "",
                    "symbol_id": symbol_key(order),
                    "symbol_name": order.get("symbol_name") or symbol_key(order),
                    "direction": str(order.get("direction") or "").lower(),
                    "order_id": order_id,
                    "requested_quantity": as_int(order.get("validated_order_quantity")),
                    "current_live_holding_quantity": as_int(order.get("current_live_holding_quantity")),
                    "row_id": str(row_index),
                    "order_path": "immediate",
                    "order_api": "order_cash",
                }
            )
    return result


def reconcile_previous_cash_orders(
    previous_orders: list[dict[str, Any]],
    broker_rows: list[dict[str, Any]],
    active_orders: list[dict[str, Any]],
    *,
    observed_at: str,
) -> list[dict[str, Any]]:
    rows_by_id = {broker_order_id(row): row for row in broker_rows if broker_order_id(row)}
    active_by_id = {
        str(item.get("order_id") or "").strip(): item
        for item in active_orders
        if isinstance(item, dict) and str(item.get("order_id") or "").strip()
    }
    reconciled: list[dict[str, Any]] = []
    for order in previous_orders:
        order_id = str(order.get("order_id") or "").strip()
        status = normalize_broker_reconciliation(
            rows_by_id.get(order_id),
            requested_quantity=as_int(order.get("requested_quantity")),
            observed_at=observed_at,
        )
        active = active_by_id.get(order_id)
        if active and active.get("active_status") == "active" and status.get("status") in {"unconfirmed", "accepted"}:
            remaining = as_int(active.get("remaining_quantity"))
            status.update(
                {
                    "status": "pending",
                    "terminal": False,
                    "remaining_quantity": remaining,
                    "ordered_quantity": max(as_int(status.get("ordered_quantity")), remaining),
                    "source_api": "inquire_psbl_rvsecncl",
                }
            )
        reconciled.append({**order, "broker_reconciliation": status})
    return reconciled


def apply_order_lifecycle_to_account(
    account: dict[str, Any],
    active_orders: list[dict[str, Any]],
    reconciled_orders: list[dict[str, Any]],
    *,
    lookup_complete: bool,
    observed_fills: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    active_qty = active_quantities(active_orders)
    symbols = [item for item in account.get("symbols", []) if isinstance(item, dict)]
    by_symbol = {symbol_key(item): item for item in symbols if symbol_key(item)}
    for active in active_orders:
        symbol_id = symbol_key(active)
        if not symbol_id or symbol_id in by_symbol:
            continue
        synthetic = {
            "symbol_id": symbol_id,
            "symbol_name": active.get("symbol_name") or symbol_id,
            "current_live_holding_quantity": 0,
            "current_price": as_int(active.get("order_price")),
            "today_buy_quantity": 0,
            "today_sell_quantity": 0,
            "lifecycle_only": True,
        }
        symbols.append(synthetic)
        by_symbol[symbol_id] = synthetic
    for previous in reconciled_orders:
        symbol_id = symbol_key(previous)
        if not symbol_id or symbol_id in by_symbol:
            continue
        synthetic = {
            "symbol_id": symbol_id,
            "symbol_name": previous.get("symbol_name") or symbol_id,
            "current_live_holding_quantity": 0,
            "current_price": 0,
            "today_buy_quantity": 0,
            "today_sell_quantity": 0,
            "lifecycle_only": True,
        }
        symbols.append(synthetic)
        by_symbol[symbol_id] = synthetic

    confirmed: dict[str, dict[str, int]] = {}
    observed: dict[str, dict[str, int]] = {}
    for fill in observed_fills or []:
        if not isinstance(fill, dict):
            continue
        symbol_id = symbol_key(fill)
        side = str(fill.get("direction") or "")
        if symbol_id and side in {"buy", "sell"}:
            observed.setdefault(symbol_id, {"buy": 0, "sell": 0})[side] += as_int(
                fill.get("filled_quantity")
            )
    unconfirmed_symbols: set[str] = set()
    for order in reconciled_orders:
        symbol_id = symbol_key(order)
        side = str(order.get("direction") or "")
        status = order.get("broker_reconciliation") if isinstance(order.get("broker_reconciliation"), dict) else {}
        if not symbol_id or side not in {"buy", "sell"}:
            continue
        confirmed.setdefault(symbol_id, {"buy": 0, "sell": 0})[side] += as_int(status.get("filled_quantity"))
        if status.get("status") == "unconfirmed":
            unconfirmed_symbols.add(symbol_id)

    inconsistencies: list[dict[str, Any]] = []
    for symbol_id, item in by_symbol.items():
        pending = active_qty.get(symbol_id, {"buy": 0, "sell": 0})
        item["pending_and_reserved_buy_quantity"] = pending["buy"]
        item["pending_and_reserved_sell_quantity"] = pending["sell"]
        reasons: list[str] = []
        local = confirmed.get(symbol_id, {"buy": 0, "sell": 0})
        observed_item = observed.get(symbol_id, {"buy": 0, "sell": 0})
        account_buy = max(as_int(item.get("today_buy_quantity")), observed_item["buy"])
        account_sell = max(as_int(item.get("today_sell_quantity")), observed_item["sell"])
        if local["buy"] > account_buy:
            reasons.append("confirmed_local_buy_fill_exceeds_account_today_buy_quantity")
        if local["sell"] > account_sell:
            reasons.append("confirmed_local_sell_fill_exceeds_account_today_sell_quantity")
        if symbol_id in unconfirmed_symbols:
            reasons.append("previous_submitted_order_status_unconfirmed")
        if not lookup_complete:
            reasons.append("order_lifecycle_lookup_incomplete")
        if any(reason.startswith("confirmed_local_") for reason in reasons):
            state = "inconsistent"
        elif reasons:
            state = "unconfirmed"
        else:
            state = "consistent"
        item["holding_state_status"] = state
        item["holding_state_reasons"] = reasons
        if state != "consistent":
            inconsistencies.append(
                {
                    "symbol_id": symbol_id,
                    "symbol_name": item.get("symbol_name") or symbol_id,
                    "status": state,
                    "reasons": reasons,
                    "confirmed_local_buy_quantity": local["buy"],
                    "confirmed_local_sell_quantity": local["sell"],
                    "account_today_buy_quantity": account_buy,
                    "account_today_sell_quantity": account_sell,
                }
            )
    account["symbols"] = symbols
    return inconsistencies


def order_lifecycle_preflight(args: argparse.Namespace, *, kis: Kis | None = None) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    account_path = Path(args.account_before_order or output_dir / "account-before-order.json")
    account = load_json(account_path)
    observed_at = now_iso()
    day = execution_order_day({"started_at": account.get("started_at") or ""})
    environment = env_dv(args.env or account.get("execution_environment"))
    errors: list[dict[str, Any]] = []
    client = kis
    if client is None:
        try:
            client = Kis(environment, args.retries)
        except Exception as exc:  # noqa: BLE001
            errors.append(lifecycle_error("kis_client_initialization_failed", redact(exc)))
    start_date, end_date = args.reservation_start_date, args.reservation_end_date
    if not start_date or not end_date:
        start_date, end_date = default_date_range()

    active_orders: list[dict[str, Any]] = []
    if client is not None:
        try:
            active_orders.extend(fetch_reservations(client, start_date, end_date))
        except Exception as exc:  # noqa: BLE001
            errors.append(lifecycle_error("reservation_lookup_failed", redact(exc)))
        try:
            active_orders.extend(fetch_pending_orders(client))
        except Exception as exc:  # noqa: BLE001
            errors.append(lifecycle_error("pending_order_lookup_failed", redact(exc)))

    previous_orders = previous_submitted_cash_orders(output_dir, day)
    today_fills_path = output_dir / "today-fills.json"
    today_fills = load_json(today_fills_path) if today_fills_path.is_file() else {}
    observed_fills = today_fills.get("fills", []) if isinstance(today_fills, dict) else []
    broker_rows: list[dict[str, Any]] = []
    if client is not None:
        try:
            broker_rows = fetch_cash_order_status_rows(client, day)
            found_ids = {broker_order_id(row) for row in broker_rows}
            for order in previous_orders:
                order_id = str(order.get("order_id") or "").strip()
                if not order_id or order_id in found_ids:
                    continue
                for row in fetch_cash_order_status_rows(client, day, order_id):
                    if broker_order_id(row) == order_id:
                        broker_rows.append(row)
                        found_ids.add(order_id)
                        break
        except Exception as exc:  # noqa: BLE001
            errors.append(lifecycle_error("cash_order_status_lookup_failed", redact(exc)))

    lookup_complete = not errors
    reconciled_orders = reconcile_previous_cash_orders(
        previous_orders,
        broker_rows,
        active_orders,
        observed_at=observed_at,
    )
    inconsistencies = apply_order_lifecycle_to_account(
        account,
        active_orders,
        reconciled_orders,
        lookup_complete=lookup_complete,
        observed_fills=observed_fills if isinstance(observed_fills, list) else [],
    )
    active_count = len([item for item in active_orders if item.get("active_status") == "active"])
    account["active_order_lookup_performed"] = lookup_complete
    account["active_orders"] = active_orders
    account.setdefault("active_order_checks", {})["order_lifecycle_preflight"] = f"{active_count} active"
    warnings = [
        item
        for item in account.get("warnings", [])
        if item not in {"active_order_lookup_not_performed", "order_lifecycle_lookup_incomplete"}
    ]
    if not lookup_complete:
        warnings.append("order_lifecycle_lookup_incomplete")
    account["warnings"] = warnings
    status = "failed" if errors else "partial" if inconsistencies else "success"
    lifecycle = {
        "schema_version": "1",
        "run_id": account.get("run_id") or output_dir.name,
        "started_at": account.get("started_at") or "",
        "generated_at": observed_at,
        "stage": "order-lifecycle-preflight",
        "status": status,
        "execution_environment": environment,
        "lookup_complete": lookup_complete,
        "active_order_count": active_count,
        "active_orders": active_orders,
        "previous_submitted_cash_order_count": len(previous_orders),
        "previous_submitted_cash_orders": reconciled_orders,
        "holding_state_issue_count": len(inconsistencies),
        "holding_state_issues": inconsistencies,
        "errors": errors,
    }
    account["order_lifecycle"] = {
        key: lifecycle[key]
        for key in (
            "status",
            "generated_at",
            "lookup_complete",
            "active_order_count",
            "previous_submitted_cash_order_count",
            "holding_state_issue_count",
        )
    }
    write_json(account_path, account)
    write_json(Path(args.output or output_dir / "order-lifecycle.json"), lifecycle)
    return lifecycle


def normalize_execution_order_prices(execution: dict[str, Any], account: dict[str, Any]) -> None:
    account_by_symbol = {symbol_key(item): item for item in account.get("symbols", []) if isinstance(item, dict)}
    for item in execution.get("orders", []):
        if not isinstance(item, dict):
            continue
        symbol = symbol_key(item)
        account_item = account_by_symbol.get(symbol, {})
        current = as_int(account_item.get("current_live_holding_quantity"), as_int(item.get("current_live_holding_quantity")))
        final_quantity = non_negative_int_value(item.get("final_holding_quantity"))
        if final_quantity is None:
            continue
        delta = final_quantity - current
        side = "buy" if delta > 0 else "sell" if delta < 0 else ""
        if not side:
            continue
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
    require_sell_capacity: bool,
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
        if local_sell_gate and require_sell_capacity and (not isinstance(sell_cap, dict) or "max_sell_qty" not in sell_cap):
            block_order(
                order,
                reason="sell_quantity_capacity_missing",
                gate="inquire_psbl_sell",
                message="max_sell_qty unavailable from order-available lookup",
                error_code="sell_gate",
            )
            return qty, 0, True
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
                    message=f"buy orders exceeded latest buy cash limit {cash_limit}",
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


def active_buy_correction_gate(
    order: dict[str, Any],
    *,
    conflict: dict[str, Any],
    desired_qty: int,
    price: int,
    symbol: str,
    capacities: dict[str, dict[str, int]],
    used_cash: int,
    cash_limit: int | None,
) -> tuple[int, bool]:
    conflict_qty = as_int(conflict.get("remaining_quantity"))
    conflict_price = as_price(conflict.get("order_price"))
    incremental_qty = max(0, desired_qty - conflict_qty)
    incremental_cash = max(0, desired_qty * price - conflict_qty * conflict_price)
    if incremental_qty <= 0 and incremental_cash <= 0:
        return 0, False
    cap = capacities.get(symbol)
    cap_present = isinstance(cap, dict)
    if incremental_qty > 0:
        max_buy_qty = as_int(cap.get("max_buy_qty")) if cap_present else 0
        if not cap_present or max_buy_qty <= 0:
            block_order(
                order,
                reason="buy_cash_limit_missing",
                gate="inquire_psbl_order",
                message="max_buy_qty unavailable from order-available lookup",
                error_code="cash_gate",
            )
            return 0, True
        if incremental_qty > max_buy_qty:
            block_order(
                order,
                reason="buy_quantity_exceeds_order_available_quantity",
                gate="inquire_psbl_order",
                message=f"max_buy_qty={max_buy_qty}",
                error_code="cash_gate",
            )
            return 0, True
    if incremental_cash > 0:
        if not cap_present or as_int(cap.get("max_buy_amt")) <= 0:
            block_order(
                order,
                reason="buy_cash_limit_missing",
                gate="inquire_psbl_order",
                message="max_buy_amt unavailable from order-available lookup",
                error_code="cash_gate",
            )
            return 0, True
        if cash_limit is None:
            block_order(
                order,
                reason="buy_cash_limit_missing",
                gate="inquire_psbl_order",
                message="max_buy_amt unavailable from order-available lookup",
                error_code="cash_gate",
            )
            return 0, True
        if cash_limit and used_cash + incremental_cash > cash_limit:
            block_order(
                order,
                reason="buy_cash_gate_reduced_reverse_rank",
                gate="inquire_psbl_order",
                message=f"incremental buy correction exceeded latest buy cash limit {cash_limit}",
                error_code="cash_gate",
            )
            return 0, True
    return incremental_cash, False


def error(code: str, message: str) -> dict[str, Any]:
    return {"stage": "order-execution", "source": "execute_orders", "code": code, "message": message, "required": True}


def lifecycle_error(code: str, message: str) -> dict[str, Any]:
    return {
        "stage": "order-lifecycle-preflight",
        "source": "execute_orders",
        "code": code,
        "message": message,
        "required": True,
    }


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
    active_symbols_with_orders = {
        symbol_key(item)
        for item in active
        if isinstance(item, dict) and item.get("active_status") == "active"
    }
    account_by_symbol = {symbol_key(item): item for item in account.get("symbols", []) if isinstance(item, dict)}
    capacities: dict[str, dict[str, int]] = {}
    sell_capacities: dict[str, dict[str, int]] = {}
    errors: list[dict[str, Any]] = []
    for item in execution.get("orders", []):
        if not isinstance(item, dict):
            continue
        symbol = symbol_key(item)
        if not symbol:
            continue
        if item.get("active_cancel_only") is True or item.get("reconciliation_only") is True:
            continue
        account_item = account_by_symbol.get(symbol, {})
        current = as_int(account_item.get("current_live_holding_quantity"), as_int(item.get("current_live_holding_quantity")))
        final_quantity = non_negative_int_value(item.get("final_holding_quantity"))
        desired_side = ""
        if final_quantity is not None:
            desired_delta = final_quantity - current
            desired_side = "buy" if desired_delta > 0 else "sell" if desired_delta < 0 else ""
        price = normalize_limit_price(item.get("order_price"), desired_side or str(item.get("direction") or ""))
        has_active_order = symbol in active_symbols_with_orders
        if price > 0 and desired_side == "buy":
            try:
                capacities[symbol] = buy_capacity(kis, symbol, price)
            except Exception as exc:  # noqa: BLE001
                if not has_active_order:
                    errors.append(error("order_available_lookup_failed", f"{symbol}: {redact(exc)}"))
        if desired_side == "sell":
            try:
                sell_capacities[symbol] = sell_capacity(kis, symbol)
            except Exception as exc:  # noqa: BLE001
                if not has_active_order:
                    errors.append(error("sell_available_lookup_failed", f"{symbol}: {redact(exc)}"))
    return active, capacities, sell_capacities, errors, kis


def reconcile(account: dict[str, Any], execution: dict[str, Any], active: list[dict[str, Any]], capacities: dict[str, dict[str, int]], sell_capacities: dict[str, dict[str, int]], *, submit: bool, kis: Kis | None, fresh_balance: dict[str, Any] | None = None) -> None:
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
    execution_orders = [
        order for order in execution.get("orders", []) if isinstance(order, dict)
    ]
    execution_symbol_counts: dict[str, int] = {}
    for order in execution_orders:
        order_symbol = symbol_key(order)
        if order_symbol:
            execution_symbol_counts[order_symbol] = (
                execution_symbol_counts.get(order_symbol, 0) + 1
            )
    duplicate_execution_symbols = {
        symbol for symbol, count in execution_symbol_counts.items() if count > 1
    }

    for order in execution_orders:
        symbol = symbol_key(order)
        if symbol in duplicate_execution_symbols:
            order["result"] = "blocked"
            order["reason"] = "duplicate_execution_symbol"
            order["attempts"] = (
                order.get("attempts") if isinstance(order.get("attempts"), list) else []
            )
            order["attempts"].append(
                attempt(
                    "schema",
                    "blocked",
                    f"execution contains more than one order row for {symbol}",
                    "duplicate_execution_symbol",
                )
            )
            blocked += 1
            continue
        account_item = account_by_symbol.get(symbol, {})
        active_item = active_qty.get(symbol, {"buy": 0, "sell": 0})
        current = as_int(account_item.get("current_live_holding_quantity"), as_int(order.get("current_live_holding_quantity")))
        decision_basis = str(order.get("decision_basis") or "")
        if decision_basis in {"profit_protection", "concentration_rebalance"}:
            fresh_current = fresh_balance_quantity(fresh_balance, symbol)
            if fresh_current is not None:
                current = fresh_current
        expected = current + active_item["buy"] - active_item["sell"]
        holding_state_status = str(
            order.get("holding_state_status") or account_item.get("holding_state_status") or ""
        ).strip()
        lifecycle_cancel_only = order.get("reconciliation_only") is True and order.get("active_cancel_only") is True
        if holding_state_status in {"inconsistent", "unconfirmed"} and not lifecycle_cancel_only:
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
                    "reason": "holding_state_not_verified",
                    "attempts": order.get("attempts") if isinstance(order.get("attempts"), list) else [],
                }
            )
            order["attempts"].append(
                attempt(
                    "order-lifecycle-preflight",
                    "blocked",
                    f"holding_state_status={holding_state_status}",
                    "holding_state_not_verified",
                )
            )
            blocked += 1
            continue
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
        desired_delta = final_quantity - current
        desired_side = "buy" if desired_delta > 0 else "sell" if desired_delta < 0 else ""
        desired_qty = abs(desired_delta)
        price = normalize_limit_price(order.get("order_price"), desired_side or side)
        original_price = as_price(order.get("order_price"))
        if price > 0 and original_price != price:
            order["order_price"] = price
            order["order_price_adjustment"] = {
                "from": original_price,
                "to": price,
                "reason": "krx_tick_size",
            }
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
                guard_reason = decision_guard_block_reason(order, side)
                if guard_reason:
                    order["result"] = "blocked"
                    order["reason"] = guard_reason
                    order["attempts"].append(attempt("decision-guard", "blocked", f"decision_guard={order.get('decision_guard')} does not permit new {side} submission", guard_reason))
                    if not active_adjustment_recorded:
                        order_adjustments.append(adjustment_row(conflict, action="keep", reason="same_direction_active_order_kept", result="skipped"))
                    blocked += 1
                    continue
                if side == "sell" and decision_basis == "profit_protection":
                    if not verify_profit_protection_pnl(kis, fresh_balance, symbol):
                        order["result"] = "blocked"
                        order["reason"] = "profit_protection_pnl_recheck_failed"
                        order["attempts"].append(attempt("profit-protection-recheck", "blocked", "positive PnL could not be reverified immediately before submission", "profit_protection_pnl_recheck_failed"))
                        if not active_adjustment_recorded:
                            order_adjustments.append(adjustment_row(conflict, action="keep", reason="same_direction_active_order_kept", result="skipped"))
                        blocked += 1
                        continue
                    if not verify_fresh_reduction_bounds(fresh_balance, symbol, order):
                        order["result"] = "blocked"
                        order["reason"] = "profit_protection_reduction_bound_recheck_failed"
                        order["attempts"].append(attempt("profit-protection-recheck", "blocked", "fresh holdings no longer support the approved partial-reduction bound", "profit_protection_reduction_bound_recheck_failed"))
                        if not active_adjustment_recorded:
                            order_adjustments.append(adjustment_row(conflict, action="keep", reason="same_direction_active_order_kept", result="skipped"))
                        blocked += 1
                        continue
                if side == "sell" and decision_basis == "concentration_rebalance":
                    if (
                        not verify_fresh_reduction_bounds(fresh_balance, symbol, order)
                        or not verify_concentration_rebalance(fresh_balance, symbol, order)
                    ):
                        order["result"] = "blocked"
                        order["reason"] = "concentration_rebalance_recheck_failed"
                        order["attempts"].append(attempt("concentration-rebalance-recheck", "blocked", "latest quantities/valuation or approved reduction bound could not be reverified immediately before submission", "concentration_rebalance_recheck_failed"))
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
                    require_sell_capacity=kis is not None,
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
            correctable = False
            required_cash = 0
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
                correctable = can_correct(conflict, desired_side, order_path, order_api)
                if correctable:
                    # The active order can genuinely be corrected in place this run, so it is
                    # about to be submitted -- gate its quantity/cash now. An uncorrectable
                    # (opposite-direction or path/api-mismatched) order only gets cancelled this
                    # run and its replacement is deferred to a later run with fresh capacity, so
                    # gating it here would risk blocking a cancellation the future replacement's
                    # own capacity has nothing to do with.
                    if desired_side == "buy":
                        required_cash, quantity_blocked = active_buy_correction_gate(
                            desired_order,
                            conflict=conflict,
                            desired_qty=desired_qty,
                            price=price,
                            symbol=symbol,
                            capacities=capacities,
                            used_cash=used_cash,
                            cash_limit=cash_limit,
                        )
                    else:
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
                            require_sell_capacity=kis is not None,
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
                    desired_order if desired_order and correctable else None,
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
                order["result"] = "submitted"
                order["reason"] = "active_order_cancel_submitted"
                order["direction"] = "none"
                order["additional_required_quantity"] = 0
                order["validated_order_quantity"] = 0
                order["order_or_reservation_id"] = request_id
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

        guard_reason = decision_guard_block_reason(order, side)
        if guard_reason:
            order["result"] = "blocked"
            order["reason"] = guard_reason
            order["attempts"].append(attempt("decision-guard", "blocked", f"decision_guard={order.get('decision_guard')} does not permit new {side} submission", guard_reason))
            blocked += 1
            continue

        if side == "sell" and decision_basis == "profit_protection":
            if not verify_profit_protection_pnl(kis, fresh_balance, symbol):
                order["result"] = "blocked"
                order["reason"] = "profit_protection_pnl_recheck_failed"
                order["attempts"].append(attempt("profit-protection-recheck", "blocked", "positive PnL could not be reverified immediately before submission", "profit_protection_pnl_recheck_failed"))
                blocked += 1
                continue
            if not verify_fresh_reduction_bounds(fresh_balance, symbol, order):
                order["result"] = "blocked"
                order["reason"] = "profit_protection_reduction_bound_recheck_failed"
                order["attempts"].append(attempt("profit-protection-recheck", "blocked", "fresh holdings no longer support the approved partial-reduction bound", "profit_protection_reduction_bound_recheck_failed"))
                blocked += 1
                continue

        if side == "sell" and decision_basis == "concentration_rebalance":
            if (
                not verify_fresh_reduction_bounds(fresh_balance, symbol, order)
                or not verify_concentration_rebalance(fresh_balance, symbol, order)
            ):
                order["result"] = "blocked"
                order["reason"] = "concentration_rebalance_recheck_failed"
                order["attempts"].append(attempt("concentration-rebalance-recheck", "blocked", "latest quantities/valuation or approved reduction bound could not be reverified immediately before submission", "concentration_rebalance_recheck_failed"))
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
            require_sell_capacity=kis is not None,
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
        account["order_gate_status"] = "failed"
        execution["status"] = "failed"
        execution["errors"] = [item for item in execution.get("errors", []) if isinstance(item, dict)] + [
            error("submit_requires_live_kis_client", "offline mode cannot submit or mark submitted orders")
        ]
        execution["requires_main_agent_order_execution"] = False
        execution["required_main_agent_actions"] = []
        execution["order_execution_mode"] = "submit-blocked"
        write_json(account_path, account)
        write_json(execution_path, execution)
        return execution
    if args.submit and request_type not in {"demo-submit", "real-submit"}:
        account["order_gate_status"] = "failed"
        execution["status"] = "failed"
        execution["errors"] = [item for item in execution.get("errors", []) if isinstance(item, dict)] + [
            error("submit_requires_explicit_execution_request", f"request_type={request_type or '<missing>'}")
        ]
        execution["requires_main_agent_order_execution"] = False
        execution["required_main_agent_actions"] = []
        execution["order_execution_mode"] = "submit-blocked"
        write_json(account_path, account)
        write_json(execution_path, execution)
        return execution
    normalize_execution_order_prices(execution, account)
    active, capacities, sell_capacities, gate_errors, kis = refresh_gates(args, account, execution)
    account["active_order_lookup_performed"] = True
    account["order_available_lookup_performed"] = not bool(gate_errors)
    account["order_gate_status"] = "failed" if gate_errors else "success"
    account["active_orders"] = active
    account.setdefault("active_order_checks", {})["order_resv_ccnl"] = f"{len([item for item in active if item.get('active_status') == 'active'])} active"
    account["warnings"] = [item for item in account.get("warnings", []) if item not in {"active_order_lookup_not_performed", "order_available_lookup_not_performed"}]
    if gate_errors:
        execution["status"] = "failed"
        execution["errors"] = [item for item in execution.get("errors", []) if isinstance(item, dict)] + gate_errors
        execution["requires_main_agent_order_execution"] = False
        execution["required_main_agent_actions"] = []
    else:
        needs_fresh_balance = any(
            isinstance(item, dict) and str(item.get("decision_basis") or "") in {"profit_protection", "concentration_rebalance"}
            for item in execution.get("orders", [])
        )
        fresh_balance = fetch_fresh_domestic_balance(kis) if needs_fresh_balance else None
        reconcile(account, execution, active, capacities, sell_capacities, submit=args.submit, kis=kis, fresh_balance=fresh_balance)
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
    preflight = sub.add_parser("preflight", help="Refresh active and prior cash-order lifecycle before Judge review.")
    preflight.add_argument("--output-dir", required=True)
    preflight.add_argument("--account-before-order", default="")
    preflight.add_argument("--output", default="")
    preflight.add_argument("--env", default=os.environ.get("CODEX_MCP_TRADING_ENV", "acct"))
    preflight.add_argument("--retries", type=int, default=2)
    preflight.add_argument("--reservation-start-date", default="")
    preflight.add_argument("--reservation-end-date", default="")
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
    if args.command == "preflight":
        payload = order_lifecycle_preflight(args)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload.get("status") != "failed" else 1
    payload = execute(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
