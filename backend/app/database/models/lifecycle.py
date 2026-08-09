"""Account deletion lifecycle tables (Entry 11B1).

Identifiers, closed state codes and timestamps. No email, no financial value,
no document reference, no exception text — a lifecycle row records that an
account is being deleted and how far that has got.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk


class AccountLifecycle(Base):
    """One row per account undergoing privacy deletion.

    The absence of a row means the account is ACTIVE — which is why there is no
    `ACTIVE` state value and no backfill.
    """

    __tablename__ = "account_lifecycle"
    __table_args__ = {
        "schema": "identity",
        "comment": (
            "One row per account undergoing privacy deletion. The absence of a "
            "row means the account is active. requested_at is the authoritative "
            "deletion cutoff, taken from the database clock."
        ),
    }

    #: The DURABLE SUBJECT of the deletion, and deliberately NOT a foreign key.
    #:
    #: PD-9: this record has to outlive the account row it describes, so a purge
    #: can finish and a restored backup can be told what to re-delete. A model
    #: that still declared the relationship would tell a reader the cascade
    #: exists — which is exactly the belief that made the account impossible to
    #: remove. Creation-time integrity is enforced by
    #: `trg_account_lifecycle_subject_exists`; see
    #: db/sql/44_pd9_durable_deletion_ledger.sql.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
    )
    state: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment=(
            "Closed lifecycle state. ACTIVE is deliberately not a value: it is "
            "the absence of the row."
        ),
    )
    requested_at: Mapped[datetime] = created_at_col()
    state_changed_at: Mapped[datetime] = created_at_col()

    claimed_by: Mapped[str | None] = mapped_column(Text)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_failure_code: Mapped[str | None] = mapped_column(
        Text,
        comment=(
            "Closed failure code, never an exception message. A lifecycle row "
            "must not become a place raw errors accumulate."
        ),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class AccountLifecycleEvent(Base):
    """Append-only lifecycle transition log.

    Deliberately carries no foreign key to the account: this record must OUTLIVE
    the account it describes, so a restored backup can be told which accounts
    were deleted after the snapshot was taken.
    """

    __tablename__ = "account_lifecycle_event"
    __table_args__ = {
        "schema": "identity",
        "comment": (
            "Append-only lifecycle transitions. Closed codes and identifiers "
            "only — never an email address, a financial value, a document "
            "reference or an exception message."
        ),
    }

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_code: Mapped[str] = mapped_column(Text, nullable=False)
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


__all__ = ["AccountLifecycle", "AccountLifecycleEvent"]


class AccountLifecyclePhase(Base):
    """Durable per-phase progress for one account deletion (Entry 11B5).

    `account_lifecycle.state` is one scalar, and privacy deletion is several
    phases. A worker that crashed between purging expenses and purging income
    would restart unable to say which half it had done — and the two honest
    options without this table are to redo everything or to guess.

    The account stays in `PURGING`; a row here says which phase is pending,
    running, complete or owed a retry.
    """

    __tablename__ = "account_lifecycle_phase"
    __table_args__ = {
        "schema": "identity",
        "comment": (
            "Durable per-phase progress for one account deletion. The account "
            "stays in PURGING; this says which phase is pending, running, done "
            "or owed a retry."
        ),
    }

    #: Subject of the deletion. The foreign key points at `account_lifecycle`,
    #: NOT at `identity.user_account` — PD-9. A purge phase ends by removing the
    #: account row and the phases after it still have work to do.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True)
    phase: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_failure_code: Mapped[str | None] = mapped_column(
        Text,
        comment=(
            "Closed failure code, never an exception message — the same rule "
            "the lifecycle row itself follows."
        ),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
