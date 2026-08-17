"""Retention change set → product contract. Pure: no session, no computation."""
from __future__ import annotations

from app.database.models import RetentionCheckpoint
from app.schemas.retention import (
    RETENTION_CHANGES_SCHEMA_VERSION,
    BaselineCheckpointOut,
    ChangeSummaryOut,
    FieldTransitionOut,
    MaterialChangeOut,
    RetentionChangesOut,
    RetentionCheckpointOut,
)
from app.services.ioe.retention.domain import RetentionChangeSet


def _baseline(row: RetentionCheckpoint | None) -> BaselineCheckpointOut | None:
    if row is None:
        return None
    return BaselineCheckpointOut(
        id=row.id,
        snapshot_hash=row.snapshot_hash,
        snapshot_schema_version=row.snapshot_schema_version,
        evaluated_as_of=row.evaluated_as_of.isoformat(),
        acknowledged_at=row.acknowledged_at.isoformat(),
    )


def changes_detail(
    changes: RetentionChangeSet, baseline: RetentionCheckpoint | None
) -> RetentionChangesOut:
    summary = changes.summary
    return RetentionChangesOut(
        schema_version=RETENTION_CHANGES_SCHEMA_VERSION,
        tax_year=changes.tax_year,
        as_of=changes.as_of,
        baseline_status=changes.baseline_status.value,
        baseline_checkpoint=_baseline(baseline),
        current_snapshot_hash=changes.current_snapshot_hash,
        changes=[
            MaterialChangeOut(
                change_id=change.change_id,
                kind=change.kind.value,
                category=change.category.value,
                severity=change.severity.value,
                subject=change.subject,
                transitions=[
                    FieldTransitionOut(
                        field=t.field, before=t.before, after=t.after)
                    for t in change.transitions
                ],
            )
            for change in changes.changes
        ],
        summary=ChangeSummaryOut(
            total=summary.total,
            by_kind=dict(summary.by_kind),
            by_category=dict(summary.by_category),
            by_severity=dict(summary.by_severity),
            opportunities_added=summary.opportunities_added,
            opportunities_removed=summary.opportunities_removed,
            newly_urgent=summary.newly_urgent,
            newly_expired=summary.newly_expired,
            newly_blocked=summary.newly_blocked,
            decision_changes=summary.decision_changes,
            execution_reports=summary.execution_reports,
            evidence_improvements=summary.evidence_improvements,
            evidence_regressions=summary.evidence_regressions,
            freshness_changes=summary.freshness_changes,
            integrity_changes=summary.integrity_changes,
        ),
    )


def checkpoint_detail(row: RetentionCheckpoint) -> RetentionCheckpointOut:
    return RetentionCheckpointOut(
        id=row.id,
        tax_year=row.tax_year,
        snapshot_hash=row.snapshot_hash,
        snapshot_schema_version=row.snapshot_schema_version,
        evaluated_as_of=row.evaluated_as_of.isoformat(),
        acknowledged_at=row.acknowledged_at.isoformat(),
        supersedes_checkpoint_id=row.supersedes_checkpoint_id,
    )
