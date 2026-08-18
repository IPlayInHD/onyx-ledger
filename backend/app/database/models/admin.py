from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk


class AdminUser(Base):
    __tablename__ = "admin_user"
    __table_args__ = {"schema": "admin"}

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Role(Base):
    __tablename__ = "role"
    __table_args__ = {"schema": "admin"}

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class Permission(Base):
    __tablename__ = "permission"
    __table_args__ = {"schema": "admin"}

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class RolePermission(Base):
    __tablename__ = "role_permission"
    __table_args__ = {"schema": "admin"}

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin.role.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin.permission.id", ondelete="CASCADE"), primary_key=True
    )


class AdminUserRole(Base):
    __tablename__ = "admin_user_role"
    __table_args__ = {"schema": "admin"}

    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin.admin_user.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin.role.id", ondelete="CASCADE"), primary_key=True
    )
    granted_at: Mapped[datetime] = created_at_col()


class RuleChangeRequest(Base):
    __tablename__ = "rule_change_request"
    __table_args__ = {"schema": "admin"}

    id: Mapped[uuid.UUID] = uuid_pk()
    tax_rule_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_rule_version.id", ondelete="CASCADE")
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="draft")
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class RulePublication(Base):
    __tablename__ = "rule_publication"
    __table_args__ = {"schema": "admin"}

    id: Mapped[uuid.UUID] = uuid_pk()
    tax_rule_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tax_kb.tax_rule_version.id", ondelete="CASCADE")
    )
    change_request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    published_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # ---- governed publication (entry: authoring pipeline) ----
    # `channel` is recorded rather than inferred: §52 forbids deciding whether a
    # publication was production from a rule-code prefix or an environment name.
    spec_hash: Mapped[str | None] = mapped_column(Text)
    pack_hash: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text, default="legacy", nullable=False)
    published_at: Mapped[datetime] = created_at_col()
