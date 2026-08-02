"""TraceabilityService — resolve a rule version back to its origin.

Because provenance is stored as foreign keys, the full chain (published version →
import job → parser → raw document → staged extracted rule → validation report →
change report → publication) is a set of joins, not a reconstruction. This is the
audit answer to "why does the engine believe this rule, and where did it come
from?".
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound
from app.database.models import (
    ChangeItem,
    ChangeReport,
    ImportJob,
    ParseResult,
    RawDocument,
    RulePublication,
    TaxRule,
    TaxRuleVersion,
    ValidationFinding,
    ValidationReport,
)
from app.database.models import (
    ExtractedRule as ExtractedRuleRow,
)


class TraceabilityService:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def trace(self, version_id: uuid.UUID) -> dict:
        version = await self.s.get(TaxRuleVersion, version_id)
        if version is None:
            raise NotFound("Rule version not found")
        rule = await self.s.get(TaxRule, version.tax_rule_id)

        trace: dict = {
            "version": {
                "id": str(version.id),
                "rule_code": rule.code if rule else None,
                "rule_name": rule.name if rule else None,
                "category": rule.category if rule else None,
                "subcategory": rule.subcategory if rule else None,
                "tax_year": version.tax_year,
                "status": version.status,
                "effective_date": version.effective_date.isoformat()
                if version.effective_date else None,
                "published_at": version.published_at.isoformat()
                if version.published_at else None,
                "parser_version": version.parser_version,
                "parser_confidence": str(version.parser_confidence)
                if version.parser_confidence is not None else None,
            },
            "import_job": None,
            "raw_document": None,
            "parser": None,
            "extracted_rule": None,
            "validation": None,
            "change_report": None,
            "publication": None,
        }

        if version.import_job_id is not None:
            job = await self.s.get(ImportJob, version.import_job_id)
            if job is not None:
                trace["import_job"] = {
                    "id": str(job.id),
                    "source_org": job.source_org,
                    "source_url": job.source_url,
                    "checksum": job.checksum,
                    "format": job.format,
                    "document_version": job.document_version,
                    "operator_admin_id": str(job.operator_admin_id)
                    if job.operator_admin_id else None,
                    "status": job.status,
                }
                raw = await self.s.scalar(
                    select(RawDocument).where(RawDocument.import_job_id == job.id)
                )
                if raw is not None:
                    trace["raw_document"] = {
                        "storage_bucket": raw.storage_bucket,
                        "object_key": raw.object_key,
                        "content_hash": raw.content_hash,
                        "byte_size": raw.byte_size,
                    }
                pr = await self.s.scalar(
                    select(ParseResult)
                    .where(ParseResult.import_job_id == job.id, ParseResult.status == "succeeded")
                    .order_by(ParseResult.created_at.desc())
                )
                if pr is not None:
                    trace["parser"] = {
                        "name": pr.parser_name,
                        "version": pr.parser_version,
                        "confidence": str(pr.confidence) if pr.confidence is not None else None,
                        "rule_count": pr.rule_count,
                    }

        staged = await self.s.scalar(
            select(ExtractedRuleRow).where(ExtractedRuleRow.promoted_version_id == version.id)
        )
        if staged is not None:
            trace["extracted_rule"] = {
                "id": str(staged.id),
                "ordinal": staged.ordinal,
                "payload": staged.payload,
                "confidence": str(staged.confidence) if staged.confidence is not None else None,
            }

        report = None
        if version.validation_report_id is not None:
            report = await self.s.get(ValidationReport, version.validation_report_id)
        if report is None and version.import_job_id is not None:
            report = await self.s.scalar(
                select(ValidationReport)
                .where(ValidationReport.import_job_id == version.import_job_id)
                .order_by(ValidationReport.created_at.desc())
            )
        if report is not None:
            findings = list(await self.s.scalars(
                select(ValidationFinding).where(ValidationFinding.report_id == report.id)
            ))
            trace["validation"] = {
                "report_id": str(report.id),
                "status": report.status,
                "findings": [
                    {"severity": f.severity, "code": f.code, "message": f.message,
                     "rule_ref": f.rule_ref}
                    for f in findings
                ],
            }

        change = await self.s.scalar(
            select(ChangeReport)
            .where(ChangeReport.draft_version_id == version.id)
            .order_by(ChangeReport.created_at.desc())
        )
        if change is not None:
            items = list(await self.s.scalars(
                select(ChangeItem).where(ChangeItem.change_report_id == change.id)
            ))
            trace["change_report"] = {
                "id": str(change.id),
                "summary": change.summary,
                "baseline_version_id": str(change.baseline_version_id)
                if change.baseline_version_id else None,
                "items": [
                    {"field": i.field, "change_type": i.change_type,
                     "old_value": i.old_value, "new_value": i.new_value}
                    for i in items
                ],
            }

        pub = await self.s.scalar(
            select(RulePublication)
            .where(RulePublication.tax_rule_version_id == version.id)
            .order_by(RulePublication.published_at.desc())
        )
        if pub is not None:
            trace["publication"] = {
                "published_by": str(pub.published_by) if pub.published_by else None,
                "published_at": pub.published_at.isoformat() if pub.published_at else None,
                "change_request_id": str(pub.change_request_id) if pub.change_request_id else None,
            }

        return trace
