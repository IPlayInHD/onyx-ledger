"""The evaluation report: body, envelope, hash, and the two views.

NO SELF-REFERENTIAL HASH. `EvaluationReportBody` contains no report hash;
`report_hash = domain_hash(DOMAIN_BILLSHIELD_EVAL_REPORT, body.as_canonical())`
and the envelope carries `{body, report_hash}`. Verification recomputes from
the body, so an edited report cannot keep its predecessor's identity, and
changing ANY bound provenance — adapter, model, prompt, policy version,
manifest identity — moves the hash.

TWO VIEWS, ONE RULE EACH. The PUBLIC view is the committable aggregate the
plan permits: counts, metrics, counters, provenance. It contains no sample
ids, no bill-issuer names or grouping (there is no governed issuer resolver
yet), no charge labels or values, no evidence, no filenames, no provider
responses — and the writer refuses to emit it if a sample id somehow appears
in its bytes. The PRIVATE diagnostic view carries opaque sample ids and
closed codes only, lives under an external output root, and for
customer-derived corpora may never be written inside a Git repository.

Deferred §10 metrics appear BY NAME with an explicit status — never omitted,
never fabricated: the price-creep false-positive rate has no analyzer yet,
latency and cost are meaningless for the fixture adapter, and the real user
correction rate has no review flow — `samples.requires_correction_proxy` is
its named stand-in.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.services.billshield.evaluation.metrics import (
    GATE_THRESHOLD,
    EvaluationTallies,
    gate_verdict,
    metrics_payload,
)
from app.services.ioe.domain import canonical as c

REPORT_SCHEMA_VERSION = "1.0.0"

#: §10.2 metrics this slice cannot honestly measure, by name and status.
DEFERRED_METRICS: Mapping[str, str] = {
    "price_creep_false_positive_rate": "DEFERRED_NO_ANALYZER",
    "latency_p50_p95": "NOT_MEASURABLE_FIXTURE_ADAPTER",
    "cost_per_confirmed_bill": "NOT_MEASURABLE_FIXTURE_ADAPTER",
    "user_correction_rate": "DEFERRED_NO_REVIEW_FLOW",
}


@dataclass(frozen=True)
class ReportProvenance:
    """Everything a reader needs to know what was evaluated, by what."""

    extraction_schema_version: str
    report_schema_version: str
    manifest_schema_version: str
    label_schema_version: str
    metric_policy_version: str
    charge_matching_policy_version: str
    critical_field_registry_version: str
    canonical_serialization_version: str
    manifest_identity: str
    corpus_id: str
    adapter_code: str
    model_version: str
    prompt_version: str | None

    def as_canonical(self) -> dict[str, object]:
        return {
            "extraction_schema_version": self.extraction_schema_version,
            "report_schema_version": self.report_schema_version,
            "manifest_schema_version": self.manifest_schema_version,
            "label_schema_version": self.label_schema_version,
            "metric_policy_version": self.metric_policy_version,
            "charge_matching_policy_version": self.charge_matching_policy_version,
            "critical_field_registry_version": self.critical_field_registry_version,
            "canonical_serialization_version": self.canonical_serialization_version,
            "manifest_identity": self.manifest_identity,
            "corpus_id": self.corpus_id,
            "adapter_code": self.adapter_code,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
        }


@dataclass(frozen=True)
class EvaluationReportBody:
    """The hashed content. Aggregate only — nothing sample-level lives here,
    so the public view is a rendering of the body, not a redaction of it."""

    provenance: ReportProvenance
    counts: Mapping[str, int]
    counts_by_origin: Mapping[str, int]
    counts_by_category: Mapping[str, int]
    counts_by_language_layout: Mapping[str, int]
    refusal_counts: Mapping[str, int]
    metrics: Mapping[str, object]
    kind_confusion: Mapping[str, int]
    systematic_counters: Mapping[str, int]
    deferred_metrics: Mapping[str, str]
    gate_threshold: str
    gate_verdict: str
    gate_failures: tuple[str, ...]

    def as_canonical(self) -> dict[str, object]:
        return {
            "provenance": self.provenance.as_canonical(),
            "counts": dict(sorted(self.counts.items())),
            "counts_by_origin": dict(sorted(self.counts_by_origin.items())),
            "counts_by_category": dict(sorted(self.counts_by_category.items())),
            "counts_by_language_layout": dict(
                sorted(self.counts_by_language_layout.items())),
            "refusal_counts": dict(sorted(self.refusal_counts.items())),
            "metrics": dict(sorted(self.metrics.items())),
            "kind_confusion": dict(sorted(self.kind_confusion.items())),
            "systematic_counters": dict(sorted(self.systematic_counters.items())),
            "deferred_metrics": dict(sorted(self.deferred_metrics.items())),
            "gate_threshold": self.gate_threshold,
            "gate_verdict": self.gate_verdict,
            "gate_failures": list(self.gate_failures),
        }

    def report_hash(self) -> str:
        return c.domain_hash(c.DOMAIN_BILLSHIELD_EVAL_REPORT, self.as_canonical())


def build_report_body(
    tallies: EvaluationTallies,
    *,
    provenance: ReportProvenance,
    counts_by_origin: Mapping[str, int],
    counts_by_category: Mapping[str, int],
    counts_by_language_layout: Mapping[str, int],
) -> EvaluationReportBody:
    verdict, failures = gate_verdict(tallies)
    return EvaluationReportBody(
        provenance=provenance,
        counts={
            "samples_total": tallies.samples_total,
            "readable_expected": tallies.readable_total,
            "refusal_expected": tallies.refusal_expected_total,
            "succeeded": tallies.succeeded_total,
            "refused": tallies.refused_total,
            "invalid_output": tallies.invalid_output_total,
        },
        counts_by_origin=dict(counts_by_origin),
        counts_by_category=dict(counts_by_category),
        counts_by_language_layout=dict(counts_by_language_layout),
        refusal_counts=dict(tallies.refusal_counts),
        metrics=metrics_payload(tallies),
        kind_confusion=dict(tallies.kind_confusion),
        systematic_counters=dict(sorted(tallies.counters.items())),
        deferred_metrics=dict(DEFERRED_METRICS),
        gate_threshold=str(GATE_THRESHOLD),
        gate_verdict=verdict,
        gate_failures=failures,
    )


def public_envelope(body: EvaluationReportBody) -> dict[str, object]:
    """The committable aggregate view: `{body, report_hash}`."""
    return {"body": body.as_canonical(), "report_hash": body.report_hash()}


def verify_envelope(envelope: Mapping[str, object]) -> bool:
    """Recompute the hash from the body — no self-reference to trust."""
    body = envelope.get("body")
    claimed = envelope.get("report_hash")
    if not isinstance(body, Mapping) or not isinstance(claimed, str):
        return False
    return c.domain_hash(c.DOMAIN_BILLSHIELD_EVAL_REPORT, body) == claimed


def diagnostic_payload(
    tallies: EvaluationTallies, *, report_hash: str, corpus_id: str
) -> dict[str, object]:
    """The PRIVATE view: opaque ids and closed codes, bound to the report it
    diagnoses. Sorted so the bytes are deterministic."""
    findings = sorted(
        tallies.findings,
        key=lambda f: (f.sample_id, f.subject, f.code.value),
    )
    return {
        "corpus_id": corpus_id,
        "report_hash": report_hash,
        "findings": [
            {"sample_id": f.sample_id, "subject": f.subject, "code": f.code}
            for f in findings
        ],
    }


__all__ = [
    "DEFERRED_METRICS",
    "REPORT_SCHEMA_VERSION",
    "EvaluationReportBody",
    "ReportProvenance",
    "build_report_body",
    "diagnostic_payload",
    "public_envelope",
    "verify_envelope",
]
