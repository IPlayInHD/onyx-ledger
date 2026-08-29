"""BillShield persistence foundation (schema: billshield).

Mirrors `backend/db/sql/67_billshield_foundation.sql`. As everywhere else in
this package, the canonical SQL owns CHECK constraints, RLS policies, triggers,
partial indexes and the governed `evidence_locators` domain; the models exist so
the application can query the tables and so the schema-drift gate can compare
both sides. A table mapped on only one side is never governable drift — it
fails outright — which is the point.

Two shapes here differ from the older domains and are deliberate:

* `Bill.storage_key` is a GENERATED column, declared with `Computed` so the ORM
  knows never to write it. Shape is not ownership: the database computes the
  object key from this row's own ids, so a key naming another tenant cannot be
  written at all.
* Money is `ref.money_amt` (NUMERIC(14,2)) and confidences are `ref.rate`
  (NUMERIC(9,6)), the shared domains — never a float, at any layer.

Enum-like columns are `Text` with SQL CHECK constraints, following the
repository's no-native-enum rule. The Python authorities for those vocabularies
are the committed extraction contract and
`app/services/billshield/domain/states.py`; the parity test compares them
against the live constraints rather than trusting either side's comment.
"""
from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk

#: The shared money and rate domains, as the tax models use them.
MONEY = Numeric(14, 2)
RATE = Numeric(9, 6)

#: Per-field evidence locators. The governed `billshield.evidence_locators`
#: domain — bounded array, closed key set, positive integer page, canonical
#: unit-interval decimal STRINGS — is enforced by the database; JSONB is the
#: base type the driver sees.
EVIDENCE = JSONB


class Provider(Base):
    """Global provider identity. Deliberately carries no category column."""

    __tablename__ = "provider"
    __table_args__ = {"schema": "billshield"}

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str] = mapped_column(Text, nullable=False, default="CA")
    created_at: Mapped[datetime.datetime] = created_at_col()
    updated_at: Mapped[datetime.datetime] = updated_at_col()


class ProviderCategory(Base):
    """Which governed service categories a provider offers, one row each."""

    __tablename__ = "provider_category"
    __table_args__ = (
        UniqueConstraint("provider_id", "category",
                         name="uq_billshield_provider_category"),
        {"schema": "billshield"},
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billshield.provider.id", ondelete="CASCADE"),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = created_at_col()


class Bill(Base):
    """One uploaded customer bill and its lifecycle. Metadata only."""

    __tablename__ = "bill"
    __table_args__ = (
        UniqueConstraint("id", "user_id",
                         name="uq_billshield_bill_identity_owner"),
        UniqueConstraint("id", "file_sha256",
                         name="uq_billshield_bill_identity_digest"),
        {"schema": "billshield"},
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identity.user_account.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="upload_pending")
    storage_key: Mapped[str] = mapped_column(
        Text,
        Computed("user_id::text || '/billshield/v1/' || id::text", persisted=True),
        nullable=False,
    )
    file_sha256: Mapped[str | None] = mapped_column(Text)
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    artifact_format: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    #: Logical deletion: the customer asked and the artifact stops being served.
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Physical erasure, stamped only by the privacy worker (Slice 3).
    erased_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime.datetime] = created_at_col()
    #: NOT `updated_at_col()`. The shared helper carries `onupdate=func.now()`,
    #: which makes every ORM UPDATE name this column explicitly — and the API's
    #: column grant deliberately excludes it, so such a statement is refused
    #: outright. Here the `ref.set_updated_at()` trigger is authoritative: the
    #: database stamps the column on every write, and no runtime can backdate
    #: its own edit. The shared helper is unchanged; only this table opts out.
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class ExtractionRun(Base):
    """One immutable extraction attempt over one finalized bill artifact."""

    __tablename__ = "extraction_run"
    __table_args__ = (
        ForeignKeyConstraint(
            ["bill_id", "input_sha256"],
            ["billshield.bill.id", "billshield.bill.file_sha256"],
            ondelete="CASCADE",
            name="fk_billshield_extraction_run_bill_artifact",
        ),
        {"schema": "billshield"},
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    bill_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: Bound compositely to the SAME bill's finalized digest, so a run can never
    #: be repointed at another artifact.
    input_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    #: The extractor read the document and declined it whole (RefusalCode).
    refusal_code: Mapped[str | None] = mapped_column(Text)
    #: The provider's output did not survive the strict parse
    #: (ExtractionParseCode). A different authority from refusal, and neither is
    #: the future outbox/runtime failure vocabulary, which has no column here.
    failure_code: Mapped[str | None] = mapped_column(Text)
    #: Trusted adapter provenance — never the untrusted issuer read off the bill.
    adapter_code: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    extraction_schema_version: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str | None] = mapped_column(Text)
    #: extraction_identity() — stored instead of the provider's response.
    response_hash: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    # Bill-level candidates. All three columns NULL means the extractor produced
    # NO candidate; it never means the document lacks the field.
    issuer_name_value: Mapped[str | None] = mapped_column(Text)
    issuer_name_confidence: Mapped[Decimal | None] = mapped_column(RATE)
    issuer_name_evidence: Mapped[dict | None] = mapped_column(EVIDENCE)
    service_category_value: Mapped[str | None] = mapped_column(Text)
    service_category_confidence: Mapped[Decimal | None] = mapped_column(RATE)
    service_category_evidence: Mapped[dict | None] = mapped_column(EVIDENCE)
    statement_date_value: Mapped[datetime.date | None] = mapped_column(Date)
    statement_date_confidence: Mapped[Decimal | None] = mapped_column(RATE)
    statement_date_evidence: Mapped[dict | None] = mapped_column(EVIDENCE)
    billing_period_start: Mapped[datetime.date | None] = mapped_column(Date)
    billing_period_end: Mapped[datetime.date | None] = mapped_column(Date)
    billing_period_confidence: Mapped[Decimal | None] = mapped_column(RATE)
    billing_period_evidence: Mapped[dict | None] = mapped_column(EVIDENCE)
    amount_due_value: Mapped[Decimal | None] = mapped_column(MONEY)
    amount_due_confidence: Mapped[Decimal | None] = mapped_column(RATE)
    amount_due_evidence: Mapped[dict | None] = mapped_column(EVIDENCE)
    previous_balance_value: Mapped[Decimal | None] = mapped_column(MONEY)
    previous_balance_confidence: Mapped[Decimal | None] = mapped_column(RATE)
    previous_balance_evidence: Mapped[dict | None] = mapped_column(EVIDENCE)
    payments_applied_value: Mapped[Decimal | None] = mapped_column(MONEY)
    payments_applied_confidence: Mapped[Decimal | None] = mapped_column(RATE)
    payments_applied_evidence: Mapped[dict | None] = mapped_column(EVIDENCE)
    subtotal_before_tax_value: Mapped[Decimal | None] = mapped_column(MONEY)
    subtotal_before_tax_confidence: Mapped[Decimal | None] = mapped_column(RATE)
    subtotal_before_tax_evidence: Mapped[dict | None] = mapped_column(EVIDENCE)
    total_tax_value: Mapped[Decimal | None] = mapped_column(MONEY)
    total_tax_confidence: Mapped[Decimal | None] = mapped_column(RATE)
    total_tax_evidence: Mapped[dict | None] = mapped_column(EVIDENCE)
    created_at: Mapped[datetime.datetime] = created_at_col()


