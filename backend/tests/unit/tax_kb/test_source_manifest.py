"""Tax source manifest and locator — the pure domain.

What is at stake: that a manifest the registry cannot vouch for is refused
rather than guessed at, and that a citation's identity is the LOCATION rather
than the order somebody typed its fields.
"""
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.services.tax_kb.sources.domain import (
    PRIMARY_AUTHORITY_TYPES,
    SOURCE_MANIFEST_SCHEMA_VERSION,
    FingerprintMethod,
    IssuerCode,
    ManifestError,
    SourceLocator,
    SourceStatus,
    SourceType,
    fingerprint_bytes,
    manifest_from_payload,
)

VALID = {
    "source_type": "CRA_GUIDE",
    "issuer_code": "CANADA_REVENUE_AGENCY",
    "jurisdiction_code": "FED",
    "official_identifier": "T4044",
    "title": "Employment Expenses",
    "edition": "2025 edition",
    "official_locator": "https://example.invalid/t4044",
    "content_fingerprint": "a" * 64,
    "fingerprint_method": "RAW_BYTES_SHA256",
    "retrieved_at": "2026-03-01T00:00:00+00:00",
}


def _payload(**over):
    return {**VALID, **over}


# ===========================================================================
# Manifest validation — fail closed
# ===========================================================================
def test_a_valid_manifest_parses_into_governed_values():
    manifest = manifest_from_payload(_payload(
        tax_year=2025, publication_date="2026-01-05",
        effective_from="2025-01-01", effective_to="2025-12-31"))

    assert manifest.source_type is SourceType.CRA_GUIDE
    assert manifest.issuer_code is IssuerCode.CANADA_REVENUE_AGENCY
    assert manifest.fingerprint_method is FingerprintMethod.RAW_BYTES_SHA256
    assert manifest.status is SourceStatus.ACTIVE
    assert manifest.tax_year == 2025
    assert manifest.publication_date == date(2026, 1, 5)
    assert manifest.manifest_schema_version == SOURCE_MANIFEST_SCHEMA_VERSION


@pytest.mark.parametrize("field", [
    "source_type", "issuer_code", "fingerprint_method", "status"])
def test_an_ungoverned_vocabulary_value_is_refused_with_the_allowed_set(field):
    with pytest.raises(ManifestError) as caught:
        manifest_from_payload(_payload(**{field: "SOMETHING_PLAUSIBLE"}))
    assert field in str(caught.value)
    assert "allowed" in str(caught.value), (
        "a refusal that does not say what is allowed cannot be acted on")


@pytest.mark.parametrize("field", [
    "official_identifier", "title", "edition", "official_locator",
    "content_fingerprint", "jurisdiction_code"])
def test_a_blank_required_field_is_refused(field):
    with pytest.raises(ManifestError, match=field):
        manifest_from_payload(_payload(**{field: "   "}))


def test_an_unknown_manifest_field_is_refused_rather_than_ignored():
    """Silently dropping a field somebody wrote is how a manifest comes to mean
    something other than what its author intended."""
    with pytest.raises(ManifestError, match="unknown manifest field"):
        manifest_from_payload(_payload(legal_authority_rank="1"))


def test_a_manifest_from_another_contract_version_is_refused():
    with pytest.raises(ManifestError, match="manifest schema"):
        manifest_from_payload(_payload(manifest_schema_version="2.0.0"))


def test_an_inverted_effective_period_is_refused():
    with pytest.raises(ManifestError, match="precedes effective_from"):
        manifest_from_payload(_payload(
            effective_from="2025-12-31", effective_to="2025-01-01"))


def test_a_malformed_date_is_refused_rather_than_coerced():
    with pytest.raises(ManifestError, match="ISO date"):
        manifest_from_payload(_payload(publication_date="Jan 5 2026"))


def test_retrieved_at_is_required():
    payload = _payload()
    del payload["retrieved_at"]
    with pytest.raises(ManifestError, match="retrieved_at"):
        manifest_from_payload(payload)


def test_tax_year_is_optional_because_statutes_are_date_based():
    """§7: forcing a tax year onto a statute would be an invented fact."""
    manifest = manifest_from_payload(_payload(
        source_type="STATUTE", issuer_code="PARLIAMENT_OF_CANADA",
        official_identifier="R.S.C. 1985, c. 1 (5th Supp.)",
        effective_from="1985-09-01"))
    assert manifest.tax_year is None
    assert manifest.effective_from == date(1985, 9, 1)


# ===========================================================================
# Source authority vocabulary
# ===========================================================================
def test_secondary_commentary_is_registrable_but_not_primary_authority():
    """§25: a blog may be recorded for research and must never be mistakable
    for a statute."""
    assert SourceType.SECONDARY_COMMENTARY in SourceType
    assert SourceType.SECONDARY_COMMENTARY not in PRIMARY_AUTHORITY_TYPES
    assert SourceType.STATUTE in PRIMARY_AUTHORITY_TYPES
    manifest = manifest_from_payload(_payload(
        source_type="SECONDARY_COMMENTARY", issuer_code="OTHER"))
    assert manifest.source_type is SourceType.SECONDARY_COMMENTARY


