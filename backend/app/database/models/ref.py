from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Boolean, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, uuid_pk


class Province(Base):
    __tablename__ = "province"
    __table_args__ = {"schema": "ref"}

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    has_surtax: Mapped[bool] = mapped_column(Boolean, default=False)
    has_health_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    federal_abatement: Mapped[Decimal] = mapped_column(Numeric(9, 6), default=0)


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
    is_credit: Mapped[bool] = mapped_column(Boolean, default=False)
    cra_line: Mapped[str | None] = mapped_column(String)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
