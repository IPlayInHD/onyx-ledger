"""IntegrityEventService — sanitized operational events for verification.

The privacy rule here is absolute and the reason is concrete: a mismatch alert
is the one integrity artefact that leaves the database. It goes to a log, a
metric, and eventually a pager. So it carries identifiers, codes, versions and
hashes — and nothing that could reconstruct a person's finances.

What may appear:  entity type and id, tenant reference, expected and actual
                  hash, reason code, verifier version, correlation id, duration.
What may never:   tax inputs, income, deductions, SIN-like values, document
                  text, assumption narrative, or any serialized canonical
                  payload. A canonical payload IS the financial data.

Hashes are treated as restricted operational metadata: they are emitted to the
structured log (which is access-controlled) but the alert-facing metric carries
only the check id, so an alert routed to a wider audience still says nothing.
"""
from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.services.ioe.domain.integrity import (
    VERIFIER_VERSION,
    CheckStatus,
    EntityType,
    IntegrityReason,
)

log = get_logger("onyx.ioe.integrity")

# Severity policy. An unavailable dependency is expected during a version
# rollout and must not page anybody; a mismatch always must.
ALERTING_OUTCOMES = frozenset({CheckStatus.MISMATCH})
LOGGED_OUTCOMES = frozenset({
    CheckStatus.VERIFIED, CheckStatus.MISMATCH,
    CheckStatus.UNAVAILABLE, CheckStatus.FAILED,
})


@dataclass
class IntegrityMetrics:
    """In-process counters. A real deployment scrapes these; the shape is what
    matters here — counts and durations, never a value from a return."""

    started: int = 0
    verified: int = 0
    mismatch: int = 0
    unavailable: int = 0
    failed: int = 0
    stale_claims_recovered: int = 0
    total_duration_ms: int = 0
    completed: int = 0

    @property
    def average_duration_ms(self) -> float:
        return self.total_duration_ms / self.completed if self.completed else 0.0

    def as_dict(self) -> dict:
        return {
            "integrity_verification_started": self.started,
            "integrity_verification_verified": self.verified,
            "integrity_verification_mismatch": self.mismatch,
            "integrity_verification_unavailable": self.unavailable,
            "integrity_verification_failed": self.failed,
            "integrity_stale_claims_recovered": self.stale_claims_recovered,
            "integrity_verification_avg_duration_ms": round(self.average_duration_ms, 2),
        }


METRICS = IntegrityMetrics()
REASON_COUNTS: Counter[str] = Counter()


class IntegrityEventService:
    """Emits the sanitized record of a verification outcome."""

    def __init__(self, session: AsyncSession | None = None):
        self.s = session

    def started(self, *, entity_type: EntityType, entity_id: uuid.UUID) -> None:
        METRICS.started += 1
        log.info(
            "integrity.verification.started",
            entity_type=entity_type.value, entity_id=str(entity_id),
            verifier_version=VERIFIER_VERSION,
        )

    def stale_claim_recovered(self, count: int) -> None:
        METRICS.stale_claims_recovered += count
        if count:
            log.warning("integrity.claim.recovered", recovered=count)

    async def emit(
        self, *, entity_type: EntityType, entity_id: uuid.UUID, user_id: uuid.UUID,
        status: CheckStatus, reason: IntegrityReason,
        expected_hash: str, actual_hash: str | None, duration_ms: int,
    ) -> uuid.UUID | None:
        """Record the outcome. Returns an event id for a mismatch, else None."""
        METRICS.completed += 1
        METRICS.total_duration_ms += duration_ms
        REASON_COUNTS[reason.value] += 1
        setattr(METRICS, status.value, getattr(METRICS, status.value, 0) + 1)

        if status not in LOGGED_OUTCOMES:
            return None

        # Identifiers, codes, versions, durations. No financial value can reach
        # this call: the arguments have no channel for one.
        payload = {
            "entity_type": entity_type.value,
            "entity_id": str(entity_id),
            "tenant": str(user_id),
            "reason_code": reason.value,
            "verifier_version": VERIFIER_VERSION,
            "duration_ms": duration_ms,
        }

        if status is CheckStatus.MISMATCH:
            event_id = uuid.uuid4()
            log.error(
                "integrity.verification.mismatch",
                correlation_id=str(event_id),
                expected_hash=expected_hash,
                actual_hash=actual_hash,
                **payload,
            )
            # Alert-facing signal carries the correlation id only. Anyone who
            # needs the hashes reads the restricted integrity record.
            log.error(
                "alert.integrity_mismatch",
                correlation_id=str(event_id),
                entity_type=entity_type.value,
                reason_code=reason.value,
            )
            return event_id

        if status is CheckStatus.UNAVAILABLE:
            # Expected during a version rollout. Recorded, never paged.
            log.info("integrity.verification.unavailable", **payload)
            return None

        if status is CheckStatus.FAILED:
            log.warning("integrity.verification.execution_failed", **payload)
            return None

        log.info("integrity.verification.verified", **payload)
        return None


def metrics_snapshot() -> dict:
    """Everything an operator can scrape, including per-reason counts."""
    snapshot = METRICS.as_dict()
    snapshot["integrity_reason_counts"] = dict(REASON_COUNTS)
    return snapshot


def reset_metrics() -> None:
    """Test hook. Production never calls this."""
    global METRICS
    METRICS = IntegrityMetrics()
    REASON_COUNTS.clear()


__all__ = [
    "ALERTING_OUTCOMES",
    "METRICS",
    "IntegrityEventService",
    "IntegrityMetrics",
    "metrics_snapshot",
    "reset_metrics",
]
