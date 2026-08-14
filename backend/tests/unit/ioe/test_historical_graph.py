"""Entry 12B1 §17 — the historical assembler, driven directly with sealed shapes.

WHY A UNIT SUITE. The integration suite proves the assembler against genuinely
sealed state, but it can only assert on whatever a real seal happened to
contain. The shapes that matter most here are the awkward ones — two candidates
pinning one rule version, a candidate with no rule version, a side with nothing
sealed — and waiting for a database to produce them is not coverage.

The assembler is pure and takes a `HistoricalSourceBundle`, so every shape can
be built directly. That is the same reason 12A separated loading from assembly.
"""
import uuid

import pytest

from app.services.ioe.scenario.held_evidence import build_snapshot
from app.services.ioe.scenario.historical_graph import (
    HistoricalGraphError,
    assemble_historical_graph,
)
from app.services.ioe.scenario.historical_source import (
    NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON,
    SIDE_BASELINE,
    SIDE_COUNTERFACTUAL,
    HistoricalSourceBundle,
)
from app.services.state_graph.contracts import (
    EdgeType,
    EvidenceReadiness,
    GraphView,
    NodeType,
)
from app.services.state_graph.readiness import DocumentRequirement
from app.services.state_graph.scenario_projection import (
    project_scenario_comparable_graph,
)

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
SCENARIO = uuid.UUID("55555555-0000-0000-0000-000000000009")
VERSION_A = "77777777-0000-0000-0000-00000000000a"
VERSION_B = "77777777-0000-0000-0000-00000000000b"

SNAPSHOT = {
    "schema_version": "1.0.0",
    "tax_year": 2025,
    "jurisdiction": "ON",
    "inputs": {"employment_income": "95000", "rrsp_deduction": "0",
               "tax_withheld": None},
}


def _candidate(code: str, version: str | None, **overrides):
    payload = {
        "candidate_key": f"{code}:{version}",
        "opportunity_code": code,
        "rule_version_id": version,
        "eligibility_status": "eligible",
        "eligibility_basis_codes": ["BASIS_X"],
        "calculation_basis": "rule_formula_determined",
        "calculated_impact": "1500.00",
        "economic_effect_type": "deduction",
        "reversibility": "reversible",
        "required_documents": [["T4", "required"]],
        "applicable_deadlines": ["FILING"],
        "dependencies": [],
        "shared_resource_codes": ["RRSP_ROOM"],
        "raw_support_score": "0.8000",
        "assumption_adjusted_score": "0.8000",
        "display_support_score": "0.8000",
        "support_cap_applied": False,
        "support_cap_reason_code": None,
    }
    payload.update(overrides)
    return payload


def _bundle(
    side: str = SIDE_COUNTERFACTUAL,
    *,
    candidates=(),
    line_items=(),
    applied_changes=(),
    deadlines=(),
    required_evidence=(),
    held=("T4",),
    assumptions=(),
) -> HistoricalSourceBundle:
    return HistoricalSourceBundle(
        side=side,
        scenario_id=SCENARIO,
        tax_year=2025,
        facts=dict(SNAPSHOT),
        applied_changes=tuple(applied_changes),
        line_items=tuple(line_items),
        candidates=tuple(candidates),
        deadlines=tuple(deadlines),
        required_evidence=tuple(required_evidence),
        held_evidence=build_snapshot(held),
        assumptions=tuple(assumptions),
        scenario_metadata={
            "scenario_id": str(SCENARIO), "tax_year": 2025,
            "jurisdiction": "ON", "scenario_spec_hash": "spec",
            "scenario_result_hash": "result", "result_schema_version": "2.0.0",
            "objective_code": "MINIMIZE_TAX", "objective_version": 1,
        },
        resource=NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON,
    )


def _full_counterfactual() -> HistoricalSourceBundle:
    return _bundle(
        candidates=[_candidate("RRSP", VERSION_A)],
        line_items=[
            {"kind": "income", "label": "Total income", "amount": "95000.00",
             "fact_key": None, "tax_rule_version_id": None},
            {"kind": "tax", "label": "Federal tax", "amount": "14000.00",
             "fact_key": None, "tax_rule_version_id": VERSION_A},
        ],
        applied_changes=[{"lever_code": "INCREASE_RRSP_DEDUCTION",
                          "field": "rrsp_deduction", "old_value": "0",
                          "new_value": "5000", "apply_order": 0}],
        deadlines=[(VERSION_A, "FILING")],
        required_evidence=[DocumentRequirement(VERSION_A, "T4", "required")],
        assumptions=[{"assumption_code": "INCOME_STABLE",
                      "materiality": "medium", "source": "user",
                      "certainty": "user_asserted",
                      "affects_eligibility": False}],
    )


