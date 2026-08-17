"""Retention / "What Changed?" — the product-facing contract.

WHAT IT PROMISES. Every material transition in the user's governed tax state
since the state they last explicitly acknowledged, each one categorised, graded
and stably identified, with the transitions that produced it.

WHAT IT REFUSES. No retention score, no priority number, no "you missed $X",
and no change manufactured from the clock: a countdown moving inside its band,
a request date advancing, or a list reordering produce nothing at all.

TWO VERSIONS, DELIBERATELY. `schema_version` here is the READ CONTRACT's
version. The acknowledged snapshot carries its own, separate version for the
STORED BYTES (`retention/snapshot.py`). They move for different reasons and
conflating them would make a storage change look like an API change.
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.services.ioe.retention.domain import RETENTION_CHANGES_CONTRACT_VERSION

#: Exposed under the product name; the domain constant is the authority.
RETENTION_CHANGES_SCHEMA_VERSION = RETENTION_CHANGES_CONTRACT_VERSION


class AcknowledgeChangesRequest(BaseModel):
    """Acknowledge the exact state the client reviewed.

    Both tokens are required rather than convenient. `snapshot_hash` says WHICH
    STATE was reviewed; `baseline_checkpoint_id` says WHICH BASELINE it was
    reviewed against. Together they make a stale acknowledgement impossible to
    express by accident, which matters because the failure mode is silent:
    recording that a user reviewed changes they never saw.
    """

    snapshot_hash: str = Field(
        min_length=64, max_length=64,
        description=(
            "The `current_snapshot_hash` from the changes response being "
            "acknowledged. Refused with 409 if current state has moved since."
        ),
    )
    baseline_checkpoint_id: uuid.UUID | None = Field(
        None,
        description=(
            "The `baseline_checkpoint.id` the client compared against, or null "
            "when establishing the first baseline. Refused with 409 if another "
            "acknowledgement superseded it first."
        ),
    )
    request_id: uuid.UUID = Field(
        description="Idempotency key. A retry returns the same checkpoint.")


class FieldTransitionOut(BaseModel):
    """One field's before and after. Structured, never a rendered diff."""

    model_config = ConfigDict(from_attributes=True)

    field: str
    before: str | None
    after: str | None


class MaterialChangeOut(BaseModel):
    """One semantic event about one governed subject."""

    model_config = ConfigDict(from_attributes=True)

    change_id: str = Field(
        description=(
            "Deterministic identity, bound to both snapshot hashes and the "
            "transition. Stable across repeated reads of the same two states, "
            "so a future delivery path can deduplicate on it. Never a row id."
        ))
    kind: str
    category: str
    severity: str
    subject: str = Field(
        description="The governed opportunity code, or the Assurance family.")
    transitions: list[FieldTransitionOut]


class ChangeSummaryOut(BaseModel):
    """Transparent counts only — no composite score, and no money."""

    model_config = ConfigDict(from_attributes=True)

    total: int
    by_kind: dict[str, int]
    by_category: dict[str, int]
    by_severity: dict[str, int]
    opportunities_added: int
    opportunities_removed: int
    newly_urgent: int
    newly_expired: int
    newly_blocked: int
    decision_changes: int
    execution_reports: int
    evidence_improvements: int
    evidence_regressions: int
    freshness_changes: int
    integrity_changes: int


class BaselineCheckpointOut(BaseModel):
    """Which acknowledged state the comparison was measured from."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    snapshot_hash: str
    snapshot_schema_version: str
    evaluated_as_of: str
    acknowledged_at: str


class RetentionChangesOut(BaseModel):
    """The whole contract.

    `baseline_status` is NO_BASELINE on first use, and `changes` is then empty
    by design: with nothing acknowledged there is no prior authoritative state,
    and describing the user's existing position as a list of arrivals would
    report events that never happened.
    """

    model_config = ConfigDict(from_attributes=True)

    schema_version: str
    tax_year: int
    as_of: str

    baseline_status: str
    baseline_checkpoint: BaselineCheckpointOut | None
    current_snapshot_hash: str = Field(
        description=(
            "Echo this to acknowledge the state described here. It is the "
            "optimistic-concurrency token."
        ))

    changes: list[MaterialChangeOut]
    summary: ChangeSummaryOut


class RetentionCheckpointOut(BaseModel):
    """A newly recorded acknowledgement."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tax_year: int
    snapshot_hash: str
    snapshot_schema_version: str
    evaluated_as_of: str
    acknowledged_at: str
    supersedes_checkpoint_id: uuid.UUID | None
