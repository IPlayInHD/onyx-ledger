from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, uuid_pk


class AnalysisRun(Base):
    __tablename__ = "analysis_run"
    __table_args__ = {"schema": "analysis"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    tax_year: Mapped[int] = mapped_column(Integer, nullable=False)
    province_code: Mapped[str | None] = mapped_column(String)
    engine_version: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")
    total_income: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    taxable_income: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    estimated_tax: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    estimated_savings: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    marginal_rate: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    average_rate: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    confidence_score: Mapped[int | None] = mapped_column(SmallInteger)
    data_verified: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class AnalysisInputSnapshot(Base):
    __tablename__ = "analysis_input_snapshot"
    __table_args__ = {"schema": "analysis"}

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis.analysis_run.id", ondelete="CASCADE"),
        primary_key=True,
    )
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class AnalysisLineItem(Base):
    __tablename__ = "analysis_line_item"
    __table_args__ = {"schema": "analysis"}

    id: Mapped[uuid.UUID] = uuid_pk()
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis.analysis_run.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    fact_key: Mapped[str | None] = mapped_column(String)
    tax_rule_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_at: Mapped[datetime] = created_at_col()


class ReconciliationCheck(Base):
    __tablename__ = "reconciliation_check"
    __table_args__ = {"schema": "analysis"}

    id: Mapped[uuid.UUID] = uuid_pk()
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis.analysis_run.id", ondelete="CASCADE")
    )
    check_code: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class AnalysisAssumption(Base):
    __tablename__ = "analysis_assumption"
    __table_args__ = {"schema": "analysis"}

    id: Mapped[uuid.UUID] = uuid_pk()
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis.analysis_run.id", ondelete="CASCADE")
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
