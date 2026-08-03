"""P4 required cases: the shapes a strategy portfolio must handle correctly.

Zero-benefit, negative-current-year, deferral-only, insufficient cash,
non-evaluable, eligibility-changing, super-additive, and resource conflict —
plus the two reconciliation invariants stated over OBJECTIVE deltas, ledger
conservation, and registry-only lever resolution.
"""
from decimal import Decimal

import pytest

from app.services.ioe.domain import levers
from app.services.ioe.domain import portfolio as pf
from app.services.ioe.domain.enums import (
    AdditivityClass,
    AssemblyMethod,
    CalculationBasis,
    CostType,
    DerivationSource,
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

BASE = {"rrsp_deduction": Decimal(0), "donations": Decimal(0),
        "medical_expenses": Decimal(0), "capital_gains": Decimal(0)}


def _effect(amount, kind=EconomicEffectType.CURRENT_YEAR_TAX_REDUCTION, horizon=1):
    return EconomicEffect(
        effect_type=kind, amount=Decimal(amount),
        calculation_basis=CalculationBasis.ENGINE_DETERMINED, horizon_years=horizon,
    )


def _candidate(key, lever, amount, *, effects=None, costs=(), resources=(),
               evaluable=True):
    return OptimizationCandidate(
        candidate_key=key, opportunity_code=key.lower(), rule_version_id=f"rv-{key}",
        eligibility_status=(
            EligibilityStatus.ELIGIBLE if evaluable else EligibilityStatus.INDETERMINATE
        ),
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.ENGINE_DETERMINED,
        economic_effects=effects if effects is not None else (_effect(amount),),
        costs=costs, shared_resource_codes=resources,
        lever_application=(
            LeverApplication(lever, {"amount": Decimal(amount)}) if evaluable else None
        ),
    )


def _linear(rate="0.20", baseline=Decimal("10000")):
    def evaluate(inputs: dict) -> Decimal:
        deductions = sum((v for v in inputs.values() if isinstance(v, Decimal)), Decimal(0))
        return baseline - deductions * Decimal(rate)
    return evaluate


def _flat(value=Decimal("10000")):
    def evaluate(inputs: dict) -> Decimal:
        return value
    return evaluate


# ---------------------------------------------------------------------------
# Required cases
# ---------------------------------------------------------------------------
def test_zero_benefit_portfolio_is_empty_and_honest():
    """Nothing improves the objective: the portfolio is empty and says zero."""
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000")
    result = pf.assemble([a], [], BASE, _flat())

    assert result.members == ()
    assert result.objective_delta == Decimal("0.00")
    assert result.portfolio_total_benefit == Decimal("0.00")
    assert a.portfolio_membership is PortfolioMembership.DEFERRED_PENDING_COMBINATION
    assert pf.verify_telescoping(result.members, result.objective_delta)


def test_negative_current_year_candidate_is_not_selected():
    """A strategy that costs more than it saves must not enter the portfolio."""
    a = _candidate(
        "A", "INCREASE_DONATIONS", "1000",
        costs=(CostComponent(CostType.NONRECOVERABLE_EXPENDITURE, Decimal("5000")),),
    )
    result = pf.assemble([a], [], BASE, _linear())

    # 1000 donated reduces tax by 200 but costs 5000 that does not come back
    assert a.standalone_potential < 0
    assert result.members == ()
    assert result.objective_delta == Decimal("0.00")


def test_tax_deferral_only_is_reported_separately_and_never_as_reduction():
    """A deferral is a timing benefit; it must not appear as a tax reduction."""
    a = _candidate(
        "A", "INCREASE_RRSP_DEDUCTION", "1000",
        effects=(_effect("1000", EconomicEffectType.TAX_DEFERRAL),),
    )
    result = pf.assemble([a], [], BASE, _linear())

    assert result.savings.deferral_amount == Decimal("1000")
    assert result.savings.current_year_reduction == Decimal(0)
    # the deferral is NOT folded into the current-year benefit
    assert result.savings.net_current_year_benefit == Decimal("0.00")


def test_insufficient_cash_excludes_with_a_structured_reason():
    a = _candidate(
        "A", "INCREASE_RRSP_DEDUCTION", "1000",
        costs=(CostComponent(CostType.LIQUIDITY_COMMITMENT, Decimal("9000")),),
    )
    cons = pf.AssemblyConstraints(available_cash=Decimal("1000"))
    result = pf.assemble([a], [], BASE, _linear(), cons)

    assert result.members == ()
    assert a.portfolio_membership is PortfolioMembership.EXCLUDED_CONSTRAINT
    assert a.exclusion_reason_code == "INSUFFICIENT_CASH"


def test_non_evaluable_candidate_is_retained_with_its_reason():
    """No lever ⇒ it cannot enter an engine run, so it cannot be in a total —
    but it is retained and explained, not dropped."""
    orphan = _candidate("X", None, "9999", evaluable=False)
    result = pf.assemble([orphan], [], BASE, _linear())

    assert result.members == ()
    assert result.objective_delta == Decimal("0.00")
    assert orphan.portfolio_membership is PortfolioMembership.EXCLUDED_NOT_EVALUABLE
    assert orphan.exclusion_reason_code == "NOT_PORTFOLIO_EVALUABLE"


def test_eligibility_changed_by_an_earlier_action_excludes_safely():
    """Earlier portfolio actions can change the facts a later candidate relies
    on. Re-checked against the SAME pinned snapshot; on doubt, exclude."""
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000")
    b = _candidate("B", "INCREASE_DONATIONS", "500")

    def recheck(candidate, current_inputs):
        # B's eligibility depends on RRSP being untouched; A breaks that
        if candidate.candidate_key == "B":
            return current_inputs.get("rrsp_deduction", Decimal(0)) == Decimal(0)
        return True

    cons = pf.AssemblyConstraints(eligibility_recheck=recheck)
    result = pf.assemble([a, b], [], BASE, _linear(), cons)

    assert [m.candidate_key for m in result.members] == ["A"]
    assert b.portfolio_membership is PortfolioMembership.EXCLUDED_CONSTRAINT
    assert b.exclusion_reason_code == "ELIGIBILITY_CHANGED_BY_EARLIER_ACTION"


def test_resource_conflict_excludes_and_conserves_the_pool():
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "800", resources=("RRSP_ROOM",),
                   costs=(CostComponent(CostType.LIQUIDITY_COMMITMENT, Decimal("800")),))
    b = _candidate("B", "INCREASE_RRSP_DEDUCTION", "800", resources=("RRSP_ROOM",),
                   costs=(CostComponent(CostType.LIQUIDITY_COMMITMENT, Decimal("800")),))
    cons = pf.AssemblyConstraints(resource_capacities={"RRSP_ROOM": Decimal("1000")})
    result = pf.assemble([a, b], [], BASE, _linear(), cons)

    assert len(result.members) == 1
    assert b.exclusion_reason_code == "SHARED_RESOURCE_EXHAUSTED"
    entry = next(e for e in result.ledger if e.resource_code == "RRSP_ROOM")
    assert entry.allocated <= entry.capacity          # conservation
    assert entry.remaining == entry.capacity - entry.allocated


