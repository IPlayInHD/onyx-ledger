"""The private-corpus manifest: locate-and-verify data, and nothing else.

The repository is public, so the manifest may carry only what is needed to
find a sample under an external root and prove it is the sample the labels
were written for: opaque ids, byte digests, closed coverage enums, and a
provenance record. There is deliberately NO path field — file locations are
DERIVED from the sample id, whose charset cannot express traversal, and the
runner still checks the resolved path stays under the corpus root.

Provenance is a closed origin model. A synthetic file has no human to
consent, so synthetic samples carry NOT_APPLICABLE for consent and redaction;
a customer-derived sample must hold APPROVED for both before the runner will
touch it. Every incoherent combination fails closed — a manifest cannot mark
synthetic data as though a person consented, and cannot wave a customer's
bill through without approval states.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.services.billshield.extraction.codes import EvaluationErrorCode
from app.services.billshield.extraction.ports import ArtifactFormat
from app.services.ioe.domain import canonical as c

MANIFEST_SCHEMA_VERSION = "1.0.0"
#: THE label-schema authority. Defined here — the one module both the
#: manifest reader and the label reader can share without a cycle (labels.py
#: already imports from this module) — and re-exported by labels.py so
#: existing importers keep one name for one constant. `parse_manifest`
#: refuses any other declaration, so a report's provenance can carry the
#: manifest's VERIFIED declaration rather than substituting a constant the
#: manifest never actually matched.
LABEL_SCHEMA_VERSION = "1.0.0"

_CORPUS_ID_RE = re.compile(r"c-[0-9a-f]{12}")
_SAMPLE_ID_RE = re.compile(r"s-[0-9a-f]{12}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class EvaluationLimits:
    """Named, versioned bounds for the OFFLINE EVALUATOR only.

    These are tooling limits — they are not, and must never be presented as,
    future customer-upload limits. Byte limits are enforced by stat BEFORE
    content is read into memory.
    """

    version: str
    max_artifact_bytes: int
    max_artifact_pages: int
    max_label_bytes: int
    max_manifest_samples: int


EVALUATION_LIMITS_V1 = EvaluationLimits(
    version="1.0.0",
    max_artifact_bytes=25 * 1024 * 1024,
    max_artifact_pages=50,
    max_label_bytes=2 * 1024 * 1024,
    max_manifest_samples=500,
)


class SampleOrigin(StrEnum):
    SYNTHETIC = "synthetic"
    CUSTOMER_DERIVED = "customer_derived"


class ApprovalState(StrEnum):
    APPROVED = "approved"
    PENDING = "pending"
    REVOKED = "revoked"
    NOT_APPLICABLE = "not_applicable"


class LanguageLayout(StrEnum):
    """Closed, non-sensitive coverage metadata (§10.1 bilingual layouts)."""

    EN = "en"
    FR = "fr"
    BILINGUAL = "bilingual"


class EvaluationError(ValueError):
    """The evaluator refused. A closed code plus OUR wording; the only data a
    message may carry is an opaque id or a digest — never content."""

    def __init__(self, code: EvaluationErrorCode, detail: str = ""):
        self.code = code
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"{code}{suffix}")


@dataclass(frozen=True)
class CorpusSample:
    sample_id: str
    origin: SampleOrigin
    artifact_sha256: str
    artifact_format: ArtifactFormat
    page_count: int
    language_layout: LanguageLayout
    label_sha256: str
    consent_state: ApprovalState
    redaction_state: ApprovalState

    def as_canonical(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "origin": self.origin,
            "artifact_sha256": self.artifact_sha256,
            "artifact_format": self.artifact_format,
            "page_count": self.page_count,
            "language_layout": self.language_layout,
            "label_sha256": self.label_sha256,
            "consent_state": self.consent_state,
            "redaction_state": self.redaction_state,
        }


@dataclass(frozen=True)
class CorpusManifest:
    manifest_schema_version: str
    label_schema_version: str
    corpus_id: str
    samples: tuple[CorpusSample, ...]

    def as_canonical(self) -> dict[str, object]:
        return {
            "manifest_schema_version": self.manifest_schema_version,
            "label_schema_version": self.label_schema_version,
            "corpus_id": self.corpus_id,
            # A corpus is a SET of samples; the order an operator listed them
            # in is not part of what was evaluated.
            "samples": [
                sample.as_canonical()
                for sample in sorted(self.samples, key=lambda s: s.sample_id)
            ],
        }

    def manifest_identity(self) -> str:
        """The semantic identity every report binds: exactly which samples,
        digests, and provenance states this evaluation claims to cover."""
        return c.domain_hash(c.DOMAIN_BILLSHIELD_EVAL_MANIFEST, self.as_canonical())

    @property
    def has_customer_derived(self) -> bool:
        return any(s.origin is SampleOrigin.CUSTOMER_DERIVED for s in self.samples)


def _text(payload: Mapping[str, object], key: str, what: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise EvaluationError(
            EvaluationErrorCode.MALFORMED_MANIFEST, f"{what}.{key} must be text")
    return value


def _enum(payload: Mapping[str, object], key: str, enum_cls: type, what: str) -> object:
    raw = _text(payload, key, what)
    try:
        return enum_cls(raw)
    except ValueError:
        allowed = sorted(m.value for m in enum_cls)  # type: ignore[attr-defined]
        raise EvaluationError(
            EvaluationErrorCode.MALFORMED_MANIFEST,
            f"{what}.{key} must be one of {allowed}") from None


def _parse_sample(
    payload: object, position: int, *, limits: EvaluationLimits
) -> CorpusSample:
    what = f"samples[{position}]"
    if not isinstance(payload, Mapping):
        raise EvaluationError(
            EvaluationErrorCode.MALFORMED_MANIFEST, f"{what} must be an object")
    known = {
        "sample_id", "origin", "artifact_sha256", "artifact_format",
        "page_count", "language_layout", "label_sha256", "consent_state",
        "redaction_state",
    }
    unknown = sorted(set(payload) - known)
    if unknown:
        raise EvaluationError(
            EvaluationErrorCode.MALFORMED_MANIFEST,
            f"{what} has {len(unknown)} unknown field(s); known fields are "
            f"{sorted(known)}")

    sample_id = _text(payload, "sample_id", what)
    if not _SAMPLE_ID_RE.fullmatch(sample_id):
        raise EvaluationError(
            EvaluationErrorCode.INVALID_SAMPLE_ID,
            f"{what}: sample ids are opaque and match s-<12 hex>")

    artifact_sha = _text(payload, "artifact_sha256", what)
    label_sha = _text(payload, "label_sha256", what)
    for digest, key in ((artifact_sha, "artifact_sha256"), (label_sha, "label_sha256")):
        if not _SHA256_RE.fullmatch(digest):
            raise EvaluationError(
                EvaluationErrorCode.MALFORMED_MANIFEST,
                f"{what}.{key} must be 64 lowercase hex characters")

    page_count = payload.get("page_count")
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
        raise EvaluationError(
            EvaluationErrorCode.MALFORMED_MANIFEST,
            f"{what}.page_count must be a whole number >= 1")
    if page_count > limits.max_artifact_pages:
        raise EvaluationError(
            EvaluationErrorCode.TOO_MANY_PAGES,
            f"{sample_id}: page_count exceeds "
            f"EVALUATION_LIMITS_V1.max_artifact_pages")

    origin = _enum(payload, "origin", SampleOrigin, what)
    consent = _enum(payload, "consent_state", ApprovalState, what)
    redaction = _enum(payload, "redaction_state", ApprovalState, what)
    assert isinstance(origin, SampleOrigin)
    assert isinstance(consent, ApprovalState)
    assert isinstance(redaction, ApprovalState)

    # The closed origin model. Synthetic data has no human to consent;
    # customer-derived data has no NOT_APPLICABLE escape hatch.
    if origin is SampleOrigin.SYNTHETIC:
        if consent is not ApprovalState.NOT_APPLICABLE or (
                redaction is not ApprovalState.NOT_APPLICABLE):
            raise EvaluationError(
                EvaluationErrorCode.INCOHERENT_PROVENANCE,
                f"{sample_id}: synthetic samples carry not_applicable "
                "consent/redaction — nobody consented to synthetic bytes")
    else:
        if ApprovalState.NOT_APPLICABLE in (consent, redaction):
            raise EvaluationError(
                EvaluationErrorCode.INCOHERENT_PROVENANCE,
                f"{sample_id}: customer-derived samples require explicit "
                "consent and redaction states")

    fmt = _enum(payload, "artifact_format", ArtifactFormat, what)
    layout = _enum(payload, "language_layout", LanguageLayout, what)
    assert isinstance(fmt, ArtifactFormat)
    assert isinstance(layout, LanguageLayout)

    return CorpusSample(
        sample_id=sample_id, origin=origin, artifact_sha256=artifact_sha,
        artifact_format=fmt, page_count=page_count, language_layout=layout,
        label_sha256=label_sha, consent_state=consent, redaction_state=redaction,
    )


def parse_manifest(
    payload: object, *, limits: EvaluationLimits = EVALUATION_LIMITS_V1
) -> CorpusManifest:
    if not isinstance(payload, Mapping):
        raise EvaluationError(
            EvaluationErrorCode.MALFORMED_MANIFEST, "a manifest must be an object")
    known = {
        "manifest_schema_version", "label_schema_version", "corpus_id", "samples",
    }
    unknown = sorted(set(payload) - known)
    if unknown:
        raise EvaluationError(
            EvaluationErrorCode.MALFORMED_MANIFEST,
            f"manifest has {len(unknown)} unknown field(s); known fields are "
            f"{sorted(known)}")

    declared = payload.get("manifest_schema_version")
    if declared != MANIFEST_SCHEMA_VERSION:
        raise EvaluationError(
            EvaluationErrorCode.UNSUPPORTED_MANIFEST_VERSION,
            f"this evaluator reads manifest schema {MANIFEST_SCHEMA_VERSION!r}")
    label_version = payload.get("label_schema_version")
    if not isinstance(label_version, str) or not label_version:
        raise EvaluationError(
            EvaluationErrorCode.MALFORMED_MANIFEST,
            "label_schema_version must be text")
    if label_version != LABEL_SCHEMA_VERSION:
        raise EvaluationError(
            EvaluationErrorCode.UNSUPPORTED_LABEL_VERSION,
            f"this evaluator reads label schema {LABEL_SCHEMA_VERSION!r}")

    corpus_id = _text(payload, "corpus_id", "manifest")
    if not _CORPUS_ID_RE.fullmatch(corpus_id):
        raise EvaluationError(
            EvaluationErrorCode.INVALID_CORPUS_ID,
            "corpus ids are opaque and match c-<12 hex>")

    samples_raw = payload.get("samples")
    if not isinstance(samples_raw, list):
        raise EvaluationError(
            EvaluationErrorCode.MALFORMED_MANIFEST, "samples must be a list")
    if len(samples_raw) > limits.max_manifest_samples:
        raise EvaluationError(
            EvaluationErrorCode.TOO_MANY_SAMPLES,
            "manifest exceeds EVALUATION_LIMITS_V1.max_manifest_samples")

    samples = tuple(
        _parse_sample(item, i, limits=limits) for i, item in enumerate(samples_raw))

    seen_ids: set[str] = set()
    seen_digests: set[str] = set()
    for sample in samples:
        if sample.sample_id in seen_ids:
            raise EvaluationError(
                EvaluationErrorCode.DUPLICATE_SAMPLE_ID, sample.sample_id)
        seen_ids.add(sample.sample_id)
        if sample.artifact_sha256 in seen_digests:
            raise EvaluationError(
                EvaluationErrorCode.DUPLICATE_ARTIFACT_DIGEST, sample.sample_id)
        seen_digests.add(sample.artifact_sha256)

    return CorpusManifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        label_schema_version=label_version,
        corpus_id=corpus_id,
        samples=samples,
    )


__all__ = [
    "EVALUATION_LIMITS_V1",
    "LABEL_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "ApprovalState",
    "CorpusManifest",
    "CorpusSample",
    "EvaluationError",
    "EvaluationLimits",
    "LanguageLayout",
    "SampleOrigin",
    "parse_manifest",
]
