"""Entry 12B1 — the sealed counterfactual derived state.

The load-bearing claims: support scores are computed the way the baseline
computes them (or the five fields cannot be compared later), the sealed shape is
invariant under every ordering the database and Python can impose on it, and
absence stays distinguishable from emptiness.
"""
import inspect
import uuid
from decimal import Decimal

import pytest

from app.services.ioe.domain import canonical as c
from app.services.ioe.domain import confidence as support
from app.services.ioe.normalization.service import OpportunityNormalizationService
from app.services.ioe.scenario.counterfactual import (
    COUNTERFACTUAL_DERIVED_STATE_SCHEMA_VERSION,
    build_candidates,
    build_derived_state,
    build_line_items,
    canonical_payload,
    derived_state_hash,
)
from app.services.tax_engine.contracts import (
    DeadlineSpec,
    DependencySpec,
    DocumentSpec,
    OpportunityContractV2,
)

V1 = uuid.UUID("11111111-1111-1111-1111-111111111111")
V2 = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _opportunity(code: str, version: uuid.UUID, **overrides) -> OpportunityContractV2:
    fields = dict(
        rule_version_id=version,
        opportunity_code=code,
        title=f"{code} title",
        category="deduction",
        tax_year=2025,
        eligibility_status="eligible",
        eligibility_basis_codes=("BASIS_B", "BASIS_A"),
        calculation_basis="rule_formula_determined",
        calculated_impact=Decimal("1234.56"),
        economic_effect_type="current_year_tax_reduction",
        reversibility="reversible",
        required_documents=(
            DocumentSpec(document_type_code="T4", necessity="required"),
            DocumentSpec(document_type_code="RRSP_SLIP", necessity="recommended"),
        ),
        applicable_deadlines=(
            DeadlineSpec(deadline_code="RRSP_CONTRIBUTION"),
            DeadlineSpec(deadline_code="FILING"),
        ),
        dependencies=(
            DependencySpec(depends_on_rule_code="OTHER", dependency_type="requires"),
        ),
        shared_resource_codes=("TFSA_ROOM", "RRSP_ROOM"),
    )
    fields.update(overrides)
    return OpportunityContractV2(**fields)


# ---------------------------------------------------------------------------
# §7 — support.compute() must be candidate-local, or parity is unreachable
# ---------------------------------------------------------------------------
def test_support_computation_takes_no_portfolio_context():
    """The §7 hard stop, asserted against the real signature.

    If support ever needed portfolio ordering, selected membership, ledger
    allocation or another candidate's state, a single scenario could not produce
    a comparable score and this entry would have had to stop. It does not — the
    entire input surface is candidate-local.
    """
    parameters = set(inspect.signature(support.compute).parameters)
    assert parameters == {
        "evidence_status", "calculation_basis", "assumptions",
        "horizon_years", "indexation_known", "rule_stability_value", "weights",
    }
    forbidden = {
        "portfolio", "portfolio_id", "members", "membership", "apply_order",
        "ledger", "resource_ledger", "allocations", "selected", "rank",
        "search_budget", "other_candidates", "session",
    }
    assert parameters & forbidden == set()


def test_support_computation_is_pure_and_reaches_no_session():
    source = inspect.getsource(support.compute)
    for leak in ("session", "select(", "await ", "self.s"):
        assert leak not in source, f"support.compute reaches {leak!r}"


