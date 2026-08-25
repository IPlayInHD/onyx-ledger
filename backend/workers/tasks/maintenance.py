"""Scheduled maintenance tasks (Beat). Real implementations wire the ingestion
and analytics pipelines; these are the registered entry points + schedule."""
from __future__ import annotations

from app.core.logging import get_logger
from app.database.session import unit_of_work
from app.services.admission.service import AdmissionService
from workers.celery_app import celery_app
from workers.runtime import run_task

log = get_logger("onyx.worker")


@celery_app.task(name="workers.tasks.maintenance.check_data_updates")
def check_data_updates() -> None:
    log.info("check_data_updates", note="poll government sources for new datasets")


@celery_app.task(name="workers.tasks.maintenance.roll_monthly_analytics")
def roll_monthly_analytics() -> None:
    log.info("roll_monthly_analytics", note="refresh materialized views; add audit partition")


@celery_app.task(name="workers.tasks.maintenance.purge_admission_history")
def purge_admission_history() -> dict[str, int]:
    """Bound the growth of the admission tables.

    Not optional maintenance. `admission.rate_counter` is keyed partly on what
    an unauthenticated caller TYPES — the login throttle's AUTH_SUBJECT scope —
    so its row count is attacker-controlled, and a table an attacker can grow
    without bound is the denial of service the limiter exists to prevent,
    arriving through the limiter.

    Drains in bounded batches rather than one delete, and stops after a fixed
    number of them so a large backlog is worked down over several runs instead
    of holding locks on the hot path for minutes. Nothing depends on this having
    run: admission already ignores expired leases and closed windows, so a
    missed run costs disk, never correctness.
    """
    async def _drain() -> dict[str, int]:
        totals = {"rate_counters_deleted": 0, "leases_deleted": 0}
        for _ in range(20):
            async with unit_of_work(actor_type="system") as session:
                removed = await AdmissionService(session).purge()
            for key, value in removed.items():
                totals[key] += value
            if not any(removed.values()):
                break
        return totals

    result = run_task(_drain)
    log.info("purge_admission_history", **result)
    return result


@celery_app.task(name="workers.tasks.maintenance.import_new_legislation")
def import_new_legislation() -> None:
    log.info("import_new_legislation", note="open next tax_year partitions; ingest new rules")
