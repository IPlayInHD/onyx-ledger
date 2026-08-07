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
    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        yield session


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
