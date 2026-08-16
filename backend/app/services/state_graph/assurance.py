"""The Tax Assurance Map — a deterministic product-readiness read model
(Entry: Tax Assurance Map).

WHAT IT IS. A pure derivation over the CURRENT-view Personal Tax State Graph:
which opportunities exist, which are actionable, which need evidence, which
need review, which are blocked, what is urgent, and what standing each family
of the user's tax position is in. One function, no I/O, no clock reads.

WHAT IT IS NOT. It computes no tax, decides no eligibility, resolves no rule,
reads no document and ranks nothing the optimizer has not already ranked.
Every value here is read off the graph, and every value on the graph was put
there by the authority that owns it. This module's entire contribution is a
closed status vocabulary and the documented precedence between conditions that
already exist.

MODE. CURRENT-STATE ONLY, deliberately. The graph's CURRENT view is the one
certified read model of live governed state; historical assurance for a sealed
scenario is a different question with a different authority, already answered
by the Before-You-Act comparison. Blending the two here would mix live
opportunity state with frozen evidence — the exact confusion the mode split
exists to prevent.

TIME. `as_of` is an explicit input, injected by the caller and echoed in the
output. Nothing here reads a clock: the same graph and the same `as_of`
produce the same map, byte for byte. `as_of` participates ONLY in deadline
urgency; no other field depends on it.

WHAT IS DELIBERATELY ABSENT.
  * A single numeric "assurance score". No governed model exists that says
    what such a number would measure, and an unweighted blend of readiness,
    urgency and support would invite reading it as a probability of being
    right. The summary stays multi-dimensional.
  * A "what changed since last visit" section. There is no governed
    previous-current-state authority to compare against — that is a retention
    engine dependency, recorded, not manufactured here.
  * An ActionStatus of COMPLETE. Nothing in governed state records that a tax
    action was actually taken; a simulated scenario is not a taken action.
    COMPLETE awaits the Decision Journal authority.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from app.services.ioe.domain import canonical as c
from app.services.state_graph.contracts import (
    EdgeType,
    EvidenceReadiness,
    GraphNode,
    NodeType,
    TaxStateGraph,
)

#: The assurance READ-MODEL contract version. Independent of the scenario-result
#: protocol and of the Before-You-Act contract: this artifact has its own shape
#: and its own consumers, and coupling it to either would re-version it for
#: changes it cannot see.
ASSURANCE_CONTRACT_VERSION = "1.0.0"

#: Urgency thresholds, in days before the deadline, defined HERE and nowhere
#: else. The numbers are product policy, not tax law: "urgent" is two weeks
#: because that is the window in which contribution/filing mechanics (opening
#: accounts, moving funds) still complete comfortably; "approaching" is two
#: months because that is when a deadline should enter planning. Change them
#: here and every consumer moves together.
URGENT_WITHIN_DAYS = 14
APPROACHING_WITHIN_DAYS = 60


class AssuranceStatus(StrEnum):
    """The closed standing vocabulary, for items and families alike.

    Precedence when several conditions hold, highest first:

        UNAVAILABLE > BLOCKED > REVIEW_REQUIRED > EVIDENCE_REQUIRED > READY

    UNAVAILABLE outranks everything because it is a statement about the
    ABSENCE of authority: a family with no governing run must never read as
    READY merely because it contains zero records — zero records is what
    missing authority looks like from the outside, and the whole point of the
    ordering is that the two never collapse.
    """

    READY = "READY"
    EVIDENCE_REQUIRED = "EVIDENCE_REQUIRED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ActionStatus(StrEnum):
    """What the customer can do about an opportunity right now.

    A deterministic projection of (AssuranceStatus, eligibility_status):

        BLOCKED            <- assurance BLOCKED
        EVIDENCE_REQUIRED  <- assurance EVIDENCE_REQUIRED
        DECISION_REQUIRED  <- assurance REVIEW_REQUIRED, or READY while the
                              rules authority says `conditionally_eligible` —
                              a governed conditional requirement means a human
                              determination is owed before acting
        ACTION_AVAILABLE   <- assurance READY and eligibility `eligible`

    COMPLETE is deliberately not a value — see the module docstring.
    """

    ACTION_AVAILABLE = "ACTION_AVAILABLE"
    DECISION_REQUIRED = "DECISION_REQUIRED"
    EVIDENCE_REQUIRED = "EVIDENCE_REQUIRED"
    BLOCKED = "BLOCKED"


class UrgencyStatus(StrEnum):
    """The deadline axis, evaluated against the explicit `as_of` date.

        EXPIRED      deadline_date <  as_of
        URGENT       0 <= days_remaining <= URGENT_WITHIN_DAYS
        APPROACHING  URGENT_WITHIN_DAYS < days_remaining <= APPROACHING_WITHIN_DAYS
        NORMAL       days_remaining > APPROACHING_WITHIN_DAYS
        NO_DEADLINE  no governed deadline exists

    Its own axis, never folded into AssuranceStatus: a deadline being close
    does not change whether evidence is held, and collapsing the two would
    make "urgent but ready" inexpressible.
    """

    NO_DEADLINE = "NO_DEADLINE"
    NORMAL = "NORMAL"
    APPROACHING = "APPROACHING"
    URGENT = "URGENT"
    EXPIRED = "EXPIRED"


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DeadlineAssurance:
    """One governed deadline, as pinned rule data reports it. Never fabricated:
    absence of a row is absence of a deadline."""

    deadline_code: str
    deadline_date: str  # ISO date, exactly as the governed row carries it
    is_hard: bool
    days_remaining: int
    urgency: UrgencyStatus


@dataclass(frozen=True)
class EvidenceRequirementAssurance:
    """One governed document requirement, product-semantic identity only.

    The document TYPE is the identity (`T4`), never a document id, bucket,
    object key or content hash — readiness semantics, not storage access.
    """

    document_type_code: str
    necessity: str
    readiness: str


@dataclass(frozen=True)
class SupportAssurance:
    """The governed support triple, verbatim. Not a probability of anything —
    the same semantics the support model sealed, passed through unchanged."""

    raw_support_score: str | None
    assumption_adjusted_score: str | None
    display_support_score: str | None
    cap_applied: bool
    cap_reason_code: str | None


@dataclass(frozen=True)
class OpportunityAssurance:
    """One governed opportunity's product-ready standing."""

    opportunity_code: str
    #: The optimizer's candidate id — a stable correlation handle, exposed
    #: because two requests must be able to name the same item.
    source_id: str
    eligibility_status: str
    status: AssuranceStatus
    action: ActionStatus
    #: Why BLOCKED, when BLOCKED. The governed exclusion reason, verbatim.
    blocked_reason_code: str | None
    #: Why REVIEW_REQUIRED, when REVIEW_REQUIRED. Closed codes derived from the
    #: governed inputs that triggered it.
    review_reason_codes: tuple[str, ...]
    evidence_readiness: str
    evidence_requirements: tuple[EvidenceRequirementAssurance, ...]
    #: The EARLIEST governed deadline — the binding one for attention. All
    #: deadlines are counted; only the earliest is unpacked.
    deadline: DeadlineAssurance | None
    deadline_count: int
    urgency: UrgencyStatus
    support: SupportAssurance
    #: True iff the governed support model ADJUSTED this item for assumption
    #: uncertainty (adjusted differs from raw). Read from the support model's
    #: own output; nothing here re-derives assumption sensitivity.
    assumption_dependent: bool
    #: Governed impact figures, exactly as sealed. Never computed here.
    standalone_potential: str | None
    incremental_portfolio_benefit: str | None
    #: The optimizer's own sealed rank, passed through for tie-breaking.
    candidate_rank: int | None
    freshness: str
    stale_reason_codes: tuple[str, ...]
    integrity: str
    integrity_reason_code: str


