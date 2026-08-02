"""Economic-effect decomposition and comparable value (architecture §13).

Two rules this module exists to enforce:

1. **Different economic outcomes are never silently added.** A tax deferral is a
   timing benefit, not a permanent reduction; a five-year projection is not a
   peer of an equal-dollar current-year saving. Effects are kept separate and
   reported by type and horizon.

2. **`comparable_value` is an internal RANKING quantity only.** It applies effect
   factors and horizon discounting to make candidates orderable. It must never
   be displayed as a dollar amount — every displayed figure is the raw amount of
   a single effect type, with its basis label and horizon.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.services.ioe.domain import canonical as c
from app.services.ioe.domain.enums import CostType, EconomicEffectType, ObjectiveMetric
from app.services.ioe.domain.models import CostComponent, EconomicEffect

SAVINGS_DECOMPOSITION_VERSION = "1.0.0"
VALUE_NORMALIZATION_POLICY_VERSION = "1.0.0"
PORTFOLIO_OBJECTIVE_VERSION = "1.0.0"

MONEY = Decimal("0.01")
EPSILON_ACCEPT = Decimal("0.01")     # Decimal, never a float literal (§E)

# Effect weighting for comparable value. Deferral and option value are weighted
# far below a permanent reduction — they are decisions D-5 (financial sign-off),
# recorded here as data so the judgement is visible and versioned.
EFFECT_FACTORS: dict[EconomicEffectType, Decimal] = {
    EconomicEffectType.IMMEDIATE_REFUND_IMPACT: Decimal("1.00"),
    EconomicEffectType.CURRENT_YEAR_TAX_REDUCTION: Decimal("1.00"),
    EconomicEffectType.REFUNDABLE_BENEFIT: Decimal("1.00"),
    EconomicEffectType.RECURRING_ANNUAL_BENEFIT: Decimal("1.00"),
    EconomicEffectType.MULTI_YEAR_PROJECTED_BENEFIT: Decimal("1.00"),
    EconomicEffectType.TAX_DEFERRAL: Decimal("0.25"),        # timing, not reduction
    EconomicEffectType.FUTURE_OPTION_VALUE: Decimal("0.10"),
}

DISCOUNT_RATE = Decimal("0.05")      # conservative default (D-5)

COST_FACTORS: dict[CostType, Decimal] = {
    CostType.REQUIRED_EXPENDITURE: Decimal("1.00"),
    CostType.IMPLEMENTATION_COST: Decimal("1.00"),
    # A retained asset is not a cost — it is a liquidity constraint (§B).
    CostType.REQUIRED_CASH_CONTRIBUTION: Decimal("0.00"),
}


def horizon_discount(horizon_years: int) -> Decimal:
    """1 / (1+r)^(h-1). A benefit in the current year is undiscounted."""
    if horizon_years <= 1:
        return Decimal("1")
    discount = Decimal(1)
    for _ in range(horizon_years - 1):
        discount /= (Decimal(1) + DISCOUNT_RATE)
    return discount


def comparable_value(
    effects: tuple[EconomicEffect, ...], costs: tuple[CostComponent, ...] = ()
) -> Decimal:
    """Internal ranking quantity. NEVER display this as a dollar figure."""
    total = Decimal(0)
    for e in effects:
        weight = EFFECT_FACTORS.get(e.effect_type, Decimal("1.00"))
        total += e.amount * weight * horizon_discount(e.horizon_years)
    for x in costs:
        total -= x.amount * COST_FACTORS.get(x.cost_type, Decimal("1.00"))
    return total.quantize(MONEY, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class SavingsBreakdown:
    """Effects grouped so no two kinds of outcome are ever conflated."""

    current_year_reduction: Decimal = Decimal(0)
    refund_impact: Decimal = Decimal(0)
    refundable_benefit: Decimal = Decimal(0)
    deferral_amount: Decimal = Decimal(0)
    recurring_annual: Decimal = Decimal(0)
    multi_year_projected: Decimal = Decimal(0)
    future_option_value: Decimal = Decimal(0)
    required_cash_contribution: Decimal = Decimal(0)
    required_expenditure: Decimal = Decimal(0)
    implementation_cost: Decimal = Decimal(0)

    @property
    def net_current_year_benefit(self) -> Decimal:
        """Current-year money in, minus true costs. Deferral is NOT included."""
        return (
            self.current_year_reduction + self.refund_impact + self.refundable_benefit
            - self.required_expenditure - self.implementation_cost
        ).quantize(MONEY, rounding=ROUND_HALF_UP)

    def as_canonical(self) -> dict:
        return {
            "current_year_reduction": c.money(self.current_year_reduction),
            "refund_impact": c.money(self.refund_impact),
            "refundable_benefit": c.money(self.refundable_benefit),
            "deferral_amount": c.money(self.deferral_amount),
            "recurring_annual": c.money(self.recurring_annual),
            "multi_year_projected": c.money(self.multi_year_projected),
            "future_option_value": c.money(self.future_option_value),
            "required_cash_contribution": c.money(self.required_cash_contribution),
            "required_expenditure": c.money(self.required_expenditure),
            "implementation_cost": c.money(self.implementation_cost),
            "net_current_year_benefit": c.money(self.net_current_year_benefit),
        }


_EFFECT_FIELD = {
    EconomicEffectType.CURRENT_YEAR_TAX_REDUCTION: "current_year_reduction",
    EconomicEffectType.IMMEDIATE_REFUND_IMPACT: "refund_impact",
    EconomicEffectType.REFUNDABLE_BENEFIT: "refundable_benefit",
    EconomicEffectType.TAX_DEFERRAL: "deferral_amount",
    EconomicEffectType.RECURRING_ANNUAL_BENEFIT: "recurring_annual",
    EconomicEffectType.MULTI_YEAR_PROJECTED_BENEFIT: "multi_year_projected",
    EconomicEffectType.FUTURE_OPTION_VALUE: "future_option_value",
}
_COST_FIELD = {
    CostType.REQUIRED_CASH_CONTRIBUTION: "required_cash_contribution",
    CostType.REQUIRED_EXPENDITURE: "required_expenditure",
    CostType.IMPLEMENTATION_COST: "implementation_cost",
}


def decompose(
    effects: tuple[EconomicEffect, ...], costs: tuple[CostComponent, ...] = ()
) -> SavingsBreakdown:
    """Group effects and costs by kind. Nothing is summed across kinds."""
    totals: dict[str, Decimal] = {}
    for e in effects:
        key = _EFFECT_FIELD.get(e.effect_type)
        if key:
            totals[key] = totals.get(key, Decimal(0)) + e.amount
    for x in costs:
        key = _COST_FIELD.get(x.cost_type)
        if key:
            totals[key] = totals.get(key, Decimal(0)) + x.amount
    return SavingsBreakdown(**totals)


# ---------------------------------------------------------------------------
# Objective function (architecture §B)
# ---------------------------------------------------------------------------
def objective_value(
    *,
    metric: ObjectiveMetric,
    baseline_tax: Decimal,
    current_tax: Decimal,
    effects: tuple[EconomicEffect, ...] = (),
    costs: tuple[CostComponent, ...] = (),
    available_cash: Decimal | None = None,
) -> Decimal:
    """The value the assembler maximizes. Explicit and versioned — never implied.

    A required cash contribution is treated as a LIQUIDITY constraint (it can
    push the value down only once it exceeds declared available cash), not as a
    cost, because the asset is retained.
    """
    reduction = baseline_tax - current_tax

    if metric is ObjectiveMetric.CURRENT_YEAR_TAX_REDUCTION:
        value = reduction

    elif metric is ObjectiveMetric.CURRENT_YEAR_TAX_REDUCTION_NET_OF_EXPENDITURE:
        expenditure = sum(
            (x.amount for x in costs if x.cost_type is CostType.REQUIRED_EXPENDITURE),
            Decimal(0),
        )
        value = reduction - expenditure

    elif metric is ObjectiveMetric.NET_CASH_BENEFIT_CURRENT_YEAR:
        true_cost = sum((x.amount for x in costs if x.is_true_cost), Decimal(0))
        value = reduction - true_cost

    elif metric is ObjectiveMetric.COMPARABLE_VALUE_MULTI_HORIZON:
        value = comparable_value(effects, costs)

    else:  # pragma: no cover - exhaustive over the enum
        raise ValueError(f"unsupported objective metric: {metric}")

    value -= _liquidity_penalty(costs, available_cash)
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _liquidity_penalty(
    costs: tuple[CostComponent, ...], available_cash: Decimal | None
) -> Decimal:
    """Zero while contributions stay within declared available cash."""
    if available_cash is None:
        return Decimal(0)
    contributions = sum(
        (x.amount for x in costs
         if x.cost_type is CostType.REQUIRED_CASH_CONTRIBUTION),
        Decimal(0),
    )
    overshoot = contributions - available_cash
    return overshoot if overshoot > 0 else Decimal(0)
