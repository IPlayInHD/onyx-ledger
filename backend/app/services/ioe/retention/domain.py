"""Retention change detection — the pure comparator.

WHAT IT IS. One deterministic function from (acknowledged snapshot, current
snapshot) to the material changes between them. No SQL, no clock, no network,
no mutation; every input arrives explicitly.

DIRECTION IS FIXED (§13): baseline -> current. Absent then present is ADDED,
present then absent is REMOVED, same identity with a material difference is
CHANGED.

WHY REMOVED IS NOT EXPIRED. An opportunity that disappeared from the
authoritative current view and one whose governed deadline passed are different
facts with different causes. EXPIRED is a timing band the lifecycle authority
assigns and it is reported as a TIMING change on an opportunity that is still
present. A disappearance is reported as what it is: a removal, with no claim
about why.

WHY NOISE CANNOT REACH HERE. The snapshot already refuses to record
`days_remaining`, `candidate_rank`, `source_id`, orderings and every derived
field, so this module cannot invent a change out of them — it has never seen
them. That is deliberate: a suppression list applied at comparison time is a
filter someone eventually forgets to extend.

NO SCORE. Severity is a transparent lookup from the transition itself, not a
weighting. Nothing here ranks by money, and nothing re-orders what the
optimizer already ordered.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.services.ioe.domain.canonical import (
    DOMAIN_RETENTION_CHANGE,
    domain_hash,
)
from app.services.ioe.retention.snapshot import (
    FamilySnapshot,
    OpportunitySnapshot,
    RetentionSnapshot,
)

#: This read model's contract version — the shape of the CHANGE SET, which is
#: independent of the persisted snapshot's schema version. The two move for
#: different reasons: one when stored bytes change, one when the product
#: contract does.
RETENTION_CHANGES_CONTRACT_VERSION = "1.0.0"


class BaselineStatus(StrEnum):
    """Whether a comparison was possible at all.

    `NO_BASELINE` is the §4 case and it is emphatically not an empty baseline:
    nothing has been acknowledged, so nothing can be called new. Reporting ten
    current opportunities as ten additions would be describing the user's
    existing position as a set of events that never happened.
    """

    NO_BASELINE = "NO_BASELINE"
    ESTABLISHED = "ESTABLISHED"


class ChangeKind(StrEnum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    CHANGED = "CHANGED"


class ChangeCategory(StrEnum):
    """What KIND of fact moved. One category per governed axis, so a client can
    render, route or ignore each independently."""

    OPPORTUNITY = "OPPORTUNITY"
    DECISION = "DECISION"
    EXECUTION = "EXECUTION"
    EVIDENCE = "EVIDENCE"
    DEADLINE = "DEADLINE"
    TIMING = "TIMING"
    FRESHNESS = "FRESHNESS"
    INTEGRITY = "INTEGRITY"
    ASSURANCE = "ASSURANCE"


class ChangeSeverity(StrEnum):
    """How much attention a transition warrants.

    A transparent lookup, defined once in `_SEVERITY` below — never a computed
    number. §19 forbids a numeric retention score, and for a reason worth
    stating: a number invites sorting by it, sorting by it invites tuning it,
    and a tuned number is a recommendation engine wearing a different hat.
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass(frozen=True)
class FieldTransition:
    """One field's before and after. Structured values, never a text diff —
    a client must be able to branch on these, not parse them."""

    field: str
    before: str | None
    after: str | None


@dataclass(frozen=True)
class MaterialChange:
    """One semantic event about one subject.

    A change GROUPS the transitions that belong to it rather than emitting one
    record per differing field: a user who moves an opportunity from DEFER to
    PROCEED and reports acting on it in the same visit has had one conversation
    with the product, and two axes moved as part of it.
    """

    #: Deterministic identity — see `_change_hash`. Stable across processes and
    #: across repeated GETs of the same two snapshots.
    change_id: str
    kind: ChangeKind
    category: ChangeCategory
    severity: ChangeSeverity
    #: The governed semantic identity of what changed: an opportunity code, or
    #: an Assurance family name. Never a label, never a position.
    subject: str
    transitions: tuple[FieldTransition, ...]


