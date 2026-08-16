"""Tax Decision Journal — the pure domain (Entry: Tax Decision Journal).

FOUR THINGS THAT ARE NOT EACH OTHER, and the reason this module exists:

    SIMULATED          a scenario was sealed. Says nothing about intent.
    USER DECIDED       the user declared PROCEED / DEFER / DECLINE. Intent,
                       not action.
    USER REPORTED      the user says they acted. A self-report, not a fact
                       the system verified.
    SYSTEM-OBSERVED    governed evidence semantics, computed by the certified
                       readiness primitive — supporting state, never proof
                       that the reported action occurred.

Nothing in this module infers one from another. The projection folds an
append-only event history into current state and does exactly that: folding.
No tax, no eligibility, no readiness, no clock.

THE USER IS THE AUTHORITY for every value here. Onyx records declarations; it
does not decide that a user proceeded because they simulated, nor that an
action occurred because tax state moved, nor that a report is verified because
the user made it.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

#: The product read contract's version. Independent of the scenario-result
#: protocol, the Before-You-Act contract and the Assurance contract — this
#: payload has its own consumers and its own shape.
DECISION_JOURNAL_SCHEMA_VERSION = "1.0.0"

#: The persisted event payload's version, stored per row. Versioned separately
#: from the read contract: a stored row must stay readable long after the
#: response shape has moved on.
JOURNAL_EVENT_SCHEMA_VERSION = "1.0.0"


class Decision(StrEnum):
    """What the user has declared. Closed, and deliberately small.

    `CONSIDERING` is the explicit opening state — recorded on the CREATED
    event rather than implied by absence, so an empty answer can never be
    mistaken for a decision. `COMPLETE` is deliberately not a value: nothing
    governed defines completion, and PROCEED is an intent, not an outcome.
    """

    CONSIDERING = "CONSIDERING"
    PROCEED = "PROCEED"
    DEFER = "DEFER"
    DECLINE = "DECLINE"


class JournalEventType(StrEnum):
    """The append-only vocabulary. Three types, each a distinct speech act.

    CREATED opens the thread and pins what informed it; DECISION_RECORDED is a
    declaration of intent (including a change of mind — a NEW event, never a
    rewrite); ACTION_REPORTED is the user saying they acted. Corrections are
    supersession: a later event outranks an earlier one in the projection, and
    the earlier one stays exactly where it was.
    """

    CREATED = "CREATED"
    DECISION_RECORDED = "DECISION_RECORDED"
    ACTION_REPORTED = "ACTION_REPORTED"


class ExecutionState(StrEnum):
    """The execution axis, kept apart from the decision axis.

    Two values, because two are all the governed record supports: the user has
    reported acting, or they have not. There is no SYSTEM_VERIFIED here — no
    governed source can prove an RRSP contribution happened. Evidence
    readiness is exposed BESIDE this state as supporting context, explicitly
    labeled, never folded in.
    """

    NOT_REPORTED = "NOT_REPORTED"
    USER_REPORTED = "USER_REPORTED"


@dataclass(frozen=True)
class JournalEventView:
    """One event, as the projection consumes it. A thin, ORM-free value so the
    projection is testable without a database and deterministic by
    construction."""

    sequence: int
    event_type: JournalEventType
    decision: Decision | None
    user_reported_action_date: date | None
    recorded_at: datetime


@dataclass(frozen=True)
class JournalProjection:
    """The current state, derived from history. Never stored: the events are
    the record, and a stored duplicate is something that can disagree."""

    current_decision: Decision
    decision_recorded_at: datetime
    execution: ExecutionState
    #: The user's claimed action date from the LATEST report, verbatim.
    last_reported_action_date: date | None
    last_action_reported_at: datetime | None
    event_count: int
    last_event_at: datetime


def project(events: Sequence[JournalEventView]) -> JournalProjection:
    """Fold ordered history into current state. Pure and total for any
    non-empty history that begins with CREATED.

    Ordering is by `sequence` — assigned under the thread's row lock at write
    time — so the same events produce the same projection regardless of query
    row order or insertion order. O(n log n) for the defensive sort, O(n) fold.
    """
    if not events:
        raise ValueError("a journal with no events has no state to project")

    ordered = sorted(events, key=lambda e: e.sequence)
    if ordered[0].event_type is not JournalEventType.CREATED:
        raise ValueError("history must begin with the CREATED event")

    current_decision = Decision.CONSIDERING
    decision_recorded_at = ordered[0].recorded_at
    execution = ExecutionState.NOT_REPORTED
    last_reported_action_date: date | None = None
    last_action_reported_at: datetime | None = None

    for event in ordered:
        if event.decision is not None:
            current_decision = event.decision
            decision_recorded_at = event.recorded_at
        if event.event_type is JournalEventType.ACTION_REPORTED:
            execution = ExecutionState.USER_REPORTED
            last_reported_action_date = event.user_reported_action_date
            last_action_reported_at = event.recorded_at

    return JournalProjection(
        current_decision=current_decision,
        decision_recorded_at=decision_recorded_at,
        execution=execution,
        last_reported_action_date=last_reported_action_date,
        last_action_reported_at=last_action_reported_at,
        event_count=len(ordered),
        last_event_at=ordered[-1].recorded_at,
    )
