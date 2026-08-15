"""Entry 12B — the Before-You-Act comparison engine, proved in isolation.

WHY UNIT TESTS. The comparator is pure and takes two projected graphs, so every
change kind, every family and every authority state can be driven directly.
Waiting for a database to produce a sealed scenario in which a deadline is
legitimately REMOVED is not coverage; it is hoping.

The integration suite proves the same engine over genuinely sealed state.
"""
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.ioe.domain import canonical as c
from app.services.ioe.domain.integrity import DependencyUnavailable, IntegrityReason
from app.services.ioe.scenario.comparison import (
    COMPARISON_CONTRACT_VERSION,
    DIRECTION_BASELINE_TO_COUNTERFACTUAL,
    ChangeKind,
    ComparisonIntegrityError,
    ComparisonSide,
    canonical_comparison_payload,
    canonical_comparison_text,
    compare_scenario_graphs,
    invert,
)
from app.services.ioe.scenario.historical_source import (
    COMPARISON_REQUIRED_FAMILIES,
    SourceAuthority,
)
from app.services.state_graph.contracts import (
    EdgeType,
    EvidenceReadiness,
    GraphAnchor,
    GraphEdge,
    GraphNode,
    GraphScope,
    GraphView,
    NodeFreshness,
    NodeType,
    Provenance,
    TaxStateGraph,
    summarize,
)
from app.services.state_graph.hashing import compute_graph_hash
from app.services.state_graph.scenario_projection import (
    project_scenario_comparable_graph,
)

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
SCENARIO = uuid.UUID("55555555-0000-0000-0000-000000000009")
VERSION = "77777777-0000-0000-0000-000000000001"

#: Every comparison-required family answered. The default for a side that is
#: not the subject of an authority test.
COMPLETE = {family: SourceAuthority.AUTHORITATIVE
            for family in COMPARISON_REQUIRED_FAMILIES}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _node(node_type: NodeType, source_kind: str, source_id: str, **attributes):
    return GraphNode(
        node_type=node_type, source_kind=source_kind, source_id=source_id,
        provenance=Provenance.ENGINE_COMPUTED,
        freshness=NodeFreshness.NOT_TRACKED, attributes=attributes,
    )


def _graph(nodes, edges):
    scope = GraphScope(user_id=str(USER), tax_year=2025,
                       view=GraphView.HISTORICAL)
    anchors = (GraphAnchor(artifact="ioe.scenario", artifact_id=str(SCENARIO),
                           content_hash="h"),)
    summary = summarize(nodes, edges)
    return TaxStateGraph(
        scope=scope, anchors=anchors,
        nodes=tuple(sorted(nodes, key=lambda n: n.key)),
        edges=tuple(sorted(edges, key=lambda e: e.sort_key)),
        summary=summary,
        graph_hash=compute_graph_hash(
            scope=scope, anchors=anchors, nodes=nodes, edges=edges,
            summary=summary),
    )


def _side(nodes, edges=(), *, authority=None):
    return ComparisonSide(
        graph=project_scenario_comparable_graph(_graph(list(nodes), list(edges))),
        authority=dict(COMPLETE if authority is None else authority),
    )


def _line(kind: str, label: str, amount: str):
    return _node(NodeType.TAX_STATE, "ioe.scenario_result.line_items",
                 f"{kind}:{label}", kind=kind, label=label, amount=amount)


def _opportunity(code: str, **attributes):
    return _node(NodeType.OPPORTUNITY, "ioe.scenario_result.candidates",
                 f"{code}:{VERSION}", opportunity_code=code,
                 rule_version_id=VERSION, **attributes)


def _requirement(type_code: str, readiness: EvidenceReadiness):
    return _node(NodeType.EVIDENCE, "rules.rule_required_document",
                 f"{VERSION}:{type_code}", evidence_kind="requirement",
                 document_type_code=type_code, necessity="required",
                 readiness=readiness.value)


def _by_key(comparison):
    return {n.key: n for n in comparison.node_changes}