@dataclass(frozen=True)
class FamilyAssurance:
    """One family's standing, with the reason the status is what it is."""

    family: str
    status: AssuranceStatus
    reason_code: str
    item_count: int


@dataclass(frozen=True)
class AssuranceSummary:
    """Counts over the full map. Multi-dimensional by design — see the module
    docstring for why there is no single number."""

    opportunity_count: int
    opportunities_by_status: Mapping[str, int]
    opportunities_by_action: Mapping[str, int]
    opportunities_by_urgency: Mapping[str, int]
    assumption_dependent_count: int
    #: Deadlines not yet expired, across all opportunities.
    upcoming_deadline_count: int
    families_by_status: Mapping[str, int]


@dataclass(frozen=True)
class TaxAssuranceMap:
    contract_version: str
    view: str
    tax_year: int
    as_of: str  # ISO date, the injected evaluation date, echoed verbatim
    #: The certified hash of the graph this map was derived from. The map's
    #: provenance anchor: same graph_hash + same as_of => same map.
    graph_hash: str
    families: tuple[FamilyAssurance, ...]
    #: All opportunity items, in stable identity order (code, then source id).
    opportunities: tuple[OpportunityAssurance, ...]
    #: The attention queue: the same items, in review-next order. Presentation
    #: ordering with a documented key — see `_attention_key` — never a claim
    #: of optimality.
    attention: tuple[str, ...]  # source_ids, most attention-worthy first
    assumption_codes: tuple[str, ...]
    summary: AssuranceSummary


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------
_EVIDENCE_GAP = frozenset({
    EvidenceReadiness.MISSING.value,
    EvidenceReadiness.PARTIAL.value,
    EvidenceReadiness.UNKNOWN.value,
})

