"""Assurance map → product contract. Pure: no session, no computation.

The domain map is already product-shaped; this layer's whole job is the
Pydantic boundary and the one repository convention the domain layer cannot
express — support always travels as `SupportScore`, so the support disclaimer
is attached on every surface that renders one.
"""
from __future__ import annotations

from app.schemas.assurance import (
    TAX_ASSURANCE_SCHEMA_VERSION,
    AssuranceSummaryOut,
    DeadlineOut,
    EvidenceRequirementOut,
    FamilyAssuranceOut,
    OpportunityAssuranceOut,
    TaxAssuranceOut,
)
from app.schemas.ioe import SupportScore
from app.services.state_graph.assurance import (
    OpportunityAssurance,
    TaxAssuranceMap,
)


def _opportunity_out(item: OpportunityAssurance) -> OpportunityAssuranceOut:
    return OpportunityAssuranceOut(
        opportunity_code=item.opportunity_code,
        source_id=item.source_id,
        eligibility_status=item.eligibility_status,
        status=item.status.value,
        action=item.action.value,
        blocked_reason_code=item.blocked_reason_code,
        review_reason_codes=list(item.review_reason_codes),
        evidence_readiness=item.evidence_readiness,
        evidence_requirements=[
            EvidenceRequirementOut.model_validate(r)
            for r in item.evidence_requirements
        ],
        deadline=(
            DeadlineOut(
                deadline_code=item.deadline.deadline_code,
                deadline_date=item.deadline.deadline_date,
                is_hard=item.deadline.is_hard,
                days_remaining=item.deadline.days_remaining,
                urgency=item.deadline.urgency.value,
            )
            if item.deadline is not None else None
        ),
        deadline_count=item.deadline_count,
        urgency=item.urgency.value,
        support=SupportScore(
            display_support_score=item.support.display_support_score,
            assumption_adjusted_score=item.support.assumption_adjusted_score,
            raw_support_score=item.support.raw_support_score,
            support_cap_applied=item.support.cap_applied,
            support_cap_reason_code=item.support.cap_reason_code,
        ),
        assumption_dependent=item.assumption_dependent,
        standalone_potential=item.standalone_potential,
        incremental_portfolio_benefit=item.incremental_portfolio_benefit,
        candidate_rank=item.candidate_rank,
        freshness=item.freshness,
        stale_reason_codes=list(item.stale_reason_codes),
        integrity=item.integrity,
        integrity_reason_code=item.integrity_reason_code,
    )


def assurance_detail(assurance: TaxAssuranceMap) -> TaxAssuranceOut:
    return TaxAssuranceOut(
        schema_version=TAX_ASSURANCE_SCHEMA_VERSION,
        view=assurance.view,
        tax_year=assurance.tax_year,
        as_of=assurance.as_of,
        graph_hash=assurance.graph_hash,
        families=[
            FamilyAssuranceOut(
                family=f.family, status=f.status.value,
                reason_code=f.reason_code, item_count=f.item_count,
            )
            for f in assurance.families
        ],
        opportunities=[_opportunity_out(i) for i in assurance.opportunities],
        attention=list(assurance.attention),
        assumption_codes=list(assurance.assumption_codes),
        summary=AssuranceSummaryOut(
            opportunity_count=assurance.summary.opportunity_count,
            opportunities_by_status=dict(
                assurance.summary.opportunities_by_status),
            opportunities_by_action=dict(
                assurance.summary.opportunities_by_action),
            opportunities_by_urgency=dict(
                assurance.summary.opportunities_by_urgency),
            assumption_dependent_count=(
                assurance.summary.assumption_dependent_count),
            upcoming_deadline_count=assurance.summary.upcoming_deadline_count,
            families_by_status=dict(assurance.summary.families_by_status),
        ),
    )