def _kinds(comparison):
    return {n.key: n.change for n in comparison.node_changes}


# ===========================================================================
# §2 — the authority gate
# ===========================================================================
def test_a_side_with_missing_authority_is_refused():
    """THE GATE. A comparator handed an unloaded family cannot tell it from an
    empty one, and would report every counterpart as something the scenario
    caused. Every sealed v2 scenario is exactly that case."""
    incomplete = dict(COMPLETE)
    incomplete["OPPORTUNITY"] = SourceAuthority.MISSING_AUTHORITY

    baseline = _side([_line("income", "Total income", "95000.00")],
                     authority=incomplete)
    counterfactual = _side([
        _line("income", "Total income", "95000.00"),
        _opportunity("RRSP"),
    ])

    with pytest.raises(DependencyUnavailable) as caught:
        compare_scenario_graphs(baseline, counterfactual)
    assert caught.value.reason is IntegrityReason.SEALED_EVIDENCE_INCOMPLETE


def test_the_gate_refuses_before_producing_any_change_record():
    """Not merely "it raises" — it must raise INSTEAD of classifying. The
    counterfactual here has an opportunity the baseline cannot answer for; the
    forbidden outcome is one ADDED."""
    incomplete = dict(COMPLETE)
    incomplete["OPPORTUNITY"] = SourceAuthority.MISSING_AUTHORITY

    with pytest.raises(DependencyUnavailable):
        compare_scenario_graphs(
            _side([], authority=incomplete), _side([_opportunity("RRSP")]))


def test_either_side_missing_authority_is_refused():
    incomplete = dict(COMPLETE)
    incomplete["TAX_STATE"] = SourceAuthority.MISSING_AUTHORITY
    with pytest.raises(DependencyUnavailable):
        compare_scenario_graphs(_side([]), _side([], authority=incomplete))


def test_authoritative_empty_compares_and_yields_no_deltas():
    """§24. An empty answer from a source that WAS read is a real answer."""
    empty = dict(COMPLETE)
    empty["OPPORTUNITY"] = SourceAuthority.AUTHORITATIVE_EMPTY

    comparison = compare_scenario_graphs(
        _side([], authority=empty), _side([], authority=empty))
    assert comparison.summary.node_counts_by_change[ChangeKind.ADDED.value] == 0
    assert comparison.summary.changed_families == ()


def test_authoritative_empty_against_a_real_opportunity_is_one_addition():
    """§24's middle case: the baseline was READ and genuinely held none, so an
    opportunity on the other side is a real addition."""
    empty = dict(COMPLETE)
    empty["OPPORTUNITY"] = SourceAuthority.AUTHORITATIVE_EMPTY

    comparison = compare_scenario_graphs(
        _side([], authority=empty), _side([_opportunity("RRSP")]))
    (change,) = comparison.node_changes
    assert change.change is ChangeKind.ADDED
    assert change.family == NodeType.OPPORTUNITY.value


# ===========================================================================
# §4 / §8 — node matching and TAX_STATE
# ===========================================================================
def test_the_four_change_kinds_over_tax_state():
    """§8's acceptance family, all four outcomes in one comparison."""
    baseline = _side([
        _line("income", "Total income", "95000.00"),      # unchanged
        _line("deduction", "Total deductions", "0.00"),   # changed
        _line("tax", "Old surtax", "10.00"),              # removed
    ])
    counterfactual = _side([
        _line("income", "Total income", "95000.00"),
        _line("deduction", "Total deductions", "5000.00"),
        _line("payable", "CPP payable", "300.00"),        # added
    ])

    comparison = compare_scenario_graphs(baseline, counterfactual)
    kinds = _kinds(comparison)

    assert kinds["TAX_STATE:ioe.scenario_result.line_items:income:Total income"] is (
        ChangeKind.UNCHANGED)
    assert kinds[
        "TAX_STATE:ioe.scenario_result.line_items:deduction:Total deductions"
    ] is ChangeKind.CHANGED
    assert kinds["TAX_STATE:ioe.scenario_result.line_items:tax:Old surtax"] is (
        ChangeKind.REMOVED)
    assert kinds["TAX_STATE:ioe.scenario_result.line_items:payable:CPP payable"] is (
        ChangeKind.ADDED)


