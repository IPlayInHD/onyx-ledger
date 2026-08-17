"""Tax source registry — the pure domain.

The manifest vocabulary, the locator model, and the two identity rules the
registry turns on. No SQL, no clock, no network, no AI: every value arrives
explicitly and every digest is computed from it.

WHAT THIS MODULE REFUSES TO DO. It does not interpret a source, rank legal
authority, decide what a section means, or fetch anything. A source document is
evidence for an interpretation; the structured rule, formula and reference-data
model remains the runtime authority.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from app.services.ioe.domain.canonical import (
    DOMAIN_SOURCE_LOCATOR,
    CanonicalizationError,
    domain_hash,
)

#: The manifest/import contract's own version. Stored on every source version so
#: a future manifest shape cannot silently reinterpret rows written under this
#: one. Independent of the rule, scenario, assurance and retention versions.
SOURCE_MANIFEST_SCHEMA_VERSION = "1.0.0"


class SourceType(StrEnum):
    """What KIND of authority a source is.

    Explicit so a later review can tell a statute from a blog post. Deliberately
    NOT ordered: nothing in this repository governs legal precedence, and an
    enum that implied one would be a tax opinion in disguise.
    """

    STATUTE = "STATUTE"
    REGULATION = "REGULATION"
    GOVERNMENT_GUIDANCE = "GOVERNMENT_GUIDANCE"
    CRA_FOLIO = "CRA_FOLIO"
    CRA_GUIDE = "CRA_GUIDE"
    FORM = "FORM"
    FORM_INSTRUCTIONS = "FORM_INSTRUCTIONS"
    SCHEDULE = "SCHEDULE"
    RATE_TABLE = "RATE_TABLE"
    INDEXED_PARAMETER_PUBLICATION = "INDEXED_PARAMETER_PUBLICATION"
    #: Registrable for research and clearly labelled. The registry's job is to
    #: make it impossible for this to be mistaken for a statute — not to pretend
    #: the business will never look at one.
    SECONDARY_COMMENTARY = "SECONDARY_COMMENTARY"


#: Source types a production tax rule may eventually be published against. NOT
#: enforced here — publication policy belongs to the next entry — but named now
#: so that policy has something governed to consult instead of a fresh opinion.
PRIMARY_AUTHORITY_TYPES = frozenset(SourceType) - {SourceType.SECONDARY_COMMENTARY}


class IssuerCode(StrEnum):
    """The issuing authority, as a controlled code.

    A code rather than free text so "CRA" and "Canada Revenue Agency" cannot
    become two publishers of the same guide.
    """

    PARLIAMENT_OF_CANADA = "PARLIAMENT_OF_CANADA"
    GOVERNMENT_OF_CANADA = "GOVERNMENT_OF_CANADA"
    DEPARTMENT_OF_FINANCE_CANADA = "DEPARTMENT_OF_FINANCE_CANADA"
    CANADA_REVENUE_AGENCY = "CANADA_REVENUE_AGENCY"
    PROVINCIAL_LEGISLATURE = "PROVINCIAL_LEGISLATURE"
    PROVINCIAL_TAX_AUTHORITY = "PROVINCIAL_TAX_AUTHORITY"
    OTHER = "OTHER"


class FingerprintMethod(StrEnum):
    """WHICH rule produced a content fingerprint.

    Recorded because a digest of raw bytes and a digest of text somebody
    extracted from a PDF are different claims, and a registry that stored only
    the hex string would let the weaker one be read as the stronger.
    """

    #: SHA-256 over the exact bytes supplied. The only method that identifies a
    #: document rather than somebody's reading of it.
    RAW_BYTES_SHA256 = "RAW_BYTES_SHA256"
    #: The operator supplied a digest without the bytes. Honest about being a
    #: claim: it pins what was declared, and cannot prove what was retrieved.
    DECLARED_BY_OPERATOR = "DECLARED_BY_OPERATOR"


class SourceStatus(StrEnum):
    """The publisher's classification of an edition AS RETRIEVED.

    Set once, never updated. Whether a NEWER edition exists is a different
    question answered by the supersession edge, so a withdrawn notice and a
    routine replacement never get confused for each other.
    """

    ACTIVE = "ACTIVE"
    WITHDRAWN = "WITHDRAWN"


#: The closed locator key set. A locator is structured so "s. 118.2(2)(a)" is
#: comparable rather than merely printable; keys outside this set are rejected
#: rather than stored, because an unvalidated locator is a free-form string with
#: extra punctuation.
LOCATOR_KEYS = (
    "section", "subsection", "paragraph", "clause",
    "page", "table", "schedule", "form_line", "heading", "anchor",
)


class ManifestError(ValueError):
    """A manifest that cannot be registered as written.

    Fails closed and says which field: a source registry that guesses at a
    malformed manifest is how an unreviewed document becomes tax law.
    """


@dataclass(frozen=True)
class SourceLocator:
    """A precise location inside one source edition."""

    values: dict[str, str]

    def __post_init__(self) -> None:
        if not self.values:
            raise ManifestError(
                "a citation locator must name at least one location; an empty "
                "locator cites the whole document, which is not provenance")
        unknown = sorted(set(self.values) - set(LOCATOR_KEYS))
        if unknown:
            raise ManifestError(
                f"unknown locator field(s) {unknown}; allowed: "
                f"{list(LOCATOR_KEYS)}")
        for key, value in self.values.items():
            if not isinstance(value, str) or not value.strip():
                raise ManifestError(f"locator field {key!r} must be a non-empty string")

    def canonical(self) -> dict[str, str]:
        """Sorted by key, so identity cannot depend on manifest field order."""
        return {key: self.values[key] for key in sorted(self.values)}

    def digest(self) -> str:
        """Domain-separated identity for this location.

        THE reason a citation is reusable: the same location in the same edition
        digests identically however the manifest happened to order its keys, so
        two rules citing one section share one row instead of duplicating the
        source metadata into each.
        """
        return domain_hash(DOMAIN_SOURCE_LOCATOR, {"locator": self.canonical()})

    def label(self) -> str:
        """A human-readable rendering. Convenience only — never identity."""
        return " ".join(
            f"{key}={self.values[key]}" for key in sorted(self.values))


def fingerprint_bytes(content: bytes) -> str:
    """SHA-256 over the exact bytes supplied.

    Uses the standard primitive directly rather than the canonicalizer, and that
    is deliberate: canonicalization exists to make STRUCTURED VALUES comparable,
    while a source document's identity is its literal bytes. Canonicalizing a
    PDF before hashing it would produce a digest of our reading of the document
    and then let it be read as a digest of the document.

    File content is UNTRUSTED INPUT. It is hashed and discarded — never parsed,
    never executed, never interpreted.
    """
    if not isinstance(content, bytes):
        raise ManifestError("a raw fingerprint requires bytes")
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class SourceManifest:
    """One registrable source edition, validated.

    Flat on purpose: the fields a human types when recording where a tax rule
    came from, with every controlled vocabulary checked before anything reaches
    the database.
    """

    # ---- the continuing publication ----
    source_type: SourceType
    issuer_code: IssuerCode
    jurisdiction_code: str
    official_identifier: str
    title: str
    # ---- this edition ----
    edition: str
    official_locator: str
    content_fingerprint: str
    fingerprint_method: FingerprintMethod
    retrieved_at: datetime
    tax_year: int | None = None
    publication_date: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    status: SourceStatus = SourceStatus.ACTIVE
    #: The edition this one replaces, named explicitly. Supersession is never
    #: inferred from dates or titles — a caller says so, or it does not happen.
    supersedes_fingerprint: str | None = None
    manifest_schema_version: str = SOURCE_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in ("official_identifier", "title", "edition",
                           "official_locator", "content_fingerprint",
                           "jurisdiction_code"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ManifestError(f"{field_name} is required")
        if (self.effective_from is not None and self.effective_to is not None
                and self.effective_to < self.effective_from):
            raise ManifestError(
                f"effective_to {self.effective_to} precedes effective_from "
                f"{self.effective_from}")
        if self.manifest_schema_version != SOURCE_MANIFEST_SCHEMA_VERSION:
            raise ManifestError(
                f"manifest schema {self.manifest_schema_version!r} is not "
                f"{SOURCE_MANIFEST_SCHEMA_VERSION!r}; a manifest written for a "
                "different contract is not silently reinterpreted")


def manifest_from_payload(payload: dict[str, Any]) -> SourceManifest:
    """Parse a registration payload, rejecting anything it cannot vouch for.

    Every controlled vocabulary is looked up rather than coerced, so a typo in
    `issuer_code` fails here instead of becoming a new publisher.
    """
    if not isinstance(payload, dict):
        raise ManifestError("a source manifest must be an object")

    def _enum(kind: type[StrEnum], key: str, default: Any = None) -> Any:
        raw = payload.get(key, default)
        if raw is None:
            raise ManifestError(f"{key} is required")
        try:
            return kind(raw)
        except ValueError as exc:
            allowed = sorted(member.value for member in kind)
            raise ManifestError(
                f"{key}={raw!r} is not a governed value; allowed: {allowed}"
            ) from exc

    def _date(key: str) -> date | None:
        raw = payload.get(key)
        if raw in (None, ""):
            return None
        try:
            return date.fromisoformat(raw) if isinstance(raw, str) else raw
        except ValueError as exc:
            raise ManifestError(f"{key}={raw!r} is not an ISO date") from exc

    retrieved = payload.get("retrieved_at")
    if retrieved in (None, ""):
        raise ManifestError("retrieved_at is required")
    if isinstance(retrieved, str):
        try:
            retrieved = datetime.fromisoformat(retrieved)
        except ValueError as exc:
            raise ManifestError(
                f"retrieved_at={retrieved!r} is not an ISO timestamp") from exc

    unknown = sorted(set(payload) - _MANIFEST_KEYS)
    if unknown:
        raise ManifestError(
            f"unknown manifest field(s) {unknown}; a field the registry does "
            "not understand is not silently ignored")

    try:
        return SourceManifest(
            source_type=_enum(SourceType, "source_type"),
            issuer_code=_enum(IssuerCode, "issuer_code"),
            jurisdiction_code=payload.get("jurisdiction_code", ""),
            official_identifier=payload.get("official_identifier", ""),
            title=payload.get("title", ""),
            edition=payload.get("edition", ""),
            official_locator=payload.get("official_locator", ""),
            content_fingerprint=payload.get("content_fingerprint", ""),
            fingerprint_method=_enum(
                FingerprintMethod, "fingerprint_method",
                FingerprintMethod.DECLARED_BY_OPERATOR.value),
            retrieved_at=retrieved,
            tax_year=payload.get("tax_year"),
            publication_date=_date("publication_date"),
            effective_from=_date("effective_from"),
            effective_to=_date("effective_to"),
            status=_enum(SourceStatus, "status", SourceStatus.ACTIVE.value),
            supersedes_fingerprint=payload.get("supersedes_fingerprint"),
            manifest_schema_version=payload.get(
                "manifest_schema_version", SOURCE_MANIFEST_SCHEMA_VERSION),
        )
    except CanonicalizationError as exc:  # pragma: no cover - defensive
        raise ManifestError(str(exc)) from exc


_MANIFEST_KEYS = {
    "source_type", "issuer_code", "jurisdiction_code", "official_identifier",
    "title", "edition", "official_locator", "content_fingerprint",
    "fingerprint_method", "retrieved_at", "tax_year", "publication_date",
    "effective_from", "effective_to", "status", "supersedes_fingerprint",
    "manifest_schema_version",
}
