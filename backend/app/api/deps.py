"""FastAPI dependencies — DB unit of work bound to the authenticated principal.

The access token identifies the user; the UoW sets `app.user_id` so PostgreSQL
RLS and the audit triggers see the actor.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Unauthorized
from app.core.security.jwt import decode_access_token, decode_admin_token
from app.database.session import unit_of_work

_bearer = HTTPBearer(auto_error=False)


async def current_user_id(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> uuid.UUID:
    if creds is None or not creds.credentials:
        raise Unauthorized("Missing bearer token")
    payload = decode_access_token(creds.credentials)
    return uuid.UUID(payload["sub"])


async def db_authed(user_id: uuid.UUID = Depends(current_user_id)) -> AsyncIterator[AsyncSession]:
    """The authenticated session, with the account-deletion cutoff applied.

    THE central lifecycle boundary. Putting it here rather than on each route is
    the difference between a rule and a habit: every protected endpoint in the
    application inherits it, including ones written after this dependency, and
    a new route cannot forget to check.

    It costs one statement on a connection that is already open. There is no
    cheaper place — access tokens in this system are self-contained and the
    request path performs no account lookup at all, so lifecycle state has to be
    read from somewhere, and this is the only somewhere every route passes
    through.

    Routes that must keep working for a deleting account — asking for deletion
    again, reading its status — use `db_authed_lifecycle_exempt`.
    """
    from app.services.privacy.lifecycle import AccountLifecycleService

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        await AccountLifecycleService(session).assert_may_act(user_id)
        yield session


async def db_authed_unverified_ok(
    user_id: uuid.UUID = Depends(current_user_id),
) -> AsyncIterator[AsyncSession]:
    """For the endpoints that EXIST to clear the unverified state.

    `db_authed` refuses an account that has not confirmed its address, which is
    the point of it — but the resend endpoint is the one thing such an account
    must be able to reach, and refusing it would make the state permanent for
    anyone whose first link expired.

    This relaxes EXACTLY that check. The deletion cutoff still applies, and so
    do suspension and closure: an unverified account that is also suspended is
    still refused here, because a verification link must never be the thing
    that brings an account back.
    """
    from app.services.privacy.lifecycle import AccountLifecycleService

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        await AccountLifecycleService(session).assert_may_act(
            user_id, require_verified=False
        )
        yield session


async def db_authed_lifecycle_exempt(
    user_id: uuid.UUID = Depends(current_user_id),
) -> AsyncIterator[AsyncSession]:
    """For the deletion endpoints themselves.

    Exempt, not unauthenticated: the caller is still identified and still
    scoped by RLS to their own account. Without this, requesting deletion twice
    would be refused by the cutoff the first request installed, and a user could
    never read the status of the thing they asked for.
    """
    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        yield session


async def assert_account_active(user_id: uuid.UUID = Depends(current_user_id)) -> None:
    """The cutoff for routes that do NOT take a session dependency.

    Most endpoints receive their unit of work from `db_authed` and inherit the
    check with it. A few open their own inside a service instead — the scenario
    routes do — and those never pass through `db_authed`, so they inherited
    nothing. Three of them wrote user data: archiving and unarchiving a
    scenario, and reading one, which persists the freshness transition it just
    evaluated.

    This is the same check in the shape those routes can use. It costs a short
    unit of work of its own, which is the price of the handler not having one to
    borrow; that is why it is not simply applied everywhere.
    """
    from app.services.privacy.lifecycle import AccountLifecycleService

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        await AccountLifecycleService(session).assert_may_act(user_id)


async def db_anon() -> AsyncIterator[AsyncSession]:
    async with unit_of_work(actor_type="system") as session:
        yield session


async def current_admin_id(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> uuid.UUID:
    if creds is None or not creds.credentials:
        raise Unauthorized("Missing bearer token")
    payload = decode_admin_token(creds.credentials)
    return uuid.UUID(payload["sub"])


async def db_admin(admin_id: uuid.UUID = Depends(current_admin_id)) -> AsyncIterator[AsyncSession]:
    # actor recorded in the audit trail; admin plane touches non-RLS tables.
    async with unit_of_work(user_id=admin_id, actor_type="admin") as session:
        yield session


def client_ip(request: Request) -> str | None:
    """The TRANSPORT peer address. Never a header.

    `X-Forwarded-For`, `Forwarded` and `X-Real-IP` are all caller-supplied
    strings. Reading any of them here would let a caller choose its own
    rate-limit key — a fresh address per request is the same as having no limit
    — and would let it pin the blame for its attempts on somebody else's
    address. `request.client` comes from the ASGI server's socket, which the
    caller cannot forge.

    The cost of this choice is real and accepted: behind a load balancer that
    does not use PROXY protocol, every request appears to come from the
    balancer, so the address scope degenerates to one bucket. That fails toward
    over-throttling one shared bucket rather than toward no throttling at all,
    and the per-identity scope (see `app.services.admission.auth`) is unaffected
    either way. Trusting a header would have to come with a configured list of
    trusted proxy hops; until that exists, this refuses to guess.
    """
    return request.client.host if request.client else None
