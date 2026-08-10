"""Shared KST order-session policy for daily-trading."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")

# Seconds from midnight. NXT after-market accepts orders from 15:30 and
# begins matching at 15:40; live KIS preflight remains the final gate.
CASH_ORDER_WINDOWS = {
    "KRX": ((9 * 3600, 15 * 3600 + 30 * 60),),
    "NXT": (
        (8 * 3600, 8 * 3600 + 50 * 60),
        (9 * 3600 + 30, 15 * 3600 + 20 * 60),
        (15 * 3600 + 30 * 60, 20 * 3600),
    ),
    "SOR": ((8 * 3600, 20 * 3600),),
}
RESERVATION_ORDER_START_SECOND = 15 * 3600 + 40 * 60
RESERVATION_ORDER_END_SECOND = 7 * 3600 + 30 * 60


def as_kst(at: datetime | None = None) -> datetime:
    observed = at or datetime.now(KST)
    return observed.replace(tzinfo=KST) if observed.tzinfo is None else observed.astimezone(KST)


def cash_order_session_open(exchange: str, at: datetime | None = None) -> bool:
    observed = as_kst(at)
    if observed.weekday() >= 5:
        return False
    windows = CASH_ORDER_WINDOWS.get(str(exchange or "").strip().upper())
    if windows is None:
        raise ValueError(f"unsupported exchange: {exchange}")
    second = observed.hour * 3600 + observed.minute * 60 + observed.second
    return any(start <= second < end for start, end in windows)


def reservation_order_session_open(at: datetime | None = None) -> bool:
    observed = as_kst(at)
    if observed.weekday() >= 5:
        return True
    second = observed.hour * 3600 + observed.minute * 60 + observed.second
    return second >= RESERVATION_ORDER_START_SECOND or second < RESERVATION_ORDER_END_SECOND
