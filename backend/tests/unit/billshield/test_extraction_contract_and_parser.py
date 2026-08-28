"""BillExtractionV1 and its strict parser — the contract, enforced.

The builder below produces a fresh minimal-valid payload per call; every
refusal test mutates its own copy, so no test order can matter. Sentinel
values planted in payloads are asserted absent from every raised exception —
provider content must never travel in an error.
"""
from __future__ import annotations

import copy
import random
from decimal import Decimal

import pytest

from app.services.billshield.extraction.codes import ExtractionParseCode, RefusalCode
from app.services.billshield.extraction.contract import (
    BILL_EXTRACTION_SCHEMA_VERSION,
    EXTRACTION_LIMITS_V1,
    BillExtractionRefusal,
    BillExtractionV1,
    Cadence,
    ChargeKind,
    NoCandidate,
    Present,
)
from app.services.billshield.extraction.parser import (
    ExtractionParseError,
    parse_extraction_result,
)


def _ev(page: int = 1, x0: str = "0.1", y0: str = "0.2",
        x1: str = "0.5", y1: str = "0.25") -> dict:
    return {"page": page, "x0": x0, "y0": y0, "x1": x1, "y1": y1}


def _present(value: object, confidence: str = "0.9",
             evidence: list | None = None) -> dict:
    return {"state": "present", "value": value, "confidence": confidence,
            "evidence": evidence if evidence is not None else [_ev()]}


NO = {"state": "no_candidate"}


def _charge(label: str = "Forfait mobile 5G", amount: str = "75.00",
            kind: str = "RECURRING_FIXED", cadence: dict | None = None) -> dict:
    return {
        "label": _present(label),
        "amount": _present(amount),
        "kind": kind,
        "kind_confidence": "0.9",
        "cadence": cadence if cadence is not None else _present("MONTHLY"),
        "service_period": dict(NO),
    }


def _payload() -> dict:
    return {
        "schema_version": BILL_EXTRACTION_SCHEMA_VERSION,
        "extraction": {
            "currency": "CAD",
            "bill_issuer_name": _present("Maple Télécom"),
            "service_category": _present("MOBILE"),
            "statement_date": _present("2026-07-15"),
            "billing_period": _present({"start": "2026-07-01", "end": "2026-07-31"}),
            "amount_due": _present("91.25"),
            "previous_balance": dict(NO),
            "payments_applied": _present("-80.00"),
            "subtotal_before_tax": dict(NO),
            "total_tax": dict(NO),
            "charges": [_charge()],
            "promotions": [
                {"expiry_date": _present("2026-09-30"), "charge_index": 0},
            ],
        },
    }


def _refused(payload: object, *, page_count: int = 3) -> ExtractionParseError:
    with pytest.raises(ExtractionParseError) as caught:
        parse_extraction_result(payload, page_count=page_count)
    return caught.value


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------
def test_a_full_payload_round_trips_with_a_stable_identity():
    first = parse_extraction_result(_payload(), page_count=3)
    second = parse_extraction_result(_payload(), page_count=3)
    assert isinstance(first, BillExtractionV1)
    assert first == second
    assert first.extraction_identity() == second.extraction_identity()
    assert first.charges[0].amount.value == Decimal("75.00")


def test_a_partial_payload_is_a_valid_success():
    """A readable bill can lack a statement date or period; no_candidate is a
    legitimate state, never an unreadable-document refusal."""
    payload = _payload()
    payload["extraction"]["statement_date"] = dict(NO)
    payload["extraction"]["billing_period"] = dict(NO)
    result = parse_extraction_result(payload, page_count=3)
    assert isinstance(result, BillExtractionV1)
    assert isinstance(result.statement_date, NoCandidate)
    assert isinstance(result.amount_due, Present)


