"""S01–S24. Every market/price/account observation below is explicitly synthetic."""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import ValidationError

from danta.market import DataQualityError, EventRegistry, SessionCalendar, TickTable, calculate_features, coverage_reason, return_over_intervals
from danta.models import Candidate, CostSchedule, DailyBar, EventRecord, Holding, Instrument, InvestmentThesis, PendingEntry, PortfolioSnapshot, Quote, Session
from danta.portfolio import allocate_entries, current_planned_risk, entry_risk, exposures, size_entry
from danta.risk import ConcentrationMonitor, DrawdownCircuit, evaluate_exit, update_trailing_stop
from danta.strategy import assess_entry, decision_fresh, entry_plan_completion_allowed, initial_stop, rank_candidates, reentry_eligibility

ROOT = Path(__file__).resolve().parents[2]
SEOUL = ZoneInfo("Asia/Seoul")


def synthetic_case():
    fixture = json.loads((ROOT / "tests/fixtures/domain-synthetic.json").read_text())
    profile = yaml.safe_load((ROOT / "config/strategy.yaml").read_text())["strategy"]["research_profile"]
    sessions = []
    day = date.fromisoformat(fixture["calendar_start"])
    while len(sessions) < fixture["calendar_sessions"]:
        if day.weekday() < 5:
            sessions.append(Session(session_id=day.isoformat(), ordinal=len(sessions),
                                    opens_at=datetime(day.year, day.month, day.day, 9, tzinfo=SEOUL),
                                    closes_at=datetime(day.year, day.month, day.day, 15, 30, tzinfo=SEOUL)))
        day += timedelta(days=1)
    calendar = SessionCalendar(sessions, provenance=fixture["provenance"], verified=True, synthetic=True)
    ticks = TickTable([(D(lower), D(tick)) for lower, tick in fixture["tick_bands"]],
                      provenance=fixture["provenance"], verified=True, synthetic=True)
    now = sessions[fixture["completed_bars"]].opens_at+timedelta(minutes=30)
    bars, index_bars = [], []
    for index, session in enumerate(sessions[:fixture["completed_bars"]]):
        price = D(fixture["initial_price"])+index*D(fixture["price_increment"])
        common = dict(session_id=session.session_id, opens_at=session.opens_at, closes_at=session.closes_at,
                      available_at=session.closes_at+timedelta(minutes=30), turnover=D(fixture["daily_turnover"]),
                      complete=True, adjustment_basis="synthetic-consistent-price-v1",
                      ohlc_consistently_adjusted=True, source="synthetic")
        bars.append(DailyBar(**common, high=price+D(fixture["high_above_close"]),
                             low=price-D(fixture["low_below_close"]), close=price))
        index_bars.append(DailyBar(**common, high=D("3001"), low=D("2999"), close=D("3000")))
    features = calculate_features(bars, index_bars, instrument_id=fixture["instrument_id"], board="KOSPI",
                                  as_of=now, research_profile=profile)
    instrument = Instrument(instrument_id=fixture["instrument_id"], issuer_id="issuer-AAA", board="KOSPI",
                            sector="synthetic-sector", status="NORMAL", status_verified=True,
                            classification_source="synthetic", effective_at=sessions[0].opens_at)
    event = EventRecord(event_id="event-AAA", instrument_id=instrument.instrument_id, official_id="synthetic-official-1",
                        normalized_key="issuer-AAA:earnings:2026Q1", family="earnings_quality",
                        source_uri="https://example.invalid/official/earnings", source_hash="synthetic-hash",
                        fact_ids=["revenue", "operating-profit"], facts={"revenue": "120", "operating_profit": "15"},
                        comparison_basis="synthetic consolidated previous-year same quarter",
                        available_at=now-timedelta(minutes=60), observed_at=now-timedelta(minutes=60),
                        published_at=now-timedelta(minutes=60), official=True, primary_source_complete=True,
                        timing_quality="EXACT", polarity="POSITIVE")
    candidate = Candidate(instrument=instrument, features=features, event_ids=[event.event_id], coverage="COMPLETE")
    quote = Quote(instrument_id=instrument.instrument_id, venue="KRX", observed_at=now, received_at=now,
                  bid=D("10599"), ask=D("10600"), source="synthetic")
    costs = CostSchedule(source="synthetic zero-fee arithmetic fixture", account_alias="synthetic", venue="KRX",
                         effective_at=sessions[0].opens_at, expires_at=sessions[-1].closes_at,
                         verified=True, synthetic=True, buy_commission_rate=D(0), sell_commission_rate=D(0),
                         sell_tax_rate=D(0), minimum_buy_commission=D(0), minimum_sell_commission=D(0),
                         buy_slippage_bps=D(0), sell_slippage_bps=D(0))
    snapshot = PortfolioSnapshot(account_alias="synthetic", strategy_id="synthetic", as_of=now, nav=D("10000000"),
                                 allocated_cash=D("10000000"), broker_available_cash=D("10000000"), holdings=[],
                                 pending_entries=[], complete=True, ownership_verified=True,
                                 sector_classification_verified=True, account_state_version=1, synthetic=True)
    return profile, calendar, ticks, now, bars, index_bars, candidate, quote, event, costs, snapshot


