"""Opportunity contract v2 — defaults, absence semantics, and v1 compatibility.

The contract's core guarantee: legally meaningful fields are SUPPLIED by the
rules layer, and their absence is explicit (`indeterminate`, not evaluable)
rather than filled in by a consumer.
"""
from decimal import Decimal

from app.services.tax_engine.contracts import (
    CONTRACT_VERSION,
    ActionSpec,
    DocumentSpec,
    Opportunity,
    OpportunityContractV2,
    PortfolioLeverRef,
)


def _minimal(**kw) -> OpportunityContractV2:
    base = dict(rule_version_id="v1", opportunity_code="rrsp", title="RRSP", category="deduction")
    base.update(kw)
    return OpportunityContractV2(**base)


def test_v1_alias_is_the_same_class():
    assert Opportunity is OpportunityContractV2


def test_estimated_impact_aliases_calculated_impact():
    o = _minimal(calculated_impact=Decimal("108.75"))
    assert o.estimated_impact == Decimal("108.75")   # v1 consumers keep working
    assert o.calculated_impact == Decimal("108.75")  # v2 canonical name


def test_defaults_are_absent_not_invented():
    o = _minimal()
    assert o.contract_version == CONTRACT_VERSION
    # legal fields default to EMPTY, never to a guessed value
    assert o.eligibility_status == "indeterminate"
    assert o.eligibility_basis_codes == ()
    assert o.required_actions == ()
    assert o.required_documents == ()
    assert o.dependencies == ()
    assert o.applicable_deadlines == ()
    assert o.citations == ()
    assert o.assumptions_required == ()
    assert o.calculation_basis is None
    assert o.calculated_impact is None
    assert o.portfolio_lever_ref is None


def test_has_contract_metadata_tracks_basis_codes():
    assert not _minimal().has_contract_metadata
    assert _minimal(eligibility_basis_codes=("AGE_71_UNDER",)).has_contract_metadata


def test_portfolio_evaluability_requires_lever_and_definite_status():
    lever = PortfolioLeverRef(lever_code="INCREASE_RRSP_DEDUCTION", parameter_bindings={"amount": "rule.max_amount"})

    # no lever → not evaluable, regardless of eligibility
    assert not _minimal(eligibility_status="eligible").is_portfolio_evaluable
    # lever but indeterminate eligibility → not evaluable
    assert not _minimal(portfolio_lever_ref=lever).is_portfolio_evaluable
    # both → evaluable
    assert _minimal(eligibility_status="eligible", portfolio_lever_ref=lever).is_portfolio_evaluable
    assert _minimal(
        eligibility_status="conditionally_eligible", portfolio_lever_ref=lever
    ).is_portfolio_evaluable
    # explicitly ineligible → never evaluable
    assert not _minimal(eligibility_status="ineligible", portfolio_lever_ref=lever).is_portfolio_evaluable


def test_lever_ref_carries_code_not_engine_fields():
    """Rule data references a lever by code; it must not name TaxInput fields."""
    lever = PortfolioLeverRef(lever_code="INCREASE_RRSP_DEDUCTION",
                              parameter_bindings={"amount": "rule.max_amount"})
    assert lever.lever_code == "INCREASE_RRSP_DEDUCTION"
    # parameter bindings map parameter names to SOURCE keys, not to TaxInput fields
    assert set(lever.parameter_bindings) == {"amount"}
    assert not hasattr(lever, "writable_fields")   # the registry owns field effects


def test_action_and_document_specs_are_structured():
    a = ActionSpec(action_code="CONTRIBUTE_RRSP", description="Contribute before the deadline",
                   effort_rating=2, cost_type="required_cash_contribution",
                   cost_amount=Decimal("5000"))
    assert a.cost_type == "required_cash_contribution"   # a retained asset, not an expenditure
    d = DocumentSpec(document_type_code="RRSP", necessity="required")
    assert d.necessity == "required"
