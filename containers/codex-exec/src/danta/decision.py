"""Frozen decision inputs and source-linked semantic checks, separate from JSON."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from .config import HumanRequired, canonical, digest
from .models import Candidate, EventRecord, InvestmentThesis, MarketFact, PortfolioSnapshot, StrictModel


class CandidateReview(StrictModel):
    instrument_id: str
    verdict: Literal["ACCEPT", "VETO", "WATCH", "INSUFFICIENT_DATA"]
    priority: Annotated[int, Field(strict=True, gt=0)]
    event_ids: list[str]
    supporting_fact_ids: list[str]
    counterevidence_fact_ids: list[str]
    economic_path: str
    horizon_case: str
    priced_in_case: str
    invalidation_case: str
    uncertainties: list[str]


class PositionReview(StrictModel):
    instrument_id: str
    thesis_id: str
    action: Literal["KEEP", "EXIT_THESIS_INVALID", "ABSTAIN"]
    changed_event_ids: list[str]
    reason: str


class DecisionProposal(StrictModel):
    schema_version: Literal[1]
    run_id: str
    input_snapshot_id: str
    account_state_version: Annotated[int, Field(strict=True, ge=0)]
    review_scope: Literal["FULL", "PARTIAL"]
    candidate_reviews: list[CandidateReview]
    position_reviews: list[PositionReview]
    human_question: str | None


def freeze_input(*, run_id: str, config_hash: str, strategy_hash: str, code_id: str,
                 now: datetime, session_id: str, profile: dict, portfolio: PortfolioSnapshot,
                 candidates: list[Candidate], events: list[EventRecord], facts: list[MarketFact],
                 theses: list[InvestmentThesis], scope: str = "FULL", reviewed_positions: list[str] | None = None) -> dict:
    if scope not in {"FULL", "PARTIAL"}:
        raise ValueError("Unknown review scope")
    reviewed_positions = reviewed_positions if reviewed_positions is not None else [holding.instrument_id for holding in portfolio.holdings]
    if scope == "FULL" and set(reviewed_positions) != {holding.instrument_id for holding in portfolio.holdings}:
        raise ValueError("FULL_REVIEW_POSITION_OMITTED")
    instrument_ids = {candidate.instrument.instrument_id for candidate in candidates} | {thesis.instrument_id for thesis in theses}
    events = [event for event in events if event.instrument_id in instrument_ids]
    facts = [fact for fact in facts if fact.instrument_id in instrument_ids]
    for obj in [*events, *facts]:
        if obj.available_at > now:
            raise ValueError("FUTURE_EVIDENCE")
    data = {"schema_version": 1, "run_id": run_id, "created_at": now, "config_hash": config_hash,
            "strategy_hash": strategy_hash, "code_id": code_id, "session_id": session_id,
            "review_scope": scope, "reviewed_positions": reviewed_positions, "strategy_contract": profile,
            "portfolio": portfolio, "theses": theses, "events": events, "facts": facts,
            "candidates": candidates, "pending_orders": portfolio.pending_entries,
            "missing_data": [candidate.instrument.instrument_id for candidate in candidates if candidate.coverage not in {"COMPLETE", "COMPLETE_NO_EVENT"}],
            "output_contract": DecisionProposal.model_json_schema()}
    # Material fingerprint deliberately excludes tick prices, but includes session,
    # account state, event freshness and qualification, not just source text.
    material = {"session_id": session_id, "account_state_version": portfolio.account_state_version,
                "strategy_hash": strategy_hash, "config_hash": config_hash,
                "theses": theses, "events": events, "facts": facts, "candidate_qualification": [
                    [candidate.instrument.instrument_id, candidate.coverage, candidate.event_ids, candidate.features.last_session_id]
                    for candidate in candidates]}
    data["material_hash"] = digest(material)
    data["input_snapshot_id"] = digest(data)
    import json
    return json.loads(canonical(data))


def validate_proposal(value: dict, frozen: dict, *, current_account_version: int,
                      current_facts_hash: str, completed_at: datetime, now: datetime,
                      maximum_age_seconds: int = 120) -> DecisionProposal:
    proposal = DecisionProposal.model_validate(value)
    if proposal.human_question:
        raise HumanRequired(proposal.human_question)
    if proposal.run_id != frozen["run_id"] or proposal.input_snapshot_id != frozen["input_snapshot_id"]:
        raise ValueError("DECISION_INPUT_MISMATCH")
    if proposal.review_scope != frozen["review_scope"]:
        raise ValueError("DECISION_SCOPE_MISMATCH")
    if proposal.account_state_version != frozen["portfolio"]["account_state_version"] or current_account_version != proposal.account_state_version:
        raise ValueError("STALE_ACCOUNT_VERSION")
    if not 0 <= (now - completed_at).total_seconds() <= maximum_age_seconds or current_facts_hash != digest([frozen["events"], frozen["facts"]]):
        raise ValueError("STALE_DECISION")
    candidates = {item["instrument"]["instrument_id"]: item for item in frozen["candidates"]}
    candidate_ids = [item.instrument_id for item in proposal.candidate_reviews]
    position_ids = [item.instrument_id for item in proposal.position_reviews]
    if len(candidate_ids) != len(set(candidate_ids)) or set(candidate_ids) != set(candidates):
        raise ValueError("CANDIDATE_RESULT_OMITTED_OR_DUPLICATED")
    if len(position_ids) != len(set(position_ids)) or set(position_ids) != set(frozen["reviewed_positions"]):
        raise ValueError("POSITION_RESULT_OMITTED_OR_DUPLICATED")
    priorities = [item.priority for item in proposal.candidate_reviews]
    if len(priorities) != len(set(priorities)):
        raise ValueError("DUPLICATE_PRIORITY")
    events = {item["event_id"]: item for item in frozen["events"]}
    facts = {item["fact_id"]: item for item in frozen["facts"]}
    theses = {item["thesis_id"]: item for item in frozen["theses"]}
    for review in proposal.candidate_reviews:
        candidate = candidates[review.instrument_id]
        if not set(review.event_ids).issubset(set(candidate["event_ids"])):
            raise ValueError("EVENT_NOT_LINKED_TO_CANDIDATE")
        for event_id in review.event_ids:
            if event_id not in events or events[event_id]["instrument_id"] != review.instrument_id:
                raise ValueError("UNKNOWN_OR_FOREIGN_EVENT")
        for fact_id in review.supporting_fact_ids + review.counterevidence_fact_ids:
            if fact_id not in facts or facts[fact_id]["instrument_id"] != review.instrument_id:
                raise ValueError("UNKNOWN_OR_FOREIGN_FACT")
        expected_counterfacts = {fact for event_id in candidate["counterevidence_event_ids"]
                                 for fact in events[event_id]["fact_ids"]}
        if not expected_counterfacts.issubset(review.counterevidence_fact_ids):
            raise ValueError("COUNTEREVIDENCE_OMITTED")
        if review.verdict == "ACCEPT":
            if not review.event_ids or not review.supporting_fact_ids:
                raise ValueError("ACCEPT_WITHOUT_PRIMARY_EVIDENCE")
            for event_id in review.event_ids:
                event = events[event_id]
                if not event["official"] or not event["primary_source_complete"] or event["withdrawn"]:
                    raise ValueError("PRIMARY_EVIDENCE_INVALID")
                if any(item["correction_of"] == event_id for item in events.values()):
                    raise ValueError("SUPERSEDED_PRIMARY_EVIDENCE")
            for text in (review.economic_path, review.horizon_case, review.priced_in_case, review.invalidation_case):
                if len(text.strip()) < 20 or text in {"EXAMPLE_ONLY", "TODO"}:
                    raise ValueError("ACCEPT_REQUIRES_SUBSTANTIVE_CASE")
    for review in proposal.position_reviews:
        thesis = theses.get(review.thesis_id)
        if not thesis or thesis["instrument_id"] != review.instrument_id:
            raise ValueError("POSITION_THESIS_MISMATCH")
        if review.action == "EXIT_THESIS_INVALID":
            if not review.changed_event_ids or thesis["invalidation_case"] not in review.reason:
                raise ValueError("INVALIDATION_CONDITION_NOT_LINKED")
            for event_id in review.changed_event_ids:
                event = events.get(event_id)
                if not event or event["instrument_id"] != review.instrument_id or not event["official"] or not event["primary_source_complete"]:
                    raise ValueError("INVALIDATION_SOURCE_UNVERIFIED")
                if event["polarity"] != "NEGATIVE" and event["correction_of"] not in thesis["event_ids"]:
                    raise ValueError("INVALIDATION_EVENT_UNRELATED")
    return proposal


def unreviewed_positions(frozen: dict) -> list[dict]:
    reviewed = set(frozen["reviewed_positions"])
    return [{"instrument_id": item["instrument_id"], "action": "KEEP_UNREVIEWED", "last_reviewed_at": item.get("created_at")}
            for item in frozen["theses"] if item["instrument_id"] not in reviewed]
