"""The extraction seam: what an adapter receives and what it must return.

The Protocol's identity fields are TRUSTED ADAPTER PROVENANCE — which of our
adapters ran, on which model, with which prompt. They are declared by adapter
code and bound into evaluation reports; the parser refuses them anywhere in
provider JSON, so adapter identity can never be confused with the bill issuer
extracted from a document.

`ValidatedBillArtifact` is the minimal stand-in for what the Slice 3 secure
file pipeline will produce (plan §9.5): identity is the byte digest, never a
filename. `content` is excluded from repr, equality, and hashing — the bytes
are somebody's bill, and a dataclass repr in a traceback is a log line.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.services.billshield.extraction.contract import (
    BillExtractionRefusal,
    BillExtractionV1,
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ArtifactFormat(StrEnum):
    """The §3.2 upload surface: PDF (native or scanned), JPEG, PNG."""

    PDF_NATIVE = "pdf_native"
    PDF_SCANNED = "pdf_scanned"
    IMAGE_PNG = "image_png"
    IMAGE_JPEG = "image_jpeg"


@dataclass(frozen=True)
class ValidatedBillArtifact:
    """One validated document, identified by its bytes.

    `compare=False` on `content` is deliberate twice over: equality and the
    dataclass hash are decided by the digest and metadata, so two artifacts
    with the same digest are the same artifact, and the raw bytes never feed
    a hash, an equality trace, or a repr.
    """

    artifact_sha256: str
    artifact_format: ArtifactFormat
    page_count: int
    content: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.artifact_sha256):
            raise ValueError("artifact_sha256 must be 64 lowercase hex characters")
        if self.page_count < 1:
            raise ValueError("page_count must be at least 1")


@runtime_checkable
class BillExtractionProvider(Protocol):
    """A BillShield-specific extraction adapter (plan §9.4).

    Never the tax explanation `LlmClient`, never the OCR regex service. The
    return type is the closed union: a validated success or a closed refusal —
    an adapter has no third channel, and no free-text one.
    """

    adapter_code: str
    model_version: str
    prompt_version: str | None

    async def extract(
        self, artifact: ValidatedBillArtifact
    ) -> BillExtractionV1 | BillExtractionRefusal: ...


__all__ = [
    "ArtifactFormat",
    "BillExtractionProvider",
    "ValidatedBillArtifact",
]
