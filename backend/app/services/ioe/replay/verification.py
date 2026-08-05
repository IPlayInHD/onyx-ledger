"""IntegrityVerificationService — turns a replay observation into a verdict.

Transaction shape, and why:

    TX-1   authorize, claim the check, insert it `running`, commit.
    ——     replay OUTSIDE any transaction (it re-runs the tax engine).
    TX-2   transition the claimed row to its terminal outcome, update the
           entity's CURRENT integrity metadata, emit an operational event if
           the outcome warrants one, commit.

Holding a transaction open across the replay would put an engine run inside a
lock; claiming first is what lets three workers race safely without any of them
blocking on the others' compute.

What this service will not do, ever:

  * overwrite a sealed result, or the expected hash it was sealed with;
  * regenerate a historical run because it failed to reproduce;
  * report `mismatch` when a dependency could not be loaded — nothing was
    compared, so the honest answer is `unavailable`;
  * store an exception message. Reason codes are a closed enumeration; an
    exception string is unbounded text that has been near financial values.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, DomainError, NotFound
from app.database.models import (
    IntegrityCheck,
    OptimizationRun,
    Scenario,
    StrategyPortfolio,
)
from app.database.session import unit_of_work
from app.services.ioe.domain import canonical as c
from app.services.ioe.domain.integrity import (
    INTEGRITY_CHECK_POLICY_VERSION,
    VERIFIER_VERSION,
    CheckStatus,
    DependencyUnavailable,
    EntityType,
    IntegrityReason,
    IntegrityStatus,
    SealedEvidenceIncomplete,
    integrity_warning,
    user_visible_state,
)
from app.services.ioe.replay.events import IntegrityEventService
from app.services.ioe.replay.services import (
    OptimizationReplayService,
    PortfolioReplayService,
    ScenarioReplayService,
)

# How long a claim is honoured before a stale-claim sweep may reclaim it. Long
# enough that a slow replay is never stolen mid-flight; short enough that a dead
# worker does not block an entity for an operator's whole afternoon.
CLAIM_TTL = timedelta(minutes=15)


class VerificationAlreadyRunning(DomainError):
    """Another worker holds the active check for this entity and verifier."""

    status_code = 409
    error_type = "https://onyx.ledger/errors/verification-already-running"
    title = "Verification Already Running"


@dataclass
class VerificationResult:
    check_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    status: IntegrityStatus
    reason_code: IntegrityReason
    integrity_state: str
    integrity_warning: str
    duration_ms: int
    engine_runs: int


class IntegrityVerificationService:
    """Verifies one sealed entity. Tenant-scoped; RLS applies throughout."""

    def __init__(self, user_id: uuid.UUID, *, worker_id: str | None = None):
        self.user_id = user_id
        self.worker_id = worker_id or "api"

    async def verify(
        self, entity_type: EntityType | str, entity_id: uuid.UUID
    ) -> VerificationResult:
        kind = EntityType(entity_type)
        started = time.perf_counter()

        # ---- TX-1: authorize + claim ----------------------------------------
        check_id, expected_hash, expected_spec_hash = await self._claim(kind, entity_id)

        # ---- replay, outside any transaction --------------------------------
        status = CheckStatus.FAILED
        reason = IntegrityReason.REPLAY_EXECUTION_FAILED
        actual_hash: str | None = None
        engine_runs = 0
        try:
            outcome = await self._replay(kind, entity_id)
            actual_hash = outcome.actual_hash
            engine_runs = outcome.engine_runs
            if outcome.matches:
                status, reason = CheckStatus.VERIFIED, IntegrityReason.NONE
            else:
                status, reason = CheckStatus.MISMATCH, outcome.mismatch_reason
        except DependencyUnavailable as exc:
            status, reason = CheckStatus.UNAVAILABLE, exc.reason
        except SealedEvidenceIncomplete as exc:
            status, reason = CheckStatus.UNAVAILABLE, exc.reason
        except Exception:  # noqa: BLE001
            # The verifier itself failed. That proves nothing about the result,
            # so the ENTITY is left unverifiable rather than being downgraded on
            # the strength of a bug. The exception never reaches storage.
            status, reason = CheckStatus.FAILED, IntegrityReason.REPLAY_EXECUTION_FAILED

        duration_ms = int((time.perf_counter() - started) * 1000)

        # ---- TX-2: append outcome + move current metadata + emit ------------
        return await self._complete(
            kind, entity_id, check_id,
            status=status, reason=reason, actual_hash=actual_hash,
            expected_hash=expected_hash, expected_spec_hash=expected_spec_hash,
            duration_ms=duration_ms, engine_runs=engine_runs,
        )

    # ---------------------------------------------------------------- TX-1 ---
    async def _claim(
        self, kind: EntityType, entity_id: uuid.UUID
    ) -> tuple[uuid.UUID, str, str | None]:
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            expected_hash, expected_spec_hash = await self._sealed_hashes(
                session, kind, entity_id)

            check = IntegrityCheck(
                user_id=self.user_id,
                entity_type=kind.value,
                optimization_run_id=entity_id if kind is EntityType.OPTIMIZATION else None,
                portfolio_id=entity_id if kind is EntityType.PORTFOLIO else None,
                scenario_id=entity_id if kind is EntityType.SCENARIO else None,
                status=CheckStatus.RUNNING.value,
                reason_code=IntegrityReason.NONE.value,
                expected_spec_hash=expected_spec_hash,
                expected_result_hash=expected_hash,
                verifier_version=VERIFIER_VERSION,
                canonical_serialization_version=c.CANONICAL_SERIALIZATION_VERSION,
                integrity_check_policy_version=INTEGRITY_CHECK_POLICY_VERSION,
                claimed_by=self.worker_id,
                claim_expires_at=datetime.now(tz=UTC) + CLAIM_TTL,
            )
            session.add(check)
            try:
                await session.flush()
            except IntegrityError:
                # The partial unique index arbitrated: another worker already
                # holds the active check for this entity and verifier version.
                await session.rollback()
                raise VerificationAlreadyRunning(
                    "verification_already_running: an active check exists "
                    "for this entity"
                ) from None
            return check.id, expected_hash, expected_spec_hash

    async def _sealed_hashes(
        self, session: AsyncSession, kind: EntityType, entity_id: uuid.UUID
    ) -> tuple[str, str | None]:
        """Read the sealed identity, and prove ownership while doing it.

        Ownership is checked in the application layer as well as by RLS, and the
        failure is `NotFound` rather than `Forbidden` so an integrity endpoint
        cannot be used to discover that another user's entity exists.
        """
        if kind is EntityType.OPTIMIZATION:
            run = await session.get(OptimizationRun, entity_id)
            if run is None or run.user_id != self.user_id:
                raise NotFound("Optimization run not found")
            if not run.optimization_result_hash:
                raise Conflict("not_sealed: this run has no sealed result to verify")
            return run.optimization_result_hash, run.optimization_spec_hash

        if kind is EntityType.SCENARIO:
            scenario = await session.get(Scenario, entity_id)
            if scenario is None or scenario.user_id != self.user_id:
                raise NotFound("Scenario not found")
            if not scenario.scenario_result_hash:
                raise Conflict("not_sealed: this scenario has no sealed result to verify")
            return scenario.scenario_result_hash, scenario.scenario_spec_hash

        portfolio = await session.get(StrategyPortfolio, entity_id)
        if portfolio is None:
            raise NotFound("Portfolio not found")
        run = await session.get(OptimizationRun, portfolio.run_id)
        if run is None or run.user_id != self.user_id:
            raise NotFound("Portfolio not found")
        if not portfolio.portfolio_result_hash:
            raise Conflict("not_sealed: this portfolio has no sealed result hash")
        return portfolio.portfolio_result_hash, run.optimization_spec_hash

    # ---------------------------------------------------------------- replay -
    async def _replay(self, kind: EntityType, entity_id: uuid.UUID):
        if kind is EntityType.OPTIMIZATION:
            return await OptimizationReplayService(self.user_id).replay(entity_id)
        if kind is EntityType.SCENARIO:
            return await ScenarioReplayService(self.user_id).replay(entity_id)
        return await PortfolioReplayService(self.user_id).replay(entity_id)

    # ---------------------------------------------------------------- TX-2 ---
    async def _complete(
        self, kind: EntityType, entity_id: uuid.UUID, check_id: uuid.UUID, *,
        status: CheckStatus, reason: IntegrityReason, actual_hash: str | None,
        expected_hash: str, expected_spec_hash: str | None,
        duration_ms: int, engine_runs: int,
    ) -> VerificationResult:
        # A failed verifier proves nothing about the entity, so the entity's
        # own status becomes `unavailable`, not `mismatch`.
        entity_status = {
            CheckStatus.VERIFIED: IntegrityStatus.VERIFIED,
            CheckStatus.MISMATCH: IntegrityStatus.MISMATCH,
            CheckStatus.UNAVAILABLE: IntegrityStatus.UNAVAILABLE,
            CheckStatus.FAILED: IntegrityStatus.UNAVAILABLE,
        }[status]
        checked_at = datetime.now(tz=UTC)

        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            check = await session.get(IntegrityCheck, check_id)
            if check is None:
                raise NotFound("Integrity check not found")
            if check.status != CheckStatus.RUNNING.value:
                # A stale-claim sweep already terminated it. Completing twice is
                # harmless: the history keeps what actually happened.
                return self._result(check, kind, entity_id, duration_ms, engine_runs)

            # The event is emitted first so its id lands in the SAME update as
            # the terminal transition. The append-only guard allows exactly one
            # transition out of `running`; a second UPDATE to attach the event
            # id afterwards would be rejected, and rightly so.
            event_id = await IntegrityEventService(session).emit(
                entity_type=kind, entity_id=entity_id, user_id=self.user_id,
                status=status, reason=reason,
                expected_hash=expected_hash, actual_hash=actual_hash,
                duration_ms=duration_ms,
            )

            check.status = status.value
            check.reason_code = reason.value
            # The expected hash is never touched. Only the OBSERVED one is
            # written, and only when a replay actually produced one.
            check.actual_result_hash = actual_hash
            check.completed_at = checked_at
            check.duration_ms = duration_ms
            check.engine_runs_used = engine_runs
            check.operational_event_id = event_id

            await self._update_current_metadata(
                session, kind, entity_id,
                status=entity_status, reason=reason, check_id=check_id,
                checked_at=checked_at,
            )
            await session.flush()

            return VerificationResult(
                check_id=check_id,
                entity_type=kind.value,
                entity_id=entity_id,
                status=entity_status,
                reason_code=reason,
                integrity_state=user_visible_state(entity_status, reason),
                integrity_warning=integrity_warning(entity_status, reason),
                duration_ms=duration_ms,
                engine_runs=engine_runs,
            )

    @staticmethod
    def _result(check, kind, entity_id, duration_ms, engine_runs) -> VerificationResult:
        status = {
            CheckStatus.VERIFIED.value: IntegrityStatus.VERIFIED,
            CheckStatus.MISMATCH.value: IntegrityStatus.MISMATCH,
        }.get(check.status, IntegrityStatus.UNAVAILABLE)
        return VerificationResult(
            check_id=check.id, entity_type=kind.value, entity_id=entity_id,
            status=status, reason_code=IntegrityReason(check.reason_code),
            integrity_state=user_visible_state(status, check.reason_code),
            integrity_warning=integrity_warning(status, check.reason_code),
            duration_ms=duration_ms, engine_runs=engine_runs,
        )

    async def _update_current_metadata(
        self, session: AsyncSession, kind: EntityType, entity_id: uuid.UUID, *,
        status: IntegrityStatus, reason: IntegrityReason,
        check_id: uuid.UUID, checked_at: datetime,
    ) -> None:
        """Move CURRENT metadata only. The sealed result is not touched.

        `strategy_portfolio` is immutable calculation evidence; the database
        guard permits exactly these four columns to move and rejects an UPDATE
        that changes anything else, so this cannot become a repair path.
        """
        # Branched rather than looked up in a dict: a dict of model classes
        # erases the concrete type, and these four attributes only exist on the
        # three parents that actually carry integrity metadata.
        row: OptimizationRun | Scenario | StrategyPortfolio | None
        if kind is EntityType.OPTIMIZATION:
            row = await session.get(OptimizationRun, entity_id)
        elif kind is EntityType.SCENARIO:
            row = await session.get(Scenario, entity_id)
        else:
            row = await session.get(StrategyPortfolio, entity_id)
        if row is None:
            return
        row.integrity_status = status.value
        row.integrity_reason_code = reason.value
        row.last_integrity_checked_at = checked_at
        row.latest_integrity_check_id = check_id

    # ---------------------------------------------------------------- reads --
    async def history(
        self, entity_type: EntityType | str, entity_id: uuid.UUID, *, limit: int = 20
    ) -> list[IntegrityCheck]:
        """Append-only history, newest first. Tenant-scoped by RLS."""
        kind = EntityType(entity_type)
        column = {
            EntityType.OPTIMIZATION: IntegrityCheck.optimization_run_id,
            EntityType.SCENARIO: IntegrityCheck.scenario_id,
            EntityType.PORTFOLIO: IntegrityCheck.portfolio_id,
        }[kind]
        async with unit_of_work(user_id=self.user_id, actor_type="user") as session:
            rows = await session.scalars(
                select(IntegrityCheck)
                .where(column == entity_id)
                .order_by(IntegrityCheck.created_at.desc())
                .limit(min(max(limit, 1), 100))
            )
            return list(rows)


__all__ = [
    "CLAIM_TTL",
    "IntegrityVerificationService",
    "VerificationAlreadyRunning",
    "VerificationResult",
]
