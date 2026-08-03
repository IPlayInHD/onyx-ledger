"""Scenario read/write orchestration for the API (§P6).

Exists so routes stay thin. Each method owns its own unit of work, performs the
ownership check, evaluates freshness where a read requires it, and hands back a
finished response object. A route calls one of these and returns the result.

The read-time freshness path lives here: `detail()` re-evaluates the scenario it
is about to return and persists any transition before assembling the response.
That is what makes it impossible to be shown a stale result labelled `current`,
whatever the event plumbing missed. The stored RESULT is never touched — only
the freshness columns on the header move.
"""
from __future__ import annotations

import uuid

from app.core.exceptions import Conflict
from app.database.session import unit_of_work
from app.schemas.ioe import ScenarioCreateRequest, ScenarioDetailOut
from app.services.ioe import presentation
from app.services.ioe.domain.scenario import ScenarioSpec, ScenarioSpecError
from app.services.ioe.read_repository import IoeReadRepository
from app.services.ioe.scenario.freshness_service import ScenarioFreshnessService
from app.services.ioe.scenario.service import ScenarioService

QUERY_SERVICE_VERSION = "1.0.0"


class ScenarioQueryService:
    """API-facing scenario operations. Owns its transactions."""

    def __init__(self, user_id: uuid.UUID):
        self.user_id = user_id

    async def create(self, body: ScenarioCreateRequest) -> ScenarioDetailOut:
        """Parse the request into a typed spec, simulate, then return the detail.

        The schema has already refused unknown fields; `ScenarioSpec.parse` then
        applies the domain rules — registered codes, declared parameters,
        applicable jurisdiction and tax year. Only a spec that survives both
        reaches the service.
        """
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            repo = IoeReadRepository(session, self.user_id)
            analysis = await self._analysis(session, body.analysis_id)
            jurisdiction = analysis.province_code or "FED"
            tax_year = analysis.tax_year
            del repo

        try:
            spec = ScenarioSpec.parse(
                [x.model_dump() for x in body.levers],
                assumptions=[a.model_dump() for a in body.assumptions],
                label=body.label,
                note=body.note,
                jurisdiction=jurisdiction,
                tax_year=tax_year,
            )
        except ScenarioSpecError as exc:
            raise Conflict(f"invalid_scenario_specification: {exc}") from exc

        outcome = await ScenarioService(self.user_id).simulate(body.analysis_id, spec)
        return await self.detail(outcome.scenario_id)

    async def detail(self, scenario_id: uuid.UUID) -> ScenarioDetailOut:
        """READ-TIME freshness: evaluate, persist the transition, then assemble."""
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            repo = IoeReadRepository(session, self.user_id)
            scenario = await repo.get_scenario(scenario_id)

            # Evaluate before assembling, so the response carries the verdict
            # this read just reached rather than the one from last time.
            await ScenarioFreshnessService(session, self.user_id).evaluate_and_record(
                scenario
            )

            result = await repo.scenario_result(scenario_id)
            levers = await repo.scenario_levers(scenario_id)
            assumptions = await repo.scenario_assumptions(scenario_id)
            changes = await repo.scenario_changes(scenario_id)
            return presentation.scenario_detail(
                scenario, result, list(levers), list(assumptions), list(changes)
            )

    async def refresh(self, scenario_id: uuid.UUID) -> ScenarioDetailOut:
        """Re-run the specification; the result is a NEW scenario."""
        outcome = await ScenarioService(self.user_id).refresh(scenario_id)
        return await self.detail(outcome.scenario_id)

    @staticmethod
    async def _analysis(session, analysis_id: uuid.UUID):
        from app.core.exceptions import NotFound
        from app.database.models import AnalysisRun

        analysis = await session.get(AnalysisRun, analysis_id)
        if analysis is None:
            raise NotFound("Analysis not found")
        return analysis


__all__ = ["QUERY_SERVICE_VERSION", "ScenarioQueryService"]
