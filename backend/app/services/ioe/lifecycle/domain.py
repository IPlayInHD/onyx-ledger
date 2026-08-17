"""Opportunity Expiry / Decay + Lifecycle — the pure domain.

WHAT IT IS. One deterministic join of two certified current-state read models:
the Tax Assurance Map (what the governed authorities say about each
opportunity) and the Decision Journal (what the user said about it). Out comes
the current lifecycle of every opportunity, on independent axes.

WHAT IT IS NOT. It computes no tax, evaluates no rule, runs no optimizer,
reads no document, and — importantly — defines no threshold of its own. Every
value here was decided by an authority that owns it; this module renames
nothing and re-derives nothing.

SEVEN AXES, KEPT APART. Availability, decision, execution, evidence, timing,
freshness and integrity answer different questions. Collapsing any two would
make a true combination inexpressible — "urgent but already reported",
"expired but the user acted", "declined but still available" are all real
states a customer needs to see, and a single status enum cannot hold them.

WHERE EACH AXIS COMES FROM, exactly:

    availability      governed `eligibility_status` + Assurance BLOCKED
    decision          Decision Journal projection (or NO_DECISION)
    execution         Decision Journal projection, verbatim
    evidence          Assurance `evidence_readiness`, verbatim
    timing            Assurance `UrgencyStatus`, VERBATIM — see below
    freshness         Assurance item freshness + stale reasons, verbatim
    integrity         Assurance item integrity + reason, verbatim

TIMING IS NOT RECOMPUTED HERE. `UrgencyStatus` and its thresholds
(`URGENT_WITHIN_DAYS`, `APPROACHING_WITHIN_DAYS`) are already governed by the
Assurance module, which evaluated them against the same `as_of`. This module
reads the verdict. A second implementation would be a second answer to "is
this urgent", and the two would eventually disagree — so there is deliberately
no threshold literal anywhere below, and a test greps for one.

`as_of` is carried through for echo and provenance only. Nothing here reads a
clock.

NO DECAY SCORE. "Decay" is represented by the governed deadline, the exact
`days_remaining`, and the deterministic timing band — three transparent facts.
A numeric decay/priority score would need a weighting no authority defines,
and would read as a second optimizer. See the architecture note.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from app.services.ioe.journal.domain import Decision, ExecutionState
from app.services.state_graph.assurance import (
    ActionStatus,
    AssuranceStatus,
    DeadlineAssurance,
    OpportunityAssurance,
    SupportAssurance,
    TaxAssuranceMap,
    UrgencyStatus,
)

#: This read model's contract version. Independent of the ScenarioResult
#: protocol, Before-You-Act, Assurance and Decision Journal versions: it has
#: its own shape and its own consumers.
OPPORTUNITY_LIFECYCLE_CONTRACT_VERSION = "1.0.0"


class Availability(StrEnum):
    """Can this opportunity be acted on at all, per the governed authorities?

    A thin renaming of two governed inputs and nothing else — the rules
    layer's `eligibility_status` and the Assurance BLOCKED verdict. Nothing
    here re-evaluates eligibility.

        BLOCKED                  Assurance says a governed exclusion applies
        CONDITIONALLY_AVAILABLE  rules say `conditionally_eligible`
        AVAILABLE                rules say `eligible`
        UNDETERMINED             rules say `indeterminate` — consumers exclude
                                 rather than guess, which is the rules layer's
                                 own documented intent

    Family-level absence of authority is NOT here: it lives on the map's
    `opportunity_authority`, because "no run exists" is a statement about the
    source, not about one opportunity.
    """

    AVAILABLE = "AVAILABLE"
    CONDITIONALLY_AVAILABLE = "CONDITIONALLY_AVAILABLE"
    UNDETERMINED = "UNDETERMINED"
    BLOCKED = "BLOCKED"


class DecisionState(StrEnum):
    """The user's declared intent — the Journal's own vocabulary, plus the
    honest absence case.

    `NO_DECISION` is not a parallel semantic: it says no Journal thread names
    this opportunity, which is different from a thread that opened and stayed
    at CONSIDERING. Inferring CONSIDERING from silence would put words in the
    user's mouth.
    """

    NO_DECISION = "NO_DECISION"
    CONSIDERING = "CONSIDERING"
    PROCEED = "PROCEED"
    DEFER = "DEFER"
    DECLINE = "DECLINE"


class LifecycleActionability(StrEnum):
    """What, if anything, is the next move — a deterministic presentation of
    the axes above, never an independent recommendation.

    It reuses Assurance's `ActionStatus` values where they still hold, and adds
    the three states that only exist once timing and the Journal are in scope.
    Precedence, highest first, with the reason each outranks the next:

        BLOCKED            a governed exclusion applies; nothing else matters
                           because the user cannot act at all
        ACTION_REPORTED    the user says they already acted. Outranks EXPIRED
                           deliberately: labelling a window the user already
                           used as "expired" would imply they missed it
        EXPIRED            the governed deadline has passed
        DECLINED           the user declined; the opportunity may still be
                           available, and the decision axis still says so
        DEFERRED           the user deferred; deliberately NOT expiry
        EVIDENCE_REQUIRED  Assurance's own verdict
        DECISION_REQUIRED  Assurance's own verdict
        ACTION_AVAILABLE   nothing stands in the way

    `COMPLETE` is deliberately absent. The Journal supplies user intent and
    self-report but no proof of execution, so nothing governed defines
    completion. ACTION_REPORTED says exactly what is known and no more.
    """

    ACTION_AVAILABLE = ActionStatus.ACTION_AVAILABLE.value
    DECISION_REQUIRED = ActionStatus.DECISION_REQUIRED.value
    EVIDENCE_REQUIRED = ActionStatus.EVIDENCE_REQUIRED.value
    BLOCKED = ActionStatus.BLOCKED.value
    DEFERRED = "DEFERRED"
    DECLINED = "DECLINED"
    ACTION_REPORTED = "ACTION_REPORTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class AttentionFlags:
    """Structured flags rather than a score.

    Each flag is one governed fact, independently checkable. A single number
    would need a weighting no authority defines and would invite reading as a
    priority the optimizer never assigned.
    """

    needs_decision: bool
    needs_evidence: bool
    deadline_approaching: bool
    deadline_urgent: bool
    expired: bool
    blocked: bool
    stale: bool

    @property
    def any_flag(self) -> bool:
        return any((
            self.needs_decision, self.needs_evidence, self.deadline_approaching,
            self.deadline_urgent, self.expired, self.blocked, self.stale,
        ))


@dataclass(frozen=True)
class JournalThreadView:
    """One Decision Journal thread, as the lifecycle consumes it.

    An ORM-free value so the join is testable without a database. `subject`
    is the thread's `subject_opportunity_code`, which is NULLABLE in the
    schema — a thread that names no opportunity cannot be joined to one, and
    saying so is more honest than guessing.
    """

    journal_id: uuid.UUID
    subject: str | None
    created_at: datetime
    decision: Decision
    execution: ExecutionState
    last_reported_action_date: date | None


@dataclass(frozen=True)
class JournalLink:
    """How one opportunity relates to the user's Journal threads."""

    #: The thread whose decision the lifecycle reports. Deterministic choice:
    #: most recently created, tie-broken by id. See `_choose_thread`.
    journal_id: uuid.UUID
    #: How many threads name this opportunity. Exposed rather than hidden: a
    #: user who decided twice has two threads, and reporting only the latest
    #: without saying so would look like the others never happened.
    thread_count: int


