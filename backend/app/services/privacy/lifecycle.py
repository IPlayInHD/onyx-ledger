"""The account deletion lifecycle (Entry 11B1).

WHAT THIS DOES, AND WHAT IT DOES NOT
------------------------------------
It makes a deletion request impossible to lose, bypass or forge. It deletes no
user data. Financial records, documents, frozen snapshots, scenarios,
optimizations, AI conversations, freshness and integrity history are all
untouched — those belong to later 11B phases, and the Entry 11A specification
says what must happen to each.

So an account that has requested deletion here is *frozen*, not erased: it can
no longer log in, hold a session, start expensive work or write anything, and a
durable record says a purge is owed.

ACTIVE IS THE ABSENCE OF A ROW
------------------------------
There is no `ACTIVE` value. An account with no lifecycle row is active. That
keeps the table empty for every account that never asks to be deleted, needs no
backfill, and removes the class of bug where a status column disagrees with a
request table.

WHY THE STATE MACHINE IS ENFORCED TWICE
---------------------------------------
This service is the API everything should use. The database trigger is what
makes "everything" true — a migration, a psql session, or a future repository
method cannot walk an account from DELETION_REQUESTED to COMPLETE. Two
enforcement points is not redundancy here; the second one is what the first one
would otherwise be trusting.

WHY THE WORKER USES FUNCTIONS INSTEAD OF THE TABLE
--------------------------------------------------
The worker is a cross-tenant process claiming an account it is not, which no
tenant session can do — and that refusal is correct rather than an obstacle to
route around. The runtime role has SELECT and INSERT on the lifecycle table and
NO UPDATE at all, so the only way any role can advance a lifecycle is through
the three privileged functions. Same keyhole shape as the freshness relay.
"""
from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.core.logging import get_logger
from app.domain.ports import ObjectStorage

log = get_logger("onyx.privacy.lifecycle")


class LifecycleState(StrEnum):
    """The closed set actually stored. `ACTIVE` is the absence of a row."""

    DELETION_REQUESTED = "DELETION_REQUESTED"
    ACCESS_DISABLED = "ACCESS_DISABLED"
    PURGE_PENDING = "PURGE_PENDING"
    PURGING = "PURGING"
    COMPLETE = "COMPLETE"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"


#: Where Entry 11B1 stops. Reaching it means access is off and a purge is owed;
#: it does NOT mean anything has been deleted. `COMPLETE` is deliberately out of
#: reach until a later phase implements the purge that earns it.
TERMINAL_FOR_11B1 = LifecycleState.PURGE_PENDING

#: Once a lifecycle row exists in any of these states the account is past the
#: cutoff: no login, no session, no new work, no writes.
_BLOCKING_STATES = frozenset(LifecycleState)


class LifecycleFailureCode(StrEnum):
    """Closed failure codes. Never an exception message — Entry 11A showed why
    an exception string is privacy-sensitive, and a lifecycle row is exactly
    the kind of place they would accumulate."""

    SESSION_REVOCATION_FAILED = "SESSION_REVOCATION_FAILED"
    WORKER_CLAIM_LOST = "WORKER_CLAIM_LOST"
    PHASE_RETRY_REQUIRED = "PHASE_RETRY_REQUIRED"
    ACCOUNT_STATE_INVALID = "ACCOUNT_STATE_INVALID"
    LIFECYCLE_CUTOFF_CONFLICT = "LIFECYCLE_CUTOFF_CONFLICT"


#: Account statuses that must not transact. `pending_verification` is
#: DELIBERATELY ABSENT: registration currently creates accounts `active`, email
#: verification is not implemented yet, and blocking the status before the flow
#: exists would lock out every account with no way to clear it. When the
#: verification flow ships it belongs here, and the test that pins this set is
#: where that decision has to be made explicitly rather than by drift.
BLOCKING_ACCOUNT_STATUSES = frozenset({"suspended", "closed"})


class AccountNotActive(DomainError):
    """The account exists and authenticated, but its status forbids acting.

    Separate from AccountDeletionInProgress on purpose. A suspended account is
    not a deleting one, and answering a suspended customer with "deletion in
    progress" would be a false statement about their data.

    403, for the same reason: the caller proved who they are and is refused for
    what the account is. A 401 would invite a client to retry a login that is
    also going to be refused.
    """

    error_type = "https://onyx.ledger/errors/account-not-active"
    title = "Account Not Active"
    status_code = 403

    def __init__(self) -> None:
        # Says nothing about WHICH state. "Suspended" versus "closed" is
        # operational detail, and the difference is not the customer's to
        # discover from an error body.
        super().__init__("This account cannot be used right now.")


