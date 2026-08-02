"""RollbackService — re-publish a prior rule version under four-eyes.

Legislative rollback (the primary feature): a first admin requests restoring a
prior (superseded) version; a second, different admin approves; the service then
supersedes the current published version and re-publishes the target — atomically
and fully audited. Because versions are immutable, historical user analyses are
unaffected (they reference the exact version used at calculation time).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, Forbidden, NotFound, ValidationError
from app.database.models import (
    RollbackRecord,
    RulePublication,
    TaxRuleVersion,
)
from app.services.admin.service import AdminService
from app.services.tkms.domain.lifecycle import PUBLISHED, SUPERSEDED, RuleLifecycle


class RollbackService:
    def __init__(self, session: AsyncSession, admin: AdminService | None = None):
        self.s = session
        self.admin = admin or AdminService(session)

    async def request(
        self, admin_id: uuid.UUID, to_version_id: uuid.UUID, *, reason: str
    ) -> RollbackRecord:
        await self.admin.require_permission(admin_id, "tkms.rollback")
        if not reason or not reason.strip():
            raise ValidationError("A rollback reason is required")
        target = await self.s.get(TaxRuleVersion, to_version_id)
        if target is None:
            raise NotFound("Target version not found")
        if target.status != SUPERSEDED:
            raise Conflict(
                f"Rollback target must be a superseded version (is '{target.status}')"
            )

        current = await self._current_published(target)
        record = RollbackRecord(
            tax_rule_id=target.tax_rule_id,
            from_version_id=current.id if current else None,
            to_version_id=target.id,
            reason=reason.strip(),
            status="pending",
            performed_by=admin_id,
            requested_at=datetime.now(tz=UTC),
        )
        self.s.add(record)
        await self.s.flush()
        return record

    async def approve(
        self, approver_id: uuid.UUID, rollback_id: uuid.UUID
    ) -> RollbackRecord:
        await self.admin.require_permission(approver_id, "tkms.rollback")
        record = await self.s.get(RollbackRecord, rollback_id)
        if record is None:
            raise NotFound("Rollback record not found")
        if record.status != "pending":
            raise Conflict(f"Rollback is '{record.status}', not pending")
        if record.performed_by == approver_id:
            raise Forbidden("Four-eyes: the approver must differ from the requester")

        target = await self.s.get(TaxRuleVersion, record.to_version_id)
        if target is None:
            raise NotFound("Target version not found")

        # supersede the current published version for this rule + year (recomputed
        # at execution time to avoid acting on a stale snapshot)
        current = await self._current_published(target)
        if current is not None and current.id != target.id:
            RuleLifecycle.assert_transition(current.status, SUPERSEDED)
            current.status = SUPERSEDED
            current.superseded_by_version_id = target.id
            await self.s.flush()

        RuleLifecycle.assert_transition(target.status, PUBLISHED)
        target.status = PUBLISHED
        target.published_at = datetime.now(tz=UTC)
        self.s.add(RulePublication(
            tax_rule_version_id=target.id,
            change_request_id=None,
            published_by=approver_id,
        ))

        record.status = "executed"
        record.approved_by = approver_id
        record.from_version_id = current.id if current else None
        record.decided_at = datetime.now(tz=UTC)
        await self.s.flush()
        return record

    async def reject(
        self, approver_id: uuid.UUID, rollback_id: uuid.UUID, *, reason: str | None = None
    ) -> RollbackRecord:
        await self.admin.require_permission(approver_id, "tkms.rollback")
        record = await self.s.get(RollbackRecord, rollback_id)
        if record is None:
            raise NotFound("Rollback record not found")
        if record.status != "pending":
            raise Conflict(f"Rollback is '{record.status}', not pending")
        if record.performed_by == approver_id:
            raise Forbidden("Four-eyes: the reviewer must differ from the requester")
        record.status = "rejected"
        record.approved_by = approver_id
        record.decided_at = datetime.now(tz=UTC)
        if reason:
            record.reason = f"{record.reason} | rejected: {reason}"
        await self.s.flush()
        return record

    async def _current_published(self, target: TaxRuleVersion) -> TaxRuleVersion | None:
        return await self.s.scalar(
            select(TaxRuleVersion).where(
                TaxRuleVersion.tax_rule_id == target.tax_rule_id,
                TaxRuleVersion.tax_year == target.tax_year,
                TaxRuleVersion.status == PUBLISHED,
            )
        )
