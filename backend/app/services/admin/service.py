"""Administration service: admin auth, KB ingestion, and four-eyes publishing.

Governance invariant: a tax_rule_version cannot become `published` without an
APPROVED change request whose reviewer is a DIFFERENT admin than the submitter.
This is enforced here AND by the DB CHECK (reviewed_by <> submitted_by) plus the
partial-unique index (one published version per rule per tax_year).
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, Forbidden, NotFound, Unauthorized, ValidationError
from app.core.security.password import hash_password, verify_password
from app.database.models import (
    AdminUser,
    AdminUserRole,
    Jurisdiction,
    Permission,
    Role,
    RolePermission,
    RuleChangeRequest,
    RulePublication,
    TaxRule,
    TaxRuleVersion,
)
from app.services.data_ingestion.pipeline import extract, validate_and_transform


class AdminService:
    def __init__(self, session: AsyncSession):
        self.s = session

    # ---- provisioning + auth ----
    async def create_admin(self, email: str, password: str, role_codes: list[str]) -> AdminUser:
        admin = AdminUser(email=email, password_hash=hash_password(password), status="active")
        self.s.add(admin)
        await self.s.flush()
        for code in role_codes:
            role = await self.s.scalar(select(Role).where(Role.code == code))
            if role:
                self.s.add(AdminUserRole(admin_user_id=admin.id, role_id=role.id))
        await self.s.flush()
        return admin

    async def authenticate(self, email: str, password: str) -> AdminUser:
        admin = await self.s.scalar(
            select(AdminUser).where(AdminUser.email == email, AdminUser.deleted_at.is_(None))
        )
        if not admin or admin.status != "active" or not verify_password(password, admin.password_hash):
            raise Unauthorized("Invalid admin credentials")
        return admin

    async def has_permission(self, admin_id: uuid.UUID, permission_code: str) -> bool:
        rows = await self.s.scalars(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(AdminUserRole, AdminUserRole.role_id == RolePermission.role_id)
            .where(AdminUserRole.admin_user_id == admin_id, Permission.code == permission_code)
        )
        return rows.first() is not None

    async def require_permission(self, admin_id: uuid.UUID, permission_code: str) -> None:
        if not await self.has_permission(admin_id, permission_code):
            raise Forbidden(f"Missing permission '{permission_code}'")

    # ---- ingestion: raw dataset -> DRAFT versions + pending change requests ----
    async def ingest(self, admin_id: uuid.UUID, payload: str, fmt: str) -> dict:
        await self.require_permission(admin_id, "rule.author")
        rows = extract(payload, fmt)
        rules, errors = validate_and_transform(rows)
        if not rules:
            raise ValidationError(f"No valid rules in dataset. Errors: {errors}")

        created = []
        for r in rules:
            jur = await self.s.scalar(select(Jurisdiction).where(Jurisdiction.code == r.jurisdiction))
            if not jur:
                errors.append(f"{r.code}: unknown jurisdiction '{r.jurisdiction}'")
                continue
            rule = await self.s.scalar(select(TaxRule).where(TaxRule.code == r.code))
            if not rule:
                rule = TaxRule(
                    code=r.code, name=r.name, category=r.category,
                    jurisdiction_id=jur.id,
                    province_code=None if r.jurisdiction == "FED" else r.jurisdiction,
                )
                self.s.add(rule)
                await self.s.flush()
            try:
                eff = date.fromisoformat(r.effective_date) if r.effective_date else date(r.tax_year, 1, 1)
            except ValueError:
                eff = date(r.tax_year, 1, 1)
            version = TaxRuleVersion(
                tax_rule_id=rule.id, tax_year=r.tax_year,
                effective_date=eff, status="draft",
                description=r.description or r.name,
                max_amount=r.max_amount, reduction_rate=r.reduction_rate,
                source_url=r.source_url,
            )
            self.s.add(version)
            await self.s.flush()
            cr = RuleChangeRequest(
                tax_rule_version_id=version.id, action="publish", status="pending",
                submitted_by=admin_id, submitted_at=datetime.now(tz=UTC),
                notes=f"Ingested via {fmt.upper()}",
            )
            self.s.add(cr)
            await self.s.flush()
            created.append({"rule_code": r.code, "version_id": str(version.id),
                            "change_request_id": str(cr.id), "warnings": r.warnings})
        return {"created": created, "errors": errors}

    # ---- four-eyes review ----
    async def approve(self, reviewer_id: uuid.UUID, change_request_id: uuid.UUID) -> RuleChangeRequest:
        await self.require_permission(reviewer_id, "rule.approve")
        cr = await self.s.get(RuleChangeRequest, change_request_id)
        if not cr:
            raise NotFound("Change request not found")
        if cr.status != "pending":
            raise Conflict(f"Change request is '{cr.status}', not pending")
        if cr.submitted_by == reviewer_id:
            raise Forbidden("Four-eyes: the approver must be a different admin than the submitter")
        cr.status = "approved"
        cr.reviewed_by = reviewer_id
        cr.decided_at = datetime.now(tz=UTC)
        await self.s.flush()
        return cr

    async def publish(self, admin_id: uuid.UUID, version_id: uuid.UUID) -> TaxRuleVersion:
        await self.require_permission(admin_id, "rule.publish")
        version = await self.s.get(TaxRuleVersion, version_id)
        if not version:
            raise NotFound("Rule version not found")
        approved = await self.s.scalar(
            select(RuleChangeRequest).where(
                RuleChangeRequest.tax_rule_version_id == version_id,
                RuleChangeRequest.action == "publish",
                RuleChangeRequest.status == "approved",
            )
        )
        if not approved:
            raise Forbidden("Cannot publish without an approved change request (four-eyes)")

        # supersede any currently-published version for the same rule + tax_year
        current = await self.s.scalar(
            select(TaxRuleVersion).where(
                TaxRuleVersion.tax_rule_id == version.tax_rule_id,
                TaxRuleVersion.tax_year == version.tax_year,
                TaxRuleVersion.status == "published",
                TaxRuleVersion.id != version.id,
            )
        )
        if current:
            current.status = "superseded"
            await self.s.flush()

        version.status = "published"
        version.published_at = datetime.now(tz=UTC)
        self.s.add(RulePublication(
            tax_rule_version_id=version.id, change_request_id=approved.id, published_by=admin_id
        ))
        await self.s.flush()
        # (production) invalidate the Redis rule cache + re-embed for AI retrieval here.
        return version