class AccountDeletionInProgress(DomainError):
    """The account is past its deletion cutoff and may not act.

    403 rather than 401: the caller authenticated successfully and is being
    refused for what the account is, not for who they failed to be. A 401 would
    invite a client to retry the login it is also going to be refused.
    """

    error_type = "https://onyx.ledger/errors/account-deletion-in-progress"
    title = "Account Deletion In Progress"
    status_code = 403

    def __init__(self) -> None:
        # Deliberately says nothing about phase, timing or what remains: a
        # deleting account learns that it is deleting, not how the machinery is
        # getting on.
        super().__init__("This account is being deleted and can no longer be used")


# ---------------------------------------------------------------------------
# Metrics. In-process counters, like every other metric in this repository —
# there is no exporter, and claiming observability that does not exist would be
# the same error Entry 11A spent a section on. Labels are bounded codes only.
# ---------------------------------------------------------------------------
DELETION_REQUESTED: Counter[str] = Counter()
DELETION_PHASE: Counter[str] = Counter()
DELETION_CLAIM_RECOVERED: Counter[str] = Counter()


def lifecycle_metrics() -> dict[str, dict[str, int]]:
    return {
        "privacy_deletion_requested_total": dict(DELETION_REQUESTED),
        "privacy_deletion_phase_total": dict(DELETION_PHASE),
        "privacy_deletion_claim_recovered_total": dict(DELETION_CLAIM_RECOVERED),
    }


def reset_lifecycle_metrics() -> None:
    for counter in (DELETION_REQUESTED, DELETION_PHASE, DELETION_CLAIM_RECOVERED):
        counter.clear()


@dataclass(frozen=True)
class LifecycleStatus:
    """What a caller may know about its own deletion.

    Deliberately small. No worker id, no attempt count, no failure code, no
    queue position — a user learns that deletion is under way and whether it has
    finished, and nothing about the machinery.
    """

    state: LifecycleState | None
    requested_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.state is None

    @property
    def is_complete(self) -> bool:
        return self.state is LifecycleState.COMPLETE

    def as_response(self) -> dict[str, str | None]:
        if self.state is None:
            return {"status": "active"}
        return {
            "status": "deletion_complete" if self.is_complete
            else "deletion_requested",
            "requested_at": self.requested_at.isoformat()
            if self.requested_at else None,
        }


@dataclass(frozen=True)
class ClaimedLifecycle:
    """One account claimed by the deletion worker.

    Carries the cutoff, because that is what a purge phase needs in order to
    tell data that existed before the request from data that should not exist
    at all.
    """

    user_id: uuid.UUID
    state: LifecycleState
    requested_at: datetime
    claim_token: uuid.UUID
    revision: int


