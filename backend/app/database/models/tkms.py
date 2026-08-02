"""SQLAlchemy models for the tkms schema — the TKMS workflow + provenance layer.

Mirrors backend/db/sql/19_tkms.sql. In-flight artifacts (import jobs, parse
results, staged extracted rules, validation/change reports, rollback records,
dead-letters) live here; the Tax Knowledge Base (tax_kb + rules) only ever holds
real rule versions. CHECK constraints, partial-unique/idempotency indexes, FKs,
and triggers remain hand-written SQL (not modelled by autogenerate).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk


class ImportJob(Base):
    __tablename__ = "import_job"
    __table_args__ = {"schema": "tkms"}

    id: Mapped[uuid.UUID] = uuid_pk()
    source_org: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    checksum: Mapped[str | None] = mapped_column(Text)
    jurisdiction_code: Mapped[str | None] = mapped_column(Text)
    province_code: Mapped[str | None] = mapped_column(Text)
    tax_year: Mapped[int | None] = mapped_column(Integer)
    document_version: Mapped[str | None] = mapped_column(Text)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    parser_name: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str | None] = mapped_column(Text)
    operator_admin_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="received")
    validation_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    approval_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class RawDocument(Base):
    __tablename__ = "raw_document"
    __table_args__ = {"schema": "tkms"}

    id: Mapped[uuid.UUID] = uuid_pk()
    import_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tkms.import_job.id", ondelete="CASCADE")
    )
    storage_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(Text)
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = created_at_col()


class ParseResult(Base):
    __tablename__ = "parse_result"
    __table_args__ = {"schema": "tkms"}

    id: Mapped[uuid.UUID] = uuid_pk()
    import_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tkms.import_job.id", ondelete="CASCADE")
    )
    parser_name: Mapped[str] = mapped_column(Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    text_object_key: Mapped[str | None] = mapped_column(Text)
    rule_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class ExtractedRule(Base):
    __tablename__ = "extracted_rule"
    __table_args__ = {"schema": "tkms"}

    id: Mapped[uuid.UUID] = uuid_pk()
    parse_result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tkms.parse_result.id", ondelete="CASCADE")
    )
    import_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tkms.import_job.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    promoted_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()


class ValidationReport(Base):
    __tablename__ = "validation_report"
    __table_args__ = {"schema": "tkms"}

    id: Mapped[uuid.UUID] = uuid_pk()
    import_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tkms.import_job.id", ondelete="CASCADE")
    )
    target_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class ValidationFinding(Base):
    __tablename__ = "validation_finding"
    __table_args__ = {"schema": "tkms"}

    id: Mapped[uuid.UUID] = uuid_pk()
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tkms.validation_report.id", ondelete="CASCADE")
    )
    rule_ref: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class ChangeReport(Base):
    __tablename__ = "change_report"
    __table_args__ = {"schema": "tkms"}

    id: Mapped[uuid.UUID] = uuid_pk()
    import_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tkms.import_job.id", ondelete="CASCADE")
    )
    draft_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    baseline_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class ChangeItem(Base):
    __tablename__ = "change_item"
    __table_args__ = {"schema": "tkms"}

    id: Mapped[uuid.UUID] = uuid_pk()
    change_report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tkms.change_report.id", ondelete="CASCADE")
    )
    field: Mapped[str] = mapped_column(Text, nullable=False)
    change_type: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class RollbackRecord(Base):
    __tablename__ = "rollback_record"
    __table_args__ = {"schema": "tkms"}

    id: Mapped[uuid.UUID] = uuid_pk()
    tax_rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_rule.id", ondelete="CASCADE")
    )
    from_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    to_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    performed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    requested_at: Mapped[datetime] = created_at_col()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class DeadLetter(Base):
    __tablename__ = "dead_letter"
    __table_args__ = {"schema": "tkms"}

    id: Mapped[uuid.UUID] = uuid_pk()
    task_name: Mapped[str] = mapped_column(Text, nullable=False)
    queue: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    import_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