class ChargeCandidate(Base):
    """An unconfirmed extracted charge, immutable and in document order."""

    __tablename__ = "charge_candidate"
    __table_args__ = (
        UniqueConstraint("extraction_run_id", "position",
                         name="uq_billshield_charge_candidate_position"),
        {"schema": "billshield"},
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    extraction_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billshield.extraction_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Document order is semantic: the extraction contract folds it into the
    #: response hash, so a re-sorted list is a different extraction.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    label_text: Mapped[str] = mapped_column(Text, nullable=False)
    label_confidence: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    label_evidence: Mapped[dict] = mapped_column(EVIDENCE, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    amount_confidence: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    amount_evidence: Mapped[dict] = mapped_column(EVIDENCE, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    kind_confidence: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    cadence_value: Mapped[str | None] = mapped_column(Text)
    cadence_confidence: Mapped[Decimal | None] = mapped_column(RATE)
    cadence_evidence: Mapped[dict | None] = mapped_column(EVIDENCE)
    service_period_start: Mapped[datetime.date | None] = mapped_column(Date)
    service_period_end: Mapped[datetime.date | None] = mapped_column(Date)
    service_period_confidence: Mapped[Decimal | None] = mapped_column(RATE)
    service_period_evidence: Mapped[dict | None] = mapped_column(EVIDENCE)
    created_at: Mapped[datetime.datetime] = created_at_col()


class PromotionCandidate(Base):
    """An expiry date printed on the bill, bound to a charge of the same run."""

    __tablename__ = "promotion_candidate"
    __table_args__ = (
        UniqueConstraint("extraction_run_id", "position",
                         name="uq_billshield_promotion_candidate_position"),
        ForeignKeyConstraint(
            ["extraction_run_id", "charge_position"],
            ["billshield.charge_candidate.extraction_run_id",
             "billshield.charge_candidate.position"],
            ondelete="CASCADE",
            name="fk_billshield_promotion_charge",
        ),
        {"schema": "billshield"},
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    extraction_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billshield.extraction_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    #: NULL means document or service level; a value names a charge POSITION in
    #: the same extraction run, which is how the contract associates them.
    charge_position: Mapped[int | None] = mapped_column(Integer)
    expiry_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    expiry_confidence: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    expiry_evidence: Mapped[dict] = mapped_column(EVIDENCE, nullable=False)
    created_at: Mapped[datetime.datetime] = created_at_col()


class JobOutbox(Base):
    """Transactional job intent: identifiers and one closed task code."""

    __tablename__ = "job_outbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ["bill_id", "user_id"],
            ["billshield.bill.id", "billshield.bill.user_id"],
            ondelete="CASCADE",
            name="fk_billshield_job_outbox_bill_owner",
        ),
        # One intent per task per bill. The globally unique dedupe key does not
        # imply this: the caller picks the key, so a fresh UUID would enqueue
        # the same work twice.
        UniqueConstraint("task_code", "bill_id",
                         name="uq_billshield_job_outbox_intent"),
        {"schema": "billshield"},
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identity.user_account.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Bound compositely to a bill OWNED BY user_id: a policy that checked only
    #: user_id would accept tenant A's id beside tenant B's bill.
    bill_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_code: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True)
    #: Server-defaulted. The API's enqueue grant excludes this and every column
    #: below it, so a request path cannot create already-claimed work.
    claim_state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    claimed_by: Mapped[str | None] = mapped_column(Text)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claimed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = created_at_col()
