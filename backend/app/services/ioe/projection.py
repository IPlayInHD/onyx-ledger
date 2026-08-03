"""Multi-year projections (§P6) — deliberately kept apart from current-year totals.

A projection and a current-year engine result look alike on screen and are not
alike at all. The engine result is a deterministic calculation from data the
user supplied. A projection additionally assumes things nobody knows: that
income holds, that indexation continues, that the rules do not change. Adding
one to the other would let assumption-laden figures inherit the credibility of a
calculated one.

So projections are computed here, stored in their own table, returned through
their own response type, and never summed into `portfolio_total_benefit` or any
scenario total. Each one carries its horizon, its assumptions, this module's
methodology version, and its uncertainty.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MultiYearProjection
from app.services.ioe.domain.enums import EconomicEffectType

PROJECTION_METHODOLOGY_VERSION = "1.0.0"

MONEY = Decimal("0.01")
MAX_HORIZON_YEARS = 10

# Effects that can legitimately be projected forward. A one-off refund impact
# cannot: repeating it would invent money the rule never promised.
PROJECTABLE_EFFECTS = frozenset({
    EconomicEffectType.RECURRING_ANNUAL_BENEFIT,
    EconomicEffectType.MULTI_YEAR_PROJECTED_BENEFIT,
})


class ProjectionNotApplicable(ValueError):
    """This effect cannot be projected forward without inventing value."""


@dataclass(frozen=True)
class ProjectedYear:
    horizon_year: int
    amount: Decimal
    effect_type: str
    is_indexation_known: bool


@dataclass(frozen=True)
class Projection:
    """A projection plus everything needed to read it honestly."""

    years: tuple[ProjectedYear, ...]
    methodology_version: str = PROJECTION_METHODOLOGY_VERSION

    @property
    def total(self) -> Decimal:
        """Sum of the PROJECTED years only.

        This total is never combined with a current-year figure; it exists so a
        projection can state its own magnitude.
        """
        return sum(
            (y.amount for y in self.years), Decimal(0)
        ).quantize(MONEY, ROUND_HALF_UP)

    @property
    def horizon_years(self) -> int:
        return len(self.years)


def project_recurring(
    *,
    annual_amount: Decimal,
    effect_type: EconomicEffectType,
    base_tax_year: int,
    horizon_years: int,
    indexation_known: bool = False,
) -> Projection:
    """Carry a recurring annual benefit forward, flat.

    No growth rate is applied. Indexation is a legislative matter, and the IOE
    does not interpret legislation — where indexation is not published as rule
    data, a flat carry-forward is stated as an assumption rather than a guess
    dressed up as a forecast.
    """
    if effect_type not in PROJECTABLE_EFFECTS:
        raise ProjectionNotApplicable(
            f"{effect_type} is a one-off effect and cannot be projected forward"
        )
    if not 1 <= horizon_years <= MAX_HORIZON_YEARS:
        raise ValueError(f"horizon must be 1..{MAX_HORIZON_YEARS}")

    amount = annual_amount.quantize(MONEY, ROUND_HALF_UP)
    return Projection(
        years=tuple(
            ProjectedYear(
                horizon_year=base_tax_year + offset,
                amount=amount,
                effect_type=effect_type.value,
                is_indexation_known=indexation_known,
            )
            for offset in range(horizon_years)
        )
    )


class ProjectionService:
    """Persists projections against a run. Holds no transaction of its own."""

    def __init__(self, session: AsyncSession):
        self.s = session

    async def persist(
        self,
        run_id: uuid.UUID,
        projection: Projection,
        *,
        candidate_id: uuid.UUID | None = None,
        assumption_set_id: uuid.UUID | None = None,
    ) -> int:
        """Write one row per projected year, in its own table.

        Nothing here touches `strategy_portfolio`: a projection is never folded
        into the current-year totals stored there.
        """
        for year in projection.years:
            self.s.add(MultiYearProjection(
                run_id=run_id,
                candidate_id=candidate_id,
                horizon_year=year.horizon_year,
                projected_amount=year.amount,
                effect_type=year.effect_type,
                calculation_basis="projection_estimate",
                assumption_set_id=assumption_set_id,
                is_indexation_known=year.is_indexation_known,
            ))
        await self.s.flush()
        return len(projection.years)


__all__ = [
    "MAX_HORIZON_YEARS",
    "PROJECTABLE_EFFECTS",
    "PROJECTION_METHODOLOGY_VERSION",
    "Projection",
    "ProjectionNotApplicable",
    "ProjectionService",
    "ProjectedYear",
    "project_recurring",
]
