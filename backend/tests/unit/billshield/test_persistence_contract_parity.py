"""The schema and the committed extraction contract say the same thing.

Two obligations, both of which fail loudly rather than drifting:

1. **Vocabulary parity.** Every closed code the schema persists has exactly one
   Python authority, and the SQL constraint lists exactly that authority's
   members. A `CHECK (code ~ '^[A-Z][A-Z0-9_]{2,63}$')` would pass every test
   in the repository while accepting `WIDGET_FROBNICATED`, so the comparison is
   against the VALUE LIST parsed out of the constraint, both directions.

2. **Lossless round trip.** A `BillExtractionV1` mapped to rows and read back
   reproduces its `extraction_identity()` byte for byte. That is the strongest
   statement available about persistence fidelity: the hash covers every field,
   every charge in document order, every promotion in occurrence order, each
   evidence locator, and the exact Decimal scale of every money and confidence
   value. Drop a promotion, re-sort the charges, or round a coordinate and the
   hash changes.

The round trip is deliberately computed WITHOUT a database in this file: the
mapping under test is the pure functions below, and the security suite proves
the same shapes are what the schema accepts. A test that needed a live
PostgreSQL to check a hash would be slower and would prove less.
"""
from __future__ import annotations

import datetime
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.billshield.domain.states import (
    OUTBOX_MAX_ATTEMPTS,
    STORAGE_KEY_INFIX,
    BillState,
    ExtractionRunState,
    OutboxClaimState,
    OutboxTaskCode,
)
from app.services.billshield.extraction.codes import ExtractionParseCode, RefusalCode
from app.services.billshield.extraction.contract import (
    EXTRACTION_LIMITS_V1,
    Cadence,
    ChargeCandidate,
    ChargeKind,
    Currency,
    Evidence,
    NoCandidate,
    Present,
    PromotionCandidate,
    ServiceCategory,
    ServicePeriod,
)
from app.services.billshield.extraction.parser import parse_extraction_result
from app.services.billshield.extraction.ports import ArtifactFormat

SQL_FILE = Path(__file__).resolve().parents[3] / "db/sql/67_billshield_foundation.sql"
SQL = SQL_FILE.read_text()


def _check_values(constraint: str) -> set[str]:
    """The literal value list of a named `IN (...)` CHECK constraint.

    Parsed from the canonical SQL rather than from a live database so the test
    fails on the file a reviewer reads, and fails in CI without PostgreSQL.
    """
    match = re.search(
        rf"CONSTRAINT {constraint} CHECK \([^()]*IN \(([^)]*)\)", SQL, re.S)
    assert match, f"no IN-list CHECK named {constraint} in {SQL_FILE.name}"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def _domain_check_values(fragment: str) -> set[str]:
    """Value list of an inline `CHECK (col IN (...))` written without a name."""
    match = re.search(rf"{fragment} IN \(([^)]*)\)", SQL, re.S)
    assert match, f"no IN-list matching {fragment!r}"
    return set(re.findall(r"'([^']+)'", match.group(1)))


# ------------------------------------------------------------ vocabulary parity

def test_the_bill_status_constraint_equals_the_bill_state_authority():
    assert _check_values("ck_billshield_bill_status") == {s.value for s in BillState}


def test_the_extraction_run_status_constraint_equals_its_authority():
    assert _check_values("ck_billshield_extraction_run_status") == {
        s.value for s in ExtractionRunState}


def test_the_refusal_code_constraint_equals_the_committed_refusal_codes():
    """Reused, not restated: RefusalCode is the Slice 1 authority."""
    assert _check_values("ck_billshield_extraction_run_refusal_code") == {
        c.value for c in RefusalCode}


def test_the_failure_code_constraint_equals_the_committed_parse_codes():
    """The OTHER committed authority, and a different one from refusal.

    `refused` means the extractor read the document and declined it whole;
    `failed` means the provider's output did not survive the strict parse. Two
    outcomes, two vocabularies, both already owned by Slice 1 — so the schema
    reuses them rather than inventing a third enum or a regex.
    """
    assert _check_values("ck_billshield_extraction_run_failure_code") == {
        c.value for c in ExtractionParseCode}


def test_the_two_outcome_vocabularies_do_not_overlap():
    """A code that meant both would make the status column ambiguous."""
    assert not ({c.value for c in RefusalCode}
                & {c.value for c in ExtractionParseCode})


