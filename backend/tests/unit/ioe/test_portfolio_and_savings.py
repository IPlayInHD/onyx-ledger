"""Portfolio assembly, savings decomposition, and interaction mathematics.

This is where the blocking correction is verified: shared pools cannot be
allocated twice, the total comes from a combined engine run, and the telescoping
invariant holds so per-card figures sum to the headline.
"""
from decimal import Decimal

import pytest

from app.services.ioe.domain import portfolio as pf
from app.services.ioe.domain import savings
from app.services.ioe.domain.enums import (
    AdditivityClass,
    AssemblyMethod,
    CalculationBasis,
    CostType,
    EconomicEffectType,
    EligibilityStatus,
    EvidenceStatus,
    ObjectiveMetric,
    OptimalityClaim,
    PortfolioMembership,
    RelationshipType,
)
from app.services.ioe.domain.models import (
    CostComponent,
    EconomicEffect,
    LeverApplication,
    OptimizationCandidate,
    RecommendationRelationship,
)


def _effect(amount, effect_type=EconomicEffectType.CURRENT_YEAR_TAX_REDUCTION, horizon=1):
    return EconomicEffect(
        effect_type=effect_type, amount=Decimal(amount),
        calculation_basis=CalculationBasis.ENGINE_DETERMINED, horizon_years=horizon,
    )


def _candidate(key, lever, amount, *, resources=(), costs=(), standalone=None):
    return OptimizationCandidate(
        candidate_key=key, opportunity_code=key.lower(), rule_version_id=f"rv-{key}",
        eligibility_status=EligibilityStatus.ELIGIBLE,
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.ENGINE_DETERMINED,
        economic_effects=(_effect(amount),),
        costs=costs,
        shared_resource_codes=resources,
        lever_application=LeverApplication(lever, {"amount": Decimal(amount)}),
        standalone_potential=Decimal(standalone) if standalone is not None else None,
    )


def _linear_engine(rate="0.20", baseline=Decimal("10000")):
    """A simple deterministic stand-in: tax falls by `rate` of total deductions."""
    def evaluate(inputs: dict) -> Decimal:
        deductions = sum(
            (v for k, v in inputs.items() if isinstance(v, Decimal)), Decimal(0)
        )
        return baseline - deductions * Decimal(rate)
    return evaluate


BASE = {"rrsp_deduction": Decimal(0), "donations": Decimal(0), "medical_expenses": Decimal(0)}


# ---- savings decomposition --------------------------------------------------
def test_effects_are_grouped_never_summed_across_kinds():
    breakdown = savings.decompose(
        (_effect("1000"),
         _effect("4000", EconomicEffectType.TAX_DEFERRAL),
         _effect("300", EconomicEffectType.RECURRING_ANNUAL_BENEFIT)),
    )
    assert breakdown.current_year_reduction == Decimal("1000")
    assert breakdown.deferral_amount == Decimal("4000")
    assert breakdown.recurring_annual == Decimal("300")
    # a deferral is never folded into the current-year benefit
    assert breakdown.net_current_year_benefit == Decimal("1000")


def test_contribution_is_not_a_cost_but_expenditure_is():
    contribution = savings.decompose(
        (_effect("1000"),),
        (CostComponent(CostType.REQUIRED_CASH_CONTRIBUTION, Decimal("5000")),),
    )
    expenditure = savings.decompose(
        (_effect("1000"),),
        (CostComponent(CostType.REQUIRED_EXPENDITURE, Decimal("5000")),),
    )
    assert contribution.net_current_year_benefit == Decimal("1000")
    assert expenditure.net_current_year_benefit == Decimal("-4000")


def test_deferral_is_weighted_far_below_a_permanent_reduction():
    reduction = savings.comparable_value((_effect("1000"),))
    deferral = savings.comparable_value(
        (_effect("1000", EconomicEffectType.TAX_DEFERRAL),)
    )
    assert deferral < reduction


def test_future_projection_cannot_rank_as_a_peer_of_an_immediate_saving():
    """A 5-year projected benefit must not equal an equal-dollar current saving."""
    now = savings.comparable_value((_effect("1000"),))
    later = savings.comparable_value(
        (_effect("1000", EconomicEffectType.MULTI_YEAR_PROJECTED_BENEFIT, horizon=5),)
    )
    assert later < now


