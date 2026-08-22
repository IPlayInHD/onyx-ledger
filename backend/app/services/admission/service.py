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

# How long admission history is kept. See `AdmissionService.purge`.
#
# A rate window is one minute, so an hour is already two orders of magnitude
# past the point where a counter can affect a decision; the margin exists so a
# purge that misses a run does not start refusing anybody.
_COUNTER_RETENTION = timedelta(hours=2)
# A released lease is an operational record of work that ran, useful for a day
# of incident review and useless after that.
_LEASE_RETENTION = timedelta(hours=24)


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
ADMISSION_BYPASSED: Counter[str] = Counter()
LEASES_RECOVERED: Counter[str] = Counter()

#: Whether the "admission is switched off" warning has already been emitted in
#: this process. The condition must be VISIBLE — an operator who forgets to turn
#: the switch back on has removed every protection in this entry — but one line
#: per request would be its own denial of service against the log pipeline.
_bypass_announced = False


def _admission_disabled() -> bool:
    """The incident switch, read per call so it can be changed without a deploy.

    `get_settings()` is `lru_cache`d, so this costs an attribute read.
    """
    global _bypass_announced
    from app.core.config import get_settings

    if get_settings().admission_enabled:
        return False
    if not _bypass_announced:
        log.warning("admission_disabled",
                    note="every rate, concurrency and capacity limit is off")
        _bypass_announced = True
    return True


def admission_metrics() -> dict[str, dict[str, int]]:
    """Snapshot of the in-process counters. Not exported anywhere."""
    return {
        "admission_requests_total": dict(ADMISSION_REQUESTS),
        "admission_accepted_total": dict(ADMISSION_ACCEPTED),
        "admission_rejected_total": dict(ADMISSION_REJECTED),
        "admission_bypassed_total": dict(ADMISSION_BYPASSED),
        "job_lease_recovered_total": dict(LEASES_RECOVERED),
    }


