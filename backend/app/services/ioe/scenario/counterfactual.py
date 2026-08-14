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
from app.services.ioe.domain.scenario import (
    SCENARIO_RESULT_SCHEMA_V2,
    SCENARIO_RESULT_SCHEMA_V3,
)
from app.services.ioe.normalization.service import OpportunityNormalizationService
from app.services.ioe.scenario import held_evidence
from app.services.ioe.scenario.held_evidence import HistoricalHeldEvidenceSnapshot
from app.services.tax_engine.contracts import OpportunityContractV2

#: Bumped when the SEALED SHAPE changes in a way that could alter a
#: derived-state hash for unchanged inputs. Distinct from the scenario result
#: schema version, which governs the result hash contract.
#:
#: v1 is FROZEN: every derived state sealed under scenario-result v2 was hashed
#: from exactly that shape, and adding a key to it unconditionally would change
#: the digest of artifacts nobody touched — reported as corruption long after
#: the cause was gone. So `canonical_payload` dispatches on the state's own
#: version, for the same reason `canonical_scenario_result` dispatches on the
#: row's.
DERIVED_STATE_SCHEMA_V1 = "1.0.0"
#: v2 adds `baseline_candidates`: the sealed BASELINE opportunity set, so a
#: comparison has an authoritative frozen source on both sides.
DERIVED_STATE_SCHEMA_V2 = "2.0.0"

#: What NEW derived states are built as.
COUNTERFACTUAL_DERIVED_STATE_SCHEMA_VERSION = DERIVED_STATE_SCHEMA_V2

SUPPORTED_DERIVED_STATE_SCHEMA_VERSIONS = frozenset({
    DERIVED_STATE_SCHEMA_V1, DERIVED_STATE_SCHEMA_V2,
})


class UnsupportedDerivedStateVersion(ValueError):
    """A sealed derived state names a shape this build cannot canonicalize."""


