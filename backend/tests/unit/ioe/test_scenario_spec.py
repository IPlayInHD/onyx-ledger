"""The scenario request boundary: typed levers in, everything else refused.

These are the tests for the decision the whole P5 security posture rests on. A
scenario names lever CODES with declared parameters. If a request can be made to
carry a field path, a patch, a formula, or anything else that a later stage would
have to execute, then the registry is no longer the only thing that decides what
a scenario does — so each of those shapes gets its own refusal test.
"""
from decimal import Decimal

import pytest

from app.services.ioe.domain.freshness import (
    ComparableScenario,
    PinnedState,
    ScenariosNotComparable,
    assert_comparable,
    compare,
    evaluate_freshness,
)
from app.services.ioe.domain.scenario import (
    FreshnessStatus,
    ScenarioSpec,
    ScenarioSpecError,
    StaleReason,
)

RRSP = "INCREASE_RRSP_DEDUCTION"
FHSA = "INCREASE_FHSA_DEDUCTION"


def _lever(code=RRSP, amount="5000"):
    return {"lever_code": code, "parameters": {"amount": Decimal(amount)}}


# ---------------------------------------------------------------------------
# What is accepted
# ---------------------------------------------------------------------------
def test_a_typed_lever_request_is_accepted_and_ordered():
    spec = ScenarioSpec.parse([_lever(RRSP, "5000"), _lever(FHSA, "3000")])
    assert spec.lever_codes == (RRSP, FHSA)
    assert [x.apply_order for x in spec.levers] == [0, 1]
    assert spec.levers[0].parameters["amount"] == Decimal("5000")


def test_apply_order_is_positional_so_reordering_is_a_different_scenario():
    forward = ScenarioSpec.parse([_lever(RRSP), _lever(FHSA)])
    reverse = ScenarioSpec.parse([_lever(FHSA), _lever(RRSP)])
    assert forward.canonical_for_hash() != reverse.canonical_for_hash()


def test_assumption_order_does_not_change_identity():
    """Assumptions are a set, not a sequence: two requests differing only in
    their order are the same scenario and must hash identically."""
    a = {"assumption_code": "EXPECTED_RETURN_RATE", "value_number": Decimal("0.05")}
    b = {"assumption_code": "CONTRIBUTION_ROOM_AVAILABLE",
         "value_number": Decimal("10000")}
    first = ScenarioSpec.parse([_lever()], assumptions=[a, b])
    second = ScenarioSpec.parse([_lever()], assumptions=[b, a])
    assert first.canonical_for_hash() == second.canonical_for_hash()


# ---------------------------------------------------------------------------
# What is refused — one test per shape, so a regression names itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("forbidden_key", [
    "field", "field_path", "path", "target_field",
    "patch", "op", "operations",
    "formula", "expression", "expr", "eval", "script", "lambda",
    "mutation", "apply",
])
def test_executable_mutation_shapes_are_refused(forbidden_key):
    request = {"lever_code": RRSP, forbidden_key: "rrsp_deduction"}
    with pytest.raises(ScenarioSpecError, match="not accepted"):
        ScenarioSpec.parse([request])


@pytest.mark.parametrize("forbidden_key", ["formula", "eval", "patch", "field"])
def test_executable_shapes_are_refused_inside_parameters_too(forbidden_key):
    request = {"lever_code": RRSP, "parameters": {forbidden_key: "1+1"}}
    with pytest.raises(ScenarioSpecError, match="not accepted"):
        ScenarioSpec.parse([request])


def test_a_callable_parameter_is_refused():
    request = {"lever_code": RRSP, "parameters": {"amount": lambda: 1}}
    with pytest.raises(ScenarioSpecError):
        ScenarioSpec.parse([request])


def test_a_float_amount_is_refused_because_money_must_be_decimal():
    request = {"lever_code": RRSP, "parameters": {"amount": 5000.5}}
    with pytest.raises(ScenarioSpecError, match="Decimal"):
        ScenarioSpec.parse([request])


def test_an_unknown_lever_code_is_refused_not_guessed():
    with pytest.raises(ScenarioSpecError, match="unknown lever_code"):
        ScenarioSpec.parse([{"lever_code": "DRAIN_THE_ACCOUNT"}])


def test_a_request_with_no_lever_code_is_refused():
    with pytest.raises(ScenarioSpecError, match="names\nlevers by CODE|lever_code"):
        ScenarioSpec.parse([{"parameters": {"amount": Decimal("1")}}])


