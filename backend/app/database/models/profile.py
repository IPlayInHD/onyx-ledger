from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk


class UserProfile(Base):
    __tablename__ = "user_profile"
    __table_args__ = {"schema": "profile"}

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE"),
        primary_key=True,
    )
    display_name: Mapped[str | None] = mapped_column(String)
    locale: Mapped[str] = mapped_column(String, default="en-CA")
    timezone: Mapped[str] = mapped_column(String, default="America/Toronto")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


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


class Dependent(Base):
    __tablename__ = "dependent"
    __table_args__ = {"schema": "profile"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    relationship: Mapped[str] = mapped_column(String, default="child")
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    has_disability: Mapped[bool] = mapped_column(Boolean, default=False)
    is_eligible_dependant: Mapped[bool] = mapped_column(Boolean, default=True)
    net_income: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SpouseProfile(Base):
    __tablename__ = "spouse_profile"
    __table_args__ = {"schema": "profile"}

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE"),
        primary_key=True,
    )
    has_spouse: Mapped[bool] = mapped_column(Boolean, default=False)
    spouse_net_income: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    spouse_date_of_birth: Mapped[date | None] = mapped_column(Date)
    spouse_is_student: Mapped[bool] = mapped_column(Boolean, default=False)
    spouse_has_disability: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class UserPreference(Base):
    __tablename__ = "user_preference"
    __table_args__ = {"schema": "profile"}

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE"),
        primary_key=True,
    )
    notification_prefs: Mapped[dict] = mapped_column(JSONB, default=dict)
    ui_prefs: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class UserPrivacySetting(Base):
    __tablename__ = "user_privacy_setting"
    __table_args__ = {"schema": "profile"}

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE"),
        primary_key=True,
    )
    marketing_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    analytics_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    data_retention_years: Mapped[int] = mapped_column(SmallInteger, default=7)
    consent_version: Mapped[str | None] = mapped_column(String)
    consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
