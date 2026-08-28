"""The typed evaluation runner — verification, invocation, reporting, output.

This module, not the CLI, owns the logic, so the whole path from manifest to
report sits inside the protected mypy scope. The CLI under `scripts/` is an
argparse wrapper and nothing else.

THE CORPUS IS NAMED, NEVER FOUND. The caller passes an explicit manifest path
and corpus root; nothing here scans a repository, a home directory, or any
default location. Sample files are DERIVED from opaque ids under that root,
resolved, and checked to still be under it. Byte limits are enforced by
`stat` BEFORE content is read into memory, digests are verified before use,
and customer-derived samples run only with approved consent and redaction.

THE PROVIDER IS INJECTED. Any `BillExtractionProvider` — the runner is not
hardwired to the fixture adapter; Slice 1's CLI simply exposes no other.

OUTPUT POLICY. The public aggregate envelope self-checks for sample-id leaks
before writing. A private diagnostic is written only where explicitly asked,
and for a corpus containing ANY customer-derived sample the runner refuses a
diagnostic path inside a Git repository or worktree (a `.git` directory or
file anywhere above the resolved destination), so a per-sample report cannot
land next to code that gets pushed.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.services.billshield.evaluation.labels import (
    GroundTruthLabel,
    Labeled,
    parse_label,
)
from app.services.billshield.evaluation.manifest import (
    EVALUATION_LIMITS_V1,
    MANIFEST_SCHEMA_VERSION,
    ApprovalState,
    CorpusManifest,
    CorpusSample,
    EvaluationError,
    EvaluationLimits,
    SampleOrigin,
    parse_manifest,
)
from app.services.billshield.evaluation.metrics import (
    CHARGE_MATCHING_POLICY_VERSION,
    CRITICAL_FIELD_REGISTRY_VERSION,
    METRIC_POLICY_VERSION,
    SampleOutcome,
    SampleResult,
    evaluate_samples,
)
from app.services.billshield.evaluation.report import (
    REPORT_SCHEMA_VERSION,
    EvaluationReportBody,
    ReportProvenance,
    build_report_body,
    diagnostic_payload,
    public_envelope,
)
from app.services.billshield.extraction.codes import EvaluationErrorCode
from app.services.billshield.extraction.contract import (
    BILL_EXTRACTION_SCHEMA_VERSION,
    BillExtractionRefusal,
    BillExtractionV1,
)
from app.services.billshield.extraction.parser import ExtractionParseError
from app.services.billshield.extraction.ports import (
    BillExtractionProvider,
    ValidatedBillArtifact,
)
from app.services.ioe.domain import canonical as c


@dataclass(frozen=True)
class EvaluationRunResult:
    body: EvaluationReportBody
    report_hash: str
    public: dict[str, object]
    diagnostics: dict[str, object]
    manifest: CorpusManifest


def _read_bounded(
    path: Path, *, limit: int, missing: EvaluationErrorCode,
    too_large: EvaluationErrorCode, sample_id: str,
) -> bytes:
    """A genuinely bounded read: one descriptor, no reopen, no unbounded call.

    The file is opened ONCE; `os.fstat` on that same descriptor rejects a
    file already over the limit, and the single read asks for at most
    `limit + 1` bytes — so a file that grew between `fstat` and the read is
    caught by the length check rather than swallowed whole. There is no
    stat-then-reopen window, and no code path reads more than `limit + 1`
    bytes into memory.
    """
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        raise EvaluationError(missing, sample_id) from None
    with handle:
        if os.fstat(handle.fileno()).st_size > limit:
            raise EvaluationError(too_large, sample_id)
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise EvaluationError(too_large, sample_id)
    return data


def _resolve_under(root: Path, relative: str, sample_id: str) -> Path:
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise EvaluationError(
            EvaluationErrorCode.PATH_ESCAPES_CORPUS_ROOT, sample_id)
    return resolved


_ARTIFACT_EXTENSIONS = {
    "pdf_native": "pdf",
    "pdf_scanned": "pdf",
    "image_png": "png",
    "image_jpeg": "jpg",
}


def _load_sample(
    sample: CorpusSample, root: Path, *, limits: EvaluationLimits
) -> tuple[ValidatedBillArtifact, object]:
    if sample.origin is SampleOrigin.CUSTOMER_DERIVED:
        if sample.consent_state is not ApprovalState.APPROVED:
            raise EvaluationError(
                EvaluationErrorCode.SAMPLE_NOT_CONSENTED, sample.sample_id)
        if sample.redaction_state is not ApprovalState.APPROVED:
            raise EvaluationError(
                EvaluationErrorCode.SAMPLE_NOT_REDACTION_APPROVED,
                sample.sample_id)

    extension = _ARTIFACT_EXTENSIONS[sample.artifact_format.value]
    artifact_path = _resolve_under(
        root, f"artifacts/{sample.sample_id}.{extension}", sample.sample_id)
    label_path = _resolve_under(
        root, f"labels/{sample.sample_id}.json", sample.sample_id)

    artifact_bytes = _read_bounded(
        artifact_path, limit=limits.max_artifact_bytes,
        missing=EvaluationErrorCode.MISSING_ARTIFACT,
        too_large=EvaluationErrorCode.ARTIFACT_TOO_LARGE,
        sample_id=sample.sample_id)
    if hashlib.sha256(artifact_bytes).hexdigest() != sample.artifact_sha256:
        raise EvaluationError(
            EvaluationErrorCode.ARTIFACT_HASH_MISMATCH, sample.sample_id)

    label_bytes = _read_bounded(
        label_path, limit=limits.max_label_bytes,
        missing=EvaluationErrorCode.MISSING_LABEL,
        too_large=EvaluationErrorCode.LABEL_TOO_LARGE,
        sample_id=sample.sample_id)
    if hashlib.sha256(label_bytes).hexdigest() != sample.label_sha256:
        raise EvaluationError(
            EvaluationErrorCode.LABEL_HASH_MISMATCH, sample.sample_id)
    try:
        label_payload = json.loads(label_bytes)
    except json.JSONDecodeError:
        raise EvaluationError(
            EvaluationErrorCode.MALFORMED_LABEL, sample.sample_id) from None

    artifact = ValidatedBillArtifact(
        artifact_sha256=sample.artifact_sha256,
        artifact_format=sample.artifact_format,
        page_count=sample.page_count,
        content=artifact_bytes,
    )
    return artifact, label_payload


async def evaluate_corpus(
    *,
    manifest_path: Path,
    corpus_root: Path,
    provider: BillExtractionProvider,
    limits: EvaluationLimits = EVALUATION_LIMITS_V1,
) -> EvaluationRunResult:
    root = corpus_root.resolve()
    manifest_bytes = _read_bounded(
        manifest_path, limit=limits.max_label_bytes,
        missing=EvaluationErrorCode.MALFORMED_MANIFEST,
        too_large=EvaluationErrorCode.MALFORMED_MANIFEST,
        sample_id="manifest file missing or oversized")
    try:
        manifest_payload = json.loads(manifest_bytes)
    except json.JSONDecodeError:
        raise EvaluationError(
            EvaluationErrorCode.MALFORMED_MANIFEST, "not valid JSON") from None
    manifest = parse_manifest(manifest_payload, limits=limits)

    results: list[SampleResult] = []
    counts_by_origin: dict[str, int] = {}
    counts_by_category: dict[str, int] = {}
    counts_by_language_layout: dict[str, int] = {}
    for sample in sorted(manifest.samples, key=lambda s: s.sample_id):
        artifact, label_payload = _load_sample(sample, root, limits=limits)
        label = parse_label(label_payload)

        outcome = SampleOutcome.SUCCEEDED
        refusal_code = None
        extraction: BillExtractionV1 | None = None
        try:
            produced = await provider.extract(artifact)
        except ExtractionParseError:
            outcome = SampleOutcome.INVALID_OUTPUT
        else:
            if isinstance(produced, BillExtractionRefusal):
                outcome = SampleOutcome.REFUSED
                refusal_code = produced.code
            else:
                extraction = produced

        results.append(SampleResult(
            sample_id=sample.sample_id, label=label, outcome=outcome,
            refusal_code=refusal_code, extraction=extraction))
        origin = sample.origin.value
        counts_by_origin[origin] = counts_by_origin.get(origin, 0) + 1
        layout = sample.language_layout.value
        counts_by_language_layout[layout] = (
            counts_by_language_layout.get(layout, 0) + 1)
        category = _labeled_category(label)
        counts_by_category[category] = counts_by_category.get(category, 0) + 1

    tallies = evaluate_samples(results)
    provenance = ReportProvenance(
        extraction_schema_version=BILL_EXTRACTION_SCHEMA_VERSION,
        report_schema_version=REPORT_SCHEMA_VERSION,
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        # The VERIFIED declaration: parse_manifest refused anything but the
        # supported label schema, so the provenance reports what the manifest
        # actually declared rather than substituting a constant.
        label_schema_version=manifest.label_schema_version,
        metric_policy_version=METRIC_POLICY_VERSION,
        charge_matching_policy_version=CHARGE_MATCHING_POLICY_VERSION,
        critical_field_registry_version=CRITICAL_FIELD_REGISTRY_VERSION,
        canonical_serialization_version=c.CANONICAL_SERIALIZATION_VERSION,
        manifest_identity=manifest.manifest_identity(),
        corpus_id=manifest.corpus_id,
        adapter_code=provider.adapter_code,
        model_version=provider.model_version,
        prompt_version=provider.prompt_version,
    )
    body = build_report_body(
        tallies, provenance=provenance,
        counts_by_origin=counts_by_origin,
        counts_by_category=counts_by_category,
        counts_by_language_layout=counts_by_language_layout,
    )
    report_hash = body.report_hash()
    return EvaluationRunResult(
        body=body,
        report_hash=report_hash,
        public=public_envelope(body),
        diagnostics=diagnostic_payload(
            tallies, report_hash=report_hash, corpus_id=manifest.corpus_id),
        manifest=manifest,
    )


def _labeled_category(label: GroundTruthLabel) -> str:
    """The GT service category as a closed grouping key, or a closed bucket.

    Grouping is by LABELED category — never by issuer, for which no governed
    resolver exists yet.
    """
    if label.success is None:
        return "refusal_expected"
    truth = label.success.scalars["service_category"]
    if isinstance(truth, Labeled):
        return str(truth.value)
    return "unlabeled"


def serialize_payload(payload: Mapping[str, object]) -> bytes:
    """Deterministic bytes for reports and goldens: canonicalized (sorted
    keys, fixed scales) then dumped with the canonical separators."""
    return (c.dumps(c.canonicalize(payload)) + "\n").encode("utf-8")


def _inside_git_repository(path: Path) -> bool:
    """A `.git` DIRECTORY (ordinary repo) or FILE (worktree) anywhere above
    the resolved destination means the destination is inside a repository."""
    for ancestor in (path, *path.parents):
        marker = ancestor / ".git"
        if marker.is_dir() or marker.is_file():
            return True
    return False


def write_outputs(
    result: EvaluationRunResult,
    *,
    public_out: Path | None,
    diagnostic_out: Path | None,
) -> None:
    if public_out is not None:
        rendered = serialize_payload(result.public)
        text = rendered.decode("utf-8")
        for sample in result.manifest.samples:
            if sample.sample_id in text:
                raise EvaluationError(
                    EvaluationErrorCode.PUBLIC_REPORT_LEAK, sample.sample_id)
        public_out.parent.mkdir(parents=True, exist_ok=True)
        public_out.write_bytes(rendered)
    if diagnostic_out is not None:
        destination = diagnostic_out.resolve()
        if result.manifest.has_customer_derived and _inside_git_repository(
                destination.parent):
            raise EvaluationError(
                EvaluationErrorCode.DIAGNOSTIC_PATH_IN_REPOSITORY,
                "customer-derived diagnostics stay under the external root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(serialize_payload(result.diagnostics))


__all__ = [
    "EvaluationRunResult",
    "evaluate_corpus",
    "serialize_payload",
    "write_outputs",
]
