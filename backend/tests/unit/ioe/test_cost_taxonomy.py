"""Deterministic resolution of authored cost values into the P4 taxonomy.

The point of these tests is that the resolution is DETERMINISTIC and BOUNDED:
authored P4 values are untouched, one legacy value has a single exact meaning,
one is ambiguous and resolved only by the pinned registry, and anything else
falls to a conservative default that claims nothing new about the money.
"""
import uuid
from decimal import Decimal

import pytest

from app.services.ioe.domain import levers
from app.services.ioe.domain.cost_taxonomy import (
    COST_TAXONOMY_VERSION,
    SOURCE_AUTHORED,
    SOURCE_LEGACY_DEFAULT,
    SOURCE_LEGACY_SYNONYM,
    SOURCE_LEVER_REGISTRY,
    resolve_cost_type,
)
from app.services.ioe.domain.enums import CostType
from app.services.ioe.domain.models import CostComponent
from app.services.ioe.normalization.service import OpportunityNormalizationService
from app.services.tax_engine.contracts import (
    ActionSpec,
    OpportunityContractV2,
    PortfolioLeverRef,
)


@pytest.mark.parametrize("authored", [
    CostType.LIQUIDITY_COMMITMENT,
    CostType.ASSET_TRANSFER,
    CostType.NONRECOVERABLE_EXPENDITURE,
    CostType.IMPLEMENTATION_COST,
])
def test_authored_p4_values_are_copied_verbatim(authored):
    """Rule data wins. The registry is never consulted for an authored value."""
    resolved = resolve_cost_type(authored, lever_code="INCREASE_DONATIONS")
    assert resolved.cost_type is authored
    assert resolved.source == SOURCE_AUTHORED
    assert resolved.is_derived is False


def test_required_expenditure_is_an_exact_synonym_not_a_judgement():
    resolved = resolve_cost_type(CostType.REQUIRED_EXPENDITURE)
    assert resolved.cost_type is CostType.NONRECOVERABLE_EXPENDITURE
    assert resolved.source == SOURCE_LEGACY_SYNONYM
    assert resolved.authored_cost_type is CostType.REQUIRED_EXPENDITURE


def test_the_ambiguous_legacy_value_is_resolved_by_the_pinned_registry():
    """An RRSP contribution is retained value; a donation is money gone. The
    legacy vocabulary called both `required_cash_contribution`."""
    rrsp = resolve_cost_type(
        CostType.REQUIRED_CASH_CONTRIBUTION, lever_code="INCREASE_RRSP_DEDUCTION"
    )
    donation = resolve_cost_type(
        CostType.REQUIRED_CASH_CONTRIBUTION, lever_code="INCREASE_DONATIONS"
    )
    assert rrsp.cost_type is CostType.ASSET_TRANSFER
    assert donation.cost_type is CostType.NONRECOVERABLE_EXPENDITURE
    assert rrsp.source == donation.source == SOURCE_LEVER_REGISTRY
    # the same authored value, two different economic facts — the whole point
    assert rrsp.authored_cost_type is donation.authored_cost_type
    assert rrsp.cost_type is not donation.cost_type


def test_not_every_contribution_becomes_the_same_type():
    """Guards against the regression this item exists to prevent."""
    resolved = {
        code: resolve_cost_type(
            CostType.REQUIRED_CASH_CONTRIBUTION, lever_code=code
        ).cost_type
        for code in ("INCREASE_RRSP_DEDUCTION", "INCREASE_FHSA_DEDUCTION",
                     "INCREASE_DONATIONS", "INCREASE_MEDICAL_EXPENSES",
                     "INCREASE_TUITION", "INCREASE_CHILDCARE")
    }
    assert len(set(resolved.values())) > 1, resolved
    assert resolved["INCREASE_RRSP_DEDUCTION"] is CostType.ASSET_TRANSFER
    assert resolved["INCREASE_MEDICAL_EXPENSES"] is CostType.NONRECOVERABLE_EXPENDITURE


def test_no_registry_declaration_falls_back_conservatively():
    """Unchanged pre-P4 behaviour: a feasibility constraint, not a loss.

    Resolving to `nonrecoverable_expenditure` instead would invent an expense
    the rule never stated and understate the user's position.
    """
    for lever_code in (None, "REALIZE_CAPITAL_GAINS", "NOT_A_REAL_LEVER"):
        resolved = resolve_cost_type(
            CostType.REQUIRED_CASH_CONTRIBUTION, lever_code=lever_code
        )
        assert resolved.cost_type is CostType.LIQUIDITY_COMMITMENT
        assert resolved.source == SOURCE_LEGACY_DEFAULT
        assert CostComponent(resolved.cost_type, Decimal(1)).is_true_cost is False


def test_resolution_is_deterministic_and_carries_its_version():
    first = resolve_cost_type(
        CostType.REQUIRED_CASH_CONTRIBUTION, lever_code="INCREASE_RRSP_DEDUCTION"
    )
    second = resolve_cost_type(
        CostType.REQUIRED_CASH_CONTRIBUTION, lever_code="INCREASE_RRSP_DEDUCTION"
    )
    assert first == second
    assert first.taxonomy_version == COST_TAXONOMY_VERSION


def test_an_unregistered_lever_code_does_not_raise():
    """This runs while classifying a cost; a missing declaration must not fail
    the run."""
    assert levers.commitment_class("NOT_A_REAL_LEVER") is None
    assert levers.commitment_class(None) is None


def test_normalization_attaches_the_provenance_to_the_candidate():
    """The authored value and the basis travel with the cost, so a stored row
    shows both what the rule said and why it was classified as it was."""
    opportunity = OpportunityContractV2(
        rule_version_id=uuid.uuid4(),
        opportunity_code="donate", title="Donate", category="credit",
        eligibility_status="eligible",
        required_actions=(ActionSpec(
            action_code="GIVE", description="Give", effort_rating=1,
            cost_type="required_cash_contribution", cost_amount=Decimal("1000"),
        ),),
        portfolio_lever_ref=PortfolioLeverRef(
            lever_code="INCREASE_DONATIONS",
            parameter_bindings={"amount": "action.cost_amount"},
        ),
    )
    candidate = OpportunityNormalizationService().normalize(opportunity)
    (cost,) = candidate.costs
    assert cost.cost_type is CostType.NONRECOVERABLE_EXPENDITURE
    assert cost.authored_cost_type is CostType.REQUIRED_CASH_CONTRIBUTION
    assert cost.cost_type_source == SOURCE_LEVER_REGISTRY
    assert cost.taxonomy_version == COST_TAXONOMY_VERSION
    # the AMOUNT is always the rule's; only the classification was resolved
    assert cost.amount == Decimal("1000")


def test_a_donation_now_reduces_the_objective_and_a_contribution_does_not():
    """The behavioural consequence of the split, stated directly."""
    donation = resolve_cost_type(
        CostType.REQUIRED_CASH_CONTRIBUTION, lever_code="INCREASE_DONATIONS"
    )
    rrsp = resolve_cost_type(
        CostType.REQUIRED_CASH_CONTRIBUTION, lever_code="INCREASE_RRSP_DEDUCTION"
    )
    assert CostComponent(donation.cost_type, Decimal(500)).is_true_cost is True
    assert CostComponent(rrsp.cost_type, Decimal(500)).is_true_cost is False
    # both still constrain feasibility
    assert CostComponent(rrsp.cost_type, Decimal(500)).is_liquidity_commitment is True
