"""The privacy lifecycle worker (Entry 11B5).

WHAT WAS MISSING. Entry 11B2 built `AccountLifecycleService.claim/advance/fail`
and Entry 11B5C built the source-purge keyhole, and until this module NOTHING
drove either of them — the only callers were tests. An account could be walked
to `PURGE_PENDING` by a request and would sit there forever, because no process
existed to pick it up. This is that process.

IDENTIFIERS ONLY. The task takes no arguments at all: it claims its own work
from the database. There is nothing to leak through `args`, `kwargs`, a result
payload or a retry record, because nothing about a subject travels through
Celery — the worker learns which account to purge by asking PostgreSQL, and
learns nothing else about it. Entry 11A turned off result storage and error
storage; this keeps there being nothing worth storing.

NO DELETION SQL LIVES HERE. Every statement that removes a row is inside
`identity.purge_source_data`, which takes one subject and runs under that
subject's own row-level security. The worker cannot express "every user".
"""
from __future__ import annotations

import asyncio

from app.core.logging import get_logger
from app.database.session import unit_of_work
from app.services.privacy import (
    AccountLifecycleService,
    LifecycleState,
    SourceDataPhase,
    SourceDataPurgeService,
)
from workers.celery_app import celery_app

log = get_logger("onyx.worker.privacy")

#: How many subjects one run will take. Bounded for the same reason every other
#: claim in this system is: a worker that claimed the whole backlog would hold
#: it all under one lease and strand every subject if it died.
_BATCH = 10


@celery_app.task(name="workers.tasks.privacy.run_account_deletion_phases")
def run_account_deletion_phases(worker_id: str = "privacy-worker") -> dict[str, int]:
    """Claim accounts owed privacy work and run the phase each one is in.

    Returns COUNTS, and only counts. A subject id in a task return value would
    outlive the task in whatever collects it.

    The claim function releases leases older than the timeout, so a worker that
    died mid-phase strands nothing — the next run reclaims it. That is why this
    is safe to schedule rather than to trigger.
    """

    async def _run() -> dict[str, int]:
        totals = {"claimed": 0, "advanced": 0, "purged": 0, "incomplete": 0}
        async with unit_of_work(actor_type="system") as session:
            claimed = await AccountLifecycleService(session).claim(
                worker_id=worker_id, batch_size=_BATCH)
        totals["claimed"] = len(claimed)

        for item in claimed:
            # Each subject gets its own transaction. One subject whose purge
            # fails must not roll back the purge of the subject before it —
            # that is the difference between a phase that converges on retry
            # and one that starts over.
            async with unit_of_work(actor_type="system") as session:
                lifecycle = AccountLifecycleService(session)

                # Walk the cheap states forward. ACCESS_DISABLED and
                # PURGE_PENDING are bookkeeping; PURGING is where work happens.
                if item.state is LifecycleState.DELETION_REQUESTED:
                    if await lifecycle.advance(
                        item, LifecycleState.ACCESS_DISABLED, worker_id=worker_id
                    ):
                        totals["advanced"] += 1
                    continue
                if item.state is LifecycleState.ACCESS_DISABLED:
                    if await lifecycle.advance(
                        item, LifecycleState.PURGE_PENDING, worker_id=worker_id
                    ):
                        totals["advanced"] += 1
                    continue
                if item.state is LifecycleState.PURGE_PENDING:
                    if await lifecycle.advance(
                        item, LifecycleState.PURGING, worker_id=worker_id
                    ):
                        totals["advanced"] += 1
                    continue

                if item.state is not LifecycleState.PURGING:
                    continue

                outcome = await SourceDataPurgeService(session).run(
                    item, worker_id=worker_id,
                    phase=SourceDataPhase.SOURCE_DATA)

                if outcome.completed:
                    totals["purged"] += 1
                elif outcome.failure_code == "SOURCE_DATA_INCOMPLETE":
                    totals["incomplete"] += 1

                # THE ACCOUNT IS NOT ADVANCED PAST PURGING HERE, and that is
                # the point. SOURCE_DATA finishing means the source data is
                # gone; documents, audit and authentication de-identification
                # are separate phases that do not exist yet. An account marked
                # COMPLETE now would be a status that lies in the direction
                # that matters — `advance` refuses COMPLETE outright for the
                # same reason.

                # A closed code, never a subject id and never exception text.
                log.info("privacy_phase",
                         phase=outcome.phase.value,
                         completed=outcome.completed,
                         reason=outcome.failure_code or "OK")
        return totals

    return asyncio.run(_run())
