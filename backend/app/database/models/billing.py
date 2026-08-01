from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, SmallInteger, String
from sqlalchemy.dialects.postgresql import CHAR, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk


class Plan(Base):
    __tablename__ = "plan"
    __table_args__ = {"schema": "billing"}

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    price_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    currency_code: Mapped[str] = mapped_column(String, default="CAD")
    billing_interval: Mapped[str] = mapped_column(String, default="month")
    features: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Subscription(Base):
    __tablename__ = "subscription"
    __table_args__ = {"schema": "billing"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("billing.plan.id"))
    status: Mapped[str] = mapped_column(String, default="trialing")
    provider: Mapped[str] = mapped_column(String, default="stripe")
    provider_subscription_id: Mapped[str | None] = mapped_column(String)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Invoice(Base):
    __tablename__ = "invoice"
    __table_args__ = {"schema": "billing"}

    id: Mapped[uuid.UUID] = uuid_pk()
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing.subscription.id", ondelete="CASCADE")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String, default="CAD")
    status: Mapped[str] = mapped_column(String, default="open")
    provider_invoice_id: Mapped[str | None] = mapped_column(String)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class PaymentMethodRef(Base):
    __tablename__ = "payment_method_ref"
    __table_args__ = {"schema": "billing"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String, default="stripe")
    provider_customer_id: Mapped[str | None] = mapped_column(String)
    provider_method_id: Mapped[str | None] = mapped_column(String)
    brand: Mapped[str | None] = mapped_column(String)
    last4: Mapped[str | None] = mapped_column(CHAR(4))
    exp_month: Mapped[int | None] = mapped_column(SmallInteger)
    exp_year: Mapped[int | None] = mapped_column(SmallInteger)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created_at_col()


class Entitlement(Base):
    __tablename__ = "entitlement"
    __table_args__ = {"schema": "billing"}

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE"),
        primary_key=True,
    )
    features: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    updated_at: Mapped[datetime] = updated_at_col()