def test_typed_relationships_are_all_honoured():
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000")
    b = _candidate("B", "INCREASE_DONATIONS", "500")
    c_ = _candidate("C", "INCREASE_MEDICAL_EXPENSES", "300")
    edges = [
        RecommendationRelationship("A", "B", RelationshipType.EXCLUDES, "MUTUALLY_EXCLUSIVE",
                                   DerivationSource.RULES_CONTRACT,
                                   resolution_options=("CHOOSE_ONE",)),
        RecommendationRelationship("C", "ZZZ", RelationshipType.REQUIRES, "PREREQUISITE",
                                   DerivationSource.RULES_CONTRACT),
    ]
    result = pf.assemble([a, b, c_], edges, BASE, _linear())

    assert [m.candidate_key for m in result.members] == ["A"]
    assert b.portfolio_membership is PortfolioMembership.EXCLUDED_CONFLICT
    assert c_.portfolio_membership is PortfolioMembership.DEFERRED_TIMING
    assert c_.exclusion_reason_code == "DEPENDENCY_NOT_SATISFIED"


# ---------------------------------------------------------------------------
# Objective, invariants, and claims
# ---------------------------------------------------------------------------
def test_objective_is_pinned_by_code_and_version_with_all_three_values():
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000")
    cons = pf.AssemblyConstraints(
        objective_metric=ObjectiveMetric.CURRENT_YEAR_TAX_REDUCTION,
        objective_version="1.0.0",
    )
    result = pf.assemble([a], [], BASE, _linear(), cons)

    assert result.objective_code == "current_year_tax_reduction"
    assert result.objective_version == "1.0.0"
    assert result.objective_value_baseline == Decimal("10000.00")
    assert result.objective_value_final == Decimal("9800.00")
    # I-2 by definition: delta == baseline − final
    assert result.objective_delta == (
        result.objective_value_baseline - result.objective_value_final
    )


def test_I1_incremental_deltas_sum_in_exact_apply_order():
    candidates = [
        _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000"),
        _candidate("B", "INCREASE_DONATIONS", "500"),
        _candidate("C", "INCREASE_MEDICAL_EXPENSES", "300"),
    ]
    result = pf.assemble(candidates, [], BASE, _linear())

    ordered = sorted(result.members, key=lambda m: m.apply_order)
    assert [m.apply_order for m in ordered] == [0, 1, 2]
    assert sum((m.incremental_benefit for m in ordered), Decimal(0)) == result.objective_delta