def test_horizon_discount_is_monotonic():
    values = [savings.horizon_discount(h) for h in (1, 2, 3, 5, 10)]
    assert values == sorted(values, reverse=True)
    assert values[0] == Decimal(1)


def test_objective_treats_contribution_as_liquidity_not_cost():
    costs = (CostComponent(CostType.REQUIRED_CASH_CONTRIBUTION, Decimal("5000")),)
    value = savings.objective_value(
        metric=ObjectiveMetric.CURRENT_YEAR_TAX_REDUCTION_NET_OF_EXPENDITURE,
        baseline_tax=Decimal("10000"), current_tax=Decimal("9000"), costs=costs,
        available_cash=Decimal("10000"),
    )
    assert value == Decimal("1000")      # contribution within cash: no penalty

    penalized = savings.objective_value(
        metric=ObjectiveMetric.CURRENT_YEAR_TAX_REDUCTION_NET_OF_EXPENDITURE,
        baseline_tax=Decimal("10000"), current_tax=Decimal("9000"), costs=costs,
        available_cash=Decimal("1000"),
    )
    assert penalized < value             # beyond available cash: penalized


# ---- portfolio assembly -----------------------------------------------------
def test_total_comes_from_the_combined_engine_run_and_telescopes():
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000", standalone="200")
    b = _candidate("B", "INCREASE_DONATIONS", "500", standalone="100")
    result = pf.assemble([a, b], [], BASE, _linear_engine())

    assert result.portfolio_total_benefit == Decimal("300.00")   # (1000+500)*0.20
    # Invariant I-1: per-card incrementals sum EXACTLY to the headline
    assert pf.verify_telescoping(result.members, result.portfolio_total_benefit)


def test_shared_pool_cannot_be_allocated_twice():
    """The anti-double-counting ledger: two candidates on one pool of 1000."""
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "800", resources=("RRSP_ROOM",),
                   costs=(CostComponent(CostType.REQUIRED_CASH_CONTRIBUTION, Decimal("800")),))
    b = _candidate("B", "INCREASE_RRSP_DEDUCTION", "800", resources=("RRSP_ROOM",),
                   costs=(CostComponent(CostType.REQUIRED_CASH_CONTRIBUTION, Decimal("800")),))
    cons = pf.AssemblyConstraints(resource_capacities={"RRSP_ROOM": Decimal("1000")})
    result = pf.assemble([a, b], [], BASE, _linear_engine(), cons)

    assert len(result.members) == 1
    assert b.portfolio_membership is PortfolioMembership.EXCLUDED_CONSTRAINT
    assert b.exclusion_reason_code == "SHARED_RESOURCE_EXHAUSTED"
    ledger = {e.resource_code: e for e in result.ledger}
    assert ledger["RRSP_ROOM"].allocated <= ledger["RRSP_ROOM"].capacity


def test_excluding_relationship_blocks_the_lower_ranked_candidate():
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000")
    b = _candidate("B", "INCREASE_DONATIONS", "500")
    edge = RecommendationRelationship(
        source_key="A", target_key="B", relationship_type=RelationshipType.EXCLUDES,
        explanation_code="MUTUALLY_EXCLUSIVE",
    )
    result = pf.assemble([a, b], [edge], BASE, _linear_engine())
    assert [m.candidate_key for m in result.members] == ["A"]
    assert b.portfolio_membership is PortfolioMembership.EXCLUDED_CONFLICT


def test_requires_relationship_defers_when_prerequisite_absent():
    b = _candidate("B", "INCREASE_DONATIONS", "500")
    edge = RecommendationRelationship(
        source_key="B", target_key="A", relationship_type=RelationshipType.REQUIRES,
        explanation_code="PREREQUISITE",
    )
    result = pf.assemble([b], [edge], BASE, _linear_engine())
    assert result.members == ()
    assert b.portfolio_membership is PortfolioMembership.DEFERRED_TIMING


def test_non_evaluable_candidate_is_excluded_not_counted():
    """Without a lever it cannot enter an engine run, so it cannot be in a total."""
    orphan = OptimizationCandidate(
        candidate_key="X", opportunity_code="x", rule_version_id="rv-x",
        eligibility_status=EligibilityStatus.INDETERMINATE,
        economic_effects=(_effect("9999"),),
    )
    result = pf.assemble([orphan], [], BASE, _linear_engine())
    assert result.members == ()
    assert result.portfolio_total_benefit == Decimal("0.00")
    assert orphan.portfolio_membership is PortfolioMembership.EXCLUDED_NOT_EVALUABLE


