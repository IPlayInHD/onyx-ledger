"""Ingestion pipeline orchestrator — composes the stage services in order.

parse → extract → promote → validate → compare. Shared by the Celery workers
(each stage a task) and by the admin API's synchronous import path, so both run
exactly the same governed pipeline. Every stage is idempotent, so a re-run
resumes rather than restarts.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.tkms.comparison.service import ComparisonService
from app.services.tkms.extraction.service import ExtractionService
from app.services.tkms.ingestion.service import ImportService
from app.services.tkms.validation.service import ValidationService


async def run_ingestion_pipeline(
    session: AsyncSession, job_id: uuid.UUID, *, registry=None, storage=None
) -> dict:
    """Run parse→extract→promote→validate→compare for a stored import job."""
    imp = ImportService(session, storage=storage, registry=registry)
    await imp.parse(job_id)
    await imp.extract(job_id)

    versions = await ExtractionService(session).promote(job_id)
    report = await ValidationService(session).validate_job(job_id)

    comparison = ComparisonService(session)
    change_reports = [await comparison.compare(v.id) for v in versions]

    return {
        "job_id": str(job_id),
        "draft_version_ids": [str(v.id) for v in versions],
        "validation_status": report.status,
        "validation_report_id": str(report.id),
        "change_report_ids": [str(cr.id) for cr in change_reports],
    }