def test_the_charge_kind_constraint_equals_the_contract_enum():
    assert _check_values("ck_billshield_charge_kind") == {k.value for k in ChargeKind}


def test_the_cadence_constraint_equals_the_contract_enum():
    assert _check_values("ck_billshield_charge_cadence_value") == {
        c.value for c in Cadence}


def test_both_service_category_constraints_equal_the_contract_enum():
    """The catalogue table and the extracted candidate use ONE vocabulary."""
    expected = {c.value for c in ServiceCategory}
    assert _check_values("ck_billshield_provider_category_value") == expected
    assert _check_values("ck_billshield_run_service_category_value") == expected


def test_the_artifact_format_constraint_equals_the_ports_enum():
    assert _check_values("ck_billshield_bill_artifact_format") == {
        f.value for f in ArtifactFormat}


def test_the_task_code_constraint_is_exactly_extract_bill():
    assert _check_values("ck_billshield_job_outbox_task_code") == {
        c.value for c in OutboxTaskCode} == {"EXTRACT_BILL"}


def test_the_claim_state_constraint_equals_its_authority():
    assert _check_values("ck_billshield_job_outbox_claim_state") == {
        s.value for s in OutboxClaimState}


def test_the_currency_constraint_is_the_single_supported_currency():
    assert Currency.CAD.value == "CAD"
    assert "CHECK (currency = 'CAD')" in SQL
    assert len(list(Currency)) == 1, "a new currency needs a schema decision too"


def test_the_attempt_ceiling_matches_the_declared_bound():
    match = re.search(r"ck_billshield_job_outbox_attempts CHECK \(attempts BETWEEN "
                      r"(\d+) AND (\d+)\)", SQL)
    assert match, "no attempts bound in the schema"
    assert (int(match.group(1)), int(match.group(2))) == (0, OUTBOX_MAX_ATTEMPTS)


def test_the_evidence_bound_matches_the_contract_limit():
    """The schema's upper bound IS the contract's max_evidence_per_field."""
    match = re.search(r"jsonb_array_length\(VALUE\) BETWEEN (\d+) AND (\d+)", SQL)
    assert match, "no evidence array bound in the schema"
    assert int(match.group(2)) == EXTRACTION_LIMITS_V1.max_evidence_per_field
    assert int(match.group(1)) == 1


def test_the_label_and_issuer_length_bounds_match_the_contract_limits():
    assert f"BETWEEN 1 AND {EXTRACTION_LIMITS_V1.max_label_chars}" in SQL
    assert f"BETWEEN 1 AND {EXTRACTION_LIMITS_V1.max_issuer_name_chars}" in SQL


def test_the_generated_storage_key_uses_the_declared_infix():
    assert f"'/{STORAGE_KEY_INFIX}/'" in SQL


def test_the_schema_persists_no_code_without_a_python_authority():
    """A regex-only code column is the failure mode this test exists for.

    Every `_code` column in the schema is listed here with the authority that
    closes it. `job_outbox` deliberately has no failure-code column: its
    authority is the Slice 3 worker's, and an open uppercase text column now
    would be a closed code in name only.
    """
    columns = set(re.findall(r"^\s+(\w*code\w*)\s+text", SQL, re.M))
    assert columns == {"refusal_code", "failure_code", "task_code",
                       "adapter_code", "code"}, (
        f"a code column changed: {sorted(columns)}")
    # Asserted on DECLARATIONS, not on the file text: the header comment
    # explaining this absence mentions the name, and a substring search would
    # match the explanation rather than the thing it explains.
    assert not re.search(r"^\s+last_error_code\s+text", SQL, re.M), (
        "an outbox failure-code column appeared without its Python authority")
    # The outbox's own failure vocabulary is the one still deferred: it belongs
    # to the Slice 3 runtime and has no committed authority yet. The extraction
    # parse vocabulary DOES have one, which is why failure_code exists on
    # extraction_run and nowhere else.
    outbox = SQL[SQL.index("CREATE TABLE billshield.job_outbox"):]
    outbox = outbox[:outbox.index(");")]
    assert "failure_code" not in outbox and "error_code" not in outbox


# ------------------------------------------------------------ lossless mapping

def _evidence_rows(evidence: tuple[Evidence, ...]) -> list[dict]:
    """Contract evidence -> the JSONB the schema accepts, order preserved."""
    return [
        {"page": e.page, "x0": str(e.x0), "y0": str(e.y0),
         "x1": str(e.x1), "y1": str(e.y1)}
        for e in evidence
    ]