class AccountLifecycleService:
    """The only place a lifecycle is created or moved."""

    def __init__(self, session: AsyncSession):
        self.s = session

    # ------------------------------------------------------------- read --
    async def status(self, user_id: uuid.UUID) -> LifecycleStatus:
        """The caller's own lifecycle state. RLS scopes this to one account."""
        row = (await self.s.execute(
            text("""
                SELECT state, requested_at
                  FROM identity.account_lifecycle
                 WHERE user_id = :uid
            """),
            {"uid": user_id},
        )).first()
        if row is None:
            return LifecycleStatus(state=None, requested_at=None)
        return LifecycleStatus(
            state=LifecycleState(row[0]), requested_at=row[1]
        )

    async def assert_may_act(self, user_id: uuid.UUID) -> None:
        """THE cutoff check. One statement, on a connection already open.

        Called from the authenticated dependency, so every protected route
        inherits it rather than each one remembering. There is no cheaper place
        to put it: access tokens in this system are self-contained and the
        request path performs no account lookup at all, so lifecycle state has
        to be read from somewhere.

        IT ALSO TAKES A SHARED LOCK, and that is not incidental. Reading the
        state alone answers "was this account deleting a moment ago", which is
        not the question — the write this request is about to make happens
        later, and a deletion committed in between would leave a row dated after
        the cutoff for a purge phase to miss. The shared lock makes the two
        transactions order against each other; see `identity.lifecycle_lock_key`
        for the two interleavings and why both end truthfully.

        TWO STATEMENTS, NOT ONE. Combining them looks like a free saving and is
        wrong: under READ COMMITTED a statement's snapshot is taken when the
        statement begins, so a SELECT that waits for the lock partway through
        still reads the snapshot from before it waited. It would block for the
        deletion, then report the account active anyway. The lock has to be
        granted in its own statement, so the read that follows takes a fresh
        snapshot that includes whatever committed while it waited.

        This was not theoretical — the single-statement version blocked
        correctly and then admitted the write, which reached admission and came
        back as 429 instead of 403.
        """
        await self.s.execute(
            text("SELECT pg_advisory_xact_lock_shared("
                 "identity.lifecycle_lock_key(:uid))"),
            {"uid": user_id},
        )
        row = (
            await self.s.execute(
                text(
                    "SELECT a.status, (a.deleted_at IS NOT NULL) AS is_deleted, l.state "
                    "FROM identity.user_account a "
                    "LEFT JOIN identity.account_lifecycle l ON l.user_id = a.id "
                    "WHERE a.id = :uid"
                ),
                {"uid": user_id},
            )
        ).one_or_none()

        # No row at all: the account was hard-removed while a token for it is
        # still inside its lifetime. Deny — an access token is a claim about who
        # the caller is, never evidence that the account still exists.
        if row is None:
            raise AccountNotActive()

        status, is_deleted, state = row

        # ORDER MATTERS. Deletion is checked first so an account that is both
        # closed and deleting still gets the deletion answer, which is the more
        # specific and the more actionable of the two.
        if state is not None and LifecycleState(state) in _BLOCKING_STATES:
            raise AccountDeletionInProgress()

        if is_deleted or status in BLOCKING_ACCOUNT_STATUSES:
            raise AccountNotActive()

    # ---------------------------------------------------------- request --
    async def request_deletion(self, user_id: uuid.UUID) -> LifecycleStatus:
        """Start the lifecycle. Idempotent.

        Everything in here commits together: the lifecycle row, the revoked
        sessions and the audit events. A deletion that disabled access without
        recording why, or recorded a request without disabling access, would be
        a worse state than either outcome.

        Idempotency is the primary key, not a check-then-insert. Twenty
        concurrent requests all attempt the insert; nineteen get a unique
        violation and read back the row the twentieth created.

        THE EXCLUSIVE LOCK IS ABOUT WRITES IN FLIGHT, NOT ABOUT DUPLICATES.
        Duplicate requests were already handled by the primary key. What the
        primary key cannot do is order this request against a financial write
        that started before it and has not committed yet: without the lock, that
        write lands after the cutoff and a purge bounded on `requested_at`
        leaves it behind. Taking the lock exclusively means the insert happens
        either strictly before such a write is admitted, or strictly after it
        has finished — and `requested_at` defaults to `clock_timestamp()` so it
        records when the lock was granted rather than when this transaction
        began waiting for it.
        """
        await self.s.execute(
            text("SELECT pg_advisory_xact_lock(identity.lifecycle_lock_key(:uid))"),
            {"uid": user_id},
        )
        try:
            async with self.s.begin_nested():
                await self.s.execute(
                    text("""
                        INSERT INTO identity.account_lifecycle
                            (user_id, state)
                        VALUES (:uid, 'DELETION_REQUESTED')
                    """),
                    {"uid": user_id},
                )
            created = True
        except IntegrityError:
            created = False

        if not created:
            # Already deleting. Deterministic, no duplicate work, no second
            # revocation storm.
            return await self.status(user_id)

        DELETION_REQUESTED["requested"] += 1
        await self._record(user_id, "DELETION_REQUESTED",
                           to_state=LifecycleState.DELETION_REQUESTED)

        revoked = await self._revoke_sessions(user_id)
        await self._record(user_id, "SESSIONS_REVOKED")
        log.info("privacy.deletion_requested", sessions_revoked=revoked)

        return await self.status(user_id)

    async def _revoke_sessions(self, user_id: uuid.UUID) -> int:
        """Kill every refresh session the account holds.

        Access tokens are self-contained and cannot be recalled — they expire on
        their own — which is exactly why `assert_may_act` exists and why it runs
        on the authenticated dependency rather than only at login. Revoking here
        closes the refresh path so a live token cannot be renewed into a new one.
        """
        result = await self.s.execute(
            text("""
                UPDATE identity.auth_session
                   SET revoked_at = now()
                 WHERE user_id = :uid AND revoked_at IS NULL
             RETURNING 1
            """),
            {"uid": user_id},
        )
        return len(result.fetchall())

    async def _record(
        self,
        user_id: uuid.UUID,
        event_code: str,
        *,
        from_state: LifecycleState | None = None,
        to_state: LifecycleState | None = None,
        reason_code: str | None = None,
    ) -> None:
        await self.s.execute(
            text("""
                INSERT INTO identity.account_lifecycle_event
                    (user_id, event_code, from_state, to_state, reason_code)
                VALUES (:uid, :code, :from_state, :to_state, :reason)
            """),
            {
                "uid": user_id, "code": event_code,
                "from_state": from_state.value if from_state else None,
                "to_state": to_state.value if to_state else None,
                "reason": reason_code,
            },
        )

    # ----------------------------------------------------------- worker --
    async def claim(self, *, worker_id: str, batch_size: int = 10
                    ) -> list[ClaimedLifecycle]:
        """Claim accounts needing lifecycle work.

        Goes through the privileged function, which also releases claims older
        than the timeout — so a worker that died mid-phase strands nothing.
        """
        rows = (await self.s.execute(
            text("""
                SELECT out_user_id, out_state, out_requested_at,
                       out_claim_token, out_revision
                  FROM identity.claim_account_lifecycle(:batch, :worker)
            """),
            {"batch": batch_size, "worker": worker_id},
        )).all()
        claimed = [
            ClaimedLifecycle(
                user_id=row[0], state=LifecycleState(row[1]),
                requested_at=row[2], claim_token=row[3], revision=row[4],
            )
            for row in rows
        ]
        for item in claimed:
            DELETION_PHASE[f"{item.state.value}:claimed"] += 1
        return claimed

    async def advance(
        self,
        claimed: ClaimedLifecycle,
        next_state: LifecycleState,
        *,
        worker_id: str,
    ) -> bool:
        """Move a claimed account forward. False if the claim was lost.

        `COMPLETE` is refused here as well as by the trigger. Entry 11B1
        implements no purge, so nothing in this codebase has earned the right to
        say an account's data is gone — and a status that lies in that direction
        is worse than one that lies in the other.
        """
        if next_state is LifecycleState.COMPLETE:
            raise ValueError(
                "no phase in Entry 11B1 may mark a lifecycle COMPLETE; the "
                "purge that would justify it is not implemented"
            )
        ok = await self.s.scalar(
            text("""
                SELECT identity.advance_account_lifecycle(
                    :uid, :token, :next, :worker)
            """),
            {"uid": claimed.user_id, "token": claimed.claim_token,
             "next": next_state.value, "worker": worker_id},
        )
        DELETION_PHASE[f"{next_state.value}:{'completed' if ok else 'claim_lost'}"] += 1
        return bool(ok)

    async def fail(
        self,
        claimed: ClaimedLifecycle,
        reason: LifecycleFailureCode,
        *,
        worker_id: str,
    ) -> bool:
        ok = await self.s.scalar(
            text("""
                SELECT identity.fail_account_lifecycle(
                    :uid, :token, :reason, :worker)
            """),
            {"uid": claimed.user_id, "token": claimed.claim_token,
             "reason": reason.value, "worker": worker_id},
        )
        DELETION_PHASE[f"failed:{reason.value}"] += 1
        return bool(ok)


