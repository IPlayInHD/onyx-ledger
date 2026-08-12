"""The taxonomy invariants — including the one this entry exists to hold.

OBLIGATION and DECISION are reserved: they stay in the taxonomy so a future
governed authority has somewhere to land, and they have no producer, so nothing
emits them. The tests below assert that at three levels — the registry, the
constructor, and (in the integration suite) real assembled output — because a
single assertion that a count is zero is satisfied just as well by a graph that
assembles nothing at all.
"""
import pytest

from app.services.ioe.domain.integrity import IntegrityStatus
from app.services.state_graph.contracts import (
    GRAPH_CONTRACT_VERSION,
    LIVE_EDGE_TYPES,
    LIVE_NODE_TYPES,
    PRODUCERS,
    RESERVED_EDGE_TYPES,
    RESERVED_NODE_TYPES,
    EdgeType,
    EvidenceReadiness,
    GraphEdge,
    GraphNode,
    NodeFreshness,
    NodeType,
    Provenance,
    freshness_from_source,
    summarize,
)


def test_exactly_eight_live_node_types_ship():
    assert len(LIVE_NODE_TYPES) == 8
    assert RESERVED_NODE_TYPES == {NodeType.OBLIGATION, NodeType.DECISION}


def test_every_live_node_type_has_an_authoritative_producer():
    """The rule the producer matrix exists to enforce. A live type without a
    producer would be a node the graph invents."""
    missing = sorted(t.value for t in LIVE_NODE_TYPES if t not in PRODUCERS)
    assert missing == [], f"live node types with no authoritative producer: {missing}"


def test_the_reserved_node_types_have_no_producer():
    for node_type in RESERVED_NODE_TYPES:
        assert node_type not in PRODUCERS, (
            f"{node_type} acquired a producer. rules.rule_outcome.outcome_type is "
            "CHECK-constrained to four values, none of them an obligation, and no "
            "Decision Journal exists — a producer here needs a governed authority "
            "behind it, not a convenient source."
        )


def test_the_reserved_node_types_are_still_in_the_taxonomy():
    """Reserved is not deleted. Removing them would mean a future governed
    authority has to re-litigate the name as well as the data."""
    assert NodeType.OBLIGATION in NodeType
    assert NodeType.DECISION in NodeType


def test_a_reserved_node_cannot_be_constructed_at_all():
    """The strongest form of the invariant: not "we do not emit these" but "one
    cannot be built". A future producer has to remove this guard deliberately."""
    for node_type in RESERVED_NODE_TYPES:
        with pytest.raises(ValueError, match="reserved"):
            GraphNode(
                node_type=node_type,
                source_kind="anything",
                source_id="anything",
                provenance=Provenance.ENGINE_COMPUTED,
            )


def test_a_reserved_edge_cannot_be_constructed_either():
    """REFERENCES_RULE needs a RULE node on the far end, and there is no such
    node type among the eight. Rule identity travels as an attribute instead."""
    assert RESERVED_EDGE_TYPES == {EdgeType.REFERENCES_RULE}
    with pytest.raises(ValueError, match="reserved"):
        GraphEdge(
            edge_type=EdgeType.REFERENCES_RULE,
            source_key="a", target_key="b",
        )
    assert EdgeType.REFERENCES_RULE not in LIVE_EDGE_TYPES


def test_the_guard_is_the_only_thing_stopping_a_reserved_node():
    """Guard-on-the-guard.

    A zero-count assertion passes trivially when nothing is assembled, so this
    proves the check has teeth: bypass the constructor exactly as a future
    producer would have to, and the summary counts it. If this ever stops
    counting, the zero assertions elsewhere have stopped meaning anything.
    """
    smuggled = GraphNode.__new__(GraphNode)
    object.__setattr__(smuggled, "node_type", NodeType.OBLIGATION)
    object.__setattr__(smuggled, "source_kind", "invented")
    object.__setattr__(smuggled, "source_id", "1")
    object.__setattr__(smuggled, "provenance", Provenance.ENGINE_COMPUTED)
    object.__setattr__(smuggled, "freshness", NodeFreshness.NOT_TRACKED)
    object.__setattr__(smuggled, "stale_reason_codes", ())
    object.__setattr__(smuggled, "integrity", IntegrityStatus.NOT_CHECKED)
    object.__setattr__(smuggled, "integrity_reason_code", "NONE")
    object.__setattr__(smuggled, "attributes", {})

    summary = summarize([smuggled], [])
    assert summary.nodes_by_type[NodeType.OBLIGATION.value] == 1