# ===========================================================================
# Shape
# ===========================================================================
def test_the_historical_view_is_historical():
    graph = assemble_historical_graph(_full_counterfactual(), user_id=USER)
    assert graph.scope.view is GraphView.HISTORICAL
    assert graph.scope.tax_year == 2025
    assert graph.scope.user_id == str(USER)


def test_every_sealed_family_becomes_its_own_nodes():
    graph = assemble_historical_graph(_full_counterfactual(), user_id=USER)
    counts = graph.summary.nodes_by_type

    assert counts["FACT"] == 3                       # one per snapshot input
    assert counts["TAX_STATE"] == 3                  # header + two line items
    assert counts["OPPORTUNITY"] == 1
    assert counts["DEADLINE"] == 1
    assert counts["EVIDENCE"] == 2                   # held type + requirement
    assert counts["ASSUMPTION"] == 1
    assert counts["SCENARIO"] == 1
    # A single scenario has no portfolio, so there is no ledger to emit.
    assert counts["RESOURCE"] == 0
    assert counts["OBLIGATION"] == 0 and counts["DECISION"] == 0


def test_the_five_retained_edge_families_are_produced_and_no_others():
    graph = assemble_historical_graph(_full_counterfactual(), user_id=USER)
    produced = {e.edge_type for e in graph.edges}
    assert produced == {EdgeType.DERIVED_FROM, EdgeType.REQUIRES,
                        EdgeType.EXPIRES_AT, EdgeType.ASSUMES,
                        EdgeType.REFERENCES_SCENARIO}


def test_no_edge_dangles():
    graph = assemble_historical_graph(_full_counterfactual(), user_id=USER)
    keys = {n.key for n in graph.nodes}
    for edge in graph.edges:
        assert edge.source_key in keys, edge
        assert edge.target_key in keys, edge


# ===========================================================================
# FACT — the frozen snapshot plus this side's sealed changes
# ===========================================================================
def test_the_counterfactual_carries_the_sealed_new_value():
    counterfactual = assemble_historical_graph(
        _full_counterfactual(), user_id=USER)
    baseline = assemble_historical_graph(_bundle(SIDE_BASELINE), user_id=USER)

    def fact(graph, field):
        (node,) = [n for n in graph.nodes_of(NodeType.FACT)
                   if n.attributes["field"] == field]
        return node

    assert fact(counterfactual, "rrsp_deduction").attributes["value"] == "5000"
    assert fact(baseline, "rrsp_deduction").attributes["value"] == "0"
    assert fact(counterfactual, "rrsp_deduction").attributes[
        "changed_by_lever_code"] == "INCREASE_RRSP_DEDUCTION"
    assert fact(baseline, "rrsp_deduction").attributes[
        "changed_by_lever_code"] is None
    # The lever moved a value, never a node identity.
    assert fact(counterfactual, "rrsp_deduction").key == (
        fact(baseline, "rrsp_deduction").key)


def test_an_untouched_input_is_identical_on_both_sides():
    counterfactual = assemble_historical_graph(
        _full_counterfactual(), user_id=USER)
    baseline = assemble_historical_graph(_bundle(SIDE_BASELINE), user_id=USER)

    def fact(graph, field):
        (node,) = [n for n in graph.nodes_of(NodeType.FACT)
                   if n.attributes["field"] == field]
        return node

    assert fact(counterfactual, "employment_income") == (
        fact(baseline, "employment_income"))


def test_two_levers_on_one_field_resolve_in_the_sealed_apply_order():
    """`apply_order` is the seal's own record of how the levers resolved. The
    last writer wins here for the same reason it won at seal time."""
    bundle = _bundle(applied_changes=[
        {"lever_code": "SECOND", "field": "rrsp_deduction",
         "old_value": "1000", "new_value": "2000", "apply_order": 1},
        {"lever_code": "FIRST", "field": "rrsp_deduction",
         "old_value": "0", "new_value": "1000", "apply_order": 0},
    ])
    graph = assemble_historical_graph(bundle, user_id=USER)
    (node,) = [n for n in graph.nodes_of(NodeType.FACT)
               if n.attributes["field"] == "rrsp_deduction"]
    assert node.attributes["value"] == "2000"
    assert node.attributes["changed_by_lever_code"] == "SECOND"


