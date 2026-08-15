"""The Before-You-Act comparison engine (Entry 12B). Pure: no session, no
clock, no network, no engine.

WHAT IT IS. Two authoritative historical sides in, one deterministic set of
differences out:

    frozen baseline  ──projection──┐
                                   ├── compare ──> TaxStateComparison
    sealed counterfactual ─projection┘

WHAT IT IS NOT. It computes no tax, decides no eligibility, resolves no rule,
reads no document and issues no query. Every value it reports was already
determined by an authority that owns it, and every value it subtracts was
already sealed. Subtracting two sealed tax figures is a comparison; it is not a
second tax engine, and the distinction is the whole reason this module is
allowed to exist beside `TaxEngineService` rather than inside it.

IT DESCRIBES DIFFERENCES. IT DOES NOT RECOMMEND.
There is no "savings", no "best", no "optimal" and no ranking here. Those are
strategy determinations owned elsewhere, and a comparator that produced one
would be deciding what a person should do while claiming only to describe what
changed.

THE GATE COMES FIRST. A side whose authority map reports MISSING_AUTHORITY for
any comparison-required family is REFUSED, through the same
`SEALED_EVIDENCE_INCOMPLETE` verdict the source layer uses. That is not
defensive coding: a comparator handed an unloaded family cannot tell it from an
empty one, and would report every counterpart on the other side as something
the scenario caused. Every sealed v2 scenario is exactly that case.

DIRECTION IS PART OF THE CONTRACT. The comparison reads BASELINE → COUNTERFACTUAL:
"what would change if you did this". Swapping the sides inverts ADDED and
REMOVED, swaps before and after, and negates every numeric delta — which is
asserted, because a comparator that is not invertible is one whose direction is
an accident rather than a decision.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from app.services.ioe.domain import canonical as c
from app.services.ioe.scenario.historical_source import (
    SourceAuthority,
    assert_authority_complete,
)
from app.services.state_graph.contracts import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
)
from app.services.state_graph.hashing import edge_payload, node_payload
from app.services.state_graph.scenario_projection import (
    FamilyApplicability,
    ScenarioComparableGraph,
)

#: The comparison READ-MODEL contract version. Deliberately its own line:
#: nothing about describing a difference belongs to `ScenarioResult`'s sealed
#: protocol, and bumping that version to ship a comparator would re-version
#: every sealed artifact for a change none of them contains.
COMPARISON_CONTRACT_VERSION = "1.0.0"

#: Read as "what changes if you do this", never the reverse.
DIRECTION_BASELINE_TO_COUNTERFACTUAL = "BASELINE_TO_COUNTERFACTUAL"


class ComparisonIntegrityError(RuntimeError):
    """The two sides disagree about something that cannot legitimately differ.

    Raised rather than reported as a difference. One semantic identity resolving
    to two different node types, or two sides projected under different
    applicability policy, is a contradiction in the inputs — and rendering it as
    `CHANGED` would present a broken read model as a change in the user's tax
    position.
    """


class ChangeKind(StrEnum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    CHANGED = "CHANGED"
    UNCHANGED = "UNCHANGED"


#: Identity components of a node's canonical payload. Excluded from field
#: comparison because a matched pair agrees on them BY CONSTRUCTION — they are
#: what made it a pair. Emitting them as unchanged fields would pad every record
#: with four values that can never differ.
_NODE_IDENTITY_FIELDS = frozenset({"key", "node_type", "source_kind", "source_id"})

#: Attribute names carrying governed MONEY, in the canonical scale
#: `canonical.money` renders. A delta is emitted only for these: a numeric
#: difference between two values whose unit nobody declared is arithmetic, not
#: meaning.
_MONEY_ATTRIBUTES = frozenset({
    "amount", "calculated_impact", "baseline_tax", "current_value",
    "acquisition_cost", "current_balance", "total_income", "taxable_income",
    "estimated_tax", "estimated_savings", "standalone_potential",
    "incremental_portfolio_benefit",
})

#: Attribute names carrying governed RATES, in `canonical.rate`'s scale.
_RATE_ATTRIBUTES = frozenset({
    "raw_support_score", "assumption_adjusted_score", "display_support_score",
    "marginal_rate", "average_rate", "interest_rate",
})


@dataclass(frozen=True)
class FieldChange:
    """One semantic field that moved, with the arithmetic where it is defined.

    `delta` is present ONLY for a governed numeric field whose two sides share a
    declared unit. Absent otherwise — including when either side is `None`,
    because inventing a delta against an absent value would state a change of a
    size the seal never recorded.
    """

    field: str
    before: Any
    after: Any
    delta: str | None = None


@dataclass(frozen=True)
class NodeChange:
    key: str
    family: str
    change: ChangeKind
    fields: tuple[FieldChange, ...] = ()


@dataclass(frozen=True)
class EdgeChange:
    edge_type: str
    source_key: str
    target_key: str
    change: ChangeKind
    fields: tuple[FieldChange, ...] = ()


@dataclass(frozen=True)
class ComparisonSummary:
    """Counts, derived only from the records above.

    Nothing here is computed from the underlying graphs a second time: a summary
    that could disagree with the records it summarises would be a second answer
    to the same question.
    """

    node_counts_by_change: Mapping[str, int]
    edge_counts_by_change: Mapping[str, int]
    family_counts: Mapping[str, Mapping[str, int]]
    changed_families: tuple[str, ...]


@dataclass(frozen=True)
class ComparisonSide:
    """One projected side, paired with the authority that produced it.

    The two travel together because neither is sufficient alone: the projection
    says what a side CONTAINS, the authority says whether the source was ever
    CONSULTED, and only the pair can distinguish "this scenario had no
    opportunities" from "nobody asked".
    """

    graph: ScenarioComparableGraph
    authority: Mapping[str, SourceAuthority]


@dataclass(frozen=True)
class TaxStateComparison:
    contract_version: str
    direction: str
    baseline_graph_hash: str
    counterfactual_graph_hash: str
    #: Carried through from the projection UNCHANGED. A comparison that dropped
    #: it would leave a reader unable to tell a family holding nothing from one
    #: that does not apply — the distinction the whole entry rests on.
    node_applicability: tuple[FamilyApplicability, ...]
    edge_applicability: tuple[FamilyApplicability, ...]
    identity_exclusions: tuple[FamilyApplicability, ...]
    node_changes: tuple[NodeChange, ...]
    edge_changes: tuple[EdgeChange, ...]
    summary: ComparisonSummary
    comparison_hash: str


# ---------------------------------------------------------------------------
# Field comparison
# ---------------------------------------------------------------------------
def _flatten(payload: Mapping[str, Any], *, skip: frozenset[str]) -> dict[str, Any]:
    """The canonical payload as flat `field -> value`, attributes namespaced.

    Attributes are flattened rather than compared as one blob so a change names
    the field that moved. `attributes.` prefixes them, so a top-level key and an
    attribute of the same name stay distinct.
    """
    flat: dict[str, Any] = {}
    for name, value in payload.items():
        if name in skip:
            continue
        if name == "attributes" and isinstance(value, Mapping):
            for attribute, inner in value.items():
                flat[f"attributes.{attribute}"] = inner
            continue
        flat[name] = value
    return flat


def _numeric_delta(field: str, before: Any, after: Any) -> str | None:
    """`after - before`, for a governed numeric field only.

    Decimal throughout and rendered back through the same canonicalizer the
    values came from, so a delta is expressed in the scale of the thing it
    measures. A float would be refused by the canonicalizer anyway, and would be
    the wrong type for money regardless.
    """
    attribute = field.split(".", 1)[-1]
    if attribute in _MONEY_ATTRIBUTES:
        render = c.money
    elif attribute in _RATE_ATTRIBUTES:
        render = c.rate
    else:
        return None
    if not isinstance(before, str) or not isinstance(after, str):
        # Includes either side being None: absent is not zero, and a delta
        # against it would report a movement the seal does not support.
        return None
    try:
        return render(Decimal(after) - Decimal(before))
    except (InvalidOperation, ValueError):
        # A stored value that is not a number is a malformed artifact, not a
        # difference. before/after are still reported; the arithmetic is not.
        return None


def _field_changes(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[FieldChange, ...]:
    """Every field that differs, ordered by name so the output is stable."""
    changes: list[FieldChange] = []
    for field in sorted(set(before) | set(after)):
        left, right = before.get(field), after.get(field)
        if left == right:
            continue
        changes.append(FieldChange(
            field=field, before=left, after=right,
            delta=_numeric_delta(field, left, right),
        ))
    return tuple(changes)


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------
def _node_family(node: GraphNode) -> str:
    return node.node_type.value


def _compare_nodes(
    baseline: Sequence[GraphNode], counterfactual: Sequence[GraphNode]
) -> tuple[NodeChange, ...]:
    """Match by the certified semantic node key, and only by that.

    No fuzzy matching, no label matching, no positional matching, and no
    regenerated identity: `GraphNode.key` is what the projection preserved
    precisely so that a baseline node and its counterfactual twin are the same
    node rather than two things that look alike.
    """
    left = {n.key: n for n in baseline}
    right = {n.key: n for n in counterfactual}

    changes: list[NodeChange] = []
    for key in sorted(set(left) | set(right)):
        before, after = left.get(key), right.get(key)
        if before is None and after is not None:
            changes.append(NodeChange(key, _node_family(after), ChangeKind.ADDED))
            continue
        if after is None and before is not None:
            changes.append(NodeChange(key, _node_family(before), ChangeKind.REMOVED))
            continue
        assert before is not None and after is not None
        if before.node_type is not after.node_type:
            # One identity, two families. `key` embeds the node type, so this is
            # unreachable while keys are built the certified way — and if it
            # ever happens, the read model is broken rather than changed.
            raise ComparisonIntegrityError(
                f"node {key!r} is {before.node_type.value} on the baseline and "
                f"{after.node_type.value} on the counterfactual; one semantic "
                "identity cannot name two families"
            )
        fields = _field_changes(
            _flatten(node_payload(before), skip=_NODE_IDENTITY_FIELDS),
            _flatten(node_payload(after), skip=_NODE_IDENTITY_FIELDS),
        )
        changes.append(NodeChange(
            key=key, family=_node_family(before),
            change=ChangeKind.CHANGED if fields else ChangeKind.UNCHANGED,
            fields=fields,
        ))
    return tuple(changes)


def _edge_key(edge: GraphEdge) -> tuple[str, str, str]:
    return edge.sort_key


def _compare_edges(
    baseline: Sequence[GraphEdge], counterfactual: Sequence[GraphEdge]
) -> tuple[EdgeChange, ...]:
    """Match by the certified canonical edge identity — type plus both endpoints.

    WHY `CHANGED` IS RARE HERE, AND WHY IT STILL EXISTS. For every edge family
    the projection retains, the whole semantic content IS the identity: none of
    `DERIVED_FROM`, `REQUIRES`, `EXPIRES_AT`, `ASSUMES` or `REFERENCES_SCENARIO`
    carries an attribute. A semantic change to one of them is therefore a
    different edge, and REMOVED + ADDED is the correct and honest rendering —
    forcing a `CHANGED` concept onto it would invent a mutable payload that does
    not exist.

    The attribute comparison below is kept anyway, so a family that later
    acquires a payload under a stable identity is compared rather than silently
    reported as unchanged. `test_no_retained_edge_family_can_report_changed_today`
    pins the current reality.
    """
    left = {_edge_key(e): e for e in baseline}
    right = {_edge_key(e): e for e in counterfactual}

    changes: list[EdgeChange] = []
    for key in sorted(set(left) | set(right)):
        edge_type, source_key, target_key = key
        before, after = left.get(key), right.get(key)
        if before is None:
            changes.append(EdgeChange(
                edge_type, source_key, target_key, ChangeKind.ADDED))
            continue
        if after is None:
            changes.append(EdgeChange(
                edge_type, source_key, target_key, ChangeKind.REMOVED))
            continue
        fields = _field_changes(
            _flatten(edge_payload(before), skip=frozenset(
                {"edge_type", "source_key", "target_key"})),
            _flatten(edge_payload(after), skip=frozenset(
                {"edge_type", "source_key", "target_key"})),
        )
        changes.append(EdgeChange(
            edge_type=edge_type, source_key=source_key, target_key=target_key,
            change=ChangeKind.CHANGED if fields else ChangeKind.UNCHANGED,
            fields=fields,
        ))
    return tuple(changes)


def _summarize(
    node_changes: Sequence[NodeChange], edge_changes: Sequence[EdgeChange]
) -> ComparisonSummary:
    node_counts = {kind.value: 0 for kind in ChangeKind}
    edge_counts = {kind.value: 0 for kind in ChangeKind}
    family_counts: dict[str, dict[str, int]] = {
        node_type.value: {kind.value: 0 for kind in ChangeKind}
        for node_type in NodeType
    }
    for edge_type in EdgeType:
        family_counts[edge_type.value] = {kind.value: 0 for kind in ChangeKind}

    for node_change in node_changes:
        node_counts[node_change.change.value] += 1
        family_counts[node_change.family][node_change.change.value] += 1
    for edge_change in edge_changes:
        edge_counts[edge_change.change.value] += 1
        family_counts[edge_change.edge_type][edge_change.change.value] += 1

    changed_families = tuple(sorted(
        family for family, counts in family_counts.items()
        if counts[ChangeKind.ADDED.value]
        or counts[ChangeKind.REMOVED.value]
        or counts[ChangeKind.CHANGED.value]
    ))
    return ComparisonSummary(
        node_counts_by_change=node_counts,
        edge_counts_by_change=edge_counts,
        family_counts=family_counts,
        changed_families=changed_families,
    )


def _policy_of(
    entries: Sequence[FamilyApplicability],
) -> tuple[tuple[str, str, str], ...]:
    """A side's applicability VERDICTS, without its counts.

    `retained` is how many nodes one side holds, which is the very thing a
    comparison measures; only status and reason are supposed to match.
    """
    return tuple((e.family, e.status.value, e.reason_code) for e in entries)


def _shared_applicability(
    entries: Sequence[FamilyApplicability],
) -> tuple[FamilyApplicability, ...]:
    """The verdicts as the COMPARISON carries them.

    `retained` is dropped to `None`: a comparison belongs to neither side, so
    quoting one side's count here would silently present the baseline's holdings
    as the comparison's. The distinction §5 requires survives regardless — a
    family that is comparable and empty says `COMPARABLE` with zeros in
    `summary.family_counts`, and one that does not apply says so in its status.
    """
    return tuple(
        FamilyApplicability(
            family=e.family, status=e.status, reason_code=e.reason_code,
            retained=None,
        )
        for e in entries
    )


def compare_scenario_graphs(
    baseline: ComparisonSide, counterfactual: ComparisonSide
) -> TaxStateComparison:
    """Compare two authoritative historical sides. Pure and deterministic.

    THE AUTHORITY GATE RUNS FIRST, on both sides, through the source layer's own
    contract. Nothing below is reached for a side that cannot answer for a
    comparison-required family, so no `ADDED` can ever be manufactured out of a
    source that was never read.
    """
    assert_authority_complete(baseline.authority)
    assert_authority_complete(counterfactual.authority)

    left, right = baseline.graph, counterfactual.graph
    if left.contract_version != right.contract_version:
        raise ComparisonIntegrityError(
            f"the two sides were projected under different contracts: "
            f"{left.contract_version!r} and {right.contract_version!r}")
    for name, a, b in (
        ("node", left.node_applicability, right.node_applicability),
        ("edge", left.edge_applicability, right.edge_applicability),
        ("identity", left.identity_exclusions, right.identity_exclusions),
    ):
        # THE POLICY, NOT THE COUNTS. `retained` is how many nodes ONE side
        # holds, and the two sides differing there is the ordinary case — it is
        # the difference being measured. What may not differ is the verdict:
        # the projection is symmetric by construction, so a disagreement about
        # status or reason means the sides came from different policy, and
        # comparing them would attribute that to the user.
        if _policy_of(a) != _policy_of(b):
            raise ComparisonIntegrityError(
                f"the two sides disagree about {name} applicability policy")

    node_changes = _compare_nodes(left.graph.nodes, right.graph.nodes)
    edge_changes = _compare_edges(left.graph.edges, right.graph.edges)
    summary = _summarize(node_changes, edge_changes)

    comparison = TaxStateComparison(
        contract_version=COMPARISON_CONTRACT_VERSION,
        direction=DIRECTION_BASELINE_TO_COUNTERFACTUAL,
        baseline_graph_hash=left.graph.graph_hash,
        counterfactual_graph_hash=right.graph.graph_hash,
        node_applicability=_shared_applicability(left.node_applicability),
        edge_applicability=_shared_applicability(left.edge_applicability),
        identity_exclusions=_shared_applicability(left.identity_exclusions),
        node_changes=node_changes,
        edge_changes=edge_changes,
        summary=summary,
        comparison_hash="",
    )
    return _with_hash(comparison)


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------
def _applicability_payload(
    entries: Sequence[FamilyApplicability],
) -> list[Mapping[str, Any]]:
    return [
        {"family": e.family, "status": e.status.value,
         "reason_code": e.reason_code, "retained": e.retained}
        for e in entries
    ]


def _field_payload(changes: Sequence[FieldChange]) -> list[Mapping[str, Any]]:
    return [
        {"field": f.field, "before": f.before, "after": f.after,
         "delta": f.delta}
        for f in changes
    ]


def canonical_comparison_payload(
    comparison: TaxStateComparison,
) -> Mapping[str, Any]:
    """The exact payload a comparison is hashed over, exposed so a test can
    assert what enters the hash rather than infer it from a digest.

    It binds BOTH sides' graph hashes. A comparison is only meaningful about the
    two artifacts it was taken over, and a digest that did not name them could
    be presented beside a different pair.
    """
    return {
        "comparison_contract_version": comparison.contract_version,
        "direction": comparison.direction,
        "baseline_graph_hash": comparison.baseline_graph_hash,
        "counterfactual_graph_hash": comparison.counterfactual_graph_hash,
        "node_applicability": _applicability_payload(
            comparison.node_applicability),
        "edge_applicability": _applicability_payload(
            comparison.edge_applicability),
        "identity_exclusions": _applicability_payload(
            comparison.identity_exclusions),
        "node_changes": [
            {"key": n.key, "family": n.family, "change": n.change.value,
             "fields": _field_payload(n.fields)}
            for n in comparison.node_changes
        ],
        "edge_changes": [
            {"edge_type": e.edge_type, "source_key": e.source_key,
             "target_key": e.target_key, "change": e.change.value,
             "fields": _field_payload(e.fields)}
            for e in comparison.edge_changes
        ],
        "summary": {
            "node_counts_by_change": dict(comparison.summary.node_counts_by_change),
            "edge_counts_by_change": dict(comparison.summary.edge_counts_by_change),
            "family_counts": {
                family: dict(counts)
                for family, counts in comparison.summary.family_counts.items()
            },
            "changed_families": list(comparison.summary.changed_families),
        },
    }


def canonical_comparison_text(comparison: TaxStateComparison) -> str:
    return c.canonical_text(canonical_comparison_payload(comparison))


def _with_hash(comparison: TaxStateComparison) -> TaxStateComparison:
    """Domain-separated through the existing canonicalizer.

    `domain_hash` refuses an unregistered domain, which is what makes reuse
    enforceable rather than a convention. Nothing persists this digest: a
    comparison is a deterministic function of two artifacts that cannot change,
    so storing it would store a derivable value.
    """
    payload = canonical_comparison_payload(comparison)
    return TaxStateComparison(
        contract_version=comparison.contract_version,
        direction=comparison.direction,
        baseline_graph_hash=comparison.baseline_graph_hash,
        counterfactual_graph_hash=comparison.counterfactual_graph_hash,
        node_applicability=comparison.node_applicability,
        edge_applicability=comparison.edge_applicability,
        identity_exclusions=comparison.identity_exclusions,
        node_changes=comparison.node_changes,
        edge_changes=comparison.edge_changes,
        summary=comparison.summary,
        comparison_hash=c.domain_hash(c.DOMAIN_SCENARIO_COMPARISON, payload),
    )


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------
_INVERSE_KIND: Mapping[ChangeKind, ChangeKind] = {
    ChangeKind.ADDED: ChangeKind.REMOVED,
    ChangeKind.REMOVED: ChangeKind.ADDED,
    ChangeKind.CHANGED: ChangeKind.CHANGED,
    ChangeKind.UNCHANGED: ChangeKind.UNCHANGED,
}


def _invert_fields(changes: Sequence[FieldChange]) -> tuple[FieldChange, ...]:
    inverted: list[FieldChange] = []
    for change in changes:
        inverted.append(FieldChange(
            field=change.field, before=change.after, after=change.before,
            delta=_numeric_delta(change.field, change.after, change.before),
        ))
    return tuple(inverted)


def invert(comparison: TaxStateComparison) -> TaxStateComparison:
    """The same comparison read the other way round.

    `invert(compare(A, B)) == compare(B, A)` is asserted, and it is the strongest
    statement available that direction is a decision rather than an accident of
    which argument came first. ADDED and REMOVED swap, before and after swap,
    every numeric delta changes sign, and UNCHANGED stays UNCHANGED.
    """
    inverted_nodes: list[NodeChange] = [
        NodeChange(key=n.key, family=n.family,
                   change=_INVERSE_KIND[n.change],
                   fields=_invert_fields(n.fields))
        for n in comparison.node_changes
    ]
    inverted_edges: list[EdgeChange] = [
        EdgeChange(edge_type=e.edge_type, source_key=e.source_key,
                   target_key=e.target_key,
                   change=_INVERSE_KIND[e.change],
                   fields=_invert_fields(e.fields))
        for e in comparison.edge_changes
    ]
    return _with_hash(TaxStateComparison(
        contract_version=comparison.contract_version,
        direction=comparison.direction,
        baseline_graph_hash=comparison.counterfactual_graph_hash,
        counterfactual_graph_hash=comparison.baseline_graph_hash,
        node_applicability=comparison.node_applicability,
        edge_applicability=comparison.edge_applicability,
        identity_exclusions=comparison.identity_exclusions,
        node_changes=tuple(inverted_nodes),
        edge_changes=tuple(inverted_edges),
        # Counted from the INVERTED records, never carried over: a summary that
        # kept the original counts would say two ADDED beside two REMOVED
        # records and be wrong about the direction it claims to describe.
        summary=_summarize(inverted_nodes, inverted_edges),
        comparison_hash="",
    ))
