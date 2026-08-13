"""The frozen v1 scenario-result protocol.

WHY THESE VECTORS ARE CHECKED IN RATHER THAN COMPUTED. `canonical_result` is
about to become version-aware, and the whole point of that refactor is that v1
bytes must not move. A test that generated both the "before" and the "expected"
side from the refactored code would prove only that the new code agrees with
itself. These were captured from the untouched tree at 71b393c — certified by
CI run 31655509801 — before a single character of the canonicalization path was
edited, and they are the authority the refactor is measured against.

If a change to v1 canonicalization is ever genuinely intended, these files must
be regenerated deliberately and the reason recorded. Silently refreshing them to
make a failing test pass would destroy the only evidence that sealed historical
scenarios still verify.
"""
import hashlib
import json
import pathlib
import types
from decimal import Decimal

import pytest

from app.services.ioe.domain import assumptions as areg
from app.services.ioe.domain import canonical as c
from app.services.ioe.domain import confidence as support
from app.services.ioe.domain.enums import CalculationBasis, EvidenceStatus
from app.services.ioe.scenario.service import ScenarioService

VECTORS = json.loads(
    (pathlib.Path(__file__).parents[2] / "golden"
     / "scenario_result_v1_vectors.json").read_text()
)
SPEC_HASH = VECTORS["_provenance"]["spec_hash_used"]
NAMES = sorted(k for k in VECTORS if not k.startswith("_"))


def _ch(order, lever, field, new):
    return types.SimpleNamespace(
        apply_order=order, lever_code=lever, field=field, new_value=new
    )


def _computed(*, tax, delta, ob, os_, od, breakdown, changes):
    return {
        "scenario_tax": tax, "tax_delta": delta,
        "objective_baseline": ob, "objective_scenario": os_,
        "objective_delta": od, "support": breakdown, "changes": changes,
    }


def _shapes() -> dict[str, dict]:
    """Rebuilt exactly as the capture script built them, so the inputs are the
    same and only the canonicalization path is under test."""
    M = Decimal
    plain = support.compute(
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.SCENARIO_ESTIMATE)
    a_money = areg.build("CONTRIBUTION_ROOM_AVAILABLE", M("5000.00"),
                         source="user", certainty="user_asserted")
    a_bool = areg.build("EMPLOYMENT_INCOME_CONSTANT", True,
                        source="user", certainty="user_asserted")
    withassum = support.compute(
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.SCENARIO_ESTIMATE,
        assumptions=(a_money, a_bool))
    low = support.compute(
        evidence_status=EvidenceStatus.INCOMPLETE,
        calculation_basis=CalculationBasis.SCENARIO_ESTIMATE,
        assumptions=(a_money,))
    proj = support.compute(
        evidence_status=EvidenceStatus.USER_ATTESTED,
        calculation_basis=CalculationBasis.PROJECTION_ESTIMATE,
        assumptions=(a_money, a_bool), horizon_years=3, indexation_known=False)

    return {
        "01_minimal_no_changes": _computed(
            tax=M("20000.00"), delta=M("0.00"), ob=M("20000.00"),
            os_=M("20000.00"), od=M("0.00"), breakdown=plain, changes=[]),
        "02_multiple_levers": _computed(
            tax=M("18500.00"), delta=M("1500.00"), ob=M("20000.00"),
            os_=M("18500.00"), od=M("1500.00"), breakdown=plain,
            changes=[_ch(0, "rrsp_contribution", "rrsp_deduction", M("5000.00")),
                     _ch(1, "fhsa_contribution", "fhsa_deduction", M("8000.00"))]),
        "03_typed_assumptions": _computed(
            tax=M("19000.00"), delta=M("1000.00"), ob=M("20000.00"),
            os_=M("19000.00"), od=M("1000.00"), breakdown=withassum,
            changes=[_ch(0, "rrsp_contribution", "rrsp_deduction", M("3000.00"))]),
        "04_incomplete_evidence": _computed(
            tax=M("19750.25"), delta=M("249.75"), ob=M("20000.00"),
            os_=M("19750.25"), od=M("249.75"), breakdown=low,
            changes=[_ch(0, "rrsp_contribution", "rrsp_deduction", M("900.00"))]),
        "05_projection_horizon": _computed(
            tax=M("17250.10"), delta=M("2749.90"), ob=M("20000.00"),
            os_=M("17250.10"), od=M("2749.90"), breakdown=proj,
            changes=[_ch(0, "rrsp_contribution", "rrsp_deduction", M("6000.00")),
                     _ch(1, "donation", "donations", M("1200.50"))]),
    }


