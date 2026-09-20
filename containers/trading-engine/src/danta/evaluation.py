"""Independent counterfactual paper accounts and preregistered evaluation.

No broker/network/model calls. A replay consumes a frozen, point-in-time input
manifest. Fixture engine checks cannot certify economic strategy performance.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import random
from typing import Literal

from pydantic import Field, model_validator

from .accounting import ExternalFlow, NavPoint, money, performance, strategy_nav
from .market import SessionCalendar, TickTable
from .models import (AwareTime, Candidate, CostSchedule, EventRecord, Holding,
                     InvestmentThesis, Nonnegative, PendingEntry, PortfolioSnapshot,
                     Positive, Quantity, Quote, Session, StrictModel)
from .reporting import json_default, write_report


ARMS = ("cash", "technical_only", "event_flag_no_ai", "full_strategy")
ARM_RULES = {
    "cash": "same initial capital, zero interest unless a confirmed interest event exists",
    "technical_only": "PRE_AI population; technical/risk/price gates; RS20,ADTV20,ID; no event or AI exits",
    "event_flag_no_ai": "PRE_AI official-event existence and technical gates; RS20,ADTV20,ID; no semantic AI exits",
    "full_strategy": "full event meaning/ranking/invalidation strategy; own cash/quantity/pending",
}


def utc_iso(at):
    return at.astimezone(timezone.utc).isoformat()


def content_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    default=json_default, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


class TimelineEvent(StrictModel):
    at: AwareTime
    kind: Literal["review", "quote", "timer", "session_close", "corporate_action", "cancel_request", "cancel_ack"]
    data: dict


class EvaluationManifest(StrictModel):
    schema_version: str = "1"
    experiment_id: str
    run_id: str
    created_at: AwareTime
    frozen_at: AwareTime
    starts_at: AwareTime
    code_id: str
    config_hash: str
    strategy_hash: str
    prompt_hash: str
    tools_hash: str
    data_hash: str
    universe_hash: str
    sector_classification_source: str
    evidence_status: Literal["FIXTURE_ONLY", "FORWARD_PAPER", "HISTORICAL_REPLAY"]
    precision: Literal["QUOTE_EVENTS", "DAILY_BARS"]
    population_point_in_time: bool
    publication_versions_point_in_time: bool
    research_profile: dict
    model_id: str
    model_effort: str
    initial_capital_krw: Positive = Decimal("10000000")
    costs: CostSchedule
    transmission_delay_ms: Quantity
    cancel_ack_delay_ms: Quantity = 0
    random_seed: int = Field(strict=True)
    operating_cost_krw: Nonnegative | None = None
    confirmation_operating_cost_krw: Nonnegative | None = None
    experiment_attempt_count: int = Field(default=1, ge=1, strict=True)
    selected_best_experiment: bool = False
    calendar_source: str
    calendar_verified: bool
    sessions: list[Session]
    tick_source: str
    tick_verified: bool
    tick_bands: list[list[Nonnegative]]
    timeline: list[TimelineEvent]

    @model_validator(mode="after")
    def frozen_contract(self):
        if self.frozen_at > self.starts_at:
            raise ValueError("experiment must be frozen before observation starts")
        if any(not getattr(self, key) for key in (
            "code_id", "config_hash", "strategy_hash", "prompt_hash", "tools_hash",
            "universe_hash", "sector_classification_source", "model_id", "model_effort")):
            raise ValueError("missing frozen experiment condition")
        if any(event.at < self.starts_at for event in self.timeline):
            raise ValueError("timeline predates frozen experiment start")
        if any(len(band) != 2 for band in self.tick_bands):
            raise ValueError("tick band needs lower bound and tick size")
        if self.costs.synthetic != (self.evidence_status == "FIXTURE_ONLY"):
            raise ValueError("fixture cost schedule cannot certify external-data economics")
        if not self.costs.verified:
            raise ValueError("unknown fees/tax cannot be silently treated as zero")
        expected = content_hash([event.model_dump(mode="json") for event in self.timeline])
        if self.data_hash != expected:
            raise ValueError("input data hash mismatch")
        return self


@dataclass
class PaperOrder:
    order_id: str
    instrument_id: str
    side: str
    quantity: int
    limit_price: Decimal | None
    submitted_at: datetime
    eligible_at: datetime
    expires_at: datetime | None
    thesis: InvestmentThesis
    issuer_id: str
    sector: str
    atr: Decimal
    unit_risk: Decimal
    reasons: list[str] = field(default_factory=list)
    filled: int = 0
    commission_charged: Decimal = Decimal(0)
    gross_filled: Decimal = Decimal(0)
    status: str = "PENDING"
    cancel_requested_at: datetime | None = None
    cancel_ack_at: datetime | None = None

    @property
    def remaining(self):
        return self.quantity - self.filled


class PaperLedger:
    """One arm's isolated cash, positions, reservations and fill journal."""

    def __init__(self, arm: str, initial_capital, costs: CostSchedule,
                 calendar: SessionCalendar, ticks: TickTable, *, slippage_multiplier=1):
        if arm not in ARMS:
            raise ValueError("unknown comparison arm")
        self.arm = arm
        self.initial_capital = self.cash = money(initial_capital)
        self.costs, self.calendar, self.ticks = costs, calendar, ticks
        self.slippage_multiplier = money(slippage_multiplier)
        self.holdings: dict[str, Holding] = {}
        self.theses: dict[str, InvestmentThesis] = {}
        self.orders: dict[str, PaperOrder] = {}
        self.journal: list[dict] = []
        self.closed_trades: list[dict] = []
        self.nav_points: list[NavPoint] = []
        self.external_flows: list[ExternalFlow] = []
        self.quality_issues: set[str] = set()
        self.gates = Counter()
        self.halted: set[str] = set()
        self.seen_quotes: set[str] = set()
        self.trade_stats: dict[str, dict] = {}
        self.paused = False
        from .risk import ConcentrationMonitor, DrawdownCircuit
        self.concentration = ConcentrationMonitor()
        self.drawdown = DrawdownCircuit(high_watermark=Decimal(1))
        self.reductions = {}

    @property
    def nav(self):
        return strategy_nav(self.cash, {key: h.quantity for key, h in self.holdings.items()},
                            {key: h.mark for key, h in self.holdings.items()})

    def _fee(self, side, gross):
        rate = self.costs.buy_commission_rate if side == 'BUY' else self.costs.sell_commission_rate
        minimum = self.costs.minimum_buy_commission if side == 'BUY' else self.costs.minimum_sell_commission
        return max(gross * rate, minimum) if gross else Decimal(0)

    def pending_entries(self):
        return [PendingEntry(instrument_id=o.instrument_id, issuer_id=o.issuer_id,
                             sector=o.sector, thesis_id=o.thesis.thesis_id, plan_id=o.order_id,
                             remaining_quantity=o.remaining, entry_price=o.limit_price,
                             unit_risk=o.unit_risk,
                             reserved_cash=o.remaining * o.limit_price + max(
                                 self._fee('BUY', o.gross_filled + o.remaining * o.limit_price)
                                 - o.commission_charged, Decimal(0)))
                for o in self.orders.values() if o.side == 'BUY' and o.status in {'PENDING', 'PARTIAL', 'CANCEL_REQUESTED'}]

    def snapshot(self, at: datetime) -> PortfolioSnapshot:
        return PortfolioSnapshot(account_alias=self.costs.account_alias, strategy_id=self.arm,
            as_of=at, nav=self.nav, allocated_cash=self.cash, broker_available_cash=self.cash,
            holdings=list(self.holdings.values()), pending_entries=self.pending_entries(),
            complete=not self.quality_issues, ownership_verified=True,
            sector_classification_verified=True, account_state_version=len(self.journal),
            new_risk_paused=self.paused, synthetic=self.costs.synthetic)

    def submit(self, order: PaperOrder):
        if self.arm == 'cash':
            raise ValueError("cash baseline cannot submit trades")
        if (isinstance(order.quantity, bool) or not isinstance(order.quantity, int)
                or order.quantity <= 0 or order.side not in {'BUY', 'SELL'}):
            raise ValueError("invalid paper order")
        if order.order_id in self.orders:
            raise ValueError("duplicate order identity")
        if order.eligible_at < order.submitted_at:
            raise ValueError("execution cannot precede decision plus transmission")
        if order.side == 'BUY':
            if order.limit_price is None or order.limit_price <= 0 or order.expires_at is None:
                raise ValueError("entry requires positive limit and expiry")
            if order.expires_at <= order.eligible_at:
                raise ValueError("entry expires before it can execute")
            reserved = sum((entry.reserved_cash for entry in self.pending_entries()), Decimal(0))
            if reserved + order.quantity * order.limit_price + self._fee('BUY', order.quantity * order.limit_price) > self.cash:
                raise ValueError("paper order exceeds independently allocated cash")
            if order.instrument_id in self.holdings or any(p.instrument_id == order.instrument_id for p in self.pending_entries()):
                raise ValueError("no averaging down or duplicate live entry plan")
        else:
            held = self.holdings.get(order.instrument_id)
            pending = sum(o.remaining for o in self.orders.values() if o.instrument_id == order.instrument_id
                          and o.side == 'SELL' and o.status in {'PENDING', 'PARTIAL', 'CANCEL_REQUESTED'})
            if held is None or order.quantity + pending > held.sellable_quantity:
                raise ValueError("paper sell exceeds strategy-owned sellable quantity")
        self.orders[order.order_id] = order
        self.theses.setdefault(order.thesis.thesis_id, order.thesis)
        self.journal.append({'type': 'SUBMIT', 'order_id': order.order_id,
                             'at': utc_iso(order.submitted_at), 'quantity': order.quantity})

    def request_cancel(self, order_id, at, ack_at=None):
        order = self.orders[order_id]
        if order.status not in {'PENDING', 'PARTIAL', 'CANCEL_REQUESTED'}:
            return
        order.status, order.cancel_requested_at = 'CANCEL_REQUESTED', at
        order.cancel_ack_at = ack_at
        if ack_at is not None and ack_at < at:
            raise ValueError("cancel ack cannot precede request")

    def advance(self, at):
        for order in self.orders.values():
            if order.status not in {'PENDING', 'PARTIAL', 'CANCEL_REQUESTED'}:
                continue
            if order.cancel_ack_at is not None and at >= order.cancel_ack_at:
                order.status = 'CANCELLED'
                self.journal.append({'type': 'CANCELLED', 'order_id': order.order_id, 'at': utc_iso(order.cancel_ack_at)})
            elif order.expires_at is not None and at >= order.expires_at:
                order.status = 'EXPIRED'
                self.journal.append({'type': 'EXPIRED', 'order_id': order.order_id, 'at': utc_iso(order.expires_at)})

    def process_quote(self, quote: Quote, *, ask_quantity: int, bid_quantity: int,
                      event_id: str, tradable=True):
        for quantity in (ask_quantity, bid_quantity):
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
                raise ValueError("executable liquidity requires nonnegative integer quantity")
        if event_id in self.seen_quotes:
            return
        self.seen_quotes.add(event_id)
        self.advance(quote.received_at)
        session = self.calendar.active(quote.observed_at)
        if (not tradable or quote.instrument_id in self.halted or not quote.valid
                or quote.venue != 'KRX' or session is None
                or self.calendar.active(quote.received_at) != session
                or quote.received_at - quote.observed_at > timedelta(seconds=5)):
            return
        if not (self.costs.effective_at <= quote.received_at < self.costs.expires_at):
            self.quality_issues.add('COST_SCHEDULE_EXPIRED')
            return
        if quote.bid is None or quote.ask is None:
            return
        held = self.holdings.get(quote.instrument_id)
        if held:
            held.mark, held.valuation_at = (quote.bid + quote.ask) / 2, quote.observed_at
            stats = self.trade_stats[held.thesis_id]
            stats['max_price'] = max(stats['max_price'], held.mark)
            stats['min_price'] = min(stats['min_price'], held.mark)
        liquidity = {'BUY': ask_quantity, 'SELL': bid_quantity}
        for order in self.orders.values():
            if order.instrument_id != quote.instrument_id or order.status not in {'PENDING', 'PARTIAL', 'CANCEL_REQUESTED'}:
                continue
            if quote.observed_at < order.eligible_at:
                continue
            if order.side == 'BUY':
                raw = quote.ask * (1 + self.costs.buy_slippage_bps * self.slippage_multiplier / 10000)
                tick = self.ticks.tick(raw)
                price = (raw / tick).to_integral_value(rounding=ROUND_CEILING) * tick
                if price > order.limit_price:
                    continue
            else:
                price = self.ticks.floor(quote.bid * (1 - self.costs.sell_slippage_bps * self.slippage_multiplier / 10000))
                if price <= 0:
                    continue
            quantity = min(order.remaining, liquidity[order.side])
            if quantity <= 0:
                continue
            gross = quantity * price
            cumulative_fee = self._fee(order.side, order.gross_filled + gross)
            fee = cumulative_fee - order.commission_charged
            tax = gross * self.costs.sell_tax_rate if order.side == 'SELL' else Decimal(0)
            self._fill(order, quantity, price, fee, tax, quote.observed_at, session.session_id)
            order.gross_filled += gross
            order.commission_charged = cumulative_fee
            order.filled += quantity
            order.status = 'FILLED' if not order.remaining else ('CANCEL_REQUESTED' if order.cancel_requested_at else 'PARTIAL')
            liquidity[order.side] -= quantity

    def _fill(self, order, quantity, price, fee, tax, at, session_id):
        symbol, thesis = order.instrument_id, self.theses[order.thesis.thesis_id]
        gross = quantity * price
        if order.side == 'BUY':
            if gross + fee > self.cash:
                raise ValueError("paper cash invariant violated")
            self.cash -= gross + fee
            if symbol not in self.holdings:
                thesis = thesis.model_copy(update={'first_fill_at': at, 'first_fill_session': session_id})
                self.theses[thesis.thesis_id] = thesis
                self.holdings[symbol] = Holding(instrument_id=symbol, issuer_id=order.issuer_id,
                    sector=order.sector, thesis_id=thesis.thesis_id, quantity=quantity,
                    sellable_quantity=quantity, mark=price, stop=thesis.current_stop,
                    atr=order.atr, average_entry=price, valuation_at=at, first_fill_session=session_id)
                self.trade_stats[thesis.thesis_id] = {'buy_gross': gross, 'buy_fee': fee,
                    'sell_gross': Decimal(0), 'sell_cost': Decimal(0), 'max_price': price,
                    'min_price': price, 'buy_quantity': quantity, 'sold_quantity': 0,
                    'income': Decimal(0), 'reasons': []}
            else:
                holding = self.holdings[symbol]
                holding.average_entry = (holding.average_entry * holding.quantity + gross) / (holding.quantity + quantity)
                holding.quantity += quantity
                holding.sellable_quantity += quantity
                stats = self.trade_stats[thesis.thesis_id]
                stats['buy_gross'] += gross
                stats['buy_fee'] += fee
                stats['buy_quantity'] += quantity
            thesis.average_entry = self.holdings[symbol].average_entry
        else:
            held = self.holdings[symbol]
            if quantity > held.sellable_quantity:
                raise ValueError("oversell")
            self.cash += gross - fee - tax
            held.sellable_quantity -= quantity
            held.quantity -= quantity
            stats = self.trade_stats[thesis.thesis_id]
            stats['sell_gross'] += gross
            stats['sell_cost'] += fee + tax
            stats['sold_quantity'] += quantity
            stats['reasons'] = sorted(set(stats['reasons'] + order.reasons))
            if 'REDUCE_TO_LIMIT' in order.reasons:
                thesis.reduced_quantity += quantity
                held.reduced = True
            if held.quantity == 0:
                thesis.exited_at = at
                thesis.exit_reason = order.reasons[0] if order.reasons else 'EXIT_UNSPECIFIED'
                entry = stats['buy_gross'] / stats['buy_quantity']
                self.closed_trades.append({'thesis_id': thesis.thesis_id, 'instrument_id': symbol,
                    'event_ids': thesis.event_ids, 'first_fill_session': thesis.first_fill_session,
                    'exit_session': session_id, 'holding_sessions': self.calendar.holding_sessions(thesis.first_fill_session, session_id),
                    'pnl': str(stats['sell_gross'] - stats['sell_cost'] - stats['buy_gross'] - stats['buy_fee'] + stats['income']),
                    'mfe': str(stats['max_price'] / entry - 1), 'mae': str(stats['min_price'] / entry - 1),
                    'exit_reasons': stats['reasons'], 'cost': str(stats['buy_fee'] + stats['sell_cost'])})
                del self.holdings[symbol]
        self.journal.append({'type': 'FILL', 'order_id': order.order_id, 'instrument_id': symbol,
            'side': order.side, 'quantity': quantity, 'price': str(price), 'commission': str(fee),
            'tax': str(tax), 'at': utc_iso(at), 'slippage_in_fill_price': True})

    def corporate_action(self, symbol, kind, at, **data):
        held = self.holdings.get(symbol)
        if kind == 'HALT':
            self.halted.add(symbol)
            return
        if kind == 'RESUME':
            self.halted.discard(symbol)
            return
        if held is None:
            return
        if kind == 'DIVIDEND' and data.get('confirmed') is True:
            amount = held.quantity * money(data['net_per_share'])
            if amount < 0:
                raise ValueError('negative dividend')
            self.cash += amount
            self.trade_stats[held.thesis_id]['income'] += amount
        elif kind == 'SPLIT' and data.get('confirmed') is True:
            ratio = money(data['ratio'])
            if ratio <= 0 or held.quantity * ratio != (held.quantity * ratio).to_integral_value():
                self.quality_issues.add('CORPORATE_ACTION_REQUIRES_CASH_IN_LIEU')
                return
            if any(o.instrument_id == symbol and o.status in {'PENDING', 'PARTIAL', 'CANCEL_REQUESTED'} for o in self.orders.values()):
                self.quality_issues.add('CORPORATE_ACTION_PENDING_ORDER_UNRESOLVED')
                return
            held = held.model_copy(update={
                'quantity': int(held.quantity * ratio),
                'sellable_quantity': int(held.sellable_quantity * ratio),
                **{key: getattr(held, key) / ratio for key in ('mark', 'stop', 'atr', 'average_entry')}})
            self.holdings[symbol] = held
            thesis = self.theses[held.thesis_id]
            # Apply the action atomically; assignment validation must not see half-adjusted stops.
            self.theses[held.thesis_id] = thesis.model_copy(update={
                'initial_stop': thesis.initial_stop / ratio, 'current_stop': thesis.current_stop / ratio,
                'initial_r_price': thesis.initial_r_price / ratio, 'average_entry': thesis.average_entry / ratio,
                'planned_quantity': int(thesis.planned_quantity * ratio)})
            stats = self.trade_stats[held.thesis_id]
            stats['max_price'] /= ratio
            stats['min_price'] /= ratio
            stats['buy_quantity'] *= ratio
            stats['sold_quantity'] *= ratio
        else:
            self.quality_issues.add('UNRESOLVED_CORPORATE_ACTION_' + kind)
            if kind in {'DELIST', 'MERGER'}:
                self.halted.add(symbol)
        self.journal.append({'type': 'CORPORATE_ACTION', 'kind': kind, 'instrument_id': symbol,
                             'at': utc_iso(at), 'confirmed': data.get('confirmed') is True})


