"""Personal Tax State Graph — the domain contract. Pure: no I/O, no session,
no clock.

WHAT THIS IS, AND WHAT IT REFUSES TO BE
---------------------------------------
The graph is ASSEMBLED STATE. It is not a second tax engine, not a second rules
engine, and not a second source of a total. Every number it carries arrives
already computed by the service that owns it:

    TaxEngineService        the sole tax authority
    RulesEvaluatorService   the sole eligibility authority
    scenario services       the sole scenario authority
    optimization/portfolio  the sole portfolio authority

The graph joins their sealed outputs and says where each value came from. That
is the whole job.

EIGHT LIVE NODE TYPES, TWO RESERVED
-----------------------------------
`OBLIGATION` and `DECISION` are in the taxonomy and have NO producer. The
repository has no filing/payment/instalment vocabulary — `rules.rule_outcome.
outcome_type` is CHECK-constrained to four values, none of them an obligation —
and no Decision Journal exists. Emitting either would mean the graph deciding
what tax law obliges a person to do, or treating deletable recommendation
interaction state as durable decision history.

They stay in the enum so a future governed authority has somewhere to land, and
`PRODUCERS` has no entry for them. `RESERVED_NODE_TYPES` is asserted against the
registry and against assembled output, so acquiring a producer has to be a
deliberate act rather than a line in an unrelated diff.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.services.ioe.domain.integrity import IntegrityStatus

# Bumped when the assembled shape changes in a way that could change a
# `graph_hash` for unchanged underlying state. It enters the hash payload, so
# two builds under different contract versions are never mistaken for a change
# in the user's data.
GRAPH_CONTRACT_VERSION = "1.0.0"


class NodeType(StrEnum):
    FACT = "FACT"
    TAX_STATE = "TAX_STATE"
    OPPORTUNITY = "OPPORTUNITY"
    DEADLINE = "DEADLINE"
    EVIDENCE = "EVIDENCE"
    RESOURCE = "RESOURCE"
    ASSUMPTION = "ASSUMPTION"
    SCENARIO = "SCENARIO"
    # ---- reserved: taxonomy only, no producer, never emitted ----
    OBLIGATION = "OBLIGATION"
    DECISION = "DECISION"


#: The two types no governed authority exists for. Kept as a named constant
#: rather than a literal in a test, so the invariant and the taxonomy cannot
#: drift apart.
RESERVED_NODE_TYPES: frozenset[NodeType] = frozenset(
    {NodeType.OBLIGATION, NodeType.DECISION}
)

LIVE_NODE_TYPES: frozenset[NodeType] = frozenset(NodeType) - RESERVED_NODE_TYPES


class Provenance(StrEnum):
    """Where a node's content came from. Closed, and every node carries one."""

    USER_DECLARED = "USER_DECLARED"
    DOCUMENT_EXTRACTED = "DOCUMENT_EXTRACTED"
    RULE_DATA = "RULE_DATA"
    ENGINE_COMPUTED = "ENGINE_COMPUTED"
    ASSUMPTION_DECLARED = "ASSUMPTION_DECLARED"
    #: A deterministic join the graph performs itself. Deliberately narrow: it
    #: is permitted ONLY for evidence readiness, which is a set difference over
    #: one shared vocabulary. It is not a licence to compute tax, eligibility or
    #: totals — this is the category through which a second rules engine would
    #: arrive, so a new use of it is a design review.
    DERIVED_DETERMINISTIC = "DERIVED_DETERMINISTIC"


class EdgeType(StrEnum):
    """Every type names a row that authorises it; an edge with no origin row is
    not emitted. `SUPERSEDES` is deliberately absent — supersession is run
    lineage, and a graph is a view of one state rather than of the history of
    how that state was recomputed."""

    DERIVED_FROM = "DERIVED_FROM"
    REFERENCES_RULE = "REFERENCES_RULE"
    REQUIRES = "REQUIRES"
    SUPPORTED_BY = "SUPPORTED_BY"
    INELIGIBLE_BECAUSE = "INELIGIBLE_BECAUSE"
    CONSTRAINED_BY = "CONSTRAINED_BY"
    CONSUMES_RESOURCE = "CONSUMES_RESOURCE"
    CONFLICTS_WITH = "CONFLICTS_WITH"
    EXPIRES_AT = "EXPIRES_AT"
    ASSUMES = "ASSUMES"
    REFERENCES_SCENARIO = "REFERENCES_SCENARIO"