__all__ = [
    "TERMINAL_FOR_11B1",
    "AccountDeletionInProgress",
    "AccountLifecycleService",
    "ClaimedLifecycle",
    "LifecycleFailureCode",
    "LifecycleState",
    "LifecycleStatus",
    "lifecycle_metrics",
    "reset_lifecycle_metrics",
    "AuditAuthDeidentificationService",
    "ScenarioRetentionService",
    "DocumentPurgeService",
    "phase_is_complete",
]


class SourceDataPhase(StrEnum):
    """The closed set of privacy phases with durable progress (Entry 11B5).

    All five are implemented: `SOURCE_DATA`, `DOCUMENTS` (11B6F),
    `SCENARIO_RETENTION` (11B6E), `HISTORICAL_DETAIL_CLEANUP` (11B6I) and
    `AUDIT_AUTH_DEIDENTIFICATION` (11B6D). A dispatcher that branched on
    free-form strings would let a typo become a silently skipped phase, which is
    why this is a closed set and the phase table's CHECK names the same values.
    """

    SOURCE_DATA = "SOURCE_DATA"
    DOCUMENTS = "DOCUMENTS"
    AUDIT_AUTH_DEIDENTIFICATION = "AUDIT_AUTH_DEIDENTIFICATION"
    SCENARIO_RETENTION = "SCENARIO_RETENTION"
    HISTORICAL_DETAIL_CLEANUP = "HISTORICAL_DETAIL_CLEANUP"


@dataclass(frozen=True)
class PhaseOutcome:
    """What one phase run achieved. Identifiers and closed codes only — no
    table contents, no counts of anything but rows, no exception text."""

    user_id: uuid.UUID
    phase: SourceDataPhase
    completed: bool
    remaining: int
    failure_code: str | None = None