def reset_admission_metrics() -> None:
    for counter in (ADMISSION_REQUESTS, ADMISSION_ACCEPTED, ADMISSION_REJECTED,
                    ADMISSION_BYPASSED, LEASES_RECOVERED):
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
class AdmissionOutcome:
    """A decision, carried OUT of the transaction that made it.

    This type exists because of a defect, and the defect is worth stating: the
    rate counter is incremented inside the caller's transaction, and raising the
    rejection from inside that transaction rolls the increment back. So the
    counter only ever recorded admissions that SUCCEEDED. A caller sitting at
    its concurrency cap was refused, its attempt was un-counted, and it could
    retry without limit — each retry paying a full advisory-lock acquisition and
    a count query while holding a pooled connection. The rate limit, whose whole
    job is to make exactly that storm cheap, never engaged.

    Measured before the fix: 50 attempts against a scope at its cap, rate
    allowance 8, left `request_count` at 2.

    So the decision is returned, the transaction commits, and the caller raises
    afterwards. `AuthAdmissionDecision` already did this for the credential
    surface in Phase 2; this is the same fix applied where it should have been
    applied then.
    """

    operation: OperationClass
    ticket: AdmissionTicket | None = None
    reason: RejectionReason | None = None
    retry_after_seconds: int = 60

    @property
    def accepted(self) -> bool:
        return self.ticket is not None

    def raise_if_rejected(self) -> AdmissionTicket:
        """The ticket, or the rejection this decision recorded."""
        if self.ticket is not None:
            return self.ticket
        if self.reason is None:  # pragma: no cover - construction invariant
            raise RuntimeError("a rejected outcome must carry a reason")
        raise AdmissionRejected(
            self.operation, self.reason, self.retry_after_seconds
        )


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
    async def charge_preauth_attempt(
        self,
        operation: OperationClass,
        *,
        source_scope_id: str,
        subject_scope_id: str,
        now: datetime | None = None,
    ) -> AuthAdmissionDecision:
        """Charge one pre-authentication attempt to BOTH scopes.

        TWO CLASSES USE THIS, and the mechanism is identical for both:
        `AUTH_ATTEMPT` for anything that presents a credential, and
        `ACCOUNT_RECOVERY` for anything that causes an email to be sent. What
        differs is only the policy — see the registry for why the numbers are
        an order of magnitude apart.

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
        policy = policy_for(operation)
        now = now or datetime.now(tz=UTC)
        ADMISSION_REQUESTS[operation.value] += 1

        if _admission_disabled():
            ADMISSION_BYPASSED[operation.value] += 1
            return AuthAdmissionDecision(
                accepted=True, retry_after_seconds=policy.retry_after_seconds
            )

        try:
            source_ok = await self._check_rate(
                policy, ScopeType.IP, source_scope_id, now=now)
            subject_ok = await self._check_rate(
                policy, ScopeType.AUTH_SUBJECT, subject_scope_id, now=now)
        except (SQLAlchemyError, DBAPIError) as exc:
            self._on_store_failure(policy, source_scope_id, exc)
            raise  # unreachable: both classes are FAIL_CLOSED, which raises above

        accepted = source_ok and subject_ok
        if accepted:
            ADMISSION_ACCEPTED[operation.value] += 1
            log.info("admission", operation=operation.value, decision="ACCEPTED")
        else:
            # Counted here, raised by the caller after the commit. The metric
            # records WHICH scope refused, because "stuffing across accounts"
            # and "guessing at one account" need different responses — but only
            # as a bounded code, never with the scope id attached.
            ADMISSION_REJECTED[
                f"{operation.value}:"
                f"{RejectionReason.AUTH_RATE_LIMIT.value}:"
                f"{'SOURCE' if not source_ok else 'SUBJECT'}"
            ] += 1
            log.info("admission", operation=operation.value,
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

        CAUTION: raising is what rolls back the rate-counter increment, so a
        rejection from here does NOT count the attempt against the caller's
        window. That is correct for a caller who owns a transaction they are
        about to abandon anyway, and wrong for anything in the request path —
        use `evaluate()` and raise after the commit, which is what
        `admission_guard` does.
        """
        return (
            await self.evaluate(
                operation, scope_id=scope_id, scope_type=scope_type,
                dedupe_key=dedupe_key, now=now,
            )
        ).raise_if_rejected()

    async def evaluate(
        self,
        operation: OperationClass,
        *,
        scope_id: str,
        scope_type: ScopeType = ScopeType.USER,
        dedupe_key: str | None = None,
        now: datetime | None = None,
    ) -> AdmissionOutcome:
        """Decide, without raising, so the decision survives the commit.

        `scope_id` MUST come from the authenticated principal. Nothing here reads
        a body field or a header for identity: a caller that could name its own
        scope could spend somebody else's budget, or evade its own by inventing
        a fresh one per request.

        A store FAILURE still raises: when the admission store cannot answer
        there is no increment worth preserving, and the transaction is already
        unusable.
        """
        policy = policy_for(operation)
        now = now or datetime.now(tz=UTC)
        ADMISSION_REQUESTS[operation.value] += 1

        # The incident switch. Checked AFTER the request is counted, so the
        # metrics still show what was asked for while the limits are off, and
        # before any statement runs, so an admission-store problem cannot be
        # what keeps the platform down.
        #
        # Deliberately does NOT disable the size and complexity bounds: those
        # are validation, they live in the handlers, and a payload that is too
        # large is malformed whether or not the platform is under strain.
        if _admission_disabled():
            ADMISSION_BYPASSED[operation.value] += 1
            return AdmissionOutcome(
                operation, ticket=AdmissionTicket(None, operation, scope_id))

        try:
            return await self._evaluate(policy, scope_id, scope_type, dedupe_key, now)
        except (SQLAlchemyError, DBAPIError) as exc:
            return AdmissionOutcome(
                operation, ticket=self._on_store_failure(policy, scope_id, exc))

    async def _evaluate(
        self,
        policy: AdmissionPolicy,
        scope_id: str,
        scope_type: ScopeType,
        dedupe_key: str | None,
        now: datetime,
    ) -> AdmissionOutcome:
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
            return self._refuse(operation, reason, policy)

        if not policy.tracks_concurrency:
            ADMISSION_ACCEPTED[operation.value] += 1
            return AdmissionOutcome(
                operation, ticket=AdmissionTicket(None, operation, scope_id))

        # 2 · an identical logical request already in flight. Checked BEFORE the
        # concurrency limits so a retry storm resolves to the running operation
        # rather than being told it hit a limit its own retries created.
        if dedupe_key is not None:
            existing = await self._find_active_duplicate(operation, dedupe_key, now=now)
            if existing is not None:
                ADMISSION_ACCEPTED[operation.value] += 1
                log.info(
                    "admission",
                    operation=operation.value,
                    decision="DUPLICATE",
                    reason=RejectionReason.DUPLICATE_ACTIVE_OPERATION.value,
                )
                return AdmissionOutcome(operation, ticket=AdmissionTicket(
                    existing, operation, scope_id, duplicate_of_active=True
                ))

        # 3 · platform capacity BEFORE the per-principal slot, so a caller with
        # quota to spare is not charged a lease the platform has no room to run.
        global_lease: uuid.UUID | None = None
        if policy.max_active_global is not None:
            global_lease = await self._acquire_lease(
                policy, ScopeType.GLOBAL, GLOBAL_SCOPE_ID,
                dedupe_key=None, now=now,
            )
            if global_lease is None:
                return self._refuse(
                    operation, RejectionReason.GLOBAL_CAPACITY, policy)

        # 4 · this principal's slot
        lease_id = await self._acquire_lease(
            policy, scope_type, scope_id, dedupe_key=dedupe_key, now=now
        )
        if lease_id is None:
            if global_lease is not None:
                # Released rather than rolled back: the transaction COMMITS now,
                # so the platform slot has to be handed back explicitly or it
                # would leak on every user-concurrency rejection.
                await self.release(global_lease, reason="CANCELLED")
            return self._refuse(
                operation, RejectionReason.USER_CONCURRENCY_LIMIT, policy)

        ADMISSION_ACCEPTED[operation.value] += 1
        log.info("admission", operation=operation.value, decision="ACCEPTED")
        # The global lease travels with the per-principal one, so releasing the
        # ticket releases both and platform capacity cannot leak while a user's
        # own quota is returned.
        return AdmissionOutcome(operation, ticket=AdmissionTicket(
            lease_id, operation, scope_id, global_lease_id=global_lease))

    def _refuse(
        self,
        operation: OperationClass,
        reason: RejectionReason,
        policy: AdmissionPolicy,
    ) -> AdmissionOutcome:
        """Record a refusal and RETURN it. Raising here is what caused the
        counter to roll back; see `AdmissionOutcome`."""
        ADMISSION_REJECTED[f"{operation.value}:{reason.value}"] += 1
        # Codes only. No scope id, no counter value, no payload — this line goes
        # to the same log stream as everything else.
        log.info("admission", operation=operation.value,
                 decision="REJECTED", reason=reason.value)
        return AdmissionOutcome(
            operation, reason=reason,
            retry_after_seconds=policy.retry_after_seconds,
        )

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

    async def purge(
        self,
        *,
        now: datetime | None = None,
        batch: int = 20_000,
    ) -> dict[str, int]:
        """Delete admission history that no decision can still depend on.

        RETENTION IS LOAD-BEARING HERE, not housekeeping.

        Before Phase 2 the counter table grew with the number of ACTIVE
        PRINCIPALS: one row per (user, operation, minute), so its cardinality
        was bounded by the size of the customer base. Throttling the login
        surface changed that. The `AUTH_SUBJECT` scope is keyed on whatever
        address the caller TYPED, so a credential-stuffing run through a million
        distinct addresses writes a million rows in a minute. The cardinality is
        now attacker-controlled, and a table an attacker can grow without bound
        is its own denial of service — the one the limiter was supposed to
        prevent, arriving through the limiter.

        Two horizons because the rows are worth different things:

          * a rate counter is dead the moment its window closes. Anything older
            than `_COUNTER_RETENTION` cannot affect a decision, and keeping it
            is keeping an attack's footprint;
          * a released lease is a small operational record of work that ran.
            Worth a day for incident review, worth nothing after that.

        Bounded per call by `batch`, so this can never become one enormous
        delete that holds locks across the hot path. Returns what it removed so
        a caller can loop until it drains.
        """
        now = now or datetime.now(tz=UTC)
        counters = await self.s.execute(
            text("""
                DELETE FROM admission.rate_counter
                 WHERE ctid IN (
                    SELECT ctid FROM admission.rate_counter
                     WHERE window_start < :cutoff
                     LIMIT :batch
                 )
             RETURNING 1
            """),
            {"cutoff": now - _COUNTER_RETENTION, "batch": batch},
        )
        leases = await self.s.execute(
            text("""
                DELETE FROM admission.lease
                 WHERE ctid IN (
                    SELECT ctid FROM admission.lease
                     WHERE released_at IS NOT NULL AND released_at < :cutoff
                     LIMIT :batch
                 )
             RETURNING 1
            """),
            {"cutoff": now - _LEASE_RETENTION, "batch": batch},
        )
        # `RETURNING 1` + fetchall rather than `rowcount`: SQLAlchemy types
        # `execute()` as `Result`, which carries no row count, and reaching for
        # the attribute anyway is exactly the kind of quietly-Any access the
        # type gate exists to stop.
        return {
            "rate_counters_deleted": len(counters.fetchall()),
            "leases_deleted": len(leases.fetchall()),
        }

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
    "AdmissionOutcome",
    "AdmissionRejected",
    "AdmissionService",
    "AdmissionTicket",
    "AuthAdmissionDecision",
    "admission_metrics",
    "reset_admission_metrics",
]
