from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, SmallInteger, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk


# ---- code/label lookups (natural PK = code) --------------------------------
class Currency(Base):
    __tablename__ = "currency"
    __table_args__ = {"schema": "ref"}
    code: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    minor_unit: Mapped[int] = mapped_column(SmallInteger, default=2)


class TaxYear(Base):
    __tablename__ = "tax_year"
    __table_args__ = {"schema": "ref"}
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    indexation_factor: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    status: Mapped[str] = mapped_column(String, default="draft")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ResidencyStatus(Base):
    __tablename__ = "residency_status"
    __table_args__ = {"schema": "ref"}
    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class MaritalStatus(Base):
    __tablename__ = "marital_status"
    __table_args__ = {"schema": "ref"}
    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)
    treated_as_partnered: Mapped[bool] = mapped_column(Boolean, default=False)


class EmploymentType(Base):
    __tablename__ = "employment_type"
    __table_args__ = {"schema": "ref"}
    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)


class HousingStatus(Base):
    __tablename__ = "housing_status"
    __table_args__ = {"schema": "ref"}
    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)


class VerificationStatus(Base):
    __tablename__ = "verification_status"
    __table_args__ = {"schema": "ref"}
    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)


class AccountRegisteredType(Base):
    __tablename__ = "account_registered_type"
    __table_args__ = {"schema": "ref"}
    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)


class RuleCategory(Base):
    __tablename__ = "rule_category"
    __table_args__ = {"schema": "ref"}
    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)


class ConditionOperator(Base):
    __tablename__ = "condition_operator"
    __table_args__ = {"schema": "ref"}
    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)
    arity: Mapped[str] = mapped_column(String, nullable=False)


# ---- UUID-keyed reference tables -------------------------------------------
class Jurisdiction(Base):
    __tablename__ = "jurisdiction"
    __table_args__ = {"schema": "ref"}
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    level: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Province(Base):
    __tablename__ = "province"
    __table_args__ = {"schema": "ref"}
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    jurisdiction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ref.jurisdiction.id")
    )
    has_surtax: Mapped[bool] = mapped_column(Boolean, default=False)
    has_health_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    federal_abatement: Mapped[Decimal] = mapped_column(Numeric(9, 6), default=0)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class IncomeType(Base):
    __tablename__ = "income_type"
    __table_args__ = {"schema": "ref"}
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    default_inclusion: Mapped[Decimal] = mapped_column(Numeric(9, 6), default=1)
    is_gross_up: Mapped[bool] = mapped_column(Boolean, default=False)
    cra_slip_hint: Mapped[str | None] = mapped_column(String)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class ExpenseCategory(Base):
    __tablename__ = "expense_category"
    __table_args__ = {"schema": "ref"}
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ref.expense_category.id")
    )
    is_credit: Mapped[bool] = mapped_column(Boolean, default=False)
    cra_line: Mapped[str | None] = mapped_column(String)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class AssetCategory(Base):
    __tablename__ = "asset_category"
    __table_args__ = {"schema": "ref"}
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    is_registered: Mapped[bool] = mapped_column(Boolean, default=False)
    is_contribution_limited: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class LiabilityCategory(Base):
    __tablename__ = "liability_category"
    __table_args__ = {"schema": "ref"}
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class DocumentType(Base):
    __tablename__ = "document_type"
    __table_args__ = {"schema": "ref"}
    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, default="slip")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
