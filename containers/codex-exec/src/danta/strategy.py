"""Deterministic entry, reentry and stale-decision gates (§3–6)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .market import DataQualityError, SessionCalendar, TickTable, coverage_reason
from .models import Candidate, DailyBar, EventRecord, FeatureSnapshot, GateResult, InvestmentThesis, Quote


def initial_stop(entry: Decimal, features: FeatureSnapshot, ticks: TickTable,
                 research_profile: dict) -> Decimal:
    exits = research_profile["exits"]
    atr = features.atr14
    if atr <= 0 or not ticks.is_valid(entry):
        raise DataQualityError("INVALID_ATR_OR_ENTRY_TICK")
    raw = max(ticks.previous(features.low5), entry - Decimal(exits["initial_stop_atr_cap"]) * atr)
    if raw <= 0:
        raise DataQualityError("INVALID_INITIAL_STOP")
    stop = ticks.floor(raw)
    distance = entry - stop
    if (stop <= 0 or stop >= entry or distance < Decimal(exits["minimum_stop_distance_atr"]) * atr or
            distance > Decimal(exits["initial_stop_atr_cap"]) * atr + ticks.tick(entry)):
        raise DataQualityError("INVALID_INITIAL_STOP")
    return stop


def quote_fresh(quote: Quote, now: datetime, maximum_age_seconds: int) -> bool:
    return (quote.valid and quote.venue == "KRX" and quote.received_at <= now and
            0 <= (now - quote.observed_at).total_seconds() <= maximum_age_seconds)


def assess_entry(candidate: Candidate, quote: Quote, events: list[EventRecord],
                 calendar: SessionCalendar, ticks: TickTable, now: datetime,
                 research_profile: dict, verdict: str = "ACCEPT", *,
                 synthetic: bool = False, require_event: bool = True,
                 require_ai: bool = True) -> GateResult:
    """Baseline callers may omit event/AI gates explicitly, never money/risk gates."""
    reasons: list[str] = []
    instrument, f = candidate.instrument, candidate.features
    universe, signal = research_profile["universe"], research_profile["signal"]
    try:
        calendar.require_environment(synthetic=synthetic)
        ticks.require_environment(synthetic=synthetic, now=now, venue=instrument.venue)
    except DataQualityError as error:
        reasons.append(str(error))
    current = calendar.active(now)
    if current is None or not calendar.entry_window(now):
        reasons.append("OUTSIDE_ENTRY_WINDOW")
    if (instrument.kind != universe["instrument_kind"] or instrument.board not in universe["boards"] or
            instrument.venue != universe["venue"] or instrument.status != "NORMAL" or
            not instrument.status_verified or not instrument.sector or not instrument.classification_source or
            instrument.effective_at > now or instrument.instrument_id in universe["excluded_instruments"]):
        reasons.append("INELIGIBLE_UNIVERSE")
    if instrument.instrument_id != f.instrument_id or instrument.board != f.board or quote.instrument_id != instrument.instrument_id:
        reasons.append("INSTRUMENT_MISMATCH")
    completed = [s for s in calendar.sessions if s.closes_at <= now]
    if (f.as_of > now or f.window_end > now or not completed or
            f.last_session_id != completed[-1].session_id or f.bars < universe["minimum_completed_bars"]):
        reasons.append("STALE_OR_INCOMPLETE_FEATURES")
    if f.adtv20 < Decimal(universe["minimum_adtv_krw"]):
        reasons.append("INSUFFICIENT_LIQUIDITY")
    if not quote_fresh(quote, now, research_profile["orders"]["quote_max_age_seconds"]):
        reasons.append("STALE_OR_INVALID_QUOTE")
    if quote.bid is None or quote.ask is None:
        reasons.append("MISSING_EXECUTABLE_QUOTE")
    else:
        spread = (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2) * 10000
        if spread > Decimal(universe["maximum_spread_bps"]):
            reasons.append("SPREAD_TOO_WIDE")
    if require_event:
        if candidate.coverage != "COMPLETE":
            reasons.append(coverage_reason(candidate.coverage))
        records = {e.event_id: e for e in events}
        valid_events = []
        for event_id in candidate.event_ids:
            event = records.get(event_id)
            if event is None:
                continue
            if (event.instrument_id != instrument.instrument_id or not event.official or
                    not event.primary_source_complete or not event.source_uri or not event.source_hash or
                    not event.fact_ids or not event.facts or not event.comparison_basis or
                    event.withdrawn or event.available_at > now or (require_ai and event.polarity != "POSITIVE") or
                    event.family not in signal["event_families"] or event.timing_quality == "UNCERTAIN"):
                continue
            if current is not None:
                try:
                    if 1 <= calendar.event_age(event, current.session_id) <= signal["max_event_age_sessions"]:
                        valid_events.append(event)
                except DataQualityError:
                    pass
        if not valid_events:
            reasons.append("NO_VALID_RECENT_OFFICIAL_EVENT")
        resolved = {event_id for e in valid_events for event_id in e.resolves_event_ids}
        for event in events if require_ai else ():
            if (event.instrument_id == instrument.instrument_id and event.available_at <= now and
                    event.official and event.primary_source_complete and event.event_id not in resolved and
                    (event.event_id in candidate.counterevidence_event_ids or event.correction_of in candidate.event_ids) and
                    (event.polarity == "NEGATIVE" or event.withdrawn)):
                reasons.append("NEGATIVE_EVENT_REVIEW_REQUIRED")
    if not (f.close > f.sma60 and f.sma20 >= f.sma20_five_sessions_ago):
        reasons.append("TREND_GATE_FAILED")
    if f.rs20 <= Decimal(signal["relative_strength_min_exclusive"]):
        reasons.append("RELATIVE_STRENGTH_GATE_FAILED")
    if f.index_close < f.index_sma60:
        reasons.append("BOARD_INDEX_GATE_FAILED")
    if require_ai and verdict != "ACCEPT":
        reasons.append("AI_" + verdict)
    if quote.ask is not None:
        if (quote.ask > f.close + Decimal(signal["chase_above_previous_close_atr"]) * f.atr14 or
                quote.ask > f.sma20 + Decimal(signal["chase_above_sma20_atr"]) * f.atr14):
            reasons.append("WAIT_PRICE")
        try:
            stop = initial_stop(quote.ask, f, ticks, research_profile)
        except DataQualityError as error:
            stop = None
            reasons.append(str(error))
    else:
        stop = None
    return GateResult(allowed=not reasons, reason=reasons[0] if reasons else "ELIGIBLE",
                      reasons=reasons, entry_price=quote.ask, stop_price=stop)


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda c: (c.priority, -c.features.rs20, -c.features.adtv20, c.instrument.instrument_id))


def reentry_eligibility(previous: InvestmentThesis, events: list[EventRecord],
                        completed_bars: list[DailyBar], calendar: SessionCalendar,
                        now: datetime, research_profile: dict, *,
                        require_event: bool = True, require_ai: bool = True) -> GateResult:
    if previous.exited_at is None:
        return GateResult(allowed=False, reason="EXISTING_THESIS_NO_ADDITIONAL_BUY")
    current = calendar.active(now)
    if current is None:
        return GateResult(allowed=False, reason="OUTSIDE_ENTRY_SESSION")
    valid_events = []
    for event in events:
        if (event.instrument_id == previous.instrument_id and event.official and event.primary_source_complete and
                not event.withdrawn and (not require_ai or event.polarity == "POSITIVE") and event.available_at <= now and
                event.timing_quality != "UNCERTAIN" and event.family in research_profile["signal"]["event_families"]):
            try:
                if 1 <= calendar.event_age(event, current.session_id) <= research_profile["signal"]["max_event_age_sessions"]:
                    valid_events.append(event)
            except DataQualityError:
                pass
    if require_event and not valid_events:
        return GateResult(allowed=False, reason="REENTRY_EVENT_EXPIRED_OR_MISSING")
    sessions = calendar.completed_after(previous.exited_at, now)
    eligible_sessions = {s.session_id for s in sessions}
    bars = [b for b in completed_bars if b.session_id in eligible_sessions and b.complete and b.available_at <= now]
    required = research_profile["exits"]["same_event_reentry_completed_sessions"]
    reason = previous.exit_reason
    if reason in {"EXIT_PROTECTION", "EXIT_STOP", "EXIT_TIME_LIMIT", "EXIT_TREND_FAILURE", "REDUCE_TO_LIMIT"}:
        if len(bars) < required:
            return GateResult(allowed=False, reason="REENTRY_COMPLETED_SESSION_REQUIRED")
    if reason in {"EXIT_PROTECTION", "EXIT_STOP"}:
        if bars[-1].close <= previous.average_entry:
            return GateResult(allowed=False, reason="REENTRY_PRICE_NOT_RECOVERED")
    elif reason == "EXIT_THESIS_INVALID":
        if not any(e.available_at > previous.exited_at and e.event_id not in previous.event_ids and
                   set(previous.invalidating_event_ids).issubset(set(e.resolves_event_ids)) and
                   previous.invalidating_event_ids for e in valid_events):
            return GateResult(allowed=False, reason="REENTRY_OFFICIAL_RESOLUTION_REQUIRED")
    elif reason in {"EXIT_TIME_LIMIT", "EXIT_TREND_FAILURE", "REDUCE_TO_LIMIT"}:
        if require_event and not any(e.event_id not in previous.event_ids and e.available_at > previous.exited_at for e in valid_events):
            return GateResult(allowed=False, reason="REENTRY_NEW_EVENT_REQUIRED")
    else:
        return GateResult(allowed=False, reason="REENTRY_EXIT_REASON_UNRESOLVED")
    return GateResult(allowed=True, reason="REENTRY_ELIGIBLE_RECHECK_ALL_ENTRY_GATES")


def entry_plan_completion_allowed(thesis: InvestmentThesis, plan_id: str, active_plan_id: str,
                                  filled_quantity: int, pending_quantity: int,
                                  proposed_quantity: int) -> bool:
    quantities = (filled_quantity, pending_quantity, proposed_quantity)
    if any(type(q) is not int or q < 0 for q in quantities):
        raise ValueError("strict nonnegative quantities required")
    return (plan_id == active_plan_id and thesis.exited_at is None and thesis.reduced_quantity == 0 and
            sum(quantities) <= thesis.planned_quantity)


def decision_fresh(*, completed_at: datetime, now: datetime,
                   frozen_facts_hash: str, current_facts_hash: str,
                   frozen_account_version: int, current_account_version: int,
                   frozen_policy_hash: str, current_policy_hash: str,
                   current_entry_gate: GateResult, research_profile: dict) -> GateResult:
    age = (now - completed_at).total_seconds()
    allowed = (0 <= age <= research_profile["orders"]["decision_max_age_seconds"] and
               frozen_facts_hash == current_facts_hash and frozen_account_version == current_account_version and
               frozen_policy_hash == current_policy_hash and current_entry_gate.allowed)
    return GateResult(allowed=allowed, reason="CURRENT" if allowed else "STALE_DECISION",
                      reasons=[] if allowed else current_entry_gate.reasons)
