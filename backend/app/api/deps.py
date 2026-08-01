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
from app.core.security.jwt import decode_access_token
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


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None