#: Review reason codes — closed, each naming the governed input that fired.
REVIEW_REQUIRES_RE_EVALUATION = "GOVERNED_RE_EVALUATION_REQUESTED"
REVIEW_STALE_INPUTS = "INPUTS_CHANGED_SINCE_EVALUATION"
REVIEW_INDETERMINATE_ELIGIBILITY = "ELIGIBILITY_INDETERMINATE"


def _urgency(days_remaining: int) -> UrgencyStatus:
    if days_remaining < 0:
        return UrgencyStatus.EXPIRED
    if days_remaining <= URGENT_WITHIN_DAYS:
        return UrgencyStatus.URGENT
    if days_remaining <= APPROACHING_WITHIN_DAYS:
        return UrgencyStatus.APPROACHING
    return UrgencyStatus.NORMAL


def _deadline_assurance(node: GraphNode, as_of: date) -> DeadlineAssurance:
    raw = node.attributes["deadline_date"]
    deadline_date = raw if isinstance(raw, date) else date.fromisoformat(str(raw))
    days = (deadline_date - as_of).days
    return DeadlineAssurance(
        deadline_code=str(node.attributes.get("deadline_code")),
        deadline_date=deadline_date.isoformat(),
        is_hard=bool(node.attributes.get("is_hard")),
        days_remaining=days,
        urgency=_urgency(days),
    )


def _status_of(
    node: GraphNode, *, blocked_reason: str | None,
    review_reasons: tuple[str, ...], readiness: str,
) -> AssuranceStatus:
    """The documented precedence, applied. First match wins."""
    if blocked_reason is not None:
        return AssuranceStatus.BLOCKED
    if review_reasons:
        return AssuranceStatus.REVIEW_REQUIRED
    if readiness in _EVIDENCE_GAP:
        return AssuranceStatus.EVIDENCE_REQUIRED
    return AssuranceStatus.READY