def synthetic_thesis(case, *, quantity=100):
    profile, calendar, ticks, now, bars, _, candidate, quote, event, costs, snapshot = case
    first = calendar.sessions[119]
    return InvestmentThesis(thesis_id="thesis-AAA", instrument_id=candidate.instrument.instrument_id,
                            event_ids=[event.event_id], source_uris=[event.source_uri], economic_path="synthetic test",
                            horizon_case="synthetic 3–20 sessions", counterevidence="synthetic one-off earnings",
                            invalidation_case="synthetic earnings correction", initial_stop=D("10000"),
                            current_stop=D("10000"), initial_r_price=D("500"), average_entry=D("10500"),
                            planned_quantity=quantity, risk_budget=D("25000"), strategy_hash="synthetic-strategy",
                            policy_hash="synthetic-policy", created_at=first.opens_at,
                            first_fill_at=first.opens_at+timedelta(minutes=30), first_fill_session=first.session_id,
                            max_holding_sessions=20)


def synthetic_holding(case, thesis=None, *, quantity=100, mark=D("10500")):
    thesis = thesis or synthetic_thesis(case, quantity=quantity)
    return Holding(instrument_id=thesis.instrument_id, issuer_id="issuer-AAA", sector="synthetic-sector",
                   thesis_id=thesis.thesis_id, quantity=quantity, sellable_quantity=quantity, mark=mark,
                   stop=thesis.current_stop, atr=D("300"), average_entry=thesis.average_entry,
                   valuation_at=case[3], first_fill_session=thesis.first_fill_session)