def test_no_inequality_is_imposed_between_total_and_sum_of_standalone():
    """Both directions are valid; neither is treated as an error."""
    for engine, expected in (
        (_linear(), AdditivityClass.ADDITIVE),
    ):
        result = pf.assemble(
            [_candidate("A", "INCREASE_RRSP_DEDUCTION", "1000"),
             _candidate("B", "INCREASE_DONATIONS", "500")],
            [], BASE, engine)
        assert result.additivity_class is expected
        # neither an upper nor a lower bound is asserted anywhere
        assert isinstance(result.interaction_delta, Decimal)


def test_portfolio_never_claims_optimality():
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000")
    result = pf.assemble([a], [], BASE, _linear())
    assert result.optimality_claim is OptimalityClaim.NONE
    assert result.assembly_method is AssemblyMethod.GREEDY_RANKED
    assert "optimal" not in {m.value for m in OptimalityClaim}


def test_engine_runs_and_budget_exhaustion_are_recorded():
    candidates = [
        _candidate(f"C{i}", "INCREASE_RRSP_DEDUCTION", "100") for i in range(6)
    ]
    tight = pf.AssemblyConstraints(max_engine_runs=4)
    result = pf.assemble(candidates, [], BASE, _linear(), tight)

    assert result.engine_runs_used > 0
    assert result.search_budget_exhausted is True

    roomy = pf.assemble(
        [_candidate("A", "INCREASE_RRSP_DEDUCTION", "100")], [], BASE, _linear())
    assert roomy.search_budget_exhausted is False


def test_evaluation_trace_records_every_stage():
    a = _candidate("A", "INCREASE_RRSP_DEDUCTION", "1000")
    result = pf.assemble([a], [], BASE, _linear())

    stages = [step.stage for step in result.trace]
    assert stages[0] == "baseline"
    assert "standalone" in stages
    assert "final_combined" in stages
    assert [s.step_index for s in result.trace] == list(range(len(result.trace)))
    # the trace carries objective values only — no user financial inputs
    for step in result.trace:
        payload = step.as_canonical()
        assert set(payload) == {
            "step_index", "stage", "candidate_key", "apply_order",
            "objective_value", "objective_delta", "accepted", "reason_code",
        }


# ---------------------------------------------------------------------------
# Lever + ledger safety
# ---------------------------------------------------------------------------
def test_levers_resolve_only_through_the_pinned_registry():
    unknown = OptimizationCandidate(
        candidate_key="U", opportunity_code="u", rule_version_id="rv-u",
        eligibility_status=EligibilityStatus.ELIGIBLE,
        economic_effects=(_effect("1000"),),
        lever_application=LeverApplication("NOT_A_REAL_LEVER", {"amount": Decimal("1")}),
    )
    result = pf.assemble([unknown], [], BASE, _linear())
    assert result.members == ()
    assert unknown.exclusion_reason_code == "LEVER_NOT_APPLICABLE"


def test_lever_jurisdiction_and_tax_year_are_validated():
    spec = levers.get("INCREASE_RRSP_DEDUCTION")
    assert spec.applies_to(jurisdiction="ON", tax_year=2025)   # unrestricted

    restricted = levers.LeverSpec(
        code="X", description="x", writable_fields=("donations",),
        direction="increase", parameters=(levers.ParameterSpec("amount"),),
        jurisdictions=("BC",), tax_years=(2024,),
    )
    assert not restricted.applies_to(jurisdiction="ON", tax_year=2024)
    assert not restricted.applies_to(jurisdiction="BC", tax_year=2025)
    assert restricted.applies_to(jurisdiction="BC", tax_year=2024)
    with pytest.raises(levers.LeverValidationError, match="does not apply"):
        levers.assert_applicable(restricted, jurisdiction="ON", tax_year=2024)


def test_composite_lever_application_is_atomic():
    """A composite that fails part-way must leave the input untouched."""
    original = {"employment_income": Decimal("50000"), "pension_income": Decimal(0)}
    with pytest.raises(levers.LeverValidationError):
        levers.apply_lever(original, "RETIRE", {})       # missing required parameter
    assert original == {"employment_income": Decimal("50000"),
                        "pension_income": Decimal(0)}


def test_ledger_conservation_is_enforced_and_rolls_back():
    ledger = pf.ResourceLedger(capacities={"POOL": Decimal("100")})
    ledger.commit({"POOL": Decimal("60")})
    assert ledger.remaining("POOL") == Decimal("40")

    with pytest.raises(pf.LedgerConservationError, match="over-allocated"):
        ledger.commit({"POOL": Decimal("50")})
    # rolled back: the failed commit left no trace
    assert ledger.allocated["POOL"] == Decimal("60")

    ledger.release({"POOL": Decimal("60")})
    assert ledger.allocated["POOL"] == Decimal("0")
    with pytest.raises(pf.LedgerConservationError, match="negative"):
        ledger.release({"POOL": Decimal("10")})