def _field_columns(state) -> dict:
    """A FieldState -> its value/confidence/evidence triple.

    `NoCandidate` becomes all-NULL, which records that the EXTRACTOR produced
    nothing. It is never a claim about what the document contains.
    """
    if isinstance(state, NoCandidate):
        return {"value": None, "confidence": None, "evidence": None}
    return {"value": state.value, "confidence": state.confidence,
            "evidence": _evidence_rows(state.evidence)}


def _rows_from(extraction) -> dict:
    """The whole extraction, as the rows the seven-table schema would hold."""
    run = {
        "extraction_schema_version": extraction.schema_version,
        "currency": extraction.currency.value,
    }
    for name in ("bill_issuer_name", "service_category", "statement_date",
                 "amount_due", "previous_balance", "payments_applied",
                 "subtotal_before_tax", "total_tax"):
        run[name] = _field_columns(getattr(extraction, name))
    period = extraction.billing_period
    if isinstance(period, NoCandidate):
        run["billing_period"] = {"start": None, "end": None,
                                 "confidence": None, "evidence": None}
    else:
        run["billing_period"] = {
            "start": period.value.start, "end": period.value.end,
            "confidence": period.confidence,
            "evidence": _evidence_rows(period.evidence)}

    charges = []
    for position, charge in enumerate(extraction.charges):
        row = {
            "position": position,
            "label_text": charge.label.value,
            "label_confidence": charge.label.confidence,
            "label_evidence": _evidence_rows(charge.label.evidence),
            "amount": charge.amount.value,
            "amount_confidence": charge.amount.confidence,
            "amount_evidence": _evidence_rows(charge.amount.evidence),
            "kind": charge.kind.value,
            "kind_confidence": charge.kind_confidence,
        }
        row["cadence"] = _field_columns(charge.cadence)
        if isinstance(charge.cadence, Present):
            row["cadence"]["value"] = charge.cadence.value.value
        sp = charge.service_period
        row["service_period"] = (
            {"start": None, "end": None, "confidence": None, "evidence": None}
            if isinstance(sp, NoCandidate) else
            {"start": sp.value.start, "end": sp.value.end,
             "confidence": sp.confidence, "evidence": _evidence_rows(sp.evidence)})
        charges.append(row)

    promotions = [
        {
            "position": position,
            "charge_position": promo.charge_index,
            "expiry_date": promo.expiry_date.value,
            "expiry_confidence": promo.expiry_date.confidence,
            "expiry_evidence": _evidence_rows(promo.expiry_date.evidence),
        }
        for position, promo in enumerate(extraction.promotions)
    ]
    return {"run": run, "charges": charges, "promotions": promotions}


def _extraction_from(rows: dict):
    """Rebuild the contract object from those rows — the other direction."""
    from app.services.billshield.extraction.contract import BillExtractionV1

    def evidence(raw) -> tuple[Evidence, ...]:
        return tuple(
            Evidence(page=e["page"], x0=Decimal(e["x0"]), y0=Decimal(e["y0"]),
                     x1=Decimal(e["x1"]), y1=Decimal(e["y1"]))
            for e in raw
        )

    def field(triple, wrap=lambda v: v):
        if triple["value"] is None and triple["evidence"] is None:
            return NoCandidate()
        return Present(value=wrap(triple["value"]),
                       confidence=triple["confidence"],
                       evidence=evidence(triple["evidence"]))

    def period_field(quad):
        if quad["start"] is None:
            return NoCandidate()
        return Present(value=ServicePeriod(start=quad["start"], end=quad["end"]),
                       confidence=quad["confidence"],
                       evidence=evidence(quad["evidence"]))

    run = rows["run"]
    charges = tuple(
        ChargeCandidate(
            label=Present(value=c["label_text"], confidence=c["label_confidence"],
                          evidence=evidence(c["label_evidence"])),
            amount=Present(value=c["amount"], confidence=c["amount_confidence"],
                           evidence=evidence(c["amount_evidence"])),
            kind=ChargeKind(c["kind"]),
            kind_confidence=c["kind_confidence"],
            cadence=field(c["cadence"], wrap=Cadence),
            service_period=period_field(c["service_period"]),
        )
        for c in sorted(rows["charges"], key=lambda c: c["position"])
    )
    promotions = tuple(
        PromotionCandidate(
            expiry_date=Present(value=p["expiry_date"],
                                confidence=p["expiry_confidence"],
                                evidence=evidence(p["expiry_evidence"])),
            charge_index=p["charge_position"],
        )
        for p in sorted(rows["promotions"], key=lambda p: p["position"])
    )
    return BillExtractionV1(
        schema_version=run["extraction_schema_version"],
        currency=Currency(run["currency"]),
        bill_issuer_name=field(run["bill_issuer_name"]),
        service_category=field(run["service_category"], wrap=ServiceCategory),
        statement_date=field(run["statement_date"]),
        billing_period=period_field(run["billing_period"]),
        amount_due=field(run["amount_due"]),
        previous_balance=field(run["previous_balance"]),
        payments_applied=field(run["payments_applied"]),
        subtotal_before_tax=field(run["subtotal_before_tax"]),
        total_tax=field(run["total_tax"]),
        charges=charges,
        promotions=promotions,
    )


