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
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_account_active, current_user_id, db_authed
from app.core.exceptions import NotFound
from app.schemas.assurance import TaxAssuranceOut
from app.schemas.before_you_act import BeforeYouActComparisonOut
from app.schemas.decision_journal import (
    CreateDecisionJournalRequest,
    DecisionJournalDetailOut,
    DecisionJournalSummaryOut,
    RecordDecisionRequest,
    ReportActionRequest,
)
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
from app.schemas.opportunity_lifecycle import OpportunityLifecycleOut
from app.services.admission import OperationClass, admission_guard
from app.services.admission.guard import user_scope
from app.services.ioe import (
    assurance_presentation,
    before_you_act_presentation,
    journal_presentation,
    lifecycle_presentation,
    presentation,
)
from app.services.ioe.domain.integrity import EntityType
from app.services.ioe.journal import DecisionJournalService
from app.services.ioe.journal.domain import Decision
from app.services.ioe.lifecycle import OpportunityLifecycleService
from app.services.ioe.projection_query import ProjectionQueryService
from app.services.ioe.read_repository import IoeReadRepository
from app.services.ioe.replay import IntegrityVerificationService
from app.services.ioe.scenario.before_you_act import BeforeYouActService
from app.services.ioe.scenario.comparison_service import ScenarioComparisonService
from app.services.ioe.scenario.query_service import ScenarioQueryService
from app.services.ioe.scenario.service import ScenarioService
from app.services.state_graph.assurance import derive_assurance_map
from app.services.state_graph.service import TaxStateGraphService

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
    _active: None = Depends(assert_account_active),
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
    _active: None = Depends(assert_account_active),
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
    _active: None = Depends(assert_account_active),
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
    _active: None = Depends(assert_account_active),
) -> None:
    await ScenarioService(user_id).archive(scenario_id)


