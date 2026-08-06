"""Producer helpers for freshness invalidation (§P6 closure item 1).

Each helper is a one-line call a producer makes inside its own transaction. They
exist so a caller does not have to know which `StaleReason` a change implies —
getting that mapping wrong at one call site would produce a correct-looking
event with a misleading cause.

The version-change producers are different in kind from the rest: they are not
triggered by a user action but by a DEPLOYMENT. `emit_version_changes()` compares
the versions currently running against the versions last recorded, and emits only
for what actually moved. Running it at startup means an engine or registry change
invalidates affected results once, on the deploy that introduced it, rather than
being noticed weeks later by a sweep.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import FreshnessOutbox
from app.services.ioe import projection
from app.services.ioe.domain import assumptions as assumption_registry
from app.services.ioe.domain import confidence, relationships
from app.services.ioe.domain import levers as lever_registry
from app.services.ioe.domain import savings as savings_domain
from app.services.ioe.freshness_events import FreshnessEvent, emit
from app.services.tax_engine.core import data as engine_data
from app.services.tax_engine.service import ENGINE_VERSION


async def on_financial_data_changed(
    session: AsyncSession, user_id: uuid.UUID, tax_year: int, *, change_token: str
) -> bool:
    """Income, expense, or holding data moved for this user and year.

    `change_token` distinguishes one edit from the next — usually the changed
    row's id — so successive edits each invalidate, while a retry of the same
    edit collides on the dedupe key.
    """
    return await emit(
        session, FreshnessEvent.FINANCIAL_DATA_CHANGED,
        user_id=user_id, tax_year=tax_year,
        dedupe_key=f"financial:{user_id}:{tax_year}:{change_token}",
    )


async def on_profile_changed(
    session: AsyncSession, user_id: uuid.UUID, *, change_token: str
) -> bool:
    """Province, marital status, or another profile fact that gates eligibility."""
    return await emit(
        session, FreshnessEvent.PROFILE_CHANGED,
        user_id=user_id,
        dedupe_key=f"profile:{user_id}:{change_token}",
    )


async def on_document_status_changed(
    session: AsyncSession, user_id: uuid.UUID, document_id: uuid.UUID, status: str
) -> bool:
    """A document became verified, or stopped being.

    This changes the EVIDENCE behind an eligibility determination rather than
    the inputs, which is why it carries its own stale reason.
    """
    return await emit(
        session, FreshnessEvent.DOCUMENT_STATUS_CHANGED,
        user_id=user_id,
        dedupe_key=f"document:{document_id}:{status}",
    )


async def on_analysis_superseded(
    session: AsyncSession, user_id: uuid.UUID, *, analysis_id: uuid.UUID,
    tax_year: int,
) -> bool:
    """A newer completed analysis supersedes older ones for this user and year.

    Distinct from `ANALYSIS_COMPLETED`'s implicit meaning: this names the reason
    a reader acts on. `NEWER_ANALYSIS_AVAILABLE` sends a user to open the new
    analysis; `BASELINE_INPUTS_CHANGED` would send them to check their figures,
    which is a different and wrong instruction.

    Scoped by tax year, so completing a 2025 analysis leaves 2024 results alone.
    """
    return await emit(
        session, FreshnessEvent.ANALYSIS_SUPERSEDED,
        user_id=user_id, tax_year=tax_year,
        dedupe_key=f"analysis_superseded:{analysis_id}",
    )


async def on_rule_withdrawn(
    session: AsyncSession, rule_version_id: uuid.UUID, tax_year: int
) -> bool:
    return await emit(
        session, FreshnessEvent.RULE_WITHDRAWN,
        tax_year=tax_year,
        dedupe_key=f"rule_withdrawn:{rule_version_id}",
    )


async def on_reference_data_changed(
    session: AsyncSession, tax_year: int, *, version: str
) -> bool:
    return await emit(
        session, FreshnessEvent.REFERENCE_DATA_CHANGED,
        tax_year=tax_year,
        dedupe_key=f"reference_data:{tax_year}:{version}",
    )


# ---------------------------------------------------------------------------
# Deployment-triggered version changes
# ---------------------------------------------------------------------------
#: The versions whose movement invalidates stored results, and the event each
#: implies. Adding a versioned component here is what makes its changes visible.
RUNNING_VERSIONS: dict[FreshnessEvent, Callable[[], str]] = {
    FreshnessEvent.ENGINE_VERSION_CHANGED: lambda: ENGINE_VERSION,
    FreshnessEvent.REFERENCE_DATA_CHANGED: lambda: engine_data.REFERENCE_DATA_VERSION,
    FreshnessEvent.OBJECTIVE_POLICY_CHANGED: lambda: (
        f"{savings_domain.PORTFOLIO_OBJECTIVE_CODE.value}"
        f"@{savings_domain.PORTFOLIO_OBJECTIVE_VERSION}"
    ),
    FreshnessEvent.LEVER_REGISTRY_CHANGED: lambda: lever_registry.LEVER_REGISTRY_VERSION,
    FreshnessEvent.ASSUMPTION_REGISTRY_CHANGED: assumption_registry.registry_version,
    FreshnessEvent.RELATIONSHIP_REGISTRY_CHANGED: (
        lambda: relationships.RELATIONSHIP_REGISTRY_VERSION),
    FreshnessEvent.SUPPORT_SCORE_POLICY_CHANGED: (
        lambda: confidence.CONFIDENCE_ALGORITHM_VERSION),
    FreshnessEvent.PROJECTION_METHODOLOGY_CHANGED: (
        lambda: projection.PROJECTION_METHODOLOGY_VERSION),
}


async def emit_version_changes(session: AsyncSession) -> list[FreshnessEvent]:
    """Emit an event for each versioned component that has moved since last seen.

    Idempotent by construction: the dedupe key contains the NEW version, so
    running this on every startup emits at most once per component per version,
    however many times the process restarts.
    """
    emitted: list[FreshnessEvent] = []
    for event, resolve in RUNNING_VERSIONS.items():
        version = resolve()
        key = f"version:{event.value}:{version}"
        already = await session.scalar(
            select(FreshnessOutbox.id).where(FreshnessOutbox.dedupe_key == key)
        )
        if already is not None:
            continue
        # The FIRST time a version is seen is not a change — there is nothing
        # older to invalidate — so a component with no prior record is recorded
        # without invalidating anything.
        seen_before = await session.scalar(
            select(FreshnessOutbox.id).where(
                FreshnessOutbox.event_type == event.value
            ).limit(1)
        )
        if await emit(session, event, dedupe_key=key) and seen_before is None:
            # mark it processed so the relay does not act on a first sighting
            row = await session.scalar(
                select(FreshnessOutbox).where(FreshnessOutbox.dedupe_key == key)
            )
            if row is not None:
                from datetime import UTC, datetime

                row.processed_at = datetime.now(tz=UTC)
        emitted.append(event)
    await session.flush()
    return emitted


__all__ = [
    "RUNNING_VERSIONS",
    "emit_version_changes",
    "on_analysis_superseded",
    "on_document_status_changed",
    "on_financial_data_changed",
    "on_profile_changed",
    "on_reference_data_changed",
    "on_rule_withdrawn",
]