def test_candidate_that_does_not_help_is_deferred_not_discarded():
    """L-3 mitigation: near ceilings a candidate may only pay off in combination."""
    useless = _candidate("A", "INCREASE_RRSP_DEDUCTION", "0")

    def flat_engine(inputs):        # nothing ever changes the tax
        return Decimal("10000")

    result = pf.assemble([useless], [], BASE, flat_engine)
    assert useless.portfolio_membership is PortfolioMembership.DEFERRED_PENDING_COMBINATION
    assert result.deferred_count == 1


def test_sub_additive_interaction_is_detected_and_signed_correctly():
    """Overlap means summing the cards would OVERSTATE the benefit."""
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000", standalone="200")
    b = _candidate("B", "INCREASE_DONATIONS", "500", standalone="250")   # inflated
    result = pf.assemble([a, b], [], BASE, _linear_engine())

    assert result.sum_of_standalone == Decimal("450.00")
    assert result.portfolio_total_benefit == Decimal("300.00")
    assert result.interaction_delta == Decimal("150.00")     # positive ⇒ overstated
    assert result.additivity_class is AdditivityClass.SUB_ADDITIVE
    assert result.additivity_verified is False


def test_additive_case_is_verified():
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000", standalone="200")
    b = _candidate("B", "INCREASE_DONATIONS", "500", standalone="100")
    result = pf.assemble([a, b], [], BASE, _linear_engine())
    assert result.interaction_delta == Decimal("0.00")
    assert result.additivity_class is AdditivityClass.ADDITIVE
    assert result.additivity_verified is True


def test_classification_thresholds_use_decimal_epsilon():
    assert pf.classify_additivity(Decimal("0.01")) is AdditivityClass.ADDITIVE
    assert pf.classify_additivity(Decimal("0.02")) is AdditivityClass.SUB_ADDITIVE
    assert pf.classify_additivity(Decimal("-0.02")) is AdditivityClass.SUPER_ADDITIVE


def test_per_candidate_interaction_delta():
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000", standalone="250")
    pf.assemble([a], [], BASE, _linear_engine())
    # standalone 250 vs incremental 200 ⇒ 50 of the standalone figure was overlap
    assert pf.interaction_delta_for(a) == Decimal("50.00")


def test_optimality_is_never_claimed():
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000")
    result = pf.assemble([a], [], BASE, _linear_engine())
    assert result.optimality_claim in (OptimalityClaim.NONE, OptimalityClaim.LOCALLY_IMPROVED)
    assert "optimal" not in {c.value for c in OptimalityClaim} - {"none"}
    assert result.assembly_method in (
        AssemblyMethod.GREEDY_RANKED, AssemblyMethod.GREEDY_RANKED_WITH_LOCAL_IMPROVEMENT,
    )


def test_assembly_is_deterministic():
    def build():
        return [_candidate("A", "INCREASE_RRSP_DEDUCTION", "1000", standalone="200"),
                _candidate("B", "INCREASE_DONATIONS", "500", standalone="100")]

    first = pf.assemble(build(), [], BASE, _linear_engine())
    second = pf.assemble(build(), [], BASE, _linear_engine())
    assert first.as_canonical() == second.as_canonical()


def test_reconciliation_failure_is_a_hard_error():
    """A total that cannot be reproduced by re-running the engine is refused."""
    # call 1 = baseline, call 2 = trial (accepted), call 3 = final combined run.
    # The final run disagrees with the accepted trial, so the total is unprovable.
    sequence = iter([Decimal("9000"), Decimal("8000"), Decimal("7000")])

    def drifting_engine(inputs):
        return next(sequence)

    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000")
    with pytest.raises(pf.PortfolioReconciliationError, match="disagrees"):
        pf.assemble([a], [], BASE, drifting_engine)


def test_cash_constraint_excludes_unaffordable_candidates():
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000",
                   costs=(CostComponent(CostType.REQUIRED_CASH_CONTRIBUTION, Decimal("9000")),))
    cons = pf.AssemblyConstraints(available_cash=Decimal("1000"))
    result = pf.assemble([a], [], BASE, _linear_engine(), cons)
    assert result.members == ()
    assert a.exclusion_reason_code == "INSUFFICIENT_CASH"


