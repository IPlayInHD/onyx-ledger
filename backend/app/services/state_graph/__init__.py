"""Personal Tax State Graph (Entry 12A).

An assembled deterministic read model. Nothing here is persisted: current state
is assembled from live rows and historical context is anchored to the frozen and
sealed artifacts that already exist, so there is no graph snapshot table and no
migration.
"""
from .assembler import assemble_graph
from .contracts import (
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
    TaxStateGraph,
)
from .loader import GraphLoader, GraphSources
from .scenario_projection import (
    EDGE_COMPARISON_POLICY,
    NODE_COMPARISON_POLICY,
    PROJECTION_CONTRACT_VERSION,
    Comparability,
    ComparabilityPolicy,
    FamilyApplicability,
    ScenarioComparableGraph,
    ScenarioProjectionError,
    canonical_projection_payload,
    canonical_projection_text,
    project_scenario_comparable_graph,
)
from .service import TaxStateGraphService

__all__ = [
    "EDGE_COMPARISON_POLICY",
    "GRAPH_CONTRACT_VERSION",
    "LIVE_EDGE_TYPES",
    "LIVE_NODE_TYPES",
    "NODE_COMPARISON_POLICY",
    "PRODUCERS",
    "PROJECTION_CONTRACT_VERSION",
    "RESERVED_EDGE_TYPES",
    "RESERVED_NODE_TYPES",
    "Comparability",
    "ComparabilityPolicy",
    "EdgeType",
    "EvidenceReadiness",
    "FamilyApplicability",
    "GraphEdge",
    "GraphLoader",
    "GraphNode",
    "GraphSources",
    "NodeFreshness",
    "NodeType",
    "Provenance",
    "ScenarioComparableGraph",
    "ScenarioProjectionError",
    "TaxStateGraph",
    "TaxStateGraphService",
    "assemble_graph",
    "canonical_projection_payload",
    "canonical_projection_text",
    "project_scenario_comparable_graph",
]
