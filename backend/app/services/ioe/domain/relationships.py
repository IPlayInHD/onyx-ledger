"""Typed relationship derivation (architecture §16).

A relationship is EVIDENCE about how two candidates interact. It is only
generated when something authoritative says the two candidates are related —
never because they happened to appear in the same run. The admissible triggers
are, in priority order:

  1. RULES_CONTRACT — an explicit dependency (`requires_codes`) or an explicit
     exclusion group (`excludes_codes`) supplied by the rules layer.
     Authoritative: these are legal/structural facts and are never trimmed.
  2. SHARED_RESOURCE — two candidates draw on the same declared allocation pool
     (`shared_resource_codes`), so one consumes room the other needs.
  3. RELATIONSHIP_REGISTRY — a governed structural pattern: two levers declared
     in conflict, or two levers writing the same engine input field. Versioned
     as `relationship_registry_version`.
  4. MEASURED_INTERACTION — an engine run showed a SELECTED candidate's benefit
     changed materially in another selected candidate's presence. The edge
     records the measured delta, never an estimate.

DERIVATION IS SPARSE, NOT PAIRWISE
----------------------------------
The previous implementation compared every candidate with every other one, so
edge count grew as n²/2 — at 75 candidates it produced 1,189 relationship rows,
15.9 per candidate, and 6,011 of the 6,127 rows in a populated database were
`overlaps` edges between candidates that merely wrote the same engine field.

Derivation now indexes candidates by RELATIONSHIP KEY first (resource code,
engine field, lever code, opportunity code) and only generates edges inside a
group that a key actually populated. Within a symmetric group the edges form a
STAR around the group's canonical anchor (its lowest candidate key) rather than
a clique: a group of k members yields k−1 edges instead of k(k−1)/2, and the
full membership of the group is still recoverable from the edges because every
member is an endpoint of one. Directional and rules-supplied edges are emitted
exactly as declared — those change portfolio membership, so their semantics are
preserved edge for edge.

Human-readable conflict text is rendered from `explanation_code` plus the
structured fields; prose is never the source of truth.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal

from app.services.ioe.domain import levers as lever_registry
from app.services.ioe.domain.enums import DerivationSource, RelationshipType
from app.services.ioe.domain.models import OptimizationCandidate, RecommendationRelationship

# 1.1.0 — sparse, key-grouped derivation with a persisted derivation source.
# The registry decides which edges exist, so it is versioned and pinned into the
# run version manifest.
RELATIONSHIP_REGISTRY_VERSION = "1.1.0"

MATERIAL_INTERACTION = Decimal("1.00")   # a $1+ change is worth recording

# Backstop, not the mechanism. Grouping is what keeps the count linear; this
# only guarantees the acceptance target holds on a fixture nobody anticipated.
MAX_EDGES_PER_CANDIDATE = 4

EXPLANATION_CODES = {
    RelationshipType.SHARES_LIMIT: "SHARED_POOL",
    RelationshipType.EXCLUDES: "MUTUALLY_EXCLUSIVE",
    RelationshipType.SUBSTITUTES: "ALTERNATIVE_STRATEGY",
    RelationshipType.REQUIRES: "PREREQUISITE",
    RelationshipType.PRECEDES: "ORDERING",
    RelationshipType.OVERLAPS: "SAME_ENGINE_INPUT",
    RelationshipType.ENHANCES: "MEASURED_SYNERGY",
    RelationshipType.REDUCES_VALUE: "MEASURED_OVERLAP",
}

# Symmetric edges are stored ONCE, under a canonical ordering of the pair.
SYMMETRIC_TYPES = frozenset({
    RelationshipType.SHARES_LIMIT,
    RelationshipType.EXCLUDES,
    RelationshipType.SUBSTITUTES,
    RelationshipType.OVERLAPS,
})
# Direction carries meaning here and must survive canonicalization.
DIRECTIONAL_TYPES = frozenset({
    RelationshipType.REQUIRES,
    RelationshipType.PRECEDES,
    RelationshipType.ENHANCES,
    RelationshipType.REDUCES_VALUE,
})

# Edges that change what the portfolio assembler admits (see
# `portfolio._relationship_maps`). Never trimmed, whatever the backstop says.
DECISION_BEARING_TYPES = frozenset({
    RelationshipType.EXCLUDES,
    RelationshipType.SUBSTITUTES,
    RelationshipType.REQUIRES,
    RelationshipType.PRECEDES,
})


class _EdgeSet:
    """Deterministic, deduplicating accumulator.

    Two derivations of the same fact — A declares it excludes B, and B declares
    it excludes A — are one relationship, not two. Symmetric pairs are keyed on
    the sorted pair so the duplicate collapses regardless of which candidate was
    visited first; directional pairs keep their orientation in the key.
    """

    def __init__(self) -> None:
        self._edges: dict[tuple, RecommendationRelationship] = {}

    @staticmethod
    def key_for(edge: RecommendationRelationship) -> tuple:
        if edge.relationship_type in SYMMETRIC_TYPES:
            first, second = sorted((edge.source_key, edge.target_key))
        else:
            first, second = edge.source_key, edge.target_key
        return (first, second, edge.relationship_type.value, edge.shared_resource_code or "")

    def add(self, edge: RecommendationRelationship) -> None:
        if edge.source_key == edge.target_key:
            return
        if edge.relationship_type in SYMMETRIC_TYPES:
            first, second = sorted((edge.source_key, edge.target_key))
            if (first, second) != (edge.source_key, edge.target_key):
                edge = _reoriented(edge, first, second)
        # First writer wins: sources are visited in authority order, so a
        # rules-contract edge is never overwritten by a registry-derived one.
        self._edges.setdefault(self.key_for(edge), edge)

    def ordered(self) -> list[RecommendationRelationship]:
        return [self._edges[k] for k in sorted(self._edges)]


def _reoriented(
    edge: RecommendationRelationship, source: str, target: str
) -> RecommendationRelationship:
    return RecommendationRelationship(
        source_key=source,
        target_key=target,
        relationship_type=edge.relationship_type,
        explanation_code=edge.explanation_code,
        derivation_source=edge.derivation_source,
        shared_resource_code=edge.shared_resource_code,
        maximum_shared_amount=edge.maximum_shared_amount,
        measured_delta=edge.measured_delta,
        resolution_options=edge.resolution_options,
    )


def derive(
    candidates: list[OptimizationCandidate],
    *,
    pool_capacities: dict[str, Decimal] | None = None,
) -> list[RecommendationRelationship]:
    """Sources 1–3. Deterministic and independent of candidate input order."""
    capacities = pool_capacities or {}
    ordered = sorted(candidates, key=lambda x: x.candidate_key)
    edges = _EdgeSet()

    # ---- one pass builds every relationship key ----------------------------
    by_opportunity: dict[str, list[OptimizationCandidate]] = {}
    by_resource: dict[str, list[OptimizationCandidate]] = {}
    by_field: dict[str, list[OptimizationCandidate]] = {}
    by_lever: dict[str, list[OptimizationCandidate]] = {}
    for candidate in ordered:
        by_opportunity.setdefault(candidate.opportunity_code, []).append(candidate)
        for code in candidate.shared_resource_codes:
            by_resource.setdefault(code, []).append(candidate)
        for engine_field in _lever_fields(candidate):
            by_field.setdefault(engine_field, []).append(candidate)
        if candidate.lever_application is not None:
            by_lever.setdefault(candidate.lever_application.lever_code, []).append(candidate)

    # ---- 1. rules-supplied dependencies and exclusion groups ---------------
    # Emitted exactly as declared. These decide portfolio membership, so every
    # declared pair is kept; the volume is bounded by the contract, not by n².
    for candidate in ordered:
        for code in sorted(set(candidate.excludes_codes)):
            for other in by_opportunity.get(code, ()):
                edges.add(RecommendationRelationship(
                    source_key=candidate.candidate_key, target_key=other.candidate_key,
                    relationship_type=RelationshipType.EXCLUDES,
                    explanation_code=EXPLANATION_CODES[RelationshipType.EXCLUDES],
                    derivation_source=DerivationSource.RULES_CONTRACT,
                    resolution_options=("CHOOSE_ONE",),
                ))
        for code in sorted(set(candidate.requires_codes)):
            for other in by_opportunity.get(code, ()):
                edges.add(RecommendationRelationship(
                    source_key=candidate.candidate_key, target_key=other.candidate_key,
                    relationship_type=RelationshipType.REQUIRES,
                    explanation_code=EXPLANATION_CODES[RelationshipType.REQUIRES],
                    derivation_source=DerivationSource.RULES_CONTRACT,
                ))

    # ---- 2. shared allocation pools ---------------------------------------
    for resource_code, group in sorted(by_resource.items()):
        for source, target in _star(group):
            edges.add(RecommendationRelationship(
                source_key=source.candidate_key, target_key=target.candidate_key,
                relationship_type=RelationshipType.SHARES_LIMIT,
                explanation_code=EXPLANATION_CODES[RelationshipType.SHARES_LIMIT],
                derivation_source=DerivationSource.SHARED_RESOURCE,
                shared_resource_code=resource_code,
                maximum_shared_amount=capacities.get(resource_code),
                resolution_options=("APPLY_HIGHER_RANKED", "SPLIT_ALLOCATION"),
            ))

    # ---- 3a. registry: levers declared in conflict --------------------------
    # A declared conflict is a substitution group. It changes admission, so it
    # is emitted for every declared pair rather than starred.
    for lever_code, group in sorted(by_lever.items()):
        if not lever_registry.exists(lever_code):
            continue
        for conflicting in sorted(lever_registry.get(lever_code).conflicts_with):
            for candidate in group:
                for other in by_lever.get(conflicting, ()):
                    edges.add(RecommendationRelationship(
                        source_key=candidate.candidate_key, target_key=other.candidate_key,
                        relationship_type=RelationshipType.SUBSTITUTES,
                        explanation_code=EXPLANATION_CODES[RelationshipType.SUBSTITUTES],
                        derivation_source=DerivationSource.RELATIONSHIP_REGISTRY,
                        resolution_options=("CHOOSE_ONE",),
                    ))

    # ---- 3b. registry: levers writing the same engine input -----------------
    # Explanatory only, and by far the largest population, so it is starred. A
    # pair that already shares a declared pool is described by that stronger
    # edge instead — the same precedence the pairwise implementation applied.
    for group in (by_field[k] for k in sorted(by_field)):
        for source, target in _star(group):
            if set(source.shared_resource_codes) & set(target.shared_resource_codes):
                continue
            edges.add(RecommendationRelationship(
                source_key=source.candidate_key, target_key=target.candidate_key,
                relationship_type=RelationshipType.OVERLAPS,
                explanation_code=EXPLANATION_CODES[RelationshipType.OVERLAPS],
                derivation_source=DerivationSource.RELATIONSHIP_REGISTRY,
            ))

    return _within_budget(edges.ordered(), len(ordered))


def _star(
    group: Sequence[OptimizationCandidate],
) -> Iterable[tuple[OptimizationCandidate, OptimizationCandidate]]:
    """Anchor → each other member. The anchor is the group's lowest candidate
    key, so the topology does not depend on input order.

    A star, not a clique: k members produce k−1 edges. Every member remains an
    endpoint, so a reader can still recover 'these candidates share this key'
    by collecting both endpoints of the edges carrying it.
    """
    if len(group) < 2:
        return ()
    members = sorted(group, key=lambda x: x.candidate_key)
    anchor = members[0]
    return ((anchor, other) for other in members[1:])


def _within_budget(
    edges: list[RecommendationRelationship], candidate_count: int
) -> list[RecommendationRelationship]:
    """Enforce the ≤ 4 × candidate-count ceiling without losing a fact that
    changes an outcome.

    Authoritative rules-contract edges and every decision-bearing type are kept
    unconditionally — dropping one would silently change portfolio membership.
    Only explanatory edges are trimmed, deterministically, from the end of the
    canonical ordering.
    """
    budget = MAX_EDGES_PER_CANDIDATE * max(candidate_count, 1)
    if len(edges) <= budget:
        return edges
    kept = [
        e for e in edges
        if e.derivation_source is DerivationSource.RULES_CONTRACT
        or e.relationship_type in DECISION_BEARING_TYPES
    ]
    trimmable = [
        e for e in edges
        if e.derivation_source is not DerivationSource.RULES_CONTRACT
        and e.relationship_type not in DECISION_BEARING_TYPES
    ]
    room = max(budget - len(kept), 0)
    survivors = kept + trimmable[:room]
    return sorted(survivors, key=_EdgeSet.key_for)


def measured_edge(
    source: OptimizationCandidate, target: OptimizationCandidate
) -> RecommendationRelationship | None:
    """Source 4: record what the engine actually measured, never an estimate.

    Compares the target's standalone potential against the incremental benefit it
    delivered once `source` was already applied.
    """
    if target.standalone_potential is None or target.incremental_portfolio_benefit is None:
        return None
    delta = target.standalone_potential - target.incremental_portfolio_benefit
    if abs(delta) < MATERIAL_INTERACTION:
        return None
    kind = (
        RelationshipType.REDUCES_VALUE if delta > 0 else RelationshipType.ENHANCES
    )
    return RecommendationRelationship(
        source_key=source.candidate_key, target_key=target.candidate_key,
        relationship_type=kind, explanation_code=EXPLANATION_CODES[kind],
        derivation_source=DerivationSource.MEASURED_INTERACTION,
        measured_delta=delta,
    )


def measured_edges(
    selected: Sequence[OptimizationCandidate],
) -> list[RecommendationRelationship]:
    """Measured interactions among the candidates that were actually selected.

    Only selected candidates carry an `incremental_portfolio_benefit`, so this
    is the only population where a measured interaction exists at all. Each
    member is compared against the member applied immediately before it — the
    context that changed its value — which yields at most `len(selected) − 1`
    edges and keeps the direction meaningful (earlier → later).
    """
    edges = _EdgeSet()
    for previous, candidate in zip(selected, selected[1:], strict=False):
        edge = measured_edge(previous, candidate)
        if edge is not None:
            edges.add(edge)
    return edges.ordered()


def _lever_fields(candidate: OptimizationCandidate) -> tuple[str, ...]:
    if candidate.lever_application is None:
        return ()
    if not lever_registry.exists(candidate.lever_application.lever_code):
        return ()
    return lever_registry.get(candidate.lever_application.lever_code).writable_fields
