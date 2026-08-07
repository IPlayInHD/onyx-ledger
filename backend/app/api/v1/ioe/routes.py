"""IOE API surfaces (§P6). Routes are THIN.

Each handler does three things and no more: resolve the principal, call one
service, return what it is given. There is no calculation, no ranking, no
comparison logic, no freshness derivation and no total reconstruction at this
layer — every one of those lives in a service that can be tested without HTTP,
and putting any of them here would mean a figure the API shows could differ from
the figure that was verified and sealed.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id, db_authed
from app.core.exceptions import NotFound
from app.schemas.ioe import (
    IntegrityCheckOut,
    IntegrityOut,
    ProjectionResponse,
    ScenarioComparisonOut,
    ScenarioCreateRequest,
    ScenarioDetailOut,
    ScenarioSummaryOut,
    StrategyPortfolioOut,
)
from app.services.admission import OperationClass, admission_guard
from app.services.admission.guard import user_scope
from app.services.ioe import presentation
from app.services.ioe.domain.integrity import EntityType
from app.services.ioe.projection_query import ProjectionQueryService
from app.services.ioe.read_repository import IoeReadRepository
from app.services.ioe.replay import IntegrityVerificationService
from app.services.ioe.scenario.comparison_service import ScenarioComparisonService
from app.services.ioe.scenario.query_service import ScenarioQueryService
from app.services.ioe.scenario.service import ScenarioService

router = APIRouter(prefix="/ioe", tags=["ioe"])


# ---------------------------------------------------------------- scenarios --
@router.post(
    "/scenarios",
    response_model=ScenarioDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Run a what-if scenario",
    description=(
        "Accepts typed lever codes and structured assumptions ONLY. There is no "
        "field through which an engine input can be named or a computation "
        "supplied."
    ),
)
async def create_scenario(
    body: ScenarioCreateRequest,
    user_id: uuid.UUID = Depends(current_user_id),
) -> ScenarioDetailOut:
    return await ScenarioQueryService(user_id).create(body)


@router.get("/scenarios", response_model=list[ScenarioSummaryOut])
async def list_scenarios(
    include_archived: bool = Query(
        False, description="Archived scenarios are hidden, not deleted."
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> list[ScenarioSummaryOut]:
    rows = await IoeReadRepository(session, user_id).list_scenarios(
        include_archived=include_archived, limit=limit, offset=offset
    )
    return [presentation.scenario_summary(r) for r in rows]


@router.get(
    "/scenarios/{scenario_id}",
    response_model=ScenarioDetailOut,
    summary="Read a scenario, re-evaluating its freshness",
    description=(
        "Freshness is evaluated on this read and any transition is persisted, so "
        "a stale result is never returned labelled current. The stored result "
        "itself is never modified."
    ),
)
async def get_scenario(
    scenario_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
) -> ScenarioDetailOut:
    return await ScenarioQueryService(user_id).detail(scenario_id)


@router.post(
    "/scenarios/{scenario_id}/refresh",
    response_model=ScenarioDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Re-run a scenario against today's world",
    description=(
        "Creates a NEW scenario and marks the original superseded. The "
        "historical result keeps its own pinned versions and its own numbers."
    ),
)
async def refresh_scenario(
    scenario_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
) -> ScenarioDetailOut:
    return await ScenarioQueryService(user_id).refresh(scenario_id)


@router.delete(
    "/scenarios/{scenario_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive a scenario",
    description=(
        "This is an ARCHIVE, not a deletion. The result, the applied-change "
        "trace, the pinned specification and the audit events all survive."
    ),
)
async def archive_scenario(
    scenario_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
) -> None:
    await ScenarioService(user_id).archive(scenario_id)


@router.post("/scenarios/{scenario_id}/unarchive", status_code=status.HTTP_204_NO_CONTENT)
async def unarchive_scenario(
    scenario_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
) -> None:
    await ScenarioService(user_id).unarchive(scenario_id)


@router.get(
    "/scenarios/{left_id}/compare/{right_id}",
    response_model=ScenarioComparisonOut,
    summary="Compare two scenarios",
    description=(
        "Refuses the comparison unless both scenarios are owned by the caller, "
        "completed, sealed, and compatible on baseline, tax year, jurisdiction, "
        "objective policy and result schema. Deltas are kept separate by concept."
    ),
)
async def compare_scenarios(
    left_id: uuid.UUID,
    right_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> ScenarioComparisonOut:
    loaded = await ScenarioComparisonService(session, user_id).compare(left_id, right_id)
    return presentation.comparison_detail(loaded)


# ---------------------------------------------------------------- portfolio --
@router.get(
    "/runs/{run_id}/portfolio",
    response_model=StrategyPortfolioOut,
    summary="Read the sealed strategy portfolio for an optimization run",
    description=(
        "Every figure is read from the sealed portfolio row. The total is not "
        "rebuilt from members, and no recommendation amounts are summed."
    ),
)
async def get_portfolio(
    run_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> StrategyPortfolioOut:
    repo = IoeReadRepository(session, user_id)
    run = await repo.get_run(run_id)
    portfolio = await repo.portfolio_for_run(run_id)
    if portfolio is None:
        raise NotFound("Portfolio not found for this run")
    members = await repo.portfolio_members(portfolio.id)
    exclusions = await repo.portfolio_exclusions(portfolio.id)
    return presentation.portfolio_detail(
        portfolio, list(members), list(exclusions), run.tax_year
    )


@router.get(
    "/runs/{run_id}/projections",
    response_model=ProjectionResponse,
    summary="Read multi-year projections for a run",
    description=(
        "Projections are returned SEPARATELY and are never part of any "
        "current-year total. Always an object with a status: a run with no "
        "rule-authorized projection returns an explicit not_generated status "
        "rather than a misleading null."
    ),
)
async def get_projections(
    run_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> ProjectionResponse:
    return await ProjectionQueryService(session, user_id).for_run(run_id)


# ---------------------------------------------------------------- integrity --
@router.get(
    "/{entity_type}/{entity_id}/integrity",
    response_model=IntegrityOut,
    summary="Read current replay-integrity metadata",
    description=(
        "Whether the SEALED result can still be reproduced from its own pinned "
        "inputs. A different question from freshness: a result can be stale and "
        "reproducible, or current and non-reproducible. Reads stored metadata "
        "only — this never triggers a replay. Hashes are not exposed."
    ),
)
async def get_integrity(
    entity_type: str,
    entity_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> IntegrityOut:
    row = await IoeReadRepository(session, user_id).integrity_target(
        _entity_type(entity_type), entity_id
    )
    if row is None:
        raise NotFound("Not found")
    return presentation.integrity_of(row)


@router.post(
    "/{entity_type}/{entity_id}/integrity/verify",
    response_model=IntegrityCheckOut,
    status_code=status.HTTP_200_OK,
    summary="Replay-verify a sealed result",
    description=(
        "Re-executes the sealed calculation against its pinned dependencies and "
        "records the outcome. The historical result is never modified, "
        "regenerated, or repaired — a mismatch is recorded and preserved, not "
        "corrected. Returns 409 while another verification of the same entity "
        "is already running."
    ),
)
async def verify_integrity(
    entity_type: str,
    entity_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
) -> IntegrityCheckOut:
    # A manual replay re-executes a sealed calculation end to end. The service
    # already refuses a second verification of the SAME entity; admission bounds
    # how many DIFFERENT entities one caller can replay at once.
    #
    # Ownership is resolved inside the service, which raises NotFound for an
    # entity the caller may not see. Admission runs first and is deliberately
    # keyed on the CALLER and the operation class only — never on the entity —
    # so a rejection cannot confirm that some other tenant's entity exists.
    async with admission_guard(
        OperationClass.INTEGRITY_VERIFY, scope_id=user_scope(user_id)
    ):
        result = await IntegrityVerificationService(user_id).verify(
            _entity_type(entity_type), entity_id
        )
    return IntegrityCheckOut(
        check_id=result.check_id,
        entity_type=result.entity_type,
        entity_id=result.entity_id,
        integrity_status=result.status.value,
        integrity_state=result.integrity_state,
        integrity_reason_code=result.reason_code.value,
        integrity_warning=result.integrity_warning,
        duration_ms=result.duration_ms,
    )


def _entity_type(value: str) -> EntityType:
    """Reject an unknown entity type before any lookup happens."""
    try:
        return EntityType(value)
    except ValueError:
        raise NotFound("Not found") from None


__all__ = ["router"]
