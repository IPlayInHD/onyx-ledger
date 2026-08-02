"""TKMS admin portal API (/api/v1/tkms) — import, monitor, govern, publish, trace.

All endpoints require an admin JWT; the specific TKMS permission is enforced
inside each service (require_permission), and every mutation is audited. The
import path runs the governed pipeline (parse→extract→promote→validate→compare)
so a reviewer immediately sees staged rules, the validation report, and the diff;
in production the same stages run as per-queue Celery tasks.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_admin_id, db_admin
from app.database.models import (
    ChangeItem,
    ChangeReport,
    DeadLetter,
    ImportJob,
    TaxRule,
    TaxRuleVersion,
    ValidationFinding,
    ValidationReport,
)
from app.database.models import (
    ExtractedRule as ExtractedRuleRow,
)
from app.services.admin.service import AdminService
from app.services.tkms.governance.service import GovernanceService
from app.services.tkms.ingestion.service import ImportService
from app.services.tkms.parsers import build_default_registry
from app.services.tkms.pipeline import run_ingestion_pipeline
from app.services.tkms.publication.service import PublicationService
from app.services.tkms.rollback.service import RollbackService
from app.services.tkms.traceability.service import TraceabilityService

router = APIRouter(prefix="/tkms", tags=["tkms"])


# ---- request bodies ---------------------------------------------------------
class ImportBody(BaseModel):
    source_org: str
    format: str
    payload: str
    tax_year: int | None = None
    jurisdiction_code: str | None = None
    province_code: str | None = None
    source_url: str | None = None
    document_version: str | None = None
    parser_name: str | None = None
    parser_version: str | None = None


class RejectBody(BaseModel):
    reason: str
    discard: bool = False


class RollbackBody(BaseModel):
    to_version_id: uuid.UUID
    reason: str


# ---- import + monitor -------------------------------------------------------
@router.post("/imports", status_code=status.HTTP_201_CREATED)
async def create_import(
    body: ImportBody,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    admin = AdminService(session)
    await admin.require_permission(admin_id, "tkms.import")
    imp = ImportService(session)
    job = await imp.create_job(
        source_org=body.source_org, fmt=body.format, operator_admin_id=admin_id,
        jurisdiction_code=body.jurisdiction_code, province_code=body.province_code,
        tax_year=body.tax_year, source_url=body.source_url,
        document_version=body.document_version, parser_name=body.parser_name,
        parser_version=body.parser_version,
    )
    await imp.store_raw(job.id, body.payload.encode("utf-8"))
    summary = await run_ingestion_pipeline(session, job.id)
    return {"job_id": str(job.id), **summary}


@router.post("/imports/{job_id}/reparse")
async def reparse_import(
    job_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
    parser_name: str | None = None,
    parser_version: str | None = None,
) -> dict:
    admin = AdminService(session)
    await admin.require_permission(admin_id, "tkms.import")
    job = await session.get(ImportJob, job_id)
    if job is None:
        from app.core.exceptions import NotFound
        raise NotFound("Import job not found")
    if parser_name:
        job.parser_name = parser_name
        job.parser_version = parser_version
    # drop the prior parse so the pipeline re-runs from parse
    from app.database.models import ParseResult
    for pr in await session.scalars(select(ParseResult).where(ParseResult.import_job_id == job_id)):
        pr.status = "superseded"
    await session.flush()
    summary = await run_ingestion_pipeline(session, job_id)
    return {"job_id": str(job_id), **summary}


@router.get("/imports")
async def list_imports(
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
    limit: int = 50,
) -> list[dict]:
    await AdminService(session).require_permission(admin_id, "tkms.read")
    jobs = await session.scalars(
        select(ImportJob).order_by(desc(ImportJob.created_at)).limit(min(limit, 200))
    )
    return [_job_summary(j) for j in jobs]


@router.get("/imports/{job_id}")
async def get_import(
    job_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    await AdminService(session).require_permission(admin_id, "tkms.read")
    job = await session.get(ImportJob, job_id)
    if job is None:
        from app.core.exceptions import NotFound
        raise NotFound("Import job not found")
    n_rules = await session.scalar(
        select(func.count(ExtractedRuleRow.id)).where(ExtractedRuleRow.import_job_id == job_id)
    )
    report = await _latest_report(session, job_id)
    return {
        **_job_summary(job),
        "extracted_rule_count": n_rules or 0,
        "validation": {"report_id": str(report.id), "status": report.status} if report else None,
    }


@router.get("/imports/{job_id}/extracted")
async def get_extracted(
    job_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> list[dict]:
    await AdminService(session).require_permission(admin_id, "tkms.read")
    rows = await session.scalars(
        select(ExtractedRuleRow)
        .where(ExtractedRuleRow.import_job_id == job_id)
        .order_by(ExtractedRuleRow.ordinal)
    )
    return [
        {"id": str(r.id), "ordinal": r.ordinal, "payload": r.payload,
         "promoted_version_id": str(r.promoted_version_id) if r.promoted_version_id else None}
        for r in rows
    ]


@router.get("/imports/{job_id}/validation")
async def get_validation(
    job_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    await AdminService(session).require_permission(admin_id, "tkms.read")
    report = await _latest_report(session, job_id)
    if report is None:
        return {"report_id": None, "status": None, "findings": []}
    findings = await session.scalars(
        select(ValidationFinding).where(ValidationFinding.report_id == report.id)
    )
    return {
        "report_id": str(report.id), "status": report.status,
        "findings": [
            {"severity": f.severity, "code": f.code, "message": f.message, "rule_ref": f.rule_ref}
            for f in findings
        ],
    }


@router.get("/versions/{version_id}/compare")
async def get_compare(
    version_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    await AdminService(session).require_permission(admin_id, "tkms.read")
    report = await session.scalar(
        select(ChangeReport)
        .where(ChangeReport.draft_version_id == version_id)
        .order_by(desc(ChangeReport.created_at))
    )
    if report is None:
        return {"report_id": None, "summary": None, "items": []}
    items = await session.scalars(
        select(ChangeItem).where(ChangeItem.change_report_id == report.id)
    )
    return {
        "report_id": str(report.id), "summary": report.summary,
        "baseline_version_id": str(report.baseline_version_id) if report.baseline_version_id else None,
        "items": [
            {"field": i.field, "change_type": i.change_type,
             "old_value": i.old_value, "new_value": i.new_value}
            for i in items
        ],
    }


# ---- governance -------------------------------------------------------------
@router.post("/versions/{version_id}/submit")
async def submit_version(
    version_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    cr = await GovernanceService(session).submit_for_review(admin_id, version_id)
    return {"version_id": str(version_id), "change_request_id": str(cr.id), "status": "pending_review"}


@router.post("/versions/{version_id}/approve")
async def approve_version(
    version_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    cr = await GovernanceService(session).approve(admin_id, version_id)
    return {"version_id": str(version_id), "change_request_id": str(cr.id), "status": "approved"}


@router.post("/versions/{version_id}/reject")
async def reject_version(
    version_id: uuid.UUID,
    body: RejectBody,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    cr = await GovernanceService(session).reject(
        admin_id, version_id, reason=body.reason, discard=body.discard
    )
    return {"version_id": str(version_id), "change_request_id": str(cr.id), "status": cr.status}


@router.post("/versions/{version_id}/publish")
async def publish_version(
    version_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    v = await PublicationService(session).publish(admin_id, version_id, reindex=True)
    return {"version_id": str(v.id), "status": v.status,
            "published_at": v.published_at.isoformat() if v.published_at else None}


# ---- rollback ---------------------------------------------------------------
@router.post("/rules/{rule_id}/rollback", status_code=status.HTTP_201_CREATED)
async def request_rollback(
    rule_id: uuid.UUID,
    body: RollbackBody,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    rec = await RollbackService(session).request(admin_id, body.to_version_id, reason=body.reason)
    return {"rollback_id": str(rec.id), "status": rec.status}


@router.post("/rollbacks/{rollback_id}/approve")
async def approve_rollback(
    rollback_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    rec = await RollbackService(session).approve(admin_id, rollback_id)
    return {"rollback_id": str(rec.id), "status": rec.status,
            "to_version_id": str(rec.to_version_id)}


# ---- traceability + search + ops -------------------------------------------
@router.get("/versions/{version_id}/trace")
async def trace_version(
    version_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    await AdminService(session).require_permission(admin_id, "tkms.read")
    return await TraceabilityService(session).trace(version_id)


@router.get("/rules/search")
async def search_rules(
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
    tax_year: int | None = None,
    status_: str | None = None,
    q: str | None = None,
    limit: int = 50,
) -> list[dict]:
    await AdminService(session).require_permission(admin_id, "tkms.read")
    stmt = select(TaxRuleVersion, TaxRule).join(TaxRule, TaxRule.id == TaxRuleVersion.tax_rule_id)
    if tax_year is not None:
        stmt = stmt.where(TaxRuleVersion.tax_year == tax_year)
    if status_:
        stmt = stmt.where(TaxRuleVersion.status == status_)
    if q:
        stmt = stmt.where(TaxRule.code.ilike(f"%{q}%"))
    stmt = stmt.order_by(desc(TaxRuleVersion.created_at)).limit(min(limit, 200))
    rows = await session.execute(stmt)
    return [
        {"version_id": str(v.id), "rule_code": r.code, "name": r.name,
         "tax_year": v.tax_year, "status": v.status}
        for v, r in rows.all()
    ]


@router.get("/parsers")
async def list_parsers(
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> list[dict]:
    await AdminService(session).require_permission(admin_id, "tkms.read")
    return build_default_registry().available()


@router.get("/dead-letters")
async def list_dead_letters(
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> list[dict]:
    await AdminService(session).require_permission(admin_id, "tkms.import")
    rows = await session.scalars(
        select(DeadLetter).where(DeadLetter.resolved_at.is_(None)).order_by(desc(DeadLetter.created_at))
    )
    return [
        {"id": str(d.id), "task_name": d.task_name, "queue": d.queue,
         "error": d.error, "attempts": d.attempts, "payload": d.payload}
        for d in rows
    ]


@router.get("/stats")
async def stats(
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    await AdminService(session).require_permission(admin_id, "tkms.read")
    jobs_by_status = dict(
        (await session.execute(
            select(ImportJob.status, func.count(ImportJob.id)).group_by(ImportJob.status)
        )).all()
    )
    published = await session.scalar(
        select(func.count(TaxRuleVersion.id)).where(TaxRuleVersion.status == "published")
    )
    open_dlq = await session.scalar(
        select(func.count(DeadLetter.id)).where(DeadLetter.resolved_at.is_(None))
    )
    return {
        "jobs_by_status": {k: int(v) for k, v in jobs_by_status.items()},
        "published_versions": int(published or 0),
        "open_dead_letters": int(open_dlq or 0),
    }


# ---- helpers ----------------------------------------------------------------
def _job_summary(j: ImportJob) -> dict:
    return {
        "id": str(j.id), "source_org": j.source_org, "format": j.format,
        "tax_year": j.tax_year, "status": j.status,
        "validation_status": j.validation_status, "approval_status": j.approval_status,
        "created_at": j.created_at.isoformat() if j.created_at else None,
    }


async def _latest_report(session: AsyncSession, job_id: uuid.UUID) -> ValidationReport | None:
    return await session.scalar(
        select(ValidationReport)
        .where(ValidationReport.import_job_id == job_id)
        .order_by(desc(ValidationReport.created_at))
    )
