"""One workflow for frozen snapshots, reviews, protection and reconciliation."""
from __future__ import annotations

import hashlib
import inspect
import json
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from .accounting import ExternalFlow, NavPoint, performance, strategy_nav
from .config import Config, HumanRequired, ROOT, aware_time, canonical, digest, utcnow, validate_activation
from .decision import DecisionProposal, freeze_input, unreviewed_positions, validate_proposal
from .execution import Executor, FixtureBroker, OrderIntent
from .market import EventRegistry, SessionCalendar, TickTable, calculate_features
from .models import Candidate, CostSchedule, DailyBar, EventRecord, Holding, Instrument, InvestmentThesis, MarketFact, PendingEntry, PortfolioSnapshot, Quote, Session
from .portfolio import buy_commission, size_entry
from .reporting import write_report
from .risk import ConcentrationMonitor, DrawdownCircuit, evaluate_exit, update_trailing_stop
from .safety import reject_credentials
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


def fixture_decision(frozen: dict, *, on_progress=None) -> dict:
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
                 protection_refresh: Callable[[], MarketBundle] | None = None,
                 chat: Callable[..., dict] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.config, self.bundle, self.approval, self.refresh = config, bundle, approval, refresh
        self.chat = chat
        self.protection_refresh = protection_refresh
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
        self.executor = Executor(self.store, self.broker, mode=config.mode, authorize=self._authorize,
                                 preflight=self._preflight, validate=self._validate_order)
        self.concentration = ConcentrationMonitor(self.store.get("concentration_state", {}))
        self.drawdown = DrawdownCircuit()
        self.monitor_stop = threading.Event()
        self.monitor_thread = None
        self.review_lock = threading.Lock()

    def adopt_account(self, bootstrap: dict, *, deployment_bootstrap: dict | None = None) -> None:
        """Import approved existing holdings once, using observed values as the new baseline."""
        self.config.require_external("account_read", self.approval)
        if self.config.mode == "paper":
            return  # Paper owns its simulated ledger, never the broker's existing positions.
        if self.store.get("account_adoption") or self.store.db.execute(
                "SELECT 1 FROM theses UNION ALL SELECT 1 FROM intents UNION ALL SELECT 1 FROM holdings LIMIT 1").fetchone():
            return  # Restarts reconcile the existing ledger; they never rebase it.
        bundle, account = self.bundle, self.bundle.data["account_snapshot"]
        quantities = {key: value for key, value in bootstrap["strategy_quantities"].items() if value}
        actual = {key: value for key, value in account.get("strategy_quantities", {}).items() if value}
        if (bundle.synthetic or not bootstrap.get("ownership_verified") or not bootstrap.get("source") or
                account.get("complete") is not True or account.get("ownership_complete") is not True or
                account.get("errors") or account.get("orders") or quantities != actual):
            raise HumanRequired("ACCOUNT_ADOPTION_REQUIRES_MATCHING_COMPLETE_UNENCUMBERED_SNAPSHOT")
        cash = Decimal(bootstrap["strategy_cash"])
        available = account.get("account_cash", {}).get("cash_krw", account["broker_available_cash"])
        if not cash.is_finite() or cash < 0 or cash > Decimal(available):
            raise HumanRequired("ACCOUNT_ADOPTION_CASH_UNAVAILABLE")
        if cash == 0 and not quantities:
            raise HumanRequired("ACCOUNT_ALLOCATION_EMPTY")
        bundle.calendar.require_environment(synthetic=False)
        bundle.ticks.require_environment(synthetic=False, now=bundle.now)
        session = bundle.calendar.available_session(bundle.now)
        from .deployment_sources import completed_sessions
        completed = completed_sessions(bundle.calendar, bundle.now)
        positions = []
        for symbol, quantity in sorted(quantities.items()):
            instrument, features = bundle.instruments.get(symbol), bundle.features.get(symbol)
            if (type(quantity) is not int or quantity <= 0 or not instrument or not features or not completed or
                    not instrument.status_verified or features.last_session_id != completed[-1].session_id):
                raise HumanRequired("INHERITED_POSITION_MARKET_DATA_UNAVAILABLE")
            quote = bundle.quotes.get(symbol)
            mark = (quote.bid if quote and quote.bid is not None and
                    quote_fresh(quote, bundle.now, self.profile["orders"]["quote_max_age_seconds"])
                    else features.close if bundle.calendar.active(bundle.now) is None else None)
            if mark is None or not bundle.ticks.is_valid(mark):
                raise HumanRequired("INHERITED_POSITION_VALUATION_UNAVAILABLE")
            # The old entry price/time is unknown. Protection starts at adoption valuation.
            stop = bundle.ticks.floor(mark - Decimal(self.profile["exits"]["initial_stop_atr_cap"]) * features.atr14)
            if not 0 < stop < mark:
                raise HumanRequired("INHERITED_POSITION_PROTECTION_UNAVAILABLE")
            thesis = InvestmentThesis(thesis_id="inherited-" + digest([bundle.data["account_identity"], symbol]),
                instrument_id=symbol, event_ids=[], source_uris=[bootstrap["source"]],
                economic_path="기존 보유 인수: 새 매수 가설은 미확인", horizon_case="인수 세션부터 보유 기한과 보호 규칙 적용",
                counterevidence="과거 매수 시점·가격과 최초 매수 근거는 미확인", invalidation_case="가격 보호·추세 훼손·보유 기한",
                initial_stop=stop, current_stop=stop, initial_r_price=mark-stop, average_entry=mark,
                planned_quantity=quantity, risk_budget=(mark-stop)*quantity,
                strategy_hash=self.config.strategy_hash, policy_hash=self.config.config_hash, created_at=bundle.now,
                origin="inherited", adopted_at=bundle.now, adopted_session=session.session_id, first_fill_time_quality="UNKNOWN",
                max_holding_sessions=self.profile["exits"]["max_holding_sessions"],
                trend_exit_consecutive_closes=self.profile["exits"]["trend_exit_consecutive_closes"])
            positions.append({"instrument_id": symbol, "quantity": quantity, "valuation_price": mark, "thesis": thesis})
        self.store.import_positions(digest([self.config.config_hash, account, bundle.now]), positions,
            cash=cash, observed_at=bundle.now.isoformat(), source=bootstrap["source"],
            config_hash=self.config.config_hash, strategy_hash=self.config.strategy_hash,
            deployment_bootstrap=deployment_bootstrap)

    def activate(self, expected_hash: str) -> None:
        """Use the same approval and current-account checks for Docker startup and CLI."""
        validate_activation(self.config, self.approval, expected_hash, self.code_id)
        result = self.reconcile()
        if (result["status"] != "RECONCILED" or not self.store.get("reconciled") or
                not self.store.get("ownership_complete")):
            raise HumanRequired("CURRENT_ACCOUNT_RECONCILIATION_REQUIRED")
        if Decimal(self.store.get("cash_krw")) <= 0 and not self.store.holdings():
            raise HumanRequired("ACCOUNT_ALLOCATION_EMPTY")
        activation = {"config_hash": self.config.config_hash, "code_id": self.code_id, "approval_id": self.approval["id"]}
        with self.store.transaction():
            if self.store.get("activation") != activation:
                self.store.set("activation", activation)
                self.store.event("operator", "ACTIVATED", {"approval_id": self.approval["id"]})

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
        refresh = (self.protection_refresh or self.refresh) if intent.side == "SELL" else self.refresh
        if refresh is not None:
            self.bundle = refresh()
            if "account_snapshot" in self.bundle.data:
                with self.executor.dispatch_lock:
                    if self.store.get("account_version") != intent.account_version:
                        raise ValueError("STALE_ACCOUNT_VERSION")
                    self.executor.reconcile(self.bundle.data["account_snapshot"])
                    self._sync_theses()
        self._validate_order(intent, now)

    def _validate_order(self, intent: OrderIntent, now: datetime) -> None:
        """Pure current-state checks, also run under the executor's dispatch boundary."""
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
            current_size = size_entry(candidate, capped_quote, thesis.initial_stop, self.portfolio(exclude_intent_id=intent.id), self.bundle.costs, now, self.profile)
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
        return [InvestmentThesis.model_validate_json(row[0]) for row in self.store.read("SELECT payload FROM theses")]

    def portfolio(self, *, exclude_intent_id: str | None = None) -> PortfolioSnapshot:
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
            if row["side"] == "BUY" and row["id"] != exclude_intent_id:
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
            pending_entries=pending, complete=self.store.get("reconciled") and
                (self.store.get("costs_complete", True) or self.store.get("account_cash_reconciled", False)) and market_complete,
            ownership_verified=self.store.get("ownership_complete"),
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
                    evidence = json.loads(first[1]) if first else {}
                    when = aware_time(evidence.get("first_fill_at") or evidence.get("observed_at") or first[0]) if first else self.bundle.now
                    session = self.bundle.calendar.session(evidence["fill_session_id"]) if evidence.get("fill_session_id") else self.bundle.calendar.active(when)
                    if session is None:
                        raise HumanRequired("First fill does not belong to a verified session")
                    thesis = thesis.model_copy(update={"average_entry": average, "first_fill_at": when, "first_fill_session": session.session_id,
                        "first_fill_time_quality": evidence.get("time_quality", "UNKNOWN")})
                reduced = self.store.db.execute("SELECT COALESCE(SUM(cumulative_quantity),0) FROM intents WHERE thesis_id=? AND side='SELL' AND json_extract(payload,'$.reason')='REDUCE_TO_LIMIT'", (thesis.thesis_id,)).fetchone()[0]
                thesis = thesis.model_copy(update={"reduced_quantity": reduced})
                if thesis.protection_started_at and self.store.quantity(thesis.instrument_id) == 0 and not self.store.working(thesis.instrument_id):
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

    def protect(self, *, allow_idle_account=False) -> list[dict]:
        """Called independently of the review thread and model quota circuit."""
        refresh = self.protection_refresh or self.refresh
        if refresh:
            self.bundle = refresh(allow_idle_account=True) if allow_idle_account and self.protection_refresh else refresh()
            if "account_snapshot" in self.bundle.data:
                self.executor.reconcile(self.bundle.data["account_snapshot"])
                self._sync_theses()
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
                        (thesis.protection_started_at is None or quote.observed_at >= thesis.protection_started_at) else None)
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
                diagnostic = {**plan.model_dump(mode='json'), 'diagnostics': [row for row in
                    bundle.data.get('runtime_diagnostics', []) if row.get('instrument_id') == holding.instrument_id
                    and row.get('scope') == 'PROTECTION']}
                with self.store.transaction():
                    newly_degraded = not self.store.get("monitor_degraded", False)
                    self.store.set("monitor_degraded", True)
                    self.store.set('monitor_diagnostic', diagnostic)
                    self.store.set('monitor_checked_at', bundle.now.isoformat())
                    self.store.set("monitor_healthy_since", None)
                    if newly_degraded:
                        self.store.event("protection", "MONITOR_DEGRADED", diagnostic, notify=True)
            if plan.cancel_pending_entries:
                self.executor.invalidate_unsubmitted_entries("protection", plan.action)
                opposite = [row for row in self.store.working(holding.instrument_id) if row["side"] == "BUY"]
                for row in opposite:
                    self.executor.cancel(row["id"], bundle.now)
                if opposite:
                    self.reconcile()
            if plan.quantity:
                with self.executor.dispatch_lock:
                    if self.store.working(holding.instrument_id):
                        continue
                    plan_id = digest([thesis.thesis_id, plan.action, thesis.reduced_quantity])
                    previous = self.store.read("SELECT * FROM intents WHERE plan_id=? AND side='SELL' ORDER BY rowid DESC LIMIT 1", (plan_id,))
                    previous = previous[0] if previous else None
                    revision = 1
                    if previous:
                        if previous["state"] not in {"REJECTED", "CANCELED", "PARTIAL_CANCELED", "EXPIRED", "INVALIDATED"}:
                            continue
                        self.reconcile()
                        if self.store.working(holding.instrument_id) or self.store.quantity(holding.instrument_id) != holding.quantity:
                            # A late fill changes the exit plan; reevaluate on the next tick.
                            continue
                        revision = json.loads(previous["payload"])["plan_revision"] + 1
                    quantity = min(plan.quantity, self.store.quantity(holding.instrument_id))
                    if quantity:
                        intent = OrderIntent(run_id="protection", plan_id=plan_id, plan_revision=revision,
                            thesis_id=thesis.thesis_id, instrument_id=holding.instrument_id, side="SELL", quantity=quantity,
                            limit_price=None, expires_at=None, reason=plan.action, account_version=self.store.get("account_version"), policy_hash=self.config.config_hash)
                        self.executor.submit(intent, bundle.now)
        if self.store.get("reconciled") and all(item["action"] not in {"MONITOR_DEGRADED", "RECONCILE_REQUIRED"} for item in results):
            with self.store.transaction():
                if self.store.get("monitor_degraded", False):
                    healthy_since = self.store.get("monitor_healthy_since")
                    if not healthy_since:
                        self.store.set("monitor_healthy_since", bundle.now.isoformat())
                    elif (bundle.now - aware_time(healthy_since)).total_seconds() >= 60:
                        self.store.set("monitor_degraded", False)
                        self.store.set('monitor_diagnostic', None)
                        self.store.event("protection", "MONITOR_RECOVERED", {'checked_at': bundle.now.isoformat(), 'stable_seconds': 60}, notify=True)
        else:
            self.store.set("monitor_healthy_since", None)
        self.store.set('monitor_checked_at', bundle.now.isoformat())
        return results

    def record_nav(self) -> dict:
        snapshot = self.portfolio()
        session = self.bundle.calendar.active(self.bundle.now)
        point = {"at": self.bundle.now.isoformat(), "nav": str(snapshot.nav), "session_id": session.session_id if session else None,
                 "completed": False, "quality": "EXACT" if snapshot.complete and snapshot.ownership_verified
                    and not self.store.get("performance_uncertain", False) else "INSUFFICIENT_COVERAGE"}
        return self._record_nav_point(point, ownership_verified=snapshot.ownership_verified)

    def _record_nav_point(self, point: dict, *, ownership_verified: bool) -> dict:
        with self.store.transaction():
            existing = self.store.get("nav_points", [])
            if not existing and self.bundle.synthetic:
                # Synthetic allocation is a fixture input; external inception needs an observation.
                existing.append({"at": (aware_time(point["at"]) - timedelta(microseconds=1)).isoformat(),
                                 "nav": self.profile["capital_krw"], "session_id": None,
                                 "completed": False, "quality": "EXACT"})
            same_time = [item for item in existing if aware_time(item["at"]) == aware_time(point["at"])]
            if not any(item["completed"] for item in same_time):
                existing = [item for item in existing if item not in same_time] + [point]
            flows = [ExternalFlow(at=aware_time(item["at"]), amount=item["amount"], before_nav=item.get("before_nav"), after_nav=item.get("after_nav"), kind=item["kind"])
                     for item in self.store.get("external_flows", [])]
            result = performance([NavPoint(at=aware_time(item["at"]), nav=Decimal(item["nav"]), session_id=item["session_id"], completed=item["completed"], quality=item["quality"])
                                  for item in existing], flows, unallocated=not ownership_verified or self.store.get("performance_uncertain", False))
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

    def finalize_nav(self) -> dict:
        """Finalize one observed exchange close; incomplete evidence never adds a session."""
        self.config.assert_current()
        bundle = self.refresh() if self.refresh else self.bundle
        self.bundle = bundle
        now = self.clock()
        from .deployment_sources import daily_bar_available_at
        session = next((item for item in reversed(bundle.calendar.sessions)
                        if daily_bar_available_at(item) <= min(now, bundle.now)
                        and now < daily_bar_available_at(item) + timedelta(hours=12)), None)
        issues = []
        if session is None:
            issues.append("COMPLETED_SESSION_UNAVAILABLE")
        if bundle.synthetic and self.config.mode != "offline":
            issues.append("SYNTHETIC_INPUT_CANNOT_FINALIZE_EXTERNAL_SESSION")
        with self.executor.dispatch_lock:
            with self.store.lock:
                previous = next((point for point in self.store.get("nav_points", [])
                                 if session and point["session_id"] == session.session_id and point["completed"]), None)
                if previous and not issues:
                    return {"status": "ALREADY_FINALIZED", "point": previous}
            account = bundle.data.get("account_snapshot")
            if account is None and bundle.synthetic:
                account = self.broker.snapshot()
            if not issues:
                if account is None or account.get("complete") is not True or account.get("errors"):
                    self.executor.reconcile({"complete": False})
                    issues.append("ACCOUNT_INCOMPLETE")
                else:
                    try:
                        self.executor.reconcile(account)
                        self._sync_theses()
                    except HumanRequired:
                        issues.append("ACCOUNT_RECONCILIATION_REQUIRED")
            with self.store.lock:
                if not self.store.get("reconciled") or not self.store.get("ownership_complete"):
                    issues.append("ACCOUNT_RECONCILIATION_REQUIRED")
                if (not self.store.get("costs_complete", False) and not self.store.get("account_cash_reconciled", False)) or self.store.get("performance_uncertain", False):
                    issues.append("SETTLEMENT_COSTS_UNCONFIRMED")
                if any(row["state"] in {"SUBMITTING", "UNKNOWN", "CANCEL_REQUESTED"} for row in self.store.working()):
                    issues.append("ORDER_RECONCILIATION_REQUIRED")
                quantities, marks, sources = {}, {}, {}
                if session:
                    for row in self.store.holdings():
                        symbol = row["instrument_id"]
                        quantities[symbol] = quantities.get(symbol, 0) + row["quantity"]
                        bars = [bar for bar in bundle.bars.get(symbol, []) if bar.session_id == session.session_id]
                        instrument = bundle.instruments.get(symbol)
                        if (len(bars) != 1 or not bars[0].complete or not bars[0].source
                                or bars[0].opens_at != session.opens_at or bars[0].closes_at != session.closes_at
                                or not session.closes_at <= bars[0].available_at <= min(now, bundle.now)
                                or not bars[0].ohlc_consistently_adjusted or not bars[0].adjustment_basis
                                or instrument is None or not instrument.status_verified or instrument.status != "NORMAL"):
                            issues.append("CLOSING_PRICE_UNVERIFIED:" + symbol)
                        else:
                            marks[symbol] = bars[0].close
                            sources[symbol] = {"source": bars[0].source, "available_at": bars[0].available_at.isoformat()}
                result = {"status": "NAV_NOT_FINALIZED", "session_id": session.session_id if session else None,
                          "provenance": bundle.data["provenance"], "issues": sorted(set(issues))}
                if not issues:
                    point = {"at": daily_bar_available_at(session).isoformat(), "nav": str(strategy_nav(self.store.get("cash_krw"), quantities, marks)),
                             "session_id": session.session_id, "completed": True, "quality": "EXACT",
                             "observed_at": bundle.now.isoformat(), "provenance": bundle.data["provenance"], "price_sources": sources}
                    result.update(status="NAV_FINALIZED", point=point,
                                  performance=self._record_nav_point(point, ownership_verified=True))
                with self.store.transaction():
                    self.store.set("nav_finalization", result)
                return result

    def start_monitor(self, interval: float = 5) -> None:
        if self.monitor_thread:
            return
        def loop():
            while not self.monitor_stop.wait(interval):
                try:
                    # Working orders retain reconcile -> expire -> protection ordering.
                    # With no orders, protect() supplies the sole complete account snapshot.
                    if self.store.working() or not (self.protection_refresh or self.refresh):
                        self.reconcile()
                    if self.config.mode != "shadow":
                        self.executor.expire_entries(self.clock())
                    if self.protection_refresh:
                        self.protect(allow_idle_account=True)
                    else:
                        self.protect()
                except Exception as error:
                    reason = str(error)[:500]
                    try:
                        reject_credentials(reason)
                    except ValueError:
                        reason = 'PRIVATE_DIAGNOSTIC_REDACTED'
                    diagnostic = {'error_type': type(error).__name__, 'reason': reason,
                                  'diagnostics': self.store.get('account_diagnostics', []) if not self.store.get('reconciled') else []}
                    with self.store.transaction():
                        newly_degraded = not self.store.get("monitor_degraded", False)
                        self.store.set("monitor_degraded", True)
                        self.store.set("monitor_healthy_since", None)
                        self.store.set('monitor_checked_at', self.clock().isoformat())
                        self.store.set('monitor_diagnostic', diagnostic)
                        if newly_degraded:
                            self.store.event("monitor", "MONITOR_DEGRADED", diagnostic, notify=True)
        self.monitor_thread = threading.Thread(target=loop, name="danta-protection", daemon=True)
        self.monitor_thread.start()

    def review(self, *, kind: str = "full_review", event_id: str | None = None,
               request_key: str | None = None, on_progress=None) -> dict:
        if kind not in {"full_review", "event_review"}:
            raise ValueError("Unknown review kind")
        with self.review_lock:
            return self._review(kind, event_id, request_key, on_progress)

    def _review(self, kind: str, event_id: str | None, request_key: str | None, on_progress=None) -> dict:
        bundle = self.bundle
        key = request_key or "manual:" + str(uuid4())
        run_id, new = self.store.accept_request(key, {"kind": kind, "event_id": event_id})
        if not new:
            previous = self.store.read("SELECT result FROM requests WHERE request_id=?", (run_id,))
            previous = previous[0] if previous else None
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
        details, plans, orders, protection = {}, [], [], []
        try:
            if on_progress:
                on_progress('계좌·주문 상태와 보호 조건을 확인하고 있습니다.')
            self.reconcile()
            if self.protection_refresh and self.refresh:
                self.bundle = self.refresh()
            protection = self.protect()
            bundle = self.bundle
            if self.store.get("paused") or self.store.get("drawdown_paused", False):
                result.update(run_status="BLOCKED", decision_status="NEW_RISK_PAUSED")
                return result
            theses = [thesis for thesis in self.theses() if thesis.exited_at is None and (self.store.quantity(thesis.instrument_id) or self.store.working(thesis.instrument_id))]
            candidates = bundle.candidates
            controls = self.store.get("candidate_controls", {"removed": [], "excluded": []})
            controlled = set(controls['removed']) | set(controls['excluded'])
            excluded_candidates = [candidate for candidate in candidates if candidate.instrument.instrument_id in controlled]
            candidates = [candidate for candidate in candidates if candidate.instrument.instrument_id not in
                          set(controls["removed"]) | set(controls["excluded"])]
            affected = None
            if kind == "event_review":
                event = next((item for item in bundle.events if item.event_id == event_id), None)
                if event is None:
                    raise ValueError("UNKNOWN_EVENT")
                candidates = [candidate for candidate in candidates if candidate.instrument.instrument_id == event.instrument_id]
                excluded_candidates = [candidate for candidate in excluded_candidates if candidate.instrument.instrument_id == event.instrument_id]
                affected = [thesis.instrument_id for thesis in theses if thesis.instrument_id == event.instrument_id]
            held_ids = {thesis.instrument_id for thesis in theses}
            prefilters = []
            eligible = []
            details = {}
            result['review_details'] = []
            result['feature_exclusions'] = [{**row, 'reason':'DAILY_HISTORY_NOT_COLLECTED' if row.get('reason') == repr(row.get('instrument_id')) else row.get('reason')}
                for row in bundle.exclusions if kind == 'full_review' or row.get('instrument_id') == event.instrument_id]
            result['entry_criteria'] = {key: self.profile[key] for key in ('universe', 'signal', 'orders')}
            failed_quotes = {row['instrument_id']: row.get('detail',row['reason']) for row in bundle.data.get('runtime_diagnostics',[])
                             if row.get('scope') == 'ENTRY' and row.get('instrument_id') and
                             (kind == 'full_review' or row['instrument_id'] == event.instrument_id)}
            for symbol in sorted({c.instrument.instrument_id for c in candidates+excluded_candidates} | held_ids | set(failed_quotes)):
                quote, features = bundle.quotes.get(symbol), bundle.features.get(symbol)
                row = {'instrument_id': symbol, 'name': bundle.data.get('instrument_names', {}).get(symbol),
                       'scope': 'HOLDING' if symbol in held_ids else 'NEW', 'stage': 'NOT_REVIEWED',
                       'evaluated_at': bundle.now.isoformat(),
                       'quote': quote.model_dump(mode='json') if quote else None,
                       'quote_age_seconds': (bundle.now-quote.observed_at).total_seconds() if quote else None,
                       'features': features.model_dump(mode='json') if features else None,
                       'evidence': [event.model_dump(mode='json') for event in bundle.events if event.instrument_id == symbol],
                       'facts': [fact.model_dump(mode='json') for fact in bundle.facts if fact.instrument_id == symbol],
                       'filter_reasons': [], 'ai': None, 'plan': None, 'orders': []}
                details[symbol] = row
                result['review_details'].append(row)
                if symbol not in held_ids and (symbol in controlled or symbol in failed_quotes):
                    row.update(stage='PREFILTERED',filter_reasons=['OPERATOR_EXCLUDED'] if symbol in controlled else [failed_quotes[symbol]])
            for candidate in candidates:
                symbol = candidate.instrument.instrument_id
                quote = bundle.quotes.get(symbol)
                gate = assess_entry(candidate, quote, bundle.events, bundle.calendar, bundle.ticks, bundle.now, self.profile,
                    synthetic=bundle.synthetic, require_ai=False) if quote else None
                if symbol not in held_ids and gate and gate.allowed:
                    eligible.append(candidate)
                    details[symbol]['stage'] = 'AWAITING_AI'
                else:
                    reasons = ['EXISTING_THESIS'] if symbol in held_ids else gate.reasons if gate else ['MISSING_QUOTE']
                    details[symbol].update(stage='AWAITING_AI' if symbol in held_ids else 'PREFILTERED', filter_reasons=reasons)
                    prefilters.append({'instrument_id': symbol, 'reason': reasons[0], 'reasons': reasons,
                                      'quote_age_seconds': details[symbol]['quote_age_seconds']})
            candidates = eligible
            session = bundle.calendar.active(bundle.now)
            if session is None:
                result.update(run_status="BLOCKED", decision_status="OUTSIDE_SESSION")
                return result
            frozen = freeze_input(run_id=run_id, config_hash=self.config.config_hash, strategy_hash=self.config.strategy_hash, code_id=self.code_id,
                now=bundle.now, session_id=session.session_id, profile=self.profile, portfolio=self.portfolio(), candidates=candidates, events=bundle.events,
                facts=bundle.facts, theses=theses, scope="FULL" if kind == "full_review" else "PARTIAL", reviewed_positions=affected)
            save("input.snapshot.json", frozen)
            save("candidates.json", {"total_universe": bundle.total_universe, "feature_exclusions": bundle.exclusions,
                "candidate_controls": controls, "candidate_controls_hash": digest(controls), "prefilters": prefilters, "candidates": candidates})
            if not candidates and not frozen["reviewed_positions"]:
                result.update(run_status="COMPLETE", decision_status="NO_CANDIDATES",
                              reason="NO_ELIGIBLE_CANDIDATES" if details else "NO_CANDIDATES", performance=self.record_nav())
                return result
            if self.store.get("material_hash") == frozen["material_hash"] and self.store.working():
                result.update(run_status="COMPLETE", decision_status="KEEP_EXISTING_PLAN")
                return result
            result["model_status"] = "FIXTURE_RECORDED_RESPONSE" if bundle.synthetic else "RUNNING"
            if on_progress:
                on_progress('확인한 계좌와 후보 자료로 투자 판단을 요청하고 있습니다.')
            supports_progress = on_progress and 'on_progress' in inspect.signature(self.decide).parameters
            proposal_value = self.decide(frozen, on_progress=on_progress) if on_progress and supports_progress else self.decide(frozen)
            if on_progress:
                on_progress('모델의 판단을 현재 계좌·주문 조건과 대조하고 있습니다.')
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
            for review in [*decision.candidate_reviews, *decision.position_reviews]:
                details[review.instrument_id].update(stage='AI_REVIEWED', ai=review.model_dump(mode='json'))
            if not bundle.synthetic:
                result["model_status"] = "SUCCEEDED"
            plans, orders = [], []
            verdicts = {review.instrument_id: review for review in decision.candidate_reviews}
            for review in decision.position_reviews:
                if review.action == "EXIT_THESIS_INVALID" and self.config.mode != "shadow":
                    thesis = next(thesis for thesis in theses if thesis.thesis_id == review.thesis_id)
                    with self.store.transaction():
                        self._save_thesis(thesis.model_copy(update={"invalidating_event_ids": review.changed_event_ids}))
            protection = self.protect()
            for candidate in rank_candidates([candidate.model_copy(update={"priority": verdicts[candidate.instrument.instrument_id].priority}) for candidate in candidates]):
                instrument_id = candidate.instrument.instrument_id
                quote = self.bundle.quotes.get(instrument_id)
                details[instrument_id]['order_quote'] = {'checked_at':self.bundle.now.isoformat(),
                    'quote':quote.model_dump(mode='json') if quote else None,
                    'quote_age_seconds':(self.bundle.now-quote.observed_at).total_seconds() if quote else None}
                if quote is None:
                    plans.append({'instrument_id':instrument_id,'reason':'MISSING_QUOTE','quantity':0})
                    continue
                gate = assess_entry(candidate, quote, self.bundle.events, self.bundle.calendar, self.bundle.ticks, self.bundle.now,
                    self.profile, verdicts[instrument_id].verdict, synthetic=bundle.synthetic)
                previous = [thesis for thesis in self.theses() if thesis.instrument_id == instrument_id]
                if previous:
                    reentry = reentry_eligibility(previous[-1], self.bundle.events, self.bundle.bars[instrument_id], self.bundle.calendar, self.bundle.now, self.profile)
                    if not reentry.allowed:
                        plans.append({"instrument_id": instrument_id, "reason": reentry.reason, "quantity": 0})
                        continue
                if not gate.allowed:
                    plans.append({"instrument_id": instrument_id, "reason": gate.reason, "reasons": gate.reasons, "quantity": 0,
                                  'checked_at':self.bundle.now.isoformat(),'quote':quote.model_dump(mode='json')})
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
            orders = [self.store.order(order["id"]) for order in orders]
            states = {order["state"] for order in orders}
            order_status = next(iter(states)) if len(states) == 1 else "MIXED" if states else "NONE"
            if self.config.mode == "shadow":
                order_status = "SHADOW_PLAN_ONLY"
            elif bundle.synthetic and states == {"FILLED"}:
                order_status = "FIXTURE_FILLED"
            reason = ("ORDER_RECONCILIATION_REQUIRED" if "UNKNOWN" in states else
                      "ORDER_" + order_status if states and not states <= {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED"} else
                      "ENTRY_ACCEPTED" if orders else plans[0]["reason"] if plans else
                      "NO_ELIGIBLE_CANDIDATES" if any(row['scope'] == 'NEW' for row in details.values()) else "NO_CANDIDATES")
            result.update(run_status="COMPLETE", order_status=order_status,
                order_states={state: sum(order["state"] == state for order in orders) for state in sorted(states)},
                reason=reason, portfolio=self.portfolio().model_dump(mode="json"), performance=self.record_nav())
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
            # A later failure must not hide earlier persisted submissions.
            orders = [dict(row) for row in self.store.read(
                "SELECT * FROM intents WHERE json_extract(payload,'$.run_id')=?", (run_id,))]
            for plan in plans:
                if plan['instrument_id'] in details:
                    details[plan['instrument_id']]['plan'] = plan
            for order in orders:
                if order['instrument_id'] in details:
                    details[order['instrument_id']]['orders'].append({key: order[key] for key in
                        ('side', 'quantity', 'state', 'cumulative_quantity', 'cumulative_notional')})
            for plan in protection:
                if plan['instrument_id'] in details:
                    details[plan['instrument_id']]['protection'] = plan
            if plans or protection or result['decision_status'] == 'VALID':
                save('plan.json', {'plans':plans, 'protection':protection})
            if orders or result['decision_status'] == 'VALID':
                save('execution.json', {'orders':orders, 'journal':[dict(row) for row in self.store.read('SELECT * FROM journal WHERE run_id=?',(run_id,))]})
            if orders and result['run_status'] != 'COMPLETE':
                states = {order['state'] for order in orders}
                result.update(order_status=next(iter(states)) if len(states) == 1 else 'MIXED',
                              order_states={state:sum(order['state'] == state for order in orders) for state in sorted(states)})
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
        health = self.store.get("model_health", {})
        paused = self.store.get('paused') or self.store.get('drawdown_paused', False)
        review_status = ('PAUSED' if paused else 'ACCOUNT_INCOMPLETE' if not self.store.get('reconciled') else
                         'MONITOR_DEGRADED' if self.store.get('monitor_degraded', False) else
                         'OUTSIDE_SESSION' if self.bundle.calendar.active(self.clock()) is None else 'ENABLED')
        return {"mode": self.config.mode, "config_hash": self.config.config_hash, "code_id": self.code_id,
                "as_of": self.bundle.now.isoformat(),
                'status_checked_at': self.clock().isoformat(),
                'model_id': self.config.app['model']['model_id'],
                'model_checked_at': health.get('checked_at'), 'model_purpose': health.get('purpose'),
                'chat_model': self.store.get('model_health:chat', health if health.get('purpose') == 'chat' else {}),
                'review_model': self.store.get('model_health:review', health if health.get('purpose') == 'review' else {}),
                "authentication": health['status'] if health.get("status", "").startswith("AUTH_") else
                    "AUTHENTICATED_AT_STARTUP" if self.config.mode != 'offline' else "UNVERIFIED",
                "model_status": health.get("status", "NOT_CALLED"), "model_diagnostic": health.get("diagnostic"),
                "account_status": "COMPLETE" if self.store.get("reconciled") else "ACCOUNT_INCOMPLETE",
                "account_diagnostics": self.store.get("account_diagnostics", []),
                'account_checked_at': self.store.get('account_checked_at'),
                'account_succeeded_at': self.store.get('account_succeeded_at'),
                'monitor_checked_at': self.store.get('monitor_checked_at'),
                'monitor_diagnostic': self.store.get('monitor_diagnostic'),
                "monitor_status": "MONITOR_DEGRADED" if self.store.get("monitor_degraded", False) else
                    "RUNNING" if self.monitor_thread and self.monitor_thread.is_alive() else "NOT_RUNNING",
                "review_status": review_status,
                "paused": self.store.get("paused"), "reconciled": self.store.get("reconciled"),
                "cash_krw": self.store.get("cash_krw"), "holdings": self.store.holdings(), "working_orders": self.store.working(),
                "costs_complete": self.store.get("costs_complete", True), "performance": self.store.get("performance"),
                "account_cash_reconciled": self.store.get("account_cash_reconciled", False),
                "account_costs": self.store.get("account_costs"), "pretrade_cost_basis": self.bundle.costs.basis,
                "nav_finalization": self.store.get("nav_finalization"),
                "performance_status": "STRATEGY_UNPROVEN", "provenance": self.bundle.data["provenance"]}

    def close(self):
        self.monitor_stop.set()
        if self.monitor_thread:
            self.monitor_thread.join(timeout=10)
            if self.monitor_thread.is_alive():
                raise HumanRequired("Monitor did not stop; retain writer until it exits")
        runtime = getattr(self.refresh, "__self__", None)
        if runtime is not None and hasattr(runtime, "close"):
            runtime.close()
        self.store.close()