class StrategyContractTests(unittest.TestCase):
    def setUp(self):
        self.case = synthetic_case()
        (self.profile, self.calendar, self.ticks, self.now, self.bars, self.index_bars,
         self.candidate, self.quote, self.event, self.costs, self.snapshot) = self.case

    def assess(self, candidate=None, quote=None, events=None, **kwargs):
        return assess_entry(candidate or self.candidate, quote or self.quote,
                            [self.event] if events is None else events, self.calendar, self.ticks,
                            self.now, self.profile, synthetic=True, **kwargs)

    def test_s01_long_term_target_without_event_is_not_entry(self):
        result = self.assess(self.candidate.model_copy(update={"event_ids": []}), events=[])
        self.assertFalse(result.allowed)
        self.assertIn("NO_VALID_RECENT_OFFICIAL_EVENT", result.reasons)
        self.assertTrue(self.assess().allowed)

    def test_s02_repeated_story_is_one_event_without_new_review(self):
        registry = EventRegistry()
        original, changed = registry.ingest(self.event)
        self.assertTrue(changed)
        copied = self.event.model_copy(update={"event_id": "article-copy", "official": False,
                                               "source_uri": "https://example.invalid/article",
                                               "available_at": self.now, "interpretation": "different model wording"})
        event, changed = registry.ingest(copied)
        self.assertFalse(changed)
        self.assertEqual(len(registry.records), 1)
        self.assertEqual(event.available_at, original.available_at)
        self.assertEqual(event.event_id, original.event_id)
        correction = self.event.model_copy(update={"facts": {"revenue": "110"}, "observed_at": self.now})
        self.assertTrue(registry.ingest(correction)[1])
        # Session close and holiday collection do not restart old event freshness.
        close = self.calendar.sessions[119].closes_at
        self.assertEqual(self.calendar.available_session(close).session_id, self.calendar.sessions[120].session_id)
        old = self.event.model_copy(update={"available_at": self.calendar.sessions[114].opens_at})
        self.assertGreater(self.calendar.event_age(old, self.calendar.sessions[120].session_id), 5)

    def test_official_correction_with_new_id_preserves_original_evidence(self):
        registry = EventRegistry([self.event])
        correction = self.event.model_copy(update={"event_id": "official-correction", "official_id": "new-receipt",
            "correction_of": self.event.event_id, "polarity": "NEGATIVE", "facts": {"revenue": "10"}, "observed_at": self.now})
        stored, changed = registry.ingest(correction)
        self.assertTrue(changed)
        self.assertEqual(stored.correction_of, self.event.event_id)
        self.assertEqual(registry.records[self.event.event_id], self.event)
        self.assertEqual(len(registry.records), 2)
        self.assertFalse(registry.ingest(correction)[1])
        self.assertFalse(self.assess(events=list(registry.records.values())).allowed)

    def test_quote_received_after_decision_cannot_authorize_entry_or_protection(self):
        quote = self.quote.model_copy(update={"received_at": self.now + timedelta(hours=1)})
        self.assertIn("STALE_OR_INVALID_QUOTE", self.assess(quote=quote).reasons)

    def test_s03_coverage_failure_is_not_no_event(self):
        self.assertNotEqual(coverage_reason("FETCH_FAILED"), coverage_reason("COMPLETE_NO_EVENT"))
        for coverage in ("FETCH_FAILED", "COMPLETE_NO_EVENT", "PARTIAL"):
            result = self.assess(self.candidate.model_copy(update={"coverage": coverage}))
            self.assertFalse(result.allowed)
            self.assertIn(coverage_reason(coverage), result.reasons)

    def test_s04_twenty_returns_need_twenty_one_closes(self):
        with self.assertRaises(DataQualityError):
            return_over_intervals([D(100)] * 20)
        self.assertEqual(return_over_intervals([D(100)] * 20+[D(110)]), D("0.1"))
        self.assertEqual(self.candidate.features.atr14, D(300))
        self.assertEqual(self.candidate.features.adtv20, D("5000000000"))

    def test_s05_in_progress_or_mixed_adjustments_fail_features(self):
        for update in ({"complete": False}, {"ohlc_consistently_adjusted": False}, {"adjustment_basis": "other"}):
            bars = self.bars[:-1]+[self.bars[-1].model_copy(update=update)]
            with self.assertRaises(DataQualityError):
                calculate_features(bars, self.index_bars, instrument_id="TEST:AAA", board="KOSPI", as_of=self.now, research_profile=self.profile)

    def test_s06_deterministic_gates_override_accept(self):
        f = self.candidate.features
        for update, reason in (({"close": f.sma60}, "TREND_GATE_FAILED"),
                               ({"sma20_five_sessions_ago": f.sma20+1}, "TREND_GATE_FAILED"),
                               ({"rs20": D(0)}, "RELATIVE_STRENGTH_GATE_FAILED"),
                               ({"index_close": f.index_sma60-1}, "BOARD_INDEX_GATE_FAILED")):
            result = self.assess(self.candidate.model_copy(update={"features": f.model_copy(update=update)}))
            self.assertIn(reason, result.reasons)

    def test_s07_chase_cap_waits_without_repricing(self):
        quote = self.quote.model_copy(update={"ask": D("12000"), "bid": D("11999")})
        self.assertIn("WAIT_PRICE", self.assess(quote=quote).reasons)

    def test_s08_invalid_initial_stop_and_tick_boundary(self):
        for update in ({"atr14": D(0)}, {"low5": self.quote.ask+D(1)}, {"low5": self.quote.ask-D(2)}):
            with self.assertRaises(DataQualityError):
                initial_stop(self.quote.ask, self.candidate.features.model_copy(update=update), self.ticks, self.profile)
        bands = TickTable([(D(0),D(1)),(D(2000),D(5))], provenance="synthetic", verified=True, synthetic=True)
        self.assertEqual(bands.previous(D(2000)), D(1999))
        with self.assertRaises(DataQualityError):
            bands.require_environment(synthetic=False, now=self.now)

    def test_s09_risk_budget_and_other_cap(self):
        candidate = self.candidate.model_copy(update={"features": self.candidate.features.model_copy(update={"atr14": D(500)})})
        quote = self.quote.model_copy(update={"ask": D(10000), "bid": D(10000)})
        snapshot = self.snapshot.model_copy(update={"broker_available_cash": D(320000)})
        plan = size_entry(candidate, quote, D(9750), snapshot, self.costs, self.now, self.profile)
        self.assertEqual(plan.risk_budget, D(25000))
        self.assertEqual(plan.unit_risk, D(500))
        self.assertEqual(plan.q_risk, 50)
        self.assertEqual(plan.quantity, 32)
        self.assertEqual(plan.reserved_cash, D(320000))

    def test_s10_no_forced_one_share_and_exact_nonlinear_costs(self):
        stop = self.assess().stop_price
        snapshot = self.snapshot.model_copy(update={"broker_available_cash": D(100)})
        self.assertEqual(size_entry(self.candidate,self.quote,stop,snapshot,self.costs,self.now,self.profile).quantity,0)
        # A fee charged per order reduces 32 shares to 31; no float/floor overrun.
        candidate = self.candidate.model_copy(update={"features": self.candidate.features.model_copy(update={"atr14": D(500)})})
        quote = self.quote.model_copy(update={"ask": D(10000), "bid": D(10000)})
        snapshot = self.snapshot.model_copy(update={"broker_available_cash": D(320000)})
        costs = self.costs.model_copy(update={"minimum_buy_commission": D(100)})
        plan = size_entry(candidate, quote, D(9750), snapshot, costs, self.now, self.profile)
        self.assertEqual(plan.quantity,31)
        self.assertEqual(plan.reserved_cash,D(310100))
        self.assertLessEqual(plan.total_risk,plan.risk_budget)

    def test_s11_unknown_or_excessive_costs_block_only_entry(self):
        stop = self.assess().stop_price
        self.assertEqual(size_entry(self.candidate,self.quote,stop,self.snapshot,None,self.now,self.profile).reason,"COSTS_UNVERIFIED")
        costly = self.costs.model_copy(update={"buy_commission_rate":D("0.1")})
        self.assertEqual(size_entry(self.candidate,self.quote,stop,self.snapshot,costly,self.now,self.profile).reason,"ROUNDTRIP_FRICTION_TOO_HIGH")
        thesis = synthetic_thesis(self.case)
        quote = self.quote.model_copy(update={"bid":D(9999),"ask":D(10000)})
        self.assertEqual(evaluate_exit(thesis,synthetic_holding(self.case,thesis),quote,self.calendar,self.now,self.profile).action,"EXIT_PROTECTION")

    def test_s12_price_noise_keeps_fixed_quantity(self):
        thesis = synthetic_thesis(self.case)
        quote = self.quote.model_copy(update={"bid":D(10400),"ask":D(10401)})
        result = evaluate_exit(thesis,synthetic_holding(self.case,thesis),quote,self.calendar,self.now,self.profile,features=self.candidate.features)
        self.assertEqual(result.action,"KEEP_QUANTITY")
        self.assertEqual(result.quantity,0)
        self.assertEqual(thesis.planned_quantity,100)

    def test_s13_protection_precedes_model_and_stale_trade_rejected(self):
        thesis = synthetic_thesis(self.case)
        quote = self.quote.model_copy(update={"bid":D(10000),"ask":D(10001)})
        result = evaluate_exit(thesis,synthetic_holding(self.case,thesis),quote,self.calendar,self.now,self.profile)
        self.assertEqual(result.action,"EXIT_PROTECTION")
        self.assertEqual(result.trigger_kind,"EXECUTABLE_BID")
        self.assertTrue(result.cancel_pending_entries)
        stale = Quote(instrument_id="TEST:AAA",venue="KRX",observed_at=self.now,received_at=self.now,
                      bid=D(10599),ask=D(10600),last=D(9990),last_observed_at=self.now-timedelta(minutes=5),source="synthetic")
        self.assertEqual(evaluate_exit(thesis,synthetic_holding(self.case,thesis),stale,self.calendar,self.now,self.profile).action,"KEEP_QUANTITY")

    def test_s14_trailing_requires_mfe_and_never_decreases(self):
        thesis = synthetic_thesis(self.case)
        bar = self.bars[-1].model_copy(update={"close":D(11200),"high":D(11300),"low":D(11000)})
        features = self.candidate.features.model_copy(update={"atr14":D(200)})
        raised = update_trailing_stop(thesis,features,[bar],self.ticks,self.profile,observed_price=D(11200))
        self.assertEqual(raised.current_stop,D(10800))
        self.assertEqual(raised.first_fill_at,thesis.first_fill_at)
        self.assertEqual(update_trailing_stop(raised,features,self.bars[-1:],self.ticks,self.profile).current_stop,D(10800))
        self.assertEqual(update_trailing_stop(thesis,features,[],self.ticks,self.profile,observed_price=D(11200)).current_stop,thesis.current_stop)
        # A trailing level above the next fresh bid is acted on, not silently displayed.
        self.assertEqual(evaluate_exit(raised,synthetic_holding(self.case,raised),self.quote,self.calendar,self.now,self.profile).action,"EXIT_PROTECTION")

    def test_s15_two_completed_closes_below_each_sma_exit(self):
        thesis = synthetic_thesis(self.case)
        f = self.candidate.features.model_copy(update={"last_two_closes":(D(100),D(101)),"last_two_sma20":(D(102),D(103))})
        self.assertEqual(evaluate_exit(thesis,synthetic_holding(self.case,thesis),self.quote,self.calendar,self.now,self.profile,features=f).action,"EXIT_TREND_FAILURE")
        f = f.model_copy(update={"last_two_closes":(D(104),D(101))})
        self.assertEqual(evaluate_exit(thesis,synthetic_holding(self.case,thesis),self.quote,self.calendar,self.now,self.profile,features=f).action,"KEEP_QUANTITY")

    def test_s16_first_fill_included_time_exit_and_restart_overdue(self):
        thesis = synthetic_thesis(self.case)
        due = self.calendar.sessions[138]
        now = due.closes_at-timedelta(minutes=10)
        quote = self.quote.model_copy(update={"observed_at":now,"received_at":now,"bid":D(10400),"ask":D(10401)})
        holding = synthetic_holding(self.case,thesis)
        result = evaluate_exit(thesis,holding,quote,self.calendar,now,self.profile)
        self.assertEqual(self.calendar.holding_sessions(thesis.first_fill_session,due.session_id),20)
        self.assertEqual(result.action,"EXIT_TIME_LIMIT")
        self.assertEqual(evaluate_exit(thesis,holding,quote,self.calendar,due.closes_at+timedelta(minutes=1),self.profile).action,"EXIT_OVERDUE")

    def test_s17_stop_same_session_reentry_is_blocked(self):
        thesis = synthetic_thesis(self.case).model_copy(update={"exit_reason":"EXIT_PROTECTION","exited_at":self.now-timedelta(minutes=1)})
        gate = reentry_eligibility(thesis,[self.event],self.bars,self.calendar,self.now,self.profile)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason,"REENTRY_COMPLETED_SESSION_REQUIRED")

    def test_s18_stop_reentry_after_completed_recovery_session(self):
        thesis = synthetic_thesis(self.case).model_copy(update={"exit_reason":"EXIT_PROTECTION","exited_at":self.bars[-1].opens_at})
        self.assertGreater(self.bars[-1].close,thesis.average_entry)
        self.assertTrue(reentry_eligibility(thesis,[self.event],self.bars,self.calendar,self.now,self.profile).allowed)
        self.assertTrue(self.assess().allowed)

    def test_s19_invalidated_contract_requires_distinct_official_resolution(self):
        thesis = synthetic_thesis(self.case).model_copy(update={"exit_reason":"EXIT_THESIS_INVALID","exited_at":self.bars[-1].opens_at,
                                                                "invalidating_event_ids":["contract-cancelled"]})
        self.assertEqual(reentry_eligibility(thesis,[self.event],self.bars,self.calendar,self.now,self.profile).reason,"REENTRY_OFFICIAL_RESOLUTION_REQUIRED")
        resolution = self.event.model_copy(update={"event_id":"official-resolution","resolves_event_ids":["contract-cancelled"]})
        self.assertTrue(reentry_eligibility(thesis,[resolution],self.bars,self.calendar,self.now,self.profile).allowed)

    def test_s20_partial_plan_completion_only_and_no_averaging(self):
        thesis = synthetic_thesis(self.case,quantity=15)
        self.assertTrue(entry_plan_completion_allowed(thesis,"p1","p1",10,3,2))
        self.assertFalse(entry_plan_completion_allowed(thesis,"p2","p1",10,3,2))
        self.assertFalse(entry_plan_completion_allowed(thesis,"p1","p1",10,3,3))
        snapshot = self.snapshot.model_copy(update={"holdings":[synthetic_holding(self.case,thesis,quantity=10)]})
        self.assertEqual(size_entry(self.candidate,self.quote,self.assess().stop_price,snapshot,self.costs,self.now,self.profile).reason,"EXISTING_THESIS_NO_ADDITIONAL_BUY")

    def test_s21_price_appreciation_to_twenty_two_percent_does_not_rebalance(self):
        holding = synthetic_holding(self.case,quantity=220,mark=D(10000))
        snapshot = self.snapshot.model_copy(update={"holdings":[holding]})
        monitor = ConcentrationMonitor()
        self.assertEqual(monitor.observe(snapshot,self.now,self.profile),[])
        later = self.now+timedelta(seconds=5)
        snapshot = snapshot.model_copy(update={"as_of":later,"holdings":[holding.model_copy(update={"valuation_at":later})]})
        self.assertEqual(monitor.observe(snapshot,later,self.profile),[])
        self.assertFalse(monitor.active_plan)

    def test_s22_two_valid_observations_fix_one_trim_plan(self):
        holding = synthetic_holding(self.case,quantity=260,mark=D(10000))
        snapshot = self.snapshot.model_copy(update={"holdings":[holding]})
        monitor = ConcentrationMonitor()
        self.assertEqual(monitor.observe(snapshot,self.now,self.profile),[])
        invalid = snapshot.model_copy(update={"complete":False,"as_of":self.now+timedelta(seconds=3)})
        self.assertEqual(monitor.observe(invalid,self.now+timedelta(seconds=3),self.profile),[])
        later = self.now+timedelta(seconds=5)
        snapshot = snapshot.model_copy(update={"as_of":later,"holdings":[holding.model_copy(update={"valuation_at":later})]})
        restored = ConcentrationMonitor(monitor.state())
        reductions = restored.observe(snapshot,later,self.profile)
        self.assertEqual(len(reductions),1)
        self.assertEqual(reductions[0].quantity,60)
        self.assertEqual(reductions[0].target_quantity,200)
        self.assertEqual(restored.observe(snapshot,later,self.profile),[])
        self.assertFalse(entry_plan_completion_allowed(synthetic_thesis(self.case).model_copy(update={"reduced_quantity":60}),"p1","p1",40,0,60))

    def test_s23_drawdown_pauses_entries_and_cancels_without_liquidating(self):
        circuit = DrawdownCircuit()
        circuit.observe(D(100),self.profile)
        state = circuit.observe(D(90),self.profile)
        self.assertTrue(state["new_risk_paused"])
        self.assertTrue(state["cancel_pending_entries"])
        self.assertTrue(state["keep_protection"])
        self.assertFalse(state["liquidate_all"])
        self.assertTrue(circuit.observe(D(120),self.profile)["new_risk_paused"])

    def test_s24_negative_event_or_price_change_invalidates_old_decision(self):
        args = dict(completed_at=self.now-timedelta(seconds=30),now=self.now,
                    frozen_facts_hash="old",current_facts_hash="old",frozen_account_version=1,current_account_version=1,
                    frozen_policy_hash="policy",current_policy_hash="policy",current_entry_gate=self.assess(),research_profile=self.profile)
        self.assertTrue(decision_fresh(**args).allowed)
        self.assertFalse(decision_fresh(**{**args,"current_facts_hash":"negative-event-arrived"}).allowed)
        quote = self.quote.model_copy(update={"ask":D(12000),"bid":D(11999)})
        self.assertFalse(decision_fresh(**{**args,"current_entry_gate":self.assess(quote=quote)}).allowed)
        self.assertFalse(decision_fresh(**{**args,"current_account_version":2}).allowed)
        negative = self.event.model_copy(update={"event_id":"correction","correction_of":self.event.event_id,"polarity":"NEGATIVE"})
        self.assertIn("NEGATIVE_EVENT_REVIEW_REQUIRED",self.assess(events=[self.event,negative]).reasons)

    def test_current_risk_pending_reservations_and_independent_rank_allocation(self):
        holding = synthetic_holding(self.case,quantity=10,mark=D(11000))
        pending = PendingEntry(instrument_id="TEST:BBB",issuer_id="issuer-BBB",sector="synthetic-sector",
                               thesis_id="t-b",plan_id="p-b",remaining_quantity=3,entry_price=D(10000),unit_risk=D(500),reserved_cash=D(30000))
        snapshot = self.snapshot.model_copy(update={"holdings":[holding],"pending_entries":[pending]})
        self.assertEqual(current_planned_risk(snapshot,self.costs,self.profile),D(10)*(D(1000)+D(150))+D(1500))
        self.assertEqual(exposures(snapshot)[2],D(140000))
        # Previously paid buy commission is absent from held risk, while sell cost is present.
        changed = self.costs.model_copy(update={"buy_commission_rate":D("0.5")})
        self.assertEqual(current_planned_risk(snapshot,changed,self.profile),current_planned_risk(snapshot,self.costs,self.profile))
        other = self.candidate.model_copy(update={"instrument":self.candidate.instrument.model_copy(update={"instrument_id":"TEST:BBB","issuer_id":"issuer-BBB"})})
        self.assertEqual([c.instrument.instrument_id for c in rank_candidates([other,self.candidate])],["TEST:AAA","TEST:BBB"])

    def test_baselines_do_not_inherit_ai_semantic_filters(self):
        negative = self.event.model_copy(update={"polarity":"NEGATIVE"})
        self.assertFalse(self.assess(events=[negative]).allowed)
        self.assertTrue(self.assess(events=[negative],require_ai=False).allowed)
        self.assertTrue(self.assess(events=[],require_ai=False,require_event=False).allowed)

    def test_official_source_completion_upgrades_partial_event_without_reaging(self):
        partial = self.event.model_copy(update={"primary_source_complete":False})
        registry = EventRegistry([partial])
        completed, changed = registry.ingest(self.event)
        self.assertTrue(changed)
        self.assertTrue(completed.primary_source_complete)
        self.assertEqual(completed.available_at,partial.available_at)
        duplicate = self.event.model_copy(update={"event_id":"alternate-model-id","normalized_key":"alternate-wording"})
        self.assertFalse(registry.ingest(duplicate)[1])
        self.assertEqual(len(registry.records),1)

    def test_overlapping_concentration_groups_do_not_double_sell(self):
        holdings = []
        for suffix in "ABC":
            holding = synthetic_holding(self.case,quantity=31,mark=D(10000)).model_copy(update={
                "instrument_id":"TEST:"+suffix,"issuer_id":"issuer-"+suffix,"thesis_id":"thesis-"+suffix})
            holdings.append(holding)
        snapshot = self.snapshot.model_copy(update={"nav":D(1000000),"holdings":holdings})
        monitor = ConcentrationMonitor()
        self.assertEqual(monitor.observe(snapshot,self.now,self.profile),[])
        later = self.now+timedelta(seconds=5)
        snapshot = snapshot.model_copy(update={"as_of":later,"holdings":[h.model_copy(update={"valuation_at":later}) for h in holdings]})
        plans = monitor.observe(snapshot,later,self.profile)
        by_id = {p.instrument_id:p for p in plans}
        self.assertEqual(by_id["TEST:A"].quantity,31)
        self.assertEqual(by_id["TEST:B"].quantity,11)
        self.assertEqual(by_id["TEST:C"].quantity,11)
        self.assertEqual(sum(p.target_quantity for p in plans),40)
        self.assertTrue(all(p.quantity <= 31 for p in plans))

    def test_aggregate_risk_and_pending_slots_remain_reserved(self):
        candidate = self.candidate.model_copy(update={"features":self.candidate.features.model_copy(update={"atr14":D(500)})})
        quote = self.quote.model_copy(update={"ask":D(10000),"bid":D(10000)})
        # The new risk budget allows 50, but previous unfilled risk leaves only 10.
        pending = PendingEntry(instrument_id="TEST:BBB",issuer_id="issuer-BBB",sector="other",
                               thesis_id="b",plan_id="b",remaining_quantity=10,entry_price=D(10000),
                               unit_risk=D(9500),reserved_cash=D(100000),reserved_risk=D(95000))
        snapshot = self.snapshot.model_copy(update={"pending_entries":[pending]})
        plan = size_entry(candidate,quote,D(9750),snapshot,self.costs,self.now,self.profile)
        self.assertEqual(plan.quantity,10)
        all_pending = [pending.model_copy(update={"instrument_id":"TEST:"+str(i),"issuer_id":"issuer-"+str(i),
                                                   "reserved_risk":D(1)}) for i in range(5)]
        snapshot = self.snapshot.model_copy(update={"pending_entries":all_pending})
        self.assertEqual(size_entry(candidate,quote,D(9750),snapshot,self.costs,self.now,self.profile).reason,"NO_ISSUER_SLOT")

    def test_strict_boundary_rejects_bool_quantity_nan_and_naive_time(self):
        for change in ({"quantity":True},{"quantity":"1"},{"mark":"NaN"},{"valuation_at":datetime(2026,1,1)}):
            data = synthetic_holding(self.case).model_dump()
            data.update(change)
            with self.assertRaises((ValidationError, ValueError)):
                Holding.model_validate(data)
        with self.assertRaises(ValidationError):
            Quote(**{**self.quote.model_dump(),"undocumented":True})
        with self.assertRaises(ValidationError):
            Quote(**{**self.quote.model_dump(),"bid":D(12000)})


if __name__ == "__main__":
    unittest.main()
