"""AdmissionService — decide whether expensive work may start, before it starts.

Three checks, in increasing cost, cheapest-rejection-first:

  1. RATE         one conditional UPSERT. Catches retry storms and scripts.
  2. DEDUPE       one indexed lookup. Catches the same logical request arriving
                  twice because a client timed out and retried.
  3. CONCURRENCY  advisory lock + count + insert. Catches queue flooding.

RACE SAFETY IS THE WHOLE POINT
------------------------------
The obvious implementation is wrong:

    count = SELECT count(*) FROM lease WHERE ... AND live
    if count < limit:
        INSERT lease

Under READ COMMITTED, ten concurrent requests all run the SELECT before any
INSERT commits, all see 0, and all ten are admitted against a limit of 2. The
window is small, which makes it worse: it will not show up in manual testing and
will show up under exactly the load the limit exists to survive.

Two mechanisms close it here:

  * RATE uses a single statement whose WHERE clause is evaluated against the
    row it has already locked (`ON CONFLICT DO UPDATE ... WHERE count < limit`).
    Concurrent writers serialize on that row lock, so the increment cannot
    overshoot.

  * CONCURRENCY takes `pg_advisory_xact_lock` on a hash of (scope, operation)
    first, so the count-then-insert pair is serialized per scope. The lock is
    transaction-scoped, so it is released by COMMIT or ROLLBACK — including the
    rollback of a process that died holding it. The same primitive is already
    used by `ioe.version_activation`.

Contention is per (scope, operation), so two different users never wait on each
other, and the critical section is two indexed statements long.

WHAT A REJECTION MAY SAY
------------------------
A closed `RejectionReason` code and a retry-after. Never a queue depth, never a
worker count, never another principal's activity, never an exception string.
"""
from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.core.logging import get_logger
from app.services.admission.policy import (
    AdmissionPolicy,
    OperationClass,
    RejectionReason,
    ScopeType,
    StoreFailurePolicy,
    policy_for,
)

log = get_logger("onyx.admission")

ADMISSION_POLICY_VERSION = "1.0.0"

# The literal scope_id used for platform-wide accounting. A user id can never
# collide with it: it is not a UUID.
GLOBAL_SCOPE_ID = "platform"


# ---------------------------------------------------------------------------
# Metrics. In-process counters only — there is no exporter in this repository,
# and claiming otherwise would be claiming alerting that does not exist.
# Labels are bounded codes: operation_code, reason_code, outcome. Never a user
# id, tenant id, request id or job id, all of which are unbounded in cardinality
# and would turn a metric into a per-user activity log.
# ---------------------------------------------------------------------------
ADMISSION_REQUESTS: Counter[str] = Counter()
ADMISSION_ACCEPTED: Counter[str] = Counter()
ADMISSION_REJECTED: Counter[str] = Counter()
LEASES_RECOVERED: Counter[str] = Counter()


def admission_metrics() -> dict[str, dict[str, int]]:
    """Snapshot of the in-process counters. Not exported anywhere."""
    return {
        "admission_requests_total": dict(ADMISSION_REQUESTS),
        "admission_accepted_total": dict(ADMISSION_ACCEPTED),
        "admission_rejected_total": dict(ADMISSION_REJECTED),
        "job_lease_recovered_total": dict(LEASES_RECOVERED),
    }


def reset_admission_metrics() -> None:
    for counter in (ADMISSION_REQUESTS, ADMISSION_ACCEPTED, ADMISSION_REJECTED,
                    LEASES_RECOVERED):
        counter.clear()


class AdmissionRejected(DomainError):
    """Refused by an admission policy. Maps to 429, or 503 for global capacity.

    Carries a closed reason code and a retry-after. Nothing else: the caller
    learns that it was refused and roughly when to come back, not why the
    platform is busy or who else is using it.
    """

    error_type = "https://onyx.ledger/errors/admission-rejected"
    title = "Too Many Requests"

    def __init__(
        self,
        operation: OperationClass,
        reason: RejectionReason,
        retry_after_seconds: int,
    ) -> None:
        self.operation = operation
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        # GLOBAL_CAPACITY is the platform being full, not this caller
        # misbehaving. 503 says "come back later"; 429 says "you asked too
        # much". Returning 429 for a platform-wide condition would tell a
        # blameless caller to slow down, and would make a capacity incident
        # invisible in the 5xx rate.
        self.status_code = 503 if reason is RejectionReason.GLOBAL_CAPACITY else 429
        if self.status_code == 503:
            self.title = "Service Unavailable"
        super().__init__(f"{operation.value}:{reason.value}")


