"""IOE workers (§P6).

Four rules hold for every task here:

1. **Resource IDs only.** A task payload carries a user id, a scenario id, an
   analysis id — never a specification, never financial values, never a lever
   list. Broker payloads are logged, retried, and persisted in Redis; putting a
   user's financial data in one spreads it somewhere with none of the
   protections the database has. The worker re-reads the sealed record it needs.
2. **Bounded retries.** Every task has a finite `max_retries` with exponential
   backoff and a cap. A task that cannot succeed fails terminally rather than
   retrying forever against a broken dependency.
3. **Sanitized terminal failures.** When retries are exhausted, an enumerated
   code is recorded. Exception text can carry SQL fragments, row values, or
   identifiers, and none of that belongs in a stored failure record.
4. **Idempotency preserved.** Tasks pass through the same `Idempotency-Key` and
   canonical-spec resolution as the API, so a redelivered message replays the
   existing result rather than creating a second one. Celery guarantees
   at-least-once delivery, which means a duplicate is a matter of when, not if.
"""
from __future__ import annotations

import uuid

from celery import Task

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.session import unit_of_work
from app.services.ioe.domain.scenario import StaleReason
from app.services.ioe.scenario.freshness_service import (
    SWEEP_BATCH_SIZE,
    ScenarioFreshnessService,
)
from workers.celery_app import celery_app
from workers.runtime import run_task

log = get_logger("onyx.worker.ioe")

MAX_RETRIES = 4
RETRY_BACKOFF_CAP_SECONDS = 300

# Enumerated terminal failures. Never a message, stack trace, or row value.
ERROR_RETRIES_EXHAUSTED = "RETRIES_EXHAUSTED"
ERROR_INVALID_PAYLOAD = "INVALID_PAYLOAD"


def _backoff(attempt: int) -> int:
    """Exponential with a cap, so a broken dependency is not hammered."""
    # `int ** int` is Any in typeshed (the exponent could be negative), which
    # would leak out through this function's declared int return.
    unbounded: int = 2 ** attempt
    return min(unbounded, RETRY_BACKOFF_CAP_SECONDS)


@celery_app.task(
    name="workers.tasks.ioe.run_optimization",
    bind=True,
    max_retries=MAX_RETRIES,
    acks_late=True,
)
def run_optimization(
    self: Task, user_id: str, analysis_id: str, idempotency_key: str | None = None
) -> str:
    """Generate an optimization run.

    Payload is three identifiers. The idempotency key is passed through, so a
    redelivered message resolves to the existing run instead of producing a
    second one.
    """
    from app.services.ioe.orchestrator import OptimizationOrchestrator

    async def _run() -> str:
        # `admission_guard` inside the orchestrator already refuses a deleting
        # account, but it does so by raising — which would send a task that is
        # behaving perfectly through the retry ladder. Preflighting turns a
        # correct refusal into a clean stop.
        from app.database.session import unit_of_work
        from app.services.privacy.preflight import refuse_if_deleting

        async with unit_of_work(actor_type="system") as session:
            if await refuse_if_deleting(session, uuid.UUID(user_id),
                                        task="ioe.run_optimization"):
                return ""

        outcome = await OptimizationOrchestrator(uuid.UUID(user_id)).generate(
            uuid.UUID(analysis_id), idempotency_key=idempotency_key
        )
        return str(outcome.run_id)

    try:
        return run_task(_run)
    except ValueError as exc:
        # a malformed identifier will never succeed; do not retry
        log.error("ioe_run_optimization_invalid", error_code=ERROR_INVALID_PAYLOAD)
        raise self.retry(exc=exc, max_retries=0) from exc
    except Exception as exc:  # noqa: BLE001
        if self.request.retries >= MAX_RETRIES:
            log.error(
                "ioe_run_optimization_failed",
                error_code=ERROR_RETRIES_EXHAUSTED,
                analysis_id=analysis_id,
            )
            raise
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc


@celery_app.task(
    name="workers.tasks.ioe.invalidate_scenarios_for_analysis",
    bind=True,
    max_retries=MAX_RETRIES,
    acks_late=True,
)
def invalidate_scenarios_for_analysis(self: Task, analysis_id: str, reason_code: str) -> int:
    """EVENT-DRIVEN freshness: a baseline moved.

    Marks every completed, currently-fresh scenario on that analysis stale. It
    writes only freshness columns; no stored result is modified. Idempotent by
    construction — the update is a no-op once the rows are already stale.
    """
    async def _run() -> int:
        async with unit_of_work(actor_type="system") as session:
            service = ScenarioFreshnessService(session)
            return await service.invalidate_for_analysis(
                uuid.UUID(analysis_id), StaleReason(reason_code)
            )

    try:
        count = run_task(_run)
        log.info("ioe_freshness_invalidated", analysis_id=analysis_id, count=count)
        return count
    except ValueError as exc:
        log.error("ioe_freshness_invalid_payload", error_code=ERROR_INVALID_PAYLOAD)
        raise self.retry(exc=exc, max_retries=0) from exc
    except Exception as exc:  # noqa: BLE001
        if self.request.retries >= MAX_RETRIES:
            log.error("ioe_freshness_failed", error_code=ERROR_RETRIES_EXHAUSTED)
            raise
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc


