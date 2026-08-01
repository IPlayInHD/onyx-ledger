from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk


class Asset(Base):
    __tablename__ = "asset"
    __table_args__ = {"schema": "wealth"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    asset_category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ref.asset_category.id")
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    acquisition_date: Mapped[date | None] = mapped_column(Date)
    acquisition_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    current_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    current_value_as_of: Mapped[date | None] = mapped_column(Date)
    currency_code: Mapped[str] = mapped_column(String, default="CAD")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetValuation(Base):
    __tablename__ = "asset_valuation"
    __table_args__ = {"schema": "wealth"}

    id: Mapped[uuid.UUID] = uuid_pk()
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wealth.asset.id", ondelete="CASCADE")
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    source: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = created_at_col()


class RegisteredAccountDetail(Base):
    __tablename__ = "registered_account_detail"
    __table_args__ = {"schema": "wealth"}

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wealth.asset.id", ondelete="CASCADE"), primary_key=True
    )
    registered_type: Mapped[str] = mapped_column(String, nullable=False)
    tax_year: Mapped[int] = mapped_column(nullable=False)
    contribution_room: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    contributions_ytd: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    withdrawals_ytd: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Liability(Base):
    __tablename__ = "liability"
    __table_args__ = {"schema": "wealth"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    liability_category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ref.liability_category.id")
    )
    provider: Mapped[str | None] = mapped_column(String)
    principal_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    current_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    current_balance_as_of: Mapped[date | None] = mapped_column(Date)
    interest_rate: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    status: Mapped[str] = mapped_column(String, default="open")
    opened_on: Mapped[date | None] = mapped_column(Date)
    currency_code: Mapped[str] = mapped_column(String, default="CAD")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LiabilityBalance(Base):
    __tablename__ = "liability_balance"
    __table_args__ = {"schema": "wealth"}

    id: Mapped[uuid.UUID] = uuid_pk()
    liability_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wealth.liability.id", ondelete="CASCADE")
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    interest_rate: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    created_at: Mapped[datetime] = created_at_col()
