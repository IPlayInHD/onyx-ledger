"""AI Explanation contract v1 — the frozen renderer boundary.

WHAT THIS CONTRACT PROMISES. `ExplanationInputV1` is everything a renderer —
model-backed or deterministic — is permitted to know, and `ExplanationOutputV1`
is everything it is permitted to say. Every value in the input was produced by
the governed authority that owns it and is echoed verbatim; nothing here
computes, decides, or re-derives.

WHAT IT DELIBERATELY REFUSES. The renderer holds NO authority: no tax amount,
eligibility verdict, deadline, evidence requirement, citation, assumption,
resource constraint, support score, freshness state or integrity state
originates here. An output that contradicts its input is rejected by the
deterministic validators, never repaired.

ITS OWN VERSIONS. `EXPLANATION_INPUT_SCHEMA_VERSION` and
`EXPLANATION_OUTPUT_SCHEMA_VERSION` belong to this contract alone. They move
when THIS boundary's shape moves — not when the scenario-result protocol,
Assurance, Before-You-Act, or any other product contract does. Both appear in
every API envelope so a client can tell them apart.

EMBEDDING, NOT COPYING. The typed subject payloads embed the existing
certified read contracts (`ScenarioDetailOut`, `StrategyPortfolioOut`,
`BeforeYouActComparisonOut`, `RetentionChangesOut`, `EvidenceContextOut`,
`OpportunityAssuranceOut`) unchanged, so there is no second data model to
drift from the first.
"""
from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas import AnalysisOut, LineItemOut
from app.schemas.assurance import OpportunityAssuranceOut
from app.schemas.before_you_act import BeforeYouActComparisonOut
from app.schemas.decision_journal import EvidenceContextOut
from app.schemas.ioe import (
    AssumptionInput,
    FreshnessOut,
    IntegrityOut,
    ScenarioDetailOut,
    StrategyPortfolioOut,
)
from app.schemas.retention import RetentionChangesOut

EXPLANATION_INPUT_SCHEMA_VERSION = "1.0.0"
EXPLANATION_OUTPUT_SCHEMA_VERSION = "1.0.0"

#: The closed set of things an explanation can be about.
EXPLANATION_TYPES = (
    "TAX_POSITION",
    "OPPORTUNITY",
    "PORTFOLIO",
    "SCENARIO",
    "COMPARISON",
    "EVIDENCE_READINESS",
    "WHAT_CHANGED",
)

ExplanationType = Literal[
    "TAX_POSITION",
    "OPPORTUNITY",
    "PORTFOLIO",
    "SCENARIO",
    "COMPARISON",
    "EVIDENCE_READINESS",
    "WHAT_CHANGED",
]


class SubjectBinding(BaseModel):
    """The sealed identity the explanation is bound to. INTERNAL ONLY.

    These values exist for validator binding and future cache identity. They
    are never part of `ExplanationOutputV1`, never rendered to a customer, and
    the leakage validator rejects any output that contains one of them.
    """

    model_config = ConfigDict(extra="forbid")

    subject_type: str
    subject_id: str
    snapshot_hash: str | None = None
    spec_hash: str | None = None
    result_hash: str | None = None
    graph_hash: str | None = None
    comparison_hash: str | None = None

    def identity_values(self) -> tuple[str, ...]:
        """Every non-null identity string, for the leakage validator."""
        return tuple(
            v for v in (
                self.snapshot_hash, self.spec_hash, self.result_hash,
                self.graph_hash, self.comparison_hash,
            ) if v
        )


class VersionManifestV1(BaseModel):
    """Version identity echoed from the authoritative artifact — never
    fabricated.

    `reference_data_binding` states the truth about reference-data identity
    rather than pretending: sealed IOE artifacts carry it INSIDE their spec
    hash (`sealed_in_spec_hash`) without exposing the value separately, and a
    bare analysis does not record it at all (`not_recorded_on_artifact`).
    Attributing a historical artifact to today's runtime dataset would be a
    false statement, so no code path does it.
    """

    model_config = ConfigDict(extra="forbid")

    engine_version: str | None = None
    reference_data_version: str | None = None
    reference_data_binding: Literal[
        "sealed_in_spec_hash", "not_recorded_on_artifact"
    ] = "not_recorded_on_artifact"
    result_schema_version: str | None = None
    objective_code: str | None = None
    objective_version: str | None = None
    product_contract_version: str | None = None