def test_the_summary_reports_reserved_types_as_explicit_zeros():
    """Absent and zero read differently. A reader should be able to see that
    the reserved types are empty rather than have to notice they are missing."""
    summary = summarize([], [])
    assert summary.nodes_by_type[NodeType.OBLIGATION.value] == 0
    assert summary.nodes_by_type[NodeType.DECISION.value] == 0
    assert set(summary.nodes_by_type) == {t.value for t in NodeType}


def test_the_summary_computes_no_money_total():
    """`ioe.strategy_portfolio` is documented as the only source of a
    user-facing total. Summing candidate amounts here would be the double-count
    the resource ledger exists to prevent, so the only total is one carried
    through."""
    nodes = [
        GraphNode(
            node_type=NodeType.OPPORTUNITY,
            source_kind="ioe.optimization_candidate",
            source_id=str(i),
            provenance=Provenance.ENGINE_COMPUTED,
            attributes={"standalone_potential": "1000.00"},
        )
        for i in range(3)
    ]
    assert summarize(nodes, []).portfolio_total_benefit is None
    assert summarize(nodes, [], portfolio_total_benefit="250.00") \
        .portfolio_total_benefit == "250.00"


def test_derived_deterministic_provenance_is_used_only_for_readiness():
    """The narrow category. It is the one through which a second rules engine
    would arrive, so its only declared user is the evidence producer."""
    users = sorted(
        spec.node_type.value for spec in PRODUCERS.values()
        if Provenance.DERIVED_DETERMINISTIC in spec.provenance
    )
    assert users == [NodeType.EVIDENCE.value]


def test_freshness_refuses_a_value_it_was_never_taught():
    """Fail closed. An unrecognised status is a schema change the graph has not
    been taught, and quietly calling it UNKNOWN would hide exactly that."""
    assert freshness_from_source("stale") is NodeFreshness.STALE
    assert freshness_from_source(None) is NodeFreshness.UNKNOWN
    with pytest.raises(ValueError, match="unrecognised freshness"):
        freshness_from_source("probably_fine")


def test_not_tracked_is_distinct_from_current_and_from_unknown():
    """Three different statements: nothing measures this, nothing measured it
    yet, and it was measured and holds. Collapsing them would let the graph
    claim currency for tables that have no freshness column at all."""
    assert NodeFreshness.NOT_TRACKED != NodeFreshness.CURRENT
    assert NodeFreshness.NOT_TRACKED != NodeFreshness.UNKNOWN


def test_node_keys_are_stable_and_namespaced_by_source_kind():
    a = GraphNode(
        node_type=NodeType.EVIDENCE, source_kind="docs.document",
        source_id="x", provenance=Provenance.DOCUMENT_EXTRACTED,
    )
    b = GraphNode(
        node_type=NodeType.EVIDENCE, source_kind="rules.rule_required_document",
        source_id="x", provenance=Provenance.DERIVED_DETERMINISTIC,
    )
    assert a.key != b.key
    assert a.key == "EVIDENCE:docs.document:x"


def test_the_readiness_vocabulary_is_the_one_the_design_settled_on():
    assert {r.value for r in EvidenceReadiness} == {
        "READY", "PARTIAL", "MISSING", "NOT_REQUIRED", "UNKNOWN",
    }


def test_the_contract_version_is_declared():
    assert GRAPH_CONTRACT_VERSION
