"""The strict parser: raw provider JSON → BillExtractionV1 | BillExtractionRefusal.

Provider output is untrusted input (plan §6.1). Three disciplines do the work:

**Nothing is left unread.** Every object consumes its keys or is refused —
the same mechanism `tax_kb/authoring/spec.py`'s reader uses — so a provider
cannot smuggle adapter identity, snippets, or anything else in as data. An
unknown-field refusal reports HOW MANY keys were unknown and what the known
set is; it never echoes a provider-chosen key name, because key names are
provider-controlled text and this module's exceptions must not carry any.

**Numbers are exact or refused.** Money is a decimal STRING with exactly two
fractional digits; confidences and coordinates are decimal strings at up to
six. A float, int, bool, NaN, or Infinity where money belongs is refused as
INEXACT_NUMBER — a JSON number is a float by the time Python sees it, and
silently rounding provider output would make the value on record not the
value extracted. Nothing here quantizes, coerces, or discards.

**One documented normalization, and only one.** Evidence order is
non-semantic: locators are validated, duplicates refused, and the collection
canonical-sorted by (page, y0, x0, y1, x1). No locator or value is dropped.

Exception hygiene: an `ExtractionParseError` carries a closed code, OUR JSON
path, and OUR fixed wording — never a provider value, never bill text. The
leak tests plant sentinel values and assert they cannot escape through any
raise in this module.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TypeVar

from app.services.billshield.extraction.codes import ExtractionParseCode, RefusalCode
from app.services.billshield.extraction.contract import (
    BILL_EXTRACTION_SCHEMA_VERSION,
    EXTRACTION_LIMITS_V1,
    BillExtractionRefusal,
    BillExtractionV1,
    Cadence,
    ChargeCandidate,
    ChargeKind,
    Currency,
    Evidence,
    FieldState,
    NoCandidate,
    Present,
    PromotionCandidate,
    ServiceCategory,
    ServicePeriod,
)
from app.services.ioe.domain import canonical as c

_T = TypeVar("_T")

#: Exactly two fractional digits, optional leading minus. The one shape money
#: may arrive in.
_MONEY_RE = re.compile(r"-?\d+\.\d{2}")
#: A plain decimal (any scale) — used only to tell "right number, wrong
#: scale" apart from "not a number at all".
_PLAIN_DECIMAL_RE = re.compile(r"-?\d+(\.\d+)?")
#: Confidences and normalized coordinates: an UNSIGNED plain decimal with at
#: most six fractional digits. No sign, no exponent, no whitespace, no
#: separators — `Decimal` would happily accept "1e-1" or " +0.5", and a
#: lexical shape the parser did not declare is a coercion in disguise.
_UNIT_INTERVAL_RE = re.compile(r"\d+(\.\d{1,6})?")
_UNIT_INTERVAL_OVERLONG_RE = re.compile(r"\d+\.\d{7,}")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: Charge kinds whose amounts must not be negative under the normalized
#: economic-effect rule. CREDIT must be strictly negative; TAX, PRORATION and
#: UNCLASSIFIED may carry either sign (adjustments, reversals, and honest
#: ignorance respectively).
_NON_NEGATIVE_KINDS = frozenset({
    ChargeKind.RECURRING_FIXED, ChargeKind.USAGE, ChargeKind.ONE_TIME,
    ChargeKind.FEE, ChargeKind.DEVICE_FINANCING,
})
#: Kinds that may carry a cadence candidate. UNCLASSIFIED is included so a
#: printed "/mo" survives a line the model honestly cannot classify.
_CADENCE_KINDS = frozenset({
    ChargeKind.RECURRING_FIXED, ChargeKind.DEVICE_FINANCING,
    ChargeKind.UNCLASSIFIED,
})


class ExtractionParseError(ValueError):
    """A provider payload could not be read as the versioned contract.

    `code` decides; `path` locates; the message is fixed wording that never
    includes a provider value or key name.
    """

    def __init__(self, code: ExtractionParseCode, path: str, note: str = ""):
        self.code = code
        self.path = path
        self.note = note
        suffix = f" ({note})" if note else ""
        super().__init__(f"{code} at {path}{suffix}")


_ABSENT = object()


class _Node:
    """One JSON object; every key must be consumed or the object is refused."""

    def __init__(self, payload: object, path: str):
        if not isinstance(payload, Mapping):
            raise ExtractionParseError(
                ExtractionParseCode.MALFORMED_VALUE, path, "expected an object")
        self._payload: Mapping[str, object] = payload
        self._path = path
        self._seen: set[str] = set()

    def take(self, key: str) -> object:
        self._seen.add(key)
        return self._payload.get(key, _ABSENT)

    def require(self, key: str) -> object:
        value = self.take(key)
        if value is _ABSENT:
            raise ExtractionParseError(
                ExtractionParseCode.MISSING_REQUIRED_FIELD, f"{self._path}.{key}")
        return value

    def done(self) -> None:
        unknown = set(self._payload) - self._seen
        if unknown:
            # COUNT and the known set only: unknown key names are
            # provider-controlled text and must not travel in an exception.
            raise ExtractionParseError(
                ExtractionParseCode.UNKNOWN_FIELD, self._path,
                f"{len(unknown)} unknown field(s); known fields are "
                f"{sorted(self._seen)}")


# ---------------------------------------------------------------------------
# Scalar readers — each fails closed, none echoes the value it refused
# ---------------------------------------------------------------------------
def _read_text(raw: object, path: str, *, max_chars: int, limit_name: str) -> str:
    if not isinstance(raw, str):
        raise ExtractionParseError(
            ExtractionParseCode.MALFORMED_VALUE, path, "expected text")
    normalized = c.normalize_text(raw)
    if not normalized.strip():
        raise ExtractionParseError(
            ExtractionParseCode.MALFORMED_VALUE, path, "empty text")
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in normalized):
        raise ExtractionParseError(
            ExtractionParseCode.MALFORMED_VALUE, path, "control characters")
    if len(normalized) > max_chars:
        raise ExtractionParseError(
            ExtractionParseCode.OUT_OF_RANGE, path,
            f"longer than {limit_name}")
    return normalized


def _refuse_inexact(raw: object, path: str) -> str:
    """Common gate for every numeric field: only a str may proceed."""
    if isinstance(raw, bool) or isinstance(raw, (int, float)):
        raise ExtractionParseError(
            ExtractionParseCode.INEXACT_NUMBER, path,
            "numbers must be exact decimal strings")
    if not isinstance(raw, str):
        raise ExtractionParseError(
            ExtractionParseCode.MALFORMED_VALUE, path, "expected a decimal string")
    return raw


def _read_money(raw: object, path: str) -> Decimal:
    text = _refuse_inexact(raw, path)
    if not _MONEY_RE.fullmatch(text):
        if _PLAIN_DECIMAL_RE.fullmatch(text):
            raise ExtractionParseError(
                ExtractionParseCode.WRONG_SCALE, path,
                "money carries exactly two fractional digits")
        raise ExtractionParseError(
            ExtractionParseCode.INEXACT_NUMBER, path,
            "not a finite decimal string")
    value = Decimal(text)
    try:
        c.money(value)  # the canonical MONEY_MAX bound — no second money limit
    except c.CanonicalizationError as exc:
        raise ExtractionParseError(
            ExtractionParseCode.OUT_OF_RANGE, path,
            "exceeds the canonical money magnitude bound") from exc
    return value


def _read_unit_interval(raw: object, path: str) -> Decimal:
    """A confidence or normalized coordinate: an unsigned plain decimal in
    [0, 1] with at most six fractional digits. The LEXICAL shape is part of
    the contract — scientific notation, signs, whitespace, and separators are
    refused rather than quietly interpreted."""
    text = _refuse_inexact(raw, path)
    if not _UNIT_INTERVAL_RE.fullmatch(text):
        if _UNIT_INTERVAL_OVERLONG_RE.fullmatch(text):
            raise ExtractionParseError(
                ExtractionParseCode.WRONG_SCALE, path, "more than six decimals")
        try:
            parsed = Decimal(text)
        except InvalidOperation as exc:
            raise ExtractionParseError(
                ExtractionParseCode.MALFORMED_VALUE, path,
                "not a plain decimal string") from exc
        if not parsed.is_finite():
            raise ExtractionParseError(
                ExtractionParseCode.INEXACT_NUMBER, path, "not finite")
        raise ExtractionParseError(
            ExtractionParseCode.MALFORMED_VALUE, path,
            "signs, exponents, whitespace and separators are not "
            "plain-decimal syntax")
    value = Decimal(text)
    if value > 1:
        raise ExtractionParseError(
            ExtractionParseCode.OUT_OF_RANGE, path, "outside [0, 1]")
    return value


def _read_date(raw: object, path: str) -> date:
    if not isinstance(raw, str) or not _ISO_DATE_RE.fullmatch(raw):
        raise ExtractionParseError(
            ExtractionParseCode.MALFORMED_VALUE, path, "expected YYYY-MM-DD")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ExtractionParseError(
            ExtractionParseCode.MALFORMED_VALUE, path,
            "not a real calendar date") from exc


def _read_enum(raw: object, path: str, enum_cls: type[_T]) -> _T:
    if isinstance(raw, str):
        try:
            return enum_cls(raw)  # type: ignore[call-arg]
        except ValueError:
            pass
    allowed = sorted(member.value for member in enum_cls)  # type: ignore[attr-defined]
    raise ExtractionParseError(
        ExtractionParseCode.UNKNOWN_ENUM_VALUE, path,
        f"allowed values are {allowed}")


# ---------------------------------------------------------------------------
# Structured readers
# ---------------------------------------------------------------------------
def _read_evidence(raw: object, path: str, *, page_count: int) -> tuple[Evidence, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ExtractionParseError(
            ExtractionParseCode.MALFORMED_VALUE, path, "expected a list")
    if len(raw) == 0:
        raise ExtractionParseError(
            ExtractionParseCode.EVIDENCE_MISSING, path,
            "a present candidate requires at least one locator")
    if len(raw) > EXTRACTION_LIMITS_V1.max_evidence_per_field:
        raise ExtractionParseError(
            ExtractionParseCode.OUT_OF_RANGE, path,
            "longer than EXTRACTION_LIMITS_V1.max_evidence_per_field")
    locators: list[Evidence] = []
    for i, item in enumerate(raw):
        node = _Node(item, f"{path}[{i}]")
        page_raw = node.require("page")
        if isinstance(page_raw, bool) or not isinstance(page_raw, int):
            raise ExtractionParseError(
                ExtractionParseCode.MALFORMED_VALUE, f"{path}[{i}].page",
                "expected a whole number")
        if page_raw < 1 or page_raw > page_count:
            raise ExtractionParseError(
                ExtractionParseCode.EVIDENCE_OUT_OF_BOUNDS, f"{path}[{i}].page",
                "outside the artifact's page count")
        coords = {
            axis: _read_unit_interval(node.require(axis), f"{path}[{i}].{axis}")
            for axis in ("x0", "y0", "x1", "y1")
        }
        node.done()
        if not (coords["x0"] < coords["x1"] and coords["y0"] < coords["y1"]):
            raise ExtractionParseError(
                ExtractionParseCode.EVIDENCE_MALFORMED, f"{path}[{i}]",
                "box requires x0 < x1 and y0 < y1")
        locators.append(Evidence(
            page=page_raw, x0=coords["x0"], y0=coords["y0"],
            x1=coords["x1"], y1=coords["y1"],
        ))
    keys = [locator.sort_key() for locator in locators]
    if len(set(keys)) != len(keys):
        raise ExtractionParseError(
            ExtractionParseCode.EVIDENCE_MALFORMED, path, "duplicate locators")
    # THE one documented normalization: evidence order is non-semantic, so it
    # is canonical-sorted. Nothing is discarded.
    return tuple(sorted(locators, key=Evidence.sort_key))


def _read_state(
    raw: object,
    path: str,
    read_value: Callable[[object, str], _T],
    *,
    page_count: int,
) -> FieldState[_T]:
    node = _Node(raw, path)
    state = node.require("state")
    if state == "no_candidate":
        node.done()
        return NoCandidate()
    if state != "present":
        raise ExtractionParseError(
            ExtractionParseCode.UNKNOWN_FIELD_STATE, f"{path}.state",
            "states are 'present' and 'no_candidate'")
    value = read_value(node.require("value"), f"{path}.value")
    confidence = _read_unit_interval(node.require("confidence"), f"{path}.confidence")
    evidence = _read_evidence(
        node.require("evidence"), f"{path}.evidence", page_count=page_count)
    node.done()
    return Present(value=value, confidence=confidence, evidence=evidence)


def _require_present(
    state: FieldState[_T], path: str, what: str
) -> Present[_T]:
    if isinstance(state, NoCandidate):
        raise ExtractionParseError(
            ExtractionParseCode.MALFORMED_VALUE, path,
            f"{what} must be a present candidate")
    return state


def _read_period(raw: object, path: str) -> ServicePeriod:
    node = _Node(raw, path)
    start = _read_date(node.require("start"), f"{path}.start")
    end = _read_date(node.require("end"), f"{path}.end")
    node.done()
    if start > end:
        raise ExtractionParseError(
            ExtractionParseCode.INCOHERENT_DATES, path, "start is after end")
    return ServicePeriod(start=start, end=end)


def _read_charge(raw: object, path: str, *, page_count: int) -> ChargeCandidate:
    node = _Node(raw, path)
    label = _require_present(
        _read_state(
            node.require("label"), f"{path}.label",
            lambda v, p: _read_text(
                v, p, max_chars=EXTRACTION_LIMITS_V1.max_label_chars,
                limit_name="EXTRACTION_LIMITS_V1.max_label_chars"),
            page_count=page_count),
        f"{path}.label", "a charge label")
    amount = _require_present(
        _read_state(node.require("amount"), f"{path}.amount", _read_money,
                    page_count=page_count),
        f"{path}.amount", "a charge amount")
    kind = _read_enum(node.require("kind"), f"{path}.kind", ChargeKind)
    kind_confidence = _read_unit_interval(
        node.require("kind_confidence"), f"{path}.kind_confidence")
    cadence = _read_state(
        node.require("cadence"), f"{path}.cadence",
        lambda v, p: _read_enum(v, p, Cadence), page_count=page_count)
    service_period = _read_state(
        node.require("service_period"), f"{path}.service_period", _read_period,
        page_count=page_count)
    node.done()

    if kind is ChargeKind.CREDIT and amount.value >= 0:
        raise ExtractionParseError(
            ExtractionParseCode.INCOHERENT_SIGN, f"{path}.amount",
            "a CREDIT decreases the balance owed, so it is strictly negative")
    if kind in _NON_NEGATIVE_KINDS and amount.value < 0:
        raise ExtractionParseError(
            ExtractionParseCode.INCOHERENT_SIGN, f"{path}.amount",
            "this kind may not decrease the balance owed")
    if isinstance(cadence, Present) and kind not in _CADENCE_KINDS:
        raise ExtractionParseError(
            ExtractionParseCode.INCOHERENT_CADENCE, f"{path}.cadence",
            "cadence is only meaningful on recurring-shaped or unclassified "
            "charges")
    return ChargeCandidate(
        label=label, amount=amount, kind=kind, kind_confidence=kind_confidence,
        cadence=cadence, service_period=service_period,
    )


def _read_promotion(
    raw: object, path: str, *, page_count: int, charge_count: int
) -> PromotionCandidate:
    node = _Node(raw, path)
    expiry = _require_present(
        _read_state(node.require("expiry_date"), f"{path}.expiry_date",
                    _read_date, page_count=page_count),
        f"{path}.expiry_date", "a promotion expiry")
    index_raw = node.require("charge_index")
    node.done()
    if index_raw is None:
        return PromotionCandidate(expiry_date=expiry, charge_index=None)
    if isinstance(index_raw, bool) or not isinstance(index_raw, int):
        raise ExtractionParseError(
            ExtractionParseCode.MALFORMED_VALUE, f"{path}.charge_index",
            "expected a whole number or null")
    if not 0 <= index_raw < charge_count:
        raise ExtractionParseError(
            ExtractionParseCode.INVALID_CHARGE_REFERENCE,
            f"{path}.charge_index", "no such charge")
    return PromotionCandidate(expiry_date=expiry, charge_index=index_raw)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_extraction_result(
    payload: object, *, page_count: int
) -> BillExtractionV1 | BillExtractionRefusal:
    """Read one raw provider result against the versioned contract.

    `page_count` is the validated artifact's actual page count — evidence
    pages are checked against it, so a locator cannot point outside the
    document that was actually processed.
    """
    envelope = _Node(payload, "$")
    declared = envelope.require("schema_version")
    if declared != BILL_EXTRACTION_SCHEMA_VERSION:
        raise ExtractionParseError(
            ExtractionParseCode.UNSUPPORTED_SCHEMA_VERSION, "$.schema_version",
            f"this parser reads {BILL_EXTRACTION_SCHEMA_VERSION!r}; an output "
            "written against a different contract is refused, not reinterpreted")

    refusal_raw = envelope.take("refusal")
    extraction_raw = envelope.take("extraction")
    envelope.done()
    if (refusal_raw is _ABSENT) == (extraction_raw is _ABSENT):
        raise ExtractionParseError(
            ExtractionParseCode.MALFORMED_VALUE, "$",
            "exactly one of 'refusal' and 'extraction' is required")

    if refusal_raw is not _ABSENT:
        node = _Node(refusal_raw, "$.refusal")
        code = _read_enum(node.require("code"), "$.refusal.code", RefusalCode)
        node.done()
        return BillExtractionRefusal(
            schema_version=BILL_EXTRACTION_SCHEMA_VERSION, code=code)

    body = _Node(extraction_raw, "$.extraction")
    currency_raw = body.require("currency")
    if currency_raw != Currency.CAD.value:
        raise ExtractionParseError(
            ExtractionParseCode.UNSUPPORTED_CURRENCY, "$.extraction.currency",
            "CAD only in MVP")

    def state(key: str, read_value: Callable[[object, str], _T]) -> FieldState[_T]:
        return _read_state(
            body.require(key), f"$.extraction.{key}", read_value,
            page_count=page_count)

    issuer = state(
        "bill_issuer_name",
        lambda v, p: _read_text(
            v, p, max_chars=EXTRACTION_LIMITS_V1.max_issuer_name_chars,
            limit_name="EXTRACTION_LIMITS_V1.max_issuer_name_chars"))
    category = state(
        "service_category", lambda v, p: _read_enum(v, p, ServiceCategory))
    statement_date = state("statement_date", _read_date)
    billing_period = state("billing_period", _read_period)
    amount_due = state("amount_due", _read_money)
    previous_balance = state("previous_balance", _read_money)
    payments_applied = state("payments_applied", _read_money)
    subtotal_before_tax = state("subtotal_before_tax", _read_money)
    total_tax = state("total_tax", _read_money)

    charges_raw = body.require("charges")
    if not isinstance(charges_raw, Sequence) or isinstance(charges_raw, (str, bytes)):
        raise ExtractionParseError(
            ExtractionParseCode.MALFORMED_VALUE, "$.extraction.charges",
            "expected a list")
    if len(charges_raw) > EXTRACTION_LIMITS_V1.max_charges:
        raise ExtractionParseError(
            ExtractionParseCode.OUT_OF_RANGE, "$.extraction.charges",
            "longer than EXTRACTION_LIMITS_V1.max_charges")
    charges = tuple(
        _read_charge(item, f"$.extraction.charges[{i}]", page_count=page_count)
        for i, item in enumerate(charges_raw))

    promotions_raw = body.require("promotions")
    if not isinstance(promotions_raw, Sequence) or isinstance(promotions_raw, (str, bytes)):
        raise ExtractionParseError(
            ExtractionParseCode.MALFORMED_VALUE, "$.extraction.promotions",
            "expected a list")
    if len(promotions_raw) > EXTRACTION_LIMITS_V1.max_promotions:
        raise ExtractionParseError(
            ExtractionParseCode.OUT_OF_RANGE, "$.extraction.promotions",
            "longer than EXTRACTION_LIMITS_V1.max_promotions")
    promotions = tuple(
        _read_promotion(
            item, f"$.extraction.promotions[{i}]", page_count=page_count,
            charge_count=len(charges))
        for i, item in enumerate(promotions_raw))
    body.done()

    if isinstance(payments_applied, Present) and payments_applied.value > 0:
        raise ExtractionParseError(
            ExtractionParseCode.INCOHERENT_SIGN,
            "$.extraction.payments_applied",
            "payments decrease the balance owed, so the normalized value is "
            "zero or negative")

    if not charges and isinstance(amount_due, NoCandidate):
        raise ExtractionParseError(
            ExtractionParseCode.EMPTY_SUCCESS, "$.extraction",
            "no amount_due candidate and no charges; the adapter should have "
            "refused with NO_USABLE_EXTRACTION")

    return BillExtractionV1(
        schema_version=BILL_EXTRACTION_SCHEMA_VERSION,
        currency=Currency.CAD,
        bill_issuer_name=issuer,
        service_category=category,
        statement_date=statement_date,
        billing_period=billing_period,
        amount_due=amount_due,
        previous_balance=previous_balance,
        payments_applied=payments_applied,
        subtotal_before_tax=subtotal_before_tax,
        total_tax=total_tax,
        charges=charges,
        promotions=promotions,
    )


__all__ = ["ExtractionParseError", "parse_extraction_result"]