def paired_bootstrap(full: dict, baseline: dict, sessions: list[str], seed: int,
                     block_sessions=5, resamples=2000) -> dict:
    """Paired noncircular moving blocks, percentile CI of the mean daily difference."""
    missing = [s for s in sessions if full.get(s) is None or baseline.get(s) is None]
    if missing or len(sessions) < block_sessions or len(set(sessions)) != len(sessions):
        return {'status': 'INSUFFICIENT_COVERAGE', 'missing_sessions': missing, 'ci95': None,
                'sessions': sessions, 'seed': seed}
    if block_sessions != 5 or resamples != 2000:
        raise ValueError("research protocol fixes 5-session blocks and 2000 resamples")
    differences = [money(full[s]) - money(baseline[s]) for s in sessions]
    randomizer = random.Random(seed)
    starts = list(range(len(differences) - block_sessions + 1))
    means = []
    for _ in range(resamples):
        sample = []
        while len(sample) < len(differences):
            start = randomizer.choice(starts)
            sample.extend(differences[start:start + block_sessions])
        means.append(sum(sample[:len(differences)], Decimal(0)) / len(differences))
    means.sort()
    def percentile(fraction):
        index = (len(means) - 1) * fraction
        lower = int(index)
        return means[lower] + (means[min(lower + 1, len(means) - 1)] - means[lower]) * (index - lower)
    cumulative = []
    for series in (full, baseline):
        value = Decimal(1)
        for session in sessions:
            value *= 1 + money(series[session])
        cumulative.append(value - 1)
    return {'status': 'COMPUTED', 'statistic': 'mean paired daily TWR difference',
            'mean_difference': str(sum(differences, Decimal(0)) / len(differences)),
            'ci95': [str(percentile(Decimal('0.025'))), str(percentile(Decimal('0.975')))],
            'seed': seed, 'sessions': sessions, 'missing_sessions': [], 'missing_policy': 'invalidate_without_zero_fill',
            'block_sessions': block_sessions, 'resamples': resamples, 'circular': False,
            'cumulative_excess_return': str(cumulative[0] - cumulative[1]),
            'limitation': 'correlated company/event/market observations; not a future prediction interval'}


