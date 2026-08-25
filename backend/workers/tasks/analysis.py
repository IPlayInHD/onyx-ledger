"""On-demand analysis task — runs the analysis orchestrator in a worker.

Bridges Celery (sync) to the async service via `workers.runtime.run_task`
inside a UoW bound to the requesting user (so RLS + audit see the actor).
"""
from __future__ import annotations

import uuid

from celery import Task

from app.database.session import unit_of_work
from app.services.analysis.service import AnalysisService
from app.services.privacy.preflight import refuse_if_deleting
from workers.celery_app import celery_app
from workers.runtime import run_task


@celery_app.task(name="workers.tasks.analysis.run_analysis", bind=True, max_retries=3)
def run_analysis(self: Task, user_id: str, tax_year: int) -> str:
    async def _run() -> str:
        async with unit_of_work(user_id=uuid.UUID(user_id), actor_type="user") as session:
            # The deletion cutoff. This task may have been queued long before
            # the account asked to be deleted; refusing here is the only
            # reliable place, because a reserved task is already past the
            # queue. See app/services/privacy/preflight.py.
            if await refuse_if_deleting(session, uuid.UUID(user_id),
                                        task="analysis.run_analysis"):
                return ""
            run = await AnalysisService(session).run(uuid.UUID(user_id), tax_year)
            return str(run.id)

    try:
        return run_task(_run)
    except Exception as exc:  # noqa: BLE001
        raise self.retry(exc=exc, countdown=2 ** self.request.retries) from exc