def test_a_changed_amount_carries_before_after_and_a_signed_delta():
    """§7. Subtracting two authoritative sealed values is a comparison, not a
    tax calculation — and the arithmetic is Decimal, in the canonical scale."""
    comparison = compare_scenario_graphs(
        _side([_line("deduction", "Total deductions", "1000.00")]),
        _side([_line("deduction", "Total deductions", "6000.00")]),
    )
    (change,) = comparison.node_changes
    assert change.change is ChangeKind.CHANGED
    (field,) = [f for f in change.fields if f.field == "attributes.amount"]
    assert field.before == "1000.00"
    assert field.after == "6000.00"
    assert field.delta == "5000.00"
    assert Decimal(field.delta) == Decimal("5000.00")


def test_a_negative_movement_keeps_its_sign():
    comparison = compare_scenario_graphs(
        _side([_line("tax", "Federal tax", "14000.00")]),
        _side([_line("tax", "Federal tax", "12500.00")]),
    )
    (field,) = [f for f in comparison.node_changes[0].fields
                if f.field == "attributes.amount"]
    assert field.delta == "-1500.00"


def test_no_delta_is_invented_against_an_absent_value():
    """Absent is not zero. A delta against `None` would state a movement of a
    size the seal never recorded."""
    comparison = compare_scenario_graphs(
        _side([_line("deduction", "D", "100.00")]),
        _side([_node(NodeType.TAX_STATE, "ioe.scenario_result.line_items",
                     "deduction:D", kind="deduction", label="D", amount=None)]),
    )
    (field,) = [f for f in comparison.node_changes[0].fields
                if f.field == "attributes.amount"]
    assert field.before == "100.00" and field.after is None
    assert field.delta is None


def test_a_non_governed_numeric_field_gets_no_delta():
    """A numeric difference between two values whose unit nobody declared is
    arithmetic, not meaning."""
    comparison = compare_scenario_graphs(
        _side([_opportunity("RRSP", candidate_rank=1)]),
        _side([_opportunity("RRSP", candidate_rank=4)]),
    )
    (field,) = [f for f in comparison.node_changes[0].fields
                if f.field == "attributes.candidate_rank"]
    assert field.before == 1 and field.after == 4
    assert field.delta is None


def test_one_identity_may_not_name_two_families():
    """§4. A semantic key resolving to incompatible node types is a broken read
    model, not a change in the user's tax position.

    Driven against the matcher directly, with the key forced to collide. A real
    `GraphNode.key` embeds its node type, so the collision is UNREACHABLE
    through the certified construction — which is the point: the guard exists
    for the day something stops being true, and a test that could only reach it
    through a real graph could not test it at all.
    """
    from app.services.ioe.scenario.comparison import _compare_nodes

    class _Colliding(GraphNode):
        @property
        def key(self) -> str:
            return "FORCED:collision:1"

    left = _Colliding(
        node_type=NodeType.TAX_STATE, source_kind="k", source_id="x",
        provenance=Provenance.ENGINE_COMPUTED)
    right = _Colliding(
        node_type=NodeType.FACT, source_kind="k", source_id="x",
        provenance=Provenance.ENGINE_COMPUTED)
    assert left.key == right.key

    with pytest.raises(ComparisonIntegrityError, match="two families"):
        _compare_nodes([left], [right])


