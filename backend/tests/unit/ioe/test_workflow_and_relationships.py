"""Workflow state machine and typed relationship derivation."""
from decimal import Decimal

import pytest

from app.services.ioe.domain import relationships as rel
from app.services.ioe.domain.enums import (
    CalculationBasis,
    EconomicEffectType,
    EligibilityStatus,
    FreshnessStatus,
    RelationshipType,
    VisibilityStatus,
    WorkflowStatus,
)
from app.services.ioe.domain.models import (
    EconomicEffect,
    LeverApplication,
    OptimizationCandidate,
)
from app.services.ioe.domain.workflow import IllegalTransition
from app.services.ioe.domain.workflow import WorkflowStateMachine as SM

W = WorkflowStatus
F = FreshnessStatus
V = VisibilityStatus


LEGAL = [
    (W.PENDING, W.RUNNING), (W.PENDING, W.FAILED), (W.PENDING, W.CANCELLED),
    (W.RUNNING, W.COMPLETED), (W.RUNNING, W.FAILED), (W.RUNNING, W.CANCELLED),
]
ILLEGAL = [
    (W.PENDING, W.COMPLETED),          # cannot skip running
    (W.COMPLETED, W.RUNNING),          # terminal
    (W.COMPLETED, W.FAILED),
    (W.FAILED, W.RUNNING),
    (W.CANCELLED, W.RUNNING),
    (W.RUNNING, W.PENDING),            # no going back
]


@pytest.mark.parametrize("frm,to", LEGAL)
def test_legal_workflow_transitions(frm, to):
    assert SM.can_transition(frm, to)
    SM.assert_transition(frm, to)


@pytest.mark.parametrize("frm,to", ILLEGAL)
def test_illegal_workflow_transitions_raise(frm, to):
    assert not SM.can_transition(frm, to)
    with pytest.raises(IllegalTransition):
        SM.assert_transition(frm, to)


def test_terminal_states():
    assert SM.is_terminal(W.COMPLETED)
    assert SM.is_terminal(W.FAILED)
    assert SM.is_terminal(W.CANCELLED)
    assert not SM.is_terminal(W.PENDING)
    assert not SM.is_terminal(W.RUNNING)


def test_freshness_is_an_independent_axis():
    """A completed run may go stale without any evidence changing."""
    assert SM.results_are_sealed(W.COMPLETED)
    SM.assert_freshness_transition(F.CURRENT, F.STALE)
    SM.assert_freshness_transition(F.STALE, F.SUPERSEDED)
    with pytest.raises(IllegalTransition):
        SM.assert_freshness_transition(F.SUPERSEDED, F.CURRENT)
    with pytest.raises(IllegalTransition):
        SM.assert_freshness_transition(F.STALE, F.CURRENT)


def test_scenario_archive_is_reversible_and_not_a_deletion():
    SM.assert_visibility_transition(V.ACTIVE, V.ARCHIVED)
    SM.assert_visibility_transition(V.ARCHIVED, V.ACTIVE)


# ---- relationships ----------------------------------------------------------
def _candidate(key, *, lever=None, resources=(), excludes=(), requires=()):
    return OptimizationCandidate(
        candidate_key=key, opportunity_code=key.lower(), rule_version_id=f"rv-{key}",
        eligibility_status=EligibilityStatus.ELIGIBLE,
        economic_effects=(EconomicEffect(
            effect_type=EconomicEffectType.CURRENT_YEAR_TAX_REDUCTION,
            amount=Decimal("100"), calculation_basis=CalculationBasis.ENGINE_DETERMINED),),
        shared_resource_codes=resources,
        excludes_codes=excludes, requires_codes=requires,
        lever_application=LeverApplication(lever, {"amount": Decimal("100")}) if lever else None,
    )


def test_shared_pool_produces_a_shares_limit_edge_with_its_capacity():
    a = _candidate("A", resources=("RRSP_ROOM",))
    b = _candidate("B", resources=("RRSP_ROOM",))
    edges = rel.derive([a, b], pool_capacities={"RRSP_ROOM": Decimal("5000")})
    shares = [e for e in edges if e.relationship_type is RelationshipType.SHARES_LIMIT]
    assert len(shares) == 1
    assert shares[0].shared_resource_code == "RRSP_ROOM"
    assert shares[0].maximum_shared_amount == Decimal("5000")
    assert shares[0].explanation_code == "SHARED_POOL"


def test_rules_supplied_exclusion_produces_an_excludes_edge():
    a = _candidate("A", excludes=("b",))
    b = _candidate("B")
    edges = rel.derive([a, b])
    assert any(e.relationship_type is RelationshipType.EXCLUDES for e in edges)


def test_same_engine_field_produces_an_overlaps_edge():
    """Two levers writing the same input interact even without a declared pool."""
    a = _candidate("A", lever="REALIZE_CAPITAL_GAINS")
    b = _candidate("B", lever="ADJUST_ELIGIBLE_DIVIDENDS")
    c = _candidate("C", lever="REALIZE_CAPITAL_GAINS")
    edges = rel.derive([a, b, c])
    overlaps = [e for e in edges if e.relationship_type is RelationshipType.OVERLAPS]
    assert {frozenset((e.source_key, e.target_key)) for e in overlaps} == {
        frozenset(("A", "C"))
    }


def test_registry_declared_lever_conflict_produces_substitutes():
    a = _candidate("A", lever="REALIZE_CAPITAL_GAINS")
    b = _candidate("B", lever="DEFER_CAPITAL_GAINS")
    edges = rel.derive([a, b])
    assert any(e.relationship_type is RelationshipType.SUBSTITUTES for e in edges)


def test_derivation_is_order_independent():
    a = _candidate("A", resources=("MEDICAL_POOL",))
    b = _candidate("B", resources=("MEDICAL_POOL",))
    first = rel.derive([a, b])
    second = rel.derive([b, a])
    assert [e.as_canonical() for e in first] == [e.as_canonical() for e in second]


def test_measured_edge_records_what_the_engine_measured():
    source = _candidate("A")
    target = _candidate("B")
    target.standalone_potential = Decimal("250")
    target.incremental_portfolio_benefit = Decimal("100")
    edge = rel.measured_edge(source, target)
    assert edge.relationship_type is RelationshipType.REDUCES_VALUE
    assert edge.measured_delta == Decimal("150")     # measured, not estimated


def test_measured_edge_detects_synergy():
    source, target = _candidate("A"), _candidate("B")
    target.standalone_potential = Decimal("100")
    target.incremental_portfolio_benefit = Decimal("180")
    edge = rel.measured_edge(source, target)
    assert edge.relationship_type is RelationshipType.ENHANCES


def test_immaterial_interaction_produces_no_edge():
    source, target = _candidate("A"), _candidate("B")
    target.standalone_potential = Decimal("100.00")
    target.incremental_portfolio_benefit = Decimal("99.50")
    assert rel.measured_edge(source, target) is None