@dataclass(frozen=True)
class OpportunityLifecycle:
    opportunity_code: str
    source_id: str

    availability: Availability
    decision: DecisionState
    execution: ExecutionState
    evidence: str
    timing: UrgencyStatus
    freshness: str
    stale_reason_codes: tuple[str, ...]
    integrity: str
    integrity_reason_code: str

    deadline: DeadlineAssurance | None
    days_remaining: int | None

    actionability: LifecycleActionability
    #: Closed codes naming every governed condition that shaped this record.
    reason_codes: tuple[str, ...]

    attention: AttentionFlags
    journal: JournalLink | None
    last_reported_action_date: date | None

    #: Governed figures, verbatim from the sealed optimizer output.
    standalone_potential: str | None
    incremental_portfolio_benefit: str | None
    candidate_rank: int | None
    support: SupportAssurance
    assumption_dependent: bool


@dataclass(frozen=True)
class LifecycleSummary:
    """Transparent counts. No composite score, and no money figure at all —
    "savings missed" would be a calculation no authority performed."""

    opportunity_count: int
    by_availability: Mapping[str, int]
    by_decision: Mapping[str, int]
    by_execution: Mapping[str, int]
    by_timing: Mapping[str, int]
    by_actionability: Mapping[str, int]
    needing_attention: int
    #: Threads naming no opportunity, so joinable to none. Reported rather than
    #: dropped: a thread the lifecycle cannot place still exists.
    unlinked_thread_count: int


