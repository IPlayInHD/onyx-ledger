"""The deterministic fixture adapter, and the private-corpus manifest."""
from __future__ import annotations

import copy
import hashlib

import pytest

from app.services.billshield.evaluation.manifest import (
    EVALUATION_LIMITS_V1,
    EvaluationError,
    EvaluationLimits,
    parse_manifest,
)
from app.services.billshield.extraction.codes import EvaluationErrorCode
from app.services.billshield.extraction.contract import BillExtractionV1
from app.services.billshield.extraction.fixture import (
    DeterministicFixtureExtractionProvider,
    FixtureConfigurationError,
)
from app.services.billshield.extraction.ports import (
    ArtifactFormat,
    BillExtractionProvider,
    ValidatedBillArtifact,
)

_BYTES = b"synthetic fixture artifact bytes"
_SHA = hashlib.sha256(_BYTES).hexdigest()


def _artifact() -> ValidatedBillArtifact:
    return ValidatedBillArtifact(
        artifact_sha256=_SHA, artifact_format=ArtifactFormat.PDF_NATIVE,
        page_count=2, content=_BYTES)


def _response() -> dict:
    return {
        "schema_version": "1.0.0",
        "extraction": {
            "currency": "CAD",
            "bill_issuer_name": {"state": "no_candidate"},
            "service_category": {"state": "no_candidate"},
            "statement_date": {"state": "no_candidate"},
            "billing_period": {"state": "no_candidate"},
            "amount_due": {
                "state": "present", "value": "42.00", "confidence": "0.9",
                "evidence": [{"page": 1, "x0": "0.1", "y0": "0.2",
                              "x1": "0.5", "y1": "0.25"}]},
            "previous_balance": {"state": "no_candidate"},
            "payments_applied": {"state": "no_candidate"},
            "subtotal_before_tax": {"state": "no_candidate"},
            "total_tax": {"state": "no_candidate"},
            "charges": [],
            "promotions": [],
        },
    }


async def test_the_fixture_is_deterministic_and_proves_the_protocol():
    provider = DeterministicFixtureExtractionProvider({_SHA: _response()})
    assert isinstance(provider, BillExtractionProvider)
    assert provider.adapter_code == "fixture"
    first = await provider.extract(_artifact())
    second = await provider.extract(_artifact())
    assert isinstance(first, BillExtractionV1)
    assert first == second
    assert first.extraction_identity() == second.extraction_identity()


async def test_an_unknown_artifact_is_a_test_configuration_error():
    """NOT a domain refusal: mapping this to UNREADABLE would let a miswired
    test measure a refusal that never happened."""
    provider = DeterministicFixtureExtractionProvider({})
    with pytest.raises(FixtureConfigurationError):
        await provider.extract(_artifact())


def test_artifact_content_is_out_of_repr_equality_and_errors():
    artifact = _artifact()
    assert _BYTES not in repr(artifact).encode()
    twin = ValidatedBillArtifact(
        artifact_sha256=_SHA, artifact_format=ArtifactFormat.PDF_NATIVE,
        page_count=2, content=b"different bytes, same digest metadata")
    assert artifact == twin, "identity is the digest, never the raw bytes"
    with pytest.raises(ValueError) as caught:
        ValidatedBillArtifact(
            artifact_sha256="nope", artifact_format=ArtifactFormat.PDF_NATIVE,
            page_count=1, content=_BYTES)
    assert _BYTES not in f"{caught.value!r}".encode()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def _sample(**overrides: object) -> dict:
    base: dict[str, object] = {
        "sample_id": "s-0123456789ab",
        "origin": "synthetic",
        "artifact_sha256": "a" * 64,
        "artifact_format": "pdf_native",
        "page_count": 2,
        "language_layout": "bilingual",
        "label_sha256": "b" * 64,
        "consent_state": "not_applicable",
        "redaction_state": "not_applicable",
    }
    base.update(overrides)
    return base


def _manifest(*samples: dict) -> dict:
    return {
        "manifest_schema_version": "1.0.0",
        "label_schema_version": "1.0.0",
        "corpus_id": "c-0123456789ab",
        "samples": list(samples),
    }


def _refused(payload: object, **kwargs: object) -> EvaluationError:
    with pytest.raises(EvaluationError) as caught:
        parse_manifest(payload, **kwargs)  # type: ignore[arg-type]
    return caught.value


def test_a_valid_manifest_parses_and_has_a_stable_identity():
    parsed = parse_manifest(_manifest(_sample()))
    again = parse_manifest(_manifest(_sample()))
    assert parsed.manifest_identity() == again.manifest_identity()
    assert not parsed.has_customer_derived


def test_manifest_identity_ignores_sample_listing_order():
    a = _sample()
    b = _sample(sample_id="s-ba9876543210", artifact_sha256="c" * 64)
    forward = parse_manifest(_manifest(a, b)).manifest_identity()
    backward = parse_manifest(_manifest(b, a)).manifest_identity()
    assert forward == backward


def test_synthetic_provenance_cannot_claim_consent():
    error = _refused(_manifest(_sample(consent_state="approved")))
    assert error.code is EvaluationErrorCode.INCOHERENT_PROVENANCE


def test_customer_derived_provenance_cannot_be_not_applicable():
    error = _refused(_manifest(_sample(
        origin="customer_derived",
        consent_state="not_applicable", redaction_state="approved")))
    assert error.code is EvaluationErrorCode.INCOHERENT_PROVENANCE