def _typed(model, value):
    return model.model_validate_json(json.dumps(value, default=json_default))


def _load_manifest(path) -> EvaluationManifest:
    return EvaluationManifest.model_validate_json(Path(path).read_text(encoding='utf-8'))


def validate_candidate_pool(data: dict, input_at: datetime) -> list[Candidate]:
    if data.get('pool_stage') != 'UNIVERSE_PRE_AI':
        raise ValueError('AI_CANDIDATE_CONTAMINATION: comparison pool must precede AI filtering')
    candidates = [_typed(Candidate, item) for item in data['candidates']]
    symbols = [candidate.instrument.instrument_id for candidate in candidates]
    if len(set(symbols)) != len(symbols) or set(symbols) != set(data['pre_ai_candidate_ids']):
        raise ValueError('AI_CANDIDATE_CONTAMINATION: incomplete original candidate pool')
    for candidate in candidates:
        if (candidate.instrument.effective_at > input_at or candidate.features.as_of > input_at
                or candidate.features.window_end > input_at):
            raise ValueError('FUTURE_INPUT_OR_SURVIVOR_UNIVERSE')
    for event in data.get('events', []):
        record = _typed(EventRecord, event)
        if record.available_at > input_at or record.observed_at > input_at:
            raise ValueError('FUTURE_DISCLOSURE_OR_REVISED_VERSION')
    return candidates