# ===========================================================================
# §9 — opportunities
# ===========================================================================
def test_opportunity_change_kinds_by_semantic_identity():
    baseline = _side([
        _opportunity("RRSP", eligibility_status="eligible"),
        _opportunity("OLD", eligibility_status="eligible"),
    ])
    counterfactual = _side([
        _opportunity("RRSP", eligibility_status="ineligible"),
        _opportunity("NEW", eligibility_status="eligible"),
    ])
    kinds = _kinds(compare_scenario_graphs(baseline, counterfactual))

    assert kinds[f"OPPORTUNITY:ioe.scenario_result.candidates:RRSP:{VERSION}"] is (
        ChangeKind.CHANGED)
    assert kinds[f"OPPORTUNITY:ioe.scenario_result.candidates:OLD:{VERSION}"] is (
        ChangeKind.REMOVED)
    assert kinds[f"OPPORTUNITY:ioe.scenario_result.candidates:NEW:{VERSION}"] is (
        ChangeKind.ADDED)


def test_an_unchanged_opportunity_reports_no_fields():
    comparison = compare_scenario_graphs(
        _side([_opportunity("RRSP", eligibility_status="eligible")]),
        _side([_opportunity("RRSP", eligibility_status="eligible")]),
    )
    (change,) = comparison.node_changes
    assert change.change is ChangeKind.UNCHANGED
    assert change.fields == ()


# ===========================================================================
# §10 — evidence readiness
# ===========================================================================
def test_ready_to_missing_is_a_change_with_explicit_before_and_after():
    """§10's named transition. It is what makes the evidence axis worth
    comparing, and it must survive an identity-free projection."""
    comparison = compare_scenario_graphs(
        _side([_requirement("T4", EvidenceReadiness.READY)]),
        _side([_requirement("T4", EvidenceReadiness.MISSING)]),
    )
    (change,) = comparison.node_changes
    assert change.change is ChangeKind.CHANGED
    (field,) = [f for f in change.fields if f.field == "attributes.readiness"]
    assert field.before == EvidenceReadiness.READY.value
    assert field.after == EvidenceReadiness.MISSING.value


def test_held_evidence_is_not_reported_as_changed_when_only_requirements_move():
    """The sealed T1 held snapshot is the SAME object on both sides. Only the
    requirement moved, so only the requirement may be reported."""
    held = _node(NodeType.EVIDENCE, "sealed.held_evidence", "T4",
                 evidence_kind="held_evidence_type", document_type_code="T4")
    comparison = compare_scenario_graphs(
        _side([held, _requirement("T4", EvidenceReadiness.READY)]),
        _side([held, _requirement("T4", EvidenceReadiness.MISSING)]),
    )
    kinds = _kinds(comparison)
    assert kinds["EVIDENCE:sealed.held_evidence:T4"] is ChangeKind.UNCHANGED
    assert kinds[f"EVIDENCE:rules.rule_required_document:{VERSION}:T4"] is (
        ChangeKind.CHANGED)


# ===========================================================================
# §5 — RESOURCE
# ===========================================================================
def test_resource_produces_no_deltas_and_stays_inapplicable():
    resource = _node(NodeType.RESOURCE, "ioe.resource_ledger_entry", "rle-1",
                     resource_code="RRSP_ROOM", capacity="1000.00")
    comparison = compare_scenario_graphs(
        _side([resource, _line("income", "Total income", "1.00")]),
        _side([_line("income", "Total income", "1.00")]),
    )
    assert [n for n in comparison.node_changes
            if n.family == NodeType.RESOURCE.value] == []

    applicability = {e.family: e for e in comparison.node_applicability}
    assert applicability[NodeType.RESOURCE.value].status.value == (
        "NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON")

    # COMPARABLE-WITH-ZERO STAYS DISTINGUISHABLE FROM DOES-NOT-APPLY. A
    # comparison belongs to neither side, so it quotes no side's `retained`
    # count; the distinction survives in the STATUS plus the family's own
    # change counts, which are zero for a comparable family holding nothing.
    assert applicability[NodeType.ASSUMPTION.value].status is (
        applicability[NodeType.SCENARIO.value].status)
    assert applicability[NodeType.ASSUMPTION.value].status.value == "COMPARABLE"
    assert applicability[NodeType.ASSUMPTION.value].status != (
        applicability[NodeType.RESOURCE.value].status)
    assert comparison.summary.family_counts[NodeType.ASSUMPTION.value] == {
        kind.value: 0 for kind in ChangeKind}
    text = canonical_comparison_text(comparison)
    assert "capacity" not in text and "RRSP_ROOM" not in text