@dataclass(frozen=True)
class ChangeSummary:
    """Transparent counts. No composite score, and no money at all — "you
    missed $X" is a calculation no authority here performed."""

    total: int
    by_kind: Mapping[str, int]
    by_category: Mapping[str, int]
    by_severity: Mapping[str, int]
    opportunities_added: int
    opportunities_removed: int
    newly_urgent: int
    newly_expired: int
    newly_blocked: int
    decision_changes: int
    execution_reports: int
    evidence_improvements: int
    evidence_regressions: int
    freshness_changes: int
    integrity_changes: int


@dataclass(frozen=True)
class RetentionChangeSet:
    contract_version: str
    tax_year: int
    as_of: str
    baseline_status: BaselineStatus
    #: The hash of the snapshot compared against, when there was one.
    baseline_snapshot_hash: str | None
    #: The hash of the state described right now. This is the token a client
    #: echoes back to acknowledge, and the reason a stale acknowledgement can
    #: be refused rather than silently accepted.
    current_snapshot_hash: str
    changes: tuple[MaterialChange, ...]
    summary: ChangeSummary


# ---------------------------------------------------------------------------
# Which fields are material, and under which category. THE contract of §8.
#
# A field absent from this table cannot produce a change even if the snapshot
# records it — which is how `integrity_reason_code` travels as context on an
# integrity change without being an independent event of its own.
# ---------------------------------------------------------------------------
_OPPORTUNITY_FIELDS: tuple[tuple[str, ChangeCategory], ...] = (
    ("availability", ChangeCategory.OPPORTUNITY),
    ("decision", ChangeCategory.DECISION),
    ("execution", ChangeCategory.EXECUTION),
    ("evidence", ChangeCategory.EVIDENCE),
    ("timing", ChangeCategory.TIMING),
    ("freshness", ChangeCategory.FRESHNESS),
    ("integrity", ChangeCategory.INTEGRITY),
    ("deadline_date", ChangeCategory.DEADLINE),
    ("deadline_code", ChangeCategory.DEADLINE),
)

#: Evidence readiness ordered from worst to best, for classifying a transition
#: as an improvement or a regression in the SUMMARY only. It is not a score and
#: nothing is ranked by it — NOT_REQUIRED sits outside the order entirely
#: because "nothing was demanded" is not a rung on a ladder.
_EVIDENCE_RANK = {"MISSING": 0, "UNKNOWN": 1, "PARTIAL": 2, "READY": 3}

#: The transitions that carry their own severity, keyed by (category, after).
#: Everything else falls back to `_DEFAULT_SEVERITY`. Written as data so the
#: mapping can be read, tested and argued with in one place.
_SEVERITY: Mapping[tuple[ChangeCategory, str], ChangeSeverity] = {
    (ChangeCategory.TIMING, "EXPIRED"): ChangeSeverity.CRITICAL,
    (ChangeCategory.TIMING, "URGENT"): ChangeSeverity.HIGH,
    (ChangeCategory.TIMING, "APPROACHING"): ChangeSeverity.MEDIUM,
    (ChangeCategory.OPPORTUNITY, "BLOCKED"): ChangeSeverity.HIGH,
}

_DEFAULT_SEVERITY: Mapping[ChangeCategory, ChangeSeverity] = {
    ChangeCategory.OPPORTUNITY: ChangeSeverity.MEDIUM,
    ChangeCategory.DECISION: ChangeSeverity.LOW,
    ChangeCategory.EXECUTION: ChangeSeverity.LOW,
    ChangeCategory.EVIDENCE: ChangeSeverity.MEDIUM,
    ChangeCategory.DEADLINE: ChangeSeverity.MEDIUM,
    ChangeCategory.TIMING: ChangeSeverity.LOW,
    ChangeCategory.FRESHNESS: ChangeSeverity.LOW,
    ChangeCategory.INTEGRITY: ChangeSeverity.HIGH,
    ChangeCategory.ASSURANCE: ChangeSeverity.MEDIUM,
}

