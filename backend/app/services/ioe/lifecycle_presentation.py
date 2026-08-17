"""Lifecycle map → product contract. Pure: no session, no computation.

The domain map is already product-shaped; this layer is the Pydantic boundary
and the one repository convention the domain cannot express — support always
travels as `SupportScore`, so its disclaimer accompanies every score.
"""
from __future__ import annotations

from app.schemas.ioe import SupportScore
from app.schemas.opportunity_lifecycle import (
    OPPORTUNITY_LIFECYCLE_SCHEMA_VERSION,
    AttentionFlagsOut,
    JournalLinkOut,
    LifecycleDeadlineOut,
    LifecycleSummaryOut,
    OpportunityLifecycleItemOut,
    OpportunityLifecycleOut,
)
from app.services.ioe.lifecycle.domain import (
    OpportunityLifecycle,
    OpportunityLifecycleMap,
)


def _item_out(entry: OpportunityLifecycle) -> OpportunityLifecycleItemOut:
    return OpportunityLifecycleItemOut(
        opportunity_code=entry.opportunity_code,
        source_id=entry.source_id,
        availability=entry.availability.value,
        decision=entry.decision.value,
        execution=entry.execution.value,
        evidence=entry.evidence,
        timing=entry.timing.value,
        freshness=entry.freshness,
        stale_reason_codes=list(entry.stale_reason_codes),
        integrity=entry.integrity,
        integrity_reason_code=entry.integrity_reason_code,
        deadline=(
            LifecycleDeadlineOut(
                deadline_code=entry.deadline.deadline_code,
                deadline_date=entry.deadline.deadline_date,
                is_hard=entry.deadline.is_hard,
                days_remaining=entry.deadline.days_remaining,
                urgency=entry.deadline.urgency.value,
            )
            if entry.deadline is not None else None
        ),
        days_remaining=entry.days_remaining,
        actionability=entry.actionability.value,
        reason_codes=list(entry.reason_codes),
        attention=AttentionFlagsOut.model_validate(entry.attention),
        journal=(
            JournalLinkOut(
                journal_id=entry.journal.journal_id,
                thread_count=entry.journal.thread_count,
            )
            if entry.journal is not None else None
        ),
        last_reported_action_date=(
            entry.last_reported_action_date.isoformat()
            if entry.last_reported_action_date is not None else None
        ),
        standalone_potential=entry.standalone_potential,
        incremental_portfolio_benefit=entry.incremental_portfolio_benefit,
        candidate_rank=entry.candidate_rank,
        support=SupportScore(
            display_support_score=entry.support.display_support_score,
            assumption_adjusted_score=entry.support.assumption_adjusted_score,
            raw_support_score=entry.support.raw_support_score,
            support_cap_applied=entry.support.cap_applied,
            support_cap_reason_code=entry.support.cap_reason_code,
        ),
        assumption_dependent=entry.assumption_dependent,
    )


def lifecycle_detail(
    lifecycle: OpportunityLifecycleMap,
) -> OpportunityLifecycleOut:
    summary = lifecycle.summary
    return OpportunityLifecycleOut(
        schema_version=OPPORTUNITY_LIFECYCLE_SCHEMA_VERSION,
        tax_year=lifecycle.tax_year,
        as_of=lifecycle.as_of,
        graph_hash=lifecycle.graph_hash,
        opportunity_authority=lifecycle.opportunity_authority,
        opportunity_authority_reason=lifecycle.opportunity_authority_reason,
        opportunities=[_item_out(e) for e in lifecycle.opportunities],
        attention_order=list(lifecycle.attention_order),
        summary=LifecycleSummaryOut(
            opportunity_count=summary.opportunity_count,
            by_availability=dict(summary.by_availability),
            by_decision=dict(summary.by_decision),
            by_execution=dict(summary.by_execution),
            by_timing=dict(summary.by_timing),
            by_actionability=dict(summary.by_actionability),
            needing_attention=summary.needing_attention,
            unlinked_thread_count=summary.unlinked_thread_count,
        ),
    )
