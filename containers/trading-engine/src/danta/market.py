"""Point-in-time market facts, explicit session/tick contracts and event identity."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from statistics import median
from zoneinfo import ZoneInfo

from .models import DailyBar, EventRecord, FeatureSnapshot, Session, aware_datetime, finite_decimal


class DataQualityError(ValueError):
    pass


class SessionCalendar:
    def __init__(self, sessions: list[Session], *, provenance: str, verified: bool, synthetic: bool):
        if not sessions or not provenance or not verified:
            raise DataQualityError("UNVERIFIED_CALENDAR")
        self.sessions = sorted(sessions, key=lambda s: s.opens_at)
        self.provenance, self.verified, self.synthetic = provenance, verified, synthetic
        self.by_id = {s.session_id: s for s in self.sessions}
        if len(self.by_id) != len(sessions):
            raise DataQualityError("DUPLICATE_SESSION")
        for previous, current in zip(self.sessions, self.sessions[1:]):
            if previous.closes_at >= current.opens_at or current.ordinal != previous.ordinal + 1:
                raise DataQualityError("NONCONTIGUOUS_CALENDAR")

    def require_environment(self, *, synthetic: bool) -> None:
        if self.synthetic != synthetic:
            raise DataQualityError("CALENDAR_ENVIRONMENT_MISMATCH")

    def session(self, session_id: str) -> Session:
        try:
            return self.by_id[session_id]
        except KeyError as error:
            raise DataQualityError("SESSION_NOT_COVERED") from error

    def available_session(self, available_at: datetime) -> Session:
        """Before continuous close counts today; close or later counts next session."""
        available_at = aware_datetime(available_at)
        # A cursor older than the manifest could silently rejuvenate an old event.
        if available_at < self.sessions[0].opens_at and available_at.date() != self.sessions[0].opens_at.date():
            raise DataQualityError("EVENT_PRECEDES_CALENDAR_COVERAGE")
        for session in self.sessions:
            if available_at < session.closes_at:
                return session
        raise DataQualityError("NEXT_SESSION_NOT_COVERED")

    def active(self, now: datetime) -> Session | None:
        now = aware_datetime(now)
        return next((s for s in self.sessions if s.opens_at <= now < s.closes_at), None)

    def event_age(self, event: EventRecord, current_session_id: str) -> int:
        available = event.available_at
        if event.timing_quality == "DATE_ONLY":
            seoul = ZoneInfo("Asia/Seoul")
            if event.published_date is None or event.published_date > available.astimezone(seoul).date():
                raise DataQualityError("INVALID_EVENT_PUBLICATION_DATE")
            # Count from the earliest possible session; collecting late never rejuvenates an event.
            available = datetime.combine(event.published_date, datetime.min.time(), tzinfo=seoul)
        return self.session(current_session_id).ordinal - self.available_session(available).ordinal + 1

    def completed_after(self, after: datetime, as_of: datetime) -> list[Session]:
        return [s for s in self.sessions if after < s.closes_at <= as_of]

    def holding_sessions(self, first_fill_session: str, current_session_id: str) -> int:
        return self.session(current_session_id).ordinal - self.session(first_fill_session).ordinal + 1

    def entry_window(self, now: datetime) -> bool:
        session = self.active(now)
        return session is not None and session.opens_at + timedelta(minutes=20) <= now <= session.closes_at - timedelta(minutes=30)


class TickTable:
    """Bands are (inclusive lower price, tick), supplied by a versioned manifest."""
    def __init__(self, bands: list[tuple[Decimal, Decimal]], *, provenance: str,
                 verified: bool, synthetic: bool, venue: str = "KRX",
                 effective_at: datetime | None = None, expires_at: datetime | None = None):
        if not provenance or not verified or not bands:
            raise DataQualityError("UNVERIFIED_TICKS")
        self.bands = sorted((finite_decimal(lower), finite_decimal(tick)) for lower, tick in bands)
        if self.bands[0][0] != 0 or any(tick <= 0 for _, tick in self.bands):
            raise DataQualityError("INVALID_TICK_BANDS")
        if len(set(lower for lower, _ in self.bands)) != len(self.bands):
            raise DataQualityError("DUPLICATE_TICK_BANDS")
        self.provenance, self.synthetic, self.venue = provenance, synthetic, venue
        self.effective_at = aware_datetime(effective_at) if effective_at is not None else None
        self.expires_at = aware_datetime(expires_at) if expires_at is not None else None
        if not synthetic and (effective_at is None or expires_at is None):
            raise DataQualityError("TICK_VALIDITY_REQUIRED")

    def require_environment(self, *, synthetic: bool, now: datetime, venue: str = "KRX") -> None:
        if self.synthetic != synthetic or self.venue != venue:
            raise DataQualityError("TICK_ENVIRONMENT_MISMATCH")
        if self.effective_at is not None and now < self.effective_at:
            raise DataQualityError("TICKS_NOT_EFFECTIVE")
        if self.expires_at is not None and now >= self.expires_at:
            raise DataQualityError("TICKS_EXPIRED")

    def tick(self, price: Decimal) -> Decimal:
        if price < 0:
            raise DataQualityError("NEGATIVE_PRICE")
        return next(tick for lower, tick in reversed(self.bands) if price >= lower)

    def floor(self, price: Decimal) -> Decimal:
        tick = self.tick(price)
        return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick

    def previous(self, price: Decimal) -> Decimal:
        return self.floor(price - min(tick for _, tick in self.bands))

    def is_valid(self, price: Decimal) -> bool:
        return price > 0 and self.floor(price) == price


def return_over_intervals(closes: list[Decimal], intervals: int = 20) -> Decimal:
    if len(closes) < intervals + 1:
        raise DataQualityError("INSUFFICIENT_RETURN_HISTORY")
    if closes[-intervals - 1] <= 0:
        raise DataQualityError("INVALID_RETURN_BASE")
    return closes[-1] / closes[-intervals - 1] - 1


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def _validate_bars(bars: list[DailyBar], as_of: datetime, minimum: int) -> None:
    if len(bars) < minimum:
        raise DataQualityError("INSUFFICIENT_COMPLETED_BARS")
    if any(not b.complete or b.closes_at > as_of or b.available_at > as_of for b in bars):
        raise DataQualityError("INCOMPLETE_OR_FUTURE_BAR")
    if any(not b.ohlc_consistently_adjusted or not b.adjustment_basis or not b.source for b in bars):
        raise DataQualityError("INCONSISTENT_OHLC_ADJUSTMENT")
    if len({b.adjustment_basis for b in bars}) != 1:
        raise DataQualityError("MIXED_ADJUSTMENT_BASIS")
    if len({b.session_id for b in bars}) != len(bars):
        raise DataQualityError("DUPLICATE_BAR")
    if any(left.closes_at >= right.closes_at for left, right in zip(bars, bars[1:])):
        raise DataQualityError("UNORDERED_BARS")


def calculate_features(bars: list[DailyBar], index_bars: list[DailyBar], *,
                       instrument_id: str, board: str, as_of: datetime,
                       research_profile: dict) -> FeatureSnapshot:
    as_of = aware_datetime(as_of)
    minimum = research_profile["universe"]["minimum_completed_bars"]
    signal = research_profile["signal"]
    fast, slow, slope = signal["fast_sma"], signal["slow_sma"], signal["sma_slope_lookback"]
    intervals, atr_count = signal["relative_return_intervals"], signal["atr_bars"]
    needed = max(minimum, slow, fast + slope, intervals + 1, atr_count + 1)
    _validate_bars(bars, as_of, needed)
    _validate_bars(index_bars, as_of, max(slow, intervals + 1))
    if [b.session_id for b in bars[-slow:]] != [b.session_id for b in index_bars[-slow:]]:
        raise DataQualityError("STOCK_INDEX_SESSION_MISMATCH")
    closes, index_closes = [b.close for b in bars], [b.close for b in index_bars]
    true_ranges = [max(b.high - b.low, abs(b.high - previous.close), abs(b.low - previous.close))
                   for previous, b in zip(bars[-atr_count-1:-1], bars[-atr_count:])]
    stock_return = return_over_intervals(closes, intervals)
    index_return = return_over_intervals(index_closes, intervals)
    return FeatureSnapshot(
        instrument_id=instrument_id, board=board, as_of=as_of,
        window_start=bars[0].opens_at, window_end=bars[-1].closes_at,
        last_session_id=bars[-1].session_id, bars=len(bars),
        adjustment_basis=bars[-1].adjustment_basis,
        index_adjustment_basis=index_bars[-1].adjustment_basis,
        close=closes[-1], sma20=_mean(closes[-fast:]),
        sma20_five_sessions_ago=_mean(closes[-fast-slope:-slope]),
        sma60=_mean(closes[-slow:]), return20=stock_return,
        index_return20=index_return, rs20=stock_return-index_return,
        atr14=_mean(true_ranges),
        adtv20=median([b.turnover for b in bars[-research_profile["universe"]["adtv_window"]:]]),
        low5=min(b.low for b in bars[-research_profile["exits"]["initial_stop_low_window"]:]),
        index_close=index_closes[-1], index_sma60=_mean(index_closes[-slow:]),
        last_two_closes=(closes[-2], closes[-1]),
        last_two_sma20=(_mean(closes[-fast-1:-1]), _mean(closes[-fast:])),
    )


def coverage_reason(coverage: str) -> str:
    return {"COMPLETE_NO_EVENT": "NO_EVENT", "FETCH_FAILED": "EVENT_FETCH_FAILED",
            "PARTIAL": "EVENT_COVERAGE_PARTIAL", "COMPLETE": "COMPLETE"}[coverage]


class EventRegistry:
    """Canonical keys come from official fact identity, never model wording."""
    def __init__(self, events: list[EventRecord] = ()):
        self.records: dict[str, EventRecord] = {}
        self.keys: dict[tuple[str, str], str] = {}
        self.official_keys: dict[tuple[str, str], str] = {}
        for event in events:
            self.ingest(event)

    def ingest(self, event: EventRecord) -> tuple[EventRecord, bool]:
        key = (event.instrument_id, event.normalized_key)
        official_key = (event.instrument_id, event.official_id)
        # A new official correction is its own evidence, linked to the original.
        # Preserve the original for thesis references and supersession checks.
        if event.official and event.correction_of and official_key not in self.official_keys:
            parent_id = self.official_keys.get((event.instrument_id, event.correction_of), event.correction_of)
            parent = self.records.get(parent_id)
            if parent is not None and parent.instrument_id != event.instrument_id:
                raise DataQualityError("FOREIGN_CORRECTION_PARENT")
            if event.event_id in self.records:
                raise DataQualityError("EVENT_ID_COLLISION")
            event = event.model_copy(update={"correction_of": parent_id})
            self.records[event.event_id] = event
            self.official_keys[official_key] = event.event_id
            self.keys[key] = event.event_id
            return event, True
        if official_key in self.official_keys:
            self.keys[key] = self.official_keys[official_key]
        if key not in self.keys:
            if event.event_id in self.records:
                raise DataQualityError("EVENT_ID_COLLISION")
            self.keys[key] = event.event_id
            self.official_keys[official_key] = event.event_id
            self.records[event.event_id] = event
            return event, True
        existing = self.records[self.keys[key]]
        # Article copies cannot replace a complete official source or reset its age.
        upgraded = (event.official and event.primary_source_complete and
                    (not existing.official or not existing.primary_source_complete))
        substantive = (event.official and event.primary_source_complete and
                       event.official_id == existing.official_id and
                       (event.facts != existing.facts or event.withdrawn != existing.withdrawn or
                        event.polarity != existing.polarity))
        if substantive and event.observed_at < existing.observed_at:
            return existing, False
        chosen = event if substantive or upgraded else existing
        merged = chosen.model_copy(update={
            "event_id": existing.event_id,
            "available_at": min(existing.available_at, event.available_at),
            "aliases": sorted(set(existing.aliases + event.aliases + [event.event_id, event.source_uri])),
        })
        self.records[existing.event_id] = merged
        self.official_keys[official_key] = existing.event_id
        return merged, substantive or upgraded
