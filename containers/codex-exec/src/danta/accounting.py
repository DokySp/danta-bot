"""Strategy-attributed economic accounting; reservations never destroy NAV."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping, Sequence


def money(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not money")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("money must be finite")
    return result


def strategy_nav(cash, managed_quantities: Mapping[str, int], marks: Mapping[str, object],
                 receivables=0, payables=0) -> Decimal:
    """Callers supply only allocated cash/quantities, never the whole account."""
    total = money(cash) + money(receivables) - money(payables)
    for symbol, quantity in managed_quantities.items():
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise ValueError("invalid managed quantity")
        if quantity:
            if symbol not in marks or marks[symbol] is None or money(marks[symbol]) <= 0:
                raise ValueError(f"missing/invalid mark: {symbol}")
            total += quantity * money(marks[symbol])
    return total


@dataclass(frozen=True)
class NavPoint:
    at: datetime
    nav: Decimal
    session_id: str | None = None
    completed: bool = False
    quality: str = "EXACT"

    def __post_init__(self):
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("NAV timestamp must be timezone aware")
        object.__setattr__(self, "nav", money(self.nav))
        if self.nav < 0:
            raise ValueError("negative NAV cannot certify this cash-only strategy")


@dataclass(frozen=True)
class ExternalFlow:
    at: datetime
    amount: Decimal
    before_nav: Decimal | None = None
    after_nav: Decimal | None = None
    kind: str = "CASH_TRANSFER"

    def __post_init__(self):
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("flow timestamp must be timezone aware")
        if self.kind not in {"CASH_TRANSFER", "ASSET_TRANSFER"}:
            raise ValueError("dividends, interest, fees and taxes are internal P&L")
        for name in ("amount", "before_nav", "after_nav"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, money(getattr(self, name)))


def performance(snapshots: Sequence[NavPoint], flows: Sequence[ExternalFlow] = (),
                operating_cost=None, *, unallocated: bool = False) -> dict:
    """Link every observed interval, including nights and weekends.

    A flow needs immediately-before/after valuations. At its timestamp a NAV
    snapshot denotes the after-flow valuation. Missing coverage is never zero.
    Cash already includes actual commissions/taxes; they are not deducted here.
    """
    points = sorted(snapshots, key=lambda point: point.at)
    issues = []
    if len(points) < 2:
        return {"coverage": "INSUFFICIENT_COVERAGE", "twr": None, "pnl": None,
                "max_drawdown": None, "new_risk_allowed": False,
                "issues": ["NEED_INITIAL_AND_FINAL_NAV"]}
    if len({point.at for point in points}) != len(points):
        raise ValueError("duplicate NAV timestamps")
    if any(point.quality != "EXACT" for point in points):
        issues.append("NONEXACT_VALUATION")
    if unallocated:
        issues.append("UNALLOCATED")
    start, end = points[0], points[-1]
    period_flows = sorted((flow for flow in flows if start.at < flow.at <= end.at),
                          key=lambda flow: flow.at)
    if len({flow.at for flow in period_flows}) != len(period_flows):
        raise ValueError("aggregate simultaneous flows with a common before/after NAV")
    net_flow = sum((flow.amount for flow in period_flows), Decimal(0))
    pnl = end.nav - start.nav - net_flow
    events = {point.at: (point.nav, Decimal(0), point.nav) for point in points[1:]}
    for flow in period_flows:
        if flow.before_nav is None or flow.after_nav is None:
            issues.append("FLOW_VALUATION_MISSING")
            continue
        if flow.before_nav + flow.amount != flow.after_nav:
            issues.append("FLOW_VALUATION_MISMATCH")
        if flow.at in events and events[flow.at][0] != flow.after_nav:
            issues.append("FLOW_SNAPSHOT_MISMATCH")
        events[flow.at] = (flow.before_nav, flow.amount, flow.after_nav)
    index = high = Decimal(1)
    max_drawdown = Decimal(0)
    previous = start.nav
    anchor_nav, anchor_index = start.nav, Decimal(1)
    flow_times = {flow.at for flow in period_flows}
    index_points = []
    for at, (before, _, after) in sorted(events.items()):
        if previous <= 0 or before < 0 or after < 0:
            issues.append("NONPOSITIVE_CAPITAL_BASE")
            break
        index = anchor_index * before / anchor_nav
        high = max(high, index)
        drawdown = 1 - index / high
        max_drawdown = max(max_drawdown, drawdown)
        index_points.append({"at": at.astimezone(timezone.utc).isoformat(), "index": str(index),
                             "drawdown": str(drawdown)})
        previous = after
        if at in flow_times:
            anchor_nav, anchor_index = after, index
    cost = None if operating_cost is None else money(operating_cost)
    if cost is not None and cost < 0:
        raise ValueError("negative operating cost")
    exact = not issues
    return {"coverage": "EXACT" if exact else "INSUFFICIENT_COVERAGE",
            "pnl": str(pnl), "net_external_flow": str(net_flow),
            "twr": str(index - 1) if exact else None,
            "max_drawdown": str(max_drawdown) if exact else None,
            "performance_index": index_points if exact else [],
            "operating_cost_status": "UNCONFIRMED" if cost is None else "ALLOCATED",
            "operating_cost": None if cost is None else str(cost),
            "pnl_after_operating_cost": None if cost is None else str(pnl - cost),
            "new_risk_allowed": exact and max_drawdown < Decimal("0.10"),
            "issues": sorted(set(issues))}


def completed_session_returns(snapshots: Sequence[NavPoint], completed_sessions: Sequence[str],
                              count: int = 20, flows: Sequence[ExternalFlow] = ()) -> dict:
    """Calendar determines sessions; queries/weekends cannot manufacture samples."""
    if count <= 0 or len(set(completed_sessions)) != len(completed_sessions):
        raise ValueError("invalid completed-session calendar")
    selected = list(completed_sessions[-count:])
    latest = {}
    for point in sorted(snapshots, key=lambda point: point.at):
        if point.completed and point.session_id in completed_sessions:
            latest[point.session_id] = point
    missing = [session for session in selected if session not in latest]
    returns = {}
    for session in selected:
        position = completed_sessions.index(session)
        previous_session = completed_sessions[position - 1] if position else None
        if session not in latest or previous_session not in latest:
            if session not in missing:
                missing.append(session)
            continue
        result = performance([latest[previous_session], latest[session]], flows)
        if result["twr"] is None:
            missing.append(session)
        else:
            returns[session] = result["twr"]
    return {"sessions": selected, "returns": returns, "missing_sessions": missing,
            "coverage": "EXACT" if not missing and len(selected) == count
            else "INSUFFICIENT_COVERAGE"}


def usage_totals(attempts: Sequence[dict]) -> dict:
    """Input includes cached input; output includes reasoning. Never add subsets."""
    result = {key: None if any(attempt.get(key) is None for attempt in attempts)
              else sum(attempt[key] for attempt in attempts)
              for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")}
    result["total_tokens"] = (result["input_tokens"] + result["output_tokens"]
                              if result["input_tokens"] is not None
                              and result["output_tokens"] is not None else None)
    result["attempt_count"] = len(attempts)
    return result