#: WHICH DERIVED-STATE SHAPE EACH RESULT CONTRACT SEALS.
#:
#: The two version lines are separate — one governs the outer result hash, the
#: other the payload that hash binds — but they are not INDEPENDENT: a scenario
#: sealed as result-v2 must carry a derived state shaped as v1, because that is
#: what every result-v2 artifact in the database already carries. Leaving the
#: derived-state version to default instead would have made a newly created v2
#: scenario seal a v2 payload, so "v2" would mean two different things
#: depending on when the row was written.
DERIVED_STATE_FOR_RESULT_VERSION: dict[str, str] = {
    SCENARIO_RESULT_SCHEMA_V2: DERIVED_STATE_SCHEMA_V1,
    SCENARIO_RESULT_SCHEMA_V3: DERIVED_STATE_SCHEMA_V2,
}

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
    """Everything a later comparison needs, and nothing it can recompute.

    ONE FIELD HERE IS NOT COUNTERFACTUAL. `baseline_held_evidence` is BASELINE
    state — what the user actually held when the scenario was sealed — and the
    scenario did not cause it and cannot change it. It rides in this artifact
    because it must be sealed in the same transaction, bound by the same hash
    and removed by the same retention rule as everything else here; giving it a
    second column and a second digest would buy nothing but another thing to
    disagree.

    A comparison uses it on BOTH sides. The baseline and the counterfactual are
    resolved against the SAME held evidence, so a `READY → MISSING` transition
    means "this scenario requires a document you do not have", never "your
    document library changed".
    """

    line_items: tuple[CounterfactualLineItem, ...]
    candidates: tuple[CounterfactualCandidate, ...]
    pinned_rule_version_ids: tuple[str, ...]
    #: T1 baseline context, NOT a counterfactual output. `None` only for states
    #: built before Entry 12B1 sealed held evidence.
    baseline_held_evidence: HistoricalHeldEvidenceSnapshot | None = None
    #: BASELINE state, and the second field here that is not counterfactual.
    #: The same rules evaluation, over the same pinned rule versions, against
    #: the frozen baseline's facts instead of the scenario's — so the two sides
    #: are the same KIND of object and can be held against each other at all.
    #:
    #: `None` means the state predates v2 of this contract and carries no
    #: baseline opportunity set. It is emphatically not an empty one: a
    #: comparator handed `()` would read every counterfactual opportunity as one
    #: the scenario created.
    baseline_candidates: tuple[CounterfactualCandidate, ...] | None = None
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
    baseline_held_evidence: HistoricalHeldEvidenceSnapshot | None = None,
    baseline_opportunities: Sequence[OpportunityContractV2] | None = None,
    schema_version: str = COUNTERFACTUAL_DERIVED_STATE_SCHEMA_VERSION,
) -> CounterfactualDerivedState:
    """`baseline_held_evidence` is an INPUT, never something this builder goes
    and fetches.

    That is the whole boundary. Creation captures it from the live library once
    and passes it here; replay loads the SEALED one and passes that. If the
    builder queried documents itself, replay would rebuild a March scenario
    against June's library and report a mismatch caused by an upload.

    `baseline_opportunities` is the same kind of input and obeys the same rule:
    it is the result of evaluating the SAME pinned rule versions against the
    frozen baseline's facts, performed by the caller that holds a session, and
    normalized and scored through the identical path the counterfactual set
    uses. Building it any other way would make the two sides different kinds of
    object and the comparison meaningless.

    `schema_version` is explicit so replay can rebuild a state under the
    contract its row was sealed under rather than under this build's current one.
    """
    if schema_version not in SUPPORTED_DERIVED_STATE_SCHEMA_VERSIONS:
        raise UnsupportedDerivedStateVersion(
            f"unsupported derived-state schema version: {schema_version!r}")
    if schema_version == DERIVED_STATE_SCHEMA_V1 and baseline_opportunities is not None:
        # Fail closed rather than drop it. Silently discarding would let a
        # caller believe the baseline side was sealed when the bytes say v1.
        raise UnsupportedDerivedStateVersion(
            "a v1 derived state cannot carry a baseline opportunity set: v1 is "
            "frozen historical shape and must not absorb v2 semantics")
    if schema_version == DERIVED_STATE_SCHEMA_V2 and baseline_opportunities is None:
        # Refused HERE rather than at canonicalization. A state built without
        # the set is already wrong, and letting it exist until something tries
        # to hash it moves the error a long way from the caller that caused it.
        # An authoritatively empty baseline is `()`; `None` means "not
        # evaluated", which v2 has no way to express.
        raise UnsupportedDerivedStateVersion(
            "a v2 derived state requires a baseline opportunity set: v2 exists "
            "to seal it, so building without one would claim a source that is "
            "not there. An authoritatively empty set is `()`, not `None`.")

    return CounterfactualDerivedState(
        line_items=build_line_items(line_items),
        candidates=build_candidates(opportunities),
        pinned_rule_version_ids=tuple(sorted(str(v) for v in pinned_rule_version_ids)),
        baseline_held_evidence=baseline_held_evidence,
        baseline_candidates=(
            build_candidates(baseline_opportunities)
            if baseline_opportunities is not None else None
        ),
        schema_version=schema_version,
    )


def _candidate_payload(candidate: CounterfactualCandidate) -> dict[str, Any]:
    """One candidate's sealed shape.

    Extracted so the baseline and counterfactual sets are rendered by the same
    code: two renderers would be two chances to describe the same kind of object
    differently, and a comparison would report the difference as the user's.
    """
    return {
        "candidate_key": candidate.candidate_key,
        "opportunity_code": candidate.opportunity_code,
        "rule_version_id": candidate.rule_version_id,
        "eligibility_status": candidate.eligibility_status,
        "eligibility_basis_codes": (
            list(candidate.eligibility_basis_codes)
            if candidate.eligibility_basis_codes is not None else None
        ),
        "calculation_basis": candidate.calculation_basis,
        "calculated_impact": candidate.calculated_impact,
        "economic_effect_type": candidate.economic_effect_type,
        "reversibility": candidate.reversibility,
        "required_documents": [list(d) for d in candidate.required_documents],
        "applicable_deadlines": list(candidate.applicable_deadlines),
        "dependencies": [list(d) for d in candidate.dependencies],
        "shared_resource_codes": list(candidate.shared_resource_codes),
        "raw_support_score": candidate.raw_support_score,
        "assumption_adjusted_score": candidate.assumption_adjusted_score,
        "display_support_score": candidate.display_support_score,
        "support_cap_applied": candidate.support_cap_applied,
        "support_cap_reason_code": candidate.support_cap_reason_code,
    }