# ===========================================================================
# §15 / §16 — edges and applicability policy
# ===========================================================================
def test_edge_change_kinds_by_canonical_identity():
    run = _node(NodeType.TAX_STATE, "ioe.scenario_result", "s")
    item = _line("tax", "Federal tax", "1.00")
    other = _line("tax", "Provincial tax", "2.00")
    kept = GraphEdge(EdgeType.DERIVED_FROM, item.key, run.key)
    gone = GraphEdge(EdgeType.DERIVED_FROM, other.key, run.key)

    comparison = compare_scenario_graphs(
        _side([run, item, other], [kept, gone]),
        _side([run, item], [kept]),
    )
    by_key = {(e.edge_type, e.source_key, e.target_key): e.change
              for e in comparison.edge_changes}
    assert by_key[("DERIVED_FROM", item.key, run.key)] is ChangeKind.UNCHANGED
    assert by_key[("DERIVED_FROM", other.key, run.key)] is ChangeKind.REMOVED


def test_no_retained_edge_family_can_report_changed_today():
    """DOCUMENTS THE CHOSEN RULE. For every family the projection retains, the
    whole semantic content IS the identity — none carries an attribute — so a
    semantic change is a different edge and REMOVED + ADDED is correct. The
    attribute comparison stays in the engine for a family that later acquires a
    payload; this pins the current reality."""
    from app.services.state_graph.scenario_projection import (
        EDGE_COMPARISON_POLICY,
    )
    retained = [t for t, p in EDGE_COMPARISON_POLICY.items() if p.retained]
    assert set(retained) == {
        EdgeType.DERIVED_FROM, EdgeType.REQUIRES, EdgeType.EXPIRES_AT,
        EdgeType.ASSUMES, EdgeType.REFERENCES_SCENARIO}
    for edge_type in retained:
        assert GraphEdge(edge_type, "a", "b").attributes == {}


def test_inapplicable_and_unavailable_edge_policy_survives_into_the_result():
    """§16. `CURRENT_SOURCE_UNAVAILABLE` is a source-capability state and must
    not be flattened into UNCHANGED or NOT_APPLICABLE by the comparison."""
    comparison = compare_scenario_graphs(_side([]), _side([]))
    edges = {e.family: e for e in comparison.edge_applicability}

    assert edges["INELIGIBLE_BECAUSE"].status.value == "CURRENT_SOURCE_UNAVAILABLE"
    assert edges["SUPPORTED_BY"].status.value == "READINESS_SEMANTICS_ONLY"
    assert edges["CONSTRAINED_BY"].status.value == (
        "NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON")
    assert edges["REFERENCES_RULE"].status.value == "RESERVED_NO_PRODUCER"
    assert [e for e in comparison.edge_changes
            if e.edge_type == "INELIGIBLE_BECAUSE"] == []


def test_the_two_sides_must_agree_on_applicability():
    from dataclasses import replace

    left = _side([])
    tampered = replace(
        left.graph,
        node_applicability=left.graph.node_applicability[1:])
    with pytest.raises(ComparisonIntegrityError, match="applicability"):
        compare_scenario_graphs(
            left, ComparisonSide(graph=tampered, authority=dict(COMPLETE)))


# ===========================================================================
# §21 — direction
# ===========================================================================
def test_inverting_a_comparison_equals_comparing_the_other_way():
    """THE MAJOR CORRECTNESS INVARIANT. A comparator that is not invertible is
    one whose direction is an accident rather than a decision."""
    baseline = _side([
        _line("income", "Total income", "95000.00"),
        _line("deduction", "Total deductions", "0.00"),
        _line("tax", "Old surtax", "10.00"),
    ], )
    counterfactual = _side([
        _line("income", "Total income", "95000.00"),
        _line("deduction", "Total deductions", "5000.00"),
        _opportunity("RRSP"),
    ])

    forward = compare_scenario_graphs(baseline, counterfactual)
    backward = compare_scenario_graphs(counterfactual, baseline)

    assert invert(forward) == backward
    assert canonical_comparison_text(invert(forward)) == (
        canonical_comparison_text(backward))
    assert invert(forward).comparison_hash == backward.comparison_hash


