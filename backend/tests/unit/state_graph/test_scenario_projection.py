"""Entry 12B1 §17 — the scenario-comparable projection, proved in isolation.

WHY THESE ARE UNIT TESTS. The projection is pure and takes an assembled graph,
so every family — including the portfolio families a real single scenario can
never produce — can be driven directly with the exact shapes the assembler
emits. Waiting for a database to yield a graph that happens to contain a
`CONSUMES_RESOURCE` edge is not coverage of the policy that excludes it.

The integration suite proves the same policy over genuinely sealed state.
"""
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.services.state_graph.contracts import (
    RESERVED_EDGE_TYPES,
    RESERVED_NODE_TYPES,
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
    EDGE_COMPARISON_POLICY,
    IDENTITY_EXCLUDED_SOURCE_KINDS,
    NODE_COMPARISON_POLICY,
    Comparability,
    ScenarioProjectionError,
    canonical_projection_payload,
    canonical_projection_text,
    project_scenario_comparable_graph,
)

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
SCENARIO = uuid.UUID("55555555-0000-0000-0000-000000000009")
VERSION = "77777777-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# Builders — every live family, so the policy is exercised rather than assumed
# ---------------------------------------------------------------------------
def _node(node_type: NodeType, source_kind: str, source_id: str, **attributes):
    return GraphNode(
        node_type=node_type,
        source_kind=source_kind,
        source_id=source_id,
        provenance=Provenance.ENGINE_COMPUTED,
        freshness=NodeFreshness.NOT_TRACKED,
        attributes=attributes,
    )


def _graph(nodes, edges, *, view: GraphView = GraphView.HISTORICAL):
    scope = GraphScope(user_id=str(USER), tax_year=2025, view=view)
    anchors = (GraphAnchor(
        artifact="ioe.scenario", artifact_id=str(SCENARIO), content_hash="h"),)
    summary = summarize(nodes, edges)
    return TaxStateGraph(
        scope=scope,
        anchors=anchors,
        nodes=tuple(sorted(nodes, key=lambda n: n.key)),
        edges=tuple(sorted(edges, key=lambda e: e.sort_key)),
        summary=summary,
        graph_hash=compute_graph_hash(
            scope=scope, anchors=anchors, nodes=nodes, edges=edges,
            summary=summary),
    )


def _full_graph() -> TaxStateGraph:
    """A graph carrying EVERY live node and edge family at once.

    Deliberately not realistic — a single sealed scenario cannot contain a
    resource ledger — because the point is to hand the projection the families
    it is supposed to exclude and watch it exclude them.
    """
    fact = _node(NodeType.FACT, "analysis.analysis_input_snapshot",
                 "employment_income", value="95000")
    run = _node(NodeType.TAX_STATE, "ioe.scenario_result", str(SCENARIO))
    item = _node(NodeType.TAX_STATE, "ioe.scenario_result.line_items",
                 "deduction:RRSP", amount="5000.00")
    opportunity = _node(NodeType.OPPORTUNITY, "ioe.scenario_result.candidates",
                        f"RRSP:{VERSION}", opportunity_code="RRSP")
    other = _node(NodeType.OPPORTUNITY, "ioe.scenario_result.candidates",
                  f"TFSA:{VERSION}", opportunity_code="TFSA")
    deadline = _node(NodeType.DEADLINE, "rules.rule_deadline",
                     f"{VERSION}:FILING", deadline_code="FILING")
    requirement = _node(
        NodeType.EVIDENCE, "rules.rule_required_document", f"{VERSION}:T4",
        evidence_kind="requirement", document_type_code="T4",
        necessity="required", readiness=EvidenceReadiness.READY.value)
    held_type = _node(NodeType.EVIDENCE, "sealed.held_evidence", "T4",
                      evidence_kind="held_evidence_type",
                      document_type_code="T4")
    held_document = _node(NodeType.EVIDENCE, "docs.document",
                          "dddddddd-0000-0000-0000-000000000001",
                          evidence_kind="held_document",
                          document_type_code="T4")
    resource = _node(NodeType.RESOURCE, "ioe.resource_ledger_entry", "rle-1",
                     resource_code="RRSP_ROOM", capacity="1000.00")
    assumption = _node(NodeType.ASSUMPTION, "ioe.scenario_assumption",
                       "ASSUME_INCOME_STABLE", assumption_code="A")
    scenario = _node(NodeType.SCENARIO, "ioe.scenario", str(SCENARIO))

    nodes = [fact, run, item, opportunity, other, deadline, requirement,
             held_type, held_document, resource, assumption, scenario]
    edges = [
        GraphEdge(EdgeType.DERIVED_FROM, item.key, run.key),
        GraphEdge(EdgeType.REQUIRES, opportunity.key, requirement.key),
        GraphEdge(EdgeType.EXPIRES_AT, opportunity.key, deadline.key),
        GraphEdge(EdgeType.ASSUMES, scenario.key, assumption.key),
        GraphEdge(EdgeType.REFERENCES_SCENARIO, scenario.key, run.key),
        GraphEdge(EdgeType.SUPPORTED_BY, requirement.key, held_document.key),
        GraphEdge(EdgeType.INELIGIBLE_BECAUSE, other.key, opportunity.key),
        GraphEdge(EdgeType.CONSTRAINED_BY, opportunity.key, resource.key),
        GraphEdge(EdgeType.CONSUMES_RESOURCE, opportunity.key, resource.key),
        GraphEdge(EdgeType.CONFLICTS_WITH, opportunity.key, other.key),
    ]
    return _graph(nodes, edges)