class SourceDataPurgeService:
    """Drives the SOURCE_DATA phase through the Entry 11B5C keyhole.

    Owns NO deletion SQL. Every statement that removes a row lives in
    `identity.purge_source_data`, which takes one subject, is authorised by the
    claim token, and executes under the subject's own row-level security. This
    class decides WHEN to call it and what to believe afterwards.

    THE COMPLETION CONDITION IS NOT WHAT THE DELETE REPORTED. `purge_source_data`
    returns per-table row counts, and they are useful for an operator and
    worthless as proof: a table the purge forgot reports nothing at all, which
    looks identical to a table that was already empty. Completion is decided by
    `count_remaining_source_data`, and the database refuses to record it while
    that count is non-zero — so a bug here cannot assert the phase done.
    """

    def __init__(self, session: AsyncSession):
        self.s = session

    async def run(
        self, claimed: ClaimedLifecycle, *, worker_id: str,
        phase: SourceDataPhase = SourceDataPhase.SOURCE_DATA,
    ) -> PhaseOutcome:
        started = await self.s.scalar(
            text("SELECT identity.start_lifecycle_phase(:u, :p, :t, :w)"),
            {"u": claimed.user_id, "p": phase.value,
             "t": claimed.claim_token, "w": worker_id},
        )
        if not started:
            # The claim expired and someone else holds the subject. Not a
            # failure: the other worker is doing this work.
            return PhaseOutcome(claimed.user_id, phase, completed=False,
                                remaining=-1, failure_code="CLAIM_LOST")

        await self.s.execute(
            text("SELECT identity.purge_source_data(:u, :t, :w)"),
            {"u": claimed.user_id, "t": claimed.claim_token, "w": worker_id},
        )

        remaining = int(await self.s.scalar(
            text("SELECT identity.count_remaining_source_data(:u)"),
            {"u": claimed.user_id},
        ) or 0)
        if remaining:
            # Refuse rather than let the database refuse for us. Both refuse —
            # `complete_lifecycle_phase` raises on a non-zero count — but a
            # worker that only found out by catching an exception would have no
            # way to distinguish "work remains" from "something broke".
            await self.s.execute(
                text("SELECT identity.fail_lifecycle_phase(:u, :p, :t, :c, :w)"),
                {"u": claimed.user_id, "p": phase.value,
                 "t": claimed.claim_token, "c": "SOURCE_DATA_INCOMPLETE",
                 "w": worker_id},
            )
            DELETION_PHASE[f"{phase.value}:incomplete"] += 1
            return PhaseOutcome(claimed.user_id, phase, completed=False,
                                remaining=remaining,
                                failure_code="SOURCE_DATA_INCOMPLETE")

        completed = bool(await self.s.scalar(
            text("SELECT identity.complete_lifecycle_phase(:u, :p, :t, :w)"),
            {"u": claimed.user_id, "p": phase.value,
             "t": claimed.claim_token, "w": worker_id},
        ))
        DELETION_PHASE[f"{phase.value}:{'completed' if completed else 'claim_lost'}"] += 1
        return PhaseOutcome(claimed.user_id, phase, completed=completed,
                            remaining=0)


class AuditAuthDeidentificationService:
    """Drives AUDIT_AUTH_DEIDENTIFICATION through the Entry 11B6 keyhole.

    Owns NO mutation SQL, exactly like `SourceDataPurgeService`. Everything that
    changes a row lives in `identity.deidentify_audit_auth`, which takes one
    subject, is authorised by the claim token, and runs as the function owner
    because the tables it touches are ones no application role may write.

    WHAT THE PHASE MEANS. Every account-to-retained-history identity mapping
    that must be severed before the account row can go has been severed, and
    the authoritative remaining count is zero. It does NOT mean audit history
    was deleted, security history was deleted, or historical actor ids were
    rewritten — none of those happen, and the append-only audit log is never
    touched at all.

    COMPLETION IS NOT WHAT THE KEYHOLE REPORTED. The keyhole returns per-table
    counts, which are useful to an operator and worthless as proof: a table it
    forgot reports nothing, which looks identical to a table that had nothing.
    Completion is decided by `identity.count_attributable_audit_auth`, and since
    0061 the database refuses to record the phase while that count is non-zero.

    IDEMPOTENCY WITHOUT A REVERSE MAP. A second run finds no
    `identity.account_subject` row, returns zeros and changes nothing. The
    ABSENCE of the mapping is the durable record that this was done — storing a
    "already de-identified" marker keyed by account would rebuild the very link the
    phase exists to destroy.
    """

    def __init__(self, session: AsyncSession):
        self.s = session

    async def run(
        self, claimed: ClaimedLifecycle, *, worker_id: str,
    ) -> PhaseOutcome:
        phase = SourceDataPhase.AUDIT_AUTH_DEIDENTIFICATION
        started = await self.s.scalar(
            text("SELECT identity.start_lifecycle_phase(:u, :p, :t, :w)"),
            {"u": claimed.user_id, "p": phase.value,
             "t": claimed.claim_token, "w": worker_id},
        )
        if not started:
            return PhaseOutcome(claimed.user_id, phase, completed=False,
                                remaining=-1, failure_code="CLAIM_LOST")

        await self.s.execute(
            text("SELECT identity.deidentify_audit_auth(:u, :t, :w)"),
            {"u": claimed.user_id, "t": claimed.claim_token, "w": worker_id},
        )

        remaining = int(await self.s.scalar(
            text("SELECT identity.count_attributable_audit_auth(:u)"),
            {"u": claimed.user_id},
        ) or 0)
        if remaining:
            # Refuse here rather than let `complete_lifecycle_phase` raise. Both
            # refuse, but a worker that only learned by catching an exception
            # could not tell "work remains" from "something broke".
            await self.s.execute(
                text("SELECT identity.fail_lifecycle_phase(:u, :p, :t, :c, :w)"),
                {"u": claimed.user_id, "p": phase.value,
                 "t": claimed.claim_token, "c": "AUDIT_AUTH_INCOMPLETE",
                 "w": worker_id},
            )
            DELETION_PHASE[f"{phase.value}:incomplete"] += 1
            return PhaseOutcome(claimed.user_id, phase, completed=False,
                                remaining=remaining,
                                failure_code="AUDIT_AUTH_INCOMPLETE")

        completed = bool(await self.s.scalar(
            text("SELECT identity.complete_lifecycle_phase(:u, :p, :t, :w)"),
            {"u": claimed.user_id, "p": phase.value,
             "t": claimed.claim_token, "w": worker_id},
        ))
        DELETION_PHASE[f"{phase.value}:{'completed' if completed else 'claim_lost'}"] += 1
        return PhaseOutcome(claimed.user_id, phase, completed=completed,
                            remaining=0)


