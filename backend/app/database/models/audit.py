from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, uuid_pk


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = {"schema": "audit"}

    # RANGE-partitioned by created_at -> composite PK (id, created_at).
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("ref.uuid_generate_v7()"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True,
        server_default=text("now()"),
    )
    actor_type: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String, nullable=False)
    entity_schema: Mapped[str] = mapped_column(String, nullable=False)
    entity_table: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String)
    previous_value: Mapped[dict | None] = mapped_column(JSONB)
    new_value: Mapped[dict | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(INET)
    # Entry 11B6 (PD-15). WHO the action concerned, as a key rather than an id;
    # `actor_id` is a different question and stays. Resolved through
    # identity.account_subject while the account lives. Written by the
    # audit.log_change trigger, not by the ORM — declared here because the
    # schema-drift gate refuses to govern a column the models cannot see.
    subject_key: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class ConsentLog(Base):
    __tablename__ = "consent_log"
    __table_args__ = {"schema": "audit"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="SET NULL")
    )
    consent_type: Mapped[str] = mapped_column(String, nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    version: Mapped[str | None] = mapped_column(String)
    ip_address: Mapped[str | None] = mapped_column(INET)
    subject_key: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()


class DataExportRequest(Base):
    __tablename__ = "data_export_request"
    __table_args__ = {"schema": "audit"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String, default="requested")
    object_key: Mapped[str | None] = mapped_column(String)
    requested_at: Mapped[datetime] = created_at_col()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DataDeletionRequest(Base):
    __tablename__ = "data_deletion_request"
    __table_args__ = {"schema": "audit"}

    id: Mapped[uuid.UUID] = uuid_pk()
    # Durable subject, not a foreign key (PD-9): a deletion request must
    # outlive the account it describes. This table is currently unused —
    # identity.account_lifecycle is the live ledger.
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String, default="requested")
    reason: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = created_at_col()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SecurityEvent(Base):
    __tablename__ = "security_event"
    __table_args__ = {"schema": "audit"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, default="info")
    detail: Mapped[dict | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(INET)
    subject_key: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()
