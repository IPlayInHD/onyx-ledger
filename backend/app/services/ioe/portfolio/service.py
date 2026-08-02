"""PortfolioEvaluationService — P4 assembly against the real tax engine.

Two responsibilities, deliberately separated so the transaction boundaries stay
with the orchestrator:

  `evaluate()`  pure compute. Adapts `TaxInput` to the plain dict the lever
                registry and assembler operate on, and hands the assembler a
                callable that runs the DETERMINISTIC ENGINE. The IOE never
                calculates tax itself — every objective value in the result,
                including the baseline, comes from an engine run.

  `persist()`   writes the portfolio, its members, the resource ledger, the
                complete evaluation trace, and every excluded candidate with a
                structured reason. Called INSIDE the orchestrator's TX-2 so the
                portfolio and its evidence become visible together.

The stored result is a feasible, deterministic, engine-evaluated strategy
portfolio. It is not a globally optimal one, which is why `optimality_claim` is
persisted as `none` on every row rather than left to a default.
"""
from __future__ import annotations

import dataclasses
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    PortfolioEvaluationStep as StepRow,
)
from app.database.models import (
    PortfolioExclusion as ExclusionRow,
)
from app.database.models import (
    PortfolioMember as MemberRow,
)
from app.database.models import (
    ResourceLedgerEntry as LedgerRow,
)
from app.database.models import (
    StrategyPortfolio as PortfolioRow,
)
from app.services.ioe.domain import portfolio as assembly
from app.services.ioe.domain import savings as savings_domain
from app.services.ioe.domain.models import OptimizationCandidate, StrategyPortfolio
from app.services.tax_engine.core.engine import TaxInput, compute

PORTFOLIO_SERVICE_VERSION = "1.0.0"

# Engine input fields a lever is permitted to reach. The registry enforces its
# own per-lever allow-list; this is the outer boundary of the adapter itself.
_ENGINE_FIELDS = frozenset(f.name for f in dataclasses.fields(TaxInput))


def inputs_from(inp: TaxInput) -> dict:
    """`TaxInput` → plain dict, so the domain stays framework-free."""
    return {f.name: getattr(inp, f.name) for f in dataclasses.fields(TaxInput)}


def to_tax_input(inputs: dict) -> TaxInput:
    """Plain dict → `TaxInput`, refusing any field the engine does not define.

    A lever that somehow produced an unknown key would otherwise be silently
    dropped here and the run would look successful while ignoring the action.
    """
    unknown = set(inputs) - _ENGINE_FIELDS
    if unknown:
        raise ValueError(f"unknown engine input fields: {sorted(unknown)}")
    return TaxInput(**inputs)


def engine_evaluator(inputs: dict) -> Decimal:
    """The single authority for what a hypothetical costs: the tax engine."""
    return compute(to_tax_input(inputs)).total_payable


