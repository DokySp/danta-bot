"""Shared KST order-session policy for daily-trading."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")

# Seconds from midnight. KRX accepts regular-session orders from 08:20;
# NXT after-market accepts orders from 15:30 and begins matching at 15:40.
CASH_ORDER_WINDOWS = {
    "KRX": ((8 * 3600 + 20 * 60, 15 * 3600 + 30 * 60),),
    "NXT": (
        (8 * 3600, 8 * 3600 + 50 * 60),
        (9 * 3600 + 30, 15 * 3600 + 20 * 60),
        (15 * 3600 + 30 * 60, 20 * 3600),
    ),
    "SOR": ((8 * 3600, 20 * 3600),),
}
RESERVATION_ORDER_START_SECOND = 15 * 3600 + 40 * 60
RESERVATION_ORDER_END_SECOND = 7 * 3600 + 30 * 60
RESERVATION_MAINTENANCE_START_SECOND = 23 * 3600 + 40 * 60
RESERVATION_MAINTENANCE_END_SECOND = 10 * 60


def as_kst(at: datetime | str | None = None) -> datetime:
    observed = at or datetime.now(KST)
    if isinstance(observed, str):
        text = observed.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        observed = datetime.fromisoformat(text)
    return observed.replace(tzinfo=KST) if observed.tzinfo is None else observed.astimezone(KST)


def cash_order_session_open(exchange: str, at: datetime | str | None = None) -> bool:
    observed = as_kst(at)
    if observed.weekday() >= 5:
        return False
    windows = CASH_ORDER_WINDOWS.get(str(exchange or "").strip().upper())
    if windows is None:
        raise ValueError(f"unsupported exchange: {exchange}")
    second = observed.hour * 3600 + observed.minute * 60 + observed.second
    return any(start <= second < end for start, end in windows)


def reservation_order_session_open(
    at: datetime | str | None = None,
    *,
    market_open_day: bool | None = None,
) -> bool:
    observed = as_kst(at)
    second = observed.hour * 3600 + observed.minute * 60 + observed.second
    if second >= RESERVATION_MAINTENANCE_START_SECOND or second < RESERVATION_MAINTENANCE_END_SECOND:
        return False
    if observed.weekday() >= 5 or market_open_day is False:
        return True
    return second >= RESERVATION_ORDER_START_SECOND or second < RESERVATION_ORDER_END_SECOND


def resolve_order_path_for_exchange(
    order_path: str,
    exchange: str,
    at: datetime | str | None = None,
    *,
    market_open_day: bool | None = None,
) -> tuple[str, str]:
    requested = str(order_path or "auto").strip().lower()
    if requested in {"reservation", "immediate"}:
        return requested, "explicit"
    if requested != "auto":
        raise ValueError(f"unsupported order_path: {requested}")

    observed = as_kst(at)
    if observed.weekday() >= 5 or market_open_day is False:
        if reservation_order_session_open(observed, market_open_day=market_open_day):
            reason = "auto_closed_weekend" if observed.weekday() >= 5 else "auto_closed_market"
            return "reservation", reason
    elif cash_order_session_open(exchange, observed):
        reason = "auto_regular_session" if cash_order_session_open("KRX", observed) else "auto_extended_session"
        return "immediate", reason
    if reservation_order_session_open(observed, market_open_day=market_open_day):
        return "reservation", "auto_reservation_session"
    raise ValueError(
        "auto order path cannot select a supported KIS order API for "
        f"{observed.isoformat(timespec='minutes')} on {str(exchange or '').upper()}"
    )
