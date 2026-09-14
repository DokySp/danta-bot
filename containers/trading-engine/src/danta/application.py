"""One workflow for frozen snapshots, reviews, protection and reconciliation."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .accounting import ExternalFlow, NavPoint, performance, strategy_nav
from .config import Config, HumanRequired, ROOT, aware_time, canonical, digest, utcnow
from .decision import DecisionProposal, freeze_input, unreviewed_positions, validate_proposal
from .execution import Executor, FixtureBroker, OrderIntent
from .market import EventRegistry, SessionCalendar, TickTable, calculate_features
from .models import Candidate, CostSchedule, DailyBar, EventRecord, Holding, Instrument, InvestmentThesis, MarketFact, PendingEntry, PortfolioSnapshot, Quote, Session
from .portfolio import buy_commission, size_entry
from .reporting import write_report
from .risk import ConcentrationMonitor, DrawdownCircuit, evaluate_exit, update_trailing_stop
from .store import Store
from .strategy import assess_entry, quote_fresh, rank_candidates, reentry_eligibility


def code_identity() -> str:
    files = sorted(path for folder in ("src", "schemas", "migrations", "prompts") for path in (ROOT / folder).rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    hasher = hashlib.sha256()
    for path in files:
        hasher.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes() + b"\0")
    return hasher.hexdigest()


class MarketBundle:
    """Typed ingestion boundary shared by recorded/fixture and external adapters."""
    def __init__(self, data: dict, profile: dict, *, mode: str):
        self.data, self.profile = data, profile
        self.synthetic = data.get("provenance") == "FIXTURE_ONLY"
        if mode == "offline" and not (self.synthetic or data.get("provenance") == "RECORDED_SNAPSHOT"):
            raise ValueError("OFFLINE_REQUIRES_RECORDED_OR_SYNTHETIC_INPUT")
        if mode in {"live", "broker_demo", "shadow"} and self.synthetic:
            raise ValueError("SYNTHETIC_INPUT_CANNOT_DRIVE_EXTERNAL_ACCOUNT")
        self.now = aware_time(data["as_of"])
        self.sessions = [Session.model_validate_json(canonical(value)) for value in data["sessions"]]
        self.calendar = SessionCalendar(self.sessions, provenance=data["calendar_source"], verified=data["calendar_verified"], synthetic=self.synthetic)
        self.ticks = TickTable([(Decimal(lower), Decimal(step)) for lower, step in data["tick_bands"]],
            provenance=data["tick_source"], verified=data["ticks_verified"], synthetic=self.synthetic,
            effective_at=aware_time(data["tick_effective_at"]), expires_at=aware_time(data["tick_expires_at"]))
        self.costs = CostSchedule.model_validate_json(canonical(data["costs"]))
        self.instruments = {value["instrument_id"]: Instrument.model_validate_json(canonical(value)) for value in data["instruments"]}
        self.quotes = {value["instrument_id"]: Quote.model_validate_json(canonical(value)) for value in data["quotes"]}
        self.bars = {key: [DailyBar.model_validate_json(canonical(value)) for value in values] for key, values in data["bars"].items()}
        self.index_bars = {key: [DailyBar.model_validate_json(canonical(value)) for value in values] for key, values in data["index_bars"].items()}
        registry = EventRegistry()
        for value in data["events"]:
            registry.ingest(EventRecord.model_validate_json(canonical(value)))
        self.events = list(registry.records.values())
        self.facts = [MarketFact.model_validate_json(canonical(value)) for value in data["facts"]]
        self.features = {}
        self.candidates, self.exclusions = [], []
        for instrument_id, instrument in self.instruments.items():
            try:
                features = calculate_features(self.bars[instrument_id], self.index_bars[instrument.board],
                    instrument_id=instrument_id, board=instrument.board, as_of=self.now, research_profile=profile)
                self.features[instrument_id] = features
                candidate = Candidate(instrument=instrument, features=features,
                    event_ids=[event.event_id for event in self.events if event.instrument_id == instrument_id and event.polarity != "NEGATIVE"],
                    counterevidence_event_ids=[event.event_id for event in self.events if event.instrument_id == instrument_id and event.polarity == "NEGATIVE"],
                    coverage=data["coverage"].get(instrument_id, "FETCH_FAILED"))
                self.candidates.append(candidate)
            except (ValueError, KeyError) as error:
                self.exclusions.append({"instrument_id": instrument_id, "reason": str(error)})
        self.total_universe = len(self.instruments)

    @property
    def facts_hash(self) -> str:
        return digest([self.events, self.facts])


def fixture_decision(frozen: dict) -> dict:
    """Recorded synthetic reviewer contract. Never presented as an actual LLM call."""
    return {"schema_version": 1, "run_id": frozen["run_id"], "input_snapshot_id": frozen["input_snapshot_id"],
        "account_state_version": frozen["portfolio"]["account_state_version"], "review_scope": frozen["review_scope"],
        "candidate_reviews": [{"instrument_id": candidate["instrument"]["instrument_id"], "verdict": "ACCEPT" if candidate["event_ids"] else "WATCH",
            "priority": index + 1, "event_ids": candidate["event_ids"],
            "supporting_fact_ids": [fact["fact_id"] for fact in frozen["facts"] if fact["instrument_id"] == candidate["instrument"]["instrument_id"]],
            "counterevidence_fact_ids": [fact_id for event in frozen["events"] if event["event_id"] in candidate["counterevidence_event_ids"] for fact_id in event["fact_ids"]],
            "economic_path": "합성 공시의 매출과 영업이익 증가가 단기 사업 재평가로 연결된다는 산식 검증용 가설입니다.",
            "horizon_case": "합성 사건의 영향이 3~20세션에 이어지는 상황을 테스트하며 실제 수익 증거는 아닙니다.",
            "priced_in_case": "현재 합성 호가가 추격 상한 이내인지는 프로그램이 확인하며 가격 반영 정도는 미확인입니다.",
            "invalidation_case": "공식 정정에서 합성 계약의 취소 또는 실적 개선의 철회가 확인되면 이 가설을 무효화합니다.",
            "uncertainties": ["FIXTURE_ONLY: 합성 입력과 기록 응답, 실제 모델 실행 아님"]}
            for index, candidate in enumerate(frozen["candidates"])],
        "position_reviews": [{"instrument_id": thesis["instrument_id"], "thesis_id": thesis["thesis_id"], "action": "KEEP",
            "changed_event_ids": [], "reason": "합성 fixture에 새 근거 무효화가 없습니다. 보호 판단은 별도로 수행합니다."}
            for thesis in frozen["theses"] if thesis["instrument_id"] in frozen["reviewed_positions"]], "human_question": None}


class Application:
    def __init__(self, config: Config, bundle: MarketBundle, *, broker=None,
                 decide: Callable[[dict], dict] | None = None, approval: dict | None = None,
                 refresh: Callable[[], MarketBundle] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.config, self.bundle, self.approval, self.refresh = config, bundle, approval, refresh
        self.clock = clock or ((lambda: self.bundle.now) if bundle.synthetic else
                               getattr(getattr(decide, "__self__", None), "clock", utcnow))
        self.profile = config.research if config.mode != "live" else config.data["strategy"]["strategy"]["live_mandate"]["accepted_risk_policy"]
        self.code_id = code_identity()
        if config.mode != "offline":
            config.require_external("account_read", approval)
            if broker is None or decide is None or refresh is None:
                raise HumanRequired("External runtime requires approved broker, decision and market adapters")
        self.broker = broker or FixtureBroker()
        self.decide = decide or fixture_decision
        self.store = getattr(self.broker, "store", None) or Store(config.state_dir / "state.sqlite", mode=config.mode,
            account_identity=bundle.data["account_identity"], initial_cash=Decimal(self.profile["capital_krw"]))
        if hasattr(self.broker, "bind_store"):
            self.broker.bind_store(self.store)
        self.executor = Executor(self.store, self.broker, mode=config.mode, authorize=self._authorize, preflight=self._preflight)
        self.concentration = ConcentrationMonitor(self.store.get("concentration_state", {}))
        self.drawdown = DrawdownCircuit()
        self.monitor_stop = threading.Event()
        self.monitor_thread = None
        self.review_lock = threading.Lock()

    def _authorize(self, intent: OrderIntent, operation: str) -> None:
        self.config.assert_current()
        if intent.policy_hash != self.config.config_hash:
            raise HumanRequired("Order policy hash differs from frozen execution policy")
        if self.config.mode == "offline":
            if self.broker.environment != "fixture":
                raise HumanRequired("Offline broker write blocked")
            return
        if self.config.mode == "shadow":
            raise HumanRequired("Shadow cannot submit/cancel/replace")
        if self.config.mode == "paper":
            self.config.require_external("paper_simulation", self.approval)
            return
        if not self.config.app["execution"]["enabled"]:
            raise HumanRequired("EXECUTION_DISABLED")
        capability = "live_orders" if self.config.mode == "live" else "demo_orders"
        self.config.require_external(capability, self.approval)
        if self.store.get("activation") != {"config_hash": self.config.config_hash, "code_id": self.code_id,
                                           "approval_id": self.approval["id"]}:
            raise HumanRequired("Trusted operator activation required")

    def _preflight(self, intent: OrderIntent, now: datetime) -> None:
        if self.refresh is not None:
            self.bundle = self.refresh()
        now = max(now, self.bundle.now, self.clock())
        if intent.side == "BUY" and now >= intent.expires_at:
            raise ValueError("ENTRY_EXPIRED")
        quote = self.bundle.quotes.get(intent.instrument_id)
        if quote is None or not quote_fresh(quote, now, self.profile["orders"]["quote_max_age_seconds"]):
            raise ValueError("MONITOR_DEGRADED")
        if not self.bundle.calendar.active(now):
            raise ValueError("INVALID_ORDER_SESSION")
        if intent.side == "BUY":
            controls = self.store.get("candidate_controls", {"removed": [], "excluded": []})
            if intent.instrument_id in controls["removed"] or intent.instrument_id in controls["excluded"]:
                raise ValueError("CANDIDATE_SCOPE_CHANGED")
            candidate = next((item for item in self.bundle.candidates if item.instrument.instrument_id == intent.instrument_id), None)
            if candidate is None:
                raise ValueError("CANDIDATE_NOT_CURRENT")
            gate = assess_entry(candidate, quote, self.bundle.events, self.bundle.calendar, self.bundle.ticks, now, self.profile, synthetic=self.bundle.synthetic)
            if not gate.allowed or quote.ask > intent.limit_price:
                raise ValueError("STALE_DECISION:" + gate.reason)
            thesis = next(item for item in self.theses() if item.thesis_id == intent.thesis_id)
            capped_quote = quote.model_copy(update={"ask": intent.limit_price})
            current_size = size_entry(candidate, capped_quote, thesis.initial_stop, self.portfolio(), self.bundle.costs, now, self.profile)
            if current_size.quantity < intent.quantity:
                raise ValueError("CURRENT_PORTFOLIO_LIMIT:" + current_size.reason)
        else:
            instrument = self.bundle.instruments[intent.instrument_id]
            if instrument.status != "NORMAL" or not instrument.status_verified:
                raise ValueError("UNEXECUTABLE")
            sellable = self.bundle.data.get("strategy_sellable_quantities", {}).get(intent.instrument_id,
                self.store.quantity(intent.instrument_id) if self.bundle.synthetic else 0)
            if intent.quantity > sellable:
                raise ValueError("CURRENT_SELLABLE_QUANTITY_EXCEEDED")

    def theses(self) -> list[InvestmentThesis]:
        return [InvestmentThesis.model_validate_json(row[0]) for row in self.store.db.execute("SELECT payload FROM theses")]

    def portfolio(self) -> PortfolioSnapshot:
        bundle = self.bundle
        theses = {thesis.thesis_id: thesis for thesis in self.theses()}
        holdings, market_complete = [], True
        for row in self.store.holdings():
            thesis = theses[row["thesis_id"]]
            instrument, quote, features = bundle.instruments.get(row["instrument_id"]), bundle.quotes.get(row["instrument_id"]), bundle.features.get(row["instrument_id"])
            fresh = quote is not None and quote.bid is not None and quote_fresh(quote, bundle.now, self.profile["orders"]["quote_max_age_seconds"])
            market_complete = market_complete and fresh and instrument is not None and features is not None
            holdings.append(Holding(instrument_id=row["instrument_id"], issuer_id=instrument.issuer_id if instrument else row["instrument_id"], sector=instrument.sector if instrument else "UNVERIFIED",
                thesis_id=thesis.thesis_id, quantity=row["quantity"], sellable_quantity=min(row["quantity"], bundle.data.get("strategy_sellable_quantities", {}).get(row["instrument_id"], row["quantity"] if bundle.synthetic else 0)),
                mark=quote.bid if quote and quote.bid else thesis.average_entry,
                stop=thesis.current_stop, atr=features.atr14 if features else Decimal(0), average_entry=thesis.average_entry, valuation_at=bundle.now,
                price_observed_at=quote.observed_at if quote else None, valuation_quality="EXACT" if fresh else "STALE" if quote and quote.bid else "MISSING",
                first_fill_session=thesis.first_fill_session, reduced=thesis.reduced_quantity > 0))
        pending = []
        for row in self.store.working():
            if row["side"] == "BUY":
                instrument = bundle.instruments[row["instrument_id"]]
                remaining = row["quantity"] - row["cumulative_quantity"]
                pending.append(PendingEntry(instrument_id=instrument.instrument_id, issuer_id=instrument.issuer_id, sector=instrument.sector,
                    thesis_id=row["thesis_id"], plan_id=row["plan_id"], remaining_quantity=remaining,
                    entry_price=row["limit_price"], unit_risk=Decimal(row["reserve_risk"]) / remaining if remaining else Decimal(1),
                    reserved_cash=row["reserve_cash"], reserved_risk=row["reserve_risk"]))
        cash = Decimal(self.store.get("cash_krw"))
        nav = strategy_nav(cash, {row.instrument_id: row.quantity for row in holdings}, {row.instrument_id: row.mark for row in holdings})
        return PortfolioSnapshot(account_alias=bundle.costs.account_alias, strategy_id="catalyst_trend_swing", as_of=bundle.now,
            nav=nav, allocated_cash=cash, broker_available_cash=Decimal(bundle.data["broker_available_cash"]), holdings=holdings,
            pending_entries=pending, complete=self.store.get("reconciled") and self.store.get("costs_complete", True) and market_complete, ownership_verified=self.store.get("ownership_complete"),
            sector_classification_verified=bundle.data["sector_classification_verified"], account_state_version=self.store.get("account_version"),
            new_risk_paused=self.store.get("paused") or self.store.get("drawdown_paused", False), monitor_degraded=self.store.get("monitor_degraded", False), synthetic=bundle.synthetic)

    def _save_thesis(self, thesis: InvestmentThesis) -> None:
        self.store.db.execute("INSERT INTO theses VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (thesis.thesis_id, thesis.model_dump_json()))

    def _sync_theses(self) -> None:
        with self.store.transaction():
            for thesis in self.theses():
                buys = [dict(row) for row in self.store.db.execute("SELECT * FROM intents WHERE thesis_id=? AND side='BUY' AND cumulative_quantity>0", (thesis.thesis_id,))]
                if buys:
                    quantity = sum(row["cumulative_quantity"] for row in buys)
                    average = sum((Decimal(row["cumulative_notional"]) for row in buys), Decimal(0)) / quantity
                    first = self.store.db.execute("SELECT available_at,payload FROM observations WHERE id=?", (f"first-fill:{thesis.thesis_id}",)).fetchone()
                    when = aware_time(first[0]) if first else self.bundle.now
                    evidence = json.loads(first[1]) if first else {}
                    session = self.bundle.calendar.session(evidence["fill_session_id"]) if evidence.get("fill_session_id") else self.bundle.calendar.active(when)
                    if session is None:
                        raise HumanRequired("First fill does not belong to a verified session")
                    thesis = thesis.model_copy(update={"average_entry": average, "first_fill_at": thesis.first_fill_at or when, "first_fill_session": thesis.first_fill_session or session.session_id,
                        "first_fill_time_quality": thesis.first_fill_time_quality if thesis.first_fill_at else evidence.get("time_quality", "UNKNOWN")})
                reduced = self.store.db.execute("SELECT COALESCE(SUM(cumulative_quantity),0) FROM intents WHERE thesis_id=? AND side='SELL' AND json_extract(payload,'$.reason')='REDUCE_TO_LIMIT'", (thesis.thesis_id,)).fetchone()[0]
                thesis = thesis.model_copy(update={"reduced_quantity": reduced})
                if thesis.first_fill_at and self.store.quantity(thesis.instrument_id) == 0 and not self.store.working(thesis.instrument_id):
                    sells = self.store.db.execute("SELECT payload FROM intents WHERE thesis_id=? AND side='SELL' ORDER BY rowid DESC LIMIT 1", (thesis.thesis_id,)).fetchone()
                    if sells:
                        thesis = thesis.model_copy(update={"exited_at": thesis.exited_at or self.bundle.now, "exit_reason": json.loads(sells[0])["reason"]})
                self._save_thesis(thesis)

    def reconcile(self) -> dict:
        result = self.executor.reconcile()
        self._sync_theses()
        if self.concentration.active_plan and not any(row["side"] == "SELL" for row in self.store.working()):
            targets = self.store.get("concentration_targets", {})
            if targets and all(self.store.quantity(symbol) <= quantity for symbol, quantity in targets.items()):
                self.concentration.completed()
                with self.store.transaction():
                    self.store.set("concentration_state", self.concentration.state())
                    self.store.set("concentration_targets", {})
        return result

    def protect(self) -> list[dict]:
        """Called independently of the review thread and model quota circuit."""
        if self.refresh:
            self.bundle = self.refresh()
        bundle, results = self.bundle, []
        snapshot = self.portfolio()
        self.record_nav()
        reductions = {item.instrument_id: item for item in self.concentration.observe(snapshot, bundle.now, self.profile)}
        with self.store.transaction():
            self.store.set("concentration_state", self.concentration.state())
            if reductions:
                self.store.set("concentration_targets", {symbol: self.store.quantity(symbol) - item.quantity for symbol, item in reductions.items()})
        targets = self.store.get("concentration_targets", {})
        by_thesis = {item.thesis_id: item for item in self.theses()}
        for holding in snapshot.holdings:
            thesis = by_thesis[holding.thesis_id]
            quote = bundle.quotes.get(holding.instrument_id)
            features = bundle.features.get(holding.instrument_id)
            if features is not None:
                thesis = update_trailing_stop(thesis, features, bundle.bars.get(holding.instrument_id, []), bundle.ticks,
                    self.profile, observed_price=quote.bid if quote and quote_fresh(quote, bundle.now, self.profile["orders"]["quote_max_age_seconds"]) and
                        (thesis.first_fill_at is None or quote.observed_at >= thesis.first_fill_at) else None)
            with self.store.transaction():
                self._save_thesis(thesis)
            plan = evaluate_exit(thesis, holding, quote, bundle.calendar, bundle.now, self.profile,
                features=features, account_complete=self.store.get("reconciled") and self.store.get("ownership_complete"),
                orders_known=all(row["state"] not in {"UNKNOWN", "CANCEL_REQUESTED"} for row in self.store.working()),
                tradable=holding.instrument_id in bundle.instruments and bundle.instruments[holding.instrument_id].status == "NORMAL" and bundle.instruments[holding.instrument_id].status_verified,
                invalidating_events=[event for event in bundle.events if event.event_id in thesis.invalidating_event_ids],
                reduction_quantity=max(0, holding.quantity - targets[holding.instrument_id]) if holding.instrument_id in targets else 0)
            results.append(plan.model_dump(mode="json"))
            if self.config.mode == "shadow":
                continue
            if plan.action == "MONITOR_DEGRADED":
                with self.store.transaction():
                    self.store.set("monitor_degraded", True)
                    self.store.event("protection", "MONITOR_DEGRADED", plan.model_dump(mode="json"), notify=True)
            if plan.cancel_pending_entries:
                self.executor.invalidate_unsubmitted_entries("protection", plan.action)
                opposite = [row for row in self.store.working(holding.instrument_id) if row["side"] == "BUY"]
                for row in opposite:
                    self.executor.cancel(row["id"], bundle.now)
                if opposite:
                    self.reconcile()
            if plan.quantity:
                if self.store.working(holding.instrument_id):
                    continue
                quantity = min(plan.quantity, self.store.quantity(holding.instrument_id))
                if quantity:
                    intent = OrderIntent(run_id="protection", plan_id=digest([thesis.thesis_id, plan.action, thesis.reduced_quantity]),
                        thesis_id=thesis.thesis_id, instrument_id=holding.instrument_id, side="SELL", quantity=quantity,
                        limit_price=None, expires_at=None, reason=plan.action, account_version=self.store.get("account_version"), policy_hash=self.config.config_hash)
                    self.executor.submit(intent, bundle.now)
        return results

    def record_nav(self) -> dict:
        snapshot = self.portfolio()
        existing = self.store.get("nav_points", [])
        if not existing:
            # Inception is the approved/synthetic allocation before first activity.
            from datetime import timedelta
            existing.append({"at": (self.bundle.now - timedelta(microseconds=1)).isoformat(), "nav": self.profile["capital_krw"],
                             "session_id": None, "completed": False, "quality": "EXACT"})
        session = self.bundle.calendar.active(self.bundle.now)
        point = {"at": self.bundle.now.isoformat(), "nav": str(snapshot.nav), "session_id": session.session_id if session else None,
                 "completed": False, "quality": "EXACT" if snapshot.complete and snapshot.ownership_verified else "INSUFFICIENT_COVERAGE"}
        existing = [item for item in existing if aware_time(item["at"]) != self.bundle.now] + [point]
        flows = [ExternalFlow(at=aware_time(item["at"]), amount=item["amount"], before_nav=item.get("before_nav"), after_nav=item.get("after_nav"), kind=item["kind"])
                 for item in self.store.get("external_flows", [])]
        result = performance([NavPoint(at=aware_time(item["at"]), nav=Decimal(item["nav"]), session_id=item["session_id"], completed=item["completed"], quality=item["quality"])
                              for item in existing], flows, unallocated=not snapshot.ownership_verified)
        with self.store.transaction():
            self.store.set("nav_points", existing)
            self.store.set("performance", result)
            if result.get("coverage") == "EXACT":
                high = self.store.get("drawdown_high_watermark")
                circuit = DrawdownCircuit(high_watermark=Decimal(high) if high is not None else Decimal(1), paused=self.store.get("drawdown_paused", False))
                state = circuit.observe(Decimal(result["twr"]) + 1, self.profile)
                self.store.set("drawdown_high_watermark", str(circuit.high_watermark))
                self.store.set("drawdown_paused", state["new_risk_paused"])
                if state["newly_paused"]:
                    self.store.event("risk", "DRAWDOWN_PAUSED", state, notify=True)
        if self.store.get("drawdown_paused", False) and self.config.mode != "shadow":
            for order in self.store.working():
                if order["side"] == "BUY":
                    self.executor.cancel(order["id"], self.bundle.now)
        return result

    def start_monitor(self, interval: float = 5) -> None:
        if self.monitor_thread:
            return
        def loop():
            while not self.monitor_stop.wait(interval):
                try:
                    self.reconcile()
                    if self.config.mode != "shadow":
                        self.executor.expire_entries(self.bundle.now)
                    self.protect()
                except Exception as error:
                    with self.store.transaction():
                        self.store.set("monitor_degraded", True)
                        self.store.event("monitor", "MONITOR_DEGRADED", {"error_type": type(error).__name__}, notify=True)
        self.monitor_thread = threading.Thread(target=loop, name="danta-protection", daemon=True)
        self.monitor_thread.start()

    def review(self, *, kind: str = "full_review", event_id: str | None = None,
               request_key: str | None = None) -> dict:
        if kind not in {"full_review", "event_review"}:
            raise ValueError("Unknown review kind")
        with self.review_lock:
            return self._review(kind, event_id, request_key)

    def _review(self, kind: str, event_id: str | None, request_key: str | None) -> dict:
        bundle = self.bundle
        key = request_key or "manual:" + str(uuid4())
        run_id, new = self.store.accept_request(key, {"kind": kind, "event_id": event_id})
        if not new:
            previous = self.store.db.execute("SELECT result FROM requests WHERE request_id=?", (run_id,)).fetchone()
            return json.loads(previous[0]) if previous and previous[0] else {"run_id": run_id, "status": "ALREADY_ACCEPTED"}
        metadata = {"schema_version": 1, "run_id": run_id, "created_at": bundle.now.isoformat(), "config_hash": self.config.config_hash,
                    "strategy_hash": self.config.strategy_hash, "code_id": self.code_id, "mode": self.config.mode,
                    "provenance": bundle.data["provenance"], "account_alias": bundle.costs.account_alias}
        directory = self.config.state_dir / "runs" / bundle.now.date().isoformat() / run_id
        directory.mkdir(parents=True, exist_ok=False)
        def save(name, data):
            (directory / name).write_text(canonical({**metadata, "data": data}) + "\n")
        save("config.snapshot.json", self.config.data)
        result = {**metadata, "run_status": "RUNNING", "model_status": "NOT_CALLED", "decision_status": "NOT_REACHED",
                  "order_status": "NONE", "performance_status": "STRATEGY_UNPROVEN", "live_status": "LIVE_NOT_AUTHORIZED" if self.config.mode != "live" else "OPERATOR_AUTHORIZED"}
        try:
            self.reconcile()
            protection = self.protect()
            bundle = self.bundle
            if self.store.get("paused") or self.store.get("drawdown_paused", False):
                result.update(run_status="BLOCKED", decision_status="NEW_RISK_PAUSED")
                return result
            portfolio = self.portfolio()
            theses = [thesis for thesis in self.theses() if thesis.exited_at is None and (self.store.quantity(thesis.instrument_id) or self.store.working(thesis.instrument_id))]
            candidates = bundle.candidates
            controls = self.store.get("candidate_controls", {"removed": [], "excluded": []})
            candidates = [candidate for candidate in candidates if candidate.instrument.instrument_id not in
                          set(controls["removed"]) | set(controls["excluded"])]
            affected = None
            if kind == "event_review":
                event = next((item for item in bundle.events if item.event_id == event_id), None)
                if event is None:
                    raise ValueError("UNKNOWN_EVENT")
                candidates = [candidate for candidate in candidates if candidate.instrument.instrument_id == event.instrument_id]
                affected = [thesis.instrument_id for thesis in theses if thesis.instrument_id == event.instrument_id]
            held_ids = {thesis.instrument_id for thesis in theses}
            prefilters = []
            eligible = []
            for candidate in candidates:
                symbol = candidate.instrument.instrument_id
                quote = bundle.quotes.get(symbol)
                gate = assess_entry(candidate, quote, bundle.events, bundle.calendar, bundle.ticks, bundle.now, self.profile,
                    synthetic=bundle.synthetic, require_ai=False) if quote else None
                if symbol not in held_ids and gate and gate.allowed:
                    eligible.append(candidate)
                else:
                    prefilters.append({"instrument_id": symbol, "reason": "EXISTING_THESIS" if symbol in held_ids else gate.reason if gate else "MISSING_QUOTE"})
            candidates = eligible
            session = bundle.calendar.active(bundle.now)
            if session is None:
                result.update(run_status="BLOCKED", decision_status="OUTSIDE_SESSION")
                return result
            frozen = freeze_input(run_id=run_id, config_hash=self.config.config_hash, strategy_hash=self.config.strategy_hash, code_id=self.code_id,
                now=bundle.now, session_id=session.session_id, profile=self.profile, portfolio=portfolio, candidates=candidates, events=bundle.events,
                facts=bundle.facts, theses=theses, scope="FULL" if kind == "full_review" else "PARTIAL", reviewed_positions=affected)
            save("input.snapshot.json", frozen)
            save("candidates.json", {"total_universe": bundle.total_universe, "feature_exclusions": bundle.exclusions,
                "candidate_controls": controls, "candidate_controls_hash": digest(controls), "prefilters": prefilters, "candidates": candidates})
            if not candidates and not frozen["reviewed_positions"]:
                result.update(run_status="COMPLETE", decision_status="NO_CANDIDATES", reason="NO_CANDIDATES", performance=self.record_nav())
                return result
            if self.store.get("material_hash") == frozen["material_hash"] and self.store.working():
                result.update(run_status="COMPLETE", decision_status="KEEP_EXISTING_PLAN")
                return result
            result["model_status"] = "FIXTURE_RECORDED_RESPONSE" if bundle.synthetic else "RUNNING"
            proposal_value = self.decide(frozen)
            completed_at = self.clock()
            from datetime import timedelta
            decision_deadline = completed_at + timedelta(seconds=self.profile["orders"]["decision_max_age_seconds"])
            if self.store.get("paused") or self.store.get("drawdown_paused", False):
                result.update(run_status="PAUSED", decision_status="DISCARDED_AFTER_PAUSE", reason="NEW_RISK_PAUSED")
                return result
            current_bundle = self.refresh() if self.refresh else self.bundle
            evidence_scope = {event["instrument_id"] for event in frozen["events"]} | {fact["instrument_id"] for fact in frozen["facts"]} | held_ids | {candidate.instrument.instrument_id for candidate in candidates}
            current_facts_hash = digest([[event for event in current_bundle.events if event.instrument_id in evidence_scope],
                [fact for fact in current_bundle.facts if fact.instrument_id in evidence_scope]])
            decision = validate_proposal(proposal_value, frozen, current_account_version=self.store.get("account_version"),
                current_facts_hash=current_facts_hash, completed_at=completed_at, now=max(current_bundle.now, self.clock()),
                maximum_age_seconds=self.profile["orders"]["decision_max_age_seconds"])
            self.bundle = current_bundle
            save("decision.json", {"proposal": decision, "completed_at": completed_at, "valid_until": decision_deadline,
                "validation": "VALID", "unreviewed": unreviewed_positions(frozen)})
            result["decision_status"] = "VALID"
            if not bundle.synthetic:
                result["model_status"] = "SUCCEEDED"
            plans, orders = [], []
            verdicts = {review.instrument_id: review for review in decision.candidate_reviews}
            for review in decision.position_reviews:
                if review.action == "EXIT_THESIS_INVALID" and self.config.mode != "shadow":
                    thesis = next(thesis for thesis in theses if thesis.thesis_id == review.thesis_id)
                    with self.store.transaction():
                        self._save_thesis(thesis.model_copy(update={"invalidating_event_ids": review.changed_event_ids}))
            self.protect()
            for candidate in rank_candidates([candidate.model_copy(update={"priority": verdicts[candidate.instrument.instrument_id].priority}) for candidate in candidates]):
                instrument_id = candidate.instrument.instrument_id
                quote = self.bundle.quotes[instrument_id]
                gate = assess_entry(candidate, quote, self.bundle.events, self.bundle.calendar, self.bundle.ticks, self.bundle.now,
                    self.profile, verdicts[instrument_id].verdict, synthetic=bundle.synthetic)
                previous = [thesis for thesis in self.theses() if thesis.instrument_id == instrument_id]
                if previous:
                    reentry = reentry_eligibility(previous[-1], self.bundle.events, self.bundle.bars[instrument_id], self.bundle.calendar, self.bundle.now, self.profile)
                    if not reentry.allowed:
                        plans.append({"instrument_id": instrument_id, "reason": reentry.reason, "quantity": 0})
                        continue
                if not gate.allowed:
                    plans.append({"instrument_id": instrument_id, "reason": gate.reason, "reasons": gate.reasons, "quantity": 0})
                    continue
                plan = size_entry(candidate, quote, gate.stop_price, self.portfolio(), bundle.costs, self.bundle.now, self.profile)
                plans.append(plan.model_dump(mode="json"))
                if not plan.quantity or self.config.mode == "shadow":
                    continue
                review = verdicts[instrument_id]
                thesis_id = str(uuid4())
                thesis = InvestmentThesis(thesis_id=thesis_id, instrument_id=instrument_id, event_ids=review.event_ids,
                    source_uris=[event.source_uri for event in bundle.events if event.event_id in review.event_ids], economic_path=review.economic_path,
                    horizon_case=review.horizon_case, counterevidence=canonical(review.counterevidence_fact_ids), invalidation_case=review.invalidation_case,
                    initial_stop=plan.stop_price, current_stop=plan.stop_price, initial_r_price=plan.entry_price - plan.stop_price,
                    average_entry=plan.entry_price, planned_quantity=plan.quantity, risk_budget=plan.risk_budget,
                    strategy_hash=self.config.strategy_hash, policy_hash=self.config.config_hash, created_at=self.bundle.now)
                with self.store.transaction():
                    self._save_thesis(thesis)
                intent = OrderIntent(run_id=run_id, plan_id=digest([frozen["material_hash"], instrument_id]), thesis_id=thesis_id, instrument_id=instrument_id,
                    side="BUY", quantity=plan.quantity, limit_price=plan.entry_price,
                    expires_at=min(plan.expires_at, decision_deadline), reason="ENTRY_ACCEPTED",
                    account_version=self.store.get("account_version"), policy_hash=self.config.config_hash, reserve_cash=plan.reserved_cash, reserve_risk=plan.total_risk)
                order = self.executor.submit(intent, self.bundle.now)
                orders.append(order)
                if self.broker.environment == "fixture" and order["state"] == "ACKNOWLEDGED":
                    # Explicit synthetic post-submission broker observation, never a real fill claim.
                    self.broker.fill(order["broker_id"], plan.quantity, plan.entry_price,
                        buy_commission(bundle.costs, plan.quantity, plan.entry_price), self.bundle.now)
                    self.reconcile()
            with self.store.transaction():
                self.store.set("material_hash", frozen["material_hash"])
            save("plan.json", {"plans": plans, "protection": protection})
            save("execution.json", {"orders": [self.store.order(order["id"]) for order in orders], "journal": [dict(row) for row in self.store.db.execute("SELECT * FROM journal WHERE run_id=?", (run_id,))]})
            result.update(run_status="COMPLETE", order_status="SHADOW_PLAN_ONLY" if self.config.mode == "shadow" else "FIXTURE_FILLED" if orders and bundle.synthetic else "SUBMITTED" if orders else "NONE",
                reason="ENTRY_ACCEPTED" if orders else plans[0]["reason"] if plans else "NO_CANDIDATES", portfolio=self.portfolio().model_dump(mode="json"), performance=self.record_nav())
            return result
        except HumanRequired as error:
            result.update(run_status=error.state, reason=str(error))
            raise
        except Exception as error:
            result.update(run_status="FAILED", reason=str(error), error_type=type(error).__name__)
            if result["model_status"] == "RUNNING":
                result["model_status"] = "MODEL_FAILED"
            raise
        finally:
            save("manifest.json", {"kind": kind, "input_source_hash": digest(bundle.data), "generated_artifacts": sorted(path.name for path in directory.iterdir()),
                "unreached_stages": [name for name in ("input.snapshot.json", "candidates.json", "decision.json", "plan.json", "execution.json") if not (directory / name).exists()],
                "result": result})
            (directory / "result.json").write_text(canonical(result) + "\n")
            write_report(result, directory / "summary.json", directory / "summary.html", "거래 판단·실행 결과")
            with self.store.transaction():
                self.store.db.execute("UPDATE requests SET status=?,result=? WHERE request_id=?", (result["run_status"], canonical(result), run_id))
                self.store.event(run_id, "RUN_OUTCOME", result, notify=True)
                self.store.queue_document("report:" + run_id,
                    f"summary-{directory.parent.name}-{run_id}.html", (directory / "summary.html").read_text(encoding="utf-8"))

    def pause(self) -> dict:
        with self.store.transaction():
            self.store.set("paused", True)
            self.store.bump_version()
            self.store.event("operator", "DISCRETIONARY_PAUSED", {"protection": "CONTINUES"}, notify=True)
        return {"status": "PAUSED", "protection": "CONTINUES"}

    def resume(self) -> dict:
        self.config.assert_current()
        self.config.require_external("resume", self.approval)
        self.reconcile()
        if not self.store.get("reconciled") or not self.store.get("ownership_complete"):
            raise HumanRequired("Current account/ownership reconciliation required")
        if self.store.get("drawdown_paused", False) and "resume_drawdown" not in self.approval["capabilities"]:
            raise HumanRequired("Drawdown requires explicit new operator decision")
        with self.store.transaction():
            self.store.set("paused", False)
            self.store.set("drawdown_paused", False)
            self.store.event("operator", "RESUMED", {"approval_id": self.approval["id"]})
        return {"status": "RESUMED", "approval_id": self.approval["id"], "protection": "CONTINUES"}

    def update_candidate_list(self, command: str, ticker: str) -> dict:
        self.config.assert_current()
        if self.config.mode != "offline":
            self.config.require_external("candidate_control", self.approval)
        commands = {"add_portfolio_ticker": ("removed", False), "remove_portfolio_ticker": ("removed", True),
                    "add_portfolio_except_ticker": ("excluded", True), "remove_portfolio_except_ticker": ("excluded", False)}
        if command not in commands:
            raise ValueError("UNKNOWN_CANDIDATE_COMMAND")
        matches = [symbol for symbol in self.bundle.instruments if symbol == ticker or symbol.split(":")[-1] == ticker]
        if len(matches) != 1:
            raise ValueError("UNKNOWN_OR_AMBIGUOUS_INSTRUMENT")
        symbol, (field, add) = matches[0], commands[command]
        with self.store.transaction():
            controls = self.store.get("candidate_controls", {"removed": [], "excluded": []})
            values = set(controls[field])
            values.add(symbol) if add else values.discard(symbol)
            controls[field] = sorted(values)
            self.store.set("candidate_controls", controls)
            self.store.bump_version()
            result = {"status": "CANDIDATE_LIST_UPDATED", "instrument_id": symbol, "controls": controls,
                      "controls_hash": digest(controls), "config_hash": self.config.config_hash,
                      "approval_id": self.approval["id"] if self.approval else "FIXTURE_ONLY",
                      "holdings_changed": False, "automatic_liquidation": False}
            self.store.event("operator", "CANDIDATE_LIST_UPDATED", result)
        return result

    def status(self) -> dict:
        return {"mode": self.config.mode, "config_hash": self.config.config_hash, "code_id": self.code_id,
                "paused": self.store.get("paused"), "reconciled": self.store.get("reconciled"),
                "cash_krw": self.store.get("cash_krw"), "holdings": self.store.holdings(), "working_orders": self.store.working(),
                "costs_complete": self.store.get("costs_complete", True), "performance": self.store.get("performance"),
                "performance_status": "STRATEGY_UNPROVEN", "provenance": self.bundle.data["provenance"]}

    def close(self):
        self.monitor_stop.set()
        if self.monitor_thread:
            self.monitor_thread.join(timeout=10)
            if self.monitor_thread.is_alive():
                raise HumanRequired("Monitor did not stop; retain writer until it exits")
        self.store.close()
