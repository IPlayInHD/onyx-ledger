"""SQLAlchemy declarative base + shared column helpers."""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime

from sqlalchemy import DateTime, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Server-generated UUIDv7 primary key (default function lives in the `ref` schema).
def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("ref.uuid_generate_v7()"),
    )


def uuid7() -> uuid.UUID:
    """A UUIDv7 identical in shape to `ref.uuid_generate_v7()`.

    The server default remains the norm and stays in place for every ordinary
    writer. This exists for BATCHED evidence inserts: a parent id has to be known
    before its children are built, and asking the database for it means one
    `INSERT ... RETURNING` round trip per row — precisely the fan-out being
    removed. Supplying the id lets a whole table go in one statement.

    The layout matches the SQL function byte for byte — 48-bit big-endian Unix
    milliseconds, 74 bits of randomness, version 7, RFC-4122 variant — so rows
    written either way sort together and are indistinguishable to a reader.
    """
    unix_ms = int(time.time() * 1000)
    raw = bytearray(unix_ms.to_bytes(6, "big") + os.urandom(10))
    raw[6] = (raw[6] & 0x0F) | 0x70          # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80          # RFC-4122 variant
    return uuid.UUID(bytes=bytes(raw))


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def updated_at_col() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
