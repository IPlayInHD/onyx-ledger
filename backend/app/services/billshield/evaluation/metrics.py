"""Deterministic metrics — every numerator and denominator written down.

THE DENOMINATOR RULE THAT RESISTS GAMING. Accuracy and recall denominators
are defined over what the GROUND TRUTH says a readable corpus contains, never
over what the extractor chose to attempt: a readable bill the extractor
refused, or whose output failed the strict parser, stays in every applicable
denominator with a numerator of zero. An extractor therefore cannot pass the
gate by refusing difficult documents — the refusals themselves are also
measured, separately, as precision, recall, spurious-refusal rate and
exact-code accuracy.

CHARGE MATCHING (CHARGE_MATCHING_POLICY_V1) never uses the value under
evaluation as identity: charges pair by normalized label (NFC, casefold,
whitespace-collapse — accents survive) plus occurrence index among equal
labels in document order. A wrong amount on a matched charge is an AMOUNT
mismatch, not a vanished charge. The stated limitation: a wholly mislabeled
charge surfaces as a presence error, not a label error; the label
exact-match metric measures residual text fidelity over matched pairs only.

Promotion identity is the association (matched charge key, or the document
level) plus occurrence order on each side — never the expiry date, which is
the value being evaluated.

Zero denominators are NOT_MEASURABLE — never 0.0, never 1.0 — and a critical
metric that is not measurable fails the gate, because an unmeasured field
cannot be certified. No overall average exists that could conceal a failed
critical metric.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from app.services.billshield.evaluation.labels import (
    SCALAR_FIELD_TYPES,
    AbsentOnDocument,
    ExpectedOutcome,
    GroundTruthLabel,
    Labeled,
)
from app.services.billshield.extraction.codes import DiagnosticCode, RefusalCode
from app.services.billshield.extraction.contract import (
    BillExtractionV1,
    ChargeCandidate,
    ChargeKind,
    Present,
    ServicePeriod,
)
from app.services.ioe.domain import canonical as c

METRIC_POLICY_VERSION = "1.0.0"
CHARGE_MATCHING_POLICY_VERSION = "1.0.0"
CRITICAL_FIELD_REGISTRY_VERSION = "1.0.0"

#: The private-beta gate threshold (plan §10.2), applied to every critical
#: metric individually.
GATE_THRESHOLD = Decimal("0.90")

#: Critical scalar fields — each "when labeled": a sample enters the
#: denominator only where ground truth labels the field present, and a metric
#: with an empty denominator is NOT_MEASURABLE, which itself fails the gate.
CRITICAL_SCALAR_FIELDS = (
    "bill_issuer_name",
    "service_category",
    "statement_date",
    "billing_period",
    "amount_due",
    "total_tax",
)

#: charges.recurring_fixed_amount_exact_match — denominator: every
#: ground-truth RECURRING_FIXED charge on a readable sample; numerator: the
#: charge is MATCHED, the predicted kind is RECURRING_FIXED, and the amount
#: is exactly equal. A correct amount under UNCLASSIFIED, ONE_TIME, or any
#: other predicted kind earns nothing here — hedging the classification
#: cannot buy the gate.
CRITICAL_CHARGE_METRICS = (
    "charges.presence_precision",
    "charges.presence_recall",
    "charges.recurring_fixed_amount_exact_match",
)


class SampleOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    REFUSED = "refused"
    #: The adapter's output failed the strict parser — an extractor failure,
    #: distinct from an honest refusal and measured as its own rate.
    INVALID_OUTPUT = "invalid_output"


@dataclass(frozen=True)
class SampleResult:
    """What the runner observed for one sample, joined to its label."""

    sample_id: str
    label: GroundTruthLabel
    outcome: SampleOutcome
    refusal_code: RefusalCode | None
    extraction: BillExtractionV1 | None


@dataclass(frozen=True)
class Finding:
    """One diagnostic: a closed code and a closed subject. Never a value."""

    sample_id: str
    subject: str
    code: DiagnosticCode


@dataclass
class MetricTally:
    numerator: int = 0
    denominator: int = 0

    @property
    def measurable(self) -> bool:
        return self.denominator > 0

    def meets(self, threshold: Decimal) -> bool:
        """Exact Decimal comparison — no float ever touches the gate."""
        return Decimal(self.numerator) >= threshold * self.denominator

    def as_payload(self) -> dict[str, object]:
        if not self.measurable:
            return {
                "numerator": self.numerator,
                "denominator": 0,
                "value": None,
                "status": "NOT_MEASURABLE",
            }
        value = Decimal(self.numerator) / Decimal(self.denominator)
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "value": c.rate(value),
            "status": "MEASURED",
        }


@dataclass
class EvaluationTallies:
    """Everything `evaluate_samples` accumulates, before the report shapes it."""

    metrics: dict[str, MetricTally] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    kind_confusion: dict[str, int] = field(default_factory=dict)
    refusal_counts: dict[str, int] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    samples_total: int = 0
    readable_total: int = 0
    refusal_expected_total: int = 0
    succeeded_total: int = 0
    refused_total: int = 0
    invalid_output_total: int = 0

    def metric(self, name: str) -> MetricTally:
        return self.metrics.setdefault(name, MetricTally())

    def bump(self, counter: str, by: int = 1) -> None:
        self.counters[counter] = self.counters.get(counter, 0) + by


_COUNTER_NAMES = (
    "money_sign_mismatches",
    "decimal_shift_mismatches",
    "recurring_fixed_one_time_confusions",
    "promotion_expiry_false_positives",
)


def normalize_label_text(text: str) -> str:
    """CHARGE_MATCHING_POLICY_V1 normalization: NFC, casefold, collapse
    whitespace. Accents are preserved — French is not reduced to ASCII."""
    return " ".join(c.normalize_text(text).casefold().split())


def _occurrence_keys(labels: Iterable[str]) -> list[tuple[str, int]]:
    seen: dict[str, int] = {}
    keys: list[tuple[str, int]] = []
    for text in labels:
        normalized = normalize_label_text(text)
        occurrence = seen.get(normalized, 0)
        seen[normalized] = occurrence + 1
        keys.append((normalized, occurrence))
    return keys


def _money_mismatch(pred: Decimal, expected: Decimal) -> DiagnosticCode:
    """Which systematic class a money mismatch belongs to. Exact patterns
    only: sign flip, then a 1-2 place decimal shift in either direction."""
    if expected != 0 and pred == -expected:
        return DiagnosticCode.SIGN_MISMATCH
    for places in (1, 2, -1, -2):
        if expected != 0 and pred == expected.scaleb(places):
            return DiagnosticCode.DECIMAL_SHIFT_MISMATCH
    return DiagnosticCode.VALUE_MISMATCH


def _scalar_equal(field_name: str, predicted: object, labeled: object) -> bool:
    kind = SCALAR_FIELD_TYPES[field_name]
    if kind == "text":
        assert isinstance(predicted, str) and isinstance(labeled, str)
        return normalize_label_text(predicted) == normalize_label_text(labeled)
    if kind == "period":
        assert isinstance(predicted, ServicePeriod)
        assert isinstance(labeled, ServicePeriod)
        return predicted == labeled
    return predicted == labeled


def evaluate_samples(samples: Sequence[SampleResult]) -> EvaluationTallies:
    """One pass over every sample; order-independent by construction (every
    accumulator is a sum, and findings are sorted by the report)."""
    t = EvaluationTallies()
    for name in SCALAR_FIELD_TYPES:
        t.metric(f"field.{name}.accuracy")
        t.metric(f"field.{name}.precision")
    for name in (
        "charges.presence_precision", "charges.presence_recall",
        "charges.label_exact_match", "charges.amount_exact_match",
        "charges.recurring_fixed_amount_exact_match",
        "charges.kind_accuracy", "charges.cadence_accuracy",
        "charges.cadence_precision",
        "promotions.expiry_precision", "promotions.expiry_recall",
        "refusals.recall", "refusals.precision", "refusals.code_exact_match",
        "refusals.spurious_refusal_rate", "refusals.spurious_success_rate",
        "samples.unsupported_unreadable_rate", "samples.invalid_output_rate",
        "samples.requires_correction_proxy",
    ):
        t.metric(name)
    for name in _COUNTER_NAMES:
        t.counters.setdefault(name, 0)

    for sample in samples:
        t.samples_total += 1
        t.metric("samples.unsupported_unreadable_rate").denominator += 1
        t.metric("samples.invalid_output_rate").denominator += 1
        if sample.outcome is SampleOutcome.REFUSED:
            t.refused_total += 1
            t.metric("samples.unsupported_unreadable_rate").numerator += 1
            t.metric("refusals.precision").denominator += 1
            code = sample.refusal_code.value if sample.refusal_code else "UNKNOWN"
            t.refusal_counts[code] = t.refusal_counts.get(code, 0) + 1
        elif sample.outcome is SampleOutcome.INVALID_OUTPUT:
            t.invalid_output_total += 1
            t.metric("samples.invalid_output_rate").numerator += 1
            t.findings.append(Finding(
                sample.sample_id, "sample", DiagnosticCode.PROVIDER_OUTPUT_INVALID))
        else:
            t.succeeded_total += 1

        if sample.label.expected_outcome is ExpectedOutcome.REFUSAL:
            t.refusal_expected_total += 1
            _evaluate_refusal_expected(t, sample)
        else:
            t.readable_total += 1
            _evaluate_readable(t, sample)
    return t


def _evaluate_refusal_expected(t: EvaluationTallies, sample: SampleResult) -> None:
    t.metric("refusals.recall").denominator += 1
    t.metric("refusals.spurious_success_rate").denominator += 1
    if sample.outcome is SampleOutcome.REFUSED:
        t.metric("refusals.recall").numerator += 1
        t.metric("refusals.precision").numerator += 1
        t.metric("refusals.code_exact_match").denominator += 1
        if sample.refusal_code is sample.label.refusal_code:
            t.metric("refusals.code_exact_match").numerator += 1
        else:
            t.findings.append(Finding(
                sample.sample_id, "sample", DiagnosticCode.WRONG_REFUSAL_CODE))
        return
    if sample.outcome is SampleOutcome.SUCCEEDED:
        t.metric("refusals.spurious_success_rate").numerator += 1
        t.findings.append(Finding(
            sample.sample_id, "sample", DiagnosticCode.SPURIOUS_SUCCESS))
        extraction = sample.extraction
        assert extraction is not None
        # Every candidate produced for a document that should have been
        # refused counts against the relevant precision.
        for name in SCALAR_FIELD_TYPES:
            if isinstance(getattr(extraction, name), Present):
                t.metric(f"field.{name}.precision").denominator += 1
        t.metric("charges.presence_precision").denominator += len(
            extraction.charges)
        promo_metric = t.metric("promotions.expiry_precision")
        promo_metric.denominator += len(extraction.promotions)
        if extraction.promotions:
            t.bump("promotion_expiry_false_positives", len(extraction.promotions))
            t.findings.append(Finding(
                sample.sample_id, "promotions",
                DiagnosticCode.PROMO_EXPIRY_FALSE_POSITIVE))
    # INVALID_OUTPUT on a refusal-expected sample: not a refusal (recall miss
    # already counted by the missing numerator) and not a success.


def _evaluate_readable(t: EvaluationTallies, sample: SampleResult) -> None:
    success = sample.label.success
    assert success is not None
    t.metric("refusals.spurious_refusal_rate").denominator += 1
    proxy = t.metric("samples.requires_correction_proxy")
    proxy.denominator += 1

    labeled_charges = success.charges
    # GT-derived denominators are owed by every readable sample, whatever the
    # extractor did with it — this is the refuse-to-pass resistance.
    for name, truth in success.scalars.items():
        if isinstance(truth, Labeled):
            t.metric(f"field.{name}.accuracy").denominator += 1
    t.metric("charges.presence_recall").denominator += len(labeled_charges)
    t.metric("charges.amount_exact_match").denominator += len(labeled_charges)
    t.metric("charges.recurring_fixed_amount_exact_match").denominator += sum(
        1 for gt in labeled_charges if gt.kind is ChargeKind.RECURRING_FIXED)
    t.metric("promotions.expiry_recall").denominator += len(success.promotions)

    if sample.outcome is not SampleOutcome.SUCCEEDED:
        if sample.outcome is SampleOutcome.REFUSED:
            t.metric("refusals.spurious_refusal_rate").numerator += 1
            t.findings.append(Finding(
                sample.sample_id, "sample", DiagnosticCode.SPURIOUS_REFUSAL))
        proxy.numerator += 1  # an incorrectly refused or invalid readable
        return                # bill REQUIRES correction by definition

    extraction = sample.extraction
    assert extraction is not None
    needs_correction = False

    for name, truth in success.scalars.items():
        predicted = getattr(extraction, name)
        critical = name in CRITICAL_SCALAR_FIELDS
        if isinstance(truth, Labeled):
            if isinstance(predicted, Present):
                t.metric(f"field.{name}.precision").denominator += 1
                if _scalar_equal(name, predicted.value, truth.value):
                    t.metric(f"field.{name}.accuracy").numerator += 1
                    t.metric(f"field.{name}.precision").numerator += 1
                else:
                    code = DiagnosticCode.VALUE_MISMATCH
                    if SCALAR_FIELD_TYPES[name] == "money":
                        assert isinstance(predicted.value, Decimal)
                        assert isinstance(truth.value, Decimal)
                        code = _money_mismatch(predicted.value, truth.value)
                        if code is DiagnosticCode.SIGN_MISMATCH:
                            t.bump("money_sign_mismatches")
                        elif code is DiagnosticCode.DECIMAL_SHIFT_MISMATCH:
                            t.bump("decimal_shift_mismatches")
                    t.findings.append(Finding(sample.sample_id, f"field.{name}", code))
                    needs_correction = needs_correction or critical
            else:
                t.findings.append(Finding(
                    sample.sample_id, f"field.{name}",
                    DiagnosticCode.MISSING_PREDICTION))
                needs_correction = needs_correction or critical
        else:
            assert isinstance(truth, AbsentOnDocument)
            if isinstance(predicted, Present):
                t.metric(f"field.{name}.precision").denominator += 1
                t.findings.append(Finding(
                    sample.sample_id, f"field.{name}",
                    DiagnosticCode.SPURIOUS_PREDICTION))
                needs_correction = needs_correction or critical

    needs_correction |= _evaluate_charges(t, sample, extraction.charges)
    needs_correction |= _evaluate_promotions(t, sample, extraction)
    if needs_correction:
        proxy.numerator += 1


def _evaluate_charges(
    t: EvaluationTallies,
    sample: SampleResult,
    predicted: tuple[ChargeCandidate, ...],
) -> bool:
    success = sample.label.success
    assert success is not None
    labeled = success.charges
    needs_correction = False

    predicted_keys = _occurrence_keys(ch.label.value for ch in predicted)
    labeled_keys = _occurrence_keys(gt.label for gt in labeled)
    labeled_by_key = dict(zip(labeled_keys, labeled, strict=True))
    matched_labeled: set[tuple[str, int]] = set()

    t.metric("charges.presence_precision").denominator += len(predicted)
    for i, (key, charge) in enumerate(zip(predicted_keys, predicted, strict=True)):
        gt = labeled_by_key.get(key)
        if gt is None:
            t.findings.append(Finding(
                sample.sample_id, f"charges.predicted[{i}]",
                DiagnosticCode.UNMATCHED_CHARGE_PREDICTED))
            needs_correction = True
            continue
        matched_labeled.add(key)
        t.metric("charges.presence_precision").numerator += 1
        t.metric("charges.presence_recall").numerator += 1

        label_metric = t.metric("charges.label_exact_match")
        label_metric.denominator += 1
        if charge.label.value == c.normalize_text(gt.label):
            label_metric.numerator += 1
        else:
            t.findings.append(Finding(
                sample.sample_id, f"charges.predicted[{i}]",
                DiagnosticCode.LABEL_TEXT_MISMATCH))

        if charge.amount.value == gt.amount:
            t.metric("charges.amount_exact_match").numerator += 1
            # The critical recurring-fixed metric credits a ground-truth
            # RECURRING_FIXED charge only when the PREDICTION also classified
            # it RECURRING_FIXED. Price-creep analysis will trust that
            # classification, so an extractor that hedges every recurring
            # line as UNCLASSIFIED (or misfiles it) has not read the bill
            # correctly, however exact its amounts — it must not pass the
            # gate on amounts alone.
            if (gt.kind is ChargeKind.RECURRING_FIXED
                    and charge.kind is ChargeKind.RECURRING_FIXED):
                t.metric(
                    "charges.recurring_fixed_amount_exact_match").numerator += 1
        else:
            code = _money_mismatch(charge.amount.value, gt.amount)
            if code is DiagnosticCode.SIGN_MISMATCH:
                t.bump("money_sign_mismatches")
            elif code is DiagnosticCode.DECIMAL_SHIFT_MISMATCH:
                t.bump("decimal_shift_mismatches")
            t.findings.append(Finding(
                sample.sample_id, f"charges.predicted[{i}]", code))
            needs_correction = True

        if gt.kind is not None:
            kind_metric = t.metric("charges.kind_accuracy")
            kind_metric.denominator += 1
            pair = f"{charge.kind.value}->{gt.kind.value}"
            t.kind_confusion[pair] = t.kind_confusion.get(pair, 0) + 1
            if charge.kind is gt.kind:
                kind_metric.numerator += 1
            else:
                if {charge.kind, gt.kind} == {ChargeKind.RECURRING_FIXED,
                                              ChargeKind.ONE_TIME}:
                    t.bump("recurring_fixed_one_time_confusions")
                t.findings.append(Finding(
                    sample.sample_id, f"charges.predicted[{i}]",
                    DiagnosticCode.KIND_CONFUSION))
                needs_correction = True

        if isinstance(gt.cadence, Labeled):
            cadence_metric = t.metric("charges.cadence_accuracy")
            cadence_metric.denominator += 1
            if isinstance(charge.cadence, Present):
                t.metric("charges.cadence_precision").denominator += 1
                if charge.cadence.value is gt.cadence.value:
                    cadence_metric.numerator += 1
                    t.metric("charges.cadence_precision").numerator += 1
                else:
                    t.findings.append(Finding(
                        sample.sample_id, f"charges.predicted[{i}]",
                        DiagnosticCode.CADENCE_MISMATCH))
            else:
                t.findings.append(Finding(
                    sample.sample_id, f"charges.predicted[{i}]",
                    DiagnosticCode.CADENCE_MISMATCH))
        elif isinstance(gt.cadence, AbsentOnDocument) and isinstance(
                charge.cadence, Present):
            t.metric("charges.cadence_precision").denominator += 1
            t.findings.append(Finding(
                sample.sample_id, f"charges.predicted[{i}]",
                DiagnosticCode.CADENCE_MISMATCH))

    for j, key in enumerate(labeled_keys):
        if key not in matched_labeled:
            t.findings.append(Finding(
                sample.sample_id, f"charges.labeled[{j}]",
                DiagnosticCode.UNMATCHED_CHARGE_LABELED))
            needs_correction = True
    return needs_correction


def _promotion_keys(
    associations: Iterable[tuple[str, int] | None]
) -> list[tuple[object, int]]:
    """Promotion identity: the association plus occurrence order — never the
    expiry date, which is the value under evaluation."""
    seen: dict[object, int] = {}
    keys: list[tuple[object, int]] = []
    for association in associations:
        anchor: object = association if association is not None else "document"
        occurrence = seen.get(anchor, 0)
        seen[anchor] = occurrence + 1
        keys.append((anchor, occurrence))
    return keys


def _evaluate_promotions(
    t: EvaluationTallies,
    sample: SampleResult,
    extraction: BillExtractionV1,
) -> bool:
    success = sample.label.success
    assert success is not None
    labeled_promotions = success.promotions
    needs_correction = False

    predicted_charge_keys = _occurrence_keys(
        ch.label.value for ch in extraction.charges)
    labeled_charge_keys = _occurrence_keys(gt.label for gt in success.charges)

    predicted_assoc = [
        predicted_charge_keys[p.charge_index] if p.charge_index is not None else None
        for p in extraction.promotions
    ]
    labeled_assoc = [
        labeled_charge_keys[p.charge_ref] if p.charge_ref is not None else None
        for p in labeled_promotions
    ]
    predicted_keys = _promotion_keys(predicted_assoc)
    labeled_keys = _promotion_keys(labeled_assoc)
    labeled_by_key = dict(zip(labeled_keys, labeled_promotions, strict=True))
    matched: set[tuple[object, int]] = set()

    precision = t.metric("promotions.expiry_precision")
    precision.denominator += len(extraction.promotions)
    for i, (key, promo) in enumerate(
            zip(predicted_keys, extraction.promotions, strict=True)):
        gt = labeled_by_key.get(key)
        if gt is None:
            # A predicted expiry with no printed counterpart is the §6.1
            # inference violation, counted by name and gating at zero.
            t.bump("promotion_expiry_false_positives")
            t.findings.append(Finding(
                sample.sample_id, f"promotions.predicted[{i}]",
                DiagnosticCode.PROMO_EXPIRY_FALSE_POSITIVE))
            needs_correction = True
            continue
        matched.add(key)
        if promo.expiry_date.value == gt.expiry_date:
            precision.numerator += 1
            t.metric("promotions.expiry_recall").numerator += 1
        else:
            t.findings.append(Finding(
                sample.sample_id, f"promotions.predicted[{i}]",
                DiagnosticCode.PROMO_EXPIRY_MISMATCH))
            needs_correction = True
    for j, key in enumerate(labeled_keys):
        if key not in matched:
            t.findings.append(Finding(
                sample.sample_id, f"promotions.labeled[{j}]",
                DiagnosticCode.PROMO_EXPIRY_MISSED))
            needs_correction = True
    return needs_correction


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def gate_verdict(tallies: EvaluationTallies) -> tuple[str, tuple[str, ...]]:
    """PASS only when every critical metric is measurable and >= 0.90 and
    every systematic counter is zero. Failures are closed reason strings."""
    failures: list[str] = []
    critical = [f"field.{name}.accuracy" for name in CRITICAL_SCALAR_FIELDS]
    critical.extend(CRITICAL_CHARGE_METRICS)
    for name in critical:
        tally = tallies.metrics[name]
        if not tally.measurable:
            failures.append(f"NOT_MEASURABLE:{name}")
        elif not tally.meets(GATE_THRESHOLD):
            failures.append(f"BELOW_THRESHOLD:{name}")
    for counter in _COUNTER_NAMES:
        if tallies.counters.get(counter, 0) != 0:
            failures.append(f"SYSTEMATIC:{counter}")
    return ("PASS" if not failures else "FAIL", tuple(sorted(failures)))


def metrics_payload(tallies: EvaluationTallies) -> Mapping[str, object]:
    return {name: tally.as_payload()
            for name, tally in sorted(tallies.metrics.items())}


__all__ = [
    "CHARGE_MATCHING_POLICY_VERSION",
    "CRITICAL_CHARGE_METRICS",
    "CRITICAL_FIELD_REGISTRY_VERSION",
    "CRITICAL_SCALAR_FIELDS",
    "GATE_THRESHOLD",
    "METRIC_POLICY_VERSION",
    "EvaluationTallies",
    "Finding",
    "MetricTally",
    "SampleOutcome",
    "SampleResult",
    "evaluate_samples",
    "gate_verdict",
    "metrics_payload",
    "normalize_label_text",
]