def _locator(page: int, y: str) -> dict:
    return {"page": page, "x0": "0.100000", "y0": y, "x1": "0.900000",
            "y1": "0.400000"}


@pytest.fixture
def extraction():
    """A rich, parser-validated extraction: candidates, absences, promotions."""
    payload = {
        "schema_version": "1.0.0",
        "extraction": {
            "currency": "CAD",
            "bill_issuer_name": {"state": "present", "value": "Acme Wireless",
                                 "confidence": "0.970000",
                                 "evidence": [_locator(1, "0.100000")]},
            "service_category": {"state": "present", "value": "MOBILE",
                                 "confidence": "0.900000",
                                 "evidence": [_locator(1, "0.150000")]},
            "statement_date": {"state": "present", "value": "2026-03-01",
                               "confidence": "0.950000",
                               "evidence": [_locator(1, "0.200000")]},
            "billing_period": {"state": "present",
                               "value": {"start": "2026-03-01", "end": "2026-03-31"},
                               "confidence": "0.910000",
                               "evidence": [_locator(1, "0.250000")]},
            "amount_due": {"state": "present", "value": "112.35",
                           "confidence": "0.990000",
                           "evidence": [_locator(1, "0.300000"),
                                        _locator(2, "0.110000")]},
            # A deliberate absence: the extractor produced no candidate. It is
            # NOT a claim that the bill lacks a previous balance.
            "previous_balance": {"state": "no_candidate"},
            "payments_applied": {"state": "present", "value": "-40.00",
                                 "confidence": "0.880000",
                                 "evidence": [_locator(2, "0.150000")]},
            "subtotal_before_tax": {"state": "present", "value": "99.99",
                                    "confidence": "0.930000",
                                    "evidence": [_locator(2, "0.200000")]},
            "total_tax": {"state": "present", "value": "12.36",
                          "confidence": "0.920000",
                          "evidence": [_locator(2, "0.250000")]},
            "charges": [
                {"label": {"state": "present", "value": "Monthly plan",
                           "confidence": "0.980000",
                           "evidence": [_locator(1, "0.320000")]},
                 "amount": {"state": "present", "value": "75.00",
                            "confidence": "0.990000",
                            "evidence": [_locator(1, "0.330000")]},
                 "kind": "RECURRING_FIXED", "kind_confidence": "0.950000",
                 "cadence": {"state": "present", "value": "MONTHLY",
                             "confidence": "0.900000",
                             "evidence": [_locator(1, "0.340000")]},
                 "service_period": {"state": "present",
                                    "value": {"start": "2026-03-01",
                                              "end": "2026-03-31"},
                                    "confidence": "0.890000",
                                    "evidence": [_locator(1, "0.350000")]}},
                {"label": {"state": "present", "value": "Data overage",
                           "confidence": "0.870000",
                           "evidence": [_locator(1, "0.360000")]},
                 "amount": {"state": "present", "value": "24.99",
                            "confidence": "0.910000",
                            "evidence": [_locator(1, "0.370000")]},
                 "kind": "USAGE", "kind_confidence": "0.820000",
                 "cadence": {"state": "no_candidate"},
                 "service_period": {"state": "no_candidate"}},
                {"label": {"state": "present", "value": "Loyalty credit",
                           "confidence": "0.940000",
                           "evidence": [_locator(2, "0.300000")]},
                 "amount": {"state": "present", "value": "-12.64",
                            "confidence": "0.960000",
                            "evidence": [_locator(2, "0.310000")]},
                 "kind": "CREDIT", "kind_confidence": "0.930000",
                 "cadence": {"state": "no_candidate"},
                 "service_period": {"state": "no_candidate"}},
            ],
            "promotions": [
                {"expiry_date": {"state": "present", "value": "2026-09-30",
                                 "confidence": "0.850000",
                                 "evidence": [_locator(2, "0.320000")]},
                 "charge_index": 2},
                {"expiry_date": {"state": "present", "value": "2027-01-15",
                                 "confidence": "0.800000",
                                 "evidence": [_locator(2, "0.330000")]},
                 "charge_index": None},
            ],
        },
    }
    return parse_extraction_result(payload, page_count=2)