def _canonical(name: str) -> dict:
    """Build the payload for one vector through the production path.

    Kept as one call so that when `canonical_result` gains a required
    `result_schema_version`, exactly one line here changes — and it must be
    passed the v1 version explicitly, which is the behaviour under test.
    """
    return ScenarioService.canonical_result(
        _shapes()[name], result_schema_version="1.0.0")


# ---------------------------------------------------------------------------
# The corpus itself must be worth trusting
# ---------------------------------------------------------------------------
def test_the_corpus_was_captured_before_the_refactor():
    provenance = VECTORS["_provenance"]
    assert provenance["captured_from_sha"] == "71b393c"
    assert provenance["captured_before_any_edit_to_canonical_result"] is True


def test_there_are_five_vectors_and_they_are_not_five_copies_of_one_shape():
    """§4. Five differently-named but semantically identical scenarios would
    certify nothing, so the distinguishing features are asserted directly."""
    assert len(NAMES) == 5
    assert len({VECTORS[n]["scenario_result_hash"] for n in NAMES}) == 5
    assert len({VECTORS[n]["canonical_text_bytes"] for n in NAMES}) >= 4

    features = {n: VECTORS[n]["feature_counts"] for n in NAMES}
    assert {f["changes"] for f in features.values()} >= {0, 1, 2}
    assert {f["cap_applied"] for f in features.values()} == {True, False}
    assert {f["has_assumption_adjustment"] for f in features.values()} == {True, False}


@pytest.mark.parametrize("name", NAMES)
def test_each_vector_is_a_complete_v1_artifact(name):
    """§10 — no vector may certify compatibility while being empty or partial."""
    vector = VECTORS[name]
    assert vector["result_schema_version"] == "1.0.0"
    assert vector["scenario_result_hash"]
    assert len(vector["scenario_result_hash"]) == 64
    assert vector["canonical_payload"]
    assert vector["canonical_text_bytes"] > 0
    assert vector["canonical_payload"]["support"]["raw_support_score"] is not None


# ---------------------------------------------------------------------------
# The protocol proof — byte identity, not "replay said VERIFIED"
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", NAMES)
def test_the_v1_canonical_payload_is_byte_identical_to_the_frozen_vector(name):
    payload = _canonical(name)
    assert payload == VECTORS[name]["canonical_payload"]

    text = c.canonical_text(payload)
    assert len(text.encode()) == VECTORS[name]["canonical_text_bytes"]
    assert hashlib.sha256(text.encode()).hexdigest() == (
        VECTORS[name]["canonical_text_sha256"]
    )


@pytest.mark.parametrize("name", NAMES)
def test_the_v1_scenario_result_hash_is_unchanged(name):
    actual = c.scenario_result_hash(
        spec_hash=SPEC_HASH, result=_canonical(name))
    assert actual == VECTORS[name]["scenario_result_hash"], (
        f"{name}: the v1 result hash moved. Every sealed v1 scenario in the "
        "database was hashed under the frozen contract, so this is a "
        "compatibility break, not a test to update."
    )


def test_the_v1_payload_declares_its_own_schema_version():
    """The version must travel INSIDE the hashed payload, or two schema
    versions could produce identical bytes for different contracts."""
    for name in NAMES:
        assert _canonical(name)["result_schema_version"] == "1.0.0"


def test_v1_carries_no_counterfactual_derived_state_field():
    """§14. v1 historical hash semantics must never absorb the v2 field, or
    sealed scenarios would start hashing differently the moment v2 exists."""
    for name in NAMES:
        assert "counterfactual_derived_state_hash" not in _canonical(name)
        assert "counterfactual_derived_state" not in _canonical(name)