def _review(ledger, manifest, data, input_at, now, latest_quotes):
    from .portfolio import size_entry
    from .strategy import assess_entry, reentry_eligibility
    from .models import DailyBar

    if ledger.arm == 'cash':
        return
    candidates = validate_candidate_pool(data, input_at)
    events = [_typed(EventRecord, item) for item in data.get('events', [])]
    decisions = data.get('decisions', {})
    for decision in decisions.values():
        rank = decision.get('rank')
        if rank is not None and (type(rank) is not int or rank <= 0):
            raise ValueError('AI rank must be a positive integer')
    if ledger.arm == 'full_strategy':
        candidates.sort(key=lambda c: (decisions.get(c.instrument.instrument_id, {}).get('rank', 10**9),
                                        -c.features.rs20, -c.features.adtv20, c.instrument.instrument_id))
    else:
        candidates.sort(key=lambda c: (-c.features.rs20, -c.features.adtv20, c.instrument.instrument_id))
    for candidate in candidates:
        symbol = candidate.instrument.instrument_id
        quote = latest_quotes.get(symbol)
        if quote is None:
            ledger.gates['MISSING_EXECUTABLE_QUOTE'] += 1
            continue
        verdict = decisions.get(symbol, {}).get('verdict', 'INSUFFICIENT_DATA')
        if ledger.arm == 'full_strategy':
            ledger.gates['AI_' + verdict] += 1
            if verdict == 'ACCEPT' and any(not decisions[symbol].get(key) for key in
                ('economic_path', 'horizon_case', 'priced_in_case', 'counterevidence', 'invalidation_case')):
                ledger.gates['MODEL_EVIDENCE_INCOMPLETE'] += 1
                continue
        gate = assess_entry(candidate, quote, events, ledger.calendar, ledger.ticks, now,
                            manifest.research_profile, verdict,
                            synthetic=ledger.costs.synthetic,
                            require_event=ledger.arm != 'technical_only',
                            require_ai=ledger.arm == 'full_strategy')
        if not gate.allowed:
            ledger.gates.update(gate.reasons or [gate.reason])
            continue
        previous = [thesis for thesis in ledger.theses.values() if thesis.instrument_id == symbol]
        if previous:
            bars = [_typed(DailyBar, item) for item in data.get('reentry_bars', {}).get(symbol, [])]
            previous_thesis = max(previous, key=lambda thesis: thesis.created_at)
            reentry = reentry_eligibility(previous_thesis, events, bars, ledger.calendar, now,
                                          manifest.research_profile,
                                          require_event=ledger.arm != 'technical_only',
                                          require_ai=ledger.arm == 'full_strategy')
            if not reentry.allowed:
                ledger.gates[reentry.reason] += 1
                continue
        plan = size_entry(candidate, quote, gate.stop_price, ledger.snapshot(now), ledger.costs,
                          now, manifest.research_profile)
        ledger.gates[plan.reason] += 1
        if plan.quantity == 0:
            continue
        identity = f'{ledger.arm}:{symbol}:{now.isoformat()}:{len(ledger.orders)}'
        explanation = decisions.get(symbol, {})
        thesis = InvestmentThesis(thesis_id=identity, instrument_id=symbol,
            event_ids=candidate.event_ids if ledger.arm != 'technical_only' else [],
            source_uris=[event.source_uri for event in events if event.event_id in candidate.event_ids],
            economic_path=explanation.get('economic_path', ARM_RULES[ledger.arm]),
            horizon_case=explanation.get('horizon_case', 'research 3-20 completed sessions'),
            counterevidence=explanation.get('counterevidence', 'deterministic baseline'),
            invalidation_case=explanation.get('invalidation_case', 'deterministic exits'),
            initial_stop=plan.stop_price, current_stop=plan.stop_price,
            initial_r_price=plan.entry_price - plan.stop_price, average_entry=plan.entry_price,
            planned_quantity=plan.quantity, risk_budget=plan.risk_budget,
            strategy_hash=manifest.strategy_hash, policy_hash=manifest.config_hash, created_at=now,
            max_holding_sessions=manifest.research_profile['exits']['max_holding_sessions'])
        ledger.submit(PaperOrder(identity, symbol, 'BUY', plan.quantity, plan.entry_price,
            now, now + timedelta(milliseconds=manifest.transmission_delay_ms), plan.expires_at,
            thesis, candidate.instrument.issuer_id, candidate.instrument.sector,
            candidate.features.atr14, plan.unit_risk))