class CitationRecordV1(BaseModel):
    """One governed citation the renderer may reference — the ONLY kind."""

    model_config = ConfigDict(extra="forbid")

    citation_id: str
    citation_text: str
    title: str | None = None
    source_url: str | None = None


class ValueRecordV1(BaseModel):
    """One number or date the renderer is permitted to mention.

    `accepted_forms` are deterministic FORMATTING variants of the same value
    (comma grouping, currency sign, percent rendering) — never arithmetic.
    An empty `field_scope` means the value may appear in any prose field; a
    non-empty one confines it to the named output fields, which is how a
    liquidity commitment is kept out of a sentence about tax reduction.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    rendered: str
    accepted_forms: list[str] = Field(default_factory=list)
    kind: Literal["money", "rate", "count", "date", "year", "text_number"]
    currency_code: str | None = None
    effect_type: str | None = None
    field_scope: list[str] = Field(default_factory=list)


class NextStepV1(BaseModel):
    """A next step that MUST reference a supplied structured action.

    `action_ref` is a rules-authored `action_code`, or `resolution:<option>`
    for a supplied portfolio-exclusion resolution option. Free-text steps
    cannot become structured next steps.
    """

    model_config = ConfigDict(extra="forbid")

    action_ref: str
    description: str


class AssumptionNoteV1(BaseModel):
    """One assumption, rendered with its TRUE origin preserved."""

    model_config = ConfigDict(extra="forbid")

    assumption_code: str
    source: str
    certainty: str
    note: str


class DisplayContextV1(BaseModel):
    """Display strings, separated by trust.

    `labels` are platform-derived and safe. `untrusted` holds user-authored
    display text (scenario labels/notes, source names, asset labels): DATA to
    delimit, never instructions to follow, and nothing a renderer should echo.
    """

    model_config = ConfigDict(extra="forbid")

    labels: dict[str, str] = Field(default_factory=dict)
    untrusted: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------- subject sections --
class ReconciliationRowV1(BaseModel):
    """One tie-out check, verbatim from `analysis.reconciliation_check`."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    check_code: str
    status: str
    label: str
    detail: str | None = None


class TaxPositionSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis: AnalysisOut
    line_items: list[LineItemOut] = Field(default_factory=list)
    reconciliation: list[ReconciliationRowV1] = Field(default_factory=list)


class OpportunityActionV1(BaseModel):
    """A rules-authored action, verbatim. Never invented downstream."""

    model_config = ConfigDict(extra="forbid")

    action_code: str
    description: str
    effort_rating: int
    cost_type: str | None = None
    cost_amount: str | None = None
    deadline_code: str | None = None


class OpportunityDocumentV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type_code: str
    necessity: str
    note: str | None = None


class OpportunityDeadlineV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deadline_code: str
    deadline_date: str | None = None
    description: str | None = None
    is_hard: bool = True


class OpportunityContractSection(BaseModel):
    """Rule-authored contract data for the opportunity — published rule
    versions only, four-eyes governed. `authored_explanation` is the version's
    own published explanation text, not model output."""

    model_config = ConfigDict(extra="forbid")

    rule_description: str | None = None
    authored_explanation: str | None = None
    actions: list[OpportunityActionV1] = Field(default_factory=list)
    documents: list[OpportunityDocumentV1] = Field(default_factory=list)
    deadlines: list[OpportunityDeadlineV1] = Field(default_factory=list)
    eligibility_basis_codes: list[str] = Field(default_factory=list)


class OpportunitySection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    standing: OpportunityAssuranceOut
    contract: OpportunityContractSection | None = None


#: explanation_type → the one input payload field it requires.
_PAYLOAD_FIELD_BY_TYPE: dict[str, str] = {
    "TAX_POSITION": "tax_position",
    "OPPORTUNITY": "opportunity",
    "PORTFOLIO": "portfolio",
    "SCENARIO": "scenario",
    "COMPARISON": "comparison",
    "EVIDENCE_READINESS": "evidence",
    "WHAT_CHANGED": "changes",
}

_PAYLOAD_FIELDS = tuple(_PAYLOAD_FIELD_BY_TYPE.values())


