"""Metric semantics, hand-computed. Every helper builds isolated objects, so
no ordering or sharing between tests is possible."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.billshield.evaluation.labels import (
    SCALAR_FIELD_TYPES,
    AbsentOnDocument,
    ExpectedOutcome,
    GroundTruthCharge,
    GroundTruthLabel,
    GroundTruthPromotion,
    GroundTruthSuccess,
    Labeled,
)
from app.services.billshield.evaluation.metrics import (
    GATE_THRESHOLD,
    SampleOutcome,
    SampleResult,
    evaluate_samples,
    gate_verdict,
)
from app.services.billshield.extraction.codes import DiagnosticCode, RefusalCode
from app.services.billshield.extraction.contract import (
    BILL_EXTRACTION_SCHEMA_VERSION,
    BillExtractionV1,
    Cadence,
    ChargeCandidate,
    ChargeKind,
    Currency,
    Evidence,
    NoCandidate,
    Present,
    PromotionCandidate,
    ServicePeriod,
)

_EV = (Evidence(page=1, x0=Decimal("0.1"), y0=Decimal("0.2"),
                x1=Decimal("0.5"), y1=Decimal("0.25")),)


def _present(value: object) -> Present:
    return Present(value=value, confidence=Decimal("0.9"), evidence=_EV)


def _charge(label: str, amount: str, kind: ChargeKind,
            cadence: object = None) -> ChargeCandidate:
    return ChargeCandidate(
        label=_present(label), amount=_present(Decimal(amount)), kind=kind,
        kind_confidence=Decimal("0.9"),
        cadence=cadence if cadence is not None else NoCandidate(),
        service_period=NoCandidate(),
    )


def _extraction(charges: tuple = (), promotions: tuple = (),
                **scalars: object) -> BillExtractionV1:
    states: dict[str, object] = {
        name: NoCandidate() for name in SCALAR_FIELD_TYPES}
    for name, value in scalars.items():
        states[name] = _present(value)
    return BillExtractionV1(
        schema_version=BILL_EXTRACTION_SCHEMA_VERSION, currency=Currency.CAD,
        charges=charges, promotions=promotions, **states)  # type: ignore[arg-type]


def _label(charges: tuple = (), promotions: tuple = (),
           absent: tuple[str, ...] = (), **scalars: object) -> GroundTruthLabel:
    truths: dict[str, object] = {}
    for name in SCALAR_FIELD_TYPES:
        if name in scalars:
            truths[name] = Labeled(scalars[name])
        elif name in absent:
            truths[name] = AbsentOnDocument()
        else:
            # Fields the test does not mention are labeled ABSENT so they
            # stay out of every denominator by the quadrant rule.
            truths[name] = AbsentOnDocument()
    return GroundTruthLabel(
        label_schema_version="1.0.0",
        expected_outcome=ExpectedOutcome.SUCCESS, refusal_code=None,
        success=GroundTruthSuccess(
            scalars=truths, charges=charges, promotions=promotions))


def _refusal_label(code: RefusalCode) -> GroundTruthLabel:
    return GroundTruthLabel(
        label_schema_version="1.0.0",
        expected_outcome=ExpectedOutcome.REFUSAL, refusal_code=code,
        success=None)


def _ok(sample_id: str, label: GroundTruthLabel,
        extraction: BillExtractionV1) -> SampleResult:
    return SampleResult(sample_id=sample_id, label=label,
                        outcome=SampleOutcome.SUCCEEDED, refusal_code=None,
                        extraction=extraction)


def _refused(sample_id: str, label: GroundTruthLabel,
             code: RefusalCode) -> SampleResult:
    return SampleResult(sample_id=sample_id, label=label,
                        outcome=SampleOutcome.REFUSED, refusal_code=code,
                        extraction=None)


# ---------------------------------------------------------------------------
# The absence quadrant
# ---------------------------------------------------------------------------
def test_the_ground_truth_quadrant_is_exact():
    label = _label(amount_due=Decimal("10.00"), absent=("statement_date",))
    # GT present + NO_CANDIDATE prediction = miss; GT absent + prediction =
    # false positive; GT absent + no candidate = true negative.
    extraction = _extraction(statement_date=date(2026, 7, 1))
    tallies = evaluate_samples([_ok("s-000000000001", label, extraction)])
    accuracy = tallies.metrics["field.amount_due.accuracy"]
    assert (accuracy.numerator, accuracy.denominator) == (0, 1)
    date_precision = tallies.metrics["field.statement_date.precision"]
    assert (date_precision.numerator, date_precision.denominator) == (0, 1)
    date_accuracy = tallies.metrics["field.statement_date.accuracy"]
    assert date_accuracy.denominator == 0, "GT-absent is no accuracy case"
    tax = tallies.metrics["field.total_tax.accuracy"]
    assert tax.denominator == 0, "true negatives stay out of denominators"
    codes = {f.code for f in tallies.findings}
    assert DiagnosticCode.MISSING_PREDICTION in codes
    assert DiagnosticCode.SPURIOUS_PREDICTION in codes


def test_zero_denominators_are_not_measurable_never_zero_or_one():
    tallies = evaluate_samples([])
    payload = tallies.metrics["field.amount_due.accuracy"].as_payload()
    assert payload == {"numerator": 0, "denominator": 0, "value": None,
                      "status": "NOT_MEASURABLE"}
    verdict, failures = gate_verdict(tallies)
    assert verdict == "FAIL"
    assert any(f.startswith("NOT_MEASURABLE:") for f in failures), (
        "an unmeasured critical metric must fail the gate, not pass it")


# ---------------------------------------------------------------------------
# Charge matching: amount is never identity
# ---------------------------------------------------------------------------
def test_a_wrong_amount_is_an_amount_error_not_a_missing_charge():
    label = _label(charges=(
        GroundTruthCharge(label="Fibre 500", amount=Decimal("89.99"),
                          kind=ChargeKind.RECURRING_FIXED, cadence=None),))
    extraction = _extraction(
        amount_due=Decimal("89.99"),
        charges=(_charge("Fibre 500", "98.99", ChargeKind.RECURRING_FIXED),))
    tallies = evaluate_samples([_ok("s-000000000001", label, extraction)])
    presence = tallies.metrics["charges.presence_recall"]
    assert (presence.numerator, presence.denominator) == (1, 1), (
        "the charge matched by label; the amount error must not unmatch it")
    amount = tallies.metrics["charges.amount_exact_match"]
    assert (amount.numerator, amount.denominator) == (0, 1)
    recurring = tallies.metrics["charges.recurring_fixed_amount_exact_match"]
    assert (recurring.numerator, recurring.denominator) == (0, 1)


def test_duplicate_labels_pair_by_occurrence():
    label = _label(charges=(
        GroundTruthCharge(label="Movie rental", amount=Decimal("5.99"),
                          kind=ChargeKind.ONE_TIME, cadence=None),
        GroundTruthCharge(label="Movie rental", amount=Decimal("7.99"),
                          kind=ChargeKind.ONE_TIME, cadence=None),))
    extraction = _extraction(
        amount_due=Decimal("13.98"),
        charges=(_charge("Movie rental", "5.99", ChargeKind.ONE_TIME),
                 _charge("Movie rental", "7.99", ChargeKind.ONE_TIME)))
    tallies = evaluate_samples([_ok("s-000000000001", label, extraction)])
    amount = tallies.metrics["charges.amount_exact_match"]
    assert (amount.numerator, amount.denominator) == (2, 2)


def test_matching_normalization_preserves_accents():
    label = _label(charges=(
        GroundTruthCharge(label="FRAIS  de réseau", amount=Decimal("5.00"),
                          kind=ChargeKind.FEE, cadence=None),))
    extraction = _extraction(
        amount_due=Decimal("5.00"),
        charges=(_charge("frais de réseau", "5.00", ChargeKind.FEE),))
    tallies = evaluate_samples([_ok("s-000000000001", label, extraction)])
    assert tallies.metrics["charges.presence_recall"].numerator == 1
    # a stripped-accent label is a DIFFERENT label, so it must not match
    stripped = _extraction(
        amount_due=Decimal("5.00"),
        charges=(_charge("frais de reseau", "5.00", ChargeKind.FEE),))
    tallies = evaluate_samples([_ok("s-000000000001", label, stripped)])
    assert tallies.metrics["charges.presence_recall"].numerator == 0


def test_unclassified_never_passes_known_kind_accuracy():
    label = _label(charges=(
        GroundTruthCharge(label="Install", amount=Decimal("49.99"),
                          kind=ChargeKind.ONE_TIME, cadence=None),
        GroundTruthCharge(label="Mystery", amount=Decimal("1.00"),
                          kind=None, cadence=None),))
    extraction = _extraction(
        amount_due=Decimal("50.99"),
        charges=(_charge("Install", "49.99", ChargeKind.UNCLASSIFIED),
                 _charge("Mystery", "1.00", ChargeKind.UNCLASSIFIED)))
    tallies = evaluate_samples([_ok("s-000000000001", label, extraction)])
    kind = tallies.metrics["charges.kind_accuracy"]
    # the unlabeled-kind charge is excluded; the labeled one counts a miss
    assert (kind.numerator, kind.denominator) == (0, 1)
    assert tallies.kind_confusion == {"UNCLASSIFIED->ONE_TIME": 1}
    # ... and both charges still matched and kept their amounts
    assert tallies.metrics["charges.amount_exact_match"].numerator == 2


def test_systematic_counters_catch_sign_shift_and_rf_ot_confusion():
    label = _label(
        total_tax=Decimal("7.80"),
        charges=(
            GroundTruthCharge(label="Plan", amount=Decimal("75.00"),
                              kind=ChargeKind.RECURRING_FIXED, cadence=None),
            GroundTruthCharge(label="Extra", amount=Decimal("5.00"),
                              kind=ChargeKind.ONE_TIME, cadence=None),))
    extraction = _extraction(
        total_tax=Decimal("-7.80"),
        amount_due=Decimal("87.80"),
        charges=(_charge("Plan", "750.00", ChargeKind.ONE_TIME),
                 _charge("Extra", "5.00", ChargeKind.ONE_TIME)))
    # amount_due GT is unlabeled here (absent) so the spurious prediction
    # only affects precision — the counters are what this test pins.
    tallies = evaluate_samples([_ok("s-000000000001", label, extraction)])
    assert tallies.counters["money_sign_mismatches"] == 1
    assert tallies.counters["decimal_shift_mismatches"] == 1
    assert tallies.counters["recurring_fixed_one_time_confusions"] == 1
    verdict, failures = gate_verdict(tallies)
    assert verdict == "FAIL"
    assert "SYSTEMATIC:money_sign_mismatches" in failures
    assert "SYSTEMATIC:decimal_shift_mismatches" in failures
    assert "SYSTEMATIC:recurring_fixed_one_time_confusions" in failures


# ---------------------------------------------------------------------------
# Refusals cannot be a strategy
# ---------------------------------------------------------------------------
def _perfect_sample(sample_id: str) -> SampleResult:
    label = _label(
        bill_issuer_name="Maple", service_category="MOBILE",
        statement_date=date(2026, 7, 15),
        billing_period=ServicePeriod(date(2026, 7, 1), date(2026, 7, 31)),
        amount_due=Decimal("10.00"), total_tax=Decimal("1.00"),
        charges=(GroundTruthCharge(
            label="Plan", amount=Decimal("9.00"),
            kind=ChargeKind.RECURRING_FIXED, cadence=Labeled(Cadence.MONTHLY)),))
    from app.services.billshield.extraction.contract import ServiceCategory

    extraction = _extraction(
        bill_issuer_name="Maple",
        service_category=ServiceCategory.MOBILE,
        statement_date=date(2026, 7, 15),
        billing_period=ServicePeriod(date(2026, 7, 1), date(2026, 7, 31)),
        amount_due=Decimal("10.00"), total_tax=Decimal("1.00"),
        charges=(_charge("Plan", "9.00", ChargeKind.RECURRING_FIXED,
                         cadence=_present(Cadence.MONTHLY)),))
    return _ok(sample_id, label, extraction)


def test_refusing_difficult_readable_bills_prevents_gate_passage():
    """Nine perfect samples; the tenth readable bill is refused. Every
    critical accuracy that the refused bill labels drops below 1.0, and at
    0.9 exactly nine-of-ten still passes — so the test uses two refusals to
    fall below threshold and prove the direction."""
    perfect = [_perfect_sample(f"s-00000000000{i}") for i in range(8)]
    hard_label = _perfect_sample("s-0000000000ff").label
    refusals = [
        _refused("s-0000000000f0", hard_label, RefusalCode.UNREADABLE),
        _refused("s-0000000000f1", hard_label, RefusalCode.UNREADABLE),
    ]
    tallies = evaluate_samples([*perfect, *refusals])
    accuracy = tallies.metrics["field.amount_due.accuracy"]
    assert (accuracy.numerator, accuracy.denominator) == (8, 10), (
        "refused readable bills left the denominator — the refuse-to-pass "
        "exploit is open")
    recall = tallies.metrics["charges.presence_recall"]
    assert (recall.numerator, recall.denominator) == (8, 10)
    verdict, failures = gate_verdict(tallies)
    assert verdict == "FAIL"
    assert "BELOW_THRESHOLD:field.amount_due.accuracy" in failures
    spurious = tallies.metrics["refusals.spurious_refusal_rate"]
    assert (spurious.numerator, spurious.denominator) == (2, 10)
    # and with no refusals the same corpus passes — the gate measures the
    # extractor, not the corpus
    verdict_clean, failures_clean = gate_verdict(evaluate_samples(
        [*perfect, _perfect_sample("s-0000000000f0"),
         _perfect_sample("s-0000000000f1")]))
    assert verdict_clean == "PASS", failures_clean


def test_refusal_metrics_split_precision_recall_and_code():
    expected = _refusal_label(RefusalCode.UNSUPPORTED_SERVICE_SCOPE)
    samples = [
        _refused("s-000000000001", expected,
                 RefusalCode.UNSUPPORTED_SERVICE_SCOPE),   # right, right code
        _refused("s-000000000002",
                 _refusal_label(RefusalCode.UNREADABLE),
                 RefusalCode.UNSUPPORTED_FORMAT),          # right, wrong code
        _refused("s-000000000003",
                 _perfect_sample("s-000000000003").label,
                 RefusalCode.UNREADABLE),                  # spurious refusal
        _ok("s-000000000004", _refusal_label(RefusalCode.UNREADABLE),
            _perfect_sample("s-000000000004").extraction),  # spurious success
    ]
    tallies = evaluate_samples(samples)
    recall = tallies.metrics["refusals.recall"]
    assert (recall.numerator, recall.denominator) == (2, 3)
    precision = tallies.metrics["refusals.precision"]
    assert (precision.numerator, precision.denominator) == (2, 3)
    code = tallies.metrics["refusals.code_exact_match"]
    assert (code.numerator, code.denominator) == (1, 2)
    spurious_success = tallies.metrics["refusals.spurious_success_rate"]
    assert (spurious_success.numerator, spurious_success.denominator) == (1, 3)
    assert {f.code for f in tallies.findings} >= {
        DiagnosticCode.WRONG_REFUSAL_CODE, DiagnosticCode.SPURIOUS_REFUSAL,
        DiagnosticCode.SPURIOUS_SUCCESS}


def test_requires_correction_includes_incorrectly_refused_bills():
    samples = [
        _perfect_sample("s-000000000001"),
        _refused("s-000000000002", _perfect_sample("s-000000000002").label,
                 RefusalCode.UNREADABLE),
    ]
    tallies = evaluate_samples(samples)
    proxy = tallies.metrics["samples.requires_correction_proxy"]
    assert (proxy.numerator, proxy.denominator) == (1, 2)


# ---------------------------------------------------------------------------
# Promotions: identity is association + occurrence, never the date
# ---------------------------------------------------------------------------
def test_promotion_matching_and_the_inference_counter():
    label = _label(
        amount_due=Decimal("20.00"),
        charges=(GroundTruthCharge(label="Plan", amount=Decimal("20.00"),
                                   kind=ChargeKind.RECURRING_FIXED,
                                   cadence=None),),
        promotions=(GroundTruthPromotion(date(2026, 9, 30), 0),
                    GroundTruthPromotion(date(2026, 12, 31), 0)))
    extraction = _extraction(
        amount_due=Decimal("20.00"),
        charges=(_charge("Plan", "20.00", ChargeKind.RECURRING_FIXED),),
        promotions=(
            PromotionCandidate(_present(date(2026, 9, 30)), 0),
            PromotionCandidate(_present(date(2027, 1, 31)), 0),   # wrong date
            PromotionCandidate(_present(date(2026, 6, 1)), None),  # invented
        ))
    tallies = evaluate_samples([_ok("s-000000000001", label, extraction)])
    precision = tallies.metrics["promotions.expiry_precision"]
    assert (precision.numerator, precision.denominator) == (1, 3)
    recall = tallies.metrics["promotions.expiry_recall"]
    assert (recall.numerator, recall.denominator) == (1, 2)
    assert tallies.counters["promotion_expiry_false_positives"] == 1, (
        "only the document-level promo with no printed counterpart is an "
        "inference; the mismatched second promo is a misread, not invention")
    verdict, failures = gate_verdict(tallies)
    assert "SYSTEMATIC:promotion_expiry_false_positives" in failures


def test_the_gate_threshold_is_the_governed_constant():
    assert GATE_THRESHOLD == Decimal("0.90")


# ---------------------------------------------------------------------------
# The UNCLASSIFIED loophole, closed: recurring-fixed credit requires the
# recurring-fixed CLASSIFICATION, not merely the right number
# ---------------------------------------------------------------------------
def _hedged_sample(sample_id: str, kind: ChargeKind) -> SampleResult:
    """A _perfect_sample whose recurring charge is re-classified as `kind`
    with the amount still exactly right."""
    perfect = _perfect_sample(sample_id)
    extraction = perfect.extraction
    assert extraction is not None
    original = extraction.charges[0]
    cadence = original.cadence if kind in (
        ChargeKind.RECURRING_FIXED, ChargeKind.DEVICE_FINANCING,
        ChargeKind.UNCLASSIFIED) else NoCandidate()
    hedged = ChargeCandidate(
        label=original.label, amount=original.amount, kind=kind,
        kind_confidence=original.kind_confidence, cadence=cadence,
        service_period=original.service_period)
    from dataclasses import replace

    return _ok(sample_id, perfect.label, replace(extraction, charges=(hedged,)))


def test_hedging_every_recurring_charge_as_unclassified_fails_the_gate():
    """Exact amounts everywhere, every recurring line marked UNCLASSIFIED:
    the ONLY defect is the hedge, and the gate must still refuse it."""
    samples = [_hedged_sample(f"s-00000000000{i}", ChargeKind.UNCLASSIFIED)
               for i in range(10)]
    tallies = evaluate_samples(samples)
    recurring = tallies.metrics["charges.recurring_fixed_amount_exact_match"]
    assert (recurring.numerator, recurring.denominator) == (0, 10), (
        "an UNCLASSIFIED prediction earned recurring-fixed credit")
    amount = tallies.metrics["charges.amount_exact_match"]
    assert (amount.numerator, amount.denominator) == (10, 10), (
        "the general amount metric still credits the exact numbers")
    verdict, failures = gate_verdict(tallies)
    assert verdict == "FAIL"
    assert failures == (
        "BELOW_THRESHOLD:charges.recurring_fixed_amount_exact_match",), (
        f"the hedge must be the one and only gate failure: {failures}")
    assert {f.code for f in tallies.findings} == {
        DiagnosticCode.KIND_CONFUSION}, "kind-confusion diagnostics survive"


def test_exact_recurring_classification_and_amount_earns_the_metric():
    tallies = evaluate_samples(
        [_perfect_sample(f"s-00000000000{i}") for i in range(3)])
    recurring = tallies.metrics["charges.recurring_fixed_amount_exact_match"]
    assert (recurring.numerator, recurring.denominator) == (3, 3)
    assert gate_verdict(tallies)[0] == "PASS"


def test_one_time_classification_with_exact_amount_fails_and_counts_confusion():
    samples = [_hedged_sample("s-0000000000aa", ChargeKind.ONE_TIME)]
    tallies = evaluate_samples(samples)
    recurring = tallies.metrics["charges.recurring_fixed_amount_exact_match"]
    assert (recurring.numerator, recurring.denominator) == (0, 1)
    assert tallies.counters["recurring_fixed_one_time_confusions"] == 1, (
        "the existing systematic control must fire on RF->ONE_TIME")
    verdict, failures = gate_verdict(tallies)
    assert verdict == "FAIL"
    assert "SYSTEMATIC:recurring_fixed_one_time_confusions" in failures
    assert ("BELOW_THRESHOLD:charges.recurring_fixed_amount_exact_match"
            in failures)