def _protect(ledger, manifest, quote, now, features=None, invalidating_events=(), reduction_quantity=0,
             *, instrument_id=None, trigger_source='QUOTE'):
    from .risk import evaluate_exit

    symbol = instrument_id if instrument_id is not None else quote.instrument_id if quote is not None else None
    if symbol is None or quote is not None and quote.instrument_id != symbol:
        raise ValueError('protection observation requires a consistent instrument identity')
    holding = ledger.holdings.get(symbol)
    if holding is None:
        return
    thesis = ledger.theses[holding.thesis_id]
    if ledger.arm == 'full_strategy' and invalidating_events:
        thesis.invalidating_event_ids = sorted(set(thesis.invalidating_event_ids)
                                               | {event.event_id for event in invalidating_events})
    pending_sells = any(o.instrument_id == symbol and o.side == 'SELL'
                        and o.status in {'PENDING', 'PARTIAL', 'CANCEL_REQUESTED'} for o in ledger.orders.values())
    plan = evaluate_exit(thesis, holding, quote, ledger.calendar, now,
                          manifest.research_profile, features=features,
                          account_complete=not ledger.quality_issues, orders_known=True,
                          tradable=symbol not in ledger.halted,
                          invalidating_events=list(invalidating_events) if ledger.arm == 'full_strategy' else [],
                          reduction_quantity=reduction_quantity)
    ledger.gates[plan.action] += 1
    ledger.journal.append({'type': 'PROTECTION_OBSERVATION', 'instrument_id': symbol,
                           'at': utc_iso(now), 'action': plan.action, 'reasons': plan.reasons,
                           'trigger_source': trigger_source,
                           'cancel_pending_entries': plan.cancel_pending_entries})
    open_buys = [o for o in ledger.orders.values() if o.instrument_id == symbol
                and o.side == 'BUY' and o.status in {'PENDING', 'PARTIAL', 'CANCEL_REQUESTED'}]
    if plan.cancel_pending_entries and open_buys:
        for order in open_buys:
            if order.cancel_requested_at is None:
                ledger.request_cancel(order.order_id, now,
                                      now + timedelta(milliseconds=manifest.cancel_ack_delay_ms))
        ledger.advance(now)
        if any(order.status == 'CANCEL_REQUESTED' for order in open_buys):
            return
    if plan.quantity == 0 or pending_sells:
        return
    identity = f'{ledger.arm}:exit:{thesis.thesis_id}:{len(ledger.orders)}'
    ledger.submit(PaperOrder(identity, symbol, 'SELL', plan.quantity, None,
        now, now + timedelta(milliseconds=manifest.transmission_delay_ms), None,
        thesis, holding.issuer_id, holding.sector, holding.atr, Decimal(0), reasons=plan.reasons))


def _risk_observation(ledger, manifest, now, latest_quotes, latest_features, completed_bars):
    from .risk import update_trailing_stop

    for symbol, holding in ledger.holdings.items():
        feature = latest_features.get(symbol)
        if feature:
            updated = update_trailing_stop(ledger.theses[holding.thesis_id], feature,
                completed_bars.get(symbol, []), ledger.ticks, manifest.research_profile,
                observed_price=holding.mark)
            ledger.theses[holding.thesis_id] = updated
            holding.stop = updated.current_stop
    if not ledger.quality_issues and ledger.nav > 0:
        circuit = ledger.drawdown.observe(ledger.nav / ledger.initial_capital, manifest.research_profile)
        ledger.paused = circuit['new_risk_paused']
        if circuit['cancel_pending_entries']:
            for order in ledger.orders.values():
                if order.side == 'BUY' and order.status in {'PENDING', 'PARTIAL'}:
                    ledger.request_cancel(order.order_id, now,
                        now + timedelta(milliseconds=manifest.cancel_ack_delay_ms))
        for reduction in ledger.concentration.observe(ledger.snapshot(now), now, manifest.research_profile):
            ledger.reductions[reduction.instrument_id] = reduction
        if ledger.concentration.cancel_pending_entries:
            for order in ledger.orders.values():
                if order.side == 'BUY' and order.status in {'PENDING', 'PARTIAL'}:
                    ledger.request_cancel(order.order_id, now,
                        now + timedelta(milliseconds=manifest.cancel_ack_delay_ms))
    ledger.advance(now)
    for symbol, reduction in list(ledger.reductions.items()):
        held = ledger.holdings.get(symbol)
        if held is None or held.quantity <= reduction.target_quantity:
            del ledger.reductions[symbol]
        elif not ledger.pending_entries() and symbol in latest_quotes:
            _protect(ledger, manifest, latest_quotes[symbol], now, latest_features.get(symbol),
                     reduction_quantity=max(0, held.quantity - reduction.target_quantity))
    if ledger.concentration.active_plan and not ledger.reductions and not ledger.pending_entries():
        ledger.concentration.completed()