def _action_of(status: AssuranceStatus, eligibility: str) -> ActionStatus:
    if status is AssuranceStatus.BLOCKED:
        return ActionStatus.BLOCKED
    if status is AssuranceStatus.EVIDENCE_REQUIRED:
        return ActionStatus.EVIDENCE_REQUIRED
    if status is AssuranceStatus.REVIEW_REQUIRED:
        return ActionStatus.DECISION_REQUIRED
    if eligibility == "conditionally_eligible":
        return ActionStatus.DECISION_REQUIRED
    return ActionStatus.ACTION_AVAILABLE


_URGENCY_RANK = {
    UrgencyStatus.URGENT: 0,
    UrgencyStatus.APPROACHING: 1,
    UrgencyStatus.NORMAL: 2,
    UrgencyStatus.NO_DEADLINE: 3,
    # Expired last: the window has closed, so "review next" effort goes first
    # to items where acting is still possible.
    UrgencyStatus.EXPIRED: 4,
}

_ACTION_RANK = {
    ActionStatus.EVIDENCE_REQUIRED: 0,
    ActionStatus.DECISION_REQUIRED: 1,
    ActionStatus.ACTION_AVAILABLE: 2,
    ActionStatus.BLOCKED: 3,
}


def _attention_key(item: OpportunityAssurance) -> tuple:
    """THE ordering rule, in one place.

    1. Deadline urgency band — time pressure first, expired last.
    2. Action band — gaps the customer can close (evidence, decisions) ahead
       of items that are simply ready, blocked items last.
    3. The optimizer's own sealed `candidate_rank` — material impact ordering
       is DELEGATED to the authority that already ranked it. This module never
       re-ranks by amount, which is what keeps it a presentation order rather
       than a second recommendation engine.
    4. Identity, so the order is total and two equal items cannot swap.
    """
    return (
        _URGENCY_RANK[item.urgency],
        _ACTION_RANK[item.action],
        item.candidate_rank if item.candidate_rank is not None else 10**9,
        item.opportunity_code,
        item.source_id,
    )


