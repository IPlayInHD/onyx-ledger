"""ComparisonService — diff a draft version against its published baseline.

Builds snapshots from the DB rows (version + formula + condition count) so it
works even for baselines that predate TKMS (no extracted-rule payload), and
persists a tkms.change_report + change_items. Non-blocking: review can proceed
whether or not a baseline exists.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound
from app.database.models import (
    CalcFormula,
    ChangeReport,
    RuleCondition,
    RuleConditionGroup,
    TaxRuleVersion,
)
from app.database.models import (
    ChangeItem as ChangeItemRow,
)
from app.services.tkms.comparison.differ import diff_snapshots


class ComparisonService:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def compare(self, draft_version_id: uuid.UUID) -> ChangeReport:
        draft = await self.s.get(TaxRuleVersion, draft_version_id)
        if draft is None:
            raise NotFound("Draft version not found")

        baseline = await self.s.scalar(
            select(TaxRuleVersion).where(
                TaxRuleVersion.tax_rule_id == draft.tax_rule_id,
                TaxRuleVersion.tax_year == draft.tax_year,
                TaxRuleVersion.status == "published",
                TaxRuleVersion.id != draft.id,
            )
        )

        draft_snap = await self._snapshot(draft)
        baseline_snap = await self._snapshot(baseline) if baseline else None
        items, summary = diff_snapshots(baseline_snap, draft_snap)

        report = ChangeReport(
            import_job_id=draft.import_job_id,
            draft_version_id=draft.id,
            baseline_version_id=baseline.id if baseline else None,
            summary=summary,
        )
        self.s.add(report)
        await self.s.flush()
        for it in items:
            self.s.add(ChangeItemRow(
                change_report_id=report.id,
                field=it.field,
                change_type=it.change_type,
                old_value=it.old_value,
                new_value=it.new_value,
            ))
        await self.s.flush()
        return report

    async def _snapshot(self, version: TaxRuleVersion) -> dict:
        formula_expr = None
        if version.formula_id is not None:
            formula_expr = await self.s.scalar(
                select(CalcFormula.expression).where(CalcFormula.id == version.formula_id)
            )
        condition_count = await self.s.scalar(
            select(func.count(RuleCondition.id))
            .join(RuleConditionGroup, RuleConditionGroup.id == RuleCondition.group_id)
            .where(RuleConditionGroup.rule_version_id == version.id)
        )
        return {
            "description": version.description,
            "effective_date": version.effective_date,
            "expiry_date": version.expiry_date,
            "max_amount": version.max_amount,
            "min_amount": version.min_amount,
            "income_threshold_low": version.income_threshold_low,
            "income_threshold_high": version.income_threshold_high,
            "reduction_rate": version.reduction_rate,
            "formula_expression": formula_expr,
            "condition_count": condition_count or 0,
        }