def _run(manifest: EvaluationManifest, slippage_multiplier=1) -> dict:
    synthetic = manifest.evidence_status == 'FIXTURE_ONLY'
    calendar = SessionCalendar(manifest.sessions, provenance=manifest.calendar_source,
                               verified=manifest.calendar_verified, synthetic=synthetic)
    ticks = TickTable([tuple(band) for band in manifest.tick_bands], provenance=manifest.tick_source,
                      verified=manifest.tick_verified, synthetic=synthetic,
                      effective_at=manifest.costs.effective_at, expires_at=manifest.costs.expires_at)
    ledgers = {arm: PaperLedger(arm, manifest.initial_capital_krw, manifest.costs,
                               calendar, ticks, slippage_multiplier=slippage_multiplier) for arm in ARMS}
    for ledger in ledgers.values():
        ledger.nav_points.append(NavPoint(manifest.starts_at, ledger.nav))
        if not manifest.population_point_in_time:
            ledger.quality_issues.add('SURVIVOR_UNIVERSE')
        if not manifest.publication_versions_point_in_time:
            ledger.quality_issues.add('REVISED_OR_FUTURE_DISCLOSURES')
        if manifest.precision == 'DAILY_BARS':
            ledger.quality_issues.add('DAILY_BAR_INTRADAY_ORDER_UNKNOWN_NO_PRECISE_CERTIFICATION')
    latest_quotes, latest_features, completed_bars = {}, {}, {}
    scheduled = []
    for event in manifest.timeline:
        if event.kind == 'review':
            validate_candidate_pool(event.data, event.at)
            completed_at = datetime.fromisoformat(event.data['completed_at'].replace('Z', '+00:00'))
            if completed_at.tzinfo is None or completed_at < event.at:
                raise ValueError('invalid review completion time')
            scheduled.append((event.at, 1, 'review_input', event))
            scheduled.append((completed_at, 2, 'review_full', event))
        else:
            scheduled.append((event.at, 0, event.kind, event))
    # Calendar timers introduce no price facts and never extend the observed horizon.
    observation_end = max((item[0] for item in scheduled), default=manifest.starts_at)
    for session in calendar.sessions:
        for at, source in ((session.closes_at - timedelta(minutes=10), 'SESSION_DEADLINE'),
                           (session.closes_at, 'SESSION_CLOSE')):
            if manifest.starts_at <= at <= observation_end:
                timer = TimelineEvent(at=at, kind='timer', data={'source': source, 'session_id': session.session_id})
                scheduled.append((at, 3, 'timer', timer))
    for now, _, kind, event in sorted(scheduled, key=lambda item: (item[0], item[1])):
        for ledger in ledgers.values():
            ledger.advance(now)
        data = event.data
        if kind == 'review_input':
            from .models import DailyBar
            if data.get('strategy_hash', manifest.strategy_hash) != manifest.strategy_hash:
                raise ValueError('POLICY_CHANGED_DURING_FIXED_EXPERIMENT')
            if data.get('model_id', manifest.model_id) != manifest.model_id:
                raise ValueError('MODEL_CHANGED_DURING_FIXED_EXPERIMENT')
            for item in data.get('quotes', []):
                quote = _typed(Quote, item)
                if quote.received_at > now or quote.observed_at > now:
                    raise ValueError('FUTURE_INPUT_QUOTE')
                latest_quotes[quote.instrument_id] = quote
            for candidate in validate_candidate_pool(data, now):
                latest_features[candidate.instrument.instrument_id] = candidate.features
            for symbol, rows in data.get('completed_bars', {}).items():
                bars = [_typed(DailyBar, row) for row in rows]
                if any(bar.available_at > now or bar.closes_at > now or not bar.complete for bar in bars):
                    raise ValueError('FUTURE_OR_INCOMPLETE_TRAILING_BAR')
                completed_bars[symbol] = bars
            for arm in ('technical_only', 'event_flag_no_ai'):
                _review(ledgers[arm], manifest, data, event.at, now, latest_quotes)
        elif kind == 'review_full':
            _review(ledgers['full_strategy'], manifest, data, event.at, now, latest_quotes)
            records = [_typed(EventRecord, item) for item in data.get('events', [])]
            for symbol, decision in data.get('decisions', {}).items():
                ids = decision.get('invalidating_event_ids', [])
                if ids and symbol in latest_quotes:
                    _protect(ledgers['full_strategy'], manifest, latest_quotes[symbol], now,
                             latest_features.get(symbol), [record for record in records if record.event_id in ids])
        elif kind == 'quote':
            quote = _typed(Quote, data['quote'])
            if quote.received_at != now:
                raise ValueError('quote event must be ordered by actual reception timestamp')
            latest_quotes[quote.instrument_id] = quote
            for ledger in ledgers.values():
                ledger.process_quote(quote, ask_quantity=data['ask_quantity'], bid_quantity=data['bid_quantity'],
                                     event_id=data['event_id'], tradable=data.get('tradable', True))
                _risk_observation(ledger, manifest, now, latest_quotes, latest_features, completed_bars)
                _protect(ledger, manifest, quote, now, latest_features.get(quote.instrument_id))
        elif kind == 'timer':
            for ledger in ledgers.values():
                for symbol in list(ledger.holdings):
                    _protect(ledger, manifest, latest_quotes.get(symbol), now,
                             latest_features.get(symbol), instrument_id=symbol,
                             trigger_source=data.get('source', 'EXPLICIT_TIMER'))
        elif kind == 'corporate_action':
            for ledger in ledgers.values():
                ledger.corporate_action(data['instrument_id'], data['action'], now,
                                        **{k: v for k, v in data.items() if k not in {'instrument_id', 'action'}})
        elif kind in {'cancel_request', 'cancel_ack'}:
            ledger = ledgers[data['arm']]
            if kind == 'cancel_request':
                ledger.request_cancel(data['order_id'], now)
            else:
                ledger.orders[data['order_id']].cancel_ack_at = now
                ledger.advance(now)
        elif kind == 'session_close':
            session = calendar.session(data['session_id'])
            if now < session.closes_at:
                raise ValueError('incomplete session cannot be an economic sample')
            for ledger in ledgers.values():
                for symbol, holding in ledger.holdings.items():
                    mark = data.get('marks', {}).get(symbol)
                    if mark is None or money(mark) <= 0 or data.get('marks_verified') is not True:
                        ledger.quality_issues.add('MISSING_SESSION_MARK:' + session.session_id)
                    else:
                        holding.mark, holding.valuation_at = money(mark), session.closes_at
                ledger.nav_points.append(NavPoint(now, ledger.nav, session.session_id, True,
                    'EXACT' if not ledger.quality_issues else 'INSUFFICIENT_COVERAGE'))
                if performance(ledger.nav_points, ledger.external_flows)['new_risk_allowed'] is False:
                    ledger.paused = True
                    for order in ledger.orders.values():
                        if order.side == 'BUY' and order.status in {'PENDING', 'PARTIAL'}:
                            ledger.request_cancel(order.order_id, now,
                                now + timedelta(milliseconds=manifest.cancel_ack_delay_ms))
    arms = {}
    for arm, ledger in ledgers.items():
        result = performance(ledger.nav_points, ledger.external_flows,
                             manifest.operating_cost_krw if arm == 'full_strategy' else Decimal(0))
        daily = {}
        for previous, current in zip(ledger.nav_points, ledger.nav_points[1:]):
            if current.session_id in daily:
                raise ValueError('duplicate completed session NAV')
            daily[current.session_id] = performance([previous, current], ledger.external_flows)['twr']
        journal_counts = Counter(item['type'] for item in ledger.journal)
        fills = [item for item in ledger.journal if item['type'] == 'FILL']
        result.update({'daily_returns': daily, 'cash_krw': str(ledger.cash), 'nav_krw': str(ledger.nav),
            'cash_fraction': str(ledger.cash / ledger.nav) if ledger.nav else None,
            'positions': {key: holding.model_dump(mode='json') for key, holding in ledger.holdings.items()},
            'pending': [item.model_dump(mode='json') for item in ledger.pending_entries()],
            'order_statuses': dict(Counter(order.status for order in ledger.orders.values())),
            'orders': {key: {'quantity': order.quantity, 'filled': order.filled, 'status': order.status,
                             'eligible_at': utc_iso(order.eligible_at),
                             'expires_at': utc_iso(order.expires_at) if order.expires_at else None}
                       for key, order in ledger.orders.items()},
            'closed_theses': len({trade['thesis_id'] for trade in ledger.closed_trades}),
            'closed_trades': ledger.closed_trades, 'gate_counts': dict(ledger.gates),
            'exit_reason_counts': dict(Counter(reason for trade in ledger.closed_trades for reason in trade['exit_reasons'])),
            'execution_conversion': {'planned_orders': len(ledger.orders),
                                     'submitted_orders': journal_counts['SUBMIT'],
                                     'orders_with_fills': sum(order.filled > 0 for order in ledger.orders.values()),
                                     'partially_filled_orders': sum(0 < order.filled < order.quantity for order in ledger.orders.values()),
                                     'expired_orders': sum(order.status == 'EXPIRED' for order in ledger.orders.values())},
            'realized_pnl_krw': str(sum((stats['sell_gross'] - stats['sell_cost'] + stats['income']
                - (stats['buy_gross'] + stats['buy_fee']) * stats['sold_quantity'] / stats['buy_quantity']
                for stats in ledger.trade_stats.values()), Decimal(0))),
            'unrealized_pnl_krw': str(sum((h.quantity * h.mark
                - (ledger.trade_stats[h.thesis_id]['buy_gross'] + ledger.trade_stats[h.thesis_id]['buy_fee'])
                * h.quantity / ledger.trade_stats[h.thesis_id]['buy_quantity']
                for h in ledger.holdings.values()), Decimal(0))),
            'concentration_state': ledger.concentration.state(),
            'new_risk_paused': ledger.paused,
            'journal_counts': dict(journal_counts), 'journal': ledger.journal,
            'transaction_cost_krw': str(sum((money(fill['commission']) + money(fill['tax']) for fill in fills), Decimal(0))),
            'slippage_accounting': 'embedded in fill prices; never deducted again',
            'issues': sorted(set(result['issues']) | ledger.quality_issues |
                             ({'MONITOR_DEGRADED_OBSERVATIONS'} if ledger.gates['MONITOR_DEGRADED'] else set()))})
        arms[arm] = result
    return {'schema_version': manifest.schema_version, 'run_id': manifest.run_id,
        'created_at': datetime.now(timezone.utc).isoformat(), 'config_hash': manifest.config_hash,
        'strategy_hash': manifest.strategy_hash, 'code_id': manifest.code_id,
        'experiment_id': manifest.experiment_id, 'manifest_hash': content_hash(manifest.model_dump(mode='json')),
        'evidence_status': manifest.evidence_status, 'strategy_status': 'STRATEGY_UNPROVEN',
        'engine_status': 'ENGINE_EXECUTED', 'automatic_live_promotion': False,
        'arm_rules': ARM_RULES, 'slippage_multiplier': str(slippage_multiplier), 'arms': arms,
        'sessions': [s.session_id for s in calendar.sessions
                     if manifest.starts_at <= s.closes_at <= max([event.at for event in manifest.timeline], default=manifest.starts_at)],
        'limitations': ['Cash baseline assumes zero interest unless confirmed.',
                         'Current-model historical replay may contain model-training hindsight.',
                         'Fixture observations do not establish real economic performance.']}


