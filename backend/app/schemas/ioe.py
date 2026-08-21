"""IOE API schemas (§P6).

One rule shapes this whole module: **a bare number is never returned.** Every
monetary field is a `MonetaryAmount`, which carries what kind of money it is,
how it was arrived at, what evidence stands behind it, which tax year and
currency it belongs to, over what horizon, how well supported it is, and whether
the result it came from is still current.

An amount without that context is the thing that makes an educational tool read
like advice: "$3,240" invites a decision, while "$3,240 — current-year tax
reduction, engine-determined, documented evidence, 2025, CAD, this year only,
support 74/100, current" invites a question. The schema makes the second form
the only expressible one.

Nothing here computes. These types are populated from sealed rows; no validator
sums, ranks, reconciles, or re-derives anything.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.ioe.domain.confidence import SUPPORT_SCORE_DISCLAIMER

CURRENCY_CAD = "CAD"


class SupportScore(BaseModel):
    """A support/reliability score — NOT a probability.

    Named `support` throughout the API for that reason. The disclaimer travels
    with the value so no surface can present it as a likelihood of CRA
    acceptance or of receiving the amount shown.
    """

    model_config = ConfigDict(from_attributes=True)

    display_support_score: Decimal | None = Field(
        None, description="User-facing support score, 0-100. Not a probability."
    )
    assumption_adjusted_score: Decimal | None = Field(
        None, description="Uncapped support after assumption uncertainty; ordering key."
    )
    raw_support_score: Decimal | None = Field(
        None, description="Support before any assumption adjustment."
    )
    support_cap_applied: bool = False
    support_cap_reason_code: str | None = None
    disclaimer: str = SUPPORT_SCORE_DISCLAIMER


class FreshnessOut(BaseModel):
    """Whether the result is still a statement about today's world."""

    model_config = ConfigDict(from_attributes=True)

    freshness_status: str = Field(
        description="unknown | current | stale | superseded"
    )
    stale_reason_code: str | None = None
    freshness_evaluated_at: datetime | None = None
    superseded_by_scenario_id: uuid.UUID | None = None


class IntegrityOut(BaseModel):
    """Whether the SEALED result can still be reproduced.

    A different question from freshness, and never merged with it: a result can
    be stale and perfectly reproducible, or current and non-reproducible. The
    second is far more serious and must not hide behind the word "stale".

    `integrity_state` is what a client should render, and it distinguishes four
    cases the two-word status cannot:

      verified             reproduced from its own pinned inputs
      non_reproducible     a `mismatch` — the replay ran on a result that was
                           contractually reproducible and produced a DIFFERENT
                           identity. The only case that implies a defect.
      unavailable          a pinned artifact is missing, replaced, malformed or
                           fails its identity check. NOTHING was compared, and
                           the next sweep may well verify it.
      legacy_unverifiable  the result predates the frozen-input guarantee, so it
                           never carried what a replay would need. Not a defect
                           and not fixable by waiting — re-run it.

    `integrity_reason_code` is the machine-readable axis; `integrity_state` is
    the human one. A client that counts failures should count
    `non_reproducible`, never `legacy_unverifiable`.

    Hashes are deliberately absent. They are restricted diagnostic metadata,
    they are not proof of ownership, and an ordinary user has no use for them.
    """

    model_config = ConfigDict(from_attributes=True)

    integrity_status: str = Field(
        description="not_checked | verified | mismatch | unavailable"
    )
    integrity_state: str = Field(
        description=(
            "User-visible classification: not_checked | verified | "
            "non_reproducible | unavailable | legacy_unverifiable. A mismatch "
            "is reported as non_reproducible; an unavailable explained by the "
            "result's age is reported as legacy_unverifiable."
        )
    )
    integrity_reason_code: str = "NONE"
    last_integrity_checked_at: datetime | None = None
    integrity_warning: str = Field(
        description=(
            "Plain statement about REPRODUCIBILITY only. It says nothing about "
            "whether the figure is correct, accepted by the CRA, or legally sound."
        )
    )
    execution_policy: str | None = Field(
        default=None,
        description=(
            "How this result's inputs were obtained. `frozen_snapshot_v1` means "
            "it was calculated exclusively from the pinned analysis snapshot. A "
            "`*_legacy` value means it predates that guarantee and may not "
            "reproduce; legacy rows are never relabelled. Null on an entity "
            "that records no policy of its own — a portfolio's is its run's."
        ),
    )