class ScenarioRetentionService:
    """Drives SCENARIO_RETENTION through the Entry 11B6E keyhole.

    Owns no SQL that changes a row, like its two siblings. Everything happens
    inside `identity.prepare_scenario_retention`, which takes one subject and
    is authorised by the claim token.

    WHAT THE PHASE MEANS. Migration 0060 detached `ioe.scenario` from the
    account so that sealed scenarios survive its removal; it has no way to tell
    a sealed scenario from a draft, so drafts survive too. This deletes the
    scenarios that never sealed a result and clears the free text on the ones
    that did, which is what makes DEIDENTIFY_THEN_RETAIN a real treatment
    rather than a label.

    WHAT IT DOES NOT MEAN. The retained rows keep their historical `user_id`,
    and while `identity.user_account` still holds that account the row is still
    attributable to it. This is preparation for terminal removal, not
    de-identification that has already happened.

    COMPLETION IS NOT WHAT THE KEYHOLE REPORTED, for the third time in this
    module and for the same reason: the keyhole reports what it changed, which
    says nothing about what it missed.
    `identity.count_remaining_scenario_privacy_work` counts both obligations,
    and 0062 makes the database refuse the completion while either is non-zero.
    """

    def __init__(self, session: AsyncSession):
        self.s = session

    async def run(
        self, claimed: ClaimedLifecycle, *, worker_id: str,
    ) -> PhaseOutcome:
        phase = SourceDataPhase.SCENARIO_RETENTION
        started = await self.s.scalar(
            text("SELECT identity.start_lifecycle_phase(:u, :p, :t, :w)"),
            {"u": claimed.user_id, "p": phase.value,
             "t": claimed.claim_token, "w": worker_id},
        )
        if not started:
            return PhaseOutcome(claimed.user_id, phase, completed=False,
                                remaining=-1, failure_code="CLAIM_LOST")

        await self.s.execute(
            text("SELECT identity.prepare_scenario_retention(:u, :t, :w)"),
            {"u": claimed.user_id, "t": claimed.claim_token, "w": worker_id},
        )

        remaining = int(await self.s.scalar(
            text("SELECT identity.count_remaining_scenario_privacy_work(:u)"),
            {"u": claimed.user_id},
        ) or 0)
        if remaining:
            await self.s.execute(
                text("SELECT identity.fail_lifecycle_phase(:u, :p, :t, :c, :w)"),
                {"u": claimed.user_id, "p": phase.value,
                 "t": claimed.claim_token, "c": "SCENARIO_RETENTION_INCOMPLETE",
                 "w": worker_id},
            )
            DELETION_PHASE[f"{phase.value}:incomplete"] += 1
            return PhaseOutcome(claimed.user_id, phase, completed=False,
                                remaining=remaining,
                                failure_code="SCENARIO_RETENTION_INCOMPLETE")

        completed = bool(await self.s.scalar(
            text("SELECT identity.complete_lifecycle_phase(:u, :p, :t, :w)"),
            {"u": claimed.user_id, "p": phase.value,
             "t": claimed.claim_token, "w": worker_id},
        ))
        DELETION_PHASE[f"{phase.value}:{'completed' if completed else 'claim_lost'}"] += 1
        return PhaseOutcome(claimed.user_id, phase, completed=completed,
                            remaining=0)


