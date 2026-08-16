"""Certified comparison → product contract. Pure: no session, no computation.

Nothing here decides anything. It renames families into the buckets a client
renders, drops the graph mechanics a client has no use for, and passes every
value through exactly as the comparison produced it.

THE ONE PRESENTATION CHOICE. `include_unchanged` selects whether UNCHANGED
records are rendered. It is presentation only, and the two things that make it
safe are asserted by tests: the summary is computed from the FULL comparison,
and `comparison_hash` is the engine's hash of the FULL comparison. Filtering
changes what a client draws, never what the comparison says.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.schemas.before_you_act import (
    BEFORE_YOU_ACT_SCHEMA_VERSION,
    BeforeYouActComparisonOut,
    ChangeOut,
    ComparisonSummaryOut,
    FamilyApplicabilityOut,
    FieldChangeOut,
    ScenarioContextOut,
)
from app.services.ioe.scenario.before_you_act import LoadedComparison
from app.services.ioe.scenario.comparison import ChangeKind, NodeChange
from app.services.state_graph.contracts import NodeType

#: Node family → the response field that renders it. EVIDENCE covers both the
#: required and held sides: a client asking "what would I need to prove" does
#: not want them in two places, and the comparison already distinguishes them
#: within the record.
_FAMILY_FIELDS: tuple[tuple[NodeType, str], ...] = (
    (NodeType.TAX_STATE, "tax_state_changes"),
    (NodeType.OPPORTUNITY, "opportunity_changes"),
    (NodeType.EVIDENCE, "evidence_changes"),
    (NodeType.DEADLINE, "deadline_changes"),
    (NodeType.FACT, "fact_changes"),
    (NodeType.ASSUMPTION, "assumption_changes"),
    (NodeType.SCENARIO, "scenario_changes"),
)


def _change_out(change: NodeChange) -> ChangeOut:
    return ChangeOut(
        key=change.key,
        change=change.change.value,
        fields=[
            FieldChangeOut(
                field=f.field, before=f.before, after=f.after, delta=f.delta
            )
            for f in change.fields
        ],
    )


def _bucket(
    changes: Sequence[NodeChange], family: NodeType, *, include_unchanged: bool
) -> list[ChangeOut]:
    """One family's records, in the comparison's own order.

    The engine already sorted by semantic key, so nothing is re-sorted here — a
    second ordering rule would be a second answer to what "first" means.
    """
    return [
        _change_out(c)
        for c in changes
        if c.family == family.value
        and (include_unchanged or c.change is not ChangeKind.UNCHANGED)
    ]


def comparison_detail(
    loaded: LoadedComparison, *, include_unchanged: bool = False
) -> BeforeYouActComparisonOut:
    comparison = loaded.comparison
    scenario = loaded.scenario

    buckets = {
        field: _bucket(
            comparison.node_changes, family, include_unchanged=include_unchanged
        )
        for family, field in _FAMILY_FIELDS
    }

    return BeforeYouActComparisonOut(
        schema_version=BEFORE_YOU_ACT_SCHEMA_VERSION,
        direction=comparison.direction,
        scenario=ScenarioContextOut(
            id=scenario.id,
            label=scenario.label,
            tax_year=scenario.tax_year,
            jurisdiction=scenario.jurisdiction,
            result_schema_version=scenario.result_schema_version,
            completed_at=scenario.completed_at,
        ),
        summary=ComparisonSummaryOut(
            # From the full comparison, never from the rendered buckets.
            node_counts_by_change=dict(comparison.summary.node_counts_by_change),
            edge_counts_by_change=dict(comparison.summary.edge_counts_by_change),
            changed_families=list(comparison.summary.changed_families),
        ),
        **buckets,
        family_applicability=[
            FamilyApplicabilityOut(
                family=entry.family,
                status=entry.status.value,
                reason_code=entry.reason_code,
            )
            for entry in comparison.node_applicability
        ],
        comparison_hash=comparison.comparison_hash,
        includes_unchanged=include_unchanged,
    )