@dataclass(frozen=True)
class AdmissionTicket:
    """Proof that an operation was admitted, and the handle used to release it.

    Carries BOTH leases an admission may have taken. The pairing lives here, on
    the value the caller already has to hold, rather than in a registry on the
    service: a per-instance dict would not survive the request, and a
    class-level one would be shared across every session in the process and
    leak an entry per admission.
    """

    lease_id: uuid.UUID | None
    operation: OperationClass
    scope_id: str
    #: The platform-capacity lease taken alongside the per-principal one, if any.
    global_lease_id: uuid.UUID | None = None
    #: True when a live lease for the same dedupe_key already existed. The caller
    #: should join that operation rather than start a second one.
    duplicate_of_active: bool = False

    @property
    def holds_lease(self) -> bool:
        return self.lease_id is not None and not self.duplicate_of_active


@dataclass(frozen=True)
class AuthAdmissionDecision:
    """The outcome of charging a credential attempt, carried out of the
    transaction so the rejection can be raised after the counters commit.

    Says whether the attempt may proceed and when to come back. Deliberately
    NOT which of the two scopes refused: that would tell an unauthenticated
    caller whether the address it typed is one the platform is tracking.
    """

    accepted: bool
    retry_after_seconds: int

    def raise_if_rejected(self) -> None:
        if not self.accepted:
            raise AdmissionRejected(
                OperationClass.AUTH_ATTEMPT,
                RejectionReason.AUTH_RATE_LIMIT,
                self.retry_after_seconds,
            )


