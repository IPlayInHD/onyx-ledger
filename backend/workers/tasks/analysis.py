"""On-demand analysis task — runs the analysis orchestrator in a worker.

Bridges Celery (sync) to the async service via asyncio.run inside a UoW bound
to the requesting user (so RLS + audit see the actor).
"""
from __future__ import annotations

import asyncio
import uuid

from app.database.session import unit_of_work
from app.services.analysis.service import AnalysisService
from workers.celery_app import celery_app


@celery_app.task(name="workers.tasks.analysis.run_analysis", bind=True, max_retries=3)
def run_analysis(self, user_id: str, tax_year: int) -> str:
    async def _run() -> str:
        async with unit_of_work(user_id=uuid.UUID(user_id), actor_type="user") as session:
            run = await AnalysisService(session).run(uuid.UUID(user_id), tax_year)
            return str(run.id)

    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        raise self.retry(exc=exc, countdown=2 ** self.request.retries) from exc