#: Ordering band for presentation. Highest attention first.
_SEVERITY_RANK = {
    ChangeSeverity.CRITICAL: 0,
    ChangeSeverity.HIGH: 1,
    ChangeSeverity.MEDIUM: 2,
    ChangeSeverity.LOW: 3,
}

#: Category order within a severity band — stable and documented, so two
#: changes of equal severity never swap between requests.
_CATEGORY_RANK = {category: i for i, category in enumerate(ChangeCategory)}


def _severity_for(category: ChangeCategory, after: str | None) -> ChangeSeverity:
    if after is not None:
        pinned = _SEVERITY.get((category, after))
        if pinned is not None:
            return pinned
    return _DEFAULT_SEVERITY[category]


def _change_hash(
    *,
    baseline_hash: str | None,
    current_hash: str,
    kind: ChangeKind,
    category: ChangeCategory,
    subject: str,
    transitions: Sequence[FieldTransition],
) -> str:
    """Stable identity for one change.

    Binds BOTH snapshot hashes, so the same transition observed between a
    different pair of states is a different change — which is what lets a future
    notification path deduplicate deliveries without inventing its own key, and
    without a database id ever becoming semantic identity.
    """
    return domain_hash(DOMAIN_RETENTION_CHANGE, {
        "baseline_snapshot_hash": baseline_hash,
        "current_snapshot_hash": current_hash,
        "kind": kind.value,
        "category": category.value,
        "subject": subject,
        "transitions": [
            {"field": t.field, "before": t.before, "after": t.after}
            for t in transitions
        ],
    })


def _opportunity_changes(
    baseline: Mapping[str, OpportunitySnapshot],
    current: Mapping[str, OpportunitySnapshot],
    *,
    baseline_hash: str | None,
    current_hash: str,
) -> list[MaterialChange]:
    changes: list[MaterialChange] = []

    for code in sorted(current.keys() - baseline.keys()):
        item = current[code]
        transitions: tuple[FieldTransition, ...] = (
            FieldTransition("availability", None, item.availability),
            FieldTransition("timing", None, item.timing),
        )
        changes.append(MaterialChange(
            change_id=_change_hash(
                baseline_hash=baseline_hash, current_hash=current_hash,
                kind=ChangeKind.ADDED, category=ChangeCategory.OPPORTUNITY,
                subject=code, transitions=transitions),
            kind=ChangeKind.ADDED,
            category=ChangeCategory.OPPORTUNITY,
            # An arrival is graded by the state it arrives in: an opportunity
            # that appears already URGENT is not the same news as one with
            # months to run.
            severity=_severity_for(ChangeCategory.TIMING, item.timing),
            subject=code,
            transitions=transitions,
        ))

    for code in sorted(baseline.keys() - current.keys()):
        item = baseline[code]
        transitions = (
            FieldTransition("availability", item.availability, None),
        )
        changes.append(MaterialChange(
            change_id=_change_hash(
                baseline_hash=baseline_hash, current_hash=current_hash,
                kind=ChangeKind.REMOVED, category=ChangeCategory.OPPORTUNITY,
                subject=code, transitions=transitions),
            kind=ChangeKind.REMOVED,
            category=ChangeCategory.OPPORTUNITY,
            severity=ChangeSeverity.MEDIUM,
            subject=code,
            transitions=transitions,
        ))

    # One CHANGED record per (subject, category), so a category that moved on
    # two fields — a deadline whose code and date both changed — stays one
    # event rather than two.
    for code in sorted(baseline.keys() & current.keys()):
        before, after = baseline[code], current[code]
        by_category: dict[ChangeCategory, list[FieldTransition]] = {}
        for field, category in _OPPORTUNITY_FIELDS:
            was, now = getattr(before, field), getattr(after, field)
            if was != now:
                by_category.setdefault(category, []).append(
                    FieldTransition(field, was, now))

        for category in sorted(by_category, key=lambda c: _CATEGORY_RANK[c]):
            grouped: tuple[FieldTransition, ...] = tuple(sorted(
                by_category[category], key=lambda t: t.field))
            # Severity reads the PRIMARY field's destination — the axis the
            # category is named for — not whichever field sorted first.
            primary = next(
                (t for t in grouped if t.field == category.value.lower()),
                grouped[0])
            severity = _severity_for(category, primary.after)
            if category is ChangeCategory.INTEGRITY:
                grouped = (*grouped, FieldTransition(
                    "integrity_reason_code",
                    before.integrity_reason_code, after.integrity_reason_code))
            changes.append(MaterialChange(
                change_id=_change_hash(
                    baseline_hash=baseline_hash, current_hash=current_hash,
                    kind=ChangeKind.CHANGED, category=category,
                    subject=code, transitions=grouped),
                kind=ChangeKind.CHANGED,
                category=category,
                severity=severity,
                subject=code,
                transitions=grouped,
            ))

    return changes


