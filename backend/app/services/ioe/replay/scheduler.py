"""Bounded verification scheduling — the same keyhole shape as the freshness relay.

A scheduler is cross-tenant by nature, and the RLS refusal it would hit reading
user rows is the boundary working. So it does not get elevated read access. It
gets `ioe.claim_integrity_targets`, which returns identifiers and an owner id
and nothing else — no hashes, no result columns, no financial values — and the
replay then runs under ORDINARY tenant context with `app.user_id` set from the
claimed row.

The privileged function therefore never calculates, never touches a sealed
result, and never reads a financial value. Every row the verification reads or
writes is checked by the same policies that protect a logged-in user's request.

Scheduling policy (`integrity_check_policy_version`):
  * oldest-never-checked-first, so a result that has never been verified is
    always ahead of one verified last week;
  * bounded batch, capped in SQL as well as here;
  * a retry limit and a per-entity timeout, so one pathological entity cannot
    consume the window;
  * verification is never triggered by an ordinary read. Replaying on every
    display would put an engine run behind a page load.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text

from app.core.logging import get_logger
from app.database.session import unit_of_work
from app.services.ioe.domain.integrity import (
    INTEGRITY_CHECK_POLICY_VERSION,
    EntityType,
    IntegrityStatus,
)
from app.services.ioe.replay.events import IntegrityEventService
from app.services.ioe.replay.verification import (
    IntegrityVerificationService,
    VerificationAlreadyRunning,
)

log = get_logger("onyx.ioe.integrity_scheduler")

DEFAULT_BATCH_SIZE = 10
MAX_BATCH_SIZE = 50
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 2


def worker_identity() -> str:
    return f"{os.uname().nodename}:{os.getpid()}"


@dataclass
class ScheduledTarget:
    """The whole payload: a type and two identifiers. Nothing else may travel."""

    entity_type: str
    entity_id: uuid.UUID
    user_id: uuid.UUID


@dataclass
class SchedulerReport:
    claimed: int = 0
    verified: int = 0
    mismatch: int = 0
    unavailable: int = 0
    skipped_active: int = 0
    errors: int = 0
    recovered_claims: int = 0
    reason_codes: list[str] = field(default_factory=list)


class IntegrityScheduler:
    """Claims verification targets and runs each under tenant context."""

    policy_version = INTEGRITY_CHECK_POLICY_VERSION

    def __init__(self, worker_id: str | None = None):
        self.worker_id = worker_id or worker_identity()

    async def claim(
        self, *, entity_type: str = "optimization", batch_size: int = DEFAULT_BATCH_SIZE
    ) -> list[ScheduledTarget]:
        bounded = min(max(batch_size, 1), MAX_BATCH_SIZE)
        async with unit_of_work(actor_type="system") as session:
            rows = await session.execute(
                text(
                    "SELECT out_entity_type AS entity_type, "
                    "       out_entity_id   AS entity_id, "
                    "       out_user_id     AS user_id "
                    "FROM ioe.claim_integrity_targets(:batch, :kind)"
                ),
                {"batch": bounded, "kind": entity_type},
            )
            return [ScheduledTarget(**dict(r._mapping)) for r in rows]

    async def recover_stale_claims(self, limit: int = 100) -> int:
        async with unit_of_work(actor_type="system") as session:
            recovered = await session.scalar(
                text("SELECT ioe.recover_stale_integrity_checks(:limit)"),
                {"limit": limit},
            )
        count = int(recovered or 0)
        IntegrityEventService().stale_claim_recovered(count)
        return count

    async def run_once(
        self, *, entity_type: str = "optimization",
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> SchedulerReport:
        report = SchedulerReport()
        report.recovered_claims = await self.recover_stale_claims()

        targets = await self.claim(entity_type=entity_type, batch_size=batch_size)
        report.claimed = len(targets)

        for target in targets:
            try:
                result = await asyncio.wait_for(
                    self._verify(target), timeout=timeout_seconds
                )
            except VerificationAlreadyRunning:
                # Another worker got there first. Not an error.
                report.skipped_active += 1
                continue
            except TimeoutError:
                report.errors += 1
                log.warning(
                    "integrity.scheduler.timeout",
                    entity_type=target.entity_type, entity_id=str(target.entity_id),
                )
                continue
            except Exception:  # noqa: BLE001
                # Sanitized: the entity ids are already identifiers, and the
                # exception is deliberately not logged with its message.
                report.errors += 1
                log.warning(
                    "integrity.scheduler.failed",
                    entity_type=target.entity_type, entity_id=str(target.entity_id),
                )
                continue

            report.reason_codes.append(result.reason_code.value)
            if result.status is IntegrityStatus.VERIFIED:
                report.verified += 1
            elif result.status is IntegrityStatus.MISMATCH:
                report.mismatch += 1
            else:
                report.unavailable += 1
        return report

    async def _verify(self, target: ScheduledTarget):
        """Step 3–5 of the keyhole flow: tenant context, ordinary service, commit.

        `IntegrityVerificationService` opens its own transactions with
        `app.user_id` set to the claimed owner, so every statement it issues is
        subject to the same RLS policies as a request from that user.
        """
        service = IntegrityVerificationService(
            target.user_id, worker_id=self.worker_id)
        IntegrityEventService().started(
            entity_type=EntityType(target.entity_type), entity_id=target.entity_id)
        return await service.verify(target.entity_type, target.entity_id)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "MAX_BATCH_SIZE",
    "MAX_RETRIES",
    "IntegrityScheduler",
    "ScheduledTarget",
    "SchedulerReport",
]