def test_a_snapshot_without_inputs_yields_no_facts_rather_than_crashing():
    bundle = _bundle()
    object.__setattr__(bundle, "facts", {"schema_version": "1.0.0"})
    graph = assemble_historical_graph(bundle, user_id=USER)
    assert graph.summary.nodes_by_type["FACT"] == 0


def test_a_change_naming_an_unknown_field_is_refused_not_dropped():
    """The seal's change trace and the seal's frozen snapshot must agree.

    Assembling only over the snapshot would silently omit a lever the user
    applied — the counterfactual would show no sign of it — which is a worse
    outcome than refusing to render a self-contradictory seal.
    """
    bundle = _bundle(applied_changes=[
        {"lever_code": "GHOST", "field": "not_a_snapshot_field",
         "old_value": "0", "new_value": "1", "apply_order": 0}])
    with pytest.raises(HistoricalGraphError, match="contradicts itself"):
        assemble_historical_graph(bundle, user_id=USER)


# ===========================================================================
# EVIDENCE — sealed types, sealed requirements, derived readiness
# ===========================================================================
def test_readiness_is_derived_from_the_two_sealed_inputs():
    bundle = _bundle(
        held=("T4",),
        required_evidence=[
            DocumentRequirement(VERSION_A, "T4", "required"),
            DocumentRequirement(VERSION_A, "RRSP", "required"),
        ])
    graph = assemble_historical_graph(bundle, user_id=USER)
    readiness = {
        n.attributes["document_type_code"]: n.attributes["readiness"]
        for n in graph.nodes_of(NodeType.EVIDENCE)
        if n.attributes["evidence_kind"] == "requirement"
    }
    assert readiness == {"T4": EvidenceReadiness.READY.value,
                         "RRSP": EvidenceReadiness.MISSING.value}


def test_held_evidence_is_a_governed_type_code_and_nothing_else():
    graph = assemble_historical_graph(_full_counterfactual(), user_id=USER)
    (held,) = [n for n in graph.nodes_of(NodeType.EVIDENCE)
               if n.attributes["evidence_kind"] == "held_evidence_type"]
    assert held.source_kind == "sealed.held_evidence"
    assert held.source_id == "T4"
    assert set(held.attributes) == {"evidence_kind", "document_type_code"}
    # Never keyed by a document, because no document id was sealed.
    assert "docs.document" not in held.key


def test_a_requirement_shared_by_two_candidates_becomes_one_node():
    """The sealed bundle re-derives requirements PER CANDIDATE, so two
    candidates pinning one rule version carry the same requirement twice.
    One requirement is one node, or two sealed values would share an identity."""
    bundle = _bundle(
        candidates=[_candidate("RRSP", VERSION_A),
                    _candidate("FHSA", VERSION_A)],
        deadlines=[(VERSION_A, "FILING")],
        required_evidence=[
            DocumentRequirement(VERSION_A, "T4", "required"),
            DocumentRequirement(VERSION_A, "T4", "required"),
        ])
    graph = assemble_historical_graph(bundle, user_id=USER)

    requirements = [n for n in graph.nodes_of(NodeType.EVIDENCE)
                    if n.attributes["evidence_kind"] == "requirement"]
    assert len(requirements) == 1
    # Both candidates still point at it — the edges are per candidate.
    assert len([e for e in graph.edges
                if e.edge_type is EdgeType.REQUIRES]) == 2


def test_a_duplicate_node_key_fails_explicitly():
    """The invariant behind every identity in this graph. Two sealed line items
    sharing `(kind, label)` would make one silently stand in for the other."""
    bundle = _bundle(line_items=[
        {"kind": "tax", "label": "Federal tax", "amount": "1.00"},
        {"kind": "tax", "label": "Federal tax", "amount": "2.00"},
    ])
    with pytest.raises(HistoricalGraphError, match="duplicate node key"):
        assemble_historical_graph(bundle, user_id=USER)


# ===========================================================================
# OPPORTUNITY
# ===========================================================================
def test_an_opportunity_keeps_the_seals_semantic_identity():
    graph = assemble_historical_graph(_full_counterfactual(), user_id=USER)
    (node,) = graph.nodes_of(NodeType.OPPORTUNITY)
    assert node.source_id == f"RRSP:{VERSION_A}"
    assert node.attributes["readiness"] == EvidenceReadiness.READY.value
    # A requirement DECLARATION travels as an attribute; no ledger is implied.
    assert node.attributes["shared_resource_codes"] == ("RRSP_ROOM",)
    assert "capacity" not in node.attributes
    assert "allocated" not in node.attributes