class IntegrityCheckOut(BaseModel):
    """The outcome of one explicit verification request."""

    model_config = ConfigDict(from_attributes=True)

    check_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    integrity_status: str
    integrity_state: str
    integrity_reason_code: str
    integrity_warning: str
    duration_ms: int
    disclaimer: str = (
        "Replay verification checks that a stored result can be reproduced from "
        "its own sealed inputs. It is not a statement of tax correctness."
    )


class MonetaryAmount(BaseModel):
    """A dollar figure that cannot be shown without its meaning.

    `effect_type` is the field that stops different kinds of money being read as
    interchangeable: a tax deferral is a timing benefit, a refundable benefit is
    cash, and a liquidity commitment is money that must be available but is not
    lost. They are never summed into one another.
    """

    amount: Decimal
    effect_type: str = Field(
        description="What KIND of money this is (current_year_tax_reduction, "
                    "tax_deferral, refundable_benefit, liquidity_commitment, ...). "
                    "Amounts of different effect types are never summed."
    )
    calculation_basis: str = Field(
        description="engine_determined | rule_formula_determined | "
                    "scenario_estimate | projection_estimate"
    )
    evidence_status: str = Field(
        description="documented_verified | documented_unverified | "
                    "user_attested | incomplete"
    )
    tax_year: int | None = None
    currency_code: str = CURRENCY_CAD
    horizon_years: int = Field(
        1, description="1 = current year only. A multi-year figure is never "
                       "comparable with a current-year one."
    )
    is_permanent: bool = Field(
        True, description="False for timing benefits such as a deferral."
    )
    support: SupportScore | None = None
    freshness: FreshnessOut | None = None


# ---------------------------------------------------------------------------
# Scenario inputs — typed lever codes and structured assumptions ONLY
# ---------------------------------------------------------------------------
class LeverInput(BaseModel):
    """A lever named by CODE with declared parameters.

    `model_config` forbids extra fields, so a request carrying `field`, `patch`,
    `formula`, or any other mutation shape is rejected by the schema before it
    reaches the domain parser — and, just as importantly, such a field never
    appears in the OpenAPI document as something a caller might supply.
    """

    model_config = ConfigDict(extra="forbid")

    lever_code: str = Field(
        pattern=r"^[A-Z][A-Z0-9_]{2,63}$",
        description="A registered lever code. Never an engine field name.",
        examples=["INCREASE_RRSP_DEDUCTION"],
    )
    parameters: dict[str, Decimal | str] = Field(
        default_factory=dict,
        description="Values for the parameters the registry declares for this "
                    "lever. Undeclared parameters are rejected.",
        examples=[{"amount": "5000.00"}],
    )


class AssumptionInput(BaseModel):
    """A registered assumption code plus exactly one typed value.

    Origin fields are CLOSED vocabularies. The full sets are expressible here
    because this model also renders platform-attached assumptions on output
    surfaces — but on input the domain parser further requires
    `source="user"` / `certainty="user_asserted"`: an API payload states the
    caller's own declaration and cannot claim platform or statutory
    provenance.
    """

    model_config = ConfigDict(extra="forbid")

    assumption_code: str = Field(
        pattern=r"^[A-Z][A-Z0-9_]{2,63}$",
        examples=["CONTRIBUTION_ROOM_AVAILABLE"],
    )
    value_number: Decimal | None = None
    value_text: str | None = None
    value_boolean: bool | None = None
    materiality: Literal["high", "medium", "low"] = "medium"
    source: Literal["user", "platform", "analysis"] = "user"
    certainty: Literal[
        "user_asserted", "platform_default", "derived_from_data", "statutory_known"
    ] = "user_asserted"
    affects_eligibility: bool = False


class ScenarioCreateRequest(BaseModel):
    """Everything a caller may say about a scenario.

    There is deliberately no field here through which an engine input could be
    named or a computation supplied.
    """

    model_config = ConfigDict(extra="forbid")

    analysis_id: uuid.UUID
    levers: list[LeverInput] = Field(min_length=1, max_length=25)
    assumptions: list[AssumptionInput] = Field(default_factory=list, max_length=25)
    label: str | None = Field(
        None, max_length=200,
        description="Descriptive only. Excluded from the scenario hashes, so "
                    "renaming never changes identity or invalidates evidence.",
    )
    note: str | None = Field(None, max_length=2000)


