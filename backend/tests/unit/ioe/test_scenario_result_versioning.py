"""Version-aware scenario-result canonicalization (Entry 12B1).

The defect: `canonical_result` read the current-write schema version from a
module constant while canonicalizing HISTORICAL artifacts. Bumping that constant
would have re-canonicalized every sealed scenario under a contract it was never
hashed with, turning a version bump into a silent invalidation of all replay.

The fix is that the version is now an argument nobody can omit. The tests below
prove that, that v1 stays frozen, that v2 is defined but demands its evidence,
and that an unrecognised version fails closed rather than guessing.
"""
import inspect
import types
from decimal import Decimal

import pytest

from app.services.ioe.domain import canonical as c
from app.services.ioe.domain import confidence as support
from app.services.ioe.domain.enums import CalculationBasis, EvidenceStatus
from app.services.ioe.domain.scenario import (
    CURRENT_SCENARIO_RESULT_SCHEMA_VERSION,
    SCENARIO_RESULT_SCHEMA_V1,
    SCENARIO_RESULT_SCHEMA_V2,
    SUPPORTED_SCENARIO_RESULT_SCHEMA_VERSIONS,
    UnsupportedResultSchemaVersion,
    canonical_scenario_result,
)
from app.services.ioe.scenario.service import ScenarioService

DERIVED_HASH = "a" * 64


def _computed():
    breakdown = support.compute(
        evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
        calculation_basis=CalculationBasis.SCENARIO_ESTIMATE)
    return {
        "scenario_tax": Decimal("18500.00"), "tax_delta": Decimal("1500.00"),
        "objective_baseline": Decimal("20000.00"),
        "objective_scenario": Decimal("18500.00"),
        "objective_delta": Decimal("1500.00"),
        "support": breakdown,
        "changes": [types.SimpleNamespace(
            apply_order=0, lever_code="rrsp_contribution",
            field="rrsp_deduction", new_value=Decimal("5000.00"))],
    }


