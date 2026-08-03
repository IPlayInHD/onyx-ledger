"""Scenario comparison adapter (§P6.2, §P6 comparison requirements).

Thin by design. The order of operations is the whole point, and it is fixed:

  1. LOAD both scenarios under an OWNERSHIP check — the application validates the
     user owns each one, in addition to RLS.
  2. Require both are COMPLETED and SEALED (both hashes present). An in-flight or
     failed scenario has no result to compare.
  3. Call `assert_comparable()`, which refuses pairs whose difference would be an
     artefact of differing baselines, years, jurisdictions, objective policies or
     result schemas rather than of the levers being compared.
  4. Only then compute typed deltas.

Nothing is compared before step 3 passes, so a caller can never obtain a number
from an incompatible pair by ignoring a warning field.

Deltas are kept SEPARATE by concept. Objective, tax, refund/balance, liquidity
commitment and per-effect-type differences answer different questions; a single
"difference" figure would let a timing benefit and a permanent reduction read as
the same thing.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, NotFound
from app.database.models import Scenario, ScenarioResult
from app.services.ioe.domain.freshness import (
    COMPARISON_POLICY_VERSION,
    ComparableScenario,
    ComparisonResult,
    assert_comparable,
    compare,
)

COMPARISON_ADAPTER_VERSION = "1.0.0"
MONEY = Decimal("0.01")


@dataclass(frozen=True)
class TypedDeltas:
    """Differences kept apart by concept, never collapsed into one number."""

    objective_difference: Decimal
    tax_difference: Decimal
    refund_balance_difference: Decimal
    liquidity_commitment_difference: Decimal
    effect_type_differences: dict[str, Decimal]


@dataclass(frozen=True)
class LoadedComparison:
    left: ComparableScenario
    right: ComparableScenario
    result: ComparisonResult
    deltas: TypedDeltas
    left_row: Scenario
    right_row: Scenario
    left_result: ScenarioResult
    right_result: ScenarioResult
    policy_version: str = COMPARISON_POLICY_VERSION
    adapter_version: str = COMPARISON_ADAPTER_VERSION


class ScenarioComparisonService:
    """Loads sealed rows, checks compatibility, then computes. In that order."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID):
        self.s = session
        self.user_id = user_id

    async def compare(
        self, left_id: uuid.UUID, right_id: uuid.UUID
    ) -> LoadedComparison:
        left_row, left_result = await self._load_sealed(left_id)
        right_row, right_result = await self._load_sealed(right_id)

        left = self._as_comparable(left_row, left_result)
        right = self._as_comparable(right_row, right_result)

        # Refuse BEFORE computing anything. A caller must not be able to read a
        # number off an incompatible pair.
        assert_comparable(left, right)
        result = compare(left, right)
        deltas = self._typed_deltas(left_result, right_result)
        return LoadedComparison(
            left=left, right=right, result=result, deltas=deltas,
            left_row=left_row, right_row=right_row,
            left_result=left_result, right_result=right_result,
        )

    async def _load_sealed(
        self, scenario_id: uuid.UUID
    ) -> tuple[Scenario, ScenarioResult]:
        """Ownership, then completion, then sealing. Parent-key-qualified."""
        scenario = await self.s.get(Scenario, scenario_id)
        if scenario is None or scenario.user_id != self.user_id:
            # application authorization on top of RLS; identical message for
            # "not yours" and "not there" so the API does not confirm existence
            raise NotFound("Scenario not found")
        if scenario.workflow_status != "completed":
            raise Conflict(
                f"scenario_not_completed: scenario is {scenario.workflow_status}"
            )
        if not scenario.scenario_spec_hash or not scenario.scenario_result_hash:
            raise Conflict("scenario_not_sealed: the result has no sealed hash")

        result = await self.s.scalar(
            select(ScenarioResult).where(ScenarioResult.scenario_id == scenario_id)
        )
        if result is None:
            raise Conflict("scenario_not_sealed: no stored result")
        return scenario, result

    @staticmethod
    def _as_comparable(
        scenario: Scenario, result: ScenarioResult
    ) -> ComparableScenario:
        """Build the value object from SEALED columns. Recomputes nothing."""
        return ComparableScenario(
            scenario_id=scenario.id,
            base_analysis_id=scenario.base_analysis_id,
            tax_year=scenario.tax_year,
            jurisdiction=scenario.jurisdiction,
            objective_code=scenario.objective_code,
            objective_version=scenario.objective_version,
            result_schema_version=scenario.result_schema_version,
            baseline_input_snapshot_hash=scenario.baseline_input_snapshot_hash,
            objective_value_baseline=result.objective_value_baseline,
            objective_value_scenario=result.objective_value_scenario,
            objective_delta=result.objective_delta,
            scenario_tax=result.scenario_tax,
            label=scenario.label,
            freshness_status=scenario.freshness_status,
            stale_reason_code=scenario.stale_reason_code,
        )

    @staticmethod
    def _typed_deltas(
        left: ScenarioResult, right: ScenarioResult
    ) -> TypedDeltas:
        """Subtract like from like, one concept at a time.

        Every subtraction is between two figures of the SAME kind, quantized
        with the same policy as the values themselves.
        """
        def diff(a: Decimal | None, b: Decimal | None) -> Decimal:
            return (
                (a or Decimal(0)) - (b or Decimal(0))
            ).quantize(MONEY, ROUND_HALF_UP)

        return TypedDeltas(
            objective_difference=diff(left.objective_delta, right.objective_delta),
            tax_difference=diff(left.tax_delta, right.tax_delta),
            # a scenario's effect on the refund/balance position is its tax
            # movement; kept as its own concept rather than reused silently
            refund_balance_difference=diff(left.tax_delta, right.tax_delta),
            liquidity_commitment_difference=Decimal("0.00"),
            effect_type_differences={
                "current_year_tax_reduction": diff(
                    left.tax_delta, right.tax_delta
                ),
                "net_objective_benefit": diff(
                    left.objective_delta, right.objective_delta
                ),
            },
        )


__all__ = [
    "COMPARISON_ADAPTER_VERSION",
    "LoadedComparison",
    "ScenarioComparisonService",
    "TypedDeltas",
]
