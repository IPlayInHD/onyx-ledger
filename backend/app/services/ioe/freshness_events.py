"""Freshness invalidation events (§P6 closure item 1).

**Producers** call `emit()` inside the transaction that makes the change. The
event row commits or rolls back with that change, so it exists if and only if
the change actually happened. That is the whole reason this is a table and not a
broker publish: a broker call outside the transaction can succeed while the
change rolls back, or be lost while the change commits, and both failure modes
are silent.

**Consumers** are in `freshness_relay.py`. They claim events through a narrow
privileged interface, then apply each one in an ORDINARY, RLS-protected
transaction scoped to the single tenant the event names. Nothing about the
staling itself is privileged.

Sealed evidence is never modified. A result stays true of the baseline it was
measured against; staleness is a label on top of it, not a correction to it.

Read-time evaluation and the hourly sweep remain, but as safeguards. This path
is the normal one. Celery and Redis are downstream transport only — this table
is the source of truth for what happened.
"""
from __future__ import annotations

import uuid
from enum import StrEnum
from typing import cast

from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import FreshnessOutbox
from app.services.ioe.domain.scenario import StaleReason

FRESHNESS_EVENTS_VERSION = "1.0.0"

MAX_ATTEMPTS = 5

ERROR_UNHANDLED_EVENT = "UNHANDLED_EVENT_TYPE"
ERROR_RELAY_FAILED = "RELAY_FAILED"


class FreshnessEvent(StrEnum):
    ANALYSIS_COMPLETED = "analysis_completed"
    ANALYSIS_SUPERSEDED = "analysis_superseded"
    BASELINE_INPUTS_CHANGED = "baseline_inputs_changed"
    FINANCIAL_DATA_CHANGED = "financial_data_changed"
    PROFILE_CHANGED = "profile_changed"
    DOCUMENT_STATUS_CHANGED = "document_status_changed"
    RULE_PUBLISHED = "rule_published"
    RULE_WITHDRAWN = "rule_withdrawn"
    RULE_SUPERSEDED = "rule_superseded"
    REFERENCE_DATA_CHANGED = "reference_data_changed"
    ENGINE_VERSION_CHANGED = "engine_version_changed"
    OBJECTIVE_POLICY_CHANGED = "objective_policy_changed"
    LEVER_REGISTRY_CHANGED = "lever_registry_changed"
    ASSUMPTION_REGISTRY_CHANGED = "assumption_registry_changed"
    RELATIONSHIP_REGISTRY_CHANGED = "relationship_registry_changed"
    SUPPORT_SCORE_POLICY_CHANGED = "support_score_policy_changed"
    PROJECTION_METHODOLOGY_CHANGED = "projection_methodology_changed"