@dataclass(frozen=True)
class OpportunityLifecycleMap:
    contract_version: str
    tax_year: int
    as_of: str
    #: The Assurance map this was derived from, by its graph anchor.
    graph_hash: str
    #: The OPPORTUNITY family's standing. `UNAVAILABLE` here means no governing
    #: run exists — which is why an empty `opportunities` list must never be
    #: read as "no opportunities".
    opportunity_authority: str
    opportunity_authority_reason: str
    opportunities: tuple[OpportunityLifecycle, ...]
    #: source_ids in review-next order. Presentation, not optimality.
    attention_order: tuple[str, ...]
    summary: LifecycleSummary


# ---------------------------------------------------------------------------
# Reason codes — closed, each naming the governed input that produced it
# ---------------------------------------------------------------------------
REASON_GOVERNED_EXCLUSION = "GOVERNED_EXCLUSION_APPLIES"
REASON_EVIDENCE_INCOMPLETE = "REQUIRED_EVIDENCE_INCOMPLETE"
REASON_REVIEW_OWED = "GOVERNED_REVIEW_OWED"
REASON_CONDITIONAL_ELIGIBILITY = "ELIGIBILITY_CONDITIONAL"
REASON_ELIGIBILITY_UNDETERMINED = "ELIGIBILITY_INDETERMINATE"
REASON_DEADLINE_PASSED = "GOVERNED_DEADLINE_PASSED"
REASON_DEADLINE_URGENT = "GOVERNED_DEADLINE_URGENT"
REASON_DEADLINE_APPROACHING = "GOVERNED_DEADLINE_APPROACHING"
REASON_NO_DEADLINE = "NO_GOVERNED_DEADLINE"
REASON_USER_DEFERRED = "USER_DEFERRED"
REASON_USER_DECLINED = "USER_DECLINED"
REASON_USER_REPORTED_ACTION = "USER_REPORTED_ACTION"
REASON_INPUTS_STALE = "INPUTS_CHANGED_SINCE_EVALUATION"

_EVIDENCE_GAP = frozenset({"MISSING", "PARTIAL", "UNKNOWN"})


def _availability(item: OpportunityAssurance) -> Availability:
    if item.status is AssuranceStatus.BLOCKED:
        return Availability.BLOCKED
    if item.eligibility_status == "conditionally_eligible":
        return Availability.CONDITIONALLY_AVAILABLE
    if item.eligibility_status == "indeterminate":
        return Availability.UNDETERMINED
    return Availability.AVAILABLE


def _choose_thread(threads: Sequence[JournalThreadView]) -> JournalThreadView:
    """The thread whose decision the lifecycle reports.

    Most recently created wins, tie-broken by id so the order is total and two
    threads created in the same instant cannot swap between requests. The
    others are not discarded — `thread_count` reports them, and the Journal
    remains the history authority for all of them.
    """
    return max(threads, key=lambda t: (t.created_at, str(t.journal_id)))


def _actionability(
    *,
    availability: Availability,
    decision: DecisionState,
    execution: ExecutionState,
    timing: UrgencyStatus,
    assurance_action: ActionStatus,
) -> LifecycleActionability:
    """The documented precedence, applied. First match wins."""
    if availability is Availability.BLOCKED:
        return LifecycleActionability.BLOCKED
    if execution is ExecutionState.USER_REPORTED:
        return LifecycleActionability.ACTION_REPORTED
    if timing is UrgencyStatus.EXPIRED:
        return LifecycleActionability.EXPIRED
    if decision is DecisionState.DECLINE:
        return LifecycleActionability.DECLINED
    if decision is DecisionState.DEFER:
        return LifecycleActionability.DEFERRED
    if assurance_action is ActionStatus.EVIDENCE_REQUIRED:
        return LifecycleActionability.EVIDENCE_REQUIRED
    if assurance_action is ActionStatus.DECISION_REQUIRED:
        return LifecycleActionability.DECISION_REQUIRED
    return LifecycleActionability.ACTION_AVAILABLE