def _family_changes(
    baseline: Mapping[str, FamilySnapshot],
    current: Mapping[str, FamilySnapshot],
    *,
    baseline_hash: str | None,
    current_hash: str,
) -> list[MaterialChange]:
    """Assurance family standing. Only CHANGED is modelled: families are a
    fixed governed taxonomy, so one appearing or vanishing is a change in what
    the software reports about itself, and it travels as a status transition to
    or from its absent state rather than as a fabricated opportunity event."""
    changes: list[MaterialChange] = []
    for family in sorted(baseline.keys() | current.keys()):
        was = baseline.get(family)
        now = current.get(family)
        before_status = was.status if was is not None else None
        after_status = now.status if now is not None else None
        if before_status == after_status:
            continue
        transitions = (
            FieldTransition("status", before_status, after_status),
            FieldTransition(
                "reason_code",
                was.reason_code if was is not None else None,
                now.reason_code if now is not None else None),
        )
        changes.append(MaterialChange(
            change_id=_change_hash(
                baseline_hash=baseline_hash, current_hash=current_hash,
                kind=ChangeKind.CHANGED, category=ChangeCategory.ASSURANCE,
                subject=family, transitions=transitions),
            kind=ChangeKind.CHANGED,
            category=ChangeCategory.ASSURANCE,
            severity=_DEFAULT_SEVERITY[ChangeCategory.ASSURANCE],
            subject=family,
            transitions=transitions,
        ))
    return changes


def _order_key(change: MaterialChange) -> tuple:
    """THE ordering rule: attention first, then a stable documented tie-break.

    Severity band, then category, then subject. Nothing sorts by money and
    nothing re-ranks what the optimizer ranked — presentation order here is
    about what needs looking at, not about what is worth most.
    """
    return (
        _SEVERITY_RANK[change.severity],
        _CATEGORY_RANK[change.category],
        change.subject,
        change.kind.value,
    )


def _transition(change: MaterialChange, field: str) -> FieldTransition | None:
    return next((t for t in change.transitions if t.field == field), None)