def test_inversion_swaps_added_and_removed_and_negates_the_delta():
    forward = compare_scenario_graphs(
        _side([_line("deduction", "D", "1000.00"), _line("tax", "Gone", "1.00")]),
        _side([_line("deduction", "D", "6000.00"), _line("tax", "New", "2.00")]),
    )
    backward = invert(forward)

    forward_kinds = _kinds(forward)
    backward_kinds = _kinds(backward)
    gone = "TAX_STATE:ioe.scenario_result.line_items:tax:Gone"
    new = "TAX_STATE:ioe.scenario_result.line_items:tax:New"
    assert forward_kinds[gone] is ChangeKind.REMOVED
    assert backward_kinds[gone] is ChangeKind.ADDED
    assert forward_kinds[new] is ChangeKind.ADDED
    assert backward_kinds[new] is ChangeKind.REMOVED

    changed = "TAX_STATE:ioe.scenario_result.line_items:deduction:D"
    (fwd,) = [f for f in _by_key(forward)[changed].fields
              if f.field == "attributes.amount"]
    (bwd,) = [f for f in _by_key(backward)[changed].fields
              if f.field == "attributes.amount"]
    assert (fwd.before, fwd.after, fwd.delta) == ("1000.00", "6000.00", "5000.00")
    assert (bwd.before, bwd.after, bwd.delta) == ("6000.00", "1000.00", "-5000.00")


def test_inversion_recounts_the_summary_rather_than_carrying_it():
    forward = compare_scenario_graphs(
        _side([]), _side([_line("tax", "New", "1.00")]))
    backward = invert(forward)
    assert forward.summary.node_counts_by_change[ChangeKind.ADDED.value] == 1
    assert backward.summary.node_counts_by_change[ChangeKind.ADDED.value] == 0
    assert backward.summary.node_counts_by_change[ChangeKind.REMOVED.value] == 1


# ===========================================================================
# §22 / §23 — self-comparison and ordering
# ===========================================================================
def test_comparing_a_graph_with_itself_reports_no_change():
    side = _side([
        _line("income", "Total income", "95000.00"),
        _opportunity("RRSP"),
        _requirement("T4", EvidenceReadiness.READY),
    ])
    comparison = compare_scenario_graphs(side, side)

    assert all(n.change is ChangeKind.UNCHANGED for n in comparison.node_changes)
    assert all(e.change is ChangeKind.UNCHANGED for e in comparison.edge_changes)
    assert comparison.summary.changed_families == ()
    for kind in (ChangeKind.ADDED, ChangeKind.REMOVED, ChangeKind.CHANGED):
        assert comparison.summary.node_counts_by_change[kind.value] == 0


def test_insertion_order_cannot_become_a_user_visible_change():
    """§23. Presentation and storage ordering must never surface as a change in
    someone's tax position."""
    nodes = [
        _line("income", "Total income", "95000.00"),
        _line("tax", "Federal tax", "14000.00"),
        _opportunity("RRSP"),
    ]
    forward = compare_scenario_graphs(_side(nodes), _side(nodes))
    reversed_ = compare_scenario_graphs(
        _side(list(reversed(nodes))), _side(list(reversed(nodes))))

    assert all(n.change is ChangeKind.UNCHANGED for n in forward.node_changes)
    assert canonical_comparison_text(forward) == (
        canonical_comparison_text(reversed_))
    assert forward.comparison_hash == reversed_.comparison_hash