# The stale reason each event implies. Resolved here, at the producer, because
# the producer knows what actually moved; a consumer inferring it would be
# guessing at the cause from the symptom.
EVENT_STALE_REASON: dict[FreshnessEvent, StaleReason] = {
    # A newly completed analysis supersedes older ones for that year. The
    # reason names THAT, not "your inputs moved" — see StaleReason.
    FreshnessEvent.ANALYSIS_COMPLETED: StaleReason.NEWER_ANALYSIS_AVAILABLE,
    FreshnessEvent.ANALYSIS_SUPERSEDED: StaleReason.NEWER_ANALYSIS_AVAILABLE,
    FreshnessEvent.BASELINE_INPUTS_CHANGED: StaleReason.BASELINE_INPUTS_CHANGED,
    FreshnessEvent.FINANCIAL_DATA_CHANGED: StaleReason.BASELINE_INPUTS_CHANGED,
    FreshnessEvent.PROFILE_CHANGED: StaleReason.BASELINE_INPUTS_CHANGED,
    # a document changing status changes the EVIDENCE behind eligibility, which
    # is a different fact from the inputs themselves
    FreshnessEvent.DOCUMENT_STATUS_CHANGED: StaleReason.BASELINE_RESULT_CHANGED,
    FreshnessEvent.RULE_PUBLISHED: StaleReason.RULE_SNAPSHOT_SUPERSEDED,
    FreshnessEvent.RULE_WITHDRAWN: StaleReason.RULE_SNAPSHOT_SUPERSEDED,
    FreshnessEvent.RULE_SUPERSEDED: StaleReason.RULE_SNAPSHOT_SUPERSEDED,
    FreshnessEvent.REFERENCE_DATA_CHANGED: StaleReason.REFERENCE_DATA_CHANGED,
    FreshnessEvent.ENGINE_VERSION_CHANGED: StaleReason.ENGINE_VERSION_CHANGED,
    FreshnessEvent.OBJECTIVE_POLICY_CHANGED: StaleReason.OBJECTIVE_POLICY_CHANGED,
    FreshnessEvent.LEVER_REGISTRY_CHANGED: StaleReason.LEVER_REGISTRY_CHANGED,
    FreshnessEvent.ASSUMPTION_REGISTRY_CHANGED: StaleReason.ASSUMPTION_SET_CHANGED,
    FreshnessEvent.RELATIONSHIP_REGISTRY_CHANGED: (
        StaleReason.RELATIONSHIP_REGISTRY_CHANGED),
    FreshnessEvent.SUPPORT_SCORE_POLICY_CHANGED: (
        StaleReason.SUPPORT_SCORE_POLICY_CHANGED),
    FreshnessEvent.PROJECTION_METHODOLOGY_CHANGED: (
        StaleReason.PROJECTION_METHODOLOGY_CHANGED),
}


async def emit(
    session: AsyncSession,
    event: FreshnessEvent,
    *,
    user_id: uuid.UUID | None = None,
    analysis_id: uuid.UUID | None = None,
    tax_year: int | None = None,
    jurisdiction: str | None = None,
    dedupe_key: str | None = None,
) -> bool:
    """Write an invalidation event IN THE CALLER'S TRANSACTION.

    Returns False when the event was already recorded — a duplicate is not an
    error, it is the expected outcome of a retry.

    The caller must not commit specially for this: the point is that the event
    shares the fate of the change that produced it.
    """
    key = dedupe_key or _default_key(event, user_id, analysis_id, tax_year)

    # ON CONFLICT DO NOTHING rather than catching an IntegrityError inside a
    # SAVEPOINT: rolling a savepoint back also discards work the caller did
    # earlier in the same transaction, so two events emitted by one business
    # change would lose the first when the second collided. Letting PostgreSQL
    # absorb the conflict keeps the caller's transaction untouched, which is the
    # whole point of the event sharing that transaction.
    inserted = await session.scalar(
        # `__table__` is declared as `FromClause` on the declarative base but
        # is always a `Table`, which is what `insert()` needs.
        pg_insert(cast("Table", FreshnessOutbox.__table__))
        .values(
            event_type=event.value,
            stale_reason_code=EVENT_STALE_REASON[event].value,
            user_id=user_id,
            analysis_id=analysis_id,
            tax_year=tax_year,
            jurisdiction=jurisdiction,
            dedupe_key=key,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(FreshnessOutbox.__table__.c.id)
    )
    return inserted is not None


def _default_key(
    event: FreshnessEvent,
    user_id: uuid.UUID | None,
    analysis_id: uuid.UUID | None,
    tax_year: int | None,
) -> str:
    """A key that identifies the logical event, not the attempt.

    Deliberately excludes any timestamp: two emissions describing the same
    change must collide rather than both be delivered.
    """
    return ":".join([
        event.value,
        str(user_id or "-"),
        str(analysis_id or "-"),
        str(tax_year or "-"),
    ])


__all__ = [
    "ERROR_RELAY_FAILED",
    "EVENT_STALE_REASON",
    "FRESHNESS_EVENTS_VERSION",
    "MAX_ATTEMPTS",
    "FreshnessEvent",
    "emit",
]
