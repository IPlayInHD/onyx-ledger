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

from app.core.logging import get_logger
from app.database.privacy_session import privacy_unit_of_work
from app.services.privacy import (
    AccountLifecycleService,
    AuditAuthDeidentificationService,
    DocumentPurgeService,
    HistoricalDetailCleanupService,
    LifecycleState,
    ScenarioRetentionService,
    SourceDataPhase,
    SourceDataPurgeService,
    phase_is_complete,
)
from workers.celery_app import celery_app
from workers.runtime import run_task

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
        async with privacy_unit_of_work() as session:
            claimed = await AccountLifecycleService(session).claim(
                worker_id=worker_id, batch_size=_BATCH)
        totals["claimed"] = len(claimed)

        for item in claimed:
            # Each subject gets its own transaction. One subject whose purge
            # fails must not roll back the purge of the subject before it —
            # that is the difference between a phase that converges on retry
            # and one that starts over.
            async with privacy_unit_of_work() as session:
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

                # ONE PHASE PER CLAIM, decided from the durable phase record
                # rather than from anything this process remembers. The worker
                # that finished the previous phase may have been a different one
                # that has since died, so "what is this account owed" is a
                # question only the database can answer.
                #
                # THE ORDER IS FIXED AND THE PHASES ARE INDEPENDENT. Source data
                # first because it is the largest and most visible removal;
                # then scenarios, which is tax-evidence retention; then
                # audit/auth identity severance last, because it is the step
                # that makes retained history non-attributable and there is no
                # reason to do it before the things that still need to be found
                # by subject. None of the three reads a surface another one
                # mutates, so the order is a convention rather than a
                # dependency — written down here because "deterministic" is
                # only useful if it is stated.
                if not await phase_is_complete(
                    session, item.user_id, SourceDataPhase.SOURCE_DATA
                ):
                    outcome = await SourceDataPurgeService(session).run(
                        item, worker_id=worker_id,
                        phase=SourceDataPhase.SOURCE_DATA)
                elif not await phase_is_complete(
                    session, item.user_id, SourceDataPhase.DOCUMENTS
                ):
                    # DOCUMENTS after SOURCE_DATA and before the rest, because
                    # of a real dependency rather than taste: confirming a
                    # document freezes tax facts, and those facts must already
                    # be frozen before the source binary disappears. SOURCE_DATA
                    # finishing is what establishes that no live financial row
                    # is still waiting on a document.
                    outcome = await DocumentPurgeService(session).run(
                        item, worker_id=worker_id)
                elif not await phase_is_complete(
                    session, item.user_id, SourceDataPhase.SCENARIO_RETENTION
                ):
                    outcome = await ScenarioRetentionService(session).run(
                        item, worker_id=worker_id)
                elif not await phase_is_complete(
                    session, item.user_id, SourceDataPhase.HISTORICAL_DETAIL_CLEANUP
                ):
                    # HISTORICAL_DETAIL_CLEANUP after SCENARIO_RETENTION, and
                    # this one IS a dependency. Scenario retention deletes whole
                    # scenarios that never sealed a result; running the detail
                    # cleanup first would delete their `scenario_result` rows and
                    # leave retention with less to find, which is harmless, but
                    # the reverse — retention first — means the cleanup only ever
                    # sees scenarios that are staying, so what it removes is
                    # exactly the derived detail of retained artifacts. That is
                    # the property the completion guard counts.
                    outcome = await HistoricalDetailCleanupService(session).run(
                        item, worker_id=worker_id)
                else:
                    outcome = await AuditAuthDeidentificationService(session).run(
                        item, worker_id=worker_id)

                if outcome.completed:
                    totals["purged"] += 1
                elif outcome.failure_code in (
                    "SOURCE_DATA_INCOMPLETE", "AUDIT_AUTH_INCOMPLETE",
                    "SCENARIO_RETENTION_INCOMPLETE", "DOCUMENTS_INCOMPLETE",
                ):
                    totals["incomplete"] += 1

                # THE ACCOUNT IS NOT ADVANCED PAST PURGING HERE, and that is
                # still the point even now that two phases run. Source data
                # gone and audit/auth attribution severed is not the same as
                # deletion finished: DOCUMENTS does not exist, the scenario
                # cleanup does not exist, and 63 privacy surfaces are
                # unclassified. An account marked COMPLETE now would be a
                # status that lies in the direction that matters — `advance`
                # refuses COMPLETE outright for the same reason.

                # A closed code, never a subject id and never exception text.
                log.info("privacy_phase",
                         phase=outcome.phase.value,
                         completed=outcome.completed,
                         reason=outcome.failure_code or "OK")
        return totals

    # Engine lifecycle lives in workers.runtime: a Celery worker calls this
    # task many times in one process, and `asyncio.run` closes a loop the
    # module-level pools outlive. See workers/runtime.py for the measurements.
    return run_task(_run)