def _reason_codes(
    item: OpportunityAssurance,
    *,
    availability: Availability,
    decision: DecisionState,
    execution: ExecutionState,
    timing: UrgencyStatus,
) -> tuple[str, ...]:
    codes: list[str] = []
    if availability is Availability.BLOCKED:
        codes.append(REASON_GOVERNED_EXCLUSION)
    if availability is Availability.CONDITIONALLY_AVAILABLE:
        codes.append(REASON_CONDITIONAL_ELIGIBILITY)
    if availability is Availability.UNDETERMINED:
        codes.append(REASON_ELIGIBILITY_UNDETERMINED)
    if item.status is AssuranceStatus.REVIEW_REQUIRED:
        codes.append(REASON_REVIEW_OWED)
    if item.evidence_readiness in _EVIDENCE_GAP:
        codes.append(REASON_EVIDENCE_INCOMPLETE)
    if timing is UrgencyStatus.EXPIRED:
        codes.append(REASON_DEADLINE_PASSED)
    elif timing is UrgencyStatus.URGENT:
        codes.append(REASON_DEADLINE_URGENT)
    elif timing is UrgencyStatus.APPROACHING:
        codes.append(REASON_DEADLINE_APPROACHING)
    elif timing is UrgencyStatus.NO_DEADLINE:
        codes.append(REASON_NO_DEADLINE)
    if decision is DecisionState.DEFER:
        codes.append(REASON_USER_DEFERRED)
    if decision is DecisionState.DECLINE:
        codes.append(REASON_USER_DECLINED)
    if execution is ExecutionState.USER_REPORTED:
        codes.append(REASON_USER_REPORTED_ACTION)
    if item.freshness == "stale":
        codes.append(REASON_INPUTS_STALE)
    return tuple(codes)


def _attention(
    item: OpportunityAssurance,
    *,
    availability: Availability,
    decision: DecisionState,
    execution: ExecutionState,
    timing: UrgencyStatus,
) -> AttentionFlags:
    """One governed fact per flag.

    A decision is "needed" only while the user has not given one and has not
    reported acting — a declined or deferred opportunity has an answer, even
    if the answer may change. Evidence stops being flagged once the user
    reports acting is NOT the rule: a reported action with missing evidence is
    exactly the state the product must keep visible.
    """
    settled = decision in (
        DecisionState.PROCEED, DecisionState.DEFER, DecisionState.DECLINE)
    return AttentionFlags(
        needs_decision=(
            not settled
            and execution is not ExecutionState.USER_REPORTED
            and availability is not Availability.BLOCKED
            and timing is not UrgencyStatus.EXPIRED
        ),
        needs_evidence=(
            item.evidence_readiness in _EVIDENCE_GAP
            and availability is not Availability.BLOCKED
            and decision is not DecisionState.DECLINE
        ),
        deadline_approaching=timing is UrgencyStatus.APPROACHING,
        deadline_urgent=timing is UrgencyStatus.URGENT,
        expired=timing is UrgencyStatus.EXPIRED,
        blocked=availability is Availability.BLOCKED,
        stale=item.freshness == "stale",
    )


#: Timing band for ordering. The SAME shape Assurance's attention key uses —
#: urgent first, expired last, because the window has closed.
_TIMING_RANK = {
    UrgencyStatus.URGENT: 0,
    UrgencyStatus.APPROACHING: 1,
    UrgencyStatus.NORMAL: 2,
    UrgencyStatus.NO_DEADLINE: 3,
    UrgencyStatus.EXPIRED: 4,
}

#: Actionability band: closable gaps first, settled and unreachable states last.
_ACTION_RANK = {
    LifecycleActionability.EVIDENCE_REQUIRED: 0,
    LifecycleActionability.DECISION_REQUIRED: 1,
    LifecycleActionability.ACTION_AVAILABLE: 2,
    LifecycleActionability.DEFERRED: 3,
    LifecycleActionability.ACTION_REPORTED: 4,
    LifecycleActionability.DECLINED: 5,
    LifecycleActionability.BLOCKED: 6,
    LifecycleActionability.EXPIRED: 7,
}


def _attention_key(entry: OpportunityLifecycle) -> tuple:
    """THE ordering rule, in one place.

    Assurance's key is (urgency, action band, optimizer rank, identity). This
    adds ONE leading term — whether any attention flag is raised — and keeps
    the rest. The addition is deliberate and is the whole reason lifecycle
    ordering differs: an opportunity the user already declined or reported
    acting on raises no flag, and a dashboard that sorted it above an urgent
    unanswered one would be showing settled work first.

    Material-impact ordering stays delegated to `candidate_rank`, the
    optimizer's own sealed verdict. Nothing here re-ranks by amount, which is
    what keeps this a presentation order rather than a second optimizer.
    """
    return (
        0 if entry.attention.any_flag else 1,
        _TIMING_RANK[entry.timing],
        _ACTION_RANK[entry.actionability],
        entry.candidate_rank if entry.candidate_rank is not None else 10**9,
        entry.opportunity_code,
        entry.source_id,
    )


