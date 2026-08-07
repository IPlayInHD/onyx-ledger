"""GovernanceService — the four-eyes lifecycle for draft rule versions.

Submit → review → approve/reject, enforced two ways: the RuleLifecycle state
machine guards legal moves, and `reviewer != submitter` is enforced here AND by
the DB CHECK on admin.rule_change_request. Every decision flows through the
audited API and lands in the append-only audit log (tax_rule_version is tracked).
Reuses the existing admin RBAC (AdminService) and the rule_change_request table.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, Forbidden, NotFound
from app.database.models import RuleChangeRequest, TaxRuleVersion
from app.services.admin.service import AdminService
from app.services.tkms.domain.lifecycle import (
    APPROVED,
    ARCHIVED,
    DRAFT,
    PENDING_REVIEW,
    RuleLifecycle,
)


class GovernanceService:
    def __init__(self, session: AsyncSession, admin: AdminService | None = None):
        self.s = session
        self.admin = admin or AdminService(session)

    async def submit_for_review(
        self, submitter_id: uuid.UUID, version_id: uuid.UUID, *, notes: str | None = None
    ) -> RuleChangeRequest:
        await self.admin.require_permission(submitter_id, "tkms.import")
        version = await self._get_version(version_id)
        RuleLifecycle.assert_transition(version.status, PENDING_REVIEW)

        cr = await self._open_change_request(version_id)
        if cr is None:
            cr = RuleChangeRequest(tax_rule_version_id=version_id, action="publish")
            self.s.add(cr)
        cr.status = "pending"
        cr.submitted_by = submitter_id
        cr.submitted_at = datetime.now(tz=UTC)
        cr.reviewed_by = None
        cr.decided_at = None
        if notes:
            cr.notes = notes
        version.status = PENDING_REVIEW
        await self.s.flush()
        return cr

    async def approve(
        self, reviewer_id: uuid.UUID, version_id: uuid.UUID
    ) -> RuleChangeRequest:
        await self.admin.require_permission(reviewer_id, "tkms.approve")
        version = await self._get_version(version_id)
        cr = await self._pending_change_request(version_id)
        if cr is None:
            raise Conflict("No pending change request to approve for this version")
        if cr.submitted_by == reviewer_id:
            raise Forbidden("Four-eyes: the approver must differ from the submitter")
        RuleLifecycle.assert_transition(version.status, APPROVED)

        cr.status = "approved"
        cr.reviewed_by = reviewer_id
        cr.decided_at = datetime.now(tz=UTC)
        version.status = APPROVED
        await self.s.flush()
        return cr

    async def reject(
        self,
        reviewer_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        reason: str,
        discard: bool = False,
    ) -> RuleChangeRequest:
        await self.admin.require_permission(reviewer_id, "tkms.approve")
        version = await self._get_version(version_id)
        cr = await self._pending_change_request(version_id)
        if cr is None:
            raise Conflict("No pending change request to reject for this version")
        if cr.submitted_by == reviewer_id:
            raise Forbidden("Four-eyes: the reviewer must differ from the submitter")

        target = ARCHIVED if discard else DRAFT
        RuleLifecycle.assert_transition(version.status, target)
        cr.status = "rejected"
        cr.reviewed_by = reviewer_id
        cr.decided_at = datetime.now(tz=UTC)
        cr.notes = reason
        version.status = target
        await self.s.flush()
        return cr

    # ---- helpers -------------------------------------------------------------
    async def _get_version(self, version_id: uuid.UUID) -> TaxRuleVersion:
        version = await self.s.get(TaxRuleVersion, version_id)
        if version is None:
            raise NotFound("Rule version not found")
        return version

    async def _open_change_request(self, version_id: uuid.UUID) -> RuleChangeRequest | None:
        # `AsyncSession.scalar` is typed `-> Any`; bound so the declared row type
        # is what callers actually see.
        request: RuleChangeRequest | None = await self.s.scalar(
            select(RuleChangeRequest)
            .where(
                RuleChangeRequest.tax_rule_version_id == version_id,
                RuleChangeRequest.status.in_(("draft", "pending")),
            )
            .order_by(RuleChangeRequest.created_at.desc())
        )
        return request

    async def _pending_change_request(self, version_id: uuid.UUID) -> RuleChangeRequest | None:
        pending: RuleChangeRequest | None = await self.s.scalar(
            select(RuleChangeRequest)
            .where(
                RuleChangeRequest.tax_rule_version_id == version_id,
                RuleChangeRequest.status == "pending",
            )
            .order_by(RuleChangeRequest.created_at.desc())
        )
        return pending
