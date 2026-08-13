"""The historical held-evidence snapshot (Entry 12B1).

WHAT THIS ANSWERS. "For scenario S sealed at T1, which governed evidence TYPES
did the user hold at T1?" — without reading `docs.document` ever again.

WHY IT HAS TO EXIST. `GraphLoader._held_documents` reads the document library
LIVE. That is right for the CURRENT graph and wrong for a sealed scenario: a
comparison sealed in March would silently change its answer in June because a
slip was uploaded, and the user would see a historical artifact rewrite itself.

WHY IT IS ONLY TYPE CODES
-------------------------
Traced, not assumed. `state_graph.readiness.readiness_for` decides on ONE thing:
whether a document of the required type is present. `resolve_requirement` also
returns `satisfying_document_ids`, but those ids are consumed by exactly one
thing — `SUPPORTED_BY` edge construction in the assembler — and never by a
readiness verdict.

For a single-scenario baseline-versus-counterfactual comparison that makes
document identity irrelevant. Held evidence is the SAME T1 state on both sides:
a scenario changes what evidence is REQUIRED, it never uploads or deletes a
document. So identity-level edges would be byte-identical on both sides of the
diff and could not produce a single comparison-relevant difference. Persisting
document ids would retain a pseudonymous identifier per document, permanently,
inside sealed evidence, in order to make two structures that are already equal
look equal — which is a privacy cost paid for nothing.

Decision recorded: READINESS_SEMANTICS_ONLY.

WHAT IS DELIBERATELY ABSENT
---------------------------
No document ids, filenames, object keys, buckets, content hashes, extracted
text, upload or processing timestamps, and no free text. A document type code is
governed reference data (`ref.document_type.code`) — the same vocabulary
`rules.rule_required_document` is written against — not user content.

RETENTION. `docs.document` already TOMBSTONES on delete: `deleted_at` is set
while `document_type_id`, `content_hash` and `object_key` are deliberately
retained to prove which document was removed. So "this user held a document of
this type for this year" already survives document deletion in the documents
table itself; sealing the type code here retains strictly less than that, and
the row it is sealed into is removed on account deletion by the
HISTORICAL_DETAIL_CLEANUP phase.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Document, DocumentType
from app.services.state_graph.readiness import (
    INGESTED_DOCUMENT_STATUS,
    DocumentRequirement,
    EvidenceReadiness,
    readiness_for,
)

#: Bumped when the SEALED SHAPE changes in a way that could alter a hash for
#: unchanged held evidence.
HELD_EVIDENCE_SNAPSHOT_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class HistoricalHeldEvidenceSnapshot:
    """What qualifying evidence TYPES existed at scenario-seal time.

    A set, not a list of documents. Two T4 slips and one T4 slip are the same
    fact to every requirement in the system, because readiness asks whether the
    type is present and never how many. Canonicalizing to a sorted unique tuple
    is therefore not lossy compression — it is the semantic content.
    """

    document_type_codes: tuple[str, ...]
    schema_version: str = HELD_EVIDENCE_SNAPSHOT_SCHEMA_VERSION

    def holds(self, document_type_code: str) -> bool:
        return document_type_code in self.document_type_codes


def build_snapshot(codes: Iterable[str]) -> HistoricalHeldEvidenceSnapshot:
    """Canonicalize to sorted-unique. Row order, insertion order and duplicate
    count are properties of a query, not facts about the user's evidence."""
    return HistoricalHeldEvidenceSnapshot(
        document_type_codes=tuple(sorted({str(code) for code in codes})))


async def capture_held_evidence(
    session: AsyncSession, user_id: uuid.UUID, tax_year: int
) -> HistoricalHeldEvidenceSnapshot:
    """Read the qualifying set ONCE, at creation, from the live library.

    This is the only function in the system permitted to read `docs.document`
    for a sealed scenario, and it runs only while that scenario is being
    created. Replay and historical read consume what this sealed; if either
    called this instead, T1 would be re-answered with today's library and the
    whole snapshot would be pointless.

    QUALIFICATION MATCHES THE CURRENT GRAPH EXACTLY — same owner, same tax year,
    `deleted_at IS NULL`, and `status = INGESTED_DOCUMENT_STATUS`, the one status
    meaning successful ingestion. The status constant is imported from
    `state_graph.readiness` rather than restated, so the two cannot drift into
    disagreeing about what counts as evidence.
    """
    rows = await session.scalars(
        select(DocumentType.code)
        .join(Document, Document.document_type_id == DocumentType.id)
        .where(
            Document.user_id == user_id,
            Document.deleted_at.is_(None),
            Document.status == INGESTED_DOCUMENT_STATUS,
            Document.tax_year == tax_year,
        )
    )
    return build_snapshot(rows)


def canonical_payload(snapshot: HistoricalHeldEvidenceSnapshot) -> dict[str, Any]:
    """The exact shape that is hashed, exposed so a test can assert what enters
    the hash rather than infer it from a digest.

    No capture timestamp: two scenarios sealed a minute apart over an unchanged
    library describe the same evidence state, and a clock in here would make
    their hashes differ for a reason the user did not cause.
    """
    return {
        "schema_version": snapshot.schema_version,
        "document_type_codes": list(snapshot.document_type_codes),
    }


def from_payload(payload: dict[str, Any]) -> HistoricalHeldEvidenceSnapshot:
    """Rebuild a snapshot READ BACK from a sealed artifact."""
    return HistoricalHeldEvidenceSnapshot(
        document_type_codes=tuple(payload.get("document_type_codes") or ()),
        schema_version=str(
            payload.get("schema_version") or HELD_EVIDENCE_SNAPSHOT_SCHEMA_VERSION),
    )


def historical_readiness(
    snapshot: HistoricalHeldEvidenceSnapshot,
    requirements: Sequence[DocumentRequirement],
) -> tuple[tuple[DocumentRequirement, EvidenceReadiness], ...]:
    """THE DETERMINISTIC READINESS PRIMITIVE for a sealed scenario.

    Pure. It executes no query, no tax engine and no rules evaluation — the two
    inputs are both already sealed: held evidence from this snapshot, required
    evidence from the sealed candidate semantics.

    Readiness is DERIVED here rather than sealed alongside the snapshot. Storing
    a precomputed verdict would duplicate a value that these two inputs already
    determine, and a duplicate is something that can disagree.

    `readiness_for` is the same function the current graph uses, so a historical
    verdict and a current verdict cannot differ by implementation — only by the
    held state they were computed from, which is the entire point.
    """
    return tuple(
        (requirement,
         readiness_for(requirement.necessity,
                       present=snapshot.holds(requirement.document_type_code)))
        for requirement in requirements
    )
