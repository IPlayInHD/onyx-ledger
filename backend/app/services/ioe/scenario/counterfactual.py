"""The sealed counterfactual derived state (Entry 12B1). Pure: no session, no
clock, no network.

WHAT THIS EXISTS TO FIX. A sealed scenario recorded what the counterfactual tax
TOTAL was and nothing about what the counterfactual tax state CONTAINED. So a
later baseline-vs-counterfactual comparison had a baseline made of line items,
opportunities, requirements and deadlines, and a counterfactual made of one
number — and differencing those would have reported every baseline detail as
`REMOVED` when it had merely never been persisted.

WHAT IT DOES NOT DO. It computes no tax and decides no eligibility. Both arrive
already determined:

    TaxEngineService      already returned `TaxResult.line_items` and the
                          scenario threw them away
    RulesEvaluatorService already accepts `pinned_rule_version_ids` and was
                          simply never called with the counterfactual facts

This module shapes, orders and hashes those two authoritative outputs. That is
the whole job, and it is why nothing here imports an engine.

SUPPORT SCORES ARE COMPUTED THE SAME WAY THE BASELINE COMPUTES THEM, which is
the only way the five fields can be compared later. `OptimizationOrchestrator`
calls:

    support.compute(evidence_status=..., calculation_basis=...)

with no `assumptions` argument, so its per-candidate scores carry no assumption
penalty. This module matches that call EXACTLY. Passing the scenario's
assumptions here would look more thorough and would be a defect: every
counterfactual candidate would score below its baseline twin purely because the
scenario declared assumptions, and the comparison would report
`SUPPORT_CHANGED` on candidates nothing had changed about. The scenario's
assumption-adjusted support stays where it already lives — on the scenario
result as a whole.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.ioe.domain import canonical as c
from app.services.ioe.domain import confidence as support
from app.services.ioe.normalization.service import OpportunityNormalizationService
from app.services.tax_engine.contracts import OpportunityContractV2

#: Bumped when the SEALED SHAPE changes in a way that could alter a
#: derived-state hash for unchanged inputs. Distinct from the scenario result
#: schema version, which governs the result hash contract.
COUNTERFACTUAL_DERIVED_STATE_SCHEMA_VERSION = "1.0.0"

#: Structured absence for scenarios sealed before this capability existed.
#: Never a backfill: evaluating today's rules against an old sealed scenario
#: would fabricate historical evidence.
LEGACY_UNAVAILABLE = "COUNTERFACTUAL_DERIVED_STATE_UNAVAILABLE_LEGACY"


@dataclass(frozen=True)
class CounterfactualLineItem:
    """One component of the counterfactual tax state, from `TaxResult`."""

    kind: str
    label: str
    amount: str            # canonical money, never a bare Decimal
    fact_key: str | None = None
    tax_rule_version_id: str | None = None


@dataclass(frozen=True)
class CounterfactualCandidate:
    """One counterfactual opportunity, sealed.

    `candidate_key` is the SEMANTIC identity — `opportunity_code:rule_version_id`
    from `OpportunityNormalizationService`, the same basis the baseline uses. No
    physical row id, no array position, no evaluation-order artefact enters it,
    which is what lets a later comparison match a baseline opportunity to its
    counterfactual twin.
    """

    candidate_key: str
    opportunity_code: str
    rule_version_id: str | None

    # ---- legal determinations, copied verbatim from the rules layer ----
    eligibility_status: str
    #: `None` means the rule said nothing; `()` means it said "no basis codes".
    #: Collapsing the two would turn silence into a positive statement.
    eligibility_basis_codes: tuple[str, ...] | None
    calculation_basis: str | None
    calculated_impact: str | None
    economic_effect_type: str | None
    reversibility: str | None

    # ---- governed metadata reached through this candidate ----
    required_documents: tuple[tuple[str, str], ...]      # (type_code, necessity)
    applicable_deadlines: tuple[str, ...]                # deadline codes
    dependencies: tuple[tuple[str, str], ...]            # (rule_code, type)
    #: A REQUIREMENT DECLARATION, not an allocation. A single scenario has no
    #: portfolio and therefore no ledger: this says "draws on this pool", never
    #: "consumed this much of it".
    shared_resource_codes: tuple[str, ...]

    # ---- support, computed exactly as the baseline computes it ----
    raw_support_score: str | None
    assumption_adjusted_score: str | None
    display_support_score: str | None
    support_cap_applied: bool
    support_cap_reason_code: str | None


@dataclass(frozen=True)
class CounterfactualDerivedState:
    """Everything a later comparison needs, and nothing it can recompute."""

    line_items: tuple[CounterfactualLineItem, ...]
    candidates: tuple[CounterfactualCandidate, ...]
    pinned_rule_version_ids: tuple[str, ...]
    schema_version: str = COUNTERFACTUAL_DERIVED_STATE_SCHEMA_VERSION


def build_line_items(raw: Sequence[dict]) -> tuple[CounterfactualLineItem, ...]:
    """Shape `TaxResult.line_items` for sealing.

    Ordered by `(kind, label)` rather than by engine emission order: the engine's
    order is an implementation detail, and a hash that moved when it changed
    would report a difference the user never caused.
    """
    items = [
        CounterfactualLineItem(
            kind=str(item.get("kind", "")),
            label=str(item.get("label", "")),
            amount=c.money(_as_decimal(item.get("amount"))) or "0.00",
            fact_key=_text_or_none(item.get("fact_key")),
            tax_rule_version_id=_text_or_none(item.get("tax_rule_version_id")),
        )
        for item in raw
    ]
    return tuple(sorted(items, key=lambda i: (i.kind, i.label, i.amount)))


def build_candidates(
    opportunities: Sequence[OpportunityContractV2],
) -> tuple[CounterfactualCandidate, ...]:
    """Normalize and score exactly as the baseline path does.

    `OpportunityNormalizationService` is reused rather than reimplemented, so a
    counterfactual candidate is the same kind of object as a baseline one — the
    precondition for comparing them at all.
    """
    normalizer = OpportunityNormalizationService()
    built: list[CounterfactualCandidate] = []

    for opportunity in opportunities:
        candidate = normalizer.normalize(opportunity)
        breakdown = support.compute(
            evidence_status=candidate.evidence_status,
            calculation_basis=(
                candidate.calculation_basis
                or support.CalculationBasis.RULE_FORMULA_DETERMINED
            ),
        )
        built.append(CounterfactualCandidate(
            candidate_key=candidate.candidate_key,
            opportunity_code=opportunity.opportunity_code,
            rule_version_id=(
                str(opportunity.rule_version_id)
                if opportunity.rule_version_id is not None else None
            ),
            eligibility_status=str(candidate.eligibility_status.value),
            eligibility_basis_codes=(
                tuple(sorted(opportunity.eligibility_basis_codes))
                if opportunity.eligibility_basis_codes else None
            ),
            calculation_basis=(
                str(candidate.calculation_basis.value)
                if candidate.calculation_basis is not None else None
            ),
            calculated_impact=(
                c.money(opportunity.calculated_impact)
                if opportunity.calculated_impact is not None else None
            ),
            economic_effect_type=opportunity.economic_effect_type,
            reversibility=opportunity.reversibility,
            required_documents=tuple(sorted(
                (d.document_type_code, d.necessity)
                for d in opportunity.required_documents
            )),
            applicable_deadlines=tuple(sorted(
                d.deadline_code for d in opportunity.applicable_deadlines
            )),
            dependencies=tuple(sorted(
                (d.depends_on_rule_code, d.dependency_type)
                for d in opportunity.dependencies
            )),
            shared_resource_codes=tuple(sorted(opportunity.shared_resource_codes)),
            raw_support_score=c.rate(breakdown.raw_support_score),
            assumption_adjusted_score=c.rate(breakdown.assumption_adjusted_score),
            display_support_score=c.rate(breakdown.display_support_score),
            support_cap_applied=breakdown.cap_applied,
            support_cap_reason_code=breakdown.cap_reason_code,
        ))

    # Sorted by SEMANTIC identity, so the hash is invariant under SQL row order,
    # evaluator output order and PYTHONHASHSEED.
    return tuple(sorted(built, key=lambda x: x.candidate_key))


def build_derived_state(
    *,
    line_items: Sequence[dict],
    opportunities: Sequence[OpportunityContractV2],
    pinned_rule_version_ids: Sequence[Any],
) -> CounterfactualDerivedState:
    return CounterfactualDerivedState(
        line_items=build_line_items(line_items),
        candidates=build_candidates(opportunities),
        pinned_rule_version_ids=tuple(sorted(str(v) for v in pinned_rule_version_ids)),
    )


def canonical_payload(state: CounterfactualDerivedState) -> dict[str, Any]:
    """The exact payload that is hashed, exposed so a test can assert what
    enters the hash rather than infer it from a digest."""
    return {
        "schema_version": state.schema_version,
        "pinned_rule_version_ids": list(state.pinned_rule_version_ids),
        "line_items": [
            {
                "kind": i.kind,
                "label": i.label,
                "amount": i.amount,
                "fact_key": i.fact_key,
                "tax_rule_version_id": i.tax_rule_version_id,
            }
            for i in state.line_items
        ],
        "candidates": [
            {
                "candidate_key": x.candidate_key,
                "opportunity_code": x.opportunity_code,
                "rule_version_id": x.rule_version_id,
                "eligibility_status": x.eligibility_status,
                "eligibility_basis_codes": (
                    list(x.eligibility_basis_codes)
                    if x.eligibility_basis_codes is not None else None
                ),
                "calculation_basis": x.calculation_basis,
                "calculated_impact": x.calculated_impact,
                "economic_effect_type": x.economic_effect_type,
                "reversibility": x.reversibility,
                "required_documents": [list(d) for d in x.required_documents],
                "applicable_deadlines": list(x.applicable_deadlines),
                "dependencies": [list(d) for d in x.dependencies],
                "shared_resource_codes": list(x.shared_resource_codes),
                "raw_support_score": x.raw_support_score,
                "assumption_adjusted_score": x.assumption_adjusted_score,
                "display_support_score": x.display_support_score,
                "support_cap_applied": x.support_cap_applied,
                "support_cap_reason_code": x.support_cap_reason_code,
            }
            for x in state.candidates
        ],
    }


def derived_state_hash(state: CounterfactualDerivedState) -> str:
    """Domain-separated, through the existing canonicalizer. `domain_hash`
    refuses unregistered domains, which is what makes reuse enforceable rather
    than a convention."""
    return c.domain_hash(
        c.DOMAIN_COUNTERFACTUAL_DERIVED_STATE, canonical_payload(state)
    )


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    # A float would be refused by the canonicalizer anyway; refusing here names
    # the reason instead of failing three frames later.
    raise TypeError(f"line item amount must be Decimal-compatible, got {type(value)}")


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