def _summarize(changes: Sequence[MaterialChange]) -> ChangeSummary:
    by_kind = {kind.value: 0 for kind in ChangeKind}
    by_category = {category.value: 0 for category in ChangeCategory}
    by_severity = {severity.value: 0 for severity in ChangeSeverity}
    for change in changes:
        by_kind[change.kind.value] += 1
        by_category[change.category.value] += 1
        by_severity[change.severity.value] += 1

    def timing_to(band: str) -> int:
        return sum(
            1 for c in changes
            if c.category is ChangeCategory.TIMING
            and c.kind is ChangeKind.CHANGED
            and (t := _transition(c, "timing")) is not None
            and t.after == band
        )

    evidence = [
        t for c in changes if c.category is ChangeCategory.EVIDENCE
        and (t := _transition(c, "evidence")) is not None
    ]

    def evidence_moved(improved: bool) -> int:
        moved = 0
        for t in evidence:
            before = _EVIDENCE_RANK.get(t.before or "")
            after = _EVIDENCE_RANK.get(t.after or "")
            # NOT_REQUIRED is outside the order, so a transition touching it is
            # counted as neither an improvement nor a regression rather than
            # being forced onto a scale it does not belong to.
            if before is None or after is None:
                continue
            if (after > before) is improved and after != before:
                moved += 1
        return moved

    return ChangeSummary(
        total=len(changes),
        by_kind=by_kind,
        by_category=by_category,
        by_severity=by_severity,
        opportunities_added=by_kind[ChangeKind.ADDED.value],
        opportunities_removed=by_kind[ChangeKind.REMOVED.value],
        newly_urgent=timing_to("URGENT"),
        newly_expired=timing_to("EXPIRED"),
        newly_blocked=sum(
            1 for c in changes
            if c.category is ChangeCategory.OPPORTUNITY
            and c.kind is ChangeKind.CHANGED
            and (t := _transition(c, "availability")) is not None
            and t.after == "BLOCKED"
        ),
        decision_changes=by_category[ChangeCategory.DECISION.value],
        execution_reports=sum(
            1 for c in changes
            if c.category is ChangeCategory.EXECUTION
            and (t := _transition(c, "execution")) is not None
            and t.after == "USER_REPORTED"
        ),
        evidence_improvements=evidence_moved(improved=True),
        evidence_regressions=evidence_moved(improved=False),
        freshness_changes=by_category[ChangeCategory.FRESHNESS.value],
        integrity_changes=by_category[ChangeCategory.INTEGRITY.value],
    )


def compare_retention_snapshots(
    acknowledged: RetentionSnapshot | None,
    current: RetentionSnapshot,
    *,
    current_hash: str,
    acknowledged_hash: str | None = None,
) -> RetentionChangeSet:
    """Baseline in, current in, material changes out. Pure and total.

    With no acknowledged baseline the result is `NO_BASELINE` and an EMPTY
    change list — never the current state restated as arrivals. There is no
    prior authoritative state to have changed from, and saying otherwise would
    manufacture events out of a first visit.

    O(n log n): both sides are indexed by semantic identity, so matching is by
    key rather than pairwise, and the only superlinear step is the final sort.
    """
    if acknowledged is None:
        return RetentionChangeSet(
            contract_version=RETENTION_CHANGES_CONTRACT_VERSION,
            tax_year=current.tax_year,
            as_of=current.evaluated_as_of,
            baseline_status=BaselineStatus.NO_BASELINE,
            baseline_snapshot_hash=None,
            current_snapshot_hash=current_hash,
            changes=(),
            summary=_summarize(()),
        )

    changes = [
        *_opportunity_changes(
            {o.opportunity_code: o for o in acknowledged.opportunities},
            {o.opportunity_code: o for o in current.opportunities},
            baseline_hash=acknowledged_hash, current_hash=current_hash),
        *_family_changes(
            {f.family: f for f in acknowledged.families},
            {f.family: f for f in current.families},
            baseline_hash=acknowledged_hash, current_hash=current_hash),
    ]
    changes.sort(key=_order_key)

    return RetentionChangeSet(
        contract_version=RETENTION_CHANGES_CONTRACT_VERSION,
        tax_year=current.tax_year,
        as_of=current.evaluated_as_of,
        baseline_status=BaselineStatus.ESTABLISHED,
        baseline_snapshot_hash=acknowledged_hash,
        current_snapshot_hash=current_hash,
        changes=tuple(changes),
        summary=_summarize(changes),
    )