class HistoricalDetailCleanupService:
    """Drives HISTORICAL_DETAIL_CLEANUP through the Entry 11B6I keyhole.

    THE PHASE THE OTHER FOUR LEFT BEHIND. Source data, documents, unsealed
    scenario text and the attributable audit trail all have a phase. The derived
    detail hanging off the sealed roots had none, so fifteen tables of per-person
    tax computation survived every phase and — since 0060 detached the roots from
    `identity.user_account` — would outlive the account carrying a `user_id` that
    resolves to nobody.

    WHAT IT MUST NEVER TOUCH. Entry 11B6H proved that integrity verification
    READS eight of the sealed-detail tables and rebuilds the hashed canonical
    form from their rows. The keyhole therefore names its fifteen tables
    explicitly and never walks a parent: walking `ioe.optimization_run` would
    reach `ioe.portfolio_member`, and the result would be a mismatch
    indistinguishable from tampering.

    Owns no SQL that changes a row, like all four siblings. Everything happens
    inside `identity.purge_historical_detail`, which takes one subject and is
    authorised by the claim token.

    COMPLETION IS NOT WHAT THE KEYHOLE REPORTED, for the fourth time in this
    module and for the same reason: the keyhole reports what it changed, which
    says nothing about what it missed.
    `identity.count_remaining_historical_detail` counts every group, and 0064
    makes the database refuse the completion while it is non-zero.
    """

    def __init__(self, session: AsyncSession):
        self.s = session

    async def run(
        self, claimed: ClaimedLifecycle, *, worker_id: str,
    ) -> PhaseOutcome:
        phase = SourceDataPhase.HISTORICAL_DETAIL_CLEANUP
        started = await self.s.scalar(
            text("SELECT identity.start_lifecycle_phase(:u, :p, :t, :w)"),
            {"u": claimed.user_id, "p": phase.value,
             "t": claimed.claim_token, "w": worker_id},
        )
        if not started:
            return PhaseOutcome(claimed.user_id, phase, completed=False,
                                remaining=-1, failure_code="CLAIM_LOST")

        await self.s.execute(
            text("SELECT * FROM identity.purge_historical_detail(:u, :t, :w)"),
            {"u": claimed.user_id, "t": claimed.claim_token, "w": worker_id},
        )

        remaining = int(await self.s.scalar(
            text("SELECT identity.count_remaining_historical_detail(:u)"),
            {"u": claimed.user_id},
        ) or 0)
        if remaining:
            await self.s.execute(
                text("SELECT identity.fail_lifecycle_phase(:u, :p, :t, :c, :w)"),
                {"u": claimed.user_id, "p": phase.value,
                 "t": claimed.claim_token, "c": "HISTORICAL_DETAIL_INCOMPLETE",
                 "w": worker_id},
            )
            DELETION_PHASE[f"{phase.value}:incomplete"] += 1
            return PhaseOutcome(claimed.user_id, phase, completed=False,
                                remaining=remaining,
                                failure_code="HISTORICAL_DETAIL_INCOMPLETE")

        completed = bool(await self.s.scalar(
            text("SELECT identity.complete_lifecycle_phase(:u, :p, :t, :w)"),
            {"u": claimed.user_id, "p": phase.value,
             "t": claimed.claim_token, "w": worker_id},
        ))
        DELETION_PHASE[f"{phase.value}:{'completed' if completed else 'claim_lost'}"] += 1
        return PhaseOutcome(claimed.user_id, phase, completed=completed,
                            remaining=0)


async def phase_is_complete(
    session: AsyncSession, user_id: uuid.UUID, phase: SourceDataPhase
) -> bool:
    """Has this subject already finished `phase`?

    The dispatcher needs this to know which phase a claimed account is owed,
    read from the durable record rather than from anything the current process
    remembers — the process that ran the previous phase may have been a
    different one that has since died.

    Through a function, not the table. `onyx_privacy_worker` holds EXECUTE on
    the lifecycle verbs and no privilege on `identity.account_lifecycle_phase`;
    the first version of this read the table directly and was refused. That
    refusal is the keyhole working, so the question got its own narrow verb.
    """
    status: str | None = await session.scalar(
        text("SELECT identity.lifecycle_phase_status(:u, :p)"),
        {"u": user_id, "p": phase.value},
    )
    return status == "COMPLETE"


