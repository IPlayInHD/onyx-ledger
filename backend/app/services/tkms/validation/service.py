"""ValidationService — orchestrates the pure checks and persists the report.

Validates the CANONICAL staged rules (tkms.extracted_rule.payload) plus a
reference context loaded from the DB, writes a tkms.validation_report with one
finding per issue, and reflects the outcome on the import job. An ERROR result
blocks promotion/publication; the deterministic engine only ever reads published
rules, so nothing that fails here can affect a user's calculation.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound, ValidationError
from app.database.models import (
    ExtractedRule as ExtractedRuleRow,
)
from app.database.models import (
    FactDefinition,
    ImportJob,
    Jurisdiction,
    Province,
    TaxYear,
    ValidationReport,
)
from app.database.models import (
    ValidationFinding as ValidationFindingRow,
)
from app.services.tkms.domain.models import ExtractedRule, ValidationOutcome
from app.services.tkms.validation.checks import (
    ValidationContext,
    check_batch_duplicates,
    validate_rule,
)


class ValidationService:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def validate_job(self, job_id: uuid.UUID) -> ValidationReport:
        """Validate every staged rule for a job; persist a report + findings."""
        job = await self.s.get(ImportJob, job_id)
        if job is None:
            raise NotFound("Import job not found")

        rows = list(
            await self.s.scalars(
                select(ExtractedRuleRow)
                .where(ExtractedRuleRow.import_job_id == job_id)
                .order_by(ExtractedRuleRow.ordinal)
            )
        )
        if not rows:
            raise ValidationError("Nothing to validate: no extracted rules for this job")

        ctx = await self._load_context()
        rules = [ExtractedRule.from_payload(r.payload) for r in rows]

        outcome = ValidationOutcome()
        for rule in rules:
            outcome.findings.extend(validate_rule(rule, ctx))
        outcome.findings.extend(check_batch_duplicates(rules))

        report = await self._persist(job, outcome, target_version_id=None)
        return report

    async def validate_draft(
        self, job_id: uuid.UUID | None, version_id: uuid.UUID, rule: ExtractedRule
    ) -> ValidationReport:
        """Validate a single draft version against its canonical rule."""
        ctx = await self._load_context()
        outcome = ValidationOutcome(findings=validate_rule(rule, ctx))
        job = await self.s.get(ImportJob, job_id) if job_id else None
        return await self._persist(job, outcome, target_version_id=version_id)

    # ---- persistence ---------------------------------------------------------
    async def _persist(
        self,
        job: ImportJob | None,
        outcome: ValidationOutcome,
        *,
        target_version_id: uuid.UUID | None,
    ) -> ValidationReport:
        report = ValidationReport(
            import_job_id=job.id if job else None,
            target_version_id=target_version_id,
            status=outcome.status,
        )
        self.s.add(report)
        await self.s.flush()
        for f in outcome.findings:
            self.s.add(ValidationFindingRow(
                report_id=report.id,
                rule_ref=f.rule_ref,
                severity=f.severity,
                code=f.code,
                message=f.message,
            ))
        if job is not None:
            job.validation_status = outcome.status
            if outcome.status == "failed":
                job.status = "validating"      # stays in validation; cannot advance
            else:
                job.status = "validated"
        await self.s.flush()
        return report

    # ---- reference context ---------------------------------------------------
    async def _load_context(self) -> ValidationContext:
        facts = set(await self.s.scalars(select(FactDefinition.fact_key)))
        jurisdictions = set(await self.s.scalars(select(Jurisdiction.code)))
        provinces = set(await self.s.scalars(select(Province.code)))
        years = set(await self.s.scalars(select(TaxYear.year)))
        return ValidationContext(
            known_facts=frozenset(facts),
            known_jurisdictions=frozenset(jurisdictions),
            known_provinces=frozenset(provinces),
            known_years=frozenset(years),
        )