def canonical_payload(state: CounterfactualDerivedState) -> dict[str, Any]:
    """The exact payload that is hashed, exposed so a test can assert what
    enters the hash rather than infer it from a digest.

    VERSION-DISPATCHED, for the reason the module header gives: a v1 state must
    render exactly the shape it was sealed from, with no `baseline_candidates`
    key at all. Adding the key with a `null` value would still change the bytes,
    and therefore the digest, of every artifact already in the database.
    """
    if state.schema_version not in SUPPORTED_DERIVED_STATE_SCHEMA_VERSIONS:
        raise UnsupportedDerivedStateVersion(
            f"unsupported derived-state schema version: "
            f"{state.schema_version!r}")

    payload = _canonical_payload_v1(state)
    if state.schema_version == DERIVED_STATE_SCHEMA_V1:
        if state.baseline_candidates is not None:
            raise UnsupportedDerivedStateVersion(
                "a v1 derived state carries a baseline opportunity set; the "
                "shape and the version disagree")
        return payload

    if state.baseline_candidates is None:
        raise UnsupportedDerivedStateVersion(
            "a v2 derived state requires a baseline opportunity set: v2 exists "
            "to seal it, and an absent one would claim a source that is not "
            "there. An authoritatively empty set is `()`, not `None`.")
    payload["baseline_candidates"] = [
        _candidate_payload(x) for x in state.baseline_candidates]
    return payload


def _canonical_payload_v1(state: CounterfactualDerivedState) -> dict[str, Any]:
    """THE FROZEN v1 SHAPE. Every derived state sealed under scenario-result v2
    was hashed from exactly this, so nothing in it may be tidied."""
    return {
        "schema_version": state.schema_version,
        "pinned_rule_version_ids": list(state.pinned_rule_version_ids),
        # Named for what it is. A reader who sees "held evidence" inside a
        # "counterfactual derived state" must be able to tell at a glance that
        # it describes the baseline, not something the scenario produced.
        "baseline_held_evidence": (
            held_evidence.canonical_payload(state.baseline_held_evidence)
            if state.baseline_held_evidence is not None else None
        ),
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
        # Rendered by the SAME function the baseline set uses. The two lists
        # describe the same kind of object, so two renderers would be two
        # chances to describe them differently — and a comparison would report
        # that difference as the user's.
        "candidates": [_candidate_payload(x) for x in state.candidates],
    }


def canonical_payload_and_hash(
    state: CounterfactualDerivedState,
) -> tuple[dict[str, Any], str]:
    """The payload to persist and the hash OF THAT EXACT OBJECT.

    Persistence must store a payload and a digest that provably describe each
    other. Calling `canonical_payload` once for the column and again inside
    `derived_state_hash` would leave two objects where there should be one, and
    any future divergence between those calls would be sealed as a
    self-inconsistent artifact — a row whose own hash does not verify, which
    replay would report as corruption long after the cause was gone.
    """
    payload = canonical_payload(state)
    return payload, c.domain_hash(c.DOMAIN_COUNTERFACTUAL_DERIVED_STATE, payload)


def derived_state_hash(state: CounterfactualDerivedState) -> str:
    """Domain-separated, through the existing canonicalizer. `domain_hash`
    refuses unregistered domains, which is what makes reuse enforceable rather
    than a convention."""
    return canonical_payload_and_hash(state)[1]


def payload_hash(payload: dict[str, Any]) -> str:
    """Hash a payload READ BACK from storage.

    Replay needs this: rebuilding the state and hashing the rebuild proves the
    determination still reproduces, but it says nothing about whether the bytes
    in the column still match the digest beside them. Only hashing what is
    actually stored can catch a payload edited in place.
    """
    return c.domain_hash(c.DOMAIN_COUNTERFACTUAL_DERIVED_STATE, payload)


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
