"""Tax Decision Journal — the product-facing contract.

WHAT THIS CONTRACT PROMISES. A durable, append-only record of what the user
decided and what they reported doing, with the sealed artifact identities that
say what they were looking at when they decided.

WHAT IT KEEPS APART, because the product's honesty depends on it: a SIMULATION
is not a decision; a decision to PROCEED is not an action; a user's report
that they acted is not system verification; and governed evidence readiness
beside the record is supporting context, never proof. Field names carry those
distinctions so no client can collapse them by accident.

ITS OWN VERSION. `schema_version` belongs to this contract alone. The persisted
event rows carry their own `event_schema_version`, because a stored row must
outlive the response shape.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.services.ioe.journal.domain import DECISION_JOURNAL_SCHEMA_VERSION

__all__ = [
    "DECISION_JOURNAL_SCHEMA_VERSION",
    "CreateDecisionJournalRequest",
    "DecisionJournalDetailOut",
    "DecisionJournalEventOut",
    "DecisionJournalSummaryOut",
    "EvidenceContextOut",
    "EvidenceReadinessRowOut",
    "RecordDecisionRequest",
    "ReportActionRequest",
    "ScenarioReferenceOut",
]


# ------------------------------------------------------------------ requests
class CreateDecisionJournalRequest(BaseModel):
    """Open a decision thread from a scenario the caller owns.

    `request_id` is the idempotency identity: a retried POST with the same id
    returns the thread the first attempt created.
    """

    scenario_id: uuid.UUID
    request_id: uuid.UUID
    subject_opportunity_code: str | None = Field(
        None, max_length=120,
        description="Optional governed opportunity code this decision is about.")


class RecordDecisionRequest(BaseModel):
    """Declare intent. PROCEED means the user decided to proceed — it is not,
    and is never treated as, evidence that anything happened."""

    decision: str = Field(
        pattern="^(CONSIDERING|PROCEED|DEFER|DECLINE)$",
        description="The closed decision vocabulary.")
    request_id: uuid.UUID


class ReportActionRequest(BaseModel):
    """The user reports having acted. Self-report, recorded verbatim."""

    request_id: uuid.UUID
    action_date: date | None = Field(
        None,
        description=(
            "The date the user says the action happened, if they choose to "
            "say. Validated for sanity; never rewrites the server's own "
            "recorded_at."))


# ----------------------------------------------------------------- responses
class ScenarioReferenceOut(BaseModel):
    """Pinned identity of what informed the decision. References, not copies —
    the sealed scenario remains the authority for its own content."""

    model_config = ConfigDict(from_attributes=True)

    scenario_id: uuid.UUID
    scenario_result_hash: str | None
    scenario_result_schema_version: str | None
    comparison_hash: str | None
    comparison_schema_version: str | None


class DecisionJournalEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence: int
    event_type: str
    decision: str | None
    user_reported_action_date: date | None
    event_schema_version: str
    recorded_at: datetime


class EvidenceReadinessRowOut(BaseModel):
    """One governed requirement resolved to a readiness verdict. Identity is
    the document TYPE — never a document id, bucket, key, or hash."""

    document_type_code: str
    necessity: str
    readiness: str


class EvidenceContextOut(BaseModel):
    """Governed evidence semantics beside the journal.

    `sealed_readiness` is what the sealed scenario recorded and will never
    change. `current_observed_readiness` is the same certified primitive over
    the documents held NOW — labeled current because it moves. Neither is
    verification that the reported action occurred.
    """

    status: str
    reason_code: str | None
    sealed_readiness: list[EvidenceReadinessRowOut]
    current_observed_readiness: list[EvidenceReadinessRowOut]
    sealed_deadline_codes: list[str]


class DecisionJournalSummaryOut(BaseModel):
    """One thread as the list renders it: identity, linkage, projection."""

    id: uuid.UUID
    schema_version: str
    subject_opportunity_code: str | None
    scenario_reference: ScenarioReferenceOut

    current_decision: str
    decision_recorded_at: datetime
    execution_state: str
    last_reported_action_date: date | None

    event_count: int
    created_at: datetime
    last_event_at: datetime


class DecisionJournalDetailOut(DecisionJournalSummaryOut):
    """The full thread: everything the summary carries, plus the history that
    produced it and the governed evidence context."""

    events: list[DecisionJournalEventOut]
    evidence_context: EvidenceContextOut
