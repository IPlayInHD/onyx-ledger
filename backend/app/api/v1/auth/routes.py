from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, current_user_id, db_anon, db_authed
from app.schemas import LoginRequest, RefreshRequest, RegisterRequest, TokenPair
from app.services.admission.auth import admit_auth_attempt
from app.services.auth.service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest, request: Request, session: AsyncSession = Depends(db_anon)
) -> dict:
    """Create an account.

    Throttled on the same class as login. Registration is not a credential test,
    but it is unauthenticated, it writes two rows and computes an Argon2 hash,
    and — because it must say whether an address is already taken — an
    unthrottled version is a fast account-enumeration oracle. The throttle does
    not remove that disclosure; it bounds how quickly it can be harvested.
    """
    await admit_auth_attempt(source_ip=client_ip(request), subject=body.email)
    user = await AuthService(session).register(body.email, body.password)
    return {"id": str(user.id), "email": user.email, "status": user.status}


@router.post("/login", response_model=TokenPair)
async def login(
    body: LoginRequest, request: Request, session: AsyncSession = Depends(db_anon)
) -> TokenPair:
    """Exchange credentials for a token pair.

    Admission runs FIRST — before the user lookup and before Argon2 — so a
    refused attempt costs one indexed UPSERT instead of a deliberately expensive
    key derivation. See `app.services.admission.auth`.
    """
    await admit_auth_attempt(source_ip=client_ip(request), subject=body.email)
    return await AuthService(session).authenticate(body.email, body.password, client_ip(request))


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    body: RefreshRequest, request: Request, session: AsyncSession = Depends(db_anon)
) -> TokenPair:
    """Rotate a refresh token.

    Also a credential surface: a stolen or guessed refresh token is presented
    here. The identity scope is keyed on the presented TOKEN rather than on an
    email, because that is the only identity claim the request carries — it
    bounds how fast one token can be hammered, while the address scope bounds
    hammering with a rotating supply of them.
    """
    await admit_auth_attempt(source_ip=client_ip(request), subject=body.refresh_token)
    return await AuthService(session).refresh(body.refresh_token, client_ip(request))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> None:
    await AuthService(session).logout(user_id)