def derive_opportunity_lifecycle(
    assurance: TaxAssuranceMap,
    threads: Sequence[JournalThreadView],
    *,
    as_of: date,
) -> OpportunityLifecycleMap:
    """Assurance + Journal in, lifecycle out. Pure, total, and deterministic.

    O(opportunities + threads) for the join, plus one O(n log n) sort for the
    attention order. The Journal threads are indexed once into a keyed map, so
    no opportunity ever scans the thread list.
    """
    by_subject: dict[str, list[JournalThreadView]] = {}
    unlinked = 0
    for thread in threads:
        if thread.subject is None:
            unlinked += 1
            continue
        by_subject.setdefault(thread.subject, []).append(thread)

    entries: list[OpportunityLifecycle] = []
    for item in assurance.opportunities:
        matched = by_subject.get(item.opportunity_code, [])
        chosen = _choose_thread(matched) if matched else None

        availability = _availability(item)
        decision = (
            DecisionState(chosen.decision.value) if chosen is not None
            else DecisionState.NO_DECISION
        )
        execution = (
            chosen.execution if chosen is not None else ExecutionState.NOT_REPORTED
        )

        entries.append(OpportunityLifecycle(
            opportunity_code=item.opportunity_code,
            source_id=item.source_id,
            availability=availability,
            decision=decision,
            execution=execution,
            evidence=item.evidence_readiness,
            timing=item.urgency,
            freshness=item.freshness,
            stale_reason_codes=item.stale_reason_codes,
            integrity=item.integrity,
            integrity_reason_code=item.integrity_reason_code,
            deadline=item.deadline,
            days_remaining=(
                item.deadline.days_remaining if item.deadline is not None else None
            ),
            actionability=_actionability(
                availability=availability, decision=decision, execution=execution,
                timing=item.urgency, assurance_action=item.action,
            ),
            reason_codes=_reason_codes(
                item, availability=availability, decision=decision,
                execution=execution, timing=item.urgency,
            ),
            attention=_attention(
                item, availability=availability, decision=decision,
                execution=execution, timing=item.urgency,
            ),
            journal=(
                JournalLink(journal_id=chosen.journal_id, thread_count=len(matched))
                if chosen is not None else None
            ),
            last_reported_action_date=(
                chosen.last_reported_action_date if chosen is not None else None
            ),
            standalone_potential=item.standalone_potential,
            incremental_portfolio_benefit=item.incremental_portfolio_benefit,
            candidate_rank=item.candidate_rank,
            support=item.support,
            assumption_dependent=item.assumption_dependent,
        ))

    entries.sort(key=lambda e: (e.opportunity_code, e.source_id))
    attention_order = tuple(
        entry.source_id for entry in sorted(entries, key=_attention_key))

    family = next(
        (f for f in assurance.families if f.family == "OPPORTUNITY"), None)

    return OpportunityLifecycleMap(
        contract_version=OPPORTUNITY_LIFECYCLE_CONTRACT_VERSION,
        tax_year=assurance.tax_year,
        as_of=as_of.isoformat(),
        graph_hash=assurance.graph_hash,
        opportunity_authority=(
            family.status.value if family is not None
            else AssuranceStatus.UNAVAILABLE.value
        ),
        opportunity_authority_reason=(
            family.reason_code if family is not None else "OPPORTUNITY_FAMILY_ABSENT"
        ),
        opportunities=tuple(entries),
        attention_order=attention_order,
        summary=_summarize(entries, unlinked_thread_count=unlinked),
    )


def _summarize(
    entries: Sequence[OpportunityLifecycle], *, unlinked_thread_count: int
) -> LifecycleSummary:
    by_availability = {value.value: 0 for value in Availability}
    by_decision = {value.value: 0 for value in DecisionState}
    by_execution = {value.value: 0 for value in ExecutionState}
    by_timing = {value.value: 0 for value in UrgencyStatus}
    by_actionability = {value.value: 0 for value in LifecycleActionability}

    for entry in entries:
        by_availability[entry.availability.value] += 1
        by_decision[entry.decision.value] += 1
        by_execution[entry.execution.value] += 1
        by_timing[entry.timing.value] += 1
        by_actionability[entry.actionability.value] += 1

    return LifecycleSummary(
        opportunity_count=len(entries),
        by_availability=by_availability,
        by_decision=by_decision,
        by_execution=by_execution,
        by_timing=by_timing,
        by_actionability=by_actionability,
        needing_attention=sum(1 for e in entries if e.attention.any_flag),
        unlinked_thread_count=unlinked_thread_count,
    )
