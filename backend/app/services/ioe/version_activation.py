"""Authoritative calculation-version activation (Entry 9).

The first cut of this emitted version-change events from application startup.
That is unsafe in production and the reason is worth stating plainly: a process
restart is not a version activation. Several replicas start at once, workers
import the same configuration, a rolling deploy restarts everything repeatedly,
and a process can restart with nothing changed at all. Any of those would have
produced events — or, worse, produced them from a process that started before
migrations finished.

Activation is therefore a **database** decision, not a process decision. The
active version of each component is a row; activating means atomically comparing
and swapping that row and emitting exactly one event in the same transaction.

    begin
    → SELECT ... FOR UPDATE the component's active-version row
    → identical? do nothing, emit nothing, report already-active
    → different? update it, bump the revision, emit ONE event
    → commit

`FOR UPDATE` is what makes ten concurrent replicas produce one activation: the
nine losers block, then re-read the row the winner wrote and take the
already-active path. The event shares the transaction, so a failed insert rolls
the activation back and the two can never disagree.

Reuses the existing outbox — there is no second event bus here, and Celery is
not the durable signal.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import ActiveCalculationVersion
from app.services.ioe.freshness_events import FreshnessEvent, emit

log = get_logger("onyx.ioe.version_activation")

VERSION_ACTIVATION_VERSION = "1.0.0"


@dataclass(frozen=True)
class ActivationOutcome:
    """What one activation attempt did. Counts and bounded codes only."""

    version_type: str
    previous_version: str | None
    active_version: str
    activation_revision: int
    activated: bool
    event_emitted: bool

    @property
    def already_active(self) -> bool:
        return not self.activated


class VersionActivationService:
    """Compare-and-swap the active version of one calculation component."""

    def __init__(self, session: AsyncSession):
        self.s = session

    async def activate(
        self, version_type: FreshnessEvent | str, version: str, *,
        tax_year: int | None = None,
    ) -> ActivationOutcome:
        """Idempotent, concurrency-safe activation inside the caller's transaction.

        Returns without emitting when `version` is already active — which is the
        common case on restart, and the whole reason this is not startup logic.
        """
        event = FreshnessEvent(version_type)
        kind = event.value

        # Lock the component row FIRST. A concurrent activation of the same
        # component blocks here and then observes the winner's value, so the
        # comparison below is made against committed state rather than a
        # snapshot that is about to be stale.
        row = await self.s.scalar(
            select(ActiveCalculationVersion)
            .where(ActiveCalculationVersion.version_type == kind)
            .with_for_update()
        )

        if row is None:
            # First activation of this component. A unique constraint on
            # version_type arbitrates a genuine race here; the loser retries
            # through the ordinary path on its next call.
            row = ActiveCalculationVersion(
                version_type=kind, active_version=version, activation_revision=1)
            self.s.add(row)
            await self.s.flush()
            emitted = await self._emit(event, kind, None, version, 1, tax_year)
            return ActivationOutcome(kind, None, version, 1, True, emitted)

        if row.active_version == version:
            # Nothing moved. No update, no event, no timestamp churn.
            return ActivationOutcome(
                kind, row.active_version, row.active_version,
                row.activation_revision, False, False)

        previous = row.active_version
        row.active_version = version
        row.activation_revision = row.activation_revision + 1
        await self.s.flush()
        emitted = await self._emit(
            event, kind, previous, version, row.activation_revision, tax_year)
        return ActivationOutcome(
            kind, previous, version, row.activation_revision, True, emitted)

    async def _emit(
        self, event: FreshnessEvent, kind: str, previous: str | None,
        version: str, revision: int, tax_year: int | None,
    ) -> bool:
        """One durable event, keyed on the transition rather than the clock.

        The key carries the component, the new version and the revision, so a
        retry of the same transition collides while a genuine A→B→A cycle does
        not (the revision differs).
        """
        return await emit(
            self.s, event, tax_year=tax_year,
            dedupe_key=f"activation:{kind}:{version}:{revision}",
        )

    async def active_version(self, version_type: FreshnessEvent | str) -> str | None:
        kind = FreshnessEvent(version_type).value
        row = await self.s.scalar(
            select(ActiveCalculationVersion)
            .where(ActiveCalculationVersion.version_type == kind))
        return row.active_version if row else None


async def advisory_lock(session: AsyncSession, key: int) -> None:
    """Serialize first-activation races across components (transaction-scoped)."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


__all__ = [
    "VERSION_ACTIVATION_VERSION",
    "ActivationOutcome",
    "VersionActivationService",
    "advisory_lock",
]