def test_an_undeclared_parameter_is_refused():
    request = {"lever_code": RRSP, "parameters": {"amount": Decimal("1"),
                                                  "province": "ON"}}
    with pytest.raises(ScenarioSpecError, match="undeclared parameter"):
        ScenarioSpec.parse([request])


def test_an_out_of_bounds_parameter_is_refused():
    with pytest.raises((ScenarioSpecError, ValueError)):
        ScenarioSpec.parse([_lever(RRSP, "-1")])


def test_an_empty_scenario_is_refused():
    with pytest.raises(ScenarioSpecError, match="at least one lever"):
        ScenarioSpec.parse([])


def test_a_lever_outside_its_jurisdiction_or_year_is_refused():
    """Applicability is checked at the boundary, not discovered at apply time."""
    from app.services.ioe.domain import levers

    spec = levers.get(RRSP)
    restricted = type(spec)(
        **{**spec.__dict__, "jurisdictions": ("QC",), "tax_years": (2099,)}
    )
    original = levers._LEVERS[RRSP]
    levers._LEVERS[RRSP] = restricted
    try:
        with pytest.raises(ScenarioSpecError, match="does not apply"):
            ScenarioSpec.parse([_lever()], jurisdiction="ON", tax_year=2025)
    finally:
        levers._LEVERS[RRSP] = original


def test_an_unregistered_assumption_code_is_refused():
    with pytest.raises(ScenarioSpecError, match="unregistered assumption_code"):
        ScenarioSpec.parse(
            [_lever()],
            assumptions=[{"assumption_code": "MADE_UP", "value_number": Decimal("1")}],
        )


def test_an_assumption_needs_exactly_one_typed_value():
    with pytest.raises(ScenarioSpecError, match="exactly one typed value"):
        ScenarioSpec.parse(
            [_lever()],
            assumptions=[{"assumption_code": "EXPECTED_RETURN_RATE"}],
        )
    with pytest.raises(ScenarioSpecError, match="exactly one typed value"):
        ScenarioSpec.parse(
            [_lever()],
            assumptions=[{
                "assumption_code": "EXPECTED_RETURN_RATE",
                "value_number": Decimal("1"), "value_text": "one",
            }],
        )


# ---------------------------------------------------------------------------
# Label and note are outside identity
# ---------------------------------------------------------------------------
def test_label_and_note_are_excluded_from_the_canonical_spec():
    plain = ScenarioSpec.parse([_lever()])
    labelled = ScenarioSpec.parse(
        [_lever()], label="My retirement plan", note="ask the accountant"
    )
    assert plain.canonical_for_hash() == labelled.canonical_for_hash()
    # and they are still carried, just not as identity
    assert labelled.label == "My retirement plan"
    serialized = str(labelled.canonical_for_hash())
    assert "retirement" not in serialized and "accountant" not in serialized


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------
def _pinned(**overrides) -> PinnedState:
    base = {
        "baseline_input_snapshot_hash": "snap-1",
        "baseline_result_hash": "res-1",
        "rule_snapshot_hash": "rules-1",
        "engine_version": "py-1.0.0",
        "reference_data_version": "2025.1.0",
        "lever_registry_version": "1.1.0",
        "objective_code": "obj", "objective_version": "1.0.0",
        "assumption_set_hash": "a-1", "tax_year": 2025,
    }
    return PinnedState(**{**base, **overrides})


def test_an_unchanged_world_is_current():
    verdict = evaluate_freshness(_pinned(), _pinned())
    assert verdict.is_current
    assert verdict.stale_reason is None


@pytest.mark.parametrize("changed_field,expected", [
    ("baseline_input_snapshot_hash", StaleReason.BASELINE_INPUTS_CHANGED),
    ("baseline_result_hash", StaleReason.BASELINE_RESULT_CHANGED),
    ("rule_snapshot_hash", StaleReason.RULE_SNAPSHOT_SUPERSEDED),
    ("engine_version", StaleReason.ENGINE_VERSION_CHANGED),
    ("reference_data_version", StaleReason.REFERENCE_DATA_CHANGED),
    ("lever_registry_version", StaleReason.LEVER_REGISTRY_CHANGED),
    ("objective_version", StaleReason.OBJECTIVE_POLICY_CHANGED),
    ("assumption_set_hash", StaleReason.ASSUMPTION_SET_CHANGED),
])
def test_each_moved_input_yields_its_own_structured_reason(changed_field, expected):
    verdict = evaluate_freshness(_pinned(), _pinned(**{changed_field: "moved"}))
    assert verdict.status is FreshnessStatus.STALE
    assert verdict.stale_reason is expected
    assert changed_field in verdict.changed_fields


