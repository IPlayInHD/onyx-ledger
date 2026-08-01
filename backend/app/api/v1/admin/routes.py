from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, current_admin_id, db_admin, db_anon
from app.core.security.jwt import create_admin_token
from app.database.models import RuleChangeRequest, TaxRule, TaxRuleVersion
from app.services.admin.service import AdminService

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminLogin(BaseModel):
    email: str
    password: str


class IngestionJob(BaseModel):
    format: str          # 'json' | 'csv' | 'xml'
    payload: str


@router.post("/auth/login")
async def admin_login(
    body: AdminLogin, request: Request, session: AsyncSession = Depends(db_anon)
) -> dict:
    admin = await AdminService(session).authenticate(body.email, body.password)
    _ = client_ip(request)
    return {"access_token": create_admin_token(admin.id), "token_type": "bearer"}


@router.post("/ingestion/jobs", status_code=status.HTTP_201_CREATED)
async def create_ingestion_job(
    body: IngestionJob,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    """Raw dataset -> Extract -> Validate -> Transform -> DRAFT rule versions +
    a pending four-eyes change request per rule."""
    return await AdminService(session).ingest(admin_id, body.payload, body.format)


@router.post("/change-requests/{change_request_id}/approve")
async def approve_change_request(
    change_request_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    cr = await AdminService(session).approve(admin_id, change_request_id)
    return {"id": str(cr.id), "status": cr.status, "reviewed_by": str(cr.reviewed_by)}


@router.post("/rules/{version_id}/publish")
async def publish_rule(
    version_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> dict:
    v = await AdminService(session).publish(admin_id, version_id)
    return {"version_id": str(v.id), "status": v.status,
            "published_at": v.published_at.isoformat() if v.published_at else None}


@router.get("/rules")
async def list_rule_versions(
    tax_year: int,
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> list[dict]:
    result = await session.execute(
        select(TaxRule.code, TaxRuleVersion.id, TaxRuleVersion.tax_year, TaxRuleVersion.status)
        .join(TaxRuleVersion, TaxRuleVersion.tax_rule_id == TaxRule.id)
        .where(TaxRuleVersion.tax_year == tax_year)
    )
    return [{"code": r.code, "version_id": str(r.id), "tax_year": r.tax_year, "status": r.status}
            for r in result]


@router.get("/change-requests")
async def list_change_requests(
    admin_id: uuid.UUID = Depends(current_admin_id),
    session: AsyncSession = Depends(db_admin),
) -> list[dict]:
    rows = await session.scalars(
        select(RuleChangeRequest).where(RuleChangeRequest.status == "pending")
    )
    return [{"id": str(c.id), "action": c.action, "status": c.status,
             "version_id": str(c.tax_rule_version_id)} for c in rows]