def test_the_persisted_rows_reconstruct_the_extraction_identity(extraction):
    """The whole point: mapping to rows and back changes nothing."""
    rebuilt = _extraction_from(_rows_from(extraction))
    assert rebuilt.as_canonical() == extraction.as_canonical()
    assert rebuilt.extraction_identity() == extraction.extraction_identity()


def test_a_dropped_promotion_changes_the_identity(extraction):
    """Non-vacuity: if promotions had no table, this is what would be lost.

    The five-table foundation had nowhere to put these rows. This assertion is
    why the correction to seven tables was not cosmetic.
    """
    rows = _rows_from(extraction)
    assert rows["promotions"], "the fixture must carry promotions"
    rows["promotions"] = rows["promotions"][:-1]
    assert _extraction_from(rows).extraction_identity() != \
        extraction.extraction_identity()


def test_reordered_charges_change_the_identity(extraction):
    """Document order is semantic, so `position` is not decoration."""
    rows = _rows_from(extraction)
    for index, row in enumerate(reversed(rows["charges"])):
        row["position"] = index
    assert _extraction_from(rows).extraction_identity() != \
        extraction.extraction_identity()


def test_a_lost_promotion_association_changes_the_identity(extraction):
    """`charge_index` is part of the extraction, not a convenience column."""
    rows = _rows_from(extraction)
    rows["promotions"][0]["charge_position"] = None
    assert _extraction_from(rows).extraction_identity() != \
        extraction.extraction_identity()


def test_a_rounded_coordinate_changes_the_identity(extraction):
    """Evidence coordinates are canonical decimal STRINGS for this reason."""
    rows = _rows_from(extraction)
    rows["charges"][0]["label_evidence"][0]["y0"] = "0.320001"
    assert _extraction_from(rows).extraction_identity() != \
        extraction.extraction_identity()


def test_a_no_candidate_field_survives_as_all_null(extraction):
    """And is rebuilt as NoCandidate — never as a value, never as 'absent'."""
    rows = _rows_from(extraction)
    assert rows["run"]["previous_balance"] == {
        "value": None, "confidence": None, "evidence": None}
    rebuilt = _extraction_from(rows)
    assert isinstance(rebuilt.previous_balance, NoCandidate)


def test_every_money_and_confidence_survives_as_an_exact_decimal(extraction):
    """No float, at any layer of the mapping."""
    rows = _rows_from(extraction)
    assert isinstance(rows["run"]["amount_due"]["value"], Decimal)
    assert isinstance(rows["run"]["amount_due"]["confidence"], Decimal)
    for charge in rows["charges"]:
        assert isinstance(charge["amount"], Decimal)
        assert isinstance(charge["kind_confidence"], Decimal)
    assert rows["run"]["amount_due"]["value"] == Decimal("112.35")
    assert rows["run"]["payments_applied"]["value"] == Decimal("-40.00")


def test_the_mapped_columns_carry_no_bill_text_beyond_the_contracts_own_fields(
        extraction):
    """Evidence rows hold five keys, and none of them is a snippet."""
    rows = _rows_from(extraction)
    for charge in rows["charges"]:
        for locator in charge["label_evidence"] + charge["amount_evidence"]:
            assert set(locator) == {"page", "x0", "y0", "x1", "y1"}
            assert isinstance(locator["page"], int) and locator["page"] >= 1


def test_the_dates_map_to_real_date_objects(extraction):
    rows = _rows_from(extraction)
    assert rows["run"]["statement_date"]["value"] == datetime.date(2026, 3, 1)
    assert rows["run"]["billing_period"]["start"] == datetime.date(2026, 3, 1)
    assert rows["run"]["billing_period"]["end"] == datetime.date(2026, 3, 31)
