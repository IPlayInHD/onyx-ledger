"""Opportunity Lifecycle — the product-facing contract.

WHAT IT PROMISES. For each governed opportunity, its current state on seven
independent axes, the governed deadline and exact days remaining, structured
attention flags, and the user's own decision where a Journal thread names it.

WHAT IT REFUSES. No decay score, no priority score, no "money missed", no
COMPLETE, no claim that a reported action occurred. Expired opportunities stay
visible with `timing = EXPIRED` rather than disappearing, because a customer
needs to tell an expired window from one that never existed.

ITS OWN VERSION. `schema_version` belongs to this contract alone — not the
ScenarioResult protocol, not Before-You-Act, not Assurance, not the Journal.
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.ioe import SupportScore
from app.services.ioe.lifecycle.domain import (
    OPPORTUNITY_LIFECYCLE_CONTRACT_VERSION,
)

#: Exposed under the product name; the domain constant is the authority.
OPPORTUNITY_LIFECYCLE_SCHEMA_VERSION = OPPORTUNITY_LIFECYCLE_CONTRACT_VERSION


class LifecycleDeadlineOut(BaseModel):
    """The governed deadline, verbatim. Never fabricated — its absence is
    reported as `deadline: null` with `timing: NO_DEADLINE`."""

    model_config = ConfigDict(from_attributes=True)

    deadline_code: str
    deadline_date: str
    is_hard: bool
    days_remaining: int
    urgency: str


class AttentionFlagsOut(BaseModel):
    """Structured flags instead of a score — each one a governed fact a client
    can render, explain, or ignore independently."""

    model_config = ConfigDict(from_attributes=True)

    needs_decision: bool
    needs_evidence: bool
    deadline_approaching: bool
    deadline_urgent: bool
    expired: bool
    blocked: bool
    stale: bool


class JournalLinkOut(BaseModel):
    """Which Journal thread the reported decision came from.

    `thread_count` is exposed rather than hidden: a user who decided twice has
    two threads, and showing only the latest without saying so would make the
    others look as if they never happened. The Journal remains the history
    authority for all of them.
    """

    model_config = ConfigDict(from_attributes=True)

    journal_id: uuid.UUID
    thread_count: int


class OpportunityLifecycleItemOut(BaseModel):
    """One opportunity, on seven axes that never collapse into one another."""

    model_config = ConfigDict(from_attributes=True)

    opportunity_code: str
    source_id: str

    availability: str
    decision: str
    execution: str = Field(
        description="NOT_REPORTED or USER_REPORTED. Never system verification.")
    evidence: str
    timing: str
    freshness: str
    stale_reason_codes: list[str]
    integrity: str
    integrity_reason_code: str

    deadline: LifecycleDeadlineOut | None
    days_remaining: int | None

    actionability: str
    reason_codes: list[str]

    attention: AttentionFlagsOut
    journal: JournalLinkOut | None
    last_reported_action_date: str | None

    standalone_potential: str | None
    incremental_portfolio_benefit: str | None
    candidate_rank: int | None
    support: SupportScore
    assumption_dependent: bool


class LifecycleSummaryOut(BaseModel):
    """Transparent counts only."""

    model_config = ConfigDict(from_attributes=True)

    opportunity_count: int
    by_availability: dict[str, int]
    by_decision: dict[str, int]
    by_execution: dict[str, int]
    by_timing: dict[str, int]
    by_actionability: dict[str, int]
    needing_attention: int
    unlinked_thread_count: int


class OpportunityLifecycleOut(BaseModel):
    """The whole contract.

    `opportunity_authority` carries the OPPORTUNITY family's standing, so an
    empty `opportunities` list is never read as "you have no opportunities"
    when the truth is that no governing run exists.
    """

    model_config = ConfigDict(from_attributes=True)

    schema_version: str
    tax_year: int
    as_of: str
    graph_hash: str

    opportunity_authority: str
    opportunity_authority_reason: str

    opportunities: list[OpportunityLifecycleItemOut]
    attention_order: list[str]
    summary: LifecycleSummaryOut
