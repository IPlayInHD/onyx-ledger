"""Entry 12B1 §13 — the producer-coverage guard.

THE FAILURE THIS PREVENTS. Comparison policy is a table of verdicts about edge
and node families. A table is only safe while it is COMPLETE, and completeness
is not something a reader notices losing: someone adds an edge type, or teaches
a new module to emit an existing one, and the projection silently applies a
verdict that was reached about a different producer — or no verdict at all.

So the inventory is read out of the SOURCE, not restated by hand. Every
`GraphEdge(...)` construction anywhere under `app/` is located by parsing the
module, attributed to the function that builds it, and checked against the
certified inventory below. A new edge type, a new producer, or an existing
producer moving house all fail here until the policy is reviewed.

§6 has its own assertion in this file, and it is the sharpest one: today
`INELIGIBLE_BECAUSE` has exactly ONE producer and it is portfolio-scoped, which
is the entire basis for classifying it `CURRENT_SOURCE_UNAVAILABLE` rather than
permanently non-comparable. A scenario-level producer would invalidate that
reasoning, so the guard names the producer rather than counting producers.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.services.state_graph.contracts import (
    RESERVED_EDGE_TYPES,
    RESERVED_NODE_TYPES,
    EdgeType,
    NodeType,
)
from app.services.state_graph.scenario_projection import (
    EDGE_COMPARISON_POLICY,
    IDENTITY_EXCLUDED_SOURCE_KINDS,
    NODE_COMPARISON_POLICY,
    Comparability,
)

APP = Path(__file__).resolve().parents[3] / "app"

#: THE CERTIFIED EDGE PRODUCER INVENTORY.
#:
#: `edge type -> {"module:function", ...}`, where a producer is one place that
#: constructs a `GraphEdge` of that type. Read from the source and compared
#: exactly: a missing entry means a producer disappeared, an extra one means a
#: producer appeared, and either is a comparison-policy review.
CERTIFIED_EDGE_PRODUCERS: dict[EdgeType, set[str]] = {
    EdgeType.DERIVED_FROM: {
        "app/services/state_graph/assembler.py:_tax_state",
        "app/services/ioe/scenario/historical_graph.py:_tax_state",
    },
    EdgeType.REQUIRES: {
        "app/services/state_graph/assembler.py:_opportunities",
        "app/services/ioe/scenario/historical_graph.py:_opportunities",
    },
    EdgeType.EXPIRES_AT: {
        "app/services/state_graph/assembler.py:_opportunities",
        "app/services/ioe/scenario/historical_graph.py:_opportunities",
    },
    EdgeType.ASSUMES: {
        "app/services/state_graph/assembler.py:_scenarios",
        "app/services/ioe/scenario/historical_graph.py:_scenario",
    },
    EdgeType.REFERENCES_SCENARIO: {
        "app/services/state_graph/assembler.py:_scenarios",
        "app/services/ioe/scenario/historical_graph.py:_scenario",
    },
    # Identity edges. Live graph only — a sealed bundle retains no document id,
    # and the historical assembler therefore has no producer for this at all.
    EdgeType.SUPPORTED_BY: {
        "app/services/state_graph/assembler.py:_evidence",
    },
    # §6. ONE producer, and it is `ioe.portfolio_exclusion` via
    # `self.src.exclusions`. The classification depends on this being true.
    EdgeType.INELIGIBLE_BECAUSE: {
        "app/services/state_graph/assembler.py:_edges_from_portfolio",
    },
    EdgeType.CONSTRAINED_BY: {
        "app/services/state_graph/assembler.py:_edges_from_portfolio",
    },
    EdgeType.CONSUMES_RESOURCE: {
        "app/services/state_graph/assembler.py:_edges_from_portfolio",
    },
    EdgeType.CONFLICTS_WITH: {
        "app/services/state_graph/assembler.py:_edges_from_relationships",
    },
    # Reserved: no node type exists for its far endpoint, so it has no producer
    # and `GraphEdge.__post_init__` refuses to construct one.
    EdgeType.REFERENCES_RULE: set(),
}

#: The sealed-bundle attribute `_edges_from_portfolio` iterates to reach an
#: `INELIGIBLE_BECAUSE`. Named so "portfolio-scoped" is a checked property of
#: the code rather than a claim in a document.
INELIGIBLE_BECAUSE_SOURCE_ATTRIBUTE = "exclusions"


def _edge_producers() -> dict[str, set[str]]:
    """Every `GraphEdge(...)` under `app/`, as `edge type -> {producers}`.

    Deliberately AST rather than grep: a keyword argument spanning lines, a
    comment mentioning an edge type, or a string in a docstring would each fool
    a textual scan, and this gate is worth nothing if it can be fooled.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for function in ast.walk(tree):
            if not isinstance(
                function, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            for call in ast.walk(function):
                edge_type = _constructed_edge_type(call)
                if edge_type is None:
                    continue
                producer = f"{path.relative_to(APP.parent)}:{function.name}"
                found.setdefault(edge_type, set()).add(producer)
    return found


def _constructed_edge_type(node: ast.AST) -> str | None:
    """`GraphEdge(edge_type=EdgeType.X, ...)` or `GraphEdge(EdgeType.X, ...)`."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else None)
    if name != "GraphEdge":
        return None

    candidates: list[ast.expr] = list(node.args[:1])
    candidates += [kw.value for kw in node.keywords if kw.arg == "edge_type"]
    for value in candidates:
        if (isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == "EdgeType"):
            return value.attr
    #: A `GraphEdge` built from a computed edge type cannot be attributed to a
    #: family by reading the source, which is exactly the shape that would let a
    #: producer hide from this gate.
    return "<COMPUTED>"


# ===========================================================================
# Completeness of the policy tables
# ===========================================================================
def test_every_live_and_reserved_family_has_a_verdict():
    """A new `NodeType` or `EdgeType` member fails here until it is classified,
    which is the point: the enum is where a family arrives."""
    assert set(NODE_COMPARISON_POLICY) == set(NodeType), (
        "a node type has no comparison verdict")
    assert set(EDGE_COMPARISON_POLICY) == set(EdgeType), (
        "an edge type has no comparison verdict")


def test_reserved_families_are_classified_as_reserved():
    for node_type in RESERVED_NODE_TYPES:
        assert NODE_COMPARISON_POLICY[node_type].status is (
            Comparability.RESERVED_NO_PRODUCER)
    for edge_type in RESERVED_EDGE_TYPES:
        assert EDGE_COMPARISON_POLICY[edge_type].status is (
            Comparability.RESERVED_NO_PRODUCER)


def test_every_verdict_carries_a_reason_code():
    for policy in (*NODE_COMPARISON_POLICY.values(),
                   *EDGE_COMPARISON_POLICY.values()):
        assert policy.reason_code and policy.reason_code.isupper(), policy


# ===========================================================================
# The producer inventory itself
# ===========================================================================
def test_the_edge_producer_inventory_is_exactly_the_certified_one():
    """A NEW EDGE PRODUCER FAILS HERE. Adding one to an existing family, or
    teaching a new module to emit an existing family, changes what the verdict
    for that family was reached about — so the review happens before the
    comparison silently inherits it."""
    actual = _edge_producers()
    certified = {
        edge_type.value: producers
        for edge_type, producers in CERTIFIED_EDGE_PRODUCERS.items()
        if producers
    }
    assert actual == certified, (
        "the GraphEdge producer inventory moved; comparison policy must be "
        f"reviewed.\n  found:     {actual}\n  certified: {certified}")


def test_no_edge_is_constructed_from_a_computed_type():
    """A producer whose family cannot be read from the source is a producer this
    gate cannot see, and one that cannot be seen cannot be classified."""
    assert "<COMPUTED>" not in _edge_producers(), (
        "a GraphEdge is built from a computed EdgeType, so its comparison "
        "policy cannot be attributed by reading the source")


def test_every_produced_edge_family_has_a_verdict():
    for edge_type in _edge_producers():
        assert EdgeType(edge_type) in EDGE_COMPARISON_POLICY


def test_reserved_edge_types_have_no_producer():
    produced = set(_edge_producers())
    for reserved in RESERVED_EDGE_TYPES:
        assert reserved.value not in produced, (
            f"{reserved} acquired a producer while still reserved")


# ===========================================================================
# §6 — the INELIGIBLE_BECAUSE future-producer guard
# ===========================================================================
def test_ineligible_because_still_has_exactly_one_portfolio_producer():
    """THE ASSERTION THE §6 CLASSIFICATION RESTS ON.

    `CURRENT_SOURCE_UNAVAILABLE` is justified by a measured fact: the only
    producer reads `ioe.portfolio_exclusion`, which exists only for a portfolio.
    A SCENARIO-LEVEL producer would make a sealed single-scenario source
    available and the verdict would have to change, so it must not be possible
    to add one quietly.
    """
    producers = _edge_producers().get(EdgeType.INELIGIBLE_BECAUSE.value, set())
    assert producers == CERTIFIED_EDGE_PRODUCERS[EdgeType.INELIGIBLE_BECAUSE], (
        "INELIGIBLE_BECAUSE gained or lost a producer; if a scenario-level "
        "producer now exists, its comparison policy is a POLICY REVIEW and it "
        "may no longer be classified CURRENT_SOURCE_UNAVAILABLE")
    assert len(producers) == 1

    (producer,) = producers
    module, _, function = producer.partition(":")
    source = ast.parse((APP.parent / module).read_text())
    (target,) = [
        f for f in ast.walk(source)
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
        and f.name == function
    ]
    read = {
        node.attr for node in ast.walk(target)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "src"
    }
    assert INELIGIBLE_BECAUSE_SOURCE_ATTRIBUTE in read, (
        f"{producer} no longer reads the portfolio exclusion rows the "
        "CURRENT_SOURCE_UNAVAILABLE classification is based on")


def test_ineligible_because_is_not_recorded_as_permanently_non_comparable():
    """The distinction §6 calls non-negotiable, asserted directly."""
    policy = EDGE_COMPARISON_POLICY[EdgeType.INELIGIBLE_BECAUSE]
    assert policy.status is Comparability.CURRENT_SOURCE_UNAVAILABLE
    assert policy.status is not (
        Comparability.NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON)
    assert policy.status is not Comparability.RESERVED_NO_PRODUCER


# ===========================================================================
# §7 — the SUPPORTED_BY / identity guard
# ===========================================================================
def test_document_identity_is_excluded_structurally():
    assert IDENTITY_EXCLUDED_SOURCE_KINDS == {NodeType.EVIDENCE: ("docs.document",)}
    assert EDGE_COMPARISON_POLICY[EdgeType.SUPPORTED_BY].status is (
        Comparability.READINESS_SEMANTICS_ONLY)


def test_the_historical_assembler_produces_no_supported_by_edge():
    """§7's "never fabricate synthetic historical SUPPORTED_BY identity edges",
    as a property of the code rather than of one fixture."""
    producers = _edge_producers()[EdgeType.SUPPORTED_BY.value]
    assert producers == {"app/services/state_graph/assembler.py:_evidence"}
    assert not any("historical" in p for p in producers)


def test_no_module_outside_the_two_assemblers_builds_a_graph_edge():
    """Edges are built where a graph is assembled, and nowhere else. A third
    site would be a producer this entry never classified."""
    modules = {
        producer.split(":")[0]
        for producers in _edge_producers().values()
        for producer in producers
    }
    assert modules == {
        "app/services/state_graph/assembler.py",
        "app/services/ioe/scenario/historical_graph.py",
    }