def _lock_key(scope_type: ScopeType, scope_id: str, operation: OperationClass) -> int:
    """A stable 63-bit advisory-lock key for one (scope, operation) pair.

    Advisory locks share one cluster-wide namespace, so the key is derived from a
    domain-separated digest rather than from `hash()`, which is randomized per
    process and would put two API replicas in different namespaces.
    """
    digest = hashlib.sha256(
        f"onyx.admission.v1|{scope_type.value}|{scope_id}|{operation.value}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


class AdmissionService:
    """Admission decisions for one caller.

    Holds no transaction: every method takes the caller's session, so a rejected
    or admitted decision commits and rolls back with the work it guards.
    """

    def __init__(self, session: AsyncSession):
        self.s = session

    # ------------------------------------------------------------------ rate --
    async def _check_rate(
        self,
        policy: AdmissionPolicy,
        scope_type: ScopeType,
        scope_id: str,
        *,
        now: datetime,
    ) -> bool:
        """True if this attempt fits inside the window's allowance.

        One statement. The conditional DO UPDATE means concurrent callers
        serialize on the counter row and the increment stops exactly at the
        allowance — there is no read-then-write gap to lose.

        The allowance depends on the scope: a source address and a claimed
        identity are bounded by different numbers (see `AdmissionPolicy`).
        """
        allowance = policy.allowance_for(scope_type)
        if allowance is None:
            return True

        window_start = now.replace(second=0, microsecond=0)
        result = await self.s.execute(
            text("""
                INSERT INTO admission.rate_counter AS rc
                    (scope_type, scope_id, operation_code, window_start,
                     request_count, updated_at)
                VALUES (:scope_type, :scope_id, :operation, :window_start, 1, now())
                ON CONFLICT (scope_type, scope_id, operation_code, window_start)
                DO UPDATE SET request_count = rc.request_count + 1,
                              updated_at    = now()
                          WHERE rc.request_count < :allowance
                RETURNING request_count
            """),
            {
                "scope_type": scope_type.value,
                "scope_id": scope_id,
                "operation": policy.operation.value,
                "window_start": window_start,
                "allowance": allowance,
            },
        )
        # No row returned => the DO UPDATE's WHERE was false => already at the
        # allowance. The counter is deliberately NOT incremented past it, so a
        # caller hammering a closed door cannot push its own window further out.
        return result.first() is not None

    # --------------------------------------------------------------- dedupe --
    async def _find_active_duplicate(
        self, operation: OperationClass, dedupe_key: str, *, now: datetime
    ) -> uuid.UUID | None:
        result = await self.s.execute(
            text("""
                SELECT id FROM admission.lease
                 WHERE operation_code = :operation
                   AND dedupe_key     = :dedupe_key
                   AND released_at IS NULL
                   AND expires_at  > :now
                 ORDER BY acquired_at
                 LIMIT 1
            """),
            {"operation": operation.value, "dedupe_key": dedupe_key, "now": now},
        )
        row = result.first()
        return uuid.UUID(str(row[0])) if row else None

    # ---------------------------------------------------------- concurrency --
    async def _acquire_lease(
        self,
        policy: AdmissionPolicy,
        scope_type: ScopeType,
        scope_id: str,
        *,
        dedupe_key: str | None,
        now: datetime,
    ) -> uuid.UUID | None:
        """Take a slot, or return None if the scope is already at its limit.

        Serialized per (scope, operation) by a transaction-scoped advisory lock,
        so the count and the insert cannot interleave with a concurrent caller's.
        """
        limit = (
            policy.max_active_global
            if scope_type is ScopeType.GLOBAL
            else policy.max_active_per_user
        )
        if limit is None:
            return None

        await self.s.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": _lock_key(scope_type, scope_id, policy.operation)},
        )

        # Expired leases are simply not counted. Recovery needs no sweeper: a
        # crashed worker's slot returns the moment its lease lapses.
        active = await self.s.scalar(
            text("""
                SELECT count(*) FROM admission.lease
                 WHERE scope_type     = :scope_type
                   AND scope_id       = :scope_id
                   AND operation_code = :operation
                   AND released_at IS NULL
                   AND expires_at  > :now
            """),
            {
                "scope_type": scope_type.value,
                "scope_id": scope_id,
                "operation": policy.operation.value,
                "now": now,
            },
        )
        if (active or 0) >= limit:
            return None

        lease_id = uuid.uuid4()
        await self.s.execute(
            text("""
                INSERT INTO admission.lease
                    (id, scope_type, scope_id, operation_code, dedupe_key,
                     acquired_at, expires_at)
                VALUES
                    (:id, :scope_type, :scope_id, :operation, :dedupe_key,
                     :now, :expires_at)
            """),
            {
                "id": lease_id,
                "scope_type": scope_type.value,
                "scope_id": scope_id,
                "operation": policy.operation.value,
                "dedupe_key": dedupe_key,
                "now": now,
                "expires_at": now + timedelta(seconds=policy.lease_seconds),
            },
        )
        return lease_id

    # -------------------------------------------------------------- pre-auth --
    async def charge_auth_attempt(
        self,
        *,
        source_scope_id: str,
        subject_scope_id: str,
        now: datetime | None = None,
    ) -> AuthAdmissionDecision:
        """Charge one credential attempt to BOTH pre-authentication scopes.

        Returns a decision instead of raising, and the distinction is
        load-bearing. `_reject` raises, and an exception inside the caller's
        transaction rolls it back — which would undo the source-address
        increment whenever the identity limit was the one that refused. The
        source counter would then never fill while an attacker hammered a single
        account, and the limit that exists to catch credential stuffing would be
        silently uncountable.

        So: charge both, commit, and let the caller raise afterwards. Both
        counters advance on every attempt, whichever limit ends up refusing it.

        A store failure still raises from here — `AUTH_ATTEMPT` is FAIL_CLOSED,
        and there is no partial state worth committing when the store cannot
        answer at all.
        """
        policy = policy_for(OperationClass.AUTH_ATTEMPT)
        now = now or datetime.now(tz=UTC)
        ADMISSION_REQUESTS[OperationClass.AUTH_ATTEMPT.value] += 1

        try:
            source_ok = await self._check_rate(
                policy, ScopeType.IP, source_scope_id, now=now)
            subject_ok = await self._check_rate(
                policy, ScopeType.AUTH_SUBJECT, subject_scope_id, now=now)
        except (SQLAlchemyError, DBAPIError) as exc:
            self._on_store_failure(policy, source_scope_id, exc)
            raise  # unreachable: AUTH_ATTEMPT is FAIL_CLOSED, which raises above

        accepted = source_ok and subject_ok
        if accepted:
            ADMISSION_ACCEPTED[OperationClass.AUTH_ATTEMPT.value] += 1
            log.info("admission", operation=OperationClass.AUTH_ATTEMPT.value,
                     decision="ACCEPTED")
        else:
            # Counted here, raised by the caller after the commit. The metric
            # records WHICH scope refused, because "stuffing across accounts"
            # and "guessing at one account" need different responses — but only
            # as a bounded code, never with the scope id attached.
            ADMISSION_REJECTED[
                f"{OperationClass.AUTH_ATTEMPT.value}:"
                f"{RejectionReason.AUTH_RATE_LIMIT.value}:"
                f"{'SOURCE' if not source_ok else 'SUBJECT'}"
            ] += 1
            log.info("admission", operation=OperationClass.AUTH_ATTEMPT.value,
                     decision="REJECTED",
                     reason=RejectionReason.AUTH_RATE_LIMIT.value,
                     scope="SOURCE" if not source_ok else "SUBJECT")
        return AuthAdmissionDecision(
            accepted=accepted, retry_after_seconds=policy.retry_after_seconds
        )

    # ------------------------------------------------------------------ api --
    async def admit(
        self,
        operation: OperationClass,
        *,
        scope_id: str,
        scope_type: ScopeType = ScopeType.USER,
        dedupe_key: str | None = None,
        now: datetime | None = None,
    ) -> AdmissionTicket:
        """Admit the operation, or raise `AdmissionRejected`.

        `scope_id` MUST come from the authenticated principal. Nothing here reads
        a body field or a header for identity: a caller that could name its own
        scope could spend somebody else's budget, or evade its own by inventing
        a fresh one per request.
        """
        policy = policy_for(operation)
        now = now or datetime.now(tz=UTC)
        ADMISSION_REQUESTS[operation.value] += 1

        try:
            return await self._admit(policy, scope_id, scope_type, dedupe_key, now)
        except AdmissionRejected:
            raise
        except (SQLAlchemyError, DBAPIError) as exc:
            return self._on_store_failure(policy, scope_id, exc)

    async def _admit(
        self,
        policy: AdmissionPolicy,
        scope_id: str,
        scope_type: ScopeType,
        dedupe_key: str | None,
        now: datetime,
    ) -> AdmissionTicket:
        operation = policy.operation

        # 1 · rate, per principal
        if not await self._check_rate(policy, scope_type, scope_id, now=now):
            # AUTH_ATTEMPT gets its own code. USER_RATE_LIMIT would be a lie
            # before authentication — there is no user yet — and a caller who
            # reads it as "MY account is limited" has learned that the account
            # exists, which is the one thing the login surface must not say.
            reason = (
                RejectionReason.AUTH_RATE_LIMIT
                if operation is OperationClass.AUTH_ATTEMPT
                else RejectionReason.USER_RATE_LIMIT
            )
            self._reject(operation, reason, policy, scope_id)

        if not policy.tracks_concurrency:
            ADMISSION_ACCEPTED[operation.value] += 1
            return AdmissionTicket(None, operation, scope_id)

        # 2 · an identical logical request already in flight. Checked BEFORE the
        # concurrency limits so a retry storm resolves to the running operation
        # rather than being told it hit a limit its own retries created.
        if dedupe_key is not None:
            existing = await self._find_active_duplicate(operation, dedupe_key, now=now)
            if existing is not None:
                ADMISSION_ACCEPTED[operation.value] += 1
                log.info("admission", operation=operation.value,
                         decision="DUPLICATE", reason=RejectionReason
                         .DUPLICATE_ACTIVE_OPERATION.value)
                return AdmissionTicket(
                    existing, operation, scope_id, duplicate_of_active=True
                )

        # 3 · platform capacity BEFORE the per-principal slot, so a caller with
        # quota to spare is not charged a lease the platform has no room to run.
        global_lease: uuid.UUID | None = None
        if policy.max_active_global is not None:
            global_lease = await self._acquire_lease(
                policy, ScopeType.GLOBAL, GLOBAL_SCOPE_ID,
                dedupe_key=None, now=now,
            )
            if global_lease is None:
                self._reject(operation, RejectionReason.GLOBAL_CAPACITY, policy, scope_id)

        # 4 · this principal's slot
        lease_id = await self._acquire_lease(
            policy, scope_type, scope_id, dedupe_key=dedupe_key, now=now
        )
        if lease_id is None:
            if global_lease is not None:
                await self.release(global_lease, reason="CANCELLED")
            self._reject(
                operation, RejectionReason.USER_CONCURRENCY_LIMIT, policy, scope_id
            )

        ADMISSION_ACCEPTED[operation.value] += 1
        log.info("admission", operation=operation.value, decision="ACCEPTED")
        # The global lease travels with the per-principal one, so releasing the
        # ticket releases both and platform capacity cannot leak while a user's
        # own quota is returned.
        return AdmissionTicket(lease_id, operation, scope_id,
                               global_lease_id=global_lease)

    def _reject(
        self,
        operation: OperationClass,
        reason: RejectionReason,
        policy: AdmissionPolicy,
        scope_id: str,
    ) -> None:
        ADMISSION_REJECTED[f"{operation.value}:{reason.value}"] += 1
        # Codes only. No scope id, no counter value, no payload — this line goes
        # to the same log stream as everything else.
        log.info("admission", operation=operation.value,
                 decision="REJECTED", reason=reason.value)
        raise AdmissionRejected(operation, reason, policy.retry_after_seconds)

    def _on_store_failure(
        self, policy: AdmissionPolicy, scope_id: str, exc: Exception
    ) -> AdmissionTicket:
        """The admission store could not answer. Behaviour is per class.

        Failing open on an optimization run would remove the protection exactly
        when a database problem makes overload most likely; failing closed on a
        profile read would turn a limiter fault into a site outage. Neither
        answer is right for both, so the policy says which.
        """
        # The exception type, never its message: a DBAPI error string can carry
        # SQL fragments and parameter values.
        log.warning("admission_store_unavailable",
                    operation=policy.operation.value,
                    policy=policy.on_store_failure.value,
                    error_class=type(exc).__name__)
        if policy.on_store_failure is StoreFailurePolicy.FAIL_CLOSED:
            ADMISSION_REJECTED[
                f"{policy.operation.value}:{RejectionReason.ADMISSION_STORE_UNAVAILABLE.value}"
            ] += 1
            raise AdmissionRejected(
                policy.operation,
                RejectionReason.ADMISSION_STORE_UNAVAILABLE,
                policy.retry_after_seconds,
            ) from exc
        ADMISSION_ACCEPTED[policy.operation.value] += 1
        return AdmissionTicket(None, policy.operation, scope_id)

    async def release(self, *lease_ids: uuid.UUID, reason: str = "COMPLETED") -> None:
        """Return slots. Idempotent, and never raises into the caller's path.

        A release failure must not turn a SUCCESSFUL operation into an error the
        user sees; the lease expiry is the backstop that makes that safe.
        """
        wanted = [lease_id for lease_id in lease_ids if lease_id is not None]
        if not wanted:
            return
        try:
            await self.s.execute(
                text("""
                    UPDATE admission.lease
                       SET released_at = now(), release_reason = :reason
                     WHERE id = ANY(:ids) AND released_at IS NULL
                """),
                {"ids": wanted, "reason": reason},
            )
        except (SQLAlchemyError, DBAPIError) as exc:
            log.warning("admission_release_failed",
                        error_class=type(exc).__name__)

    async def release_ticket(
        self, ticket: AdmissionTicket, *, reason: str = "COMPLETED"
    ) -> None:
        """Release everything the ticket holds — the principal slot and, with it,
        the platform-capacity slot."""
        if not ticket.holds_lease:
            return
        ids = [i for i in (ticket.lease_id, ticket.global_lease_id) if i is not None]
        await self.release(*ids, reason=reason)

    # ------------------------------------------------------------ inspection --
    async def active_count(
        self,
        operation: OperationClass,
        *,
        scope_id: str,
        scope_type: ScopeType = ScopeType.USER,
        now: datetime | None = None,
    ) -> int:
        """Live lease count for one scope. Operational/testing use only.

        Never reachable from the API: a caller learns its own accept/reject
        outcome and nothing about anyone else's.
        """
        now = now or datetime.now(tz=UTC)
        count = await self.s.scalar(
            text("""
                SELECT count(*) FROM admission.lease
                 WHERE scope_type     = :scope_type
                   AND scope_id       = :scope_id
                   AND operation_code = :operation
                   AND released_at IS NULL
                   AND expires_at  > :now
            """),
            {
                "scope_type": scope_type.value,
                "scope_id": scope_id,
                "operation": operation.value,
                "now": now,
            },
        )
        return int(count or 0)

    async def sweep_expired(self, *, now: datetime | None = None) -> int:
        """Mark lapsed leases EXPIRED. Housekeeping, never load-bearing.

        Admission already ignores expired leases, so quota recovery does not
        depend on this running. It exists so the table reads honestly and so the
        recovery is countable.
        """
        now = now or datetime.now(tz=UTC)
        result = await self.s.execute(
            text("""
                UPDATE admission.lease
                   SET released_at = :now, release_reason = 'EXPIRED'
                 WHERE released_at IS NULL AND expires_at <= :now
             RETURNING operation_code
            """),
            {"now": now},
        )
        rows = result.fetchall()
        for row in rows:
            LEASES_RECOVERED[str(row[0])] += 1
        return len(rows)


__all__ = [
    "ADMISSION_POLICY_VERSION",
    "GLOBAL_SCOPE_ID",
    "AdmissionRejected",
    "AdmissionService",
    "AdmissionTicket",
    "AuthAdmissionDecision",
    "admission_metrics",
    "reset_admission_metrics",
]