def derive_assurance_map(graph: TaxStateGraph, *, as_of: date) -> TaxAssuranceMap:
    """Graph in, map out. Pure and total: any well-formed CURRENT graph
    derives, and the same (graph, as_of) always derives the same map."""
    nodes_by_key = {node.key: node for node in graph.nodes}

    # Edges, indexed once. O(nodes + edges) throughout — no pairwise pass.
    requires: dict[str, list[str]] = {}
    expires_at: dict[str, list[str]] = {}
    blocked_by_edge: set[str] = set()
    for edge in graph.edges:
        if edge.edge_type is EdgeType.REQUIRES:
            requires.setdefault(edge.source_key, []).append(edge.target_key)
        elif edge.edge_type is EdgeType.EXPIRES_AT:
            expires_at.setdefault(edge.source_key, []).append(edge.target_key)
        elif edge.edge_type in (
            EdgeType.INELIGIBLE_BECAUSE, EdgeType.CONSTRAINED_BY,
        ):
            blocked_by_edge.add(edge.source_key)

    run_present = any(
        anchor.artifact == "ioe.optimization_run" for anchor in graph.anchors)
    analysis_present = any(
        anchor.artifact == "analysis.analysis_run" for anchor in graph.anchors)

    items: list[OpportunityAssurance] = []
    for node in graph.nodes:
        if node.node_type is not NodeType.OPPORTUNITY:
            continue
        attributes = node.attributes

        exclusion_reason = attributes.get("exclusion_reason_code")
        blocked_reason = (
            str(exclusion_reason) if exclusion_reason is not None
            else ("PORTFOLIO_EXCLUSION" if node.key in blocked_by_edge else None)
        )

        review_reasons: list[str] = []
        if attributes.get("requires_re_evaluation"):
            review_reasons.append(REVIEW_REQUIRES_RE_EVALUATION)
        if node.freshness.value == "stale":
            review_reasons.append(REVIEW_STALE_INPUTS)
        if attributes.get("eligibility_status") == "indeterminate":
            review_reasons.append(REVIEW_INDETERMINATE_ELIGIBILITY)

        # `None` means no governed requirement row exists for this item's rule
        # version — nothing demanded, so nothing can be missing. Distinct from
        # UNKNOWN, which is a real requirement whose applicability is not
        # decidable from structured data.
        readiness = attributes.get("readiness") or EvidenceReadiness.NOT_REQUIRED.value

        requirements = tuple(sorted(
            (
                EvidenceRequirementAssurance(
                    document_type_code=str(req.attributes["document_type_code"]),
                    necessity=str(req.attributes["necessity"]),
                    readiness=str(req.attributes["readiness"]),
                )
                for req in (
                    nodes_by_key[target] for target in requires.get(node.key, ())
                )
                if req.attributes.get("evidence_kind") == "requirement"
            ),
            key=lambda r: (r.document_type_code, r.necessity),
        ))

        deadlines = sorted(
            (
                _deadline_assurance(nodes_by_key[target], as_of)
                for target in expires_at.get(node.key, ())
            ),
            key=lambda d: (d.deadline_date, d.deadline_code),
        )
        earliest = deadlines[0] if deadlines else None

        status = _status_of(
            node, blocked_reason=blocked_reason,
            review_reasons=tuple(review_reasons), readiness=str(readiness),
        )
        eligibility = str(attributes.get("eligibility_status"))

        raw = attributes.get("raw_support_score")
        adjusted = attributes.get("assumption_adjusted_score")
        items.append(OpportunityAssurance(
            opportunity_code=str(attributes.get("opportunity_code")),
            source_id=node.source_id,
            eligibility_status=eligibility,
            status=status,
            action=_action_of(status, eligibility),
            blocked_reason_code=blocked_reason,
            review_reason_codes=tuple(review_reasons),
            evidence_readiness=str(readiness),
            evidence_requirements=requirements,
            deadline=earliest,
            deadline_count=len(deadlines),
            urgency=earliest.urgency if earliest else UrgencyStatus.NO_DEADLINE,
            support=SupportAssurance(
                raw_support_score=raw,
                assumption_adjusted_score=adjusted,
                display_support_score=attributes.get("display_support_score"),
                cap_applied=bool(attributes.get("support_cap_applied")),
                cap_reason_code=attributes.get("support_cap_reason_code"),
            ),
            assumption_dependent=(
                raw is not None and adjusted is not None and raw != adjusted
            ),
            standalone_potential=attributes.get("standalone_potential"),
            incremental_portfolio_benefit=attributes.get(
                "incremental_portfolio_benefit"),
            candidate_rank=attributes.get("candidate_rank"),
            freshness=node.freshness.value,
            stale_reason_codes=node.stale_reason_codes,
            integrity=node.integrity.value,
            integrity_reason_code=node.integrity_reason_code,
        ))

    items.sort(key=lambda i: (i.opportunity_code, i.source_id))
    attention = tuple(
        item.source_id for item in sorted(items, key=_attention_key))

    assumption_codes = tuple(sorted(
        str(node.attributes["assumption_code"])
        for node in graph.nodes
        if node.node_type is NodeType.ASSUMPTION
    ))

    families = _families(
        graph, items,
        run_present=run_present, analysis_present=analysis_present,
        assumption_count=len(assumption_codes),
    )

    return TaxAssuranceMap(
        contract_version=ASSURANCE_CONTRACT_VERSION,
        view=graph.scope.view.value,
        tax_year=graph.scope.tax_year,
        as_of=as_of.isoformat(),
        graph_hash=graph.graph_hash,
        families=families,
        opportunities=tuple(items),
        attention=attention,
        assumption_codes=assumption_codes,
        summary=_summarize(items, families),
    )