def test_the_comparison_is_invariant_under_pythonhashseed(tmp_path: Path):
    script = tmp_path / "compare.py"
    script.write_text(
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "from test_scenario_comparison import _side, _line, _opportunity\n"
        "from app.services.ioe.scenario.comparison import (\n"
        "    canonical_comparison_text, compare_scenario_graphs)\n"
        "a = _side([_line('income', 'Total income', '1.00')])\n"
        "b = _side([_line('income', 'Total income', '2.00'), _opportunity('X')])\n"
        "print(canonical_comparison_text(compare_scenario_graphs(a, b)))\n"
    )
    rendered = set()
    for seed in ("0", "1", "42"):
        out = subprocess.run(                                       # noqa: S603
            [sys.executable, str(script)],
            capture_output=True, text=True, check=True,
            env={"PYTHONHASHSEED": seed, "PYTHONPATH": ".",
                 "PATH": "/usr/bin:/bin", "ONYX_JWT_SECRET": "x" * 40},
        )
        rendered.add(out.stdout.strip())
    assert len(rendered) == 1, f"comparison moved with PYTHONHASHSEED: {rendered}"
    (only,) = rendered
    assert '"comparison_contract_version":"1.0.0"' in only, only[:200]


# ===========================================================================
# §18 — the comparison hash
# ===========================================================================
def test_the_hash_is_domain_separated_and_binds_both_sides():
    assert c.DOMAIN_SCENARIO_COMPARISON in c.ALL_HASH_DOMAINS
    payload = {"same": "payload"}
    digests = {d: c.domain_hash(d, payload) for d in c.ALL_HASH_DOMAINS}
    assert len(set(digests.values())) == len(c.ALL_HASH_DOMAINS)

    comparison = compare_scenario_graphs(
        _side([_line("tax", "T", "1.00")]), _side([_line("tax", "T", "2.00")]))
    body = canonical_comparison_payload(comparison)
    assert body["baseline_graph_hash"] and body["counterfactual_graph_hash"]
    assert body["baseline_graph_hash"] != body["counterfactual_graph_hash"]
    assert body["comparison_contract_version"] == COMPARISON_CONTRACT_VERSION
    assert body["direction"] == DIRECTION_BASELINE_TO_COUNTERFACTUAL


def test_a_different_comparison_produces_a_different_hash():
    one = compare_scenario_graphs(
        _side([_line("tax", "T", "1.00")]), _side([_line("tax", "T", "2.00")]))
    two = compare_scenario_graphs(
        _side([_line("tax", "T", "1.00")]), _side([_line("tax", "T", "3.00")]))
    assert one.comparison_hash != two.comparison_hash


def test_the_canonical_payload_states_what_enters_the_hash():
    comparison = compare_scenario_graphs(_side([]), _side([]))
    assert set(canonical_comparison_payload(comparison)) == {
        "comparison_contract_version", "direction", "baseline_graph_hash",
        "counterfactual_graph_hash", "node_applicability", "edge_applicability",
        "identity_exclusions", "node_changes", "edge_changes", "summary",
    }


# ===========================================================================
# §3 — the engine executes no authority
# ===========================================================================
def test_the_comparison_module_imports_no_engine_evaluator_or_session():
    """Checked over parsed IMPORTS rather than source text: the docstring names
    those services to explain what it does NOT do, and a test that could not
    tell a mention from a dependency would forbid documenting the boundary."""
    import ast
    import inspect

    import app.services.ioe.scenario.comparison as module

    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = ("tax_engine", "rules_service", "sqlalchemy", "session",
                 "portfolio", "optimizer", "replay")
    for name in imported:
        assert not any(bad in name for bad in forbidden), (
            f"the comparison engine imports {name!r}")


def test_the_summary_invents_no_strategy_vocabulary():
    """It describes differences. It does not decide what a person should do."""
    comparison = compare_scenario_graphs(
        _side([_line("tax", "T", "10.00")]), _side([_line("tax", "T", "1.00")]))
    text = canonical_comparison_text(comparison).lower()
    for word in ("saving", "recommend", "best", "optimal", "should"):
        assert word not in text, word
