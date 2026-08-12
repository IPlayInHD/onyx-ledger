"""`graph_hash` — determinism, domain separation, and what it deliberately omits.

The claim these tests defend is the one that makes persistence unnecessary: the
same underlying state produces the same hash, so a caller compares hashes
instead of trusting a stored copy.
"""
import pytest

from app.services.ioe.domain import canonical as c
from app.services.state_graph.contracts import (
    EdgeType,
    GraphAnchor,
    GraphEdge,
    GraphNode,
    GraphScope,
    GraphView,
    NodeType,
    Provenance,
    summarize,
)
from app.services.state_graph.hashing import compute_graph_hash, graph_hash_payload

SCOPE = GraphScope(
    user_id="8f14e45f-ceea-467a-9e5f-cf4f0ba3ea44", tax_year=2025,
    view=GraphView.CURRENT,
)


def _node(source_id: str, **attrs: object) -> GraphNode:
    return GraphNode(
        node_type=NodeType.OPPORTUNITY,
        source_kind="ioe.optimization_candidate",
        source_id=source_id,
        provenance=Provenance.ENGINE_COMPUTED,
        attributes=attrs,
    )


def _hash(nodes, edges, anchors=()):
    summary = summarize(nodes, edges)
    return compute_graph_hash(
        scope=SCOPE, anchors=anchors, nodes=nodes, edges=edges, summary=summary
    )


def test_the_same_state_hashes_the_same_twice():
    nodes = [_node("a"), _node("b")]
    assert _hash(nodes, []) == _hash(nodes, [])


def test_node_and_edge_order_do_not_change_the_hash():
    """Assembly order is not a fact about the user's tax state. If iteration
    order could move the hash, every 'has anything changed' answer would be
    noise."""
    a, b = _node("a"), _node("b")
    e1 = GraphEdge(EdgeType.CONFLICTS_WITH, a.key, b.key)
    e2 = GraphEdge(EdgeType.CONFLICTS_WITH, b.key, a.key)
    assert _hash([a, b], [e1, e2]) == _hash([b, a], [e2, e1])


def test_anchor_order_does_not_change_the_hash_either():
    x = GraphAnchor("ioe.scenario", "s-1", "hash-1")
    y = GraphAnchor("ioe.scenario", "s-2", "hash-2")
    assert _hash([], [], (x, y)) == _hash([], [], (y, x))


def test_a_changed_attribute_changes_the_hash():
    """The other half of determinism: it has to be sensitive to what it commits
    to, or it is a constant."""
    assert _hash([_node("a", amount="100.00")], []) != \
        _hash([_node("a", amount="100.01")], [])


def test_a_changed_anchor_hash_changes_the_graph_hash():
    before = GraphAnchor("analysis.analysis_run", "r-1", "snapshot-hash-1")
    after = GraphAnchor("analysis.analysis_run", "r-1", "snapshot-hash-2")
    assert _hash([], [], (before,)) != _hash([], [], (after,))


def test_the_hash_is_domain_separated_from_every_sealed_hash():
    """Registered in `ALL_HASH_DOMAINS` rather than reimplemented. `domain_hash`
    refuses an unregistered domain, which is what makes reuse enforceable."""
    assert c.DOMAIN_TAX_STATE_GRAPH in c.ALL_HASH_DOMAINS
    payload = {"same": "payload"}
    digests = {d: c.domain_hash(d, payload) for d in c.ALL_HASH_DOMAINS}
    assert len(set(digests.values())) == len(c.ALL_HASH_DOMAINS)


def test_there_is_no_second_canonicalizer():
    """The hash body is produced by the existing canonicalizer, not beside it.
    Reconstructing the digest from `canonical_text` proves the payload took the
    documented path rather than a private one."""
    import hashlib

    nodes, edges = [_node("a")], []
    payload = graph_hash_payload(
        scope=SCOPE, anchors=(), nodes=nodes, edges=edges,
        summary=summarize(nodes, edges),
    )
    body = f"{c.domain_tag(c.DOMAIN_TAX_STATE_GRAPH)}\n{c.canonical_text(payload)}"
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == _hash(nodes, edges)


def test_the_payload_carries_the_contract_version():
    """Two builds under different contract versions must not be mistaken for a
    change in the user's data."""
    payload = graph_hash_payload(
        scope=SCOPE, anchors=(), nodes=[], edges=[], summary=summarize([], []),
    )
    assert payload["contract_version"]


def test_a_bare_decimal_or_a_datetime_is_refused_rather_than_silently_scaled():
    """The canonicalizer's own guard, reached through the graph payload. It is
    what forces every money value through `money()` with an explicit scale."""
    from datetime import UTC, datetime
    from decimal import Decimal

    with pytest.raises(c.CanonicalizationError):
        _hash([_node("a", amount=Decimal("1.00"))], [])
    with pytest.raises(c.CanonicalizationError):
        _hash([_node("a", when=datetime.now(tz=UTC))], [])
