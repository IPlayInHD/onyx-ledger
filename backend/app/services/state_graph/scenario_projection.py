"""Scenario-comparable projection (Entry 12B1 §17). Pure: no session, no clock,
no network, no rule resolution.

WHAT THIS IS
------------
A read-model transformation that takes ONE already-authoritative assembled graph
and returns the part of it that a single-scenario baseline-versus-counterfactual
comparison is allowed to compare, plus an explicit statement about every family
it did not retain and why.

It is NOT the comparator. Nothing here diffs two graphs, and no ADDED / REMOVED /
CHANGED / UNCHANGED vocabulary exists in this module. Comparison is Entry 12B.

WHY IT LIVES BESIDE THE ASSEMBLER RATHER THAN INSIDE IT
-------------------------------------------------------
`assemble_graph` answers "what is this person's tax state". Comparison policy
answers "which parts of two such states may be held against each other", which
is a different question with a different authority. Folding the second into the
first would make the canonical graph quietly narrower for every reader, and the
12A hash would move for a reason 12A never agreed to.

THE INVARIANT THIS MODULE EXISTS TO PROTECT
-------------------------------------------
    zero nodes in a comparable family  !=  a family that does not apply

A single scenario has no portfolio, so it has no resource ledger — `RESOURCE` is
INAPPLICABLE, not empty. If the projection expressed that by dropping the nodes
and saying nothing, a later comparator would read "baseline had N resources,
counterfactual has none" and report N phantom removals. So applicability is
carried as a VALUE, one entry per family, and a comparable family that genuinely
holds nothing is reported as comparable-with-zero rather than as absent.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.services.ioe.domain import canonical as c

from .contracts import (
    RESERVED_EDGE_TYPES,
    RESERVED_NODE_TYPES,
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    TaxStateGraph,
    summarize,
)
from .hashing import compute_graph_hash, graph_hash_payload

#: Bumped when the projected shape changes in a way that could alter a
#: projection's canonical text for an unchanged input graph.
PROJECTION_CONTRACT_VERSION = "1.0.0"


class ScenarioProjectionError(RuntimeError):
    """The policy tables and the graph disagree about what may be compared."""


class Comparability(StrEnum):
    """Why a family is or is not part of a single-scenario comparison.

    Five states, and the distinctions between them are the whole contract. In
    particular `NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON` and
    `CURRENT_SOURCE_UNAVAILABLE` must never be collapsed: the first says the
    concept does not exist for one scenario, the second says the concept is
    meaningful and today has no sealed source. Only the second can be resolved
    by a future producer, and treating it as the first would encode a permanent
    exclusion that nobody decided.
    """

    #: Retained. Both sides carry it and it means the same thing on each.
    COMPARABLE = "COMPARABLE"

    #: The concept belongs to a PORTFOLIO — a chosen set of actions. A scenario
    #: is one action, so there is no ledger, no allocation and no blocking
    #: peer. Not empty: inapplicable.
    NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON = (
        "NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON")

    #: Semantically meaningful for a single action, but every producer that
    #: exists today is portfolio-scoped, so no sealed single-scenario source is
    #: available. A future scenario-level producer is a POLICY REVIEW, not an
    #: automatic inclusion — which is what the producer-coverage guard enforces.
    CURRENT_SOURCE_UNAVAILABLE = "CURRENT_SOURCE_UNAVAILABLE"

    #: The live semantics are document IDENTITY, and historical bundles
    #: deliberately retain readiness semantics only. Readiness survives on the
    #: requirement node; identity is never reconstructed or invented.
    READINESS_SEMANTICS_ONLY = "READINESS_SEMANTICS_ONLY"

    #: Reserved in the 12A taxonomy with zero producers. Inert here for the same
    #: reason it is inert there.
    RESERVED_NO_PRODUCER = "RESERVED_NO_PRODUCER"


#: The statuses under which a family contributes nothing to the comparable view.
#: Named rather than inlined so "was it retained" is asked in exactly one place.
_RETAINED = frozenset({Comparability.COMPARABLE})


@dataclass(frozen=True)
class ComparabilityPolicy:
    """One family's verdict, with the reason recorded beside it.

    The reason is a CODE rather than prose so a later comparator can render it
    to a user without this module owning any presentation.
    """

    status: Comparability
    reason_code: str

    @property
    def retained(self) -> bool:
        return self.status in _RETAINED


# ---------------------------------------------------------------------------
# Node policy — §4 of the entry, one row per live type
# ---------------------------------------------------------------------------
NODE_COMPARISON_POLICY: Mapping[NodeType, ComparabilityPolicy] = {
    NodeType.FACT: ComparabilityPolicy(
        Comparability.COMPARABLE, "SEALED_ON_BOTH_SIDES"),
    NodeType.TAX_STATE: ComparabilityPolicy(
        Comparability.COMPARABLE, "SEALED_ON_BOTH_SIDES"),
    NodeType.OPPORTUNITY: ComparabilityPolicy(
        Comparability.COMPARABLE, "SEALED_ON_BOTH_SIDES"),
    NodeType.DEADLINE: ComparabilityPolicy(
        Comparability.COMPARABLE, "REACHED_THROUGH_PINNED_RULE_VERSION"),
    NodeType.EVIDENCE: ComparabilityPolicy(
        Comparability.COMPARABLE, "SEALED_REQUIRED_AND_HELD_SEMANTICS"),
    NodeType.RESOURCE: ComparabilityPolicy(
        Comparability.NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON,
        "SINGLE_SCENARIO_HAS_NO_PORTFOLIO_LEDGER"),
    NodeType.ASSUMPTION: ComparabilityPolicy(
        Comparability.COMPARABLE, "SEALED_ON_BOTH_SIDES"),
    NodeType.SCENARIO: ComparabilityPolicy(
        Comparability.COMPARABLE, "SEALED_ON_BOTH_SIDES"),
    NodeType.OBLIGATION: ComparabilityPolicy(
        Comparability.RESERVED_NO_PRODUCER, "RESERVED_IN_12A_TAXONOMY"),
    NodeType.DECISION: ComparabilityPolicy(
        Comparability.RESERVED_NO_PRODUCER, "RESERVED_IN_12A_TAXONOMY"),
}


# ---------------------------------------------------------------------------
# Edge policy — §5/§6/§7 of the entry, one row per type
# ---------------------------------------------------------------------------
EDGE_COMPARISON_POLICY: Mapping[EdgeType, ComparabilityPolicy] = {
    EdgeType.DERIVED_FROM: ComparabilityPolicy(
        Comparability.COMPARABLE, "SEALED_ON_BOTH_SIDES"),
    EdgeType.REQUIRES: ComparabilityPolicy(
        Comparability.COMPARABLE, "SEALED_ON_BOTH_SIDES"),
    EdgeType.EXPIRES_AT: ComparabilityPolicy(
        Comparability.COMPARABLE, "REACHED_THROUGH_PINNED_RULE_VERSION"),
    EdgeType.ASSUMES: ComparabilityPolicy(
        Comparability.COMPARABLE, "SEALED_ON_BOTH_SIDES"),
    EdgeType.REFERENCES_SCENARIO: ComparabilityPolicy(
        Comparability.COMPARABLE, "SEALED_ON_BOTH_SIDES"),

    # ---- the one that is meaningful but has no sealed single-scenario source --
    # Its only producer is `ioe.portfolio_exclusion`, written by portfolio
    # assembly: "this candidate was blocked by that candidate". For ONE action
    # the idea is perfectly meaningful — it is the source that is missing, not
    # the meaning. Recorded as unavailable rather than inapplicable so a future
    # scenario-level producer forces a review instead of inheriting a permanent
    # blanket exclusion nobody chose.
    EdgeType.INELIGIBLE_BECAUSE: ComparabilityPolicy(
        Comparability.CURRENT_SOURCE_UNAVAILABLE,
        "ONLY_PRODUCER_IS_PORTFOLIO_EXCLUSION"),

    # ---- portfolio concepts, inapplicable rather than absent ----
    EdgeType.CONSTRAINED_BY: ComparabilityPolicy(
        Comparability.NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON,
        "SINGLE_SCENARIO_HAS_NO_PORTFOLIO_LEDGER"),
    EdgeType.CONSUMES_RESOURCE: ComparabilityPolicy(
        Comparability.NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON,
        "SINGLE_SCENARIO_HAS_NO_PORTFOLIO_LEDGER"),
    EdgeType.CONFLICTS_WITH: ComparabilityPolicy(
        Comparability.NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON,
        "RUN_SCOPED_PAIRWISE_PORTFOLIO_RELATIONSHIP"),

    # ---- identity, deliberately not retained historically ----
    # The live edge terminates on `docs.document`. Historical Held-Evidence
    # Semantics chose READINESS_SEMANTICS_ONLY, so a sealed bundle carries
    # governed TYPE codes and no document ids. Readiness therefore travels on
    # the requirement node — where a READY -> MISSING transition stays fully
    # observable — and no identity edge is ever manufactured to stand in for it.
    EdgeType.SUPPORTED_BY: ComparabilityPolicy(
        Comparability.READINESS_SEMANTICS_ONLY,
        "HISTORICAL_BUNDLES_RETAIN_NO_DOCUMENT_IDENTITY"),

    EdgeType.REFERENCES_RULE: ComparabilityPolicy(
        Comparability.RESERVED_NO_PRODUCER, "NO_LIVE_RULE_NODE_TYPE"),
}


#: Node kinds inside an otherwise comparable family that carry document
#: IDENTITY. Excluded structurally rather than by happening not to occur: the
#: §7 guarantee is that a comparable view never contains document identity for
#: ANY input, not merely for the inputs that happen to lack it today.
IDENTITY_EXCLUDED_SOURCE_KINDS: Mapping[NodeType, tuple[str, ...]] = {
    NodeType.EVIDENCE: ("docs.document",),
}


@dataclass(frozen=True)
class FamilyApplicability:
    """One family's applicability, as a value the comparator reads.

    `retained` is the count for a comparable family — ZERO IS A REAL ANSWER and
    means "comparable, and this side holds none". It is `None` for every family
    that does not participate, so "none held" and "does not apply" can never be
    read as the same thing.
    """

    family: str
    status: Comparability
    reason_code: str
    retained: int | None


@dataclass(frozen=True)
class ScenarioComparableGraph:
    """One side, projected. Ready to be compared; not yet compared.

    `graph` is a real `TaxStateGraph` — same node and edge types, same identity,
    same summary and hash construction — holding only what policy retained. That
    is what makes the projection idempotent: projecting it again is a no-op
    rather than a second, quieter narrowing.
    """

    graph: TaxStateGraph
    node_applicability: tuple[FamilyApplicability, ...]
    edge_applicability: tuple[FamilyApplicability, ...]
    identity_exclusions: tuple[FamilyApplicability, ...]
    contract_version: str = PROJECTION_CONTRACT_VERSION


def _identity_excluded(node: GraphNode) -> bool:
    return node.source_kind in IDENTITY_EXCLUDED_SOURCE_KINDS.get(
        node.node_type, ())


def project_scenario_comparable_graph(
    graph: TaxStateGraph,
) -> ScenarioComparableGraph:
    """Project ONE authoritative graph into its scenario-comparable view.

    Pure and total. It issues no query, runs no engine, resolves no rule, reads
    no document and persists nothing — every decision is a lookup in the two
    policy tables above against the graph it was handed.

    ONE IMPLEMENTATION, BOTH SIDES. There is no baseline branch and no
    counterfactual branch, and the function cannot tell which side it is looking
    at, because a difference in projection policy between the two sides would
    surface later as a difference in the user's tax position that did not exist.
    """
    retained_nodes: list[GraphNode] = []
    node_counts: dict[NodeType, int] = {t: 0 for t in NodeType}

    for node in graph.nodes:
        if _identity_excluded(node):
            continue
        if not NODE_COMPARISON_POLICY[node.node_type].retained:
            continue
        retained_nodes.append(node)
        node_counts[node.node_type] += 1

    #: Every key the projection dropped, so a retained edge pointing at one can
    #: be told apart from an edge that was already dangling on the way in. How
    #: MANY were dropped is deliberately not carried on the result: it is a
    #: property of the transformation rather than of the projected graph, and
    #: recording it there would make a second projection differ from the first.
    #: What was excluded, and why, is stated in the applicability metadata.
    dropped_keys = {n.key for n in graph.nodes} - {n.key for n in retained_nodes}

    retained_edges: list[GraphEdge] = []
    edge_counts: dict[EdgeType, int] = {t: 0 for t in EdgeType}
    for edge in graph.edges:
        if not EDGE_COMPARISON_POLICY[edge.edge_type].retained:
            continue
        for endpoint in (edge.source_key, edge.target_key):
            if endpoint in dropped_keys:
                # A retained edge reaching into a family this projection just
                # removed means the two policy tables contradict each other.
                # Silently dropping it would hide the contradiction and leave a
                # comparator to explain a hole nobody could account for.
                raise ScenarioProjectionError(
                    f"{edge.edge_type.value} edge {edge.source_key!r} -> "
                    f"{edge.target_key!r} is retained by policy but its "
                    f"endpoint {endpoint!r} is in a family the same policy "
                    "excludes; the node and edge tables disagree"
                )
        retained_edges.append(edge)
        edge_counts[edge.edge_type] += 1

    nodes = tuple(sorted(retained_nodes, key=lambda n: n.key))
    edges = tuple(sorted(retained_edges, key=lambda e: e.sort_key))
    # `portfolio_total_benefit` is DROPPED, for the same reason `RESOURCE`,
    # `CONSTRAINED_BY`, `CONSUMES_RESOURCE` and `CONFLICTS_WITH` are: it is the
    # total a PORTFOLIO produced by allocating a pool across a chosen set of
    # actions. Carrying it into a view that declares portfolio concepts
    # inapplicable would let a comparator report that a portfolio total changed
    # between two scenarios that have no portfolio at all.
    summary = summarize(nodes, edges)
    projected = TaxStateGraph(
        scope=graph.scope,
        anchors=graph.anchors,
        nodes=nodes,
        edges=edges,
        summary=summary,
        graph_hash=compute_graph_hash(
            scope=graph.scope, anchors=graph.anchors,
            nodes=nodes, edges=edges, summary=summary,
        ),
        contract_version=graph.contract_version,
    )

    return ScenarioComparableGraph(
        graph=projected,
        node_applicability=tuple(
            FamilyApplicability(
                family=node_type.value,
                status=policy.status,
                reason_code=policy.reason_code,
                # Reserved types are not "zero comparable nodes"; they have no
                # producer at all, which is a different statement.
                retained=(
                    node_counts[node_type]
                    if policy.retained and node_type not in RESERVED_NODE_TYPES
                    else None
                ),
            )
            for node_type, policy in sorted(
                NODE_COMPARISON_POLICY.items(), key=lambda kv: kv[0].value)
        ),
        edge_applicability=tuple(
            FamilyApplicability(
                family=edge_type.value,
                status=policy.status,
                reason_code=policy.reason_code,
                retained=(
                    edge_counts[edge_type]
                    if policy.retained and edge_type not in RESERVED_EDGE_TYPES
                    else None
                ),
            )
            for edge_type, policy in sorted(
                EDGE_COMPARISON_POLICY.items(), key=lambda kv: kv[0].value)
        ),
        identity_exclusions=tuple(
            FamilyApplicability(
                family=f"{node_type.value}:{source_kind}",
                status=Comparability.READINESS_SEMANTICS_ONLY,
                reason_code="HISTORICAL_BUNDLES_RETAIN_NO_DOCUMENT_IDENTITY",
                retained=None,
            )
            for node_type, source_kinds in sorted(
                IDENTITY_EXCLUDED_SOURCE_KINDS.items(),
                key=lambda kv: kv[0].value)
            for source_kind in sorted(source_kinds)
        ),
    )


def _applicability_payload(
    entries: Sequence[FamilyApplicability],
) -> list[Mapping[str, Any]]:
    return [
        {
            "family": e.family,
            "status": e.status.value,
            "reason_code": e.reason_code,
            "retained": e.retained,
        }
        for e in entries
    ]


def canonical_projection_payload(
    projected: ScenarioComparableGraph,
) -> Mapping[str, Any]:
    """The exact payload a projection is compared and hashed through.

    Built on `graph_hash_payload`, the construction 12A already uses, rather
    than on a second serializer — there is one canonicalizer in this repository
    and a projection that grew its own would be one more thing able to disagree
    about what two equal graphs look like.
    """
    return {
        "projection_contract_version": projected.contract_version,
        "graph": graph_hash_payload(
            scope=projected.graph.scope,
            anchors=projected.graph.anchors,
            nodes=projected.graph.nodes,
            edges=projected.graph.edges,
            summary=projected.graph.summary,
        ),
        "node_applicability": _applicability_payload(
            projected.node_applicability),
        "edge_applicability": _applicability_payload(
            projected.edge_applicability),
        "identity_exclusions": _applicability_payload(
            projected.identity_exclusions),
    }


def canonical_projection_text(projected: ScenarioComparableGraph) -> str:
    """The canonical representation, through the project's one canonicalizer.

    Deliberately NOT a new hash domain. A projection is a derivable view of an
    already-hashed graph, and registering a domain for it would imply a sealed
    artifact that nothing persists.
    """
    return c.canonical_text(canonical_projection_payload(projected))