def _by_family(entries):
    return {e.family: e for e in entries}


# ===========================================================================
# §4 — node applicability, and the invariant the whole entry turns on
# ===========================================================================
def test_every_node_and_edge_type_is_classified():
    """§13, at the contract level. A family with no verdict is a family whose
    comparison behaviour nobody decided."""
    assert set(NODE_COMPARISON_POLICY) == set(NodeType)
    assert set(EDGE_COMPARISON_POLICY) == set(EdgeType)


def test_the_seven_comparable_families_survive_and_resource_does_not():
    projected = project_scenario_comparable_graph(_full_graph())
    nodes = _by_family(projected.node_applicability)

    for node_type in (NodeType.FACT, NodeType.TAX_STATE, NodeType.OPPORTUNITY,
                      NodeType.DEADLINE, NodeType.EVIDENCE,
                      NodeType.ASSUMPTION, NodeType.SCENARIO):
        entry = nodes[node_type.value]
        assert entry.status is Comparability.COMPARABLE, node_type
        assert entry.retained is not None and entry.retained > 0, node_type

    resource = nodes[NodeType.RESOURCE.value]
    assert resource.status is (
        Comparability.NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON)
    assert resource.retained is None, (
        "an inapplicable family must not be reported as a count")
    assert projected.graph.nodes_of(NodeType.RESOURCE) == ()


def test_resource_is_inapplicable_rather_than_removed_missing_or_empty():
    """§4's non-negotiable. The four wrong ways to say it are each excluded."""
    projected = project_scenario_comparable_graph(_full_graph())
    resource = _by_family(projected.node_applicability)[NodeType.RESOURCE.value]

    assert resource.status is not Comparability.COMPARABLE
    assert resource.retained is None            # not zero nodes
    assert resource.reason_code == "SINGLE_SCENARIO_HAS_NO_PORTFOLIO_LEDGER"

    # The family's own verdict says INAPPLICABLE and never borrows the
    # comparator's vocabulary. Asserted against this entry rather than by
    # scanning the whole document, because `MISSING` is also a legitimate
    # `EvidenceReadiness` rollup key and a blanket search would fail on it.
    assert "REMOVED" not in (resource.status.value, resource.reason_code)
    assert "MISSING" not in (resource.status.value, resource.reason_code)

    # No fabricated zero capacity or zero allocation stands in for the ledger:
    # the ledger's own vocabulary reaches no retained node at all.
    ledger_fields = {"capacity", "allocated", "remaining", "resource_code",
                     "pool_scope"}
    for node in projected.graph.nodes:
        assert not (ledger_fields & set(node.attributes)), node.key
    for edge in projected.graph.edges:
        assert not (ledger_fields & set(edge.attributes)), edge.sort_key


def test_zero_nodes_in_a_comparable_family_is_not_not_applicable():
    """THE CRITICAL INVARIANT. A scenario side that genuinely holds no
    opportunities must not be indistinguishable from the resource family, or a
    comparator will report phantom removals for one and miss real ones for the
    other."""
    empty = _graph([_node(NodeType.SCENARIO, "ioe.scenario", str(SCENARIO))], [])
    projected = project_scenario_comparable_graph(empty)
    families = _by_family(projected.node_applicability)

    opportunity = families[NodeType.OPPORTUNITY.value]
    resource = families[NodeType.RESOURCE.value]

    assert opportunity.retained == 0
    assert opportunity.status is Comparability.COMPARABLE
    assert resource.retained is None
    assert resource.status is not Comparability.COMPARABLE
    assert (opportunity.status, opportunity.retained) != (
        resource.status, resource.retained)