# ---------------------------------------------------------------------------
# Scenario outputs
# ---------------------------------------------------------------------------
class ScenarioSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str | None
    workflow_status: str
    visibility_status: str
    tax_year: int | None
    jurisdiction: str | None
    created_at: datetime
    freshness: FreshnessOut


class AppliedChangeOut(BaseModel):
    """One field the levers moved. Records the FIELD the registry wrote, never
    the user's other financial inputs."""

    model_config = ConfigDict(from_attributes=True)

    apply_order: int
    lever_code: str
    field: str
    old_value: str | None
    new_value: str | None


class OptimizationRequest(BaseModel):
    """Start an optimization run over a completed analysis.

    `resource_capacities` is the existing typed user-constraint contract the
    orchestrator seals into the spec hash: shared-pool capacities the taxpayer
    declares (e.g. FHSA_ROOM carry-forward). Nothing here can name an engine
    input or supply a computation.
    """

    model_config = ConfigDict(extra="forbid")

    analysis_id: uuid.UUID
    resource_capacities: dict[str, Decimal] | None = Field(
        None,
        description="Declared shared-pool capacities by resource code; "
                    "omitted pools use governed defaults where product rules "
                    "define one.",
    )


class OptimizationRunOut(BaseModel):
    """The orchestrator's structured outcome, verbatim."""

    model_config = ConfigDict(from_attributes=True)

    run_id: uuid.UUID
    spec_hash: str
    result_hash: str | None
    workflow_status: str
    replayed: bool
    candidate_count: int
    relationship_count: int
    portfolio_member_count: int
    error_code: str | None
    warnings: list[str]


class ScenarioDetailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str | None
    note: str | None
    workflow_status: str
    visibility_status: str
    tax_year: int | None
    jurisdiction: str | None
    base_analysis_id: uuid.UUID
    objective_code: str | None
    objective_version: str | None
    result_schema_version: str | None
    scenario_spec_hash: str | None
    scenario_result_hash: str | None
    created_at: datetime
    completed_at: datetime | None
    error_code: str | None

    freshness: FreshnessOut
    integrity: IntegrityOut
    levers: list[LeverInput]
    assumptions: list[AssumptionInput]

    # Every figure carries its own meaning; none is a bare number.
    baseline_tax: MonetaryAmount | None = None
    scenario_tax: MonetaryAmount | None = None
    tax_delta: MonetaryAmount | None = None
    objective_delta: MonetaryAmount | None = None
    support: SupportScore | None = None
    applied_changes: list[AppliedChangeOut] = Field(default_factory=list)

    disclaimer: str = (
        "Educational information only. This is not tax advice, not a filing, "
        "and is not submitted to the CRA."
    )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
class ComparisonSideOut(BaseModel):
    scenario_id: uuid.UUID
    label: str | None
    freshness: FreshnessOut
    objective_delta: MonetaryAmount
    tax_delta: MonetaryAmount


class ScenarioComparisonOut(BaseModel):
    """Deltas kept SEPARATE by concept.

    Objective, tax, refund/balance, cash commitment and effect-type differences
    each answer a different question. Folding them into one "difference" would
    make a timing benefit and a permanent reduction look like the same thing.
    """

    left: ComparisonSideOut
    right: ComparisonSideOut
    objective_code: str
    objective_version: str
    comparison_policy_version: str

    objective_difference: MonetaryAmount
    tax_difference: MonetaryAmount
    refund_balance_difference: MonetaryAmount | None = None
    liquidity_commitment_difference: MonetaryAmount | None = None
    effect_type_differences: list[MonetaryAmount] = Field(default_factory=list)

    better: str = Field(description="left | right | equivalent")
    both_current: bool
    stale_notices: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Portfolio (sealed P4 evidence)
# ---------------------------------------------------------------------------
class PortfolioMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    candidate_id: uuid.UUID
    apply_order: int
    incremental_benefit: MonetaryAmount


