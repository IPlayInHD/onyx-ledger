"""TKMS Celery tasks — one per pipeline stage, on independently-scaled queues.

Each stage persists its output, so a retry resumes rather than restarts. On
terminal failure (retries exhausted) the task records a tkms.dead_letter row and
pins the job's status, so a poison document never wedges the pipeline. Task
bodies are thin: they bridge Celery (sync) to the async stage services via
asyncio.run inside an admin-scoped unit of work, and chain the next stage.
"""
from __future__ import annotations

import asyncio
import uuid

from app.database.models import DeadLetter, ImportJob
from app.database.session import unit_of_work
from app.services.tkms.comparison.service import ComparisonService
from app.services.tkms.extraction.service import ExtractionService
from app.services.tkms.ingestion.service import ImportService
from app.services.tkms.validation.service import ValidationService
from workers.celery_app import celery_app

# retry budget per stage (mirrors the architecture's queue table)
_MAX_RETRIES = {
    "parse": 3, "extract": 3, "promote": 3,
    "validate": 2, "compare": 2, "index": 5, "notify": 5,
}


def _run(coro):
    return asyncio.run(coro)


async def _dead_letter(task_name: str, queue: str, job_id: str, error: str, attempts: int) -> None:
    async with unit_of_work(actor_type="admin") as s:
        s.add(DeadLetter(
            task_name=task_name, queue=queue,
            payload={"job_id": job_id}, error=error[:4000], attempts=attempts,
            import_job_id=uuid.UUID(job_id),
        ))
        job = await s.get(ImportJob, uuid.UUID(job_id))
        if job is not None:
            job.status = "failed"
            job.error = f"{task_name}: {error}"[:2000]


def _handle_failure(self, stage: str, queue: str, job_id: str, exc: Exception):
    """Retry with exponential backoff; on exhaustion, dead-letter and stop."""
    if self.request.retries < _MAX_RETRIES[stage]:
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
    _run(_dead_letter(f"workers.tasks.tkms.{stage}", queue, job_id,
                      str(exc), self.request.retries))


@celery_app.task(name="workers.tasks.tkms.parse", bind=True, max_retries=3)
def parse(self, job_id: str) -> str:
    async def _do():
        async with unit_of_work(actor_type="admin") as s:
            await ImportService(s).parse(uuid.UUID(job_id))
        return job_id
    try:
        result = _run(_do())
        extract.delay(job_id)
        return result
    except Exception as exc:  # noqa: BLE001
        _handle_failure(self, "parse", "tkms_parse", job_id, exc)
        return job_id


@celery_app.task(name="workers.tasks.tkms.extract", bind=True, max_retries=3)
def extract(self, job_id: str) -> str:
    async def _do():
        async with unit_of_work(actor_type="admin") as s:
            await ImportService(s).extract(uuid.UUID(job_id))
        return job_id
    try:
        result = _run(_do())
        promote.delay(job_id)
        return result
    except Exception as exc:  # noqa: BLE001
        _handle_failure(self, "extract", "tkms_extract", job_id, exc)
        return job_id


@celery_app.task(name="workers.tasks.tkms.promote", bind=True, max_retries=3)
def promote(self, job_id: str) -> str:
    async def _do():
        async with unit_of_work(actor_type="admin") as s:
            await ExtractionService(s).promote(uuid.UUID(job_id))
        return job_id
    try:
        result = _run(_do())
        validate.delay(job_id)
        return result
    except Exception as exc:  # noqa: BLE001
        _handle_failure(self, "promote", "tkms_extract", job_id, exc)
        return job_id


@celery_app.task(name="workers.tasks.tkms.validate", bind=True, max_retries=2)
def validate(self, job_id: str) -> str:
    async def _do():
        async with unit_of_work(actor_type="admin") as s:
            await ValidationService(s).validate_job(uuid.UUID(job_id))
        return job_id
    try:
        result = _run(_do())
        compare.delay(job_id)
        return result
    except Exception as exc:  # noqa: BLE001
        _handle_failure(self, "validate", "tkms_validate", job_id, exc)
        return job_id


@celery_app.task(name="workers.tasks.tkms.compare", bind=True, max_retries=2)
def compare(self, job_id: str) -> str:
    async def _do():
        async with unit_of_work(actor_type="admin") as s:
            from sqlalchemy import select

            from app.database.models import ExtractedRule as ERow
            rows = list(await s.scalars(
                select(ERow.promoted_version_id).where(ERow.import_job_id == uuid.UUID(job_id))
            ))
            comparison = ComparisonService(s)
            for vid in rows:
                if vid is not None:
                    await comparison.compare(vid)
        return job_id
    try:
        return _run(_do())
    except Exception as exc:  # noqa: BLE001
        _handle_failure(self, "compare", "tkms_compare", job_id, exc)
        return job_id


@celery_app.task(name="workers.tasks.tkms.reindex", bind=True, max_retries=5)
def reindex(self, tax_year: int) -> int:
    async def _do():
        async with unit_of_work(actor_type="admin") as s:
            from app.services.ai.retrieval import KnowledgeIndex
            return await KnowledgeIndex(s).reindex_published_rules(int(tax_year))
    try:
        return _run(_do())
    except Exception as exc:  # noqa: BLE001
        raise self.retry(exc=exc, countdown=2 ** self.request.retries) from exc


def enqueue_pipeline(job_id: str) -> None:
    """Kick the async pipeline at its first stage (called after raw is stored)."""
    parse.delay(job_id)