def test_the_vocabulary_defines_no_legal_ranking():
    """Explicit about WHAT KIND of authority, silent about which outranks which
    — nothing in this repository governs legal precedence, and an enum that
    implied one would be a tax opinion in disguise."""
    from app.services.tax_kb.sources import domain

    text = Path(domain.__file__).read_text()
    for ranking in ("rank", "precedence", "outranks", "authority_level",
                    "weight"):
        assert f"def {ranking}" not in text
    assert not any(name.endswith("_rank") for name in dir(domain))


# ===========================================================================
# Fingerprint
# ===========================================================================
def test_a_raw_fingerprint_is_sha256_over_the_exact_bytes():
    import hashlib

    content = b"%PDF-1.7 governed source bytes"
    assert fingerprint_bytes(content) == hashlib.sha256(content).hexdigest()


def test_identical_bytes_fingerprint_identically_and_one_byte_changes_it():
    assert fingerprint_bytes(b"abc") == fingerprint_bytes(b"abc")
    assert fingerprint_bytes(b"abc") != fingerprint_bytes(b"abd")


def test_the_fingerprint_method_distinguishes_a_digest_from_a_claim():
    """A digest of raw bytes and a digest somebody declared are different
    claims; storing only the hex string would let the weaker read as the
    stronger."""
    assert {m.value for m in FingerprintMethod} == {
        "RAW_BYTES_SHA256", "DECLARED_BY_OPERATOR"}


def test_bytes_are_hashed_not_parsed():
    """Source files are untrusted input. Malformed content must fingerprint
    without being interpreted."""
    for hostile in (b"", b"\x00\xff\xfe", b"<script>alert(1)</script>",
                    b"%PDF-1.7\n/JS (app.alert\\(1\\))"):
        assert len(fingerprint_bytes(hostile)) == 64


# ===========================================================================
# Locator — structured, canonical, reusable
# ===========================================================================
def test_a_locator_digest_ignores_field_order():
    """THE reason a citation is reusable: the same location digests identically
    however the manifest ordered its keys."""
    a = SourceLocator({"section": "118.2", "subsection": "2", "paragraph": "a"})
    b = SourceLocator({"paragraph": "a", "section": "118.2", "subsection": "2"})
    assert a.digest() == b.digest()
    assert a.canonical() == b.canonical()


def test_different_locations_digest_differently():
    assert SourceLocator({"section": "118.2"}).digest() != \
        SourceLocator({"section": "118.3"}).digest()
    assert SourceLocator({"section": "118"}).digest() != \
        SourceLocator({"page": "118"}).digest(), (
        "a section and a page number are different locations")


def test_an_empty_locator_is_refused():
    with pytest.raises(ManifestError, match="at least one location"):
        SourceLocator({})


def test_an_unknown_locator_field_is_refused():
    with pytest.raises(ManifestError, match="unknown locator field"):
        SourceLocator({"paragraph_ish": "a"})


@pytest.mark.parametrize("bad", [{"section": ""}, {"section": "   "},
                                 {"section": None}, {"section": 118}])
def test_a_malformed_locator_value_is_refused(bad):
    with pytest.raises(ManifestError):
        SourceLocator(bad)


def test_the_label_is_convenience_and_never_identity():
    locator = SourceLocator({"section": "118.2", "subsection": "2"})
    assert "118.2" in locator.label()
    # Two locators with the same digest must agree; the label is derived from
    # the same canonical form rather than being an independent free-form field.
    same = SourceLocator({"subsection": "2", "section": "118.2"})
    assert locator.label() == same.label()
    assert locator.digest() == same.digest()


def test_the_locator_digest_is_invariant_under_pythonhashseed(tmp_path: Path):
    script = tmp_path / "run.py"
    script.write_text(
        "from app.services.tax_kb.sources.domain import SourceLocator\n"
        "keys = ['section','subsection','paragraph','clause','page','table',\n"
        "        'schedule','form_line','heading','anchor']\n"
        "loc = SourceLocator({k: f'v{i}' for i, k in enumerate(keys)})\n"
        "print(loc.digest())\n")
    digests = set()
    for seed in ("0", "1", "42"):
        result = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True,
            check=True, cwd=str(Path(__file__).parents[3]),
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin",
                 "PYTHONPATH": str(Path(__file__).parents[3])})
        digests.add(result.stdout.strip())
    assert len(digests) == 1, f"seed variation moved the digest: {digests}"


def test_the_domain_reads_no_clock_and_no_network():
    from app.services.tax_kb.sources import domain

    text = Path(domain.__file__).read_text()
    for forbidden in ("requests", "httpx", "urlopen", "datetime.now(",
                      "utcnow", "openai", "anthropic"):
        assert forbidden not in text, f"the source domain references {forbidden}"


def test_a_manifest_carries_its_retrieval_time_rather_than_reading_one():
    """as_of style discipline: the caller supplies when the document was
    fetched; the domain never asks the clock."""
    manifest = manifest_from_payload(_payload(
        retrieved_at="2026-03-01T12:30:00+00:00"))
    assert manifest.retrieved_at == datetime(2026, 3, 1, 12, 30, tzinfo=UTC)