def test_no_candidate_never_claims_document_absence():
    """The docstring is the contract; the canonical form carries only the
    extractor's statement — no absence claim exists to leak downstream."""
    assert "never" in (NoCandidate.__doc__ or "").lower()
    payload = _payload()
    payload["extraction"]["statement_date"] = dict(NO)
    result = parse_extraction_result(payload, page_count=3)
    assert isinstance(result, BillExtractionV1)
    canonical = result.as_canonical()
    state = canonical["statement_date"]
    assert state == {"state": "no_candidate"}, (
        "no_candidate must serialize as exactly itself — any richer claim "
        "would be an absence statement the extractor cannot make")


def test_a_refusal_is_a_separate_type_with_only_a_code():
    result = parse_extraction_result(
        {"schema_version": "1.0.0", "refusal": {"code": "NO_USABLE_EXTRACTION"}},
        page_count=1)
    assert isinstance(result, BillExtractionRefusal)
    assert result.code is RefusalCode.NO_USABLE_EXTRACTION


def test_multiple_promotions_may_share_one_charge():
    payload = _payload()
    payload["extraction"]["promotions"] = [
        {"expiry_date": _present("2026-09-30"), "charge_index": 0},
        {"expiry_date": _present("2026-12-31"), "charge_index": 0},
        {"expiry_date": _present("2027-01-31"), "charge_index": None},
    ]
    result = parse_extraction_result(_payload() | payload, page_count=3)
    assert isinstance(result, BillExtractionV1)
    assert len(result.promotions) == 3
    assert result.promotions[2].charge_index is None


def test_account_suffix_exists_nowhere_in_the_v1_contract():
    """Removed by decision: identifying data whose valid shape depends on the
    first real provider. Reconsidered only with the Slice 2 privacy review."""
    from dataclasses import fields

    from app.services.billshield.evaluation.labels import SCALAR_FIELD_TYPES

    assert not any("suffix" in f.name for f in fields(BillExtractionV1))
    assert not any("suffix" in name for name in SCALAR_FIELD_TYPES)


# ---------------------------------------------------------------------------
# Refusals — every closed code, one counterexample each
# ---------------------------------------------------------------------------
def test_unsupported_schema_version_is_refused():
    payload = _payload()
    payload["schema_version"] = "2.0.0"
    assert _refused(payload).code is ExtractionParseCode.UNSUPPORTED_SCHEMA_VERSION


def test_unknown_fields_are_refused_at_every_level():
    for mutate in (
        lambda p: p.update({"extra": 1}),
        lambda p: p["extraction"].update({"account_number": "12345678"}),
        lambda p: p["extraction"]["amount_due"].update({"snippet": "leak"}),
        lambda p: p["extraction"]["charges"][0].update({"note": "x"}),
        lambda p: p["extraction"]["amount_due"]["evidence"][0].update({"text": "x"}),
        lambda p: p["extraction"]["promotions"][0].update({"reason": "x"}),
    ):
        payload = _payload()
        mutate(payload)
        assert _refused(payload).code is ExtractionParseCode.UNKNOWN_FIELD


def test_adapter_identity_cannot_arrive_as_data():
    """adapter_code/model_version/prompt_version are trusted provenance on
    the Protocol; a payload carrying them is refused unread."""
    for key in ("adapter_code", "model_version", "prompt_version"):
        payload = _payload()
        payload["extraction"][key] = "sneaky"
        assert _refused(payload).code is ExtractionParseCode.UNKNOWN_FIELD


def test_every_declared_key_must_be_present():
    payload = _payload()
    del payload["extraction"]["total_tax"]
    error = _refused(payload)
    assert error.code is ExtractionParseCode.MISSING_REQUIRED_FIELD
    assert "total_tax" in error.path


def test_unknown_field_state_is_refused():
    payload = _payload()
    payload["extraction"]["statement_date"] = {"state": "absent_on_document"}
    assert _refused(payload).code is ExtractionParseCode.UNKNOWN_FIELD_STATE


