"""Ground-truth labels — the human-reviewed answer key, with its own vocabulary.

Labels are AUTHORITATIVE about the document in a way no extractor output ever
is, which is why their absence vocabulary differs from the contract's: a
label says PRESENT or ABSENT_ON_DOCUMENT (a person looked), while a
prediction says only present or no_candidate (the extractor produced a
candidate or did not). Evaluation joins the two: labeled-present with no
candidate is a miss; labeled-absent with a candidate is a false positive;
labeled-absent with no candidate is a true negative.

Label files live ONLY under the external corpus root — they contain charge
labels and amounts, which for customer-derived samples is bill content. Only
their SHA-256 enters git, and label corrections are versioned by that digest
changing in a committed manifest revision. Everything read here is untrusted
file input and parsed strictly; nulls mean "not labeled", never "absent".
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Generic, TypeAlias, TypeVar

from app.services.billshield.evaluation.manifest import (
    LABEL_SCHEMA_VERSION,
    EvaluationError,
)
from app.services.billshield.extraction.codes import (
    EvaluationErrorCode,
    RefusalCode,
)
from app.services.billshield.extraction.contract import (
    Cadence,
    ChargeKind,
    ServiceCategory,
    ServicePeriod,
)

T = TypeVar("T")

#: The scalar contract fields a label must decide, and how each is typed.
SCALAR_FIELD_TYPES: dict[str, str] = {
    "bill_issuer_name": "text",
    "service_category": "category",
    "statement_date": "date",
    "billing_period": "period",
    "amount_due": "money",
    "previous_balance": "money",
    "payments_applied": "money",
    "subtotal_before_tax": "money",
    "total_tax": "money",
}


class ExpectedOutcome(StrEnum):
    SUCCESS = "success"
    REFUSAL = "refusal"


@dataclass(frozen=True)
class Labeled(Generic[T]):
    """A person confirmed the field is printed, with this value."""

    value: T


@dataclass(frozen=True)
class AbsentOnDocument:
    """A person confirmed the document does not carry this field."""


GroundTruth: TypeAlias = Labeled[T] | AbsentOnDocument


@dataclass(frozen=True)
class GroundTruthCharge:
    """One labeled line item, in document order.

    `kind` and `cadence` may be None — NOT LABELED — which excludes the
    charge from those metrics' denominators; it is distinct from
    AbsentOnDocument, which is an answer.
    """

    label: str
    amount: Decimal
    kind: ChargeKind | None
    cadence: GroundTruth[Cadence] | None


@dataclass(frozen=True)
class GroundTruthPromotion:
    """An explicitly printed promotion expiry a person confirmed."""

    expiry_date: date
    charge_ref: int | None


@dataclass(frozen=True)
class GroundTruthSuccess:
    scalars: Mapping[str, GroundTruth[object]]
    charges: tuple[GroundTruthCharge, ...]
    promotions: tuple[GroundTruthPromotion, ...]


@dataclass(frozen=True)
class GroundTruthLabel:
    label_schema_version: str
    expected_outcome: ExpectedOutcome
    refusal_code: RefusalCode | None
    success: GroundTruthSuccess | None


def _fail(detail: str) -> EvaluationError:
    return EvaluationError(EvaluationErrorCode.MALFORMED_LABEL, detail)


def _object(raw: object, what: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise _fail(f"{what} must be an object")
    return raw


def _known(payload: Mapping[str, object], known: set[str], what: str) -> None:
    unknown = sorted(set(payload) - known)
    if unknown:
        raise _fail(
            f"{what} has {len(unknown)} unknown field(s); known fields are "
            f"{sorted(known)}")


def _money(raw: object, what: str) -> Decimal:
    if not isinstance(raw, str):
        raise _fail(f"{what} must be an exact decimal string")
    if not re.fullmatch(r"-?\d+\.\d{2}", raw):
        raise _fail(f"{what} must carry exactly two fractional digits")
    return Decimal(raw)


def _date(raw: object, what: str) -> date:
    if not isinstance(raw, str):
        raise _fail(f"{what} must be an ISO date string")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise _fail(f"{what} is not an ISO date") from exc


def _text(raw: object, what: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise _fail(f"{what} must be non-empty text")
    return raw


def _scalar_value(kind: str, raw: object, what: str) -> object:
    if kind == "money":
        return _money(raw, what)
    if kind == "date":
        return _date(raw, what)
    if kind == "text":
        return _text(raw, what)
    if kind == "category":
        raw_text = _text(raw, what)
        try:
            return ServiceCategory(raw_text)
        except ValueError:
            raise _fail(f"{what} is not a service category") from None
    if kind == "period":
        node = _object(raw, what)
        _known(node, {"start", "end"}, what)
        start = _date(node.get("start"), f"{what}.start")
        end = _date(node.get("end"), f"{what}.end")
        if start > end:
            raise _fail(f"{what} start is after end")
        return ServicePeriod(start=start, end=end)
    raise AssertionError(f"unhandled scalar kind {kind}")  # pragma: no cover


def _ground_truth(raw: object, kind: str, what: str) -> GroundTruth[object]:
    node = _object(raw, what)
    state = node.get("state")
    if state == "absent_on_document":
        _known(node, {"state"}, what)
        return AbsentOnDocument()
    if state == "present":
        _known(node, {"state", "value"}, what)
        if "value" not in node:
            raise _fail(f"{what}.value is required when present")
        return Labeled(_scalar_value(kind, node["value"], f"{what}.value"))
    raise _fail(f"{what}.state must be 'present' or 'absent_on_document'")


def _charge(raw: object, position: int) -> GroundTruthCharge:
    what = f"charges[{position}]"
    node = _object(raw, what)
    _known(node, {"label", "amount", "kind", "cadence"}, what)
    label = _text(node.get("label"), f"{what}.label")
    amount = _money(node.get("amount"), f"{what}.amount")

    kind_raw = node.get("kind", None)
    kind: ChargeKind | None
    if kind_raw is None:
        kind = None
    elif isinstance(kind_raw, str):
        try:
            kind = ChargeKind(kind_raw)
        except ValueError:
            raise _fail(f"{what}.kind is not a charge kind") from None
        if kind is ChargeKind.UNCLASSIFIED:
            # Extractor uncertainty is not a human answer. A labeler who does
            # not know the kind records null (not labeled); UNCLASSIFIED in
            # ground truth would let a hedging prediction score as correct.
            raise _fail(
                f"{what}.kind: UNCLASSIFIED is an extractor uncertainty "
                "state, not human ground truth; use null when the kind is "
                "not labeled")
    else:
        raise _fail(f"{what}.kind must be a charge kind or null")

    cadence_raw = node.get("cadence", None)
    cadence: GroundTruth[Cadence] | None
    if cadence_raw is None:
        cadence = None
    else:
        cadence_node = _object(cadence_raw, f"{what}.cadence")
        state = cadence_node.get("state")
        if state == "absent_on_document":
            _known(cadence_node, {"state"}, f"{what}.cadence")
            cadence = AbsentOnDocument()
        elif state == "present":
            _known(cadence_node, {"state", "value"}, f"{what}.cadence")
            value = cadence_node.get("value")
            if not isinstance(value, str):
                raise _fail(f"{what}.cadence.value must be a cadence")
            try:
                cadence = Labeled(Cadence(value))
            except ValueError:
                raise _fail(f"{what}.cadence.value is not a cadence") from None
        else:
            raise _fail(
                f"{what}.cadence.state must be 'present' or "
                "'absent_on_document'")
    return GroundTruthCharge(label=label, amount=amount, kind=kind, cadence=cadence)


def parse_label(payload: object) -> GroundTruthLabel:
    root = _object(payload, "label")
    _known(root, {
        "label_schema_version", "expected_outcome", "refusal_code",
        "fields", "charges", "promotions",
    }, "label")

    declared = root.get("label_schema_version")
    if declared != LABEL_SCHEMA_VERSION:
        raise EvaluationError(
            EvaluationErrorCode.UNSUPPORTED_LABEL_VERSION,
            f"this evaluator reads label schema {LABEL_SCHEMA_VERSION!r}")

    outcome_raw = root.get("expected_outcome")
    if outcome_raw not in (ExpectedOutcome.SUCCESS.value, ExpectedOutcome.REFUSAL.value):
        raise _fail("expected_outcome must be 'success' or 'refusal'")
    outcome = ExpectedOutcome(outcome_raw)

    if outcome is ExpectedOutcome.REFUSAL:
        code_raw = root.get("refusal_code")
        if not isinstance(code_raw, str):
            raise _fail("refusal_code is required for an expected refusal")
        try:
            code = RefusalCode(code_raw)
        except ValueError:
            raise _fail("refusal_code is not a refusal code") from None
        for forbidden in ("fields", "charges", "promotions"):
            if forbidden in root:
                raise _fail(f"an expected refusal carries no {forbidden}")
        return GroundTruthLabel(
            label_schema_version=LABEL_SCHEMA_VERSION,
            expected_outcome=outcome, refusal_code=code, success=None)

    if "refusal_code" in root:
        raise _fail("an expected success carries no refusal_code")
    fields_node = _object(root.get("fields"), "fields")
    _known(fields_node, set(SCALAR_FIELD_TYPES), "fields")
    scalars: dict[str, GroundTruth[object]] = {}
    for name, kind in SCALAR_FIELD_TYPES.items():
        if name not in fields_node:
            raise _fail(f"fields.{name} must be decided (present or absent)")
        scalars[name] = _ground_truth(fields_node[name], kind, f"fields.{name}")

    charges_raw = root.get("charges")
    if not isinstance(charges_raw, list):
        raise _fail("charges must be a list (empty is allowed)")
    charges = tuple(_charge(item, i) for i, item in enumerate(charges_raw))

    promotions_raw = root.get("promotions")
    if not isinstance(promotions_raw, list):
        raise _fail("promotions must be a list (empty is allowed)")
    promotions: list[GroundTruthPromotion] = []
    for i, item in enumerate(promotions_raw):
        what = f"promotions[{i}]"
        node = _object(item, what)
        _known(node, {"expiry_date", "charge_ref"}, what)
        expiry = _date(node.get("expiry_date"), f"{what}.expiry_date")
        ref_raw = node.get("charge_ref", None)
        if ref_raw is None:
            ref: int | None = None
        elif isinstance(ref_raw, bool) or not isinstance(ref_raw, int):
            raise _fail(f"{what}.charge_ref must be a whole number or null")
        elif not 0 <= ref_raw < len(charges):
            raise _fail(f"{what}.charge_ref points at no labeled charge")
        else:
            ref = ref_raw
        promotions.append(GroundTruthPromotion(expiry_date=expiry, charge_ref=ref))

    return GroundTruthLabel(
        label_schema_version=LABEL_SCHEMA_VERSION,
        expected_outcome=outcome, refusal_code=None,
        success=GroundTruthSuccess(
            scalars=scalars, charges=charges, promotions=tuple(promotions)),
    )


__all__ = [
    "LABEL_SCHEMA_VERSION",
    "SCALAR_FIELD_TYPES",
    "AbsentOnDocument",
    "ExpectedOutcome",
    "GroundTruth",
    "GroundTruthCharge",
    "GroundTruthLabel",
    "GroundTruthPromotion",
    "GroundTruthSuccess",
    "Labeled",
    "parse_label",
]