# ---------------------------------------------------------------------------
# §9 — support-score parity with the baseline path
# ---------------------------------------------------------------------------
def test_counterfactual_support_matches_the_baseline_call_exactly():
    """The same governed candidate inputs must produce the same five outputs.

    This reproduces the orchestrator's per-candidate call verbatim. If the two
    paths ever diverge, a later comparison would report SUPPORT_CHANGED on
    candidates nothing had changed about.
    """
    opportunity = _opportunity("opp_a", V1)

    # The baseline path, as OptimizationOrchestrator performs it.
    baseline_candidate = OpportunityNormalizationService().normalize(opportunity)
    baseline = support.compute(
        evidence_status=baseline_candidate.evidence_status,
        calculation_basis=(
            baseline_candidate.calculation_basis
            or support.CalculationBasis.RULE_FORMULA_DETERMINED
        ),
    )

    sealed = build_candidates([opportunity])[0]
    assert sealed.raw_support_score == c.rate(baseline.raw_support_score)
    assert sealed.assumption_adjusted_score == c.rate(baseline.assumption_adjusted_score)
    assert sealed.display_support_score == c.rate(baseline.display_support_score)
    assert sealed.support_cap_applied is baseline.cap_applied
    assert sealed.support_cap_reason_code == baseline.cap_reason_code
    assert sealed.raw_support_score is not None, "a null score would compare vacuously"


def test_scenario_assumptions_do_not_enter_per_candidate_support():
    """Deliberate, and the reason is recorded in the module docstring: passing
    the scenario's assumptions here would score every counterfactual candidate
    below its baseline twin purely because the scenario declared assumptions."""
    source = inspect.getsource(build_candidates)
    assert "assumptions=" not in source


# ---------------------------------------------------------------------------
# Stable identity and ordering
# ---------------------------------------------------------------------------
def test_candidate_identity_is_semantic_not_positional():
    sealed = build_candidates([_opportunity("opp_a", V1)])[0]
    assert sealed.candidate_key == f"opp_a:{V1}"


def test_the_hash_is_invariant_under_evaluator_output_order():
    """SQL row order and evaluator emission order are not facts about the
    user's counterfactual state."""
    a, b = _opportunity("opp_a", V1), _opportunity("opp_b", V2)
    forward = build_derived_state(
        line_items=[], opportunities=[a, b], pinned_rule_version_ids=[V1, V2])
    reverse = build_derived_state(
        line_items=[], opportunities=[b, a], pinned_rule_version_ids=[V2, V1])
    assert derived_state_hash(forward) == derived_state_hash(reverse)


def test_line_items_are_ordered_by_meaning_not_by_engine_emission():
    raw = [
        {"kind": "credit", "label": "B", "amount": Decimal("2.00")},
        {"kind": "credit", "label": "A", "amount": Decimal("1.00")},
    ]
    assert [i.label for i in build_line_items(raw)] == ["A", "B"]
    assert [i.label for i in build_line_items(list(reversed(raw)))] == ["A", "B"]


def test_a_changed_candidate_changes_the_hash():
    """The other half of determinism: sensitive to what it commits to."""
    base = build_derived_state(
        line_items=[], opportunities=[_opportunity("opp_a", V1)],
        pinned_rule_version_ids=[V1])
    changed = build_derived_state(
        line_items=[],
        opportunities=[_opportunity(
            "opp_a", V1, calculated_impact=Decimal("9999.99"))],
        pinned_rule_version_ids=[V1])
    assert derived_state_hash(base) != derived_state_hash(changed)


def test_the_hash_is_domain_separated_from_every_other_sealed_hash():
    assert c.DOMAIN_COUNTERFACTUAL_DERIVED_STATE in c.ALL_HASH_DOMAINS
    payload = {"same": "payload"}
    digests = {d: c.domain_hash(d, payload) for d in c.ALL_HASH_DOMAINS}
    assert len(set(digests.values())) == len(c.ALL_HASH_DOMAINS)


# ---------------------------------------------------------------------------
# Absence, emptiness, and what must not be collapsed
# ---------------------------------------------------------------------------
def test_absent_basis_codes_stay_distinguishable_from_empty_ones():
    """§36. `None` means the rule said nothing; `()` means it said no codes.
    Collapsing them turns silence into a positive statement about eligibility.
    """
    said_nothing = build_candidates(
        [_opportunity("opp_a", V1, eligibility_basis_codes=())])[0]
    said_something = build_candidates([_opportunity("opp_b", V1)])[0]
    assert said_nothing.eligibility_basis_codes is None
    assert said_something.eligibility_basis_codes == ("BASIS_A", "BASIS_B")