def test_inexact_numbers_are_refused():
    for bad in (75.0, 75, True, "NaN", "Infinity", "-Infinity"):
        payload = _payload()
        payload["extraction"]["charges"][0]["amount"]["value"] = bad
        assert _refused(payload).code is ExtractionParseCode.INEXACT_NUMBER, bad


def test_money_scale_must_be_exactly_two_decimals():
    for bad in ("75", "75.0", "75.000"):
        payload = _payload()
        payload["extraction"]["amount_due"]["value"] = bad
        assert _refused(payload).code is ExtractionParseCode.WRONG_SCALE, bad


def test_confidence_bounds_and_scale():
    payload = _payload()
    payload["extraction"]["amount_due"]["confidence"] = "1.1"
    assert _refused(payload).code is ExtractionParseCode.OUT_OF_RANGE
    payload = _payload()
    payload["extraction"]["amount_due"]["confidence"] = "0.1234567"
    assert _refused(payload).code is ExtractionParseCode.WRONG_SCALE
    payload = _payload()
    payload["extraction"]["amount_due"]["confidence"] = 0.9
    assert _refused(payload).code is ExtractionParseCode.INEXACT_NUMBER


def test_money_magnitude_reuses_the_canonical_bound():
    """One money authority: the canonicalizer's MONEY_MAX, no second limit."""
    from app.services.ioe.domain import canonical as c

    over = Decimal("9999999999999.99")     # 13 integer digits at 2dp
    assert over > c.MONEY_MAX, "the probe no longer exceeds the shared bound"
    payload = _payload()
    payload["extraction"]["amount_due"]["value"] = str(over)
    assert _refused(payload).code is ExtractionParseCode.OUT_OF_RANGE


def test_unsupported_currency_is_refused():
    payload = _payload()
    payload["extraction"]["currency"] = "USD"
    assert _refused(payload).code is ExtractionParseCode.UNSUPPORTED_CURRENCY


def test_unknown_enum_values_are_refused():
    payload = _payload()
    payload["extraction"]["service_category"]["value"] = "UTILITIES"
    assert _refused(payload).code is ExtractionParseCode.UNKNOWN_ENUM_VALUE
    payload = _payload()
    payload["extraction"]["charges"][0]["kind"] = "RECURRING"
    assert _refused(payload).code is ExtractionParseCode.UNKNOWN_ENUM_VALUE


def test_a_printed_unsupported_cadence_is_other_not_missing():
    payload = _payload()
    payload["extraction"]["charges"][0]["cadence"] = _present("OTHER")
    result = parse_extraction_result(payload, page_count=3)
    assert isinstance(result, BillExtractionV1)
    cadence = result.charges[0].cadence
    assert isinstance(cadence, Present) and cadence.value is Cadence.OTHER
    assert cadence.evidence, "OTHER is evidence-backed, not a shrug"


def test_incoherent_dates_are_refused():
    payload = _payload()
    payload["extraction"]["billing_period"]["value"] = {
        "start": "2026-07-31", "end": "2026-07-01"}
    assert _refused(payload).code is ExtractionParseCode.INCOHERENT_DATES


def test_the_sign_table_is_enforced():
    # CREDIT strictly negative
    payload = _payload()
    payload["extraction"]["charges"].append(_charge(
        label="Loyalty credit", amount="10.00", kind="CREDIT", cadence=dict(NO)))
    assert _refused(payload).code is ExtractionParseCode.INCOHERENT_SIGN
    # non-negative kinds reject negatives
    payload = _payload()
    payload["extraction"]["charges"][0]["amount"]["value"] = "-75.00"
    assert _refused(payload).code is ExtractionParseCode.INCOHERENT_SIGN
    # payments_applied is zero or negative
    payload = _payload()
    payload["extraction"]["payments_applied"]["value"] = "80.00"
    assert _refused(payload).code is ExtractionParseCode.INCOHERENT_SIGN
    # TAX and PRORATION may carry either sign — reversals exist
    payload = _payload()
    payload["extraction"]["charges"].append(_charge(
        label="Tax adjustment", amount="-1.13", kind="TAX", cadence=dict(NO)))
    payload["extraction"]["charges"].append(_charge(
        label="Prorated days", amount="-4.50", kind="PRORATION", cadence=dict(NO)))
    assert isinstance(
        parse_extraction_result(payload, page_count=3), BillExtractionV1)


