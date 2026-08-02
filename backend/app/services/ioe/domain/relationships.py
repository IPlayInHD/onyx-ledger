"""Typed relationship derivation (architecture §16).

Edges come from three deterministic sources, in priority order:

  1. RULES-SUPPLIED — shared resource codes and dependency specs from the
     contract. Authoritative: these are legal/structural facts.
  2. REGISTRY — known structural patterns (e.g. two levers that write the same
     engine field), versioned as `relationship_registry_version`.
  3. MEASURED — when an incremental engine run shows a candidate's benefit
     changed materially in another's presence, the edge records the MEASURED
     delta rather than an estimate.

Human-readable conflict text is rendered from `explanation_code` plus structured
fields; prose is never the source of truth.
"""
from __future__ import annotations

from decimal import Decimal

from app.services.ioe.domain import levers as lever_registry
from app.services.ioe.domain.enums import RelationshipType
from app.services.ioe.domain.models import OptimizationCandidate, RecommendationRelationship

RELATIONSHIP_REGISTRY_VERSION = "1.0.0"

MATERIAL_INTERACTION = Decimal("1.00")   # a $1+ change is worth recording

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


def derive(
    candidates: list[OptimizationCandidate],
    *,
    pool_capacities: dict[str, Decimal] | None = None,
) -> list[RecommendationRelationship]:
    """Sources 1 and 2. Deterministic and order-independent in its output."""
    capacities = pool_capacities or {}
    edges: list[RecommendationRelationship] = []
    ordered = sorted(candidates, key=lambda x: x.candidate_key)

    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            # (1) rules-supplied shared pools
            shared = sorted(set(a.shared_resource_codes) & set(b.shared_resource_codes))
            for resource_code in shared:
                edges.append(RecommendationRelationship(
                    source_key=a.candidate_key, target_key=b.candidate_key,
                    relationship_type=RelationshipType.SHARES_LIMIT,
                    explanation_code=EXPLANATION_CODES[RelationshipType.SHARES_LIMIT],
                    shared_resource_code=resource_code,
                    maximum_shared_amount=capacities.get(resource_code),
                    resolution_options=("APPLY_HIGHER_RANKED", "SPLIT_ALLOCATION"),
                ))

            # (1) rules-supplied exclusions / prerequisites
            if b.opportunity_code in a.excludes_codes or a.opportunity_code in b.excludes_codes:
                edges.append(RecommendationRelationship(
                    source_key=a.candidate_key, target_key=b.candidate_key,
                    relationship_type=RelationshipType.EXCLUDES,
                    explanation_code=EXPLANATION_CODES[RelationshipType.EXCLUDES],
                    resolution_options=("CHOOSE_ONE",),
                ))
            if b.opportunity_code in a.requires_codes:
                edges.append(RecommendationRelationship(
                    source_key=a.candidate_key, target_key=b.candidate_key,
                    relationship_type=RelationshipType.REQUIRES,
                    explanation_code=EXPLANATION_CODES[RelationshipType.REQUIRES],
                ))

            # (2) registry: same engine field, or levers declared in conflict
            if not shared and _same_engine_field(a, b):
                edges.append(RecommendationRelationship(
                    source_key=a.candidate_key, target_key=b.candidate_key,
                    relationship_type=RelationshipType.OVERLAPS,
                    explanation_code=EXPLANATION_CODES[RelationshipType.OVERLAPS],
                ))
            if _levers_conflict(a, b):
                edges.append(RecommendationRelationship(
                    source_key=a.candidate_key, target_key=b.candidate_key,
                    relationship_type=RelationshipType.SUBSTITUTES,
                    explanation_code=EXPLANATION_CODES[RelationshipType.SUBSTITUTES],
                    resolution_options=("CHOOSE_ONE",),
                ))
    return edges


def measured_edge(
    source: OptimizationCandidate, target: OptimizationCandidate
) -> RecommendationRelationship | None:
    """Source 3: record what the engine actually measured, never an estimate.

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
        measured_delta=delta,
    )


def _lever_fields(candidate: OptimizationCandidate) -> tuple[str, ...]:
    if candidate.lever_application is None:
        return ()
    if not lever_registry.exists(candidate.lever_application.lever_code):
        return ()
    return lever_registry.get(candidate.lever_application.lever_code).writable_fields


def _same_engine_field(a: OptimizationCandidate, b: OptimizationCandidate) -> bool:
    return bool(set(_lever_fields(a)) & set(_lever_fields(b)))


def _levers_conflict(a: OptimizationCandidate, b: OptimizationCandidate) -> bool:
    if a.lever_application is None or b.lever_application is None:
        return False
    code_a, code_b = a.lever_application.lever_code, b.lever_application.lever_code
    if not (lever_registry.exists(code_a) and lever_registry.exists(code_b)):
        return False
    return (
        code_b in lever_registry.get(code_a).conflicts_with
        or code_a in lever_registry.get(code_b).conflicts_with
    )