def test_an_empty_candidate_set_is_sealed_rather_than_absent():
    """§46. A scenario that legitimately changes no eligibility must seal an
    EMPTY set, which is a positive statement. Missing evidence is a different
    condition and must not read the same."""
    state = build_derived_state(
        line_items=[], opportunities=[], pinned_rule_version_ids=[V1])
    assert state.candidates == ()
    assert derived_state_hash(state)
    assert canonical_payload(state)["candidates"] == []


def test_shared_resources_are_sealed_as_requirements_not_allocations():
    """§2 of the decisions. A single scenario has no portfolio and therefore no
    ledger: the sealed shape carries codes only, with no capacity, allocated or
    remaining field to mistake for one."""
    sealed = build_candidates([_opportunity("opp_a", V1)])[0]
    assert sealed.shared_resource_codes == ("RRSP_ROOM", "TFSA_ROOM")
    fields = canonical_payload(
        build_derived_state(line_items=[], opportunities=[_opportunity("opp_a", V1)],
                            pinned_rule_version_ids=[V1]))["candidates"][0]
    for ledger_field in ("capacity", "allocated", "remaining", "pool_scope"):
        assert ledger_field not in fields


def test_requirements_and_deadlines_travel_with_the_candidate():
    """Evidence requirements and deadlines are reached THROUGH opportunities, so
    a new counterfactual opportunity brings both with it. Sealing them here is
    what makes that reachability survive into a later comparison."""
    sealed = build_candidates([_opportunity("opp_a", V1)])[0]
    assert sealed.required_documents == (
        ("RRSP_SLIP", "recommended"), ("T4", "required"))
    assert sealed.applicable_deadlines == ("FILING", "RRSP_CONTRIBUTION")


def test_money_is_canonical_and_never_a_bare_decimal():
    """§50. A bare Decimal is ambiguous in a canonical payload and a float is
    refused outright; both would be hash defects."""
    state = build_derived_state(
        line_items=[{"kind": "credit", "label": "A", "amount": Decimal("1.005")}],
        opportunities=[_opportunity("opp_a", V1)],
        pinned_rule_version_ids=[V1])
    assert state.line_items[0].amount == "1.01"
    assert state.candidates[0].calculated_impact == "1234.56"
    c.canonical_text(canonical_payload(state))  # would raise on a stray Decimal


def test_a_float_amount_is_refused_rather_than_silently_scaled():
    with pytest.raises(TypeError, match="Decimal-compatible"):
        build_line_items([{"kind": "credit", "label": "A", "amount": 1.005}])


def test_the_schema_version_enters_the_hash():
    state = build_derived_state(
        line_items=[], opportunities=[], pinned_rule_version_ids=[])
    assert canonical_payload(state)["schema_version"] == (
        COUNTERFACTUAL_DERIVED_STATE_SCHEMA_VERSION
    )


def test_the_pinned_rule_set_is_sealed_with_the_state():
    """Without it, a reader cannot tell which rule universe produced the
    candidate set, and §47's "no current-rule contamination" is unprovable."""
    state = build_derived_state(
        line_items=[], opportunities=[], pinned_rule_version_ids=[V2, V1])
    assert state.pinned_rule_version_ids == tuple(sorted([str(V1), str(V2)]))


def test_nothing_here_imports_a_tax_or_rules_engine():
    """The module shapes authoritative output; it must never produce any.

    Checked over the parsed IMPORT statements rather than the source text: the
    docstring names both services to explain where the values come from, and a
    test that could not tell a mention from a dependency would forbid
    documenting the architecture.
    """
    import ast

    import app.services.ioe.scenario.counterfactual as module

    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    forbidden = {
        "app.services.tax_engine.rules_service",
        "app.services.tax_engine.service",
        "app.services.tax_engine.core.engine",
        "app.services.optimization.service",
    }
    assert imported & forbidden == set(), f"reaches an engine: {imported & forbidden}"
    assert not any("RulesEvaluatorService" in name for name in imported)
