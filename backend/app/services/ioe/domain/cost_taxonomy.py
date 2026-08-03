"""Deterministic resolution of authored cost values into the P4 taxonomy.

The legacy vocabulary conflated two economically different things under one
name. `required_cash_contribution` was used both for money that must be
available and then stays the user's (an RRSP contribution) and for money that
leaves for good (a donation). P4 separates them because they behave differently:

    liquidity_commitment        cash must be free; value retained
    asset_transfer              value moved in kind; value retained
    nonrecoverable_expenditure  money is gone; REDUCES the objective
    implementation_cost         fees and advice; REDUCES the objective

Resolution is deterministic and ordered, and every result carries the basis it
was resolved on so a stored cost can be audited:

  1. AUTHORED_VERBATIM            the rule already authored a P4 value → copy it.
  2. LEGACY_EXACT_SYNONYM         `required_expenditure` has exactly one P4
                                  meaning. This is a rename, not a judgement.
  3. LEVER_REGISTRY               `required_cash_contribution` is ambiguous, so
                                  it is resolved by the PINNED lever registry —
                                  the same versioned, reviewable authority that
                                  already owns what an action does. Rule data
                                  never supplies the classification directly.
  4. LEGACY_CONSERVATIVE_DEFAULT  no registry entry. Resolve to
                                  `liquidity_commitment`, which is exactly how
                                  the value behaved before P4: it constrains
                                  feasibility and does not reduce the objective.
                                  Nothing new is claimed about the money.

What this module deliberately does NOT do is guess from the rule's name, its
category, or its economic effect type. A classification that is not authored and
not in the pinned registry stays at the conservative default, and the basis code
says so rather than hiding it.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.ioe.domain.enums import CostType

COST_TAXONOMY_VERSION = "1.0.0"

# Basis codes, mirrored by the CHECK on ioe.candidate_cost.cost_type_source.
SOURCE_AUTHORED = "authored_verbatim"
SOURCE_LEGACY_SYNONYM = "legacy_exact_synonym"
SOURCE_LEVER_REGISTRY = "lever_registry"
SOURCE_LEGACY_DEFAULT = "legacy_conservative_default"

# Values that already speak the P4 vocabulary.
_P4_TAXONOMY = frozenset({
    CostType.LIQUIDITY_COMMITMENT,
    CostType.ASSET_TRANSFER,
    CostType.NONRECOVERABLE_EXPENDITURE,
    CostType.IMPLEMENTATION_COST,
})

# Legacy values with exactly one P4 meaning — a rename, not an interpretation.
_EXACT_SYNONYMS: dict[CostType, CostType] = {
    CostType.REQUIRED_EXPENDITURE: CostType.NONRECOVERABLE_EXPENDITURE,
}

# The one genuinely ambiguous legacy value.
_AMBIGUOUS = CostType.REQUIRED_CASH_CONTRIBUTION


@dataclass(frozen=True)
class ResolvedCostType:
    """A cost type plus the basis on which it was arrived at."""

    cost_type: CostType
    authored_cost_type: CostType
    source: str
    taxonomy_version: str = COST_TAXONOMY_VERSION

    @property
    def is_derived(self) -> bool:
        """True when the value is an IOE derivation rather than rule data."""
        return self.source != SOURCE_AUTHORED


def resolve_cost_type(
    authored: CostType, *, lever_code: str | None = None
) -> ResolvedCostType:
    """Resolve an authored cost value to the P4 taxonomy, deterministically.

    `lever_code` is consulted ONLY for the ambiguous legacy value, and only
    through the pinned registry.
    """
    if authored in _P4_TAXONOMY:
        return ResolvedCostType(authored, authored, SOURCE_AUTHORED)

    synonym = _EXACT_SYNONYMS.get(authored)
    if synonym is not None:
        return ResolvedCostType(synonym, authored, SOURCE_LEGACY_SYNONYM)

    if authored is _AMBIGUOUS:
        from app.services.ioe.domain import levers

        declared = levers.commitment_class(lever_code) if lever_code else None
        if declared is not None:
            return ResolvedCostType(declared, authored, SOURCE_LEVER_REGISTRY)
        # Unchanged behaviour: a feasibility constraint, not a loss.
        return ResolvedCostType(
            CostType.LIQUIDITY_COMMITMENT, authored, SOURCE_LEGACY_DEFAULT
        )

    # Unreachable for the closed CostType enum, but an unknown member must not
    # silently become a nonrecoverable cost and overstate the user's expense.
    return ResolvedCostType(authored, authored, SOURCE_AUTHORED)


__all__ = [
    "COST_TAXONOMY_VERSION",
    "SOURCE_AUTHORED",
    "SOURCE_LEGACY_DEFAULT",
    "SOURCE_LEGACY_SYNONYM",
    "SOURCE_LEVER_REGISTRY",
    "ResolvedCostType",
    "resolve_cost_type",
]
