"""Ranking and confidence — invariants, factor discipline, and golden cases."""
from decimal import Decimal

import pytest

from app.services.ioe.domain import confidence as conf
from app.services.ioe.domain import scoring
from app.services.ioe.domain.enums import (
    AssumptionCertainty,
    AssumptionSource,
    CalculationBasis,
    ConfidenceFactor,
    CostType,
    EconomicEffectType,
    EligibilityStatus,
    EvidenceStatus,
    Materiality,
    Reversibility,
    ScoreFactor,
)
from app.services.ioe.domain.models import (
    CostComponent,
    EconomicEffect,
    OptimizationCandidate,
    StructuredAssumption,
)


def _candidate(key: str, amount: str = "1000", **kw) -> OptimizationCandidate:
    base = dict(
        candidate_key=key, opportunity_code=key.lower(), rule_version_id=f"rv-{key}",
        eligibility_status=EligibilityStatus.ELIGIBLE,
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.ENGINE_DETERMINED,
        economic_effects=(EconomicEffect(
            effect_type=EconomicEffectType.CURRENT_YEAR_TAX_REDUCTION,
            amount=Decimal(amount), calculation_basis=CalculationBasis.ENGINE_DETERMINED,
        ),),
    )
    base.update(kw)
    return OptimizationCandidate(**base)


# ---- weight configuration safety -------------------------------------------
def test_default_weights_are_valid_and_total_one():
    validated = scoring.validate_weights(scoring.DEFAULT_WEIGHTS)
    assert sum(validated.values()) == Decimal(1)


def test_weights_missing_a_factor_are_rejected():
    bad = dict(scoring.DEFAULT_WEIGHTS)
    del bad[ScoreFactor.CONFIDENCE]
    with pytest.raises(scoring.WeightConfigError, match="missing factor"):
        scoring.validate_weights(bad)


def test_weights_that_do_not_total_one_are_rejected_not_rescaled():
    bad = dict(scoring.DEFAULT_WEIGHTS)
    bad[ScoreFactor.ECONOMIC_VALUE] = Decimal("0.90")
    with pytest.raises(scoring.WeightConfigError, match="total exactly 1"):
        scoring.validate_weights(bad)


def test_out_of_range_weight_is_rejected():
    bad = dict(scoring.DEFAULT_WEIGHTS)
    bad[ScoreFactor.ECONOMIC_VALUE] = Decimal("-0.1")
    with pytest.raises(scoring.WeightConfigError, match=r"within \[0,1\]"):
        scoring.validate_weights(bad)


# ---- factor discipline ------------------------------------------------------
def test_every_score_factor_appears_exactly_once():
    breakdown = scoring.compute_score(_candidate("A"))
    codes = [comp.factor_code for comp in breakdown.components]
    assert len(codes) == len(set(codes)) == len(ScoreFactor)


def test_confidence_concepts_do_not_reappear_in_the_score():
    """Rule stability, documentation quality, and scenario/projection
    uncertainty belong to confidence only — repeating them would double-weight."""
    score_codes = {f.value for f in ScoreFactor}
    confidence_only = {
        ConfidenceFactor.RULE_STABILITY.value,
        ConfidenceFactor.DOCUMENTATION_QUALITY.value,
        ConfidenceFactor.SCENARIO_UNCERTAINTY.value,
        ConfidenceFactor.PROJECTION_UNCERTAINTY.value,
        ConfidenceFactor.ELIGIBILITY_EVIDENCE_STRENGTH.value,
    }
    assert score_codes & confidence_only == set()


def test_historical_behaviour_is_absent_from_the_financial_score():
    """Decision D-7: prior acceptance/dismissal must not move a financial figure."""
    codes = {f.value for f in ScoreFactor}
    for banned in ("historical", "behaviour", "behavior", "freshness", "dismissed"):
        assert not any(banned in code for code in codes)


# ---- score invariants -------------------------------------------------------
def test_score_is_bounded_and_contributions_sum_to_it():
    breakdown = scoring.compute_score(_candidate("A", "3000"))
    assert Decimal(0) <= breakdown.overall <= Decimal(100)
    total = sum(comp.contribution for comp in breakdown.components)
    assert abs(total * 100 - breakdown.overall) <= Decimal("0.05")


def test_score_is_monotonic_in_economic_value():
    small = scoring.compute_score(_candidate("A", "100"))
    large = scoring.compute_score(_candidate("A", "4000"))
    assert large.overall > small.overall


def test_higher_effort_scores_lower_all_else_equal():
    easy = scoring.compute_score(_candidate("A", effort_rating=1))
    hard = scoring.compute_score(_candidate("A", effort_rating=5))
    assert easy.overall > hard.overall


def test_closer_deadline_scores_higher():
    soon = scoring.compute_score(_candidate("A", days_to_deadline=10))
    later = scoring.compute_score(_candidate("A", days_to_deadline=300))
    assert soon.overall > later.overall