def test_unclassified_preserves_the_candidate_without_a_sign_guess():
    payload = _payload()
    payload["extraction"]["charges"].append(_charge(
        label="Mystery line", amount="-3.00", kind="UNCLASSIFIED",
        cadence=dict(NO)))
    result = parse_extraction_result(payload, page_count=3)
    assert isinstance(result, BillExtractionV1)
    assert result.charges[1].kind is ChargeKind.UNCLASSIFIED


def test_cadence_is_incoherent_on_non_recurring_kinds():
    payload = _payload()
    payload["extraction"]["charges"].append(_charge(
        label="One time", amount="5.00", kind="ONE_TIME",
        cadence=_present("MONTHLY")))
    assert _refused(payload).code is ExtractionParseCode.INCOHERENT_CADENCE


def test_promotion_charge_reference_is_bounds_checked():
    payload = _payload()
    payload["extraction"]["promotions"][0]["charge_index"] = 5
    assert _refused(payload).code is ExtractionParseCode.INVALID_CHARGE_REFERENCE


def test_empty_success_is_impossible():
    payload = _payload()
    payload["extraction"]["charges"] = []
    payload["extraction"]["amount_due"] = dict(NO)
    payload["extraction"]["promotions"] = []
    assert _refused(payload).code is ExtractionParseCode.EMPTY_SUCCESS
    # amount_due alone is minimum useful content
    payload["extraction"]["amount_due"] = _present("42.00")
    result = parse_extraction_result(payload, page_count=3)
    assert isinstance(result, BillExtractionV1)


def test_evidence_is_required_bounded_and_validated():
    payload = _payload()
    payload["extraction"]["amount_due"]["evidence"] = []
    assert _refused(payload).code is ExtractionParseCode.EVIDENCE_MISSING
    payload = _payload()
    payload["extraction"]["amount_due"]["evidence"] = [
        _ev(x0="0.9", x1="0.1")]
    assert _refused(payload).code is ExtractionParseCode.EVIDENCE_MALFORMED
    payload = _payload()
    payload["extraction"]["amount_due"]["evidence"] = [_ev(), _ev()]
    assert _refused(payload).code is ExtractionParseCode.EVIDENCE_MALFORMED
    payload = _payload()
    payload["extraction"]["amount_due"]["evidence"] = [_ev(page=9)]
    assert _refused(
        payload, page_count=3).code is ExtractionParseCode.EVIDENCE_OUT_OF_BOUNDS
    payload = _payload()
    payload["extraction"]["amount_due"]["evidence"] = [
        _ev(y0=f"0.{i:03d}", y1=f"0.9{i:02d}")
        for i in range(EXTRACTION_LIMITS_V1.max_evidence_per_field + 1)
    ]
    assert _refused(payload).code is ExtractionParseCode.OUT_OF_RANGE


def test_structural_limits_are_named_and_enforced():
    payload = _payload()
    payload["extraction"]["charges"][0]["label"]["value"] = (
        "x" * (EXTRACTION_LIMITS_V1.max_label_chars + 1))
    error = _refused(payload)
    assert error.code is ExtractionParseCode.OUT_OF_RANGE
    assert "max_label_chars" in str(error)


def test_bilingual_text_survives_and_control_characters_do_not():
    accented = "Frais d'itinérance – États-Unis"
    payload = _payload()
    payload["extraction"]["charges"][0]["label"]["value"] = accented
    result = parse_extraction_result(payload, page_count=3)
    assert isinstance(result, BillExtractionV1)
    assert "é" in result.charges[0].label.value, "French was ASCII-folded"

    payload = _payload()
    payload["extraction"]["charges"][0]["label"]["value"] = "bad\x00label"
    assert _refused(payload).code is ExtractionParseCode.MALFORMED_VALUE