def test_reserved_families_are_reported_as_reserved_not_as_zero():
    """`OBLIGATION` and `DECISION` have no producer at all, which is a different
    statement from "comparable and this side has none"."""
    projected = project_scenario_comparable_graph(_full_graph())
    nodes = _by_family(projected.node_applicability)
    for reserved in RESERVED_NODE_TYPES:
        entry = nodes[reserved.value]
        assert entry.status is Comparability.RESERVED_NO_PRODUCER
        assert entry.retained is None

    edges = _by_family(projected.edge_applicability)
    for reserved_edge in RESERVED_EDGE_TYPES:
        entry = edges[reserved_edge.value]
        assert entry.status is Comparability.RESERVED_NO_PRODUCER
        assert entry.retained is None


# ===========================================================================
# §5 / §6 / §7 — edge policy
# ===========================================================================
def test_the_five_retained_edge_families_survive():
    projected = project_scenario_comparable_graph(_full_graph())
    kept = {e.edge_type for e in projected.graph.edges}
    assert kept == {
        EdgeType.DERIVED_FROM, EdgeType.REQUIRES, EdgeType.EXPIRES_AT,
        EdgeType.ASSUMES, EdgeType.REFERENCES_SCENARIO,
    }


def test_the_three_portfolio_edge_families_are_inapplicable():
    projected = project_scenario_comparable_graph(_full_graph())
    edges = _by_family(projected.edge_applicability)
    for edge_type in (EdgeType.CONSTRAINED_BY, EdgeType.CONSUMES_RESOURCE,
                      EdgeType.CONFLICTS_WITH):
        entry = edges[edge_type.value]
        assert entry.status is (
            Comparability.NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON)
        assert entry.retained is None


def test_ineligible_because_is_unavailable_not_permanently_excluded():
    """§6, non-negotiable. The edge is semantically meaningful for a single
    action; what is missing is a sealed single-scenario SOURCE. Encoding that as
    a permanent blanket exclusion would decide, silently and forever, a question
    that a future scenario-level producer is entitled to reopen."""
    projected = project_scenario_comparable_graph(_full_graph())
    entry = _by_family(projected.edge_applicability)[
        EdgeType.INELIGIBLE_BECAUSE.value]

    assert entry.status is Comparability.CURRENT_SOURCE_UNAVAILABLE
    assert entry.status is not (
        Comparability.NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON)
    assert entry.reason_code == "ONLY_PRODUCER_IS_PORTFOLIO_EXCLUSION"
    assert entry.retained is None
    # Today: no applicable source, so no historical edge.
    assert [e for e in projected.graph.edges
            if e.edge_type is EdgeType.INELIGIBLE_BECAUSE] == []


def test_supported_by_keeps_readiness_and_fabricates_no_document_identity():
    """§7, non-negotiable. Readiness survives on the requirement node and the
    sealed held TYPE survives as its own node; the document id does not survive
    at all, and none is invented to replace it."""
    projected = project_scenario_comparable_graph(_full_graph())

    entry = _by_family(projected.edge_applicability)[
        EdgeType.SUPPORTED_BY.value]
    assert entry.status is Comparability.READINESS_SEMANTICS_ONLY
    assert entry.retained is None

    kinds = {n.source_kind for n in projected.graph.nodes_of(NodeType.EVIDENCE)}
    assert "docs.document" not in kinds, "document identity entered the view"
    assert "rules.rule_required_document" in kinds, "requirements were lost"
    assert "sealed.held_evidence" in kinds, "held TYPE semantics were lost"

    readiness = [
        n.attributes["readiness"]
        for n in projected.graph.nodes_of(NodeType.EVIDENCE)
        if n.attributes.get("evidence_kind") == "requirement"
    ]
    assert readiness == [EvidenceReadiness.READY.value]

    # No document id anywhere in the canonical representation.
    assert "dddddddd-0000-0000-0000-000000000001" not in (
        canonical_projection_text(projected))
    assert _by_family(projected.identity_exclusions)["EVIDENCE:docs.document"]


