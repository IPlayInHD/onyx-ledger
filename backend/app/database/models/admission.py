"""Admission-control tables (Entry 10).

Identifiers, operation codes, bounded counters and timestamps. Deliberately no
financial column: an admission row records that expensive work happened, never
what was in it.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, uuid_pk


class RateCounter(Base):
    """Fixed-window request counter, one row per (scope, operation, window)."""

    __tablename__ = "rate_counter"
    __table_args__ = {
        "schema": "admission",
        "comment": (
            "Fixed-window request counters. One row per (scope, operation, "
            "window); incremented by a single conditional UPSERT so a "
            "concurrent burst cannot overshoot the allowance."
        ),
    }

    scope_type: Mapped[str] = mapped_column(Text, primary_key=True)
    scope_id: Mapped[str] = mapped_column(
        Text, primary_key=True,
        comment=(
            "Principal identity as text: a user or admin UUID, or the literal "
            "scope name for GLOBAL. Text rather than uuid because the same "
            "table serves non-UUID scopes."
        ),
    )
    operation_code: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = created_at_col()


class Lease(Base):
    """One admitted in-flight expensive operation.

    Live rows ARE the concurrency count; there is no separate counter to drift.
    """

    __tablename__ = "lease"
    __table_args__ = {
        "schema": "admission",
        "comment": (
            "One row per admitted in-flight expensive operation. Live rows are "
            "the concurrency count; expiry is what returns quota after a worker "
            "dies without running its release path."
        ),
    }

    id: Mapped[uuid.UUID] = uuid_pk()
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)
    operation_code: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(
        Text,
        comment=(
            "Bounded identity of the logical work (never a payload). A retry "
            "presenting the same key finds the running operation instead of "
            "starting a second one."
        ),
    )
    acquired_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        comment=(
            "After this instant the lease no longer counts against any limit, "
            "whether or not it was released. A crashed job therefore cannot "
            "hold a user quota forever."
        ),
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(Text)


__all__ = ["Lease", "RateCounter"]
