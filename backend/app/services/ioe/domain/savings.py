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
from app.services.ioe.domain.enums import (
    LIQUIDITY_COMMITMENT_TYPES,
    NONRECOVERABLE_COST_TYPES,
    CostType,
    EconomicEffectType,
    ObjectiveMetric,
)
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

# Only NONRECOVERABLE money reduces value. A liquidity commitment or an asset
# transfer constrains feasibility but is not a loss, so it carries no weight
# here — it is handled by the liquidity constraint instead.
COST_FACTORS: dict[CostType, Decimal] = {
    **{t: Decimal("1.00") for t in NONRECOVERABLE_COST_TYPES},
    **{t: Decimal("0.00") for t in LIQUIDITY_COMMITMENT_TYPES},
}

# The objective actually used is pinned by CODE and VERSION on every portfolio,
# so a stored result can be re-derived rather than taken on trust.
PORTFOLIO_OBJECTIVE_CODE = ObjectiveMetric.CURRENT_YEAR_TAX_REDUCTION_NET_OF_EXPENDITURE
PORTFOLIO_OBJECTIVE_VERSION = PORTFOLIO_OBJECTIVE_VERSION_1 = "1.0.0"


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
    liquidity_commitment: Decimal = Decimal(0)
    asset_transfer: Decimal = Decimal(0)
    nonrecoverable_expenditure: Decimal = Decimal(0)
    implementation_cost: Decimal = Decimal(0)

    @property
    def net_current_year_benefit(self) -> Decimal:
        """Current-year money in, minus true costs. Deferral is NOT included."""
        return (
            self.current_year_reduction + self.refund_impact + self.refundable_benefit
            - self.nonrecoverable_expenditure - self.implementation_cost
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
            "liquidity_commitment": c.money(self.liquidity_commitment),
            "asset_transfer": c.money(self.asset_transfer),
            "nonrecoverable_expenditure": c.money(self.nonrecoverable_expenditure),
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
    CostType.LIQUIDITY_COMMITMENT: "liquidity_commitment",
    CostType.ASSET_TRANSFER: "asset_transfer",
    CostType.NONRECOVERABLE_EXPENDITURE: "nonrecoverable_expenditure",
    CostType.IMPLEMENTATION_COST: "implementation_cost",
    # legacy aliases map onto the concept they represented
    CostType.REQUIRED_CASH_CONTRIBUTION: "liquidity_commitment",
    CostType.REQUIRED_EXPENDITURE: "nonrecoverable_expenditure",
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
            (x.amount for x in costs
             if x.cost_type in (CostType.NONRECOVERABLE_EXPENDITURE,
                                CostType.REQUIRED_EXPENDITURE)),
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
        (x.amount for x in costs if x.cost_type in LIQUIDITY_COMMITMENT_TYPES),
        Decimal(0),
    )
    overshoot = contributions - available_cash
    return overshoot if overshoot > 0 else Decimal(0)


def objective_cost(
    *,
    metric: ObjectiveMetric,
    current_tax: Decimal,
    effects: tuple[EconomicEffect, ...] = (),
    costs: tuple[CostComponent, ...] = (),
    available_cash: Decimal | None = None,
) -> Decimal:
    """The portfolio objective expressed as a quantity to MINIMIZE.

    Sign convention, fixed once here and used by every stage of assembly:

        objective_delta = baseline_objective_cost - final_objective_cost

    so a POSITIVE delta is an improvement. Expressing the objective as a cost
    (rather than as a benefit to maximize) is what makes that formula and that
    reading agree; a benefit-shaped objective would need the subtraction the
    other way round and would silently invert every stored delta.

    Only NONRECOVERABLE money is added to the cost. A liquidity commitment or an
    asset transfer constrains feasibility but is not a loss, so it enters only
    through the liquidity penalty when it exceeds declared available cash.
    """
    if metric is ObjectiveMetric.CURRENT_YEAR_TAX_REDUCTION:
        cost = current_tax

    elif metric is ObjectiveMetric.CURRENT_YEAR_TAX_REDUCTION_NET_OF_EXPENDITURE:
        expenditure = sum(
            (x.amount for x in costs
             if x.cost_type in (CostType.NONRECOVERABLE_EXPENDITURE,
                                CostType.REQUIRED_EXPENDITURE)),
            Decimal(0),
        )
        cost = current_tax + expenditure

    elif metric is ObjectiveMetric.NET_CASH_BENEFIT_CURRENT_YEAR:
        cost = current_tax + sum(
            (x.amount for x in costs if x.is_true_cost), Decimal(0)
        )

    elif metric is ObjectiveMetric.COMPARABLE_VALUE_MULTI_HORIZON:
        # negate the benefit so that lower is still better
        cost = -comparable_value(effects, costs)

    else:  # pragma: no cover - exhaustive over the enum
        raise ValueError(f"unsupported objective metric: {metric}")

    cost += _liquidity_penalty(costs, available_cash)
    return cost.quantize(MONEY, rounding=ROUND_HALF_UP)