def test_a_readiness_transition_stays_observable_without_identity():
    """A `READY -> MISSING` transition is what makes the whole evidence axis
    worth comparing, and it must survive an identity-free projection."""
    def side(readiness: EvidenceReadiness):
        requirement = _node(
            NodeType.EVIDENCE, "rules.rule_required_document", f"{VERSION}:T4",
            evidence_kind="requirement", document_type_code="T4",
            necessity="required", readiness=readiness.value)
        return project_scenario_comparable_graph(_graph([requirement], []))

    ready = side(EvidenceReadiness.READY)
    missing = side(EvidenceReadiness.MISSING)

    assert ready.graph.nodes[0].key == missing.graph.nodes[0].key, (
        "the requirement must keep one identity across the transition")
    assert ready.graph.nodes[0].attributes["readiness"] != (
        missing.graph.nodes[0].attributes["readiness"])
    assert canonical_projection_text(ready) != canonical_projection_text(missing)


# ===========================================================================
# §8 — symmetry
# ===========================================================================
def test_both_sides_run_the_identical_projection_policy():
    """§8. Not "the same function was called twice" — the same VERDICTS, family
    by family, over two structurally different sides. A baseline-only or
    counterfactual-only filter would show up here as a difference in
    applicability rather than in content."""
    baseline = _graph(
        [_node(NodeType.SCENARIO, "ioe.scenario", str(SCENARIO)),
         _node(NodeType.RESOURCE, "ioe.resource_ledger_entry", "rle-b")], [])
    counterfactual = _full_graph()

    left = project_scenario_comparable_graph(baseline)
    right = project_scenario_comparable_graph(counterfactual)

    def verdicts(projected):
        return {
            e.family: (e.status, e.reason_code)
            for e in (*projected.node_applicability, *projected.edge_applicability,
                      *projected.identity_exclusions)
        }

    assert verdicts(left) == verdicts(right)
    assert left.contract_version == right.contract_version
    # And the sides still differ in CONTENT, or the comparison would be vacuous.
    assert left.graph.graph_hash != right.graph.graph_hash


def test_the_projection_cannot_tell_which_side_it_is_looking_at():
    """The structural half of symmetry: the input carries no side marker, so no
    branch on one is reachable. Two graphs equal in content project equally
    regardless of which side produced them."""
    one = project_scenario_comparable_graph(_full_graph())
    two = project_scenario_comparable_graph(_full_graph())
    assert canonical_projection_text(one) == canonical_projection_text(two)
    assert one == two


# ===========================================================================
# §9 — identity
# ===========================================================================
def test_projection_never_regenerates_a_semantic_identity():
    """Projection changes applicability and visibility only. A replacement id
    would break the comparator's ability to match a node to its twin."""
    graph = _full_graph()
    projected = project_scenario_comparable_graph(graph)

    before = {n.key: n for n in graph.nodes}
    for node in projected.graph.nodes:
        assert node.key in before, f"{node.key} was not in the input graph"
        assert node is before[node.key], (
            f"{node.key} was rebuilt rather than carried through")


# ===========================================================================
# §11 — determinism
# ===========================================================================
def test_projection_is_invariant_under_insertion_order():
    graph = _full_graph()
    shuffled = _graph(list(reversed(graph.nodes)), list(reversed(graph.edges)))
    assert canonical_projection_text(
        project_scenario_comparable_graph(graph)) == (
        canonical_projection_text(project_scenario_comparable_graph(shuffled)))