def test_irreversible_scores_lower_than_reversible():
    rev = scoring.compute_score(_candidate("A", reversibility=Reversibility.REVERSIBLE))
    irr = scoring.compute_score(_candidate("A", reversibility=Reversibility.IRREVERSIBLE))
    assert rev.overall > irr.overall


def test_ranking_is_deterministic_regardless_of_input_order():
    a = _candidate("A", "1000")
    b = _candidate("B", "2000")
    c_ = _candidate("C", "1500")
    first = [x.candidate_key for x in scoring.rank([a, b, c_])]
    a2, b2, c2 = _candidate("A", "1000"), _candidate("B", "2000"), _candidate("C", "1500")
    second = [x.candidate_key for x in scoring.rank([c2, a2, b2])]
    assert first == second == ["B", "C", "A"]


def test_ties_break_canonically_by_opportunity_code():
    x = _candidate("ZZZ", "1000")
    y = _candidate("AAA", "1000")
    ordered = scoring.rank([x, y])
    assert [i.candidate_key for i in ordered] == ["AAA", "ZZZ"]
    assert [i.rank for i in ordered] == [1, 2]


# ---- confidence -------------------------------------------------------------
def test_confidence_components_are_omitted_not_zeroed_when_inapplicable():
    """A non-scenario candidate must not be penalized for having no scenario
    uncertainty; its weight is redistributed instead."""
    breakdown = conf.compute(
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.ENGINE_DETERMINED,
    )
    codes = {comp.factor_code for comp in breakdown.components}
    assert ConfidenceFactor.SCENARIO_UNCERTAINTY not in codes
    assert ConfidenceFactor.PROJECTION_UNCERTAINTY not in codes
    # redistributed weights still total 1
    assert abs(sum(comp.weight for comp in breakdown.components) - Decimal(1)) <= Decimal("0.0001")
    assert breakdown.overall == Decimal("100.00")


def test_weaker_evidence_lowers_confidence():
    verified = conf.compute(
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.ENGINE_DETERMINED,
    )
    attested = conf.compute(
        evidence_status=EvidenceStatus.USER_ATTESTED,
        calculation_basis=CalculationBasis.ENGINE_DETERMINED,
    )
    assert attested.overall < verified.overall


def test_projection_basis_is_less_certain_than_engine_basis():
    engine = conf.compute(
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.ENGINE_DETERMINED,
    )
    projection = conf.compute(
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.PROJECTION_ESTIMATE, horizon_years=5,
    )
    assert projection.overall < engine.overall


def _assumption(materiality: Materiality, *, sensitivity=None, eligibility=False):
    return StructuredAssumption(
        code="EMPLOYMENT_INCOME_CONSTANT", value=True,
        source=AssumptionSource.USER, certainty=AssumptionCertainty.USER_ASSERTED,
        materiality=materiality, sensitivity=sensitivity, affects_eligibility=eligibility,
    )


def test_confidence_is_not_reduced_by_assumption_COUNT():
    """Five low-materiality assumptions must not beat one high-materiality one."""
    one_high = conf.compute(
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.SCENARIO_ESTIMATE,
        assumptions=(_assumption(Materiality.HIGH),),
    )
    five_low = conf.compute(
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.SCENARIO_ESTIMATE,
        assumptions=tuple(_assumption(Materiality.LOW) for _ in range(5)),
    )
    assert five_low.overall > one_high.overall


def test_eligibility_affecting_assumption_costs_more_than_amount_only():
    amount_only = conf.compute(
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.SCENARIO_ESTIMATE,
        assumptions=(_assumption(Materiality.MEDIUM),),
    )
    eligibility = conf.compute(
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.SCENARIO_ESTIMATE,
        assumptions=(_assumption(Materiality.MEDIUM, eligibility=True),),
    )
    assert eligibility.overall < amount_only.overall


def test_assumption_bearing_results_are_capped():
    """Certainty is never implied where assumptions exist."""
    result = conf.compute(
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.ENGINE_DETERMINED,
        assumptions=(_assumption(Materiality.LOW),),
    )
    assert result.overall <= conf.ASSUMPTION_CONFIDENCE_CAP


def test_confidence_bounded_and_reason_codes_present():
    breakdown = conf.compute(
        evidence_status=EvidenceStatus.INCOMPLETE,
        calculation_basis=CalculationBasis.PROJECTION_ESTIMATE, horizon_years=5,
    )
    assert Decimal(0) <= breakdown.overall <= Decimal(100)
    assert all(comp.reason_code for comp in breakdown.components)


def test_cost_reduces_score_but_contribution_is_not_a_cost():
    """A retained asset (contribution) must not be treated like an expenditure."""
    contribution = _candidate("A", "1000", costs=(CostComponent(
        cost_type=CostType.REQUIRED_CASH_CONTRIBUTION, amount=Decimal("5000")),))
    expenditure = _candidate("A", "1000", costs=(CostComponent(
        cost_type=CostType.REQUIRED_EXPENDITURE, amount=Decimal("5000")),))
    assert (
        scoring.economic_value_raw(contribution) > scoring.economic_value_raw(expenditure)
    )