#: Family status reasons — closed codes, each naming the authority present or
#: absent. `NO_OPTIMIZATION_RUN_FOR_TAX_YEAR` marks every family whose content
#: is reached through the pinned rule versions of a run that does not exist.
REASON_RUN_AUTHORITATIVE = "OPTIMIZATION_RUN_AUTHORITATIVE"
REASON_NO_RUN = "NO_OPTIMIZATION_RUN_FOR_TAX_YEAR"
REASON_ANALYSIS_AUTHORITATIVE = "ANALYSIS_RUN_AUTHORITATIVE"
REASON_NO_ANALYSIS = "NO_ANALYSIS_RUN_FOR_TAX_YEAR"
REASON_LIVE_RECORDS = "LIVE_RECORDS_AUTHORITATIVE"
REASON_RESERVED = "RESERVED_NO_PRODUCER"
#: A run exists but declared no assumptions THE GRAPH CAN SEE. Runs sealed
#: before the assumption-set column existed and runs that genuinely declared
#: an empty set both emit zero nodes, and the graph does not record which one
#: happened — so this family fails CLOSED to UNAVAILABLE rather than claiming
#: an authoritative emptiness that was never recorded.
REASON_ASSUMPTIONS_ABSENT = "ASSUMPTION_DECLARATION_ABSENT"


def _families(
    graph: TaxStateGraph,
    items: Sequence[OpportunityAssurance],
    *,
    run_present: bool,
    analysis_present: bool,
    assumption_count: int,
) -> tuple[FamilyAssurance, ...]:
    counts = {t.value: 0 for t in NodeType}
    for node in graph.nodes:
        counts[node.node_type.value] += 1

    def family(
        node_type: NodeType, status: AssuranceStatus, reason: str,
    ) -> FamilyAssurance:
        return FamilyAssurance(
            family=node_type.value, status=status, reason_code=reason,
            item_count=counts[node_type.value],
        )

    run_gated = (
        AssuranceStatus.READY if run_present else AssuranceStatus.UNAVAILABLE,
        REASON_RUN_AUTHORITATIVE if run_present else REASON_NO_RUN,
    )
    return (
        family(NodeType.FACT, AssuranceStatus.READY, REASON_LIVE_RECORDS),
        family(
            NodeType.TAX_STATE,
            AssuranceStatus.READY if analysis_present
            else AssuranceStatus.UNAVAILABLE,
            REASON_ANALYSIS_AUTHORITATIVE if analysis_present
            else REASON_NO_ANALYSIS,
        ),
        family(NodeType.OPPORTUNITY, *run_gated),
        family(NodeType.DEADLINE, *run_gated),
        family(NodeType.EVIDENCE, *run_gated),
        family(NodeType.RESOURCE, *run_gated),
        family(
            NodeType.ASSUMPTION,
            AssuranceStatus.READY
            if run_present and assumption_count > 0
            else AssuranceStatus.UNAVAILABLE,
            (REASON_RUN_AUTHORITATIVE if assumption_count > 0
             else REASON_ASSUMPTIONS_ABSENT)
            if run_present else REASON_NO_RUN,
        ),
        family(NodeType.SCENARIO, AssuranceStatus.READY, REASON_LIVE_RECORDS),
        family(
            NodeType.OBLIGATION, AssuranceStatus.NOT_APPLICABLE, REASON_RESERVED),
        family(
            NodeType.DECISION, AssuranceStatus.NOT_APPLICABLE, REASON_RESERVED),
    )


def _summarize(
    items: Sequence[OpportunityAssurance],
    families: Sequence[FamilyAssurance],
) -> AssuranceSummary:
    by_status = {status.value: 0 for status in AssuranceStatus}
    by_action = {action.value: 0 for action in ActionStatus}
    by_urgency = {urgency.value: 0 for urgency in UrgencyStatus}
    for item in items:
        by_status[item.status.value] += 1
        by_action[item.action.value] += 1
        by_urgency[item.urgency.value] += 1

    families_by_status = {status.value: 0 for status in AssuranceStatus}
    for entry in families:
        families_by_status[entry.status.value] += 1

    return AssuranceSummary(
        opportunity_count=len(items),
        opportunities_by_status=by_status,
        opportunities_by_action=by_action,
        opportunities_by_urgency=by_urgency,
        assumption_dependent_count=sum(
            1 for item in items if item.assumption_dependent),
        upcoming_deadline_count=sum(
            1 for item in items
            if item.deadline is not None
            and item.urgency is not UrgencyStatus.EXPIRED
        ),
        families_by_status=families_by_status,
    )


