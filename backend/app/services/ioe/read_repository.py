"""Read repository for sealed IOE evidence (§P6).

Two rules, both enforced by construction rather than by convention.

**1. Every read is parent-key-qualified.** Carried forward from P4:

    RLS is the tenant-correctness boundary, not the query access path.

RLS guarantees a caller only ever sees their own rows. It does not make an
unqualified read cheap: the ownership predicate is an `EXISTS` over another
table and can never become a searchable index condition on the child, so a
`SELECT` without a parent key costs a full child-table scan no matter how few
rows come back. Every method here therefore takes a `run_id`, `portfolio_id`,
`candidate_id` or `scenario_id`, and the two entry points that legitimately list
by owner are qualified by `user_id`, which is indexed.

**2. Nothing here computes.** No summing, no ranking, no reconciliation, no
re-derivation of a total. These methods return sealed rows; the numbers were
settled when the evidence was written and are read back as stored.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound
from app.database.models import (
    MultiYearProjection,
    OptimizationCandidate,
    OptimizationRun,
    PortfolioExclusion,
    PortfolioMember,
    ResourceLedgerEntry,
    Scenario,
    ScenarioAssumption,
    ScenarioConfidenceComponent,
    ScenarioInputChange,
    ScenarioLever,
    ScenarioResult,
    StrategyPortfolio,
)

READ_REPOSITORY_VERSION = "1.0.0"

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class IoeReadRepository:
    """Sealed-evidence reads. Ownership is checked here as well as by RLS."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID):
        self.s = session
        self.user_id = user_id

    # ------------------------------------------------------------ scenarios --
    async def list_scenarios(
        self,
        *,
        include_archived: bool = False,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> Sequence[Scenario]:
        """Owner-qualified and bounded. Archived rows are excluded by default —
        they are hidden, not deleted, and can be asked for explicitly."""
        stmt = select(Scenario).where(Scenario.user_id == self.user_id)
        if not include_archived:
            stmt = stmt.where(Scenario.visibility_status == "active")
        stmt = (
            stmt.order_by(Scenario.created_at.desc())
            .limit(min(limit, MAX_PAGE_SIZE))
            .offset(max(offset, 0))
        )
        return list(await self.s.scalars(stmt))

    async def get_scenario(self, scenario_id: uuid.UUID) -> Scenario:
        scenario = await self.s.get(Scenario, scenario_id)
        if scenario is None or scenario.user_id != self.user_id:
            # identical response for "not yours" and "not there": the API must
            # not confirm that another user's scenario exists
            raise NotFound("Scenario not found")
        return scenario

    async def scenario_result(self, scenario_id: uuid.UUID) -> ScenarioResult | None:
        return await self.s.scalar(
            select(ScenarioResult).where(ScenarioResult.scenario_id == scenario_id)
        )

    async def scenario_levers(self, scenario_id: uuid.UUID) -> Sequence[ScenarioLever]:
        return list(await self.s.scalars(
            select(ScenarioLever)
            .where(ScenarioLever.scenario_id == scenario_id)
            .order_by(ScenarioLever.apply_order)
        ))

    async def scenario_assumptions(
        self, scenario_id: uuid.UUID
    ) -> Sequence[ScenarioAssumption]:
        return list(await self.s.scalars(
            select(ScenarioAssumption)
            .where(ScenarioAssumption.scenario_id == scenario_id)
            .order_by(ScenarioAssumption.assumption_code)
        ))

    async def scenario_changes(
        self, scenario_id: uuid.UUID
    ) -> Sequence[ScenarioInputChange]:
        return list(await self.s.scalars(
            select(ScenarioInputChange)
            .where(ScenarioInputChange.scenario_id == scenario_id)
            .order_by(ScenarioInputChange.apply_order)
        ))

    async def scenario_confidence_components(
        self, scenario_id: uuid.UUID
    ) -> Sequence[ScenarioConfidenceComponent]:
        return list(await self.s.scalars(
            select(ScenarioConfidenceComponent)
            .where(ScenarioConfidenceComponent.scenario_id == scenario_id)
            .order_by(ScenarioConfidenceComponent.factor_code)
        ))

    # ---------------------------------------------------------------- runs ---
    async def get_run(self, run_id: uuid.UUID) -> OptimizationRun:
        run = await self.s.get(OptimizationRun, run_id)
        if run is None or run.user_id != self.user_id:
            raise NotFound("Optimization run not found")
        return run

    async def list_runs(
        self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> Sequence[OptimizationRun]:
        return list(await self.s.scalars(
            select(OptimizationRun)
            .where(OptimizationRun.user_id == self.user_id)
            .order_by(OptimizationRun.created_at.desc())
            .limit(min(limit, MAX_PAGE_SIZE))
            .offset(max(offset, 0))
        ))

    async def run_candidates(
        self, run_id: uuid.UUID
    ) -> Sequence[OptimizationCandidate]:
        return list(await self.s.scalars(
            select(OptimizationCandidate)
            .where(OptimizationCandidate.run_id == run_id)
            .order_by(OptimizationCandidate.candidate_rank.asc().nullslast())
        ))

    # ----------------------------------------------------------- portfolio ---
    async def portfolio_for_run(self, run_id: uuid.UUID) -> StrategyPortfolio | None:
        return await self.s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == run_id)
        )

    async def portfolio_members(
        self, portfolio_id: uuid.UUID
    ) -> Sequence[PortfolioMember]:
        return list(await self.s.scalars(
            select(PortfolioMember)
            .where(PortfolioMember.portfolio_id == portfolio_id)
            .order_by(PortfolioMember.apply_order)
        ))

    async def portfolio_exclusions(
        self, portfolio_id: uuid.UUID
    ) -> Sequence[PortfolioExclusion]:
        return list(await self.s.scalars(
            select(PortfolioExclusion)
            .where(PortfolioExclusion.portfolio_id == portfolio_id)
            .order_by(PortfolioExclusion.reason_code)
        ))

    async def portfolio_ledger(
        self, portfolio_id: uuid.UUID
    ) -> Sequence[ResourceLedgerEntry]:
        return list(await self.s.scalars(
            select(ResourceLedgerEntry)
            .where(ResourceLedgerEntry.portfolio_id == portfolio_id)
            .order_by(ResourceLedgerEntry.resource_code)
        ))

    # --------------------------------------------------------- projections ---
    async def projections_for_run(
        self, run_id: uuid.UUID
    ) -> Sequence[MultiYearProjection]:
        return list(await self.s.scalars(
            select(MultiYearProjection)
            .where(MultiYearProjection.run_id == run_id)
            .order_by(MultiYearProjection.horizon_year)
        ))

    # ----------------------------------------------------------- integrity ---
    async def integrity_target(self, entity_type, entity_id: uuid.UUID):
        """The row carrying current integrity metadata, or None.

        Every lookup is by primary key and every one is RLS-protected: the
        portfolio resolves ownership through its run, so a caller cannot use an
        integrity read to discover that another tenant's entity exists — the
        answer is the same None either way.
        """
        from app.services.ioe.domain.integrity import EntityType

        kind = EntityType(entity_type)
        owned: OptimizationRun | Scenario | None
        if kind is EntityType.OPTIMIZATION:
            owned = await self.s.get(OptimizationRun, entity_id)
            return owned if owned is not None and owned.user_id == self.user_id else None
        if kind is EntityType.SCENARIO:
            owned = await self.s.get(Scenario, entity_id)
            return owned if owned is not None and owned.user_id == self.user_id else None
        portfolio = await self.s.get(StrategyPortfolio, entity_id)
        if portfolio is None:
            return None
        run = await self.s.get(OptimizationRun, portfolio.run_id)
        return portfolio if run is not None and run.user_id == self.user_id else None


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "READ_REPOSITORY_VERSION",
    "IoeReadRepository",
]