def test_projection_is_invariant_under_pythonhashseed(tmp_path: Path):
    """0, 1 and 42, in real subprocesses. A dict iteration order that leaked
    into the output would be invisible in-process, because one interpreter has
    one seed."""
    script = tmp_path / "project.py"
    script.write_text(
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "from test_scenario_projection import _full_graph\n"
        "from app.services.state_graph.scenario_projection import (\n"
        "    canonical_projection_text, project_scenario_comparable_graph)\n"
        "print(canonical_projection_text("
        "project_scenario_comparable_graph(_full_graph())))\n"
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
    assert len(rendered) == 1, f"projection moved with PYTHONHASHSEED: {rendered}"
    # A subprocess that printed nothing would also produce one distinct value,
    # so the agreement is only evidence once there is something to agree on.
    (only,) = rendered
    assert '"projection_contract_version":"1.0.0"' in only, only[:200]
    assert "NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON" in only


# ===========================================================================
# §12 — idempotence
# ===========================================================================
def test_projecting_a_projection_removes_nothing_further():
    """§12. The second pass must be a no-op, not a second and quieter
    narrowing. `ScenarioComparableGraph.graph` is a real `TaxStateGraph`, so
    this is the literal `project(project(g)) == project(g)`."""
    once = project_scenario_comparable_graph(_full_graph())
    twice = project_scenario_comparable_graph(once.graph)

    assert twice == once
    assert twice.graph.nodes == once.graph.nodes
    assert twice.graph.edges == once.graph.edges
    assert twice.graph.graph_hash == once.graph.graph_hash
    assert canonical_projection_text(twice) == canonical_projection_text(once)


def test_a_third_projection_is_still_a_no_op():
    once = project_scenario_comparable_graph(_full_graph())
    assert project_scenario_comparable_graph(
        project_scenario_comparable_graph(once.graph).graph) == once


# ===========================================================================
# Closure — a retained edge may never point into an excluded family
# ===========================================================================
def test_a_retained_edge_into_an_excluded_family_fails_explicitly():
    """If the node and edge tables ever disagree, the projection says so rather
    than dropping the edge and leaving a comparator to explain a hole."""
    resource = _node(NodeType.RESOURCE, "ioe.resource_ledger_entry", "rle-1")
    opportunity = _node(NodeType.OPPORTUNITY, "ioe.scenario_result.candidates",
                        f"RRSP:{VERSION}")
    graph = _graph(
        [resource, opportunity],
        [GraphEdge(EdgeType.REQUIRES, opportunity.key, resource.key)])

    with pytest.raises(ScenarioProjectionError, match="disagree"):
        project_scenario_comparable_graph(graph)


def test_an_edge_already_dangling_on_the_way_in_is_carried_through():
    """The projection is not a validator of its input. Only endpoints IT
    removed are its responsibility; a graph that arrived malformed is reported
    by whatever assembled it."""
    opportunity = _node(NodeType.OPPORTUNITY, "ioe.scenario_result.candidates",
                        f"RRSP:{VERSION}")
    graph = _graph(
        [opportunity],
        [GraphEdge(EdgeType.REQUIRES, opportunity.key, "EVIDENCE:nowhere:x")])
    projected = project_scenario_comparable_graph(graph)
    assert len(projected.graph.edges) == 1


# ===========================================================================
# The projected graph is a real graph
# ===========================================================================
def test_the_projected_graph_is_hashed_by_the_existing_construction():
    """§11's "reuse existing canonicalization infrastructure", checked rather
    than asserted in prose: the projected hash is what `compute_graph_hash`
    returns for the retained content, not a second digest built beside it."""
    projected = project_scenario_comparable_graph(_full_graph()).graph
    assert projected.graph_hash == compute_graph_hash(
        scope=projected.scope, anchors=projected.anchors,
        nodes=projected.nodes, edges=projected.edges,
        summary=projected.summary)
    assert projected.summary.node_count == len(projected.nodes)
    assert projected.summary.edge_count == len(projected.edges)


def test_the_portfolio_total_does_not_survive_into_the_comparable_view():
    """A portfolio total is what a PORTFOLIO produced by allocating a pool
    across a chosen set of actions. Leaving it in a view that declares portfolio
    concepts inapplicable would let a comparator report that it changed between
    two scenarios which have no portfolio at all."""
    graph = _full_graph()
    with_total = _graph(list(graph.nodes), list(graph.edges))
    object.__setattr__(
        with_total.summary, "portfolio_total_benefit", "1234.00")
    assert with_total.summary.portfolio_total_benefit == "1234.00"

    projected = project_scenario_comparable_graph(with_total)
    assert projected.graph.summary.portfolio_total_benefit is None
    assert "1234.00" not in canonical_projection_text(projected)
    # And dropping it keeps the projection idempotent rather than oscillating.
    assert project_scenario_comparable_graph(projected.graph) == projected


def test_the_projection_carries_the_anchors_through_unchanged():
    """Anchors name the sealed artifacts the graph is derived from. Narrowing
    the view does not change which seal it came out of."""
    graph = _full_graph()
    assert project_scenario_comparable_graph(graph).graph.anchors == graph.anchors


def test_the_canonical_payload_states_what_enters_the_comparison():
    payload = canonical_projection_payload(
        project_scenario_comparable_graph(_full_graph()))
    assert set(payload) == {
        "projection_contract_version", "graph", "node_applicability",
        "edge_applicability", "identity_exclusions",
    }
    families = {e["family"] for e in payload["node_applicability"]}
    assert families == {t.value for t in NodeType}
    assert {e["family"] for e in payload["edge_applicability"]} == {
        t.value for t in EdgeType}
    assert {e["family"] for e in payload["identity_exclusions"]} == {
        f"{node_type.value}:{kind}"
        for node_type, kinds in IDENTITY_EXCLUDED_SOURCE_KINDS.items()
        for kind in kinds
    }