class DocumentPurgeService:
    """Drives DOCUMENTS: every binary gone, every extraction row gone.

    THE ONLY PHASE WITH TWO STORAGE SYSTEMS, and that is what shapes it.
    PostgreSQL and the object store are not one transaction, so one commits
    first and a crash between them is possible. `delete_document` settled the
    ordering for a single document in Entry 11B4 — the binary goes first,
    because it is the irreversible part with no transaction to roll back, and a
    crash after it leaves a live row whose retry converges. The reverse order
    would leave a binary nobody can find again.

    So this service walks the account one document at a time and keeps that
    ordering per document, rather than doing all the storage work and then all
    the database work. A crash anywhere leaves either an untouched document or
    a live row whose binary is already gone, and both converge on retry.

    IT OWNS NO SQL THAT CHANGES A ROW. The two keyholes are account-scoped and
    claim-authorised, and `onyx_privacy_worker` holds no DELETE on the document
    tables — so "delete every account's documents" is not expressible by the
    role that runs this.

    WHAT SURVIVES, unchanged from Entry 11A §8: the content hash, which proves
    which document was deleted; the opaque object key, which distinguishes
    deleted-on-purpose from vanished; the provenance edge in
    `docs.document_link`, so a confirmed figure never looks unsourced; and the
    confirmed facts themselves, which the user asserted and which are the tax
    input.
    """

    #: One pass takes at most this many documents. A phase that claimed a
    #: 10 000-document account in one lease would hold it for the whole storage
    #: pass; the remainder is picked up by the next run, and the guard keeps the
    #: phase incomplete until there is none.
    BATCH = 500

    def __init__(self, session: AsyncSession, storage: ObjectStorage | None = None):
        self.s = session
        #: Injectable so a test can make the provider fail. Resolved lazily
        #: otherwise, because importing the adapter at module import time would
        #: drag settings into every consumer of this module.
        self._storage = storage

    @property
    def storage(self) -> ObjectStorage:
        if self._storage is None:
            from app.integrations.storage import get_object_storage

            self._storage = get_object_storage()
        return self._storage

    async def run(
        self, claimed: ClaimedLifecycle, *, worker_id: str,
    ) -> PhaseOutcome:
        from app.integrations.storage import DeleteOutcome

        phase = SourceDataPhase.DOCUMENTS
        started = await self.s.scalar(
            text("SELECT identity.start_lifecycle_phase(:u, :p, :t, :w)"),
            {"u": claimed.user_id, "p": phase.value,
             "t": claimed.claim_token, "w": worker_id},
        )
        if not started:
            return PhaseOutcome(claimed.user_id, phase, completed=False,
                                remaining=-1, failure_code="CLAIM_LOST")

        outstanding = (await self.s.execute(
            text("SELECT * FROM identity.list_account_documents_for_purge"
                 "(:u, :t, :w, :n)"),
            {"u": claimed.user_id, "t": claimed.claim_token,
             "w": worker_id, "n": self.BATCH},
        )).all()

        failure: str | None = None
        for document_id, bucket, object_key in outstanding:
            result = self.storage.delete(bucket, object_key)
            if result not in (DeleteOutcome.DELETED, DeleteOutcome.ALREADY_ABSENT):
                # One object that will not go must not stop the others, and
                # must not let the phase finish. The closed outcome name is the
                # only thing recorded — never the provider's message, which
                # Entry 11A proved carries values a privacy store must not keep.
                failure = f"STORAGE_{result.name}"
                DELETION_PHASE[f"{phase.value}:storage_{result.name.lower()}"] += 1
                # The phase row already records the failure code durably; this
                # is the operational half — an erasure that needs another run is
                # the one privacy event nobody should have to query for.
                #
                # The document id identifies WHICH object is stuck, which an
                # operator needs. No bucket, no key, no subject: a key is
                # `{user_id}/v2/{document_id}`, so logging it would put an
                # account identifier in the log line.
                log.warning(
                    "privacy_erasure_retry_required",
                    phase=phase.value,
                    document_id=str(document_id),
                    outcome=result.name,
                )
                continue
            await self.s.execute(
                text("SELECT identity.finalize_document_purge(:u, :d, :t, :w)"),
                {"u": claimed.user_id, "d": document_id,
                 "t": claimed.claim_token, "w": worker_id},
            )

        remaining = int(await self.s.scalar(
            text("SELECT identity.count_remaining_document_privacy_work(:u)"),
            {"u": claimed.user_id},
        ) or 0)
        if remaining:
            code = failure or "DOCUMENTS_INCOMPLETE"
            await self.s.execute(
                text("SELECT identity.fail_lifecycle_phase(:u, :p, :t, :c, :w)"),
                {"u": claimed.user_id, "p": phase.value,
                 "t": claimed.claim_token, "c": code, "w": worker_id},
            )
            DELETION_PHASE[f"{phase.value}:incomplete"] += 1
            return PhaseOutcome(claimed.user_id, phase, completed=False,
                                remaining=remaining, failure_code=code)

        completed = bool(await self.s.scalar(
            text("SELECT identity.complete_lifecycle_phase(:u, :p, :t, :w)"),
            {"u": claimed.user_id, "p": phase.value,
             "t": claimed.claim_token, "w": worker_id},
        ))
        DELETION_PHASE[f"{phase.value}:{'completed' if completed else 'claim_lost'}"] += 1
        return PhaseOutcome(claimed.user_id, phase, completed=completed,
                            remaining=0)