class PortfolioExclusionOut(BaseModel):
    """Why something is NOT recommended — itself a result, never hidden."""

    model_config = ConfigDict(from_attributes=True)

    candidate_id: uuid.UUID
    membership: str
    reason_code: str
    shared_resource_code: str | None = None
    resolution_options: list[str] = Field(default_factory=list)


class StrategyPortfolioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    objective_code: str | None
    objective_version: str | None
    assembly_method: str
    optimality_claim: str = Field(
        description="Always 'none'. This is a feasible, deterministic, "
                    "engine-evaluated portfolio, not a globally optimal one."
    )
    search_budget_exhausted: bool
    engine_runs_used: int

    # The ONLY displayable total, read from the sealed row.
    portfolio_total_benefit: MonetaryAmount
    objective_value_baseline: MonetaryAmount | None = None
    objective_value_final: MonetaryAmount | None = None

    # Per-concept totals, never folded together.
    total_tax_reduction: MonetaryAmount | None = None
    total_refund_impact: MonetaryAmount | None = None
    total_refundable_benefit: MonetaryAmount | None = None
    total_deferral_amount: MonetaryAmount | None = None
    total_liquidity_commitment: MonetaryAmount | None = None
    total_asset_transfer: MonetaryAmount | None = None
    total_nonrecoverable_expenditure: MonetaryAmount | None = None
    total_implementation_cost: MonetaryAmount | None = None

    members: list[PortfolioMemberOut] = Field(default_factory=list)
    exclusions: list[PortfolioExclusionOut] = Field(default_factory=list)

    optimality_note: str = (
        "This is a feasible, deterministic, engine-evaluated strategy set. It is "
        "not a globally optimal portfolio."
    )


# ---------------------------------------------------------------------------
# Projections — kept apart from current-year totals
# ---------------------------------------------------------------------------

    integrity: IntegrityOut | None = None


class ProjectionAssumptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    assumption_code: str
    value: str | None
    materiality: str
    certainty: str
    source: str


class ProjectionUncertaintyOut(BaseModel):
    """A projection's uncertainty is stated, not implied by a single number."""

    low_estimate: MonetaryAmount | None = None
    high_estimate: MonetaryAmount | None = None
    sensitivity_note: str | None = None
    support: SupportScore | None = None


class MultiYearProjectionOut(BaseModel):
    """Deliberately NOT part of any current-year total.

    A multi-year projection rests on assumptions that a current-year engine
    result does not. Merging the two would let assumption-laden figures inherit
    the credibility of a deterministic calculation.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    horizon_year: int = Field(description="The final year of the horizon.")
    horizon_years: int = Field(description="Length of the horizon in years.")
    methodology_version: str
    projected_total: MonetaryAmount
    per_year: list[MonetaryAmount] = Field(default_factory=list)
    assumptions: list[ProjectionAssumptionOut] = Field(default_factory=list)
    uncertainty: ProjectionUncertaintyOut

    separation_note: str = (
        "Projected figures are estimates over a multi-year horizon and are NOT "
        "included in any current-year total."
    )


class ProjectionResponse(BaseModel):
    """Always an object with a STATUS, never a bare null.

    A null would leave the caller unable to tell "no rule authorized a
    projection" from "the feature is off" from "something went wrong". Each of
    those is a different fact and each gets its own status.
    """

    status: str = Field(
        description="generated | not_generated_no_eligible_candidates | "
                    "not_generated_missing_assumptions | feature_not_enabled"
    )
    reason: str | None = Field(
        None, description="Why nothing was generated, when nothing was."
    )
    missing_assumption_codes: list[str] = Field(default_factory=list)
    projection: MultiYearProjectionOut | None = None


__all__ = [
    "CURRENCY_CAD",
    "OptimizationRequest",
    "OptimizationRunOut",
    "ProjectionResponse",
    "AppliedChangeOut",
    "AssumptionInput",
    "ComparisonSideOut",
    "FreshnessOut",
    "LeverInput",
    "MonetaryAmount",
    "MultiYearProjectionOut",
    "PortfolioExclusionOut",
    "PortfolioMemberOut",
    "ProjectionAssumptionOut",
    "ProjectionUncertaintyOut",
    "ScenarioComparisonOut",
    "ScenarioCreateRequest",
    "ScenarioDetailOut",
    "ScenarioSummaryOut",
    "StrategyPortfolioOut",
    "SupportScore",
]
