"""Strict domain contracts for the README research strategy; no live authority."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def finite_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise ValueError("expected a finite decimal")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("nonfinite money/ratio")
    return number


def aware_datetime(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value


Number = Annotated[Decimal, BeforeValidator(finite_decimal)]
Positive = Annotated[Number, Field(gt=0)]
Nonnegative = Annotated[Number, Field(ge=0)]
Ratio = Annotated[Number, Field(ge=0, le=1)]
Quantity = Annotated[int, Field(strict=True, ge=0)]
AwareTime = Annotated[datetime, BeforeValidator(aware_datetime)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, strict=True)


class Session(StrictModel):
    session_id: str
    ordinal: Annotated[int, Field(strict=True)]
    opens_at: AwareTime
    closes_at: AwareTime
    venue: Literal["KRX"] = "KRX"

    @model_validator(mode="after")
    def valid_window(self) -> Session:
        if self.opens_at >= self.closes_at:
            raise ValueError("session close must follow open")
        return self


class Instrument(StrictModel):
    instrument_id: str
    issuer_id: str
    board: Literal["KOSPI", "KOSDAQ"]
    kind: str = "common_stock"
    venue: str = "KRX"
    sector: str | None
    status: Literal["NORMAL", "HALTED", "ADMINISTRATIVE", "DELISTING", "UNKNOWN"]
    status_verified: bool
    classification_source: str
    effective_at: AwareTime


class DailyBar(StrictModel):
    session_id: str
    opens_at: AwareTime
    closes_at: AwareTime
    available_at: AwareTime
    high: Positive
    low: Positive
    close: Positive
    turnover: Nonnegative
    complete: bool
    adjustment_basis: str
    price_return_basis: Literal["price_only"] = "price_only"
    ohlc_consistently_adjusted: bool
    source: str

    @model_validator(mode="after")
    def valid_prices(self) -> DailyBar:
        if self.low > self.close or self.high < self.close or self.low > self.high:
            raise ValueError("invalid OHLC")
        if self.opens_at >= self.closes_at:
            raise ValueError("invalid bar interval")
        if self.complete and self.available_at < self.closes_at:
            raise ValueError("complete bar cannot be available before close")
        return self


class Quote(StrictModel):
    instrument_id: str
    venue: str
    observed_at: AwareTime
    received_at: AwareTime
    bid: Positive | None
    ask: Positive | None
    last: Positive | None = None
    last_observed_at: AwareTime | None = None
    valid: bool = True
    source: str

    @model_validator(mode="after")
    def valid_quote(self) -> Quote:
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("crossed quote")
        if self.observed_at > self.received_at:
            raise ValueError("quote observation follows reception")
        if self.last is not None and self.last_observed_at is None:
            raise ValueError("last trade requires actual observation time")
        if self.last_observed_at is not None and self.last_observed_at > self.received_at:
            raise ValueError("trade observation follows reception")
        return self


class MarketFact(StrictModel):
    fact_id: str
    instrument_id: str
    value: str
    unit: str
    source: str
    content_hash: str
    published_at: AwareTime | None
    observed_at: AwareTime
    available_at: AwareTime
    quality: Literal["VERIFIED", "PARTIAL", "UNCERTAIN"]


class EventRecord(StrictModel):
    event_id: str
    instrument_id: str
    official_id: str
    normalized_key: str
    family: str
    source_uri: str
    source_hash: str
    fact_ids: list[str]
    facts: dict[str, str]
    comparison_basis: str
    available_at: AwareTime
    observed_at: AwareTime
    published_at: AwareTime | None = None
    official: bool
    primary_source_complete: bool
    timing_quality: Literal["EXACT", "FIRST_COLLECTED", "UNCERTAIN"]
    polarity: Literal["POSITIVE", "NEGATIVE", "UNKNOWN"]
    correction_of: str | None = None
    resolves_event_ids: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    withdrawn: bool = False
    interpretation: str | None = None


class FeatureSnapshot(StrictModel):
    instrument_id: str
    board: Literal["KOSPI", "KOSDAQ"]
    as_of: AwareTime
    window_start: AwareTime
    window_end: AwareTime
    last_session_id: str
    bars: Quantity
    bar_interval: Literal["regular_session"] = "regular_session"
    completed: Literal[True] = True
    formula_id: str = "sma-arithmetic_atr-simple_rs20-price_v1"
    adjustment_basis: str
    index_adjustment_basis: str
    price_return_basis: Literal["price_only"] = "price_only"
    close: Positive
    sma20: Positive
    sma20_five_sessions_ago: Positive
    sma60: Positive
    return20: Number
    index_return20: Number
    rs20: Number
    atr14: Nonnegative
    adtv20: Nonnegative
    low5: Positive
    index_close: Positive
    index_sma60: Positive
    last_two_closes: tuple[Positive, Positive]
    last_two_sma20: tuple[Positive, Positive]


class Candidate(StrictModel):
    instrument: Instrument
    features: FeatureSnapshot
    event_ids: list[str]
    coverage: Literal["COMPLETE", "COMPLETE_NO_EVENT", "FETCH_FAILED", "PARTIAL"]
    counterevidence_event_ids: list[str] = Field(default_factory=list)
    priority: Annotated[int, Field(strict=True, gt=0)] = 1


class CostSchedule(StrictModel):
    """Order-level rates and minimum fees. Explicit zero is allowed; unknown is not."""
    source: str
    account_alias: str
    venue: str
    effective_at: AwareTime
    expires_at: AwareTime
    verified: bool
    synthetic: bool
    buy_commission_rate: Ratio
    sell_commission_rate: Ratio
    sell_tax_rate: Ratio
    minimum_buy_commission: Nonnegative
    minimum_sell_commission: Nonnegative
    buy_slippage_bps: Nonnegative
    sell_slippage_bps: Nonnegative

    @model_validator(mode="after")
    def valid_period(self) -> CostSchedule:
        if self.effective_at >= self.expires_at:
            raise ValueError("invalid cost validity interval")
        return self


class Holding(StrictModel):
    instrument_id: str
    issuer_id: str
    sector: str
    thesis_id: str
    quantity: Quantity
    sellable_quantity: Quantity
    mark: Positive
    stop: Positive
    atr: Nonnegative
    average_entry: Positive
    valuation_at: AwareTime
    price_observed_at: AwareTime | None = None
    valuation_quality: Literal["EXACT", "STALE", "MISSING"] = "EXACT"
    first_fill_session: str
    source_verified: bool = True
    reduced: bool = False

    @model_validator(mode="after")
    def valid_quantity(self) -> Holding:
        if self.sellable_quantity > self.quantity:
            raise ValueError("sellable exceeds strategy ownership")
        return self


class PendingEntry(StrictModel):
    instrument_id: str
    issuer_id: str
    sector: str
    thesis_id: str
    plan_id: str
    remaining_quantity: Quantity
    entry_price: Positive
    unit_risk: Positive
    reserved_cash: Nonnegative
    reserved_risk: Nonnegative | None = None


class PortfolioSnapshot(StrictModel):
    account_alias: str
    strategy_id: str
    as_of: AwareTime
    nav: Positive
    allocated_cash: Nonnegative
    broker_available_cash: Nonnegative
    holdings: list[Holding]
    pending_entries: list[PendingEntry]
    complete: bool
    ownership_verified: bool
    sector_classification_verified: bool
    account_state_version: Quantity
    new_risk_paused: bool = False
    monitor_degraded: bool = False
    synthetic: bool = False


class InvestmentThesis(StrictModel):
    thesis_id: str
    instrument_id: str
    event_ids: list[str]
    source_uris: list[str]
    economic_path: str
    horizon_case: str
    counterevidence: str
    invalidation_case: str
    initial_stop: Positive
    current_stop: Positive
    initial_r_price: Positive
    average_entry: Positive
    planned_quantity: Quantity
    risk_budget: Positive
    strategy_hash: str
    policy_hash: str
    created_at: AwareTime
    first_fill_at: AwareTime | None = None
    first_fill_session: str | None = None
    first_fill_time_quality: Literal["EXACT", "FIRST_OBSERVED", "UNKNOWN"] = "EXACT"
    max_holding_sessions: Annotated[int, Field(strict=True, gt=0)] = 20
    trend_exit_consecutive_closes: Annotated[int, Field(strict=True, gt=0)] = 2
    mfe_price: Positive | None = None
    reduced_quantity: Quantity = 0
    exit_reason: str | None = None
    exited_at: AwareTime | None = None
    invalidating_event_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def fixed_protection(self) -> InvestmentThesis:
        if self.current_stop < self.initial_stop:
            raise ValueError("stop cannot move below initial protection")
        if (self.first_fill_at is None) != (self.first_fill_session is None):
            raise ValueError("first fill timestamp/session must both exist")
        return self


class GateResult(StrictModel):
    allowed: bool
    reason: str
    reasons: list[str] = Field(default_factory=list)
    entry_price: Positive | None = None
    stop_price: Positive | None = None


class EntryPlan(StrictModel):
    instrument_id: str
    quantity: Quantity
    reason: str
    entry_price: Positive
    stop_price: Positive
    risk_budget: Nonnegative
    unit_risk: Positive | None
    total_risk: Nonnegative
    reserved_cash: Nonnegative
    expected_roundtrip_friction: Nonnegative
    target_weight: Nonnegative
    q_risk: Quantity
    expires_at: AwareTime


class ExitPlan(StrictModel):
    instrument_id: str
    action: str
    quantity: Quantity
    reasons: list[str]
    cancel_pending_entries: bool
    trigger_kind: str | None = None
    trigger_observed_at: AwareTime | None = None


class Reduction(StrictModel):
    instrument_id: str
    thesis_id: str
    quantity: Quantity
    target_quantity: Quantity
    reasons: list[str]
    frozen_nav: Positive
    cancel_pending_entries: bool = True
