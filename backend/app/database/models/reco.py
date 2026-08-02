from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk


class Recommendation(Base):
    __tablename__ = "recommendation"
    __table_args__ = {"schema": "reco"}

    id: Mapped[uuid.UUID] = uuid_pk()
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis.analysis_run.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    tax_rule_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    opportunity_code: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(String, nullable=False)
    mechanism: Mapped[str | None] = mapped_column(String)
    where_text: Mapped[str | None] = mapped_column(Text)
    how_text: Mapped[str | None] = mapped_column(Text)
    why_text: Mapped[str | None] = mapped_column(Text)
    estimated_impact: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    impact_label: Mapped[str | None] = mapped_column(String)
    confidence_score: Mapped[int | None] = mapped_column(SmallInteger)
    priority: Mapped[int] = mapped_column(SmallInteger, default=3)
    # ---- IOE link (additive) ----
    # `calculation_basis` records HOW the figure was produced; `evidence_status`
    # records how well the INPUTS are supported. Independent axes, never
    # collapsed. NULL on pre-IOE rows ("basis not recorded") rather than guessed.
    optimization_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    calculation_basis: Mapped[str | None] = mapped_column(Text)
    evidence_status: Mapped[str | None] = mapped_column(Text)
    citation: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="generated")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class RecommendationStatusEvent(Base):
    __tablename__ = "recommendation_status_event"
    __table_args__ = {"schema": "reco"}

    id: Mapped[uuid.UUID] = uuid_pk()
    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reco.recommendation.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class RecommendationFeedback(Base):
    __tablename__ = "recommendation_feedback"
    __table_args__ = {"schema": "reco"}

    id: Mapped[uuid.UUID] = uuid_pk()
    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reco.recommendation.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