def test_shuffled_evidence_canonicalizes_identically():
    """Evidence order is non-semantic: the parser's canonical sort makes any
    input order yield the same value and the same identity hash."""
    locators = [
        _ev(page=2, y0="0.10", y1="0.15"),
        _ev(page=1, y0="0.90", y1="0.95"),
        _ev(page=1, y0="0.10", y1="0.15"),
        _ev(page=3, y0="0.50", y1="0.55"),
    ]
    identities = set()
    rng = random.Random(42)
    for _ in range(6):
        shuffled = list(locators)
        rng.shuffle(shuffled)
        payload = _payload()
        payload["extraction"]["amount_due"]["evidence"] = copy.deepcopy(shuffled)
        result = parse_extraction_result(payload, page_count=3)
        assert isinstance(result, BillExtractionV1)
        identities.add(result.extraction_identity())
    assert len(identities) == 1, "evidence input order leaked into the hash"


def test_exceptions_never_carry_provider_values():
    """Sentinels planted as VALUES in refused payloads must not surface in
    the raised error — the same discipline the document-extraction refusal
    test pins for tax slips."""
    sentinel = "SENTINEL-9F3A2B-ACCT-4111111111111111"
    mutations = [
        lambda p: p["extraction"]["charges"][0]["amount"].__setitem__(
            "value", sentinel),
        lambda p: p["extraction"]["statement_date"].__setitem__(
            "value", sentinel),
        lambda p: p["extraction"]["charges"][0].__setitem__("kind", sentinel),
        lambda p: p["extraction"].__setitem__("currency", sentinel),
        lambda p: p["extraction"]["charges"][0]["label"].__setitem__(
            "value", f"bad\x00{sentinel}"),
        lambda p: p["extraction"].__setitem__(sentinel, "1"),  # unknown KEY
    ]
    for mutate in mutations:
        payload = _payload()
        mutate(payload)
        error = _refused(payload)
        rendered = f"{error!r} {error!s} {error.__cause__!r} {error.note}"
        assert sentinel not in rendered, rendered


def test_unit_interval_syntax_is_lexically_strict():
    """Confidences and coordinates are UNSIGNED PLAIN decimals: Decimal's
    tolerance for exponents, signs, whitespace and separators is not part of
    the wire contract, and the value never travels in the refusal."""
    accepted = ("0", "1", "0.5", "1.000000", "0.123456")
    for good in accepted:
        payload = _payload()
        payload["extraction"]["amount_due"]["confidence"] = good
        result = parse_extraction_result(payload, page_count=3)
        assert isinstance(result, BillExtractionV1), good

    rejected = {
        "1e-1": ExtractionParseCode.MALFORMED_VALUE,
        "1E-1": ExtractionParseCode.MALFORMED_VALUE,
        "+0.5": ExtractionParseCode.MALFORMED_VALUE,
        "-0.5": ExtractionParseCode.MALFORMED_VALUE,
        " 0.5": ExtractionParseCode.MALFORMED_VALUE,
        "0.5 ": ExtractionParseCode.MALFORMED_VALUE,
        "0_5": ExtractionParseCode.MALFORMED_VALUE,
        "NaN": ExtractionParseCode.INEXACT_NUMBER,
        "Infinity": ExtractionParseCode.INEXACT_NUMBER,
        "0.1234567": ExtractionParseCode.WRONG_SCALE,
    }
    for bad, code in rejected.items():
        payload = _payload()
        payload["extraction"]["amount_due"]["confidence"] = bad
        error = _refused(payload)
        assert error.code is code, (bad, error.code)
        assert bad.strip() not in (error.note or "zz-never"), (
            "the rejected provider value travelled in the exception")
