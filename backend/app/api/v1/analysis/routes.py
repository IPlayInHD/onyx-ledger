from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id, db_authed
from app.core.exceptions import NotFound
from app.database.models import AnalysisRun, Recommendation
from app.schemas import AnalysisOut, AnalysisRequest, RecommendationOut
from app.services.analysis.service import AnalysisService

router = APIRouter(tags=["analysis"])


@router.post("/analysis", response_model=AnalysisOut, status_code=status.HTTP_201_CREATED)
async def run_analysis(
    body: AnalysisRequest,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> AnalysisOut:
    run = await AnalysisService(session).run(user_id, body.tax_year)
    return AnalysisOut.model_validate(run)


@router.get("/analysis", response_model=list[AnalysisOut])
async def list_analyses(
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> list[AnalysisOut]:
    rows = await session.scalars(
        select(AnalysisRun).where(AnalysisRun.user_id == user_id).order_by(AnalysisRun.created_at.desc())
    )
    return [AnalysisOut.model_validate(r) for r in rows]


@router.get("/recommendations", response_model=list[RecommendationOut])
async def list_recommendations(
    analysis_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> list[RecommendationOut]:
    run = await session.get(AnalysisRun, analysis_id)
    if run is None or run.user_id != user_id:
        raise NotFound("Analysis not found")
    rows = await session.scalars(
        select(Recommendation).where(Recommendation.analysis_id == analysis_id)
        .order_by(Recommendation.priority)
    )
    return [RecommendationOut.model_validate(r) for r in rows]
