"""Scheduled maintenance tasks (Beat). Real implementations wire the ingestion
and analytics pipelines; these are the registered entry points + schedule."""
from __future__ import annotations

from app.core.logging import get_logger
from workers.celery_app import celery_app

log = get_logger("onyx.worker")


@celery_app.task(name="workers.tasks.maintenance.check_data_updates")
def check_data_updates() -> None:
    log.info("check_data_updates", note="poll government sources for new datasets")


@celery_app.task(name="workers.tasks.maintenance.roll_monthly_analytics")
def roll_monthly_analytics() -> None:
    log.info("roll_monthly_analytics", note="refresh materialized views; add audit partition")


@celery_app.task(name="workers.tasks.maintenance.import_new_legislation")
def import_new_legislation() -> None:
    log.info("import_new_legislation", note="open next tax_year partitions; ingest new rules")
