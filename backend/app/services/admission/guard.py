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

from app.database.session import unit_of_work
from app.services.admission.policy import OperationClass, ScopeType
from app.services.admission.service import (
    AdmissionService,
    AdmissionTicket,
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
    """
    async with unit_of_work(actor_type="system") as session:
        ticket = await AdmissionService(session).admit(
            operation, scope_id=scope_id, scope_type=scope_type, dedupe_key=dedupe_key
        )

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
