from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import CITEXT, INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk


class UserAccount(Base):
    __tablename__ = "user_account"
    __table_args__ = {"schema": "identity"}

    id: Mapped[uuid.UUID] = uuid_pk()
    # CITEXT, NOT String, AND IT IS A CORRECTNESS FIX RATHER THAN TIDYING.
    #
    # The column has been `citext` since the schema was written, so the unique
    # index and any hand-written SQL compare addresses case-insensitively. The
    # ORM mapping said `String`, which made SQLAlchemy bind the parameter as
    # varchar — and PostgreSQL resolves `citext = varchar` by casting the citext
    # side DOWN to text, so every ORM lookup keyed on email was case SENSITIVE
    # while the database it sat on was not.
    #
    # Measured, not inferred: with the same row stored as `Person@Example.com`,
    # a raw text-parameter query for `person@example.com` matched and the ORM
    # query did not.
    #
    # Three things were wrong because of it, and B3 made the third serious:
    #
    #   login          — `Person@x.ca` could not sign in as `person@x.ca`
    #   registration   — the duplicate pre-check missed, so the citext unique
    #                    index raised an IntegrityError: a 500 where the
    #                    service means to answer 409
    #   password reset — a customer typing their own address in a different
    #                    case got NO EMAIL AND NO ERROR, because the endpoint
    #                    is non-enumerating by design and silence is its
    #                    correct answer for an address it does not recognise
    #
    # The last one has no failure signal at all from the outside, which is why
    # it is fixed here rather than worked around at each call site.
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending_verification")
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class UserCredential(Base):
    __tablename__ = "user_credential"
    __table_args__ = {"schema": "identity"}

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE"),
        primary_key=True,
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(String, nullable=False, default="argon2id")
    must_reset: Mapped[bool] = mapped_column(default=False)
    password_changed_at: Mapped[datetime] = created_at_col()
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class AuthSession(Base):
    __tablename__ = "auth_session"
    __table_args__ = {"schema": "identity"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    refresh_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class LoginEvent(Base):
    __tablename__ = "login_event"
    __table_args__ = {"schema": "identity"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="SET NULL")
    )
    # Also `citext` in the schema — see the note on `UserAccount.email`.
    email_tried: Mapped[str | None] = mapped_column(CITEXT)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    subject_key: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Entry 11B6 (PD-3). When identity was removed from this row. Non-NULL
    # implies `email_tried IS NULL` and a coarsened `ip_address`; the guard in
    # identity.assert_login_events_deidentified checks exactly that.
    deidentified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class PasswordResetToken(Base):
    __tablename__ = "password_reset_token"
    __table_args__ = {"schema": "identity"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_token"
    __table_args__ = {"schema": "identity"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class MfaMethod(Base):
    __tablename__ = "mfa_method"
    __table_args__ = {"schema": "identity"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    method_type: Mapped[str] = mapped_column(String, nullable=False)
    secret_kms_ref: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(String)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class AccountSubject(Base):
    """Entry 11B6 (PD-15). The live half of the subject mapping.

    The only thing that resolves `audit.audit_log.subject_key` back to an
    account. Deleting one row here is what makes every audit row carrying that
    key non-attributable, without a single audit row being edited — which is the
    only way to de-identify an append-only log honestly.

    Deliberately NOT a foreign key to `user_account`: removal is ordered by the
    deletion lifecycle, not by a cascade. Written and read only by
    `identity.deidentify_audit_auth` and its SECURITY DEFINER siblings; the
    application roles hold no privilege on it at all. It is declared here
    because the schema-drift gate treats an unmapped table as structural loss
    and refuses to govern it with a policy entry.
    """

    __tablename__ = "account_subject"
    __table_args__ = {"schema": "identity"}

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    subject_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True,
        server_default=text("ref.uuid_generate_v7()"),
    )
    created_at: Mapped[datetime] = created_at_col()


class DeletionSubject(Base):
    """Entry 11B6B. The tombstone: this subject key belongs to a deleted account.

    Carries NO account id, on purpose. A column mapping the former account UUID
    to the retained key would make severance reversible by a single join, which
    is exactly the defect this table shape exists to prevent — so the primary
    key is the subject key itself.
    """

    __tablename__ = "deletion_subject"
    __table_args__ = {"schema": "identity"}

    subject_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    retired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