# ---------------------------------------------------------------------------
# Invariants I-1 and I-2, and what is deliberately NOT an invariant
# ---------------------------------------------------------------------------
def test_I1_telescoping_holds_exactly_for_every_shape():
    """I-1: Σ incremental_i == portfolio_total_benefit, to the cent, always."""
    for amounts in (["1000"], ["1000", "500"], ["100", "200", "300", "400"]):
        candidates = [
            _candidate(f"C{i}", "INCREASE_RRSP_DEDUCTION", amt, standalone="1")
            for i, amt in enumerate(amounts)
        ]
        result = pf.assemble(candidates, [], BASE, _linear_engine())
        total = sum((m.incremental_benefit for m in result.members), Decimal(0))
        assert total == result.portfolio_total_benefit
        assert pf.verify_telescoping(result.members, result.portfolio_total_benefit)


def test_I2_reconciliation_holds_when_levers_are_order_independent():
    """I-2: the combined run equals the last accepted trial run."""
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000")
    b = _candidate("B", "INCREASE_DONATIONS", "500")
    result = pf.assemble([a, b], [], BASE, _linear_engine())
    # portfolio_tax IS the combined run; the assembler raises if it disagreed
    assert result.portfolio_tax == result.baseline_tax - result.portfolio_total_benefit


def test_super_additive_synergy_is_permitted_not_treated_as_an_error():
    """Combined benefit MAY exceed the sum of standalone values. There is
    deliberately no invariant that portfolio_total <= sum_of_standalone."""
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000", standalone="100")
    b = _candidate("B", "INCREASE_DONATIONS", "500", standalone="100")
    result = pf.assemble([a, b], [], BASE, _linear_engine())

    assert result.sum_of_standalone == Decimal("200.00")
    assert result.portfolio_total_benefit == Decimal("300.00")   # exceeds the sum
    assert result.interaction_delta == Decimal("-100.00")        # negative ⇒ synergy
    assert result.additivity_class is AdditivityClass.SUPER_ADDITIVE
    # I-1 still holds regardless of the sign of the interaction
    assert pf.verify_telescoping(result.members, result.portfolio_total_benefit)


def test_interaction_sign_convention_is_consistent_in_both_directions():
    """Positive delta = summing would OVERSTATE; negative = would UNDERSTATE."""
    over = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000", standalone="400")
    result_over = pf.assemble([over], [], BASE, _linear_engine())
    assert result_over.interaction_delta > 0
    assert result_over.additivity_class is AdditivityClass.SUB_ADDITIVE

    under = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000", standalone="50")
    result_under = pf.assemble([under], [], BASE, _linear_engine())
    assert result_under.interaction_delta < 0
    assert result_under.additivity_class is AdditivityClass.SUPER_ADDITIVE


def test_per_candidate_deltas_sum_to_the_aggregate_delta():
    """Consistency between per-candidate and aggregate interaction, which
    follows from I-1."""
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000", standalone="250")
    b = _candidate("B", "INCREASE_DONATIONS", "500", standalone="150")
    result = pf.assemble([a, b], [], BASE, _linear_engine())
    per_candidate = sum(
        (pf.interaction_delta_for(x) for x in (a, b)), Decimal(0)
    )
    assert per_candidate == result.interaction_delta


def test_invariants_share_one_objective_and_rounding_stage():
    """I-1 and I-2 are stated over the same objective, sign convention, Decimal
    policy, and rounding stage — money scale 2, ROUND_HALF_UP, quantized once."""
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "333.33", standalone="66.67")
    result = pf.assemble([a], [], BASE, _linear_engine(),
                         pf.AssemblyConstraints(
                             objective_metric=ObjectiveMetric.CURRENT_YEAR_TAX_REDUCTION))
    assert result.objective_metric is ObjectiveMetric.CURRENT_YEAR_TAX_REDUCTION
    for value in (result.portfolio_total_benefit, result.sum_of_standalone,
                  result.interaction_delta, result.baseline_tax, result.portfolio_tax):
        assert value == value.quantize(Decimal("0.01"))     # single rounding stage
    assert pf.verify_telescoping(result.members, result.portfolio_total_benefit)