#: `REFERENCES_RULE` needs a `RULE` node on the far end, and there is no such
#: node type among the eight. Adding one to carry an edge would make a ninth
#: live type, so rule identity is carried as a `tax_rule_version_id` ATTRIBUTE
#: on the nodes that reference one — nothing is lost, and the edge stays
#: reserved until a rule node has its own governed producer.
RESERVED_EDGE_TYPES: frozenset[EdgeType] = frozenset({EdgeType.REFERENCES_RULE})

LIVE_EDGE_TYPES: frozenset[EdgeType] = frozenset(EdgeType) - RESERVED_EDGE_TYPES


class NodeFreshness(StrEnum):
    """Freshness as the graph reports it.

    The first four mirror what the source stores. `NOT_TRACKED` is the fifth and
    it is the point: `finance.income_source`, `analysis.analysis_run` and the
    document tables have no freshness column, so nothing measures whether they
    are current. Reporting them as `CURRENT` would be the graph inventing a fact
    about itself, and reporting them as `UNKNOWN` would suggest a measurement
    that failed rather than one that was never taken.
    """

    CURRENT = "current"
    STALE = "stale"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"
    NOT_TRACKED = "not_tracked"


_SOURCE_FRESHNESS: Mapping[str, NodeFreshness] = {
    "current": NodeFreshness.CURRENT,
    "stale": NodeFreshness.STALE,
    "superseded": NodeFreshness.SUPERSEDED,
    "unknown": NodeFreshness.UNKNOWN,
}


def freshness_from_source(value: str | None) -> NodeFreshness:
    """Map a stored freshness value, refusing anything unrecognised.

    Fail-closed rather than defaulting: a value this build does not know is a
    schema change the graph has not been taught, and silently calling it
    `UNKNOWN` would hide exactly that.
    """
    if value is None:
        return NodeFreshness.UNKNOWN
    try:
        return _SOURCE_FRESHNESS[value]
    except KeyError:
        raise ValueError(f"unrecognised freshness status: {value!r}") from None


class EvidenceReadiness(StrEnum):
    """Whether the documents a rule demands are held.

    A SECOND AXIS, never a replacement for `EvidenceStatus`. They answer
    different questions — `evidence_status` describes how well the inputs to a
    computed figure are supported, readiness describes whether the required
    documents exist — and the repository already refuses this same collapse one
    level down, where `EvidenceStatus` is documented as never being folded into
    `CalculationBasis`.

    `UNKNOWN` is not a hedge. A `conditional` requirement's applicability is
    carried only in a free-text note, so resolving it would mean reading prose
    and deciding.
    """

    READY = "READY"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    NOT_REQUIRED = "NOT_REQUIRED"
    UNKNOWN = "UNKNOWN"


class GraphView(StrEnum):
    """`CURRENT` assembles from live rows. `HISTORICAL` assembles from the
    frozen and sealed artifacts that already exist, which is why no graph
    snapshot table is needed to look at the past."""

    CURRENT = "current"
    HISTORICAL = "historical"


# ---------------------------------------------------------------------------
# The producer registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProducerSpec:
    """One authoritative producer. A node type without one is not emitted."""

    node_type: NodeType
    #: The service or authority that computes the content. Prose, because its
    #: purpose is to name who owns the number when someone asks.
    authority: str
    #: The tables read. Ordered, and used by the loading plan test to prove the
    #: assembler reads only what it declares.
    source_tables: tuple[str, ...]
    provenance: tuple[Provenance, ...]
    #: Whether freshness is measured at source at all.
    freshness_tracked: bool
    #: Whether the type can appear in a historical view.
    supports_historical: bool


