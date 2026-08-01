from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, current_user_id, db_anon, db_authed
from app.schemas import LoginRequest, RefreshRequest, RegisterRequest, TokenPair
from app.services.auth.service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, session: AsyncSession = Depends(db_anon)) -> dict:
    user = await AuthService(session).register(body.email, body.password)
    return {"id": str(user.id), "email": user.email, "status": user.status}


@router.post("/login", response_model=TokenPair)
async def login(
    body: LoginRequest, request: Request, session: AsyncSession = Depends(db_anon)
) -> TokenPair:
    return await AuthService(session).authenticate(body.email, body.password, client_ip(request))


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    body: RefreshRequest, request: Request, session: AsyncSession = Depends(db_anon)
) -> TokenPair:
    return await AuthService(session).refresh(body.refresh_token, client_ip(request))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(user_id=Depends(current_user_id), session: AsyncSession = Depends(db_authed)) -> None:
    await AuthService(session).logout(user_id)