def replay_manifest(path) -> dict:
    return _run(_load_manifest(path))


def evaluate_metrics(manifest: EvaluationManifest, replay: dict, stress: dict) -> dict:
    """Fixed discovery, then the immediately following unchanged confirmation window."""
    sessions = replay['sessions']
    discovery, confirmation = sessions[:60], sessions[60:80]
    full, technical = replay['arms']['full_strategy'], replay['arms']['technical_only']
    full_returns, baseline_returns = full['daily_returns'], technical['daily_returns']
    closed = {trade['thesis_id'] for trade in full['closed_trades'] if trade['exit_session'] in discovery}
    reasons, failures = [], []
    bootstrap = paired_bootstrap(full_returns, baseline_returns, discovery, manifest.random_seed)
    if len(discovery) < 60 or len(closed) < 30:
        reasons.append('MINIMUM_60_COMPLETED_SESSIONS_AND_30_CLOSED_THESES_NOT_MET')
    if bootstrap['status'] != 'COMPUTED':
        reasons.append('PAIRED_RETURN_COVERAGE_INCOMPLETE')
    def compounded(values, selected):
        if not selected or any(values.get(session) is None for session in selected):
            return None
        product = Decimal(1)
        for session in selected:
            product *= 1 + money(values[session])
        return product - 1
    gain = compounded(full_returns, discovery)
    baseline = compounded(baseline_returns, discovery)
    pnl = None if gain is None else manifest.initial_capital_krw * gain
    if gain is not None and (gain <= 0 or baseline is not None and gain <= baseline):
        failures.append('NONPOSITIVE_NET_PNL_OR_PRIMARY_BASELINE_EXCESS')
    if manifest.operating_cost_krw is None:
        reasons.append('OPERATING_COST_ALLOCATION_UNCONFIRMED')
    elif pnl is not None and pnl - manifest.operating_cost_krw <= 0:
        failures.append('NONPOSITIVE_PNL_AFTER_OPERATING_COST')
    if bootstrap['status'] == 'COMPUTED' and money(bootstrap['ci95'][0]) <= 0:
        failures.append('BOOTSTRAP_LOWER_BOUND_NOT_POSITIVE')
    if full.get('max_drawdown') is None or full['issues'] or technical['issues']:
        reasons.append('DATA_OR_OPERATIONAL_COVERAGE_INCOMPLETE')
    elif money(full['max_drawdown']) >= Decimal('0.10'):
        failures.append('RESEARCH_DRAWDOWN_BOUNDARY_REACHED')
    stress_gain = compounded(stress['arms']['full_strategy']['daily_returns'], discovery)
    stress_baseline = compounded(stress['arms']['technical_only']['daily_returns'], discovery)
    if stress_gain is None or stress_baseline is None:
        reasons.append('STRESS_COVERAGE_INCOMPLETE')
    elif stress_gain <= 0 or stress_gain <= stress_baseline:
        reasons.append('DOUBLE_SLIPPAGE_STRESS_NOT_POSITIVE_OR_NOT_ABOVE_BASELINE')
    if manifest.selected_best_experiment:
        reasons.append('BEST_OF_MULTIPLE_EXPERIMENTS_SELECTION_BIAS')
    confirm_gain = compounded(full_returns, confirmation)
    confirm_baseline = compounded(baseline_returns, confirmation)
    if len(confirmation) < 20:
        reasons.append('SEPARATE_20_SESSION_CONFIRMATION_PENDING')
    elif confirm_gain is None or confirm_baseline is None:
        reasons.append('CONFIRMATION_COVERAGE_INCOMPLETE')
    elif confirm_gain <= 0 or confirm_gain <= confirm_baseline:
        failures.append('CONFIRMATION_NET_PNL_OR_BASELINE_EXCESS_NOT_POSITIVE')
    sample_sufficient = len(discovery) == 60 and len(closed) >= 30 and bootstrap['status'] == 'COMPUTED'
    criteria_verdict = ('INCONCLUSIVE' if not sample_sufficient else
                        'FAIL' if failures else 'INCONCLUSIVE' if reasons else 'PASS_REVIEW_ELIGIBLE')
    verdict = criteria_verdict
    if manifest.evidence_status != 'FORWARD_PAPER':
        reasons.append('FORWARD_PAPER_EVIDENCE_REQUIRED')
        verdict = 'FAIL' if sample_sufficient and failures else 'INCONCLUSIVE'
    return {**{key: replay[key] for key in ('schema_version', 'run_id', 'created_at', 'config_hash',
                                           'strategy_hash', 'code_id', 'experiment_id', 'manifest_hash', 'evidence_status')},
        'engine_status': replay['engine_status'], 'strategy_status': 'STRATEGY_UNPROVEN',
        'verdict': verdict, 'criteria_verdict': criteria_verdict,
        'automatic_live_promotion': False, 'live_review_eligible': verdict == 'PASS_REVIEW_ELIGIBLE',
        'discovery_sessions': discovery, 'closed_discovery_theses': len(closed),
        'confirmation_sessions': confirmation, 'discovery_net_pnl_krw': None if pnl is None else str(pnl),
        'discovery_return': None if gain is None else str(gain),
        'primary_baseline_return': None if baseline is None else str(baseline),
        'confirmation_return': None if confirm_gain is None else str(confirm_gain),
        'confirmation_primary_baseline_return': None if confirm_baseline is None else str(confirm_baseline),
        'bootstrap': bootstrap, 'failures': failures, 'hold_reasons': reasons,
        'experiment_attempt_count': manifest.experiment_attempt_count,
        'selected_best_experiment': manifest.selected_best_experiment,
        'double_slippage_stress': {'full_return': None if stress_gain is None else str(stress_gain),
                                  'technical_return': None if stress_baseline is None else str(stress_baseline),
                                  'fees_and_taxes_unchanged': True},
        'replay': replay, 'stress_replay': stress}


def evaluate_manifest(path, *, output_dir=None) -> dict:
    """Evaluate frozen inputs; optionally export once under evaluations/<manifest hash>."""
    manifest = _load_manifest(path)
    result = evaluate_metrics(manifest, _run(manifest), _run(manifest, 2))
    result['evaluation_id'] = result['manifest_hash']
    result['manifest'] = manifest.model_dump(mode='json')
    if output_dir is not None:
        # Manifest identifiers are untrusted labels, never filesystem components.
        directory = Path(output_dir) / result['evaluation_id']
        directory.mkdir(parents=True, exist_ok=False)
        result['artifacts'] = {'json': str(directory / 'result.json'),
                               'html': str(directory / 'report.html')}
        write_report(result, result['artifacts']['json'], result['artifacts']['html'],
                     '독립 paper 전략 평가')
    return result