PRODUCERS: Mapping[NodeType, ProducerSpec] = {
    NodeType.FACT: ProducerSpec(
        node_type=NodeType.FACT,
        authority="the tenant's own declarations",
        source_tables=(
            "finance.income_source", "finance.expense_record",
            "wealth.asset", "wealth.liability", "profile.tax_profile",
            "analysis.analysis_input_snapshot",
        ),
        provenance=(Provenance.USER_DECLARED, Provenance.DOCUMENT_EXTRACTED),
        freshness_tracked=False,
        supports_historical=True,
    ),
    NodeType.TAX_STATE: ProducerSpec(
        node_type=NodeType.TAX_STATE,
        authority="TaxEngineService",
        source_tables=("analysis.analysis_run", "analysis.analysis_line_item"),
        provenance=(Provenance.ENGINE_COMPUTED,),
        freshness_tracked=False,
        supports_historical=True,
    ),
    NodeType.OPPORTUNITY: ProducerSpec(
        node_type=NodeType.OPPORTUNITY,
        authority="RulesEvaluatorService via the IOE",
        source_tables=(
            "ioe.optimization_candidate", "ioe.optimization_run",
            # Membership, and the rows that explain a non-membership.
            "ioe.portfolio_member", "ioe.portfolio_exclusion",
            "ioe.recommendation_relationship",
        ),
        provenance=(Provenance.ENGINE_COMPUTED,),
        freshness_tracked=True,
        supports_historical=True,
    ),
    NodeType.DEADLINE: ProducerSpec(
        node_type=NodeType.DEADLINE,
        authority="governed rule data (TKMS)",
        source_tables=("rules.rule_deadline",),
        provenance=(Provenance.RULE_DATA,),
        freshness_tracked=False,
        supports_historical=True,
    ),
    NodeType.EVIDENCE: ProducerSpec(
        node_type=NodeType.EVIDENCE,
        authority="governed required-document metadata resolved against held documents",
        # `docs.document_extraction` is deliberately NOT listed: readiness is
        # decided by `docs.document.status = 'processed'`, and declaring a table
        # the loader never reads would make this registry aspirational rather
        # than descriptive.
        source_tables=(
            "rules.rule_required_document", "docs.document", "ref.document_type",
        ),
        provenance=(Provenance.DERIVED_DETERMINISTIC,),
        freshness_tracked=False,
        supports_historical=False,
    ),
    NodeType.RESOURCE: ProducerSpec(
        node_type=NodeType.RESOURCE,
        authority="IOE portfolio assembly over governed shared-resource declarations",
        # The ledger already carries `resource_code` and `pool_scope`, so
        # `rules.rule_shared_resource` is not read and is not listed.
        source_tables=("ioe.resource_ledger_entry", "ioe.strategy_portfolio"),
        provenance=(Provenance.ENGINE_COMPUTED,),
        freshness_tracked=True,
        supports_historical=True,
    ),
    NodeType.ASSUMPTION: ProducerSpec(
        node_type=NodeType.ASSUMPTION,
        authority="the declared assumption registry",
        source_tables=(
            "ioe.optimization_run.assumption_set", "ioe.scenario_assumption",
        ),
        provenance=(Provenance.ASSUMPTION_DECLARED,),
        freshness_tracked=False,
        supports_historical=True,
    ),
    NodeType.SCENARIO: ProducerSpec(
        node_type=NodeType.SCENARIO,
        authority="ScenarioService",
        source_tables=("ioe.scenario", "ioe.scenario_result"),
        provenance=(Provenance.ENGINE_COMPUTED,),
        freshness_tracked=True,
        supports_historical=True,
    ),
}


