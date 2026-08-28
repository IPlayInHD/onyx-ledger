"""The closed vocabularies BillShield extraction and evaluation decide with.

Machine decisions are made on machine-readable codes, never on message text —
the same rule `app/services/tax_kb/authoring/codes.py` states for the
authoring pipeline. A message explains a refusal to a person; it never IS the
refusal, and in this package a message additionally must never carry provider
content, because a provider payload is derived from somebody's bill.
"""
from __future__ import annotations

from enum import StrEnum


class RefusalCode(StrEnum):
    """What an extraction adapter may honestly say about a whole document.

    Closed and small on purpose. A refusal is for a document the adapter
    cannot or must not read — it is never the representation of a partial
    read, which is what `no_candidate` field states exist for. A media type
    BillShield cannot read is recorded as one of these, never as a successful
    extraction with no fields (plan §9.5; `workers/tasks/documents.py:13-19`
    makes the same argument for tax slips).
    """

    #: The bytes could not be turned into content at all.
    UNREADABLE = "UNREADABLE"
    #: A format the adapter does not support (wrong media, encrypted, etc.).
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    #: Readable, but not a bill in BillShield's supported service scope.
    UNSUPPORTED_SERVICE_SCOPE = "UNSUPPORTED_SERVICE_SCOPE"
    #: Readable and in scope, but no evidence-backed candidate could be
    #: produced. The honest alternative to an empty success.
    NO_USABLE_EXTRACTION = "NO_USABLE_EXTRACTION"


class ExtractionParseCode(StrEnum):
    """Why the strict parser refused a provider payload.

    One code per implemented constraint. The parser raises these with a JSON
    path; it never echoes a provider value, a provider key name, or bill text.
    """

    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    UNKNOWN_FIELD_STATE = "UNKNOWN_FIELD_STATE"
    MALFORMED_VALUE = "MALFORMED_VALUE"
    #: A number arrived in an inexact or non-finite representation: JSON
    #: float, int, bool, NaN, or Infinity where an exact decimal string is
    #: required.
    INEXACT_NUMBER = "INEXACT_NUMBER"
    #: An exact decimal string at the wrong scale — money must carry exactly
    #: two fractional digits; confidences and coordinates at most six.
    WRONG_SCALE = "WRONG_SCALE"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"
    UNKNOWN_ENUM_VALUE = "UNKNOWN_ENUM_VALUE"
    INCOHERENT_DATES = "INCOHERENT_DATES"
    INCOHERENT_SIGN = "INCOHERENT_SIGN"
    INCOHERENT_CADENCE = "INCOHERENT_CADENCE"
    INVALID_CHARGE_REFERENCE = "INVALID_CHARGE_REFERENCE"
    #: A "success" with no amount_due candidate and no charges. The adapter
    #: should have refused with NO_USABLE_EXTRACTION.
    EMPTY_SUCCESS = "EMPTY_SUCCESS"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    EVIDENCE_MALFORMED = "EVIDENCE_MALFORMED"
    EVIDENCE_OUT_OF_BOUNDS = "EVIDENCE_OUT_OF_BOUNDS"


class EvaluationErrorCode(StrEnum):
    """Why the offline evaluator refused to run, or refused a sample.

    Every one is a hard failure of the run: the evaluator never silently
    skips a sample, because a skipped sample is a measurement that quietly
    stopped covering what the manifest claims it covers.
    """

    MALFORMED_MANIFEST = "MALFORMED_MANIFEST"
    MALFORMED_LABEL = "MALFORMED_LABEL"
    UNSUPPORTED_MANIFEST_VERSION = "UNSUPPORTED_MANIFEST_VERSION"
    UNSUPPORTED_LABEL_VERSION = "UNSUPPORTED_LABEL_VERSION"
    INVALID_CORPUS_ID = "INVALID_CORPUS_ID"
    INVALID_SAMPLE_ID = "INVALID_SAMPLE_ID"
    PATH_ESCAPES_CORPUS_ROOT = "PATH_ESCAPES_CORPUS_ROOT"
    MISSING_ARTIFACT = "MISSING_ARTIFACT"
    MISSING_LABEL = "MISSING_LABEL"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    LABEL_HASH_MISMATCH = "LABEL_HASH_MISMATCH"
    INCOHERENT_PROVENANCE = "INCOHERENT_PROVENANCE"
    SAMPLE_NOT_CONSENTED = "SAMPLE_NOT_CONSENTED"
    SAMPLE_NOT_REDACTION_APPROVED = "SAMPLE_NOT_REDACTION_APPROVED"
    DUPLICATE_SAMPLE_ID = "DUPLICATE_SAMPLE_ID"
    DUPLICATE_ARTIFACT_DIGEST = "DUPLICATE_ARTIFACT_DIGEST"
    #: Checked by stat BEFORE any content is read into memory.
    ARTIFACT_TOO_LARGE = "ARTIFACT_TOO_LARGE"
    LABEL_TOO_LARGE = "LABEL_TOO_LARGE"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"
    TOO_MANY_SAMPLES = "TOO_MANY_SAMPLES"
    #: The adapter's output failed the strict parser. Counted per sample and
    #: treated as a miss everywhere a prediction was owed.
    PROVIDER_OUTPUT_INVALID = "PROVIDER_OUTPUT_INVALID"
    #: A private diagnostic may never land inside a Git repository.
    DIAGNOSTIC_PATH_IN_REPOSITORY = "DIAGNOSTIC_PATH_IN_REPOSITORY"
    #: The public report failed its own leak self-check before writing.
    PUBLIC_REPORT_LEAK = "PUBLIC_REPORT_LEAK"


class DiagnosticCode(StrEnum):
    """Per-sample findings in the PRIVATE diagnostic view.

    Diagnostics carry opaque sample ids, closed subject names, and these
    codes — never a predicted or labeled value, never bill text.
    """

    MISSING_PREDICTION = "MISSING_PREDICTION"
    SPURIOUS_PREDICTION = "SPURIOUS_PREDICTION"
    VALUE_MISMATCH = "VALUE_MISMATCH"
    SIGN_MISMATCH = "SIGN_MISMATCH"
    DECIMAL_SHIFT_MISMATCH = "DECIMAL_SHIFT_MISMATCH"
    LABEL_TEXT_MISMATCH = "LABEL_TEXT_MISMATCH"
    KIND_CONFUSION = "KIND_CONFUSION"
    CADENCE_MISMATCH = "CADENCE_MISMATCH"
    UNMATCHED_CHARGE_PREDICTED = "UNMATCHED_CHARGE_PREDICTED"
    UNMATCHED_CHARGE_LABELED = "UNMATCHED_CHARGE_LABELED"
    PROMO_EXPIRY_FALSE_POSITIVE = "PROMO_EXPIRY_FALSE_POSITIVE"
    PROMO_EXPIRY_MISSED = "PROMO_EXPIRY_MISSED"
    PROMO_EXPIRY_MISMATCH = "PROMO_EXPIRY_MISMATCH"
    SPURIOUS_REFUSAL = "SPURIOUS_REFUSAL"
    SPURIOUS_SUCCESS = "SPURIOUS_SUCCESS"
    WRONG_REFUSAL_CODE = "WRONG_REFUSAL_CODE"
    PROVIDER_OUTPUT_INVALID = "PROVIDER_OUTPUT_INVALID"
