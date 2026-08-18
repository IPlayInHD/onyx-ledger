"""The authoring manifest — a reviewable file that publishes reproducibly.

JSON, because the repository already speaks it: every canonical payload, every
sealed artifact and every stored spec is JSON, and adding a second document
language would mean a second parser to keep honest.

What a manifest may contain is deliberately narrow. It carries STRUCTURE —
conditions, outcomes, formulas, reference data — and it carries REFERENCES to
governed provenance. It never carries a copy of a source: no issuer, no title,
no URL, no quoted section text. The Source Registry is canonical, and a manifest
that restated a citation would be a second, quietly diverging record of what the
law says.

It also never carries executable logic. There is no expression field a runtime
would `eval`, no callable, no SQL and no Python. A manifest may hold prose for a
person to read; prose is never interpreted into semantics.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.tax_kb.authoring.codes import Family, ValidationCode
from app.services.tax_kb.authoring.report import Finding, error
from app.services.tax_kb.authoring.spec import (
    KNOWLEDGE_SPEC_SCHEMA_VERSION,
    ReferenceDataSpec,
    SpecError,
    TaxKnowledgeDraftSpec,
)

#: The manifest envelope's own version, distinct from the spec version it wraps.
MANIFEST_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class KnowledgePack:
    """One reviewable unit: reference data and rules that publish together."""

    name: str
    reference_data: tuple[ReferenceDataSpec, ...] = ()
    rules: tuple[TaxKnowledgeDraftSpec, ...] = ()
    manifest_schema_version: str = MANIFEST_SCHEMA_VERSION
    #: Operator prose. Read by people, never by the runtime — §44.
    notes: str | None = None

    @property
    def rule_codes(self) -> frozenset[str]:
        return frozenset(rule.rule_code for rule in self.rules)


def parse_manifest(payload: Any) -> KnowledgePack:
    """Read a manifest, refusing anything it does not understand.

    Members are read INDEPENDENTLY of each other, so a malformed rule is
    reported against that rule rather than aborting the pack — an operator
    running a dry run over a hundred rules needs every error at once, not the
    first one.
    """
    if not isinstance(payload, Mapping):
        raise SpecError(ValidationCode.MALFORMED_VALUE,
                        "a manifest must be an object")
    known = {"manifest_schema_version", "name", "notes", "reference_data", "rules"}
    unknown = sorted(set(payload) - known)
    if unknown:
        raise SpecError(
            ValidationCode.UNKNOWN_FIELD,
            f"manifest has unknown field(s) {unknown}; sorted(known) is "
            f"{sorted(known)}")

    declared = payload.get("manifest_schema_version", MANIFEST_SCHEMA_VERSION)
    if declared != MANIFEST_SCHEMA_VERSION:
        raise SpecError(
            ValidationCode.SPEC_SCHEMA_VERSION_UNSUPPORTED,
            f"manifest schema {declared!r} is not {MANIFEST_SCHEMA_VERSION!r}")

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SpecError(ValidationCode.MISSING_REQUIRED_FIELD,
                        "a manifest must be named so a review can refer to it")
    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise SpecError(ValidationCode.MALFORMED_VALUE, "notes must be text")

    for key in ("reference_data", "rules"):
        if key in payload and not isinstance(payload[key], list):
            raise SpecError(ValidationCode.MALFORMED_VALUE,
                            f"manifest.{key} must be a list")

    return KnowledgePack(
        name=name.strip(),
        notes=notes,
        reference_data=tuple(
            ReferenceDataSpec.read(item)
            for item in payload.get("reference_data", [])),
        rules=tuple(
            TaxKnowledgeDraftSpec.read(item) for item in payload.get("rules", [])),
    )


def load_manifest(path: str | Path) -> KnowledgePack:
    """Read a manifest from disk.

    The file is untrusted input: parsed as JSON and validated, never executed.
    Nothing about the filename becomes identity.
    """
    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecError(ValidationCode.MALFORMED_VALUE,
                        f"manifest is not valid JSON: {exc}") from exc
    return parse_manifest(payload)


def parse_members(payload: Any) -> tuple[KnowledgePack, list[SpecError]]:
    """Parse a manifest, collecting per-member failures instead of raising.

    The envelope must still be well formed — there is no partial reading of a
    document whose shape is unknown — but a bad rule yields an error naming that
    rule while every other member is still read and reported on.
    """
    if not isinstance(payload, Mapping):
        raise SpecError(ValidationCode.MALFORMED_VALUE,
                        "a manifest must be an object")
    shell = {k: v for k, v in payload.items() if k not in ("rules", "reference_data")}
    pack = parse_manifest({**shell, "rules": [], "reference_data": []})

    errors: list[SpecError] = []
    rules: list[TaxKnowledgeDraftSpec] = []
    reference_data: list[ReferenceDataSpec] = []
    for i, item in enumerate(payload.get("reference_data") or []):
        try:
            reference_data.append(ReferenceDataSpec.read(item))
        except SpecError as exc:
            errors.append(_located(exc, f"reference_data[{i}]"))
    for i, item in enumerate(payload.get("rules") or []):
        try:
            rules.append(TaxKnowledgeDraftSpec.read(item))
        except SpecError as exc:
            errors.append(_located(exc, f"rules[{i}]"))

    return (
        KnowledgePack(name=pack.name, notes=pack.notes,
                      reference_data=tuple(reference_data), rules=tuple(rules)),
        errors,
    )


def _located(exc: SpecError, position: str) -> SpecError:
    """Give a parse failure a position when the member had no readable code."""
    if exc.object_ref:
        return exc
    return SpecError(exc.code, exc.detail, family=exc.family, object_ref=position)


def as_finding(exc: SpecError) -> Finding:
    """Turn a parse refusal into a report finding.

    A manifest that cannot be read is not an exception an operator should have
    to interpret from a traceback; it is a validation result with a code, like
    every other refusal.
    """
    return error(exc.object_ref or "manifest",
                 exc.family or Family.STRUCTURE, exc.code, exc.detail)


__all__ = [
    "KNOWLEDGE_SPEC_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "KnowledgePack",
    "as_finding",
    "load_manifest",
    "parse_manifest",
    "parse_members",
]
