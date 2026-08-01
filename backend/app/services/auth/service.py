"""Authentication service — registration, login, JWT issuance, refresh rotation."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import Conflict, Unauthorized
from app.core.security.jwt import create_access_token, generate_refresh_token
from app.core.security.password import hash_password, hash_token, verify_password
from app.database.models import AuthSession, LoginEvent, UserAccount, UserCredential
from app.schemas import TokenPair


class AuthService:
    def __init__(self, session: AsyncSession):
        self.s = session
        self.settings = get_settings()

    async def register(self, email: str, password: str) -> UserAccount:
        existing = await self.s.scalar(
            select(UserAccount).where(UserAccount.email == email, UserAccount.deleted_at.is_(None))
        )
        if existing:
            raise Conflict("An account with this email already exists")
        user = UserAccount(email=email, status="active")
        self.s.add(user)
        await self.s.flush()  # populate user.id via RETURNING
        self.s.add(UserCredential(user_id=user.id, password_hash=hash_password(password)))
        await self.s.flush()
        return user

    async def authenticate(self, email: str, password: str, ip: str | None = None) -> TokenPair:
        user = await self.s.scalar(
            select(UserAccount).where(UserAccount.email == email, UserAccount.deleted_at.is_(None))
        )
        cred = (
            await self.s.scalar(select(UserCredential).where(UserCredential.user_id == user.id))
            if user
            else None
        )
        if not user or not cred or not verify_password(password, cred.password_hash):
            self.s.add(LoginEvent(user_id=user.id if user else None, email_tried=email,
                                  event_type="failure", ip_address=ip))
            raise Unauthorized("Invalid email or password")
        user.last_login_at = datetime.now(tz=UTC)
        self.s.add(LoginEvent(user_id=user.id, event_type="success", ip_address=ip))
        return await self._issue_tokens(user.id, ip)

    async def refresh(self, raw_refresh: str, ip: str | None = None) -> TokenPair:
        token_hash = hash_token(raw_refresh)
        sess = await self.s.scalar(
            select(AuthSession).where(AuthSession.refresh_token_hash == token_hash)
        )
        now = datetime.now(tz=UTC)
        if not sess or sess.revoked_at is not None or sess.expires_at <= now:
            # reuse of a rotated/expired token → revoke the whole family for safety
            if sess and sess.revoked_at is not None:
                await self._revoke_user_sessions(sess.user_id)
            raise Unauthorized("Invalid or expired refresh token")
        sess.revoked_at = now  # rotate: single-use refresh tokens
        return await self._issue_tokens(sess.user_id, ip)

    async def logout(self, user_id: uuid.UUID) -> None:
        await self._revoke_user_sessions(user_id)

    async def _issue_tokens(self, user_id: uuid.UUID, ip: str | None) -> TokenPair:
        raw_refresh = generate_refresh_token()
        self.s.add(AuthSession(
            user_id=user_id,
            refresh_token_hash=hash_token(raw_refresh),
            ip_address=ip,
            expires_at=datetime.now(tz=UTC)
            + timedelta(seconds=self.settings.refresh_token_ttl_seconds),
        ))
        await self.s.flush()
        return TokenPair(
            access_token=create_access_token(user_id), refresh_token=raw_refresh
        )

    async def _revoke_user_sessions(self, user_id: uuid.UUID) -> None:
        sessions = await self.s.scalars(
            select(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        )
        now = datetime.now(tz=UTC)
        for sess in sessions:
            sess.revoked_at = now
