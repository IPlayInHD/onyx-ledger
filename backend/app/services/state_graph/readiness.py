"""Evidence readiness — the second axis. Pure: no I/O.

THE JOIN IS EXACT, NOT APPROXIMATE
----------------------------------
`rules.rule_required_document.document_type_code` references
`ref.document_type(code)` and `docs.document.document_type_id` references
`ref.document_type(id)`. Same table, so required-versus-held is a set difference
over one shared vocabulary with no mapping layer to get wrong. That is the only
reason this is allowed to be `DERIVED_DETERMINISTIC` rather than inference.

WHAT "HELD" MEANS
-----------------
A `docs.document` of the required type with `deleted_at IS NULL` and
`status = 'processed'` — the only value in the CHECK vocabulary meaning
successful ingestion. `quarantined` and `failed` are not evidence at all;
`uploaded` and `processing` are not evidence yet. Counting them would make
readiness a report on upload activity rather than on evidence.

WHY `conditional` IS `UNKNOWN`
------------------------------
`necessity` is CHECK-constrained to `required / recommended / conditional`, and
whether a conditional requirement applies is carried only in a free-text `note`.
Resolving it would mean reading prose and deciding. `UNKNOWN` is the truthful
state, not a hedge — and it is strictly better than guessing `MISSING`, which
would tell a user to go and find a document they may not need.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .contracts import EvidenceReadiness

#: The `docs.document.status` value that means the document was successfully
#: ingested. Named rather than inlined, because the argument for it living here
#: is in this module's docstring.
INGESTED_DOCUMENT_STATUS = "processed"

NECESSITY_REQUIRED = "required"
NECESSITY_RECOMMENDED = "recommended"
NECESSITY_CONDITIONAL = "conditional"


@dataclass(frozen=True)
class DocumentRequirement:
    """One governed row of `rules.rule_required_document`."""

    rule_version_id: str
    document_type_code: str
    necessity: str


@dataclass(frozen=True)
class RequirementCoverage:
    """A requirement resolved against what the user actually holds."""

    requirement: DocumentRequirement
    readiness: EvidenceReadiness
    #: Ids of the ingested documents that satisfy it. Sorted, so the value is
    #: stable across assemblies and safe to hash.
    satisfying_document_ids: tuple[str, ...]


def resolve_requirement(
    requirement: DocumentRequirement,
    held_document_ids_by_type: Mapping[str, Sequence[str]],
) -> RequirementCoverage:
    """Resolve ONE requirement. `held_document_ids_by_type` must already be
    filtered to ingested, undeleted documents — the filter is the caller's, so
    that this function stays a pure statement about set membership."""
    held = tuple(sorted(held_document_ids_by_type.get(requirement.document_type_code, ())))

    if requirement.necessity == NECESSITY_CONDITIONAL:
        readiness = EvidenceReadiness.UNKNOWN
    elif requirement.necessity == NECESSITY_RECOMMENDED:
        # Recommended is not a readiness blocker: its absence never makes a
        # rule's evidence incomplete, so reporting MISSING would overstate it.
        readiness = (
            EvidenceReadiness.READY if held else EvidenceReadiness.NOT_REQUIRED
        )
    elif held:
        readiness = EvidenceReadiness.READY
    else:
        readiness = EvidenceReadiness.MISSING

    return RequirementCoverage(
        requirement=requirement,
        readiness=readiness,
        satisfying_document_ids=held,
    )


def rollup_readiness(coverages: Sequence[RequirementCoverage]) -> EvidenceReadiness:
    """The readiness of a rule version as a whole, from its requirements.

    Ordered by how much it should worry a reader: an UNKNOWN anywhere means the
    answer is not knowable, which outranks a confident PARTIAL. `PARTIAL` is
    produced HERE rather than per requirement, because a single requirement is
    binary — it is the set of them that can be half satisfied.
    """
    required = [
        c for c in coverages
        if c.requirement.necessity in (NECESSITY_REQUIRED, NECESSITY_CONDITIONAL)
    ]
    if not required:
        return EvidenceReadiness.NOT_REQUIRED
    if any(c.readiness is EvidenceReadiness.UNKNOWN for c in required):
        return EvidenceReadiness.UNKNOWN

    ready = sum(1 for c in required if c.readiness is EvidenceReadiness.READY)
    if ready == len(required):
        return EvidenceReadiness.READY
    if ready == 0:
        return EvidenceReadiness.MISSING
    return EvidenceReadiness.PARTIAL