def test_a_rolled_over_tax_year_is_its_own_reason():
    verdict = evaluate_freshness(_pinned(), _pinned(tax_year=2026))
    assert verdict.stale_reason is StaleReason.TAX_YEAR_ROLLED_OVER


def test_the_most_fundamental_change_is_reported_first():
    """A changed baseline explains a changed rule snapshot, not the reverse."""
    verdict = evaluate_freshness(
        _pinned(), _pinned(baseline_input_snapshot_hash="x", rule_snapshot_hash="y")
    )
    assert verdict.stale_reason is StaleReason.BASELINE_INPUTS_CHANGED
    assert set(verdict.changed_fields) == {
        "baseline_input_snapshot_hash", "rule_snapshot_hash"
    }


def test_an_unpinned_input_is_unknown_rather_than_fresh():
    """Absence of a pin is reported as such, never as freshness."""
    verdict = evaluate_freshness(_pinned(baseline_result_hash=None), _pinned())
    assert verdict.status is FreshnessStatus.UNKNOWN
    assert "baseline_result_hash" in verdict.changed_fields


def test_supersession_wins_over_every_other_state():
    verdict = evaluate_freshness(_pinned(), _pinned(), superseded_by=object())
    assert verdict.status is FreshnessStatus.SUPERSEDED
    assert verdict.stale_reason is StaleReason.SUPERSEDED_BY_REFRESH


# ---------------------------------------------------------------------------
# Comparison compatibility
# ---------------------------------------------------------------------------
def _comparable(**overrides) -> ComparableScenario:
    base = {
        "scenario_id": "s1", "base_analysis_id": "a1", "tax_year": 2025,
        "jurisdiction": "ON", "objective_code": "obj", "objective_version": "1.0.0",
        "result_schema_version": "1.0.0",
        "baseline_input_snapshot_hash": "snap-1",
        "objective_value_baseline": Decimal("20000.00"),
        "objective_value_scenario": Decimal("18000.00"),
        "objective_delta": Decimal("2000.00"),
        "scenario_tax": Decimal("18000.00"),
        "freshness_status": "current",
    }
    return ComparableScenario(**{**base, **overrides})


def test_two_compatible_scenarios_compare():
    left = _comparable(scenario_id="s1", objective_delta=Decimal("2000.00"))
    right = _comparable(scenario_id="s2", objective_delta=Decimal("1500.00"))
    result = compare(left, right)
    assert result.difference == Decimal("500.00")
    assert result.better == "left"
    assert result.both_current is True


def test_equivalent_scenarios_are_reported_as_such_not_as_a_winner():
    left = _comparable(scenario_id="s1")
    right = _comparable(scenario_id="s2")
    assert compare(left, right).better == "equivalent"


@pytest.mark.parametrize("field,value,expected", [
    ("base_analysis_id", "a2", "different baselines"),
    ("baseline_input_snapshot_hash", "snap-2", "different baseline input snapshots"),
    ("tax_year", 2024, "different tax years"),
    ("jurisdiction", "BC", "different jurisdictions"),
    ("objective_version", "2.0.0", "different objective policies"),
    ("objective_code", "other", "different objective policies"),
    ("result_schema_version", "2.0.0", "different result schema versions"),
])
def test_incompatible_scenarios_are_refused_with_a_reason(field, value, expected):
    left = _comparable(scenario_id="s1")
    right = _comparable(scenario_id="s2", **{field: value})
    with pytest.raises(ScenariosNotComparable) as exc:
        compare(left, right)
    assert expected in str(exc.value)


def test_a_scenario_cannot_be_compared_with_itself():
    same = _comparable(scenario_id="s1")
    with pytest.raises(ScenariosNotComparable, match="with itself"):
        assert_comparable(same, same)


def test_an_incomplete_scenario_cannot_be_compared():
    left = _comparable(scenario_id="s1")
    right = _comparable(scenario_id="s2", objective_delta=None)
    with pytest.raises(ScenariosNotComparable, match="no completed objective delta"):
        compare(left, right)


def test_comparing_stale_scenarios_is_allowed_but_labelled():
    """A stale result is still a true statement about its own baseline. It is
    surfaced with a notice rather than withheld or silently treated as current."""
    left = _comparable(scenario_id="s1")
    right = _comparable(
        scenario_id="s2", freshness_status="stale",
        stale_reason_code="RULE_SNAPSHOT_SUPERSEDED",
    )
    result = compare(left, right)
    assert result.both_current is False
    assert any("RULE_SNAPSHOT_SUPERSEDED" in n for n in result.stale_notices)
