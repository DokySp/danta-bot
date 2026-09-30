"""Model-independent protection, immutable holding clocks and risk reduction."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING

from .market import SessionCalendar, TickTable
from .models import DailyBar, EventRecord, ExitPlan, FeatureSnapshot, Holding, InvestmentThesis, PortfolioSnapshot, Quote, Reduction
from .portfolio import exposures, snapshot_valid
from .strategy import monitor_quote_max_age, quote_fresh


def update_trailing_stop(thesis: InvestmentThesis, features: FeatureSnapshot,
                         completed_since_entry: list[DailyBar], ticks: TickTable,
                         research_profile: dict, *, observed_price: Decimal | None = None) -> InvestmentThesis:
    """Return a new thesis; fill clock/risk budget/initial stop are never replaced."""
    if thesis.protection_started_at is None:
        return thesis.model_copy(deep=True)
    mfe = max(thesis.average_entry, thesis.mfe_price or thesis.average_entry,
              observed_price if observed_price is not None else thesis.average_entry)
    bars = [b for b in completed_since_entry if b.complete and b.available_at <= features.as_of and
            thesis.protection_started_at < b.closes_at <= features.window_end]
    if any(not b.ohlc_consistently_adjusted or b.adjustment_basis != features.adjustment_basis for b in bars):
        raise ValueError("TRAILING_ADJUSTMENT_MISMATCH")
    # Completed closes after a fill are known post-fill observations; pre-fill bar highs are not.
    if bars:
        mfe = max(mfe, *(b.close for b in bars))
    stop = thesis.current_stop
    activated = mfe-thesis.average_entry >= thesis.initial_r_price*Decimal(research_profile["exits"]["trail_activation_initial_r"])
    if activated and bars and features.atr14 > 0:
        candidate = max(b.close for b in bars)-Decimal(research_profile["exits"]["trail_atr"])*features.atr14
        if candidate > 0:
            stop = max(stop, ticks.floor(candidate))
    return thesis.model_copy(update={"mfe_price": mfe, "current_stop": stop})


def evaluate_exit(thesis: InvestmentThesis, holding: Holding, quote: Quote | None,
                  calendar: SessionCalendar, now: datetime, research_profile: dict, *,
                  features: FeatureSnapshot | None = None, account_complete: bool = True,
                  orders_known: bool = True, tradable: bool = True,
                  invalidating_events: list[EventRecord] = (), reduction_quantity: int = 0) -> ExitPlan:
    """Intent only; caller cancels opposite orders and reconciles before submission."""
    def result(action: str, quantity: int = 0, reasons: list[str] | None = None,
               cancel: bool = False, kind: str | None = None, observed_at: datetime | None = None) -> ExitPlan:
        return ExitPlan(instrument_id=holding.instrument_id, action=action, quantity=quantity,
                        reasons=reasons or [action], cancel_pending_entries=cancel,
                        trigger_kind=kind, trigger_observed_at=observed_at)

    if not account_complete or not orders_known or not holding.source_verified:
        return result("RECONCILE_REQUIRED")
    session = calendar.active(now)
    max_age = monitor_quote_max_age(research_profile)
    fresh_bid = (quote is not None and quote.instrument_id == holding.instrument_id and
                 quote_fresh(quote, now, max_age) and quote.bid is not None)
    fresh_trade = (quote is not None and quote.instrument_id == holding.instrument_id and quote.valid and
                   quote.received_at <= now and
                   quote.venue == "KRX" and quote.last is not None and quote.last_observed_at is not None and
                   0 <= (now-quote.last_observed_at).total_seconds() <= max_age)
    reasons: list[str] = []
    kind, observed_at = None, None
    stop = max(thesis.current_stop, holding.stop)
    if fresh_bid and quote.bid <= stop:
        reasons.append("EXIT_PROTECTION")
        kind, observed_at = "EXECUTABLE_BID", quote.observed_at
    elif fresh_trade and quote.last <= stop:
        reasons.append("EXIT_PROTECTION")
        kind, observed_at = "SAME_VENUE_TRADE", quote.last_observed_at
    invalid = [e for e in invalidating_events if e.instrument_id == holding.instrument_id and
               e.official and e.primary_source_complete and e.source_hash and e.fact_ids and e.available_at <= now and
               (e.event_id in thesis.invalidating_event_ids or e.correction_of in thesis.event_ids) and
               (e.polarity == "NEGATIVE" or e.withdrawn or
                e.event_id in thesis.invalidating_event_ids and e.correction_of in thesis.event_ids)]
    if invalid:
        reasons.append("EXIT_THESIS_INVALID")
    # An expired session remains overdue even after close, during a halt, or on restart.
    deadline = None
    if thesis.protection_session is not None:
        first = calendar.session(thesis.protection_session)
        due = next((s for s in calendar.sessions if s.ordinal == first.ordinal+thesis.max_holding_sessions-1), None)
        if due is not None:
            deadline = due.closes_at-timedelta(minutes=10)
            if now >= deadline:
                reasons.append("EXIT_TIME_LIMIT")
    from .deployment_sources import completed_sessions
    completed = completed_sessions(calendar, now)
    if (features is not None and features.window_end <= now and features.as_of <= now and
            completed and features.last_session_id == completed[-1].session_id):
        if all(close < sma for close, sma in zip(features.last_two_closes, features.last_two_sma20)):
            reasons.append("EXIT_TREND_FAILURE")
    if type(reduction_quantity) is not int or reduction_quantity < 0:
        raise ValueError("strict nonnegative reduction quantity required")
    if reduction_quantity:
        reasons.append("REDUCE_TO_LIMIT")
    if not tradable or session is None:
        if "EXIT_TIME_LIMIT" in reasons:
            return result("EXIT_OVERDUE", reasons=reasons, cancel=True)
        return result("UNEXECUTABLE", reasons=reasons or ["INVALID_SELL_SESSION"], cancel=bool(reasons))
    if not fresh_bid and not fresh_trade:
        return result("MONITOR_DEGRADED", reasons=["PRICE_UNVERIFIED"] + reasons)
    if not reasons:
        return result("KEEP_QUANTITY")
    if holding.sellable_quantity == 0:
        return result("UNEXECUTABLE", reasons=reasons+["NO_STRATEGY_SELLABLE_QUANTITY"], cancel=True)
    quantity = min(holding.quantity, holding.sellable_quantity)
    if reasons[0] == "REDUCE_TO_LIMIT":
        quantity = min(quantity, reduction_quantity)
    return result(reasons[0], quantity, reasons, cancel=True, kind=kind, observed_at=observed_at)


class ConcentrationMonitor:
    """Serializable observations; caller persists state and the fixed plan atomically."""
    def __init__(self, state: dict | None = None):
        state = state or {}
        self.observations: dict[str, tuple[int, datetime]] = {
            key: (item[0], datetime.fromisoformat(item[1])) for key, item in state.get("observations", {}).items()}
        self.active_plan = bool(state.get("active_plan", False))
        self.cancel_pending_entries = bool(state.get("cancel_pending_entries", False))
        self.last_valid_at = datetime.fromisoformat(state["last_valid_at"]) if state.get("last_valid_at") else None

    def state(self) -> dict:
        return {"observations": {key: [count, at.isoformat()] for key, (count, at) in self.observations.items()},
                "active_plan": self.active_plan, "cancel_pending_entries": self.cancel_pending_entries,
                "last_valid_at": self.last_valid_at.isoformat() if self.last_valid_at else None}

    def completed(self) -> None:
        self.active_plan = False
        self.cancel_pending_entries = False
        self.observations.clear()

    def observe(self, snapshot: PortfolioSnapshot, now: datetime, research_profile: dict) -> list[Reduction]:
        if self.active_plan or not snapshot_valid(snapshot, now):
            return []
        # Re-fetching the same valuation is not a second independent observation.
        at = snapshot.as_of
        if self.last_valid_at is not None and at <= self.last_valid_at:
            return []
        self.last_valid_at = at
        p = research_profile["portfolio"]
        issuers, sectors, gross = exposures(snapshot)
        crossed = {"issuer:"+key for key, value in issuers.items() if value > snapshot.nav*Decimal(p["trim_position_trigger"])}
        crossed |= {"sector:"+key for key, value in sectors.items() if value > snapshot.nav*Decimal(p["trim_sector_trigger"])}
        if gross > snapshot.nav*Decimal(p["trim_gross_trigger"]):
            crossed.add("gross")
        self.observations = {key: item for key, item in self.observations.items() if key in crossed}
        confirmed = set()
        for key in crossed:
            count, previous = self.observations.get(key, (0, at))
            if not count or (at-previous).total_seconds() >= p["trim_min_observation_gap_seconds"]:
                count += 1
                self.observations[key] = (count, at)
            if count >= p["trim_confirmation_observations"]:
                confirmed.add(key)
        if not confirmed:
            return []
        self.active_plan = True
        self.cancel_pending_entries = bool(snapshot.pending_entries)
        # Pending buys are cancelled first. Targets below are for reconciled held shares.
        quantities = {h.instrument_id: h.quantity for h in snapshot.holdings}
        reductions = {h.instrument_id: 0 for h in snapshot.holdings}
        reasons: dict[str, list[str]] = {h.instrument_id: [] for h in snapshot.holdings}

        def trim(group, target: Decimal, reason: str) -> None:
            total = sum((quantities[h.instrument_id]*h.mark for h in group), Decimal(0))
            for holding in sorted(group, key=lambda h: (-h.quantity*h.mark, h.instrument_id)):
                excess = total-target
                if excess <= 0:
                    break
                needed = int((excess/holding.mark).to_integral_value(rounding=ROUND_CEILING))
                quantity = min(needed, quantities[holding.instrument_id], holding.sellable_quantity-reductions[holding.instrument_id])
                if quantity <= 0:
                    continue
                quantities[holding.instrument_id] -= quantity
                reductions[holding.instrument_id] += quantity
                total -= quantity*holding.mark
                reasons[holding.instrument_id].append(reason)

        for issuer in sorted(issuers):
            if "issuer:"+issuer in confirmed:
                trim([h for h in snapshot.holdings if h.issuer_id == issuer], snapshot.nav*Decimal(p["entry_position_weight"]), "POSITION_CONCENTRATION")
        for sector in sorted(sectors):
            if "sector:"+sector in confirmed:
                trim([h for h in snapshot.holdings if h.sector == sector], snapshot.nav*Decimal(p["entry_sector_weight"]), "SECTOR_CONCENTRATION")
        if "gross" in confirmed:
            trim(snapshot.holdings, snapshot.nav*Decimal(p["entry_gross_weight"]), "GROSS_CONCENTRATION")
        return [Reduction(instrument_id=h.instrument_id, thesis_id=h.thesis_id,
                          quantity=reductions[h.instrument_id], target_quantity=quantities[h.instrument_id],
                          reasons=reasons[h.instrument_id], frozen_nav=snapshot.nav)
                for h in snapshot.holdings if reductions[h.instrument_id]]


class DrawdownCircuit:
    """Use a cash-flow-adjusted NAV index, not raw account cash balances."""
    def __init__(self, *, high_watermark: Decimal | None = None, paused: bool = False):
        self.high_watermark, self.paused = high_watermark, paused

    def observe(self, flow_adjusted_nav_index: Decimal, research_profile: dict) -> dict:
        if not flow_adjusted_nav_index.is_finite() or flow_adjusted_nav_index <= 0:
            raise ValueError("positive flow-adjusted NAV index required")
        self.high_watermark = max(self.high_watermark or flow_adjusted_nav_index, flow_adjusted_nav_index)
        drawdown = 1-flow_adjusted_nav_index/self.high_watermark
        newly_paused = not self.paused and drawdown >= Decimal(research_profile["exits"]["drawdown_pause_fraction"])
        self.paused = self.paused or newly_paused
        return {"drawdown": drawdown, "new_risk_paused": self.paused,
                "cancel_pending_entries": self.paused, "keep_protection": True,
                "liquidate_all": False, "newly_paused": newly_paused,
                "requires_human_resume": self.paused}