def test_customer_derived_with_explicit_states_parses():
    manifest = parse_manifest(_manifest(_sample(
        origin="customer_derived",
        consent_state="approved", redaction_state="approved")))
    assert manifest.has_customer_derived


def test_ids_are_opaque_and_validated():
    for bad_id in ("s-TELUS-0001", "../escape", "s-0123", "mobile-bill-1"):
        error = _refused(_manifest(_sample(sample_id=bad_id)))
        assert error.code is EvaluationErrorCode.INVALID_SAMPLE_ID, bad_id
    payload = _manifest(_sample())
    payload["corpus_id"] = "telecom-corpus"
    assert _refused(payload).code is EvaluationErrorCode.INVALID_CORPUS_ID


def test_duplicates_versions_and_unknown_fields_fail_closed():
    duplicate_id = _manifest(_sample(), _sample(artifact_sha256="c" * 64))
    assert _refused(duplicate_id).code is EvaluationErrorCode.DUPLICATE_SAMPLE_ID
    duplicate_digest = _manifest(
        _sample(), _sample(sample_id="s-ba9876543210"))
    assert _refused(
        duplicate_digest).code is EvaluationErrorCode.DUPLICATE_ARTIFACT_DIGEST
    wrong_version = _manifest(_sample())
    wrong_version["manifest_schema_version"] = "9.0.0"
    assert _refused(
        wrong_version).code is EvaluationErrorCode.UNSUPPORTED_MANIFEST_VERSION
    unknown = _manifest(_sample())
    unknown["provider"] = "should not exist"
    assert _refused(unknown).code is EvaluationErrorCode.MALFORMED_MANIFEST
    unknown_sample = _manifest(_sample(filename="bill.pdf"))
    assert _refused(unknown_sample).code is EvaluationErrorCode.MALFORMED_MANIFEST


def test_evaluation_limits_bound_pages_and_sample_count():
    tight = EvaluationLimits(
        version="test", max_artifact_bytes=10, max_artifact_pages=3,
        max_label_bytes=10, max_manifest_samples=1)
    too_many_pages = _manifest(_sample(page_count=4))
    assert _refused(
        too_many_pages, limits=tight).code is EvaluationErrorCode.TOO_MANY_PAGES
    too_many_samples = copy.deepcopy(_manifest(
        _sample(), _sample(sample_id="s-ba9876543210",
                           artifact_sha256="c" * 64)))
    assert _refused(
        too_many_samples,
        limits=tight).code is EvaluationErrorCode.TOO_MANY_SAMPLES
    assert EVALUATION_LIMITS_V1.max_artifact_pages == 50


def test_an_unsupported_manifest_label_version_is_rejected():
    """One authority: the manifest's declared label schema must equal the
    constant labels.py re-exports from manifest.py — '999.0.0' reaching
    gate=PASS while provenance claims 1.0.0 was the reproduced defect."""
    from app.services.billshield.evaluation.labels import (
        LABEL_SCHEMA_VERSION as reexported,
    )
    from app.services.billshield.evaluation.manifest import LABEL_SCHEMA_VERSION

    assert reexported is LABEL_SCHEMA_VERSION, (
        "two label-schema authorities exist; labels.py must re-export "
        "manifest.py's constant")
    payload = _manifest(_sample())
    payload["label_schema_version"] = "999.0.0"
    assert _refused(payload).code is (
        EvaluationErrorCode.UNSUPPORTED_LABEL_VERSION)


# ---------------------------------------------------------------------------
# Ground-truth labels
# ---------------------------------------------------------------------------
def _success_label(charge_kind: object) -> dict:
    return {
        "label_schema_version": "1.0.0",
        "expected_outcome": "success",
        "fields": {
            "bill_issuer_name": {"state": "present", "value": "Issuer"},
            "service_category": {"state": "absent_on_document"},
            "statement_date": {"state": "absent_on_document"},
            "billing_period": {"state": "absent_on_document"},
            "amount_due": {"state": "present", "value": "10.00"},
            "previous_balance": {"state": "absent_on_document"},
            "payments_applied": {"state": "absent_on_document"},
            "subtotal_before_tax": {"state": "absent_on_document"},
            "total_tax": {"state": "absent_on_document"},
        },
        "charges": [{"label": "Line item", "amount": "10.00",
                     "kind": charge_kind, "cadence": None}],
        "promotions": [],
    }


def test_unclassified_is_extractor_uncertainty_never_ground_truth():
    """A labeler who does not know the kind records null; UNCLASSIFIED in
    ground truth would let a hedging prediction score as correct."""
    import pytest as _pytest

    from app.services.billshield.evaluation.labels import parse_label
    from app.services.billshield.extraction.contract import ChargeKind

    with _pytest.raises(EvaluationError) as caught:
        parse_label(_success_label("UNCLASSIFIED"))
    assert caught.value.code is EvaluationErrorCode.MALFORMED_LABEL
    assert "Line item" not in str(caught.value), "bill content in an error"

    # every substantive kind remains valid ground truth ...
    for kind in ChargeKind:
        if kind is ChargeKind.UNCLASSIFIED:
            continue
        parsed = parse_label(_success_label(kind.value))
        assert parsed.success is not None
        assert parsed.success.charges[0].kind is kind
    # ... and null remains the not-labeled representation
    unlabeled = parse_label(_success_label(None))
    assert unlabeled.success is not None
    assert unlabeled.success.charges[0].kind is None