@router.post("/scenarios/{scenario_id}/unarchive", status_code=status.HTTP_204_NO_CONTENT)
async def unarchive_scenario(
    scenario_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    _active: None = Depends(assert_account_active),
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


@router.get(
    "/scenarios/{scenario_id}/comparison",
    response_model=BeforeYouActComparisonOut,
    summary="What would change if you did this",
    description=(
        "The Before-You-Act comparison: the difference between this scenario's "
        "frozen baseline and its sealed counterfactual. Read entirely from "
        "sealed rows — no tax is computed, no rule is evaluated, no current "
        "document is read. A scenario whose seal cannot answer for a comparison "
        "family is refused rather than partially compared. Reports what "
        "changed; it does not recommend a course of action."
    ),
)
async def get_before_you_act_comparison(
    scenario_id: uuid.UUID,
    include_unchanged: bool = Query(
        False,
        description=(
            "Render records that did not change. Presentation only: the "
            "summary counts and the comparison hash are identical either way."
        ),
    ),
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> BeforeYouActComparisonOut:
    loaded = await BeforeYouActService(session, user_id).comparison_for(scenario_id)
    return before_you_act_presentation.comparison_detail(
        loaded, include_unchanged=include_unchanged
    )


@router.get(
    "/assurance",
    response_model=TaxAssuranceOut,
    summary="The Tax Assurance Map for one tax year",
    description=(
        "The current standing of the caller's tax position, derived "
        "deterministically from governed state: opportunities, evidence "
        "readiness, deadlines, assumptions, and a documented review-next "
        "order. Reports standing only — it computes no tax, decides no "
        "eligibility, and makes no recommendation beyond its documented "
        "presentation order. A family with no governing run reads UNAVAILABLE, "
        "never as an empty READY."
    ),
)
async def get_tax_assurance(
    tax_year: int = Query(..., ge=2000, le=2100),
    as_of: date | None = Query(
        None,
        description=(
            "Evaluation date for deadline urgency, echoed in the response. "
            "Defaults to today (UTC). The only field the clock touches."
        ),
    ),
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> TaxAssuranceOut:
    # The clock is read HERE, at the boundary, and injected. The derivation
    # itself never consults it, which is what keeps the map reproducible.
    evaluation_date = as_of or datetime.now(tz=UTC).date()
    graph = await TaxStateGraphService(session, user_id).build(tax_year=tax_year)
    return assurance_presentation.assurance_detail(
        derive_assurance_map(graph, as_of=evaluation_date)
    )


# ---------------------------------------------------------- decision journal --
@router.post(
    "/decision-journal",
    response_model=DecisionJournalDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Open a decision thread from a sealed scenario",
    description=(
        "Records that the user is CONSIDERING a decision about a scenario "
        "they own, pinning the sealed artifact identities that informed it. "
        "Append-only from here: every later change is a new event. Retrying "
        "with the same request_id returns the thread already created."
    ),
)
async def create_decision_journal(
    body: CreateDecisionJournalRequest,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> DecisionJournalDetailOut:
    service = DecisionJournalService(session, user_id)
    journal = await service.create(
        scenario_id=body.scenario_id,
        request_id=body.request_id,
        subject_opportunity_code=body.subject_opportunity_code,
    )
    return journal_presentation.journal_detail(await service.detail(journal.id))


@router.get(
    "/decision-journal",
    response_model=list[DecisionJournalSummaryOut],
    summary="List decision threads, newest first",
)
async def list_decision_journals(
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> list[DecisionJournalSummaryOut]:
    loaded = await DecisionJournalService(session, user_id).list_journals()
    return [journal_presentation.journal_summary(entry) for entry in loaded]


@router.get(
    "/decision-journal/{journal_id}",
    response_model=DecisionJournalDetailOut,
    summary="Read one decision thread with its full history",
    description=(
        "The append-only history, the projection derived from it, the pinned "
        "scenario reference, and governed evidence context — sealed readiness "
        "as recorded, current observed readiness as it stands now. A user "
        "report of an action is presented as a report, never as verification."
    ),
)
async def get_decision_journal(
    journal_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> DecisionJournalDetailOut:
    return journal_presentation.journal_detail(
        await DecisionJournalService(session, user_id).detail(journal_id)
    )


@router.post(
    "/decision-journal/{journal_id}/decision",
    response_model=DecisionJournalDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record a decision",
    description=(
        "Appends the user's declaration — PROCEED, DEFER, DECLINE, or back to "
        "CONSIDERING. A change of mind appends; nothing is rewritten. PROCEED "
        "records intent and is never treated as proof an action occurred."
    ),
)
async def record_decision(
    journal_id: uuid.UUID,
    body: RecordDecisionRequest,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> DecisionJournalDetailOut:
    service = DecisionJournalService(session, user_id)
    await service.record_decision(
        journal_id, decision=Decision(body.decision), request_id=body.request_id
    )
    return journal_presentation.journal_detail(await service.detail(journal_id))


@router.post(
    "/decision-journal/{journal_id}/action-report",
    response_model=DecisionJournalDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Report that the action was taken",
    description=(
        "Records the user's report that they acted, optionally with the date "
        "they say it happened. A self-report: the response continues to label "
        "execution USER_REPORTED, and evidence context remains a separate, "
        "governed observation."
    ),
)
async def report_decision_action(
    journal_id: uuid.UUID,
    body: ReportActionRequest,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> DecisionJournalDetailOut:
    service = DecisionJournalService(session, user_id)
    await service.report_action(
        journal_id, request_id=body.request_id, action_date=body.action_date
    )
    return journal_presentation.journal_detail(await service.detail(journal_id))


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
    _active: None = Depends(assert_account_active),
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


@router.get(
    "/opportunity-lifecycle",
    response_model=OpportunityLifecycleOut,
    summary="The current lifecycle of every governed opportunity",
    description=(
        "Joins the Tax Assurance Map with the Decision Journal: availability, "
        "the user's decision, whether they reported acting, evidence "
        "readiness, the governed deadline and its timing band, freshness and "
        "integrity — seven axes that never collapse into one another. "
        "Expired opportunities stay visible with timing EXPIRED rather than "
        "disappearing. Reports state; it computes no tax, evaluates no rule, "
        "and ranks nothing the optimizer has not already ranked."
    ),
)
async def get_opportunity_lifecycle(
    tax_year: int = Query(..., ge=2000, le=2100),
    as_of: date | None = Query(
        None,
        description=(
            "Evaluation date for deadline timing, echoed in the response. "
            "Defaults to today (UTC). The only field the clock touches."
        ),
    ),
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> OpportunityLifecycleOut:
    # The clock is read HERE, at the boundary, and injected into both the
    # Assurance derivation and the lifecycle join.
    evaluation_date = as_of or datetime.now(tz=UTC).date()
    lifecycle = await OpportunityLifecycleService(session, user_id).build(
        tax_year=tax_year, as_of=evaluation_date
    )
    return lifecycle_presentation.lifecycle_detail(lifecycle)
