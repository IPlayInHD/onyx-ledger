"""Batch loading for graph assembly. One query per entity family, never one per
node.

The regression this module exists to prevent is not slowness in the abstract; it
is a per-node lookup appearing in a later edit and nobody noticing until a user
with two hundred candidates waits. `test_assembly_issues_a_bounded_number_of_queries`
counts statements and asserts the count does not grow with node count, which is
the only form of that promise a test can actually keep.

Ownership is filtered in the query as well as by RLS, following
`IoeReadRepository`, whose docstring says ownership is checked there *as well
as* by RLS. Two enforcement points, for the same reason the lifecycle state
machine has two.

Every load is bounded by an explicit limit. An unbounded read of a tenant's
history is a denial-of-service surface even when the tenant is the one asking.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.analysis import (
    AnalysisInputSnapshot,
    AnalysisLineItem,
    AnalysisRun,
)
from app.database.models.docs import Document
from app.database.models.finance import ExpenseRecord, IncomeSource
from app.database.models.ioe import (
    OptimizationCandidate,
    OptimizationRun,
    PortfolioExclusion,
    PortfolioMember,
    RecommendationRelationship,
    ResourceLedgerEntry,
    Scenario,
    ScenarioAssumption,
    StrategyPortfolio,
)
from app.database.models.profile import TaxProfile
from app.database.models.ref import DocumentType
from app.database.models.tax_kb import RuleDeadline, RuleRequiredDocument
from app.database.models.wealth import Asset, Liability

#: Per-family ceilings. Generous enough that a real tenant is never truncated in
#: practice, low enough that no single request can read an unbounded history.
MAX_ROWS_PER_FAMILY = 2000
MAX_SCENARIOS = 200


@dataclass(frozen=True)
class GraphSources:
    """Everything one assembly reads, loaded before any node is built.

    Separating loading from assembly is what makes the assembler purely a
    projection: it cannot reach back to the database for a value it forgot,
    because it has no session.
    """

    analysis: AnalysisRun | None = None
    line_items: tuple[AnalysisLineItem, ...] = ()
    snapshot: AnalysisInputSnapshot | None = None

    income: tuple[IncomeSource, ...] = ()
    expenses: tuple[ExpenseRecord, ...] = ()
    assets: tuple[Asset, ...] = ()
    liabilities: tuple[Liability, ...] = ()
    tax_profile: TaxProfile | None = None

    run: OptimizationRun | None = None
    candidates: tuple[OptimizationCandidate, ...] = ()
    portfolio: StrategyPortfolio | None = None
    members: tuple[PortfolioMember, ...] = ()
    exclusions: tuple[PortfolioExclusion, ...] = ()
    ledger: tuple[ResourceLedgerEntry, ...] = ()
    relationships: tuple[RecommendationRelationship, ...] = ()

    scenarios: tuple[Scenario, ...] = ()
    scenario_assumptions: tuple[ScenarioAssumption, ...] = ()

    required_documents: tuple[RuleRequiredDocument, ...] = ()
    deadlines: tuple[RuleDeadline, ...] = ()
    #: (document_id, document_type_code) for ingested, undeleted documents only.
    held_documents: tuple[tuple[uuid.UUID, str], ...] = field(default_factory=tuple)


class GraphLoader:
    """Owner-qualified, bounded, one query per family."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self.s = session
        self.user_id = user_id

    async def load(self, *, tax_year: int) -> GraphSources:
        analysis = await self._latest_analysis(tax_year)
        if analysis is None:
            # No completed analysis means no tax state to assemble around. The
            # facts the user has declared are still real, so they are still
            # loaded — an empty graph and a graph of facts with no analysis are
            # different situations and must not look the same.
            return GraphSources(
                income=await self._income(tax_year),
                expenses=await self._expenses(tax_year),
                assets=await self._assets(),
                liabilities=await self._liabilities(),
                tax_profile=await self._tax_profile(),
                held_documents=await self._held_documents(tax_year),
            )

        line_items = tuple(
            await self.s.scalars(
                select(AnalysisLineItem)
                .where(AnalysisLineItem.analysis_id == analysis.id)
                .order_by(AnalysisLineItem.sort_order, AnalysisLineItem.id)
                .limit(MAX_ROWS_PER_FAMILY)
            )
        )
        snapshot = await self.s.scalar(
            select(AnalysisInputSnapshot).where(
                AnalysisInputSnapshot.analysis_id == analysis.id
            )
        )

        run = await self._latest_run(analysis.id)
        candidates: tuple[OptimizationCandidate, ...] = ()
        portfolio: StrategyPortfolio | None = None
        members: tuple[PortfolioMember, ...] = ()
        exclusions: tuple[PortfolioExclusion, ...] = ()
        ledger: tuple[ResourceLedgerEntry, ...] = ()
        relationships: tuple[RecommendationRelationship, ...] = ()

        if run is not None:
            candidates = tuple(
                await self.s.scalars(
                    select(OptimizationCandidate)
                    .where(OptimizationCandidate.run_id == run.id)
                    .order_by(OptimizationCandidate.id)
                    .limit(MAX_ROWS_PER_FAMILY)
                )
            )
            relationships = tuple(
                await self.s.scalars(
                    select(RecommendationRelationship)
                    .where(RecommendationRelationship.run_id == run.id)
                    .order_by(RecommendationRelationship.id)
                    .limit(MAX_ROWS_PER_FAMILY)
                )
            )
            portfolio = await self.s.scalar(
                select(StrategyPortfolio)
                .where(StrategyPortfolio.run_id == run.id)
                .order_by(StrategyPortfolio.created_at.desc())
                .limit(1)
            )
            if portfolio is not None:
                members = tuple(
                    await self.s.scalars(
                        select(PortfolioMember)
                        .where(PortfolioMember.portfolio_id == portfolio.id)
                        .order_by(PortfolioMember.apply_order, PortfolioMember.id)
                        .limit(MAX_ROWS_PER_FAMILY)
                    )
                )
                exclusions = tuple(
                    await self.s.scalars(
                        select(PortfolioExclusion)
                        .where(PortfolioExclusion.portfolio_id == portfolio.id)
                        .order_by(PortfolioExclusion.id)
                        .limit(MAX_ROWS_PER_FAMILY)
                    )
                )
                ledger = tuple(
                    await self.s.scalars(
                        select(ResourceLedgerEntry)
                        .where(ResourceLedgerEntry.portfolio_id == portfolio.id)
                        .order_by(ResourceLedgerEntry.id)
                        .limit(MAX_ROWS_PER_FAMILY)
                    )
                )
        scenarios = tuple(
            await self.s.scalars(
                select(Scenario)
                .where(
                    Scenario.user_id == self.user_id,
                    Scenario.base_analysis_id == analysis.id,
                    Scenario.visibility_status == "active",
                )
                .order_by(Scenario.created_at.desc(), Scenario.id)
                .limit(MAX_SCENARIOS)
            )
        )
        scenario_assumptions: tuple[ScenarioAssumption, ...] = ()
        if scenarios:
            scenario_assumptions = tuple(
                await self.s.scalars(
                    select(ScenarioAssumption)
                    .where(ScenarioAssumption.scenario_id.in_([s.id for s in scenarios]))
                    .order_by(ScenarioAssumption.assumption_code, ScenarioAssumption.id)
                    .limit(MAX_ROWS_PER_FAMILY)
                )
            )

        rule_version_ids = _pinned_rule_version_ids(line_items, candidates)
        required_documents: tuple[RuleRequiredDocument, ...] = ()
        deadlines: tuple[RuleDeadline, ...] = ()
        if rule_version_ids:
            required_documents = tuple(
                await self.s.scalars(
                    select(RuleRequiredDocument)
                    .where(RuleRequiredDocument.rule_version_id.in_(rule_version_ids))
                    .order_by(
                        RuleRequiredDocument.rule_version_id,
                        RuleRequiredDocument.document_type_code,
                    )
                    .limit(MAX_ROWS_PER_FAMILY)
                )
            )
            deadlines = tuple(
                await self.s.scalars(
                    select(RuleDeadline)
                    .where(RuleDeadline.rule_version_id.in_(rule_version_ids))
                    .order_by(RuleDeadline.deadline_code, RuleDeadline.id)
                    .limit(MAX_ROWS_PER_FAMILY)
                )
            )

        return GraphSources(
            analysis=analysis,
            line_items=line_items,
            snapshot=snapshot,
            income=await self._income(tax_year),
            expenses=await self._expenses(tax_year),
            assets=await self._assets(),
            liabilities=await self._liabilities(),
            tax_profile=await self._tax_profile(),
            run=run,
            candidates=candidates,
            portfolio=portfolio,
            members=members,
            exclusions=exclusions,
            ledger=ledger,
            relationships=relationships,
            scenarios=scenarios,
            scenario_assumptions=scenario_assumptions,
            required_documents=required_documents,
            deadlines=deadlines,
            held_documents=await self._held_documents(tax_year),
        )

    # ------------------------------------------------------------- families --
    async def _latest_analysis(self, tax_year: int) -> AnalysisRun | None:
        # Bound to a declared name: `AsyncSession.scalar` is typed `-> Any`, so
        # returning it directly would erase this method's declared row type.
        found: AnalysisRun | None = await self.s.scalar(
            select(AnalysisRun)
            .where(
                AnalysisRun.user_id == self.user_id,
                AnalysisRun.tax_year == tax_year,
                AnalysisRun.status == "completed",
            )
            .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id)
            .limit(1)
        )
        return found

    async def _latest_run(self, analysis_id: uuid.UUID) -> OptimizationRun | None:
        """The newest run that nothing supersedes.

        Filtering on `superseded_by_run_id IS NULL` rather than taking the newest
        row: a superseded run is not the current state by definition, and its
        successor may legitimately be older by `created_at` in a retry.
        """
        found: OptimizationRun | None = await self.s.scalar(
            select(OptimizationRun)
            .where(
                OptimizationRun.user_id == self.user_id,
                OptimizationRun.analysis_id == analysis_id,
                OptimizationRun.superseded_by_run_id.is_(None),
                OptimizationRun.workflow_status == "completed",
            )
            .order_by(OptimizationRun.created_at.desc(), OptimizationRun.id)
            .limit(1)
        )
        return found

    async def _income(self, tax_year: int) -> tuple[IncomeSource, ...]:
        return tuple(
            await self.s.scalars(
                select(IncomeSource)
                .where(
                    IncomeSource.user_id == self.user_id,
                    IncomeSource.tax_year == tax_year,
                    IncomeSource.deleted_at.is_(None),
                )
                .order_by(IncomeSource.id)
                .limit(MAX_ROWS_PER_FAMILY)
            )
        )

    async def _expenses(self, tax_year: int) -> tuple[ExpenseRecord, ...]:
        return tuple(
            await self.s.scalars(
                select(ExpenseRecord)
                .where(
                    ExpenseRecord.user_id == self.user_id,
                    ExpenseRecord.tax_year == tax_year,
                    ExpenseRecord.deleted_at.is_(None),
                )
                .order_by(ExpenseRecord.id)
                .limit(MAX_ROWS_PER_FAMILY)
            )
        )

    async def _assets(self) -> tuple[Asset, ...]:
        return tuple(
            await self.s.scalars(
                select(Asset)
                .where(Asset.user_id == self.user_id, Asset.deleted_at.is_(None))
                .order_by(Asset.id)
                .limit(MAX_ROWS_PER_FAMILY)
            )
        )

    async def _liabilities(self) -> tuple[Liability, ...]:
        return tuple(
            await self.s.scalars(
                select(Liability)
                .where(Liability.user_id == self.user_id, Liability.deleted_at.is_(None))
                .order_by(Liability.id)
                .limit(MAX_ROWS_PER_FAMILY)
            )
        )

    async def _tax_profile(self) -> TaxProfile | None:
        found: TaxProfile | None = await self.s.scalar(
            select(TaxProfile).where(TaxProfile.user_id == self.user_id)
        )
        return found

    async def _held_documents(self, tax_year: int) -> tuple[tuple[uuid.UUID, str], ...]:
        """Ingested, undeleted documents with their governed type code.

        The status filter is the whole point: `quarantined` and `failed` are not
        evidence, and `uploaded` / `processing` are not evidence yet. Joining to
        `ref.document_type` here rather than in the assembler keeps the type code
        — the thing `rule_required_document` actually references — next to the
        row it belongs to.
        """
        rows = await self.s.execute(
            select(Document.id, DocumentType.code)
            .join(DocumentType, Document.document_type_id == DocumentType.id)
            .where(
                Document.user_id == self.user_id,
                Document.deleted_at.is_(None),
                Document.status == "processed",
                Document.tax_year == tax_year,
            )
            .order_by(Document.id)
            .limit(MAX_ROWS_PER_FAMILY)
        )
        return tuple((row[0], row[1]) for row in rows.all())


def _pinned_rule_version_ids(
    line_items: Sequence[AnalysisLineItem],
    candidates: Sequence[OptimizationCandidate],
) -> list[uuid.UUID]:
    """The rule versions this state actually references.

    Sorted so the IN-list is stable, which keeps query plans and test assertions
    reproducible.
    """
    ids: set[uuid.UUID] = set()
    for item in line_items:
        if item.tax_rule_version_id is not None:
            ids.add(item.tax_rule_version_id)
    for candidate in candidates:
        if candidate.tax_rule_version_id is not None:
            ids.add(candidate.tax_rule_version_id)
    return sorted(ids)