class ExplanationInputV1(BaseModel):
    """Everything the renderer may know. Assembled read-only from governed
    services; nothing in it was computed for the explanation."""

    model_config = ConfigDict(extra="forbid")

    explanation_input_version: Literal["1.0.0"] = "1.0.0"
    explanation_type: ExplanationType
    tax_year: int
    jurisdiction: str | None = None
    analysis_id: uuid.UUID | None = None
    as_of: str

    subject_binding: SubjectBinding
    version_manifest: VersionManifestV1

    tax_position: TaxPositionSection | None = None
    opportunity: OpportunitySection | None = None
    portfolio: StrategyPortfolioOut | None = None
    scenario: ScenarioDetailOut | None = None
    comparison: BeforeYouActComparisonOut | None = None
    changes: RetentionChangesOut | None = None
    evidence: EvidenceContextOut | None = None

    assumptions: list[AssumptionInput] = Field(default_factory=list)
    citations: list[CitationRecordV1] = Field(default_factory=list)
    value_table: dict[str, ValueRecordV1] = Field(default_factory=dict)
    freshness: FreshnessOut
    integrity: IntegrityOut
    display_context: DisplayContextV1 = Field(default_factory=DisplayContextV1)

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> ExplanationInputV1:
        required = _PAYLOAD_FIELD_BY_TYPE[self.explanation_type]
        populated = [f for f in _PAYLOAD_FIELDS if getattr(self, f) is not None]
        if populated != [required]:
            raise ValueError(
                f"explanation_type {self.explanation_type} requires exactly the "
                f"'{required}' payload; got {populated or 'none'}"
            )
        return self


class ExplanationOutputV1(BaseModel):
    """Everything the renderer may say.

    Absence is meaningful: an optional field left null says "not applicable to
    this explanation", never "fill it with something plausible". `citation_refs`
    must be a subset of the input's citation ids and `value_refs_used` of its
    value-table keys — both enforced mechanically by the validators, not by
    convention.
    """

    model_config = ConfigDict(extra="forbid")

    explanation_output_version: Literal["1.0.0"] = "1.0.0"
    explanation_type: ExplanationType

    summary: str = Field(min_length=1)
    why_this_result: str | None = None
    why_this_applies: str | None = None
    estimated_effect_explanation: str | None = None
    what_you_can_do: list[NextStepV1] = Field(default_factory=list)
    required_cash_or_resource: str | None = None
    what_you_need: list[str] = Field(default_factory=list)
    important_assumptions: list[AssumptionNoteV1] = Field(default_factory=list)
    constraints_and_exclusions: str | None = None
    what_changed: str | None = None
    freshness_notice: str | None = None
    limitations: str = Field(min_length=1)
    citation_refs: list[str] = Field(default_factory=list)
    value_refs_used: list[str] = Field(default_factory=list)


class ExplanationEnvelopeOut(BaseModel):
    """The API response: the validated explanation plus non-sensitive
    metadata. No internal hashes, no subject binding."""

    model_config = ConfigDict(extra="forbid")

    explanation: ExplanationOutputV1
    explanation_type: ExplanationType
    renderer_mode: Literal["model", "fallback"]
    explanation_input_version: str = EXPLANATION_INPUT_SCHEMA_VERSION
    explanation_output_version: str = EXPLANATION_OUTPUT_SCHEMA_VERSION
    as_of: str
    disclaimer: str = (
        "Educational information only. This is not tax advice, not a filing, "
        "and is not submitted to the CRA."
    )


__all__ = [
    "EXPLANATION_INPUT_SCHEMA_VERSION",
    "EXPLANATION_OUTPUT_SCHEMA_VERSION",
    "EXPLANATION_TYPES",
    "AssumptionNoteV1",
    "CitationRecordV1",
    "DisplayContextV1",
    "ExplanationEnvelopeOut",
    "ExplanationInputV1",
    "ExplanationOutputV1",
    "ExplanationType",
    "NextStepV1",
    "OpportunityActionV1",
    "OpportunityContractSection",
    "OpportunityDeadlineV1",
    "OpportunityDocumentV1",
    "OpportunitySection",
    "ReconciliationRowV1",
    "SubjectBinding",
    "TaxPositionSection",
    "ValueRecordV1",
    "VersionManifestV1",
]