# ---------------------------------------------------------------------------
# Nodes, edges, anchors
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GraphNode:
    """One assembled node.

    `attributes` holds values the producing service computed, carried through
    unchanged. The graph never recomputes one, and money is rendered through
    `canonical.money` at hash time exactly as every sealed artifact renders it.
    """

    node_type: NodeType
    #: The table or artifact the node came from — so a key says where to look.
    source_kind: str
    #: Primary key, or a governed code pair. Never row order, never assembly time.
    source_id: str
    provenance: Provenance
    freshness: NodeFreshness = NodeFreshness.NOT_TRACKED
    stale_reason_codes: tuple[str, ...] = ()
    integrity: IntegrityStatus = IntegrityStatus.NOT_CHECKED
    integrity_reason_code: str = "NONE"
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.node_type in RESERVED_NODE_TYPES:
            raise ValueError(
                f"{self.node_type} is a reserved taxonomy value with no governed "
                "authority; it has no producer and must not be constructed"
            )

    @property
    def key(self) -> str:
        return f"{self.node_type.value}:{self.source_kind}:{self.source_id}"


@dataclass(frozen=True)
class GraphEdge:
    edge_type: EdgeType
    source_key: str
    target_key: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.edge_type in RESERVED_EDGE_TYPES:
            raise ValueError(
                f"{self.edge_type} is reserved: its far endpoint has no node type "
                "in this contract, so it has no producer and must not be constructed"
            )

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.edge_type.value, self.source_key, self.target_key)


@dataclass(frozen=True)
class GraphAnchor:
    """A frozen or sealed artifact this graph is derived from.

    Anchors are why no graph snapshot table is needed: a historical graph is a
    deterministic function of artifacts that cannot change, so storing it would
    store a derivable value.
    """

    artifact: str
    artifact_id: str
    content_hash: str | None = None


@dataclass(frozen=True)
class GraphSummary:
    node_count: int
    edge_count: int
    nodes_by_type: Mapping[str, int]
    edges_by_type: Mapping[str, int]
    freshness_rollup: Mapping[str, int]
    integrity_rollup: Mapping[str, int]
    readiness_rollup: Mapping[str, int]
    #: The portfolio's own total, carried through when a portfolio exists.
    #: The summary NEVER sums node amounts: `ioe.strategy_portfolio` is
    #: documented as the only source of a user-facing total, and adding up
    #: `standalone_potential` across candidates is precisely the double-count
    #: the resource ledger exists to prevent.
    portfolio_total_benefit: str | None = None


@dataclass(frozen=True)
class GraphScope:
    user_id: str
    tax_year: int
    view: GraphView


@dataclass(frozen=True)
class TaxStateGraph:
    scope: GraphScope
    anchors: tuple[GraphAnchor, ...]
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    summary: GraphSummary
    graph_hash: str
    contract_version: str = GRAPH_CONTRACT_VERSION

    def nodes_of(self, node_type: NodeType) -> tuple[GraphNode, ...]:
        return tuple(n for n in self.nodes if n.node_type is node_type)


def summarize(
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
    *,
    portfolio_total_benefit: str | None = None,
) -> GraphSummary:
    """Counts only. Every node type appears, including explicit zeros for the
    two reserved ones — a reader should be able to see that they are zero rather
    than have to notice they are absent."""
    nodes_by_type = {t.value: 0 for t in NodeType}
    freshness_rollup = {f.value: 0 for f in NodeFreshness}
    integrity_rollup = {i.value: 0 for i in IntegrityStatus}
    readiness_rollup = {r.value: 0 for r in EvidenceReadiness}

    for node in nodes:
        nodes_by_type[node.node_type.value] += 1
        freshness_rollup[node.freshness.value] += 1
        integrity_rollup[node.integrity.value] += 1
        readiness = node.attributes.get("readiness")
        if isinstance(readiness, str) and readiness in readiness_rollup:
            readiness_rollup[readiness] += 1

    edges_by_type = {e.value: 0 for e in EdgeType}
    for edge in edges:
        edges_by_type[edge.edge_type.value] += 1

    return GraphSummary(
        node_count=len(nodes),
        edge_count=len(edges),
        nodes_by_type=nodes_by_type,
        edges_by_type=edges_by_type,
        freshness_rollup=freshness_rollup,
        integrity_rollup=integrity_rollup,
        readiness_rollup=readiness_rollup,
        portfolio_total_benefit=portfolio_total_benefit,
    )
