from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col


class IncomeSource(Base):
    __tablename__ = "income_source"
    __table_args__ = {"schema": "finance"}

    # Composite PK (id, tax_year) — the table is LIST-partitioned by tax_year.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("ref.uuid_generate_v7()"),
    )
    tax_year: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    income_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ref.income_type.id")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String, default="CAD")
    frequency: Mapped[str] = mapped_column(String, default="annual")
    province_code: Mapped[str | None] = mapped_column(String)
    source_name: Mapped[str | None] = mapped_column(String)
    verification_status: Mapped[str] = mapped_column(String, default="unverified")
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    # `Date` here silently truncated the time of day: the column is
    # timestamptz, and writing 14:37:42Z stored 00:00:00Z. Nothing had ever
    # written it, so nothing had noticed. Entry 11B5 is what starts writing
    # it, and a deletion stamp that rounds to midnight orders WRONGLY
    # against identity.account_lifecycle.requested_at — a row deleted at
    # 14:37 would appear to predate a deletion requested at 09:00 the same
    # day, which is exactly the comparison the purge cutoff makes.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExpenseRecord(Base):
    __tablename__ = "expense_record"
    __table_args__ = {"schema": "finance"}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("ref.uuid_generate_v7()"),
    )
    tax_year: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    expense_category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ref.expense_category.id")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String, default="CAD")
    description: Mapped[str | None] = mapped_column(Text)
    incurred_on: Mapped[date | None] = mapped_column(Date)
    verification_status: Mapped[str] = mapped_column(String, default="unverified")
    receipt_document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    # `Date` here silently truncated the time of day: the column is
    # timestamptz, and writing 14:37:42Z stored 00:00:00Z. Nothing had ever
    # written it, so nothing had noticed. Entry 11B5 is what starts writing
    # it, and a deletion stamp that rounds to midnight orders WRONGLY
    # against identity.account_lifecycle.requested_at — a row deleted at
    # 14:37 would appear to predate a deletion requested at 09:00 the same
    # day, which is exactly the comparison the purge cutoff makes.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
