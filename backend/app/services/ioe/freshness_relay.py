"""The freshness relay (§P6 closure, corrected).

The relay is a cross-tenant process, and the RLS refusal it hits when it tries
to read user-scoped rows is the boundary working, not an obstacle. So the relay
does not get elevated read access. It gets a keyhole: three privileged functions
that move rows in one queue table between four states, and nothing else.

The flow, per event:

  1. CLAIM a minimal event through the privileged function — identifiers, codes
     and scope keys only, never a financial value.
  2. Open an ORDINARY transaction.
  3. Set `app.user_id` from the claimed event, transaction-locally.
  4. Invoke the ordinary, RLS-protected `ScenarioFreshnessService` for that one
     tenant. Every row it touches is checked by the same policies that protect a
     logged-in user's request.
  5. Commit.
  6. ACKNOWLEDGE through the privileged completion function.

The staling itself is therefore never privileged. A bug in the relay can lose or
duplicate an invalidation; it cannot read or alter another tenant's data,
because at the moment any row is touched the session is scoped to exactly one
tenant.

A tax-year-scoped event (a rule publication) affects many tenants, so it is
fanned out into per-tenant child events first — still queue management, still
writing nothing but outbox rows — so that step 3 always has one tenant to name.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.privacy_session import freshness_unit_of_work
from app.database.session import unit_of_work
from app.services.ioe.domain.scenario import FreshnessStatus, StaleReason

log = get_logger("onyx.ioe.freshness_relay")

RELAY_VERSION = "1.0.0"
DEFAULT_BATCH_SIZE = 50
MAX_BATCH_SIZE = 200

ERROR_TENANT_APPLY_FAILED = "TENANT_APPLY_FAILED"
ERROR_UNSCOPED_EVENT = "UNSCOPED_EVENT"


def worker_identity() -> str:
    """A stable, per-process identity used as the claim owner."""
    return f"{os.uname().nodename}:{os.getpid()}"


@dataclass
class ClaimedEvent:
    """The minimal event body. Scope keys and codes; no financial values."""

    event_id: uuid.UUID
    claim_token: uuid.UUID
    stale_reason_code: str
    analysis_id: uuid.UUID | None
    tax_year: int | None
    # Optional QUALIFIER, not a rival scope. NULL means "not jurisdiction
    # scoped" and fans out as before; a value narrows to matching results.
    jurisdiction: str | None
    user_id: uuid.UUID | None

    @property
    def is_single_tenant(self) -> bool:
        return self.user_id is not None


@dataclass
class RelayReport:
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    fanned_out: int = 0
    scenarios_marked: int = 0
    runs_marked: int = 0
    errors: list[str] = field(default_factory=list)


class FreshnessRelay:
    """Claims outbox events and applies them under ordinary tenant context."""

    def __init__(self, worker_id: str | None = None):
        self.worker_id = worker_id or worker_identity()

    # -- step 1: claim, through the keyhole -----------------------------------
    async def claim(
        self, session: AsyncSession, *, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> list[ClaimedEvent]:
        rows = await session.execute(
            text(
                "SELECT out_event_id AS event_id, "
                "       out_claim_token AS claim_token, "
                "       out_stale_reason_code AS stale_reason_code, "
                "       out_analysis_id AS analysis_id, "
                "       out_tax_year AS tax_year, "
                "       out_jurisdiction AS jurisdiction, "
                "       out_user_id AS user_id "
                "FROM ioe.claim_freshness_events(:batch, :worker)"
            ),
            {"batch": min(batch_size, MAX_BATCH_SIZE), "worker": self.worker_id},
        )
        return [ClaimedEvent(**dict(r._mapping)) for r in rows]

    async def run_once(self, *, batch_size: int = DEFAULT_BATCH_SIZE) -> RelayReport:
        report = RelayReport()

        # The claim runs in its own short transaction so a slow tenant apply
        # does not hold the queue — and under the DEDICATED freshness runtime,
        # not the role that serves HTTP. That separation is PD-16's fix: the
        # capability to move queue rows across tenants must not be reachable
        # from the application identity.
        async with freshness_unit_of_work() as session:
            events = await self.claim(session, batch_size=batch_size)
        report.claimed = len(events)

        for event in events:
            if not event.is_single_tenant:
                report.fanned_out += await self._fan_out(event)
                # the parent is done once its children exist
                await self._acknowledge(event, None)
                report.completed += 1
                continue

            try:
                # steps 2-5: ordinary transaction, tenant context, ordinary
                # RLS-protected service, commit
                marked = await self._apply_for_tenant(event)
            except Exception:  # noqa: BLE001
                await self._acknowledge(event, ERROR_TENANT_APPLY_FAILED)
                report.failed += 1
                report.errors.append(ERROR_TENANT_APPLY_FAILED)
                continue

            report.scenarios_marked += marked
            await self._acknowledge(event, None)
            report.completed += 1

        return report

    async def drain(
        self, *, batch_size: int = DEFAULT_BATCH_SIZE, max_passes: int = 10
    ) -> RelayReport:
        """Run until the queue is idle, bounded.

        More than one pass is needed because fanning a scope-wide event out
        creates children that are themselves pending; bounded so a producer
        emitting faster than the relay drains cannot spin here forever.
        """
        total = RelayReport()
        for _ in range(max_passes):
            report = await self.run_once(batch_size=batch_size)
            total.claimed += report.claimed
            total.completed += report.completed
            total.failed += report.failed
            total.fanned_out += report.fanned_out
            total.scenarios_marked += report.scenarios_marked
            total.runs_marked += report.runs_marked
            total.errors.extend(report.errors)
            if report.claimed == 0:
                break
        return total

    # -- steps 2-5: ordinary tenant transaction -------------------------------
    async def _apply_for_tenant(self, event: ClaimedEvent) -> int:
        """Stale this ONE tenant's records through the ordinary service.

        `unit_of_work(user_id=...)` sets `app.user_id` transaction-locally, so
        every statement below is filtered by the same RLS policies that protect
        an authenticated request. Nothing here is privileged.
        """
        from app.services.ioe.scenario.freshness_service import (
            ScenarioFreshnessService,
        )

        async with unit_of_work(user_id=event.user_id, actor_type="user") as session:
            service = ScenarioFreshnessService(session, event.user_id)
            reason = _reason(event.stale_reason_code)
            if event.analysis_id is not None:
                marked = await service.invalidate_for_analysis(
                    event.analysis_id, reason
                )
            elif event.tax_year is not None:
                marked = await service.invalidate_for_tax_year(
                    event.tax_year, reason, jurisdiction=event.jurisdiction)
            else:
                marked = await service.invalidate_for_user(reason)
            await self._stale_runs_for_tenant(session, event)
            return marked

    @staticmethod
    async def _stale_runs_for_tenant(
        session: AsyncSession, event: ClaimedEvent
    ) -> int:
        """Optimization runs for the same tenant, under the same RLS session."""
        from sqlalchemy import update

        from app.database.models import OptimizationRun

        conditions = [
            OptimizationRun.user_id == event.user_id,
            OptimizationRun.workflow_status == "completed",
            OptimizationRun.freshness_status == "current",
        ]
        if event.analysis_id is not None:
            conditions.append(OptimizationRun.analysis_id == event.analysis_id)
        elif event.tax_year is not None:
            conditions.append(OptimizationRun.tax_year == event.tax_year)

        result = await session.execute(
            update(OptimizationRun).where(*conditions).values(
                freshness_status="stale",
                stale_reason_codes=[event.stale_reason_code],
            )
        )
        # `AsyncSession.execute` is typed as returning `Result`, but a DML
        # statement always yields a `CursorResult`, which is where
        # `rowcount` lives. The cast states that rather than hiding it.
        return cast("CursorResult[Any]", result).rowcount or 0

    # -- fan-out and step 6 ---------------------------------------------------
    async def _fan_out(self, event: ClaimedEvent) -> int:
        # Privileged, like claim and acknowledge: it moves queue rows the
        # calling tenant does not own.
        async with freshness_unit_of_work() as session:
            count = await session.scalar(
                text("SELECT ioe.fan_out_freshness_event(:id, :worker)"),
                {"id": event.event_id, "worker": self.worker_id},
            )
            return int(count or 0)

    async def _acknowledge(self, event: ClaimedEvent, error_code: str | None) -> bool:
        """Step 6. Idempotent: a redelivered acknowledgement is a no-op."""
        async with freshness_unit_of_work() as session:
            if error_code is None:
                ok = await session.scalar(
                    text("SELECT ioe.complete_freshness_event(:id, :worker)"),
                    {"id": event.event_id, "worker": self.worker_id},
                )
            else:
                ok = await session.scalar(
                    text("SELECT ioe.fail_freshness_event(:id, :worker, :err)"),
                    {"id": event.event_id, "worker": self.worker_id,
                     "err": error_code},
                )
            return bool(ok)


def _reason(code: str) -> StaleReason:
    return StaleReason(code)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "ERROR_TENANT_APPLY_FAILED",
    "MAX_BATCH_SIZE",
    "RELAY_VERSION",
    "ClaimedEvent",
    "FreshnessRelay",
    "FreshnessStatus",
    "RelayReport",
    "worker_identity",
]