# ---------------------------------------------------------------------------
# The fix itself
# ---------------------------------------------------------------------------
def test_the_schema_version_has_no_default_and_cannot_be_omitted():
    """The heart of it. A default would let a caller inherit "whatever this
    build writes" for an artifact sealed years earlier — which is the defect."""
    parameter = inspect.signature(canonical_scenario_result).parameters[
        "result_schema_version"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    with pytest.raises(TypeError):
        canonical_scenario_result(_computed())  # type: ignore[call-arg]

    service_param = inspect.signature(
        ScenarioService.canonical_result).parameters["result_schema_version"]
    assert service_param.default is inspect.Parameter.empty


def test_write_version_and_supported_versions_are_separate_concepts():
    """Replay must not infer what it can interpret from what it writes."""
    assert CURRENT_SCENARIO_RESULT_SCHEMA_VERSION == SCENARIO_RESULT_SCHEMA_V1
    assert SUPPORTED_SCENARIO_RESULT_SCHEMA_VERSIONS == {
        SCENARIO_RESULT_SCHEMA_V1, SCENARIO_RESULT_SCHEMA_V2}
    assert CURRENT_SCENARIO_RESULT_SCHEMA_VERSION in (
        SUPPORTED_SCENARIO_RESULT_SCHEMA_VERSIONS)


def test_a_historical_v1_artifact_is_unaffected_by_what_new_writes_use():
    """THE CENTRAL REGRESSION.

    Canonicalizing a v1 artifact is driven entirely by the version passed in.
    Asking for v1 and asking for v2 over the SAME computed result produce
    different payloads, which is only possible because the version is an
    argument — under the old code both calls would have returned whatever the
    module constant said, and a constant bump would have moved the historical
    one.
    """
    computed = _computed()
    historical = canonical_scenario_result(
        computed, result_schema_version=SCENARIO_RESULT_SCHEMA_V1)
    future = canonical_scenario_result(
        computed, result_schema_version=SCENARIO_RESULT_SCHEMA_V2,
        counterfactual_derived_state_hash=DERIVED_HASH)

    assert historical["result_schema_version"] == SCENARIO_RESULT_SCHEMA_V1
    assert future["result_schema_version"] == SCENARIO_RESULT_SCHEMA_V2
    assert c.scenario_result_hash(spec_hash="0" * 64, result=historical) != (
        c.scenario_result_hash(spec_hash="0" * 64, result=future))


def test_replay_reads_the_stored_version_rather_than_the_constant():
    """Structural, over the parsed call rather than prose: the replay call site
    must pass the version it read from the row."""
    import app.services.ioe.replay.services as replay

    source = inspect.getsource(replay)
    assert "result_schema_version=sealed_schema_version" in source
    assert "result_schema_version=CURRENT_SCENARIO_RESULT_SCHEMA_VERSION" not in source
    assert "result_schema_version=SCENARIO_RESULT_SCHEMA_VERSION" not in source


# ---------------------------------------------------------------------------
# v1 is frozen; v2 is defined but not activated
# ---------------------------------------------------------------------------
def test_v1_refuses_to_absorb_the_v2_field():
    """§11. Silently ignoring it would let a caller believe v2 evidence was
    bound when the bytes say v1."""
    with pytest.raises(UnsupportedResultSchemaVersion, match="must not absorb"):
        canonical_scenario_result(
            _computed(), result_schema_version=SCENARIO_RESULT_SCHEMA_V1,
            counterfactual_derived_state_hash=DERIVED_HASH)


def test_v2_requires_the_derived_state_hash():
    """§10. v2 exists to bind that evidence; sealing without it would claim a
    binding that is not there."""
    for missing in (None, ""):
        with pytest.raises(UnsupportedResultSchemaVersion, match="requires"):
            canonical_scenario_result(
                _computed(), result_schema_version=SCENARIO_RESULT_SCHEMA_V2,
                counterfactual_derived_state_hash=missing)


def test_v2_binds_the_hash_and_not_a_second_copy_of_the_payload():
    """The derived state already has its own domain-separated hash; copying the
    whole artifact into the outer hash would buy nothing but a bigger input."""
    payload = canonical_scenario_result(
        _computed(), result_schema_version=SCENARIO_RESULT_SCHEMA_V2,
        counterfactual_derived_state_hash=DERIVED_HASH)
    assert payload["counterfactual_derived_state_hash"] == DERIVED_HASH
    assert "counterfactual_derived_state" not in payload


def test_v2_is_v1_plus_exactly_two_differences():
    v1 = canonical_scenario_result(
        _computed(), result_schema_version=SCENARIO_RESULT_SCHEMA_V1)
    v2 = canonical_scenario_result(
        _computed(), result_schema_version=SCENARIO_RESULT_SCHEMA_V2,
        counterfactual_derived_state_hash=DERIVED_HASH)
    assert set(v2) - set(v1) == {"counterfactual_derived_state_hash"}
    assert set(v1) - set(v2) == set()
    differing = {k for k in v1 if v1[k] != v2[k]}
    assert differing == {"result_schema_version"}


def test_v2_is_deterministic():
    """§17. Same semantic input, same bytes — asserted here; the cross-seed run
    is exercised by the determinism gate."""
    first = canonical_scenario_result(
        _computed(), result_schema_version=SCENARIO_RESULT_SCHEMA_V2,
        counterfactual_derived_state_hash=DERIVED_HASH)
    second = canonical_scenario_result(
        _computed(), result_schema_version=SCENARIO_RESULT_SCHEMA_V2,
        counterfactual_derived_state_hash=DERIVED_HASH)
    assert c.canonical_text(first) == c.canonical_text(second)
    assert c.scenario_result_hash(spec_hash="0" * 64, result=first) == (
        c.scenario_result_hash(spec_hash="0" * 64, result=second))


# ---------------------------------------------------------------------------
# Unknown versions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("version", ["", "0.9.0", "3.0.0", "latest", "1.0", "v1"])
def test_an_unsupported_version_fails_closed(version):
    """§12. No fallback to v1, v2 or current — a guess would hash under a
    contract the artifact was never sealed with and report the mismatch as
    though the data had changed."""
    with pytest.raises(UnsupportedResultSchemaVersion):
        canonical_scenario_result(
            _computed(), result_schema_version=version)


def test_production_still_writes_v1():
    """§21. v2 is now reachable end to end, and still not activated.

    Rewritten for Phase A2. It used to assert that `_persist` named the write
    authority literally, which stopped being the right question once one chosen
    version began travelling from the seam to every field. The property that
    actually matters is unchanged and is asserted directly: the PUBLIC entry
    point chooses the write authority, and the write authority is still v1.
    """
    assert CURRENT_SCENARIO_RESULT_SCHEMA_VERSION == "1.0.0"
    source = inspect.getsource(ScenarioService.simulate)
    assert "result_schema_version=CURRENT_SCENARIO_RESULT_SCHEMA_VERSION" in source
    assert "SCENARIO_RESULT_SCHEMA_V2" not in source


def test_the_public_entry_point_has_no_schema_version_parameter():
    """§2. The seam is internal. A public parameter would let a request decide
    what contract its own evidence is sealed under."""
    public = inspect.signature(ScenarioService.simulate).parameters
    assert "result_schema_version" not in public

    internal = inspect.signature(ScenarioService._simulate).parameters
    assert internal["result_schema_version"].kind is (
        inspect.Parameter.KEYWORD_ONLY)
    assert internal["result_schema_version"].default is inspect.Parameter.empty


def test_the_seam_refuses_an_unsupported_version_before_anything_is_created():
    """No fallback and no widening: an unsupported version must not reach the
    point where a header row exists."""
    import asyncio

    for version in ("0.9.0", "3.0.0", "latest", ""):
        with pytest.raises(UnsupportedResultSchemaVersion):
            asyncio.run(ScenarioService(user_id=None)._simulate(
                None, None, result_schema_version=version))  # type: ignore[arg-type]
