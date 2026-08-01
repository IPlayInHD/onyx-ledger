from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col


class TaxProfile(Base):
    __tablename__ = "tax_profile"
    __table_args__ = {"schema": "profile"}

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE"),
        primary_key=True,
    )
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    province_code: Mapped[str | None] = mapped_column(String, ForeignKey("ref.province.code"))
    residency_status: Mapped[str | None] = mapped_column(String)
    marital_status: Mapped[str | None] = mapped_column(String)
    is_student: Mapped[bool] = mapped_column(Boolean, default=False)
    has_disability: Mapped[bool] = mapped_column(Boolean, default=False)
    first_time_home_buyer: Mapped[bool] = mapped_column(Boolean, default=False)
    employment_type: Mapped[str | None] = mapped_column(String)
    industry: Mapped[str | None] = mapped_column(String)
    employer_name: Mapped[str | None] = mapped_column(String)
    is_self_employed: Mapped[bool] = mapped_column(Boolean, default=False)
    housing_status: Mapped[str | None] = mapped_column(String)
    owns_home: Mapped[bool] = mapped_column(Boolean, default=False)
    has_mortgage: Mapped[bool] = mapped_column(Boolean, default=False)
    has_investments: Mapped[bool] = mapped_column(Boolean, default=False)
    has_rental_income: Mapped[bool] = mapped_column(Boolean, default=False)
    has_foreign_income: Mapped[bool] = mapped_column(Boolean, default=False)
    has_crypto: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    row_version: Mapped[int] = mapped_column(Integer, default=1)
