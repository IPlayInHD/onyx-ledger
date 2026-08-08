"""Authentication service — registration, login, JWT issuance, refresh rotation."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import Conflict, Unauthorized
from app.core.security.jwt import create_access_token, generate_refresh_token
from app.core.security.password import hash_password, hash_token, verify_password
from app.database.models import AuthSession, LoginEvent, UserAccount, UserCredential
from app.database.session import unit_of_work
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
            await self._record_login_failure(email, user.id if user else None, ip)
            raise Unauthorized("Invalid email or password")

        # The deletion cutoff, applied AFTER credential verification on purpose.
        # Checking first would answer "is this address being deleted?" to
        # anyone who typed it, which is a worse disclosure than the one it would
        # save. A caller who gets here has already proved they own the account.
        await self._refuse_if_deleting(user.id)
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
                await self._revoke_user_sessions_durably(sess.user_id)
            raise Unauthorized("Invalid or expired refresh token")
        sess.revoked_at = now  # rotate: single-use refresh tokens
        # A token minted before the deletion request must not survive rotation:
        # revocation closes the sessions that exist, and this closes the path
        # that would create new ones.
        await self._refuse_if_deleting(sess.user_id)
        return await self._issue_tokens(sess.user_id, ip)

    async def _refuse_if_deleting(self, user_id: uuid.UUID) -> None:
        """Deny authentication for an account past its deletion cutoff.

        Goes through `identity.account_deletion_state` rather than reading the
        table, because this session is ANONYMOUS: `app.user_id` is unset during
        login, so the RLS policy correctly hides the lifecycle row — and a check
        that silently sees nothing would admit every deleting account. That is a
        failure mode worth naming, because the query looks right and does the
        opposite of what it claims.
        """
        from app.services.privacy.lifecycle import AccountDeletionInProgress

        state = await self.s.scalar(
            text("SELECT identity.account_deletion_state(:uid)"),
            {"uid": user_id},
        )
        if state is not None:
            raise AccountDeletionInProgress()

    # ---- effects that must outlive the raise that follows them -------------
    #
    # `unit_of_work` wraps the request in `session.begin()`, so raising discards
    # everything staged on the way out. For a SUCCESSFUL request that is exactly
    # right. For a failure it is not: the security value of a failed login is
    # the record of it, and the security value of detecting a replayed refresh
    # token is the revocation that follows — and both were being staged into the
    # transaction that was about to be rolled back.
    #
    # Recorded as PD-6 in Entry 11A, as a missing audit row. It was worse: the
    # revocation was going the same way, so a stolen refresh token kept working
    # after the theft had been detected.
    #
    # Their own short transaction, committed before the raise. Cheaper than
    # restructuring both call paths to return an outcome and raise after the
    # commit — the shape Entry 10 used for the rate counter — and it does not
    # move the decision away from the code that makes it. The extra connection
    # is taken only on the failure path, which admission already bounds.

    async def _record_login_failure(
        self, email: str, user_id: uuid.UUID | None, ip: str | None
    ) -> None:
        async with unit_of_work(actor_type="system") as session:
            session.add(LoginEvent(user_id=user_id, email_tried=email,
                                   event_type="failure", ip_address=ip))

    async def _revoke_user_sessions_durably(self, user_id: uuid.UUID) -> None:
        async with unit_of_work(actor_type="system") as session:
            await AuthService(session)._revoke_user_sessions(user_id)

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