# ---------------------------------------------------------------------------
# Canonical form — for determinism proofs. No hash is minted and nothing is
# persisted; the map's provenance anchor is the graph_hash it already carries.
# ---------------------------------------------------------------------------
def canonical_assurance_payload(assurance: TaxAssuranceMap) -> Mapping[str, Any]:
    def deadline(entry: DeadlineAssurance | None) -> Mapping[str, Any] | None:
        if entry is None:
            return None
        return {
            "deadline_code": entry.deadline_code,
            "deadline_date": entry.deadline_date,
            "is_hard": entry.is_hard,
            "days_remaining": entry.days_remaining,
            "urgency": entry.urgency.value,
        }

    return {
        "assurance_contract_version": assurance.contract_version,
        "view": assurance.view,
        "tax_year": assurance.tax_year,
        "as_of": assurance.as_of,
        "graph_hash": assurance.graph_hash,
        "families": [
            {"family": f.family, "status": f.status.value,
             "reason_code": f.reason_code, "item_count": f.item_count}
            for f in assurance.families
        ],
        "opportunities": [
            {
                "opportunity_code": item.opportunity_code,
                "source_id": item.source_id,
                "eligibility_status": item.eligibility_status,
                "status": item.status.value,
                "action": item.action.value,
                "blocked_reason_code": item.blocked_reason_code,
                "review_reason_codes": list(item.review_reason_codes),
                "evidence_readiness": item.evidence_readiness,
                "evidence_requirements": [
                    {"document_type_code": r.document_type_code,
                     "necessity": r.necessity, "readiness": r.readiness}
                    for r in item.evidence_requirements
                ],
                "deadline": deadline(item.deadline),
                "deadline_count": item.deadline_count,
                "urgency": item.urgency.value,
                "support": {
                    "raw_support_score": item.support.raw_support_score,
                    "assumption_adjusted_score":
                        item.support.assumption_adjusted_score,
                    "display_support_score": item.support.display_support_score,
                    "cap_applied": item.support.cap_applied,
                    "cap_reason_code": item.support.cap_reason_code,
                },
                "assumption_dependent": item.assumption_dependent,
                "standalone_potential": item.standalone_potential,
                "incremental_portfolio_benefit":
                    item.incremental_portfolio_benefit,
                "candidate_rank": item.candidate_rank,
                "freshness": item.freshness,
                "stale_reason_codes": list(item.stale_reason_codes),
                "integrity": item.integrity,
                "integrity_reason_code": item.integrity_reason_code,
            }
            for item in assurance.opportunities
        ],
        "attention": list(assurance.attention),
        "assumption_codes": list(assurance.assumption_codes),
        "summary": {
            "opportunity_count": assurance.summary.opportunity_count,
            "opportunities_by_status":
                dict(assurance.summary.opportunities_by_status),
            "opportunities_by_action":
                dict(assurance.summary.opportunities_by_action),
            "opportunities_by_urgency":
                dict(assurance.summary.opportunities_by_urgency),
            "assumption_dependent_count":
                assurance.summary.assumption_dependent_count,
            "upcoming_deadline_count":
                assurance.summary.upcoming_deadline_count,
            "families_by_status": dict(assurance.summary.families_by_status),
        },
    }


def canonical_assurance_text(assurance: TaxAssuranceMap) -> str:
    return c.canonical_text(canonical_assurance_payload(assurance))