class PortfolioEvaluationService:
    """Stateless. Holds no transaction; the orchestrator owns those."""

    def evaluate(
        self,
        candidates: list[OptimizationCandidate],
        relationships: list,
        baseline_input: TaxInput,
        constraints: assembly.AssemblyConstraints,
    ) -> StrategyPortfolio:
        """Assemble and evaluate. Every figure here is engine-measured."""
        return assembly.assemble(
            candidates,
            relationships,
            inputs_from(baseline_input),
            engine_evaluator,
            constraints,
        )

    # ---------------------------------------------------------------- TX-2 ---
    async def persist(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        portfolio: StrategyPortfolio,
        candidate_ids: dict[str, uuid.UUID],
    ) -> uuid.UUID:
        """Write the portfolio and all of its evidence. Caller supplies the
        transaction and the candidate_key → persisted-id mapping."""
        breakdown = portfolio.savings or savings_domain.SavingsBreakdown()
        row = PortfolioRow(
            run_id=run_id,
            assembly_policy_version=assembly.PORTFOLIO_ASSEMBLY_VERSION,
            assembly_method=portfolio.assembly_method.value,
            # A rule-based assembler makes no optimality claim — stored, not defaulted.
            optimality_claim=portfolio.optimality_claim.value,
            objective_metric=portfolio.objective_metric.value,
            objective_version=portfolio.objective_version,
            objective_value_baseline=portfolio.objective_value_baseline,
            objective_value_final=portfolio.objective_value_final,
            # pinned objective identity + the delta the two values define
            portfolio_objective_code=portfolio.objective_code,
            portfolio_objective_version=portfolio.objective_version,
            objective_delta=portfolio.objective_delta,
            search_budget_exhausted=portfolio.search_budget_exhausted,
            baseline_tax=portfolio.baseline_tax,
            portfolio_tax=portfolio.portfolio_tax,
            portfolio_total_benefit=portfolio.portfolio_total_benefit,
            sum_of_standalone=portfolio.sum_of_standalone,
            interaction_delta=portfolio.interaction_delta,
            additivity_class=portfolio.additivity_class.value,
            additivity_verified=portfolio.additivity_verified,
            attribution_method_version=assembly.ATTRIBUTION_METHOD_VERSION,
            # each economic outcome kept separate; never folded into another
            total_tax_reduction=breakdown.current_year_reduction,
            total_refund_impact=breakdown.refund_impact,
            total_refundable_benefit=breakdown.refundable_benefit,
            total_deferral_amount=breakdown.deferral_amount,
            total_recurring_annual=breakdown.recurring_annual,
            total_multi_year_projected=breakdown.multi_year_projected,
            total_liquidity_commitment=breakdown.liquidity_commitment,
            total_asset_transfer=breakdown.asset_transfer,
            total_nonrecoverable_expenditure=breakdown.nonrecoverable_expenditure,
            total_implementation_cost=breakdown.implementation_cost,
            net_current_year_benefit=breakdown.net_current_year_benefit,
            deferred_count=portfolio.deferred_count,
            excluded_count=portfolio.excluded_count,
            improvement_moves_applied=portfolio.improvement_moves_applied,
            improvement_runs_used=portfolio.improvement_runs_used,
            unexplored_alternatives_count=portfolio.unexplored_alternatives_count,
            engine_runs_used=portfolio.engine_runs_used,
        )
        session.add(row)
        await session.flush()

        for member in portfolio.members:
            candidate_id = candidate_ids.get(member.candidate_key)
            if candidate_id is None:            # never invent an association
                continue
            session.add(MemberRow(
                portfolio_id=row.id,
                candidate_id=candidate_id,
                apply_order=member.apply_order,
                incremental_benefit=member.incremental_benefit,
                resource_allocations={
                    code: str(amount) for code, amount in member.resource_allocations
                },
            ))

        for entry in portfolio.ledger:
            session.add(LedgerRow(
                portfolio_id=row.id,
                resource_code=entry.resource_code,
                capacity=entry.capacity,
                allocated=entry.allocated,
                remaining=entry.remaining,
            ))

        # The trace records objective values and codes only. The user's financial
        # inputs already live in the frozen analysis snapshot and are deliberately
        # not duplicated here.
        for step in portfolio.trace:
            session.add(StepRow(
                portfolio_id=row.id,
                step_index=step.step_index,
                stage=step.stage,
                candidate_id=candidate_ids.get(step.candidate_key)
                if step.candidate_key else None,
                apply_order=step.apply_order,
                objective_value=step.objective_value,
                objective_delta=step.objective_delta,
                accepted=step.accepted,
                reason_code=step.reason_code,
            ))

        # Excluded candidates are retained with a structured reason and what
        # would change the answer — "not recommended" is itself a result.
        for exclusion in portfolio.exclusions:
            candidate_id = candidate_ids.get(exclusion.candidate_key)
            if candidate_id is None:
                continue
            session.add(ExclusionRow(
                portfolio_id=row.id,
                candidate_id=candidate_id,
                membership=exclusion.membership.value,
                reason_code=exclusion.reason_code,
                blocking_candidate_id=candidate_ids.get(
                    exclusion.blocking_candidate_key
                ) if exclusion.blocking_candidate_key else None,
                shared_resource_code=exclusion.shared_resource_code,
                resolution_options=list(exclusion.resolution_options),
            ))

        await session.flush()
        return row.id


__all__ = [
    "PORTFOLIO_SERVICE_VERSION",
    "PortfolioEvaluationService",
    "engine_evaluator",
    "inputs_from",
    "to_tax_input",
]
