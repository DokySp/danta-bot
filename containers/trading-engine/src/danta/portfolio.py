"""Cash-based integer sizing with current holdings and unfilled cash reservations."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR

from .market import DataQualityError
from .models import Candidate, CostSchedule, EntryPlan, PendingEntry, PortfolioSnapshot, Quote
from .strategy import rank_candidates


ZERO = Decimal(0)


def floor_quantity(value: Decimal) -> int:
    return max(0, int(value.to_integral_value(rounding=ROUND_FLOOR)))


def validate_costs(costs: CostSchedule | None, snapshot: PortfolioSnapshot, now: datetime) -> None:
    if costs is None or not costs.verified or not costs.source:
        raise DataQualityError("COSTS_UNVERIFIED")
    if costs.synthetic != snapshot.synthetic or costs.account_alias != snapshot.account_alias or costs.venue != "KRX":
        raise DataQualityError("COST_SCOPE_MISMATCH")
    if not costs.effective_at <= now < costs.expires_at:
        raise DataQualityError("COSTS_OUTSIDE_VALIDITY")


def buy_commission(costs: CostSchedule, quantity: int, price: Decimal) -> Decimal:
    return max(quantity * price * costs.buy_commission_rate, costs.minimum_buy_commission) if quantity else ZERO


def sell_cost(costs: CostSchedule, quantity: int, price: Decimal) -> Decimal:
    if not quantity:
        return ZERO
    return max(quantity * price * costs.sell_commission_rate, costs.minimum_sell_commission) + quantity * price * costs.sell_tax_rate


def slippage(costs: CostSchedule, quantity: int, price: Decimal, side: str) -> Decimal:
    bps = costs.buy_slippage_bps if side == "BUY" else costs.sell_slippage_bps
    return quantity * price * bps / 10000


def entry_risk(costs: CostSchedule, quantity: int, entry: Decimal, stop: Decimal,
               atr: Decimal, gap_buffer: Decimal) -> Decimal:
    return (quantity * (entry - stop + gap_buffer * atr) + buy_commission(costs, quantity, entry) +
            sell_cost(costs, quantity, stop) + slippage(costs, quantity, stop, "SELL"))


def roundtrip_friction(costs: CostSchedule, quantity: int, ask: Decimal, bid: Decimal) -> Decimal:
    return (quantity * (ask - bid) + buy_commission(costs, quantity, ask) + sell_cost(costs, quantity, bid) +
            slippage(costs, quantity, ask, "BUY") + slippage(costs, quantity, bid, "SELL"))


def snapshot_valid(snapshot: PortfolioSnapshot, now: datetime) -> bool:
    return (snapshot.complete and snapshot.ownership_verified and snapshot.sector_classification_verified and
            snapshot.as_of <= now and
            snapshot.broker_reflected_reserve_cash <= sum((p.reserved_cash for p in snapshot.pending_entries), ZERO) and
            all(h.source_verified and h.valuation_quality == "EXACT" and h.valuation_at == snapshot.as_of and h.sector for h in snapshot.holdings))


def current_planned_risk(snapshot: PortfolioSnapshot, costs: CostSchedule,
                         research_profile: dict) -> Decimal:
    gap = Decimal(research_profile["portfolio"]["gap_buffer_atr"])
    existing = sum((h.quantity * (max(h.mark - h.stop, ZERO) + gap * h.atr) +
                    sell_cost(costs, h.quantity, h.stop) + slippage(costs, h.quantity, h.stop, "SELL")
                    for h in snapshot.holdings), ZERO)
    pending = sum((p.reserved_risk if p.reserved_risk is not None else p.remaining_quantity * p.unit_risk
                   for p in snapshot.pending_entries), ZERO)
    return existing + pending


def exposures(snapshot: PortfolioSnapshot) -> tuple[dict[str, Decimal], dict[str, Decimal], Decimal]:
    issuers: dict[str, Decimal] = {}
    sectors: dict[str, Decimal] = {}
    for holding in snapshot.holdings:
        value = holding.quantity * holding.mark
        issuers[holding.issuer_id] = issuers.get(holding.issuer_id, ZERO) + value
        sectors[holding.sector] = sectors.get(holding.sector, ZERO) + value
    for pending in snapshot.pending_entries:
        value = pending.remaining_quantity * pending.entry_price
        issuers[pending.issuer_id] = issuers.get(pending.issuer_id, ZERO) + value
        sectors[pending.sector] = sectors.get(pending.sector, ZERO) + value
    return issuers, sectors, sum(issuers.values(), ZERO)


def size_entry(candidate: Candidate, quote: Quote, stop: Decimal,
               snapshot: PortfolioSnapshot, costs: CostSchedule | None,
               now: datetime, research_profile: dict) -> EntryPlan:
    """Returns a fixed zero-quantity plan on an infeasible or unverifiable entry."""
    if quote.ask is None:
        raise DataQualityError("MISSING_ASK")
    entry, instrument = quote.ask, candidate.instrument
    p = research_profile["portfolio"]
    empty = dict(instrument_id=instrument.instrument_id, quantity=0, entry_price=entry,
                 stop_price=stop, risk_budget=None, unit_risk=None,
                 total_risk=ZERO, reserved_cash=ZERO, expected_roundtrip_friction=ZERO,
                 target_weight=ZERO, q_risk=None,
                 expires_at=now + timedelta(seconds=research_profile["orders"]["entry_expiry_seconds"]))

    def blocked(reason: str) -> EntryPlan:
        return EntryPlan(reason=reason, **empty)

    if stop <= 0 or stop >= entry or candidate.features.atr14 <= 0:
        return blocked("INVALID_INITIAL_STOP")
    if not snapshot_valid(snapshot, now):
        return blocked("PORTFOLIO_QUALITY_INSUFFICIENT")
    if snapshot.new_risk_paused or snapshot.monitor_degraded:
        return blocked("NEW_RISK_PAUSED")
    if quote.instrument_id != instrument.instrument_id or quote.bid is None:
        return blocked("QUOTE_SCOPE_MISMATCH")
    if any(h.issuer_id == instrument.issuer_id and h.quantity > 0 for h in snapshot.holdings):
        return blocked("EXISTING_THESIS_NO_ADDITIONAL_BUY")
    if any(e.issuer_id == instrument.issuer_id and e.remaining_quantity > 0 for e in snapshot.pending_entries):
        return blocked("ENTRY_PLAN_ALREADY_WORKING")
    try:
        validate_costs(costs, snapshot, now)
    except DataQualityError as error:
        return blocked(str(error))
    assert costs is not None
    issuer_values, _, _ = exposures(snapshot)
    if len([v for v in issuer_values.values() if v > 0]) >= p["max_issuers"]:
        return blocked("NO_ISSUER_SLOT")
    gap = Decimal(p["gap_buffer_atr"])
    reserved_cash = sum((e.reserved_cash for e in snapshot.pending_entries), ZERO)
    cash = min(snapshot.allocated_cash - reserved_cash,
               snapshot.broker_available_cash - (reserved_cash - snapshot.broker_reflected_reserve_cash))
    cap = min(floor_quantity(cash/entry),
              floor_quantity(candidate.features.adtv20*Decimal(p["max_plan_adtv_fraction"])/entry))
    if cap <= 0:
        return blocked("NO_FEASIBLE_SIZE")

    def exact_feasible(q: int) -> bool:
        return q*entry + buy_commission(costs, q, entry) <= cash

    # All cost functions are nondecreasing: binary search avoids per-share decrement loops.
    low, high = 0, cap
    while low < high:
        middle = (low + high + 1) // 2
        if exact_feasible(middle):
            low = middle
        else:
            high = middle - 1
    quantity = low
    if not quantity:
        return blocked("NO_FEASIBLE_SIZE")
    friction = roundtrip_friction(costs, quantity, entry, quote.bid)
    total_risk = entry_risk(costs, quantity, entry, stop, candidate.features.atr14, gap)
    return EntryPlan(**{**empty, "quantity": quantity, "reason": "SIZED",
                        "unit_risk": total_risk/quantity, "total_risk": total_risk,
                        "reserved_cash": quantity*entry + buy_commission(costs, quantity, entry),
                        "expected_roundtrip_friction": friction,
                        "target_weight": quantity*entry/snapshot.nav})


def allocate_entries(candidates: list[Candidate], quotes: dict[str, Quote],
                     stops: dict[str, Decimal], snapshot: PortfolioSnapshot,
                     costs: CostSchedule | None, now: datetime,
                     research_profile: dict) -> list[EntryPlan]:
    """Use only candidates which already passed assess_entry; reserve each result."""
    working = snapshot.model_copy(deep=True)
    plans = []
    for candidate in rank_candidates(candidates):
        instrument = candidate.instrument
        plan = size_entry(candidate, quotes[instrument.instrument_id], stops[instrument.instrument_id],
                          working, costs, now, research_profile)
        plans.append(plan)
        if plan.quantity:
            working.pending_entries.append(PendingEntry(
                instrument_id=instrument.instrument_id, issuer_id=instrument.issuer_id,
                sector=instrument.sector, thesis_id="allocation:"+instrument.instrument_id,
                plan_id="allocation:"+instrument.instrument_id, remaining_quantity=plan.quantity,
                entry_price=plan.entry_price, unit_risk=plan.unit_risk,
                reserved_cash=plan.reserved_cash, reserved_risk=plan.total_risk))
    return plans
