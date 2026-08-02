"""Lever registry and structured assumptions — the boundary and safety rules."""
from decimal import Decimal

import pytest

from app.services.ioe.domain import assumptions, levers
from app.services.ioe.domain.enums import (
    AssumptionCertainty,
    AssumptionSource,
    Materiality,
)


# ---- registry authority (architecture §C / decision D-16) -------------------
def test_unknown_lever_code_is_rejected_not_guessed():
    with pytest.raises(levers.LeverNotRegistered, match="not in registry"):
        levers.get("MAKE_UP_A_LEVER")
    assert not levers.exists("MAKE_UP_A_LEVER")


def test_every_lever_declares_a_closed_writable_field_allow_list():
    for spec in levers.all_levers():
        assert spec.writable_fields, f"{spec.code} must declare writable fields"
        assert spec.direction in ("increase", "decrease", "set")


def test_lever_writes_only_its_allow_listed_field():
    inputs = {"rrsp_deduction": Decimal("1000"), "donations": Decimal("500")}
    result = levers.apply_lever(
        inputs, "INCREASE_RRSP_DEDUCTION", {"amount": Decimal("2000")}
    )
    assert result.inputs["rrsp_deduction"] == Decimal("3000")
    assert result.inputs["donations"] == Decimal("500")     # untouched
    assert [c.field for c in result.changes] == ["rrsp_deduction"]


def test_apply_never_mutates_the_caller_input():
    inputs = {"rrsp_deduction": Decimal("1000")}
    levers.apply_lever(inputs, "INCREASE_RRSP_DEDUCTION", {"amount": Decimal("500")})
    assert inputs["rrsp_deduction"] == Decimal("1000")      # baseline is preserved


def test_unknown_parameter_is_rejected():
    with pytest.raises(levers.LeverValidationError, match="unknown parameter"):
        levers.apply_lever({}, "INCREASE_RRSP_DEDUCTION",
                           {"amount": Decimal(1), "sneaky_field": "x"})


def test_missing_required_parameter_is_rejected():
    with pytest.raises(levers.LeverValidationError, match="is required"):
        levers.apply_lever({}, "INCREASE_RRSP_DEDUCTION", {})


def test_out_of_bounds_parameter_is_rejected():
    with pytest.raises(levers.LeverValidationError, match="below minimum"):
        levers.apply_lever({}, "INCREASE_RRSP_DEDUCTION", {"amount": Decimal("-5")})


def test_non_numeric_parameter_is_rejected():
    with pytest.raises(levers.LeverValidationError, match="not numeric"):
        levers.apply_lever({}, "INCREASE_RRSP_DEDUCTION", {"amount": "abc"})


def test_decrease_lever_floors_at_zero():
    result = levers.apply_lever(
        {"capital_gains": Decimal("100")}, "DEFER_CAPITAL_GAINS",
        {"amount": Decimal("500")},
    )
    assert result.inputs["capital_gains"] == Decimal("0")


def test_set_lever_replaces_the_value():
    result = levers.apply_lever(
        {"province": "ON"}, "CHANGE_PROVINCE", {"province": "BC"}
    )
    assert result.inputs["province"] == "BC"


def test_shared_resource_codes_link_levers_to_pools():
    assert levers.get("INCREASE_RRSP_DEDUCTION").shared_resource_code == "RRSP_ROOM"
    assert levers.get("INCREASE_MEDICAL_EXPENSES").shared_resource_code == "MEDICAL_POOL"


def test_conflicting_levers_are_declared():
    assert "REALIZE_CAPITAL_GAINS" in levers.get("DEFER_CAPITAL_GAINS").conflicts_with


def test_apply_all_records_order():
    result = levers.apply_all(
        {"rrsp_deduction": Decimal(0), "donations": Decimal(0)},
        [("INCREASE_RRSP_DEDUCTION", {"amount": Decimal("100")}),
         ("INCREASE_DONATIONS", {"amount": Decimal("50")})],
    )
    assert [c.apply_order for c in result.changes] == [0, 1]
    assert result.inputs["rrsp_deduction"] == Decimal("100")
    assert result.inputs["donations"] == Decimal("50")


def test_levers_are_deterministic():
    inputs = {"rrsp_deduction": Decimal("1000")}
    a = levers.apply_lever(inputs, "INCREASE_RRSP_DEDUCTION", {"amount": Decimal("500")})
    b = levers.apply_lever(inputs, "INCREASE_RRSP_DEDUCTION", {"amount": Decimal("500")})
    assert a.inputs == b.inputs


# ---- structured assumptions (architecture §15) ------------------------------
def test_unregistered_assumption_code_is_rejected():
    with pytest.raises(assumptions.AssumptionNotRegistered):
        assumptions.build("MADE_UP_ASSUMPTION", True,
                          source=AssumptionSource.USER,
                          certainty=AssumptionCertainty.USER_ASSERTED)


def test_assumption_type_is_enforced():
    with pytest.raises(assumptions.AssumptionValidationError, match="expects a boolean"):
        assumptions.build("EMPLOYMENT_INCOME_CONSTANT", "yes",
                          source=AssumptionSource.USER,
                          certainty=AssumptionCertainty.USER_ASSERTED)


def test_assumption_bounds_are_enforced():
    with pytest.raises(assumptions.AssumptionValidationError, match="above maximum"):
        assumptions.build("EXPECTED_RETURN_RATE", Decimal("2.0"),
                          source=AssumptionSource.PLATFORM,
                          certainty=AssumptionCertainty.PLATFORM_DEFAULT)


def test_registry_declares_eligibility_impact():
    built = assumptions.build(
        "PROVINCE_UNCHANGED", True,
        source=AssumptionSource.USER, certainty=AssumptionCertainty.USER_ASSERTED,
    )
    assert built.affects_eligibility is True
    assert built.materiality is Materiality.HIGH


def test_display_note_is_excluded_from_the_canonical_form():
    """Rewording a note must never change a calculation's identity."""
    a = assumptions.build(
        "EMPLOYMENT_INCOME_CONSTANT", True, source=AssumptionSource.USER,
        certainty=AssumptionCertainty.USER_ASSERTED, display_note="one wording",
    )
    b = assumptions.build(
        "EMPLOYMENT_INCOME_CONSTANT", True, source=AssumptionSource.USER,
        certainty=AssumptionCertainty.USER_ASSERTED, display_note="a totally different note",
    )
    assert a.as_canonical() == b.as_canonical()
    assert "display_note" not in a.as_canonical()
