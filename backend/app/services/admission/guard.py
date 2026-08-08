"""`admission_guard` — the boundary expensive work is wrapped in.

WHY THE LEASE GETS ITS OWN TRANSACTION
--------------------------------------
The tempting shape is to take the lease inside the transaction that does the
work. It does not function: an uncommitted INSERT is invisible to every other
connection, so ten API replicas would each count zero live leases and each admit,
and the limit would hold only against a single-process test.

So the lease is committed by itself, immediately, before the work starts — and
released in another short transaction afterwards. The cost is that a crash
between the two leaves a lease behind, which is exactly what `expires_at` is for:
recovery is a property of time, not of cleanup code having run.

The guard therefore never holds a transaction open across an engine run. That
also keeps a long optimization from pinning a connection out of the pool, which
would be its own denial-of-service.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import unit_of_work
from app.services.admission.policy import (
    OperationClass,
    RejectionReason,
    ScopeType,
    policy_for,
)
from app.services.admission.service import (
    AdmissionRejected,
    AdmissionService,
    AdmissionTicket,
)


async def _refuse_if_account_deleting(
    session: AsyncSession, operation: OperationClass, scope_id: str
) -> None:
    """Refuse new expensive work for an account past its deletion cutoff.

    Reads through the privileged state function rather than the table: this is
    a `system` unit of work with no `app.user_id`, so the lifecycle row is
    correctly invisible to RLS and a direct read would silently see nothing —
    the same trap the login path has.
    """
    state = await session.scalar(
        text("SELECT identity.account_deletion_state(cast(:uid AS uuid))"),
        {"uid": scope_id},
    )
    if state is not None:
        raise AdmissionRejected(
            operation,
            RejectionReason.ACCOUNT_DELETION_IN_PROGRESS,
            policy_for(operation).retry_after_seconds,
        )


@asynccontextmanager
async def admission_guard(
    operation: OperationClass,
    *,
    scope_id: str,
    scope_type: ScopeType = ScopeType.USER,
    dedupe_key: str | None = None,
) -> AsyncIterator[AdmissionTicket]:
    """Admit, run the body, release. Raises `AdmissionRejected` before the body.

    `scope_id` must be the AUTHENTICATED principal. Never a body field, never a
    header: a caller able to name its own scope could spend another principal's
    budget or mint a fresh one per request and have no budget at all.

    The release runs in a `finally`, so a failing operation returns its slot
    rather than holding it until expiry.

    THE REJECTION IS RAISED AFTER THE COMMIT, not inside it. Raising from inside
    the transaction rolls back the rate-counter increment the decision was based
    on, so a caller being refused never accumulated any rate-limit budget and
    could retry forever — paying a full advisory-lock acquisition each time
    while holding a pooled connection. See `AdmissionOutcome`.
    """
    async with unit_of_work(actor_type="system") as session:
        # The privacy cutoff comes FIRST, and is not a rate decision. A deleting
        # account must not consume its own allowance to be told it may not act,
        # and the rejection must not appear in the abuse metrics as though the
        # caller had misbehaved.
        #
        # `db_authed` already refuses every authenticated route for such an
        # account, so this is the second boundary rather than the only one — it
        # covers admission taken from a worker, where no HTTP dependency ran.
        if scope_type is ScopeType.USER:
            await _refuse_if_account_deleting(session, operation, scope_id)

        outcome = await AdmissionService(session).evaluate(
            operation, scope_id=scope_id, scope_type=scope_type, dedupe_key=dedupe_key
        )
    ticket = outcome.raise_if_rejected()

    try:
        yield ticket
        reason = "COMPLETED"
    except BaseException:
        reason = "FAILED"
        raise
    finally:
        if ticket.holds_lease:
            async with unit_of_work(actor_type="system") as session:
                await AdmissionService(session).release_ticket(ticket, reason=reason)


def user_scope(user_id: uuid.UUID) -> str:
    """The scope key for a user principal.

    In this product the user IS the tenant — there is no organization entity and
    row-level security is keyed on `app.user_id`. Routed through one function so
    that if an organization tier is ever added, every call site changes together.
    """
    return str(user_id)


def admin_scope(admin_id: uuid.UUID) -> str:
    """The scope key for an operator principal.

    A separate function from `user_scope` even though both stringify a UUID,
    because they are different populations with different budgets, and the pair
    (`ScopeType.ADMIN`, this id) is what keeps an operator's expensive work off
    a tenant's ledger. Keyed on the ACTING OPERATOR, never on the entity being
    operated on: keying a publish limit on the rule version would give an
    operator a fresh allowance for every version they touched.
    """
    return str(admin_id)


def owned_dedupe_key(user_id: uuid.UUID, *parts: str) -> str:
    """A dedupe key that is always scoped to its owner.

    The user id is a PREFIX, not a component the caller can influence. A client
    that supplies its own idempotency string still cannot construct a key that
    collides with another account's, so a key can never be used to reach into
    somebody else's running operation.
    """
    return ":".join([str(user_id), *parts])


__all__ = ["admin_scope", "admission_guard", "owned_dedupe_key", "user_scope"]
