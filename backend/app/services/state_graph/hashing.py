"""`graph_hash` — built on the existing canonicalizer, never beside it.

There is exactly one canonical serializer in this repository and exactly one
hash construction. This module supplies a payload; `canonical.domain_hash` does
the rest, and it refuses domains that are not registered, which is what makes
"reuse" enforceable rather than a convention.

WHAT THE HASH COMMITS TO, AND WHAT IT DELIBERATELY DOES NOT
-----------------------------------------------------------
Excluded: the assembly timestamp, and every user-supplied label or note. The
precedent is already set on `ioe.scenario`, whose `label` and `note` carry column
comments saying they are excluded from both hashes because renaming must not
change identity or invalidate evidence. A graph whose hash moved when a scenario
was renamed would be reporting a change that did not happen.

Included: the contract version. Two builds under different contract versions
must not be mistaken for a change in the user's data.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.services.ioe.domain.canonical import DOMAIN_TAX_STATE_GRAPH, domain_hash

from .contracts import (
    GRAPH_CONTRACT_VERSION,
    GraphAnchor,
    GraphEdge,
    GraphNode,
    GraphScope,
    GraphSummary,
)


def _node_payload(node: GraphNode) -> Mapping[str, Any]:
    return {
        "key": node.key,
        "node_type": node.node_type.value,
        "source_kind": node.source_kind,
        "source_id": node.source_id,
        "provenance": node.provenance.value,
        "freshness": node.freshness.value,
        "stale_reason_codes": sorted(node.stale_reason_codes),
        "integrity": node.integrity.value,
        "integrity_reason_code": node.integrity_reason_code,
        "attributes": dict(node.attributes),
    }


def _edge_payload(edge: GraphEdge) -> Mapping[str, Any]:
    return {
        "edge_type": edge.edge_type.value,
        "source_key": edge.source_key,
        "target_key": edge.target_key,
        "attributes": dict(edge.attributes),
    }


def _summary_payload(summary: GraphSummary) -> Mapping[str, Any]:
    return {
        "node_count": summary.node_count,
        "edge_count": summary.edge_count,
        "nodes_by_type": dict(summary.nodes_by_type),
        "edges_by_type": dict(summary.edges_by_type),
        "freshness_rollup": dict(summary.freshness_rollup),
        "integrity_rollup": dict(summary.integrity_rollup),
        "readiness_rollup": dict(summary.readiness_rollup),
        "portfolio_total_benefit": summary.portfolio_total_benefit,
    }


def graph_hash_payload(
    *,
    scope: GraphScope,
    anchors: Sequence[GraphAnchor],
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
    summary: GraphSummary,
) -> Mapping[str, Any]:
    """The exact payload that is hashed. Exposed separately so a test can assert
    what enters the hash rather than infer it from a digest."""
    return {
        "contract_version": GRAPH_CONTRACT_VERSION,
        "scope": {
            "user_id": scope.user_id,
            "tax_year": scope.tax_year,
            "view": scope.view.value,
        },
        "anchors": [
            {
                "artifact": a.artifact,
                "artifact_id": a.artifact_id,
                "content_hash": a.content_hash,
            }
            for a in sorted(anchors, key=lambda a: (a.artifact, a.artifact_id))
        ],
        "nodes": [_node_payload(n) for n in sorted(nodes, key=lambda n: n.key)],
        "edges": [_edge_payload(e) for e in sorted(edges, key=lambda e: e.sort_key)],
        "summary": _summary_payload(summary),
    }


def compute_graph_hash(
    *,
    scope: GraphScope,
    anchors: Sequence[GraphAnchor],
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
    summary: GraphSummary,
) -> str:
    return domain_hash(
        DOMAIN_TAX_STATE_GRAPH,
        graph_hash_payload(
            scope=scope, anchors=anchors, nodes=nodes, edges=edges, summary=summary
        ),
    )
