"""Tax Assurance Map — the product-facing read contract.

WHAT THIS CONTRACT PROMISES. The current standing of a user's tax position for
one tax year, derived deterministically from governed state: which
opportunities exist, what stands between the user and acting on each one, what
is urgent, and which parts of the position are ready, unavailable or not
applicable.

WHAT IT DELIBERATELY DOES NOT PROMISE. No correctness guarantee, no CRA
acceptance probability, no "assurance score", no recommendation of what to do
first beyond a documented, deterministic review order. It reports standing;
deciding is the customer's, and explaining is a later AI layer's — one that
renders THIS structure and is never required for its correctness.

ITS OWN VERSION. `schema_version` belongs to this contract alone — not the
scenario-result protocol, not the Before-You-Act contract. A client renders
this payload and cares only when this payload's shape moves.

NUMBERS ARE STRINGS. Money and rate values arrive exactly as the governed
canonicalizer rendered them and pass through verbatim, except the support
block, which reuses the repository's existing `SupportScore` type so the
support disclaimer travels with the value on every surface that shows one.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.ioe import SupportScore

#: This product contract's version.
TAX_ASSURANCE_SCHEMA_VERSION = "1.0.0"


class DeadlineOut(BaseModel):
    """A governed deadline. `days_remaining` and `urgency` are evaluated
    against the response's `as_of` date, which is echoed at the top level so
    the derivation is reproducible."""

    model_config = ConfigDict(from_attributes=True)

    deadline_code: str
    deadline_date: str
    is_hard: bool
    days_remaining: int
    urgency: str


class EvidenceRequirementOut(BaseModel):
    """One governed document requirement. Identity is the document TYPE —
    never a document id, bucket, object key or content hash."""

    model_config = ConfigDict(from_attributes=True)

    document_type_code: str
    necessity: str
    readiness: str


class OpportunityAssuranceOut(BaseModel):
    """One opportunity's standing. Every figure was produced by the governed
    authority that owns it; nothing in this payload was computed at read time
    except day counts against `as_of`."""

    model_config = ConfigDict(from_attributes=True)

    opportunity_code: str
    source_id: str
    eligibility_status: str
    status: str
    action: str
    blocked_reason_code: str | None
    review_reason_codes: list[str]
    evidence_readiness: str
    evidence_requirements: list[EvidenceRequirementOut]
    deadline: DeadlineOut | None
    deadline_count: int
    urgency: str
    support: SupportScore
    assumption_dependent: bool
    standalone_potential: str | None = Field(
        None, description="Governed impact figure, sealed by the optimizer. "
        "Not recomputed here.")
    incremental_portfolio_benefit: str | None
    candidate_rank: int | None
    freshness: str
    stale_reason_codes: list[str]
    integrity: str
    integrity_reason_code: str


class FamilyAssuranceOut(BaseModel):
    """One family's standing. UNAVAILABLE with zero items and READY with zero
    items are different claims, and this row is what keeps them different."""

    model_config = ConfigDict(from_attributes=True)

    family: str
    status: str
    reason_code: str
    item_count: int


class AssuranceSummaryOut(BaseModel):
    """Multi-dimensional counts. Deliberately not a single score — no governed
    model defines what such a number would measure."""

    model_config = ConfigDict(from_attributes=True)

    opportunity_count: int
    opportunities_by_status: dict[str, int]
    opportunities_by_action: dict[str, int]
    opportunities_by_urgency: dict[str, int]
    assumption_dependent_count: int
    upcoming_deadline_count: int
    families_by_status: dict[str, int]


class TaxAssuranceOut(BaseModel):
    """The whole contract.

    `attention` orders opportunity `source_id`s in documented review-next
    order: urgency band, then closable-gap band, then the optimizer's own
    sealed rank, then identity. It is a presentation order, not an optimum.
    """

    model_config = ConfigDict(from_attributes=True)

    schema_version: str
    view: str
    tax_year: int
    as_of: str
    graph_hash: str

    families: list[FamilyAssuranceOut]
    opportunities: list[OpportunityAssuranceOut]
    attention: list[str]
    assumption_codes: list[str]
    summary: AssuranceSummaryOut
