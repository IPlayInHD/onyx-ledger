"""Retention — the read path and the acknowledgement.

TWO OPERATIONS, AND ONLY ONE OF THEM WRITES.

  `changes_for`      builds current state, loads the latest acknowledged
                     checkpoint, and compares. SIDE-EFFECT FREE. Refreshing ten
                     times returns the same answer ten times, because a read
                     that advanced the baseline would let a background fetch
                     erase changes nobody ever saw.

  `acknowledge`      the ONLY writer. It re-derives current state server-side
                     and refuses unless the client's token matches, so a client
                     can never acknowledge a state it did not review.

WHAT IT DOES NOT DO. It computes no tax, evaluates no rule, runs no optimizer,
resolves no current rule version and reads no document. Current state arrives
through the certified Opportunity Lifecycle and Tax Assurance contracts, which
own those decisions; this module compares two values.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict
from app.database.models import RetentionCheckpoint
from app.services.ioe.lifecycle.service import OpportunityLifecycleService
from app.services.ioe.retention.domain import (
    RetentionChangeSet,
    compare_retention_snapshots,
)
from app.services.ioe.retention.snapshot import (
    RETENTION_SNAPSHOT_SCHEMA_VERSION,
    RetentionSnapshot,
    snapshot_from_payload,
    snapshot_from_product,
    snapshot_hash,
    snapshot_payload,
)


class StaleAcknowledgement(Conflict):
    """The client tried to acknowledge a state that is no longer current, or to
    supersede a baseline that is no longer the latest.

    A `Conflict` rather than a validation error, because nothing about the
    request is malformed — it is a correct request that arrived too late. The
    reason code says which of the two guards refused it, and the caller learns
    nothing about anyone else's data either way.

    Refusing here is the point of §49: the alternative is silently recording
    that the user reviewed changes they never saw, which destroys the only
    thing this engine promises.
    """

    error_type = "https://onyx.ledger/errors/stale-acknowledgement"
    title = "Stale Acknowledgement"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            "The state you are acknowledging is no longer the current state."
        )


#: The client acknowledged a snapshot hash that is not the current one.
REASON_SNAPSHOT_MOVED = "CURRENT_STATE_CHANGED_SINCE_READ"
#: The client's baseline is no longer the latest checkpoint — someone else
#: (another tab, another device) acknowledged first.
REASON_BASELINE_SUPERSEDED = "BASELINE_ALREADY_SUPERSEDED"


@dataclass(frozen=True)
class LoadedCheckpoint:
    """A stored checkpoint plus its decoded snapshot."""

    row: RetentionCheckpoint
    snapshot: RetentionSnapshot


class RetentionService:
    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self.s = session
        self.user_id = user_id

    async def _latest_checkpoint(self, tax_year: int) -> LoadedCheckpoint | None:
        """The newest acknowledged checkpoint, or None on first use.

        ONE row, via the covering index — never a scan of a user's history. A
        `LIMIT 1` over an ordered index is what keeps this O(1) as checkpoints
        accumulate, which matters because every GET pays it.

        The `id` tie-break makes the answer total: two checkpoints cannot share
        an `acknowledged_at` and leave "latest" ambiguous between requests.
        """
        row = await self.s.scalar(
            select(RetentionCheckpoint)
            .where(
                RetentionCheckpoint.user_id == self.user_id,
                RetentionCheckpoint.tax_year == tax_year,
            )
            .order_by(
                RetentionCheckpoint.acknowledged_at.desc(),
                RetentionCheckpoint.id.desc(),
            )
            .limit(1)
        )
        if row is None:
            return None
        # Decoded from the STORED bytes. Nothing here consults current state to
        # fill a gap — that would reconstruct the baseline from the very world
        # it is supposed to be compared against.
        return LoadedCheckpoint(row=row, snapshot=snapshot_from_payload(row.snapshot))

    async def _current_snapshot(
        self, *, tax_year: int, as_of: date
    ) -> RetentionSnapshot:
        """Current product state, through the certified contracts.

        One graph build serves both maps: the Opportunity Lifecycle for
        per-opportunity state, Tax Assurance for family standing.
        """
        assurance, lifecycle = await OpportunityLifecycleService(
            self.s, self.user_id
        ).build_with_assurance(tax_year=tax_year, as_of=as_of)
        # Pure from here on: no statement is issued below this line.
        return snapshot_from_product(lifecycle, assurance, as_of=as_of)

    async def changes_for(
        self, *, tax_year: int, as_of: date
    ) -> tuple[RetentionChangeSet, RetentionCheckpoint | None]:
        """What changed since the last acknowledged checkpoint. Reads only."""
        current = await self._current_snapshot(tax_year=tax_year, as_of=as_of)
        baseline = await self._latest_checkpoint(tax_year)
        changes = compare_retention_snapshots(
            baseline.snapshot if baseline is not None else None,
            current,
            current_hash=snapshot_hash(current),
            acknowledged_hash=baseline.row.snapshot_hash if baseline else None,
        )
        return changes, (baseline.row if baseline is not None else None)

    async def acknowledge(
        self,
        *,
        tax_year: int,
        as_of: date,
        acknowledged_snapshot_hash: str,
        expected_baseline_checkpoint_id: uuid.UUID | None,
        request_id: uuid.UUID,
    ) -> RetentionCheckpoint:
        """Record that the user reviewed the current state. The only writer.

        THREE GUARDS, in order, because they fail for different reasons:

        1. IDEMPOTENCY. A retry carrying the same `request_id` returns the
           checkpoint the first attempt created. Checked first so a network
           retry never trips the staleness guards below — by the time a retry
           arrives, its own write has already moved the world it is compared
           against.

        2. THE STATE MATCHES. The server re-derives current state and refuses
           unless the client's hash is that state's hash. This is what makes
           "acknowledge" mean "I reviewed THIS", and it is the §49 acceptance:
           state that moved between the GET and the POST must not be
           acknowledged.

        3. THE BASELINE MATCHES. The checkpoint the client believed it was
           superseding must still be the latest. Two tabs racing from one
           baseline: the first wins, the second is told why.

        The database enforces (3) again through `uq_retention_checkpoint_chain`,
        because two requests can pass the check concurrently and only one may
        land. A service check alone would be a race with a comfortable name.
        """
        existing = await self.s.scalar(
            select(RetentionCheckpoint).where(
                RetentionCheckpoint.user_id == self.user_id,
                RetentionCheckpoint.request_id == request_id,
            )
        )
        if existing is not None:
            return existing

        current = await self._current_snapshot(tax_year=tax_year, as_of=as_of)
        current_hash = snapshot_hash(current)
        if current_hash != acknowledged_snapshot_hash:
            raise StaleAcknowledgement(REASON_SNAPSHOT_MOVED)

        baseline = await self._latest_checkpoint(tax_year)
        latest_id = baseline.row.id if baseline is not None else None
        if latest_id != expected_baseline_checkpoint_id:
            raise StaleAcknowledgement(REASON_BASELINE_SUPERSEDED)

        checkpoint = RetentionCheckpoint(
            user_id=self.user_id,
            tax_year=tax_year,
            snapshot_schema_version=RETENTION_SNAPSHOT_SCHEMA_VERSION,
            snapshot=snapshot_payload(current),
            snapshot_hash=current_hash,
            evaluated_as_of=as_of,
            supersedes_checkpoint_id=latest_id,
            request_id=request_id,
        )
        self.s.add(checkpoint)
        try:
            await self.s.flush()
        except IntegrityError as exc:
            # The chain constraint. Another acknowledgement superseded the same
            # baseline between our check and our insert, which is precisely the
            # race the constraint exists to lose.
            raise StaleAcknowledgement(REASON_BASELINE_SUPERSEDED) from exc
        return checkpoint