@celery_app.task(
    name="workers.tasks.ioe.invalidate_scenarios_for_tax_year",
    bind=True,
    max_retries=MAX_RETRIES,
    acks_late=True,
)
def invalidate_scenarios_for_tax_year(self: Task, tax_year: int, reason_code: str) -> int:
    """EVENT-DRIVEN freshness: a rule publication or reference-data change."""
    async def _run() -> int:
        async with unit_of_work(actor_type="system") as session:
            service = ScenarioFreshnessService(session)
            return await service.invalidate_for_tax_year(
                int(tax_year), StaleReason(reason_code)
            )

    try:
        count = run_task(_run)
        log.info("ioe_freshness_invalidated_year", tax_year=tax_year, count=count)
        return count
    except ValueError as exc:
        log.error("ioe_freshness_invalid_payload", error_code=ERROR_INVALID_PAYLOAD)
        raise self.retry(exc=exc, max_retries=0) from exc
    except Exception as exc:  # noqa: BLE001
        if self.request.retries >= MAX_RETRIES:
            log.error("ioe_freshness_failed", error_code=ERROR_RETRIES_EXHAUSTED)
            raise
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc


@celery_app.task(
    name="workers.tasks.ioe.relay_freshness_outbox",
    bind=True,
    max_retries=MAX_RETRIES,
    acks_late=True,
)
def relay_freshness_outbox(self: Task, batch_size: int = 50) -> int:
    """Drain the transactional outbox — the NORMAL freshness path.

    Celery is transport only. The outbox table is the source of truth: a lost
    or duplicated message costs at most a delayed or repeated invalidation, and
    a repeated one is a no-op.

    The relay claims through a narrow privileged interface and then applies each
    event in an ordinary RLS-protected transaction scoped to one tenant, so no
    step of this task can read across tenants.
    """
    from app.services.ioe.freshness_relay import FreshnessRelay

    async def _run() -> int:
        report = await FreshnessRelay().drain(batch_size=batch_size)
        log.info(
            "ioe_freshness_relay",
            claimed=report.claimed, completed=report.completed,
            failed=report.failed, fanned_out=report.fanned_out,
            scenarios=report.scenarios_marked,
        )
        return report.completed

    try:
        return run_task(_run)
    except Exception as exc:  # noqa: BLE001
        if self.request.retries >= MAX_RETRIES:
            log.error("ioe_freshness_relay_failed", error_code=ERROR_RETRIES_EXHAUSTED)
            raise
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc


@celery_app.task(
    name="workers.tasks.ioe.sweep_scenario_freshness",
    bind=True,
    max_retries=2,
)
def sweep_scenario_freshness(self: Task, limit: int = SWEEP_BATCH_SIZE) -> int:
    """SCHEDULED fallback sweep.

    The safety net, not the primary mechanism: it catches events that were never
    emitted and scenarios nobody has opened. Bounded per run and ordered by
    least-recently-evaluated, so no scenario can be starved and the sweep cannot
    become an unbounded scan.
    """
    async def _run() -> int:
        async with unit_of_work(actor_type="system") as session:
            transitions = await ScenarioFreshnessService(session).sweep(limit=limit)
            return sum(1 for t in transitions if t.changed)

    try:
        changed = run_task(_run)
        log.info("ioe_freshness_sweep", changed=changed, limit=limit)
        return changed
    except Exception as exc:  # noqa: BLE001
        if self.request.retries >= 2:
            log.error("ioe_freshness_sweep_failed", error_code=ERROR_RETRIES_EXHAUSTED)
            raise
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc


@celery_app.task(
    name="workers.tasks.ioe.verify_sealed_integrity",
    bind=True,
    max_retries=2,
    acks_late=True,
)
def verify_sealed_integrity(self: Task, batch_size: int | None = None) -> dict:
    """SCHEDULED replay-integrity verification (closure entry 8C).

    Detection already existed; nothing invoked it. This is the invocation, and
    it deliberately adds no logic of its own — it calls `IntegrityScheduler`,
    which claims through `ioe.claim_integrity_targets` and verifies through
    `IntegrityVerificationService`. There is no second claim protocol and no
    second verifier, so the scheduled path cannot drift from the API path.

    Bounded twice over: `batch_size` is the budget for the WHOLE execution
    across every target type, the scheduler clamps it to `MAX_BATCH_SIZE`, and
    the SQL clamps it again. One execution claims once per type and returns —
    there is no loop-until-empty, so a backlog is drained by the beat schedule
    rather than by one long-running task.

    Safe to overlap. Two workers may select the same record, but only one can
    insert the active check; the loser records `skipped_active` and moves on. A
    record abandoned by a crashed worker is released by
    `recover_stale_integrity_checks`, which runs at the start of every cycle.

    The return value is counts only — it is a Celery result stored in Redis, so
    nothing identifying may travel in it.
    """
    from app.services.ioe.replay.scheduler import IntegrityScheduler

    settings = get_settings()
    if not settings.ioe_integrity_verification_enabled:
        log.info("ioe_integrity_verification_disabled")
        return {"enabled": False}

    size = settings.ioe_integrity_batch_size if batch_size is None else int(batch_size)

    async def _run() -> dict:
        report = await IntegrityScheduler().run_cycle(
            batch_size=size,
            timeout_seconds=settings.ioe_integrity_timeout_seconds,
        )
        metrics = report.as_metrics()
        # Counts and enumerated reason codes. No entity id, no tenant, no hash.
        log.info(
            "ioe_integrity_verification_batch",
            **metrics,
            reason_codes=sorted(set(report.reason_codes)),
        )
        return metrics

    try:
        return run_task(_run)
    except Exception as exc:  # noqa: BLE001
        if self.request.retries >= 2:
            log.error(
                "ioe_integrity_verification_failed",
                error_code=ERROR_RETRIES_EXHAUSTED,
            )
            raise
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries)) from exc


__all__ = [
    "ERROR_INVALID_PAYLOAD",
    "ERROR_RETRIES_EXHAUSTED",
    "MAX_RETRIES",
    "verify_sealed_integrity",
    "invalidate_scenarios_for_analysis",
    "relay_freshness_outbox",
    "invalidate_scenarios_for_tax_year",
    "run_optimization",
    "sweep_scenario_freshness",
]
