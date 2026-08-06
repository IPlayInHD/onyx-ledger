"""PublicationService — publish an approved + validated version.

Publication is a single transaction: verify the gates (approved change request +
a passed validation report), supersede the currently-published version for the
same rule+year, flip the new version to `published`, write provenance
(validation_report_id) and an admin.rule_publication row. The partial-unique
index (one published per rule+year) makes a double-publish impossible at the DB
level. Re-embedding for AI retrieval is a separate, best-effort step (the engine
never depends on it).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Forbidden, NotFound
from app.database.models import (
    RuleChangeRequest,
    RulePublication,
    TaxRuleVersion,
    ValidationReport,
)
from app.services.admin.service import AdminService
from app.services.ioe.freshness_events import FreshnessEvent, emit
from app.services.tkms.domain.lifecycle import PUBLISHED, SUPERSEDED, RuleLifecycle


class PublicationService:
    def __init__(self, session: AsyncSession, admin: AdminService | None = None):
        self.s = session
        self.admin = admin or AdminService(session)

    async def _jurisdiction_of(self, version) -> str | None:
        """The province a rule version applies to, or None for federal/global.

        FED is deliberately mapped to None: a federal rule applies everywhere,
        so narrowing on it would exclude every provincial result.
        """
        from app.database.models import Jurisdiction, TaxRule

        rule = await self.s.get(TaxRule, version.tax_rule_id)
        if rule is None or rule.jurisdiction_id is None:
            return None
        jurisdiction = await self.s.get(Jurisdiction, rule.jurisdiction_id)
        if jurisdiction is None or jurisdiction.code == "FED":
            return None
        return jurisdiction.code

    async def publish(
        self, publisher_id: uuid.UUID, version_id: uuid.UUID, *, reindex: bool = False
    ) -> TaxRuleVersion:
        await self.admin.require_permission(publisher_id, "tkms.publish")
        version = await self.s.get(TaxRuleVersion, version_id)
        if version is None:
            raise NotFound("Rule version not found")

        RuleLifecycle.assert_transition(version.status, PUBLISHED)

        approved_cr = await self.s.scalar(
            select(RuleChangeRequest).where(
                RuleChangeRequest.tax_rule_version_id == version_id,
                RuleChangeRequest.action == "publish",
                RuleChangeRequest.status == "approved",
            )
        )
        if approved_cr is None:
            raise Forbidden("Cannot publish without an approved change request (four-eyes)")

        report = await self._passed_validation_report(version)
        if report is None:
            raise Forbidden("Cannot publish without a passed validation report")

        # supersede the currently-published version for this rule + tax_year
        current = await self.s.scalar(
            select(TaxRuleVersion).where(
                TaxRuleVersion.tax_rule_id == version.tax_rule_id,
                TaxRuleVersion.tax_year == version.tax_year,
                TaxRuleVersion.status == PUBLISHED,
                TaxRuleVersion.id != version.id,
            )
        )
        if current is not None:
            RuleLifecycle.assert_transition(current.status, SUPERSEDED)
            current.status = SUPERSEDED
            current.superseded_by_version_id = version.id
            await self.s.flush()
            # Same transaction as the supersession, so the event exists if and
            # only if the supersession committed.
            await emit(
                self.s, FreshnessEvent.RULE_SUPERSEDED,
                tax_year=version.tax_year,
                jurisdiction=await self._jurisdiction_of(version),
                dedupe_key=f"rule_superseded:{current.id}",
            )

        version.status = PUBLISHED
        version.published_at = datetime.now(tz=UTC)
        version.validation_report_id = report.id
        self.s.add(RulePublication(
            tax_rule_version_id=version.id,
            change_request_id=approved_cr.id,
            published_by=publisher_id,
        ))
        # A newly published rule changes what every completed result for that
        # year was evaluated against. Emitted here, inside the publishing
        # transaction, so it cannot be lost or fire for a rollback.
        # Carrying the jurisdiction is what stops an Ontario publication from
        # staling every other province's results for that year. A rule with no
        # resolvable jurisdiction stays NULL, which fans out as it always did —
        # broad, but never silently narrower than the truth.
        await emit(
            self.s, FreshnessEvent.RULE_PUBLISHED,
            tax_year=version.tax_year,
            jurisdiction=await self._jurisdiction_of(version),
            dedupe_key=f"rule_published:{version.id}",
        )
        await self.s.flush()

        if reindex:
            await self._reindex(version.tax_year)
        return version

    async def _passed_validation_report(self, version: TaxRuleVersion) -> ValidationReport | None:
        """A passing report targeting this version, or its import job."""
        report = await self.s.scalar(
            select(ValidationReport)
            .where(
                ValidationReport.target_version_id == version.id,
                ValidationReport.status.in_(("passed", "warnings")),
            )
            .order_by(ValidationReport.created_at.desc())
        )
        if report is not None:
            return report
        if version.import_job_id is None:
            return None
        return await self.s.scalar(
            select(ValidationReport)
            .where(
                ValidationReport.import_job_id == version.import_job_id,
                ValidationReport.status.in_(("passed", "warnings")),
            )
            .order_by(ValidationReport.created_at.desc())
        )

    async def _reindex(self, tax_year: int) -> None:
        """Best-effort AI re-embedding; never blocks or fails a publish."""
        try:
            from app.services.ai.retrieval import KnowledgeIndex

            await KnowledgeIndex(self.s).reindex_published_rules(tax_year)
        except Exception:  # noqa: BLE001 — indexing is eventual, not authoritative
            pass
