"""BillExtractionV1 — the versioned success contract, and its refusal twin.

TWO IDENTITIES THAT MUST NEVER MEET. The extraction ADAPTER (our code and its
model: `adapter_code`, `model_version`, `prompt_version`) is trusted
provenance declared on the `BillExtractionProvider` Protocol and bound into
evaluation reports. The BILL ISSUER (`bill_issuer_name` — Bell, Rogers, a
streaming service) is an untrusted, evidence-backed candidate extracted FROM
the document. The parser refuses any payload that tries to smuggle adapter
identity in as data, and nothing here maps issuer text to a catalogue
provider id — that deterministic resolver belongs to the catalogue slice.

EVERYTHING IS A CANDIDATE. `Present` means "the extractor produced an
evidence-backed candidate"; `NoCandidate` means only "it did not". NoCandidate
is NEVER a statement that the document lacks the field — the extractor holds
no authority over what a document contains, and ground-truth labels carry
their own human-reviewed PRESENT / ABSENT_ON_DOCUMENT distinction. A success
may therefore be PARTIAL; what it may not be is EMPTY (no amount_due candidate
and no charges), because "we read your bill" must mean something was read.

MONEY is Decimal-only under one normalized economic-effect rule: positive
increases the balance owed, negative decreases it. Printed notation —
parentheses, trailing minus, "CR" — is an adapter concern; only the
normalized Decimal enters the contract, and evidence points at the notation.

Hashing reuses the repository's sole canonicalizer; `extraction_identity()`
is the §9.5 "response hash" a later slice persists instead of raw provider
output. Serialization is deterministic: Decimals through the canonical scale
helpers, dates as dates, no datetime, no set, no float — the canonicalizer
refuses them all.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Generic, TypeAlias, TypeVar

from app.services.billshield.extraction.codes import RefusalCode
from app.services.ioe.domain import canonical as c

BILL_EXTRACTION_SCHEMA_VERSION = "1.0.0"

T = TypeVar("T")

Renderer: TypeAlias = Callable[[T], object]


class Currency(StrEnum):
    """MVP supports CAD only (plan §6.2); anything else is refused."""

    CAD = "CAD"


class ServiceCategory(StrEnum):
    """The approved V1 category set (plan §3.2 build-now scope).

    Utilities and insurance are §3.3 build-later and deliberately absent.
    `OTHER_SUBSCRIPTION` covers the common-subscription tail without
    pretending an unqualified "other" is a classification.
    """

    MOBILE = "MOBILE"
    INTERNET = "INTERNET"
    TV = "TV"
    HOME_PHONE = "HOME_PHONE"
    BUNDLE = "BUNDLE"
    STREAMING = "STREAMING"
    OTHER_SUBSCRIPTION = "OTHER_SUBSCRIPTION"


class ChargeKind(StrEnum):
    """What a line item is. RECURRING_FIXED is the only kind later price-creep
    analysis may compare; UNCLASSIFIED is the fail-closed candidate state that
    preserves a useful label and amount without forcing the model to guess —
    it is never savings-eligible and counts as incorrect whenever ground truth
    knows the kind."""

    RECURRING_FIXED = "RECURRING_FIXED"
    USAGE = "USAGE"
    ONE_TIME = "ONE_TIME"
    TAX = "TAX"
    CREDIT = "CREDIT"
    FEE = "FEE"
    DEVICE_FINANCING = "DEVICE_FINANCING"
    PRORATION = "PRORATION"
    UNCLASSIFIED = "UNCLASSIFIED"


class Cadence(StrEnum):
    """How often a recurring charge recurs. A printed but unsupported cadence
    becomes evidence-backed OTHER — never "missing". Only MONTHLY is eligible
    for initial savings comparisons (plan §6.2); every value may be tracked."""

    MONTHLY = "MONTHLY"
    WEEKLY = "WEEKLY"
    BIWEEKLY = "BIWEEKLY"
    SEMI_MONTHLY = "SEMI_MONTHLY"
    QUARTERLY = "QUARTERLY"
    SEMI_ANNUAL = "SEMI_ANNUAL"
    ANNUAL = "ANNUAL"
    OTHER = "OTHER"


@dataclass(frozen=True)
class ExtractionLimits:
    """Named, versioned structural/DoS limits — not scattered literals.

    These bound the CONTRACT the parser will accept; they are not customer
    upload limits and must not be presented as one.
    """

    version: str
    max_charges: int
    max_promotions: int
    max_label_chars: int
    max_issuer_name_chars: int
    max_evidence_per_field: int


EXTRACTION_LIMITS_V1 = ExtractionLimits(
    version="1.0.0",
    max_charges=500,
    max_promotions=50,
    max_label_chars=500,
    max_issuer_name_chars=200,
    max_evidence_per_field=16,
)


@dataclass(frozen=True)
class Evidence:
    """One locator: page plus a normalized bounding box.

    Deliberately carries NO text snippet — bill text cannot enter the
    contract, a report, or an exception through a type that cannot hold it.
    A text-span locator is DEFERRED, not impossible: page+bbox is the one
    shape native PDFs, scanned PDFs, and images all support, so V1 keeps a
    single location comparator; a future schema version may add spans.

    Evidence order is non-semantic. The parser validates every locator,
    refuses duplicates, and then canonical-sorts by (page, y0, x0, y1, x1) —
    its one documented normalization, discarding nothing.
    """

    page: int
    x0: Decimal
    y0: Decimal
    x1: Decimal
    y1: Decimal

    def sort_key(self) -> tuple[int, Decimal, Decimal, Decimal, Decimal]:
        return (self.page, self.y0, self.x0, self.y1, self.x1)

    def as_canonical(self) -> dict[str, object]:
        return {
            "page": self.page,
            "x0": c.rate(self.x0),
            "y0": c.rate(self.y0),
            "x1": c.rate(self.x1),
            "y1": c.rate(self.y1),
        }


@dataclass(frozen=True)
class Present(Generic[T]):
    """An evidence-backed candidate. At least one locator, always."""

    value: T
    confidence: Decimal
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class NoCandidate:
    """The extractor produced no evidence-backed candidate for this field.

    This is a statement about the EXTRACTOR, never about the document. It must
    not be rendered, stored, or interpreted as "absent from the document" —
    only a person reviewing the bill (or a ground-truth label) can say that.
    """


FieldState: TypeAlias = Present[T] | NoCandidate


@dataclass(frozen=True)
class ServicePeriod:
    """A half-open-in-spirit printed period; the only invariant is
    start <= end. Real bills set their own calendars — no window relative to
    the statement date and no maximum length is imposed."""

    start: date
    end: date

    def as_canonical(self) -> dict[str, object]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class ChargeCandidate:
    """One line item. Label and amount are each their own evidence-backed
    candidate — one locator on a whole charge would prove neither.

    Sign rules under the normalized economic-effect convention:
    CREDIT strictly negative; RECURRING_FIXED, USAGE, ONE_TIME, FEE and
    DEVICE_FINANCING zero or positive; TAX and PRORATION either sign
    (adjustments and reversals exist); UNCLASSIFIED either sign, because
    constraining the sign of an unknown kind would be a guess.

    A cadence candidate is allowed on RECURRING_FIXED and DEVICE_FINANCING —
    and on UNCLASSIFIED, so a printed "/mo" survives even when the model
    honestly cannot classify the line. `kind` and `kind_confidence` are an
    unconfirmed classification; evidence elsewhere on the charge indicates
    source material, never that the classification is right.
    """

    label: Present[str]
    amount: Present[Decimal]
    kind: ChargeKind
    kind_confidence: Decimal
    cadence: FieldState[Cadence]
    service_period: FieldState[ServicePeriod]

    def as_canonical(self) -> dict[str, object]:
        return {
            "label": _canonical_state(self.label, _render_text),
            "amount": _canonical_state(self.amount, c.money),
            "kind": self.kind,
            "kind_confidence": c.rate(self.kind_confidence),
            "cadence": _canonical_state(self.cadence, _render_enum),
            "service_period": _canonical_state(self.service_period, _render_period),
        }


@dataclass(frozen=True)
class PromotionCandidate:
    """An EXPLICITLY PRINTED promotion expiry, bound to what it affects.

    Never inferred (plan §6.1 forbids the model to guess an expiry).
    `charge_index` binds it to one charge; None means the document/service
    level, for a promotion no single line item carries. Several promotions
    may bind to one charge; their identity in evaluation is the association
    plus occurrence order, never the expiry date.
    """

    expiry_date: Present[date]
    charge_index: int | None

    def as_canonical(self) -> dict[str, object]:
        return {
            "expiry_date": _canonical_state(self.expiry_date, _render_date),
            "charge_index": self.charge_index,
        }


@dataclass(frozen=True)
class BillExtractionV1:
    """A validated, possibly PARTIAL, successful extraction.

    Success and refusal are different types: this one cannot represent an
    unreadable or unsupported document, and `BillExtractionRefusal` cannot
    carry a field. The parser guarantees minimum content — a Present
    amount_due or at least one charge.
    """

    schema_version: str
    currency: Currency
    bill_issuer_name: FieldState[str]
    service_category: FieldState[ServiceCategory]
    statement_date: FieldState[date]
    billing_period: FieldState[ServicePeriod]
    amount_due: FieldState[Decimal]
    previous_balance: FieldState[Decimal]
    payments_applied: FieldState[Decimal]
    subtotal_before_tax: FieldState[Decimal]
    total_tax: FieldState[Decimal]
    charges: tuple[ChargeCandidate, ...]
    promotions: tuple[PromotionCandidate, ...]

    def as_canonical(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "currency": self.currency,
            "bill_issuer_name": _canonical_state(self.bill_issuer_name, _render_text),
            "service_category": _canonical_state(self.service_category, _render_enum),
            "statement_date": _canonical_state(self.statement_date, _render_date),
            "billing_period": _canonical_state(self.billing_period, _render_period),
            "amount_due": _canonical_state(self.amount_due, c.money),
            "previous_balance": _canonical_state(self.previous_balance, c.money),
            "payments_applied": _canonical_state(self.payments_applied, c.money),
            "subtotal_before_tax": _canonical_state(self.subtotal_before_tax, c.money),
            "total_tax": _canonical_state(self.total_tax, c.money),
            # Document order is semantic and preserved — charges hash in the
            # order the bill listed them, exactly as the caller-order rule in
            # the canonicalizer intends.
            "charges": [charge.as_canonical() for charge in self.charges],
            "promotions": [promo.as_canonical() for promo in self.promotions],
        }

    def extraction_identity(self) -> str:
        """The §9.5 response hash: the candidates a strict parse accepted."""
        return c.domain_hash(c.DOMAIN_BILLSHIELD_EXTRACTION, self.as_canonical())


@dataclass(frozen=True)
class BillExtractionRefusal:
    """A whole-document refusal. A closed code and nothing else — no free
    text travels from a provider through this type."""

    schema_version: str
    code: RefusalCode


# ---------------------------------------------------------------------------
# Canonical rendering helpers
# ---------------------------------------------------------------------------
def _canonical_state(
    state: Present[T] | NoCandidate, render: Renderer[T]
) -> dict[str, object]:
    if isinstance(state, NoCandidate):
        return {"state": "no_candidate"}
    return {
        "state": "present",
        "value": render(state.value),
        "confidence": c.rate(state.confidence),
        "evidence": [locator.as_canonical() for locator in state.evidence],
    }


def _render_text(value: str) -> object:
    return value


def _render_enum(value: StrEnum) -> object:
    return value


def _render_date(value: date) -> object:
    return value


def _render_period(value: ServicePeriod) -> object:
    return value.as_canonical()


__all__ = [
    "BILL_EXTRACTION_SCHEMA_VERSION",
    "EXTRACTION_LIMITS_V1",
    "BillExtractionRefusal",
    "BillExtractionV1",
    "Cadence",
    "ChargeCandidate",
    "ChargeKind",
    "Currency",
    "Evidence",
    "ExtractionLimits",
    "FieldState",
    "NoCandidate",
    "Present",
    "PromotionCandidate",
    "ServiceCategory",
    "ServicePeriod",
]