def test_a_candidate_with_no_rule_version_produces_no_reachability_edges():
    bundle = _bundle(
        candidates=[_candidate("MANUAL", None)],
        deadlines=[(VERSION_A, "FILING")],
        required_evidence=[DocumentRequirement(VERSION_A, "T4", "required")])
    graph = assemble_historical_graph(bundle, user_id=USER)
    assert graph.nodes_of(NodeType.OPPORTUNITY)
    assert not [e for e in graph.edges
                if e.edge_type in (EdgeType.REQUIRES, EdgeType.EXPIRES_AT)]


def test_an_empty_code_list_is_not_the_same_as_no_code_list():
    """The seal uses `None` for "the rule said nothing" and `[]` for "it said
    there are none". Collapsing them turns silence into a positive statement."""
    silent = assemble_historical_graph(
        _bundle(candidates=[
            _candidate("A", VERSION_A, eligibility_basis_codes=None)]),
        user_id=USER)
    explicit = assemble_historical_graph(
        _bundle(candidates=[
            _candidate("A", VERSION_A, eligibility_basis_codes=[])]),
        user_id=USER)

    (a,) = silent.nodes_of(NodeType.OPPORTUNITY)
    (b,) = explicit.nodes_of(NodeType.OPPORTUNITY)
    assert a.attributes["eligibility_basis_codes"] is None
    assert b.attributes["eligibility_basis_codes"] == ()
    assert silent.graph_hash != explicit.graph_hash


def test_deadlines_are_reached_only_through_the_pinned_version():
    bundle = _bundle(
        candidates=[_candidate("RRSP", VERSION_A)],
        deadlines=[(VERSION_A, "FILING"), (VERSION_B, "OTHER")])
    graph = assemble_historical_graph(bundle, user_id=USER)

    expires = [e for e in graph.edges if e.edge_type is EdgeType.EXPIRES_AT]
    assert len(expires) == 1
    assert VERSION_A in expires[0].target_key
    # The other version's deadline is still a node — it was sealed — but no
    # opportunity of this side reaches it.
    assert graph.summary.nodes_by_type["DEADLINE"] == 2


# ===========================================================================
# Determinism and the sealed-side contract
# ===========================================================================
def test_assembly_is_deterministic_over_an_unchanged_bundle():
    first = assemble_historical_graph(_full_counterfactual(), user_id=USER)
    second = assemble_historical_graph(_full_counterfactual(), user_id=USER)
    assert first.graph_hash == second.graph_hash
    assert first.nodes == second.nodes
    assert first.edges == second.edges


def test_the_anchor_is_the_sealed_scenario_result():
    graph = assemble_historical_graph(_full_counterfactual(), user_id=USER)
    (anchor,) = graph.anchors
    assert anchor.artifact == "ioe.scenario"
    assert anchor.artifact_id == str(SCENARIO)
    assert anchor.content_hash == "result"


def test_an_empty_baseline_side_still_assembles_and_projects():
    """A side with nothing sealed in two families is a graph with zero nodes in
    them — not an error, and not an inapplicable family."""
    graph = assemble_historical_graph(_bundle(SIDE_BASELINE), user_id=USER)
    assert graph.summary.nodes_by_type["OPPORTUNITY"] == 0
    assert graph.summary.nodes_by_type["TAX_STATE"] == 1   # the header

    projected = project_scenario_comparable_graph(graph)
    families = {e.family: e for e in projected.node_applicability}
    assert families["OPPORTUNITY"].retained == 0
    assert families["RESOURCE"].retained is None


def test_both_sides_share_the_tax_state_header_identity():
    """The header names the scenario, not the side. A header keyed by side would
    never match its counterpart and would read as an addition and a removal of
    the same thing."""
    counterfactual = assemble_historical_graph(
        _full_counterfactual(), user_id=USER)
    baseline = assemble_historical_graph(_bundle(SIDE_BASELINE), user_id=USER)

    def header(graph):
        (node,) = [n for n in graph.nodes_of(NodeType.TAX_STATE)
                   if n.source_kind == "ioe.scenario_result"]
        return node

    assert header(counterfactual).key == header(baseline).key
    assert header(counterfactual) == header(baseline)
