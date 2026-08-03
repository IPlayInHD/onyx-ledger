"""Projection reads (§P6 closure item 2).

Returns an explicit status in every case. A caller must be able to tell these
apart, and a null tells them apart from nothing:

  generated                            a rule authorized it and here it is
  not_generated_no_eligible_candidates no published rule authorized one
  not_generated_missing_assumptions    a conditional authorization whose
                                       conditions were not supplied
  feature_not_enabled                  the surface is switched off

The distinction matters because "we found nothing" and "we did not look" lead a
user to completely different next actions.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.schemas.ioe import ProjectionResponse
from app.services.ioe import presentation
from app.services.ioe.projection import (
    PROJECTION_METHODOLOGY_VERSION,
    ProjectionStatus,
)
from app.services.ioe.read_repository import IoeReadRepository

PROJECTION_QUERY_VERSION = "1.0.0"


class ProjectionQueryService:
    """Assembles the projection response, status first."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID):
        self.s = session
        self.user_id = user_id

    async def for_run(self, run_id: uuid.UUID) -> ProjectionResponse:
        settings = get_settings()
        repo = IoeReadRepository(self.s, self.user_id)
        # ownership first: a disabled feature must not leak that a run exists
        run = await repo.get_run(run_id)

        if not settings.ioe_projections_enabled:
            return ProjectionResponse(
                status=ProjectionStatus.FEATURE_NOT_ENABLED.value,
                reason="multi-year projections are not enabled in this environment",
            )

        rows = list(await repo.projections_for_run(run_id))
        if not rows:
            return ProjectionResponse(
                status=ProjectionStatus.NOT_GENERATED_NO_ELIGIBLE_CANDIDATES.value,
                reason=(
                    "no candidate in this run carried published rule metadata "
                    "authorizing a projection"
                ),
            )

        return ProjectionResponse(
            status=ProjectionStatus.GENERATED.value,
            projection=presentation.projection_detail(
                rows, None, run.tax_year,
                methodology_version=(
                    rows[0].methodology_version or PROJECTION_METHODOLOGY_VERSION
                ),
            ),
        )


__all__ = ["PROJECTION_QUERY_VERSION", "ProjectionQueryService"]
