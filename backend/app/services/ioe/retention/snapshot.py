"""The retention snapshot — what a user acknowledged, and nothing else.

WHY IT IS LOSSY ON PURPOSE. Noise suppression is structural here, not a filter
applied later. A field that never enters the snapshot can never produce a
change, so the single most important design decision in this module is what it
REFUSES to record:

    source_id            the optimization candidate's row id — NEW ON EVERY RUN.
                         Recording it would make every re-run look like the
                         entire portfolio was replaced.
    days_remaining       the daily countdown. 38 -> 37 is not news; the BAND
                         transition is. This is the §9/§47 acceptance.
    candidate_rank       the optimizer's presentation order. A rank shift means
                         some other opportunity moved, not that this one changed.
    actionability        derived from the axes below, so it moves exactly when
                         one of them does. Including it would report one
                         semantic event twice.
    attention flags      likewise derived.
    support / potential  governed figures, but scores that drift with inputs the
                         customer did not act on; retention is about state.
    reason_codes         derived from the axes.
    journal thread ids   the Journal is the history authority; the retention
                         record needs the DECISION, not which thread carried it.
    ordering             every collection here is sorted by semantic identity.

WHAT IDENTIFIES AN OPPORTUNITY. `opportunity_code`, and it is safe: rule codes
are globally unique (`tax_rule_code_key`) and at most one version of a rule is
published per tax year (`uq_rule_version_published`), so the code is unique
within a snapshot and stable across observations. A rule republished under a
new version keeps its code, which is what makes that read as a CHANGE to an
existing opportunity rather than a removal plus an addition.

NO CLOCK. `evaluated_as_of` is supplied by the caller and recorded, because the
timing band is only meaningful against the date that produced it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.services.ioe.domain.canonical import (
    DOMAIN_RETENTION_SNAPSHOT,
    domain_hash,
)
from app.services.ioe.lifecycle.domain import OpportunityLifecycleMap
from app.services.state_graph.assurance import TaxAssuranceMap

#: The PERSISTED snapshot's own schema version. Distinct from the read
#: contract's version (`app/schemas/retention.py`) and from every upstream
#: product version: this is the shape of bytes that outlive the code that wrote
#: them, and it changes only when those bytes change.
RETENTION_SNAPSHOT_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class OpportunitySnapshot:
    """One opportunity's retention-relevant state.

    Every field here is a governed value carried verbatim from the Opportunity
    Lifecycle contract. Nothing is computed, re-derived or rounded.
    """

    opportunity_code: str
    availability: str
    decision: str
    execution: str
    evidence: str
    timing: str
    freshness: str
    integrity: str
    integrity_reason_code: str
    #: The governed deadline's identity and date. Present together or not at
    #: all. §8 requires detecting a deadline that MOVED, which the timing band
    #: alone cannot express — a deadline can shift months and stay NORMAL.
    deadline_code: str | None
    deadline_date: str | None


@dataclass(frozen=True)
class FamilySnapshot:
    """One Assurance family's standing, with its reason.

    The reason travels because §18's rule applies to families too: an
    UNAVAILABLE family is not an empty one, and dropping the reason would let
    "no governing run exists" read as "nothing to report".
    """

    family: str
    status: str
    reason_code: str


@dataclass(frozen=True)
class RetentionSnapshot:
    schema_version: str
    tax_year: int
    evaluated_as_of: str
    opportunity_authority: str
    opportunity_authority_reason: str
    #: Sorted by opportunity_code. Sorted at construction, not at serialization,
    #: so the hash cannot depend on the order the source happened to yield.
    opportunities: tuple[OpportunitySnapshot, ...]
    #: Sorted by family.
    families: tuple[FamilySnapshot, ...]


def snapshot_payload(snapshot: RetentionSnapshot) -> dict[str, Any]:
    """The canonical wire/storage form. One writer, used for both the stored
    JSON and the hash, so stored bytes and hashed bytes cannot diverge."""
    return {
        "schema_version": snapshot.schema_version,
        "tax_year": snapshot.tax_year,
        "evaluated_as_of": snapshot.evaluated_as_of,
        "opportunity_authority": snapshot.opportunity_authority,
        "opportunity_authority_reason": snapshot.opportunity_authority_reason,
        "opportunities": [
            {
                "opportunity_code": o.opportunity_code,
                "availability": o.availability,
                "decision": o.decision,
                "execution": o.execution,
                "evidence": o.evidence,
                "timing": o.timing,
                "freshness": o.freshness,
                "integrity": o.integrity,
                "integrity_reason_code": o.integrity_reason_code,
                "deadline_code": o.deadline_code,
                "deadline_date": o.deadline_date,
            }
            for o in snapshot.opportunities
        ],
        "families": [
            {"family": f.family, "status": f.status, "reason_code": f.reason_code}
            for f in snapshot.families
        ],
    }


def snapshot_hash(snapshot: RetentionSnapshot) -> str:
    """Domain-separated hash over the canonical payload.

    Binds the schema version, the tax year and `evaluated_as_of` along with the
    state, because a snapshot means "this state, on this date, under this
    shape". It binds NO acknowledgement timestamp: the hash identifies the
    state a user reviewed, and two users reviewing identical state a week apart
    must produce the same token for the concurrency check to mean anything.
    """
    return domain_hash(DOMAIN_RETENTION_SNAPSHOT, snapshot_payload(snapshot))


def snapshot_from_payload(payload: dict[str, Any]) -> RetentionSnapshot:
    """Rebuild a snapshot from stored bytes.

    Reads what is there. It never consults current product state to fill a gap —
    reconstructing an old baseline from today's world would silently erase the
    very changes this engine exists to report.
    """
    return RetentionSnapshot(
        schema_version=payload["schema_version"],
        tax_year=payload["tax_year"],
        evaluated_as_of=payload["evaluated_as_of"],
        opportunity_authority=payload["opportunity_authority"],
        opportunity_authority_reason=payload["opportunity_authority_reason"],
        opportunities=tuple(
            OpportunitySnapshot(
                opportunity_code=o["opportunity_code"],
                availability=o["availability"],
                decision=o["decision"],
                execution=o["execution"],
                evidence=o["evidence"],
                timing=o["timing"],
                freshness=o["freshness"],
                integrity=o["integrity"],
                integrity_reason_code=o["integrity_reason_code"],
                deadline_code=o["deadline_code"],
                deadline_date=o["deadline_date"],
            )
            for o in payload["opportunities"]
        ),
        families=tuple(
            FamilySnapshot(
                family=f["family"], status=f["status"], reason_code=f["reason_code"]
            )
            for f in payload["families"]
        ),
    )


def snapshot_from_product(
    lifecycle: OpportunityLifecycleMap,
    assurance: TaxAssuranceMap,
    *,
    as_of: date,
) -> RetentionSnapshot:
    """Project the two certified current-state maps into a retention snapshot.
    Pure — no I/O, no clock, no business derivation.

    BOTH sources, each for what it owns (§5): the Opportunity Lifecycle is the
    per-opportunity authority, and Tax Assurance is the authority for family
    standing, which the lifecycle map does not carry. Neither is re-derived
    here.

    This is the ONLY place current product state becomes a snapshot, so it is
    the only place the exclusion list above has to be enforced.
    """
    return RetentionSnapshot(
        schema_version=RETENTION_SNAPSHOT_SCHEMA_VERSION,
        tax_year=lifecycle.tax_year,
        evaluated_as_of=as_of.isoformat(),
        opportunity_authority=lifecycle.opportunity_authority,
        opportunity_authority_reason=lifecycle.opportunity_authority_reason,
        opportunities=tuple(sorted(
            (
                OpportunitySnapshot(
                    opportunity_code=entry.opportunity_code,
                    availability=entry.availability.value,
                    decision=entry.decision.value,
                    execution=entry.execution.value,
                    evidence=entry.evidence,
                    timing=entry.timing.value,
                    freshness=entry.freshness,
                    integrity=entry.integrity,
                    integrity_reason_code=entry.integrity_reason_code,
                    deadline_code=(
                        entry.deadline.deadline_code
                        if entry.deadline is not None else None
                    ),
                    deadline_date=(
                        entry.deadline.deadline_date
                        if entry.deadline is not None else None
                    ),
                )
                for entry in lifecycle.opportunities
            ),
            key=lambda o: o.opportunity_code,
        )),
        families=tuple(sorted(
            (
                FamilySnapshot(
                    family=family.family,
                    status=family.status.value,
                    reason_code=family.reason_code,
                )
                for family in assurance.families
            ),
            key=lambda f: f.family,
        )),
    )
