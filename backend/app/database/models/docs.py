from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk


class Document(Base):
    __tablename__ = "document"
    __table_args__ = {"schema": "docs"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    document_type_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tax_year: Mapped[int | None] = mapped_column()
    storage_provider: Mapped[str] = mapped_column(String, default="s3")
    bucket: Mapped[str] = mapped_column(String, nullable=False)
    object_key: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String)
    mime_type: Mapped[str | None] = mapped_column(String)
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String, default="uploaded")
    uploaded_at: Mapped[datetime] = created_at_col()
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentExtraction(Base):
    __tablename__ = "document_extraction"
    __table_args__ = {"schema": "docs"}

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("docs.document.id", ondelete="CASCADE")
    )
    engine: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class ExtractionField(Base):
    __tablename__ = "extraction_field"
    __table_args__ = {"schema": "docs"}

    id: Mapped[uuid.UUID] = uuid_pk()
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("docs.document_extraction.id", ondelete="CASCADE")
    )
    field_name: Mapped[str] = mapped_column(String, nullable=False)
    fact_key: Mapped[str | None] = mapped_column(String)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_number: Mapped[Decimal | None] = mapped_column(Numeric)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    bounding_box: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_col()


class DocumentLink(Base):
    __tablename__ = "document_link"
    __table_args__ = {"schema": "docs"}

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("docs.document.id", ondelete="CASCADE")
    )
    income_source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    income_tax_year: Mapped[int | None] = mapped_column()
    expense_record_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    expense_tax_year: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime] = created_at_col()
