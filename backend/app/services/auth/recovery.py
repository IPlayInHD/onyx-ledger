"""Email verification and password recovery.

THE TWO TOKEN TABLES ALREADY EXISTED and are used as they were designed:
`identity.email_verification_token` and `identity.password_reset_token`, each
carrying a unique `token_hash`, an `expires_at` and a `used_at`. Nothing here
adds a parallel model, and nothing stores a token in a form anyone can read
back.

WHAT A TOKEN IS. `secrets.token_urlsafe(48)` — 384 bits from the OS CSPRNG —
handed to the customer once, in the link, and never again. What the database
holds is `sha256(raw)`. That is deliberately NOT the password hasher: Argon2 is
slow by design because a password is low-entropy and must be expensive to
guess, while a 384-bit random token needs no work factor and would cost a
verification round trip for nothing. A fast digest over a high-entropy secret is
the right primitive, and it is the one `AuthSession` already uses for refresh
tokens.

WHY THERE IS NO CONSTANT-TIME COMPARE IN HERE. Lookup is `WHERE token_hash = ?`
against a unique index — the presented secret is hashed before it touches the
query, and no candidate row is ever fetched and then compared field by field.
`tokens_equal` exists for the case where a stored value is compared to a
presented one, and adding it where nothing is compared would be decoration that
implies a threat this shape does not have.

CLAIMING IS ATOMIC. Every consumption is a single conditional UPDATE — `SET
used_at = now() WHERE id = ? AND used_at IS NULL RETURNING` — so two requests
arriving with the same token race in PostgreSQL rather than in Python, and
exactly one of them wins. A read-then-write would let both pass the check.

RECOVERY IS NOT A BACK DOOR INTO A CLOSED ACCOUNT. Neither flow may change what
an account is allowed to do beyond the one transition it exists for. A
suspended account that verifies its email is still suspended; a deleting account
that completes a password reset is still deleting. `_usable_account` is the one
place that decides, and both flows go through it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import DomainError
from app.core.logging import get_logger
from app.core.security.jwt import generate_recovery_token
from app.core.security.password import hash_password, hash_token
from app.database.models import (
    EmailVerificationToken,
    PasswordResetToken,
    SecurityEvent,
    UserAccount,
    UserCredential,
)
from app.domain.ports import EmailProvider, RenderedEmail, TransactionalEmail
from app.services.email.templates import (
    render_email_verification,
    render_password_changed,
    render_password_reset,
)

log = get_logger("onyx.auth.recovery")

#: Statuses that may complete a recovery flow. `pending_verification` is here
#: on purpose — an unverified account is exactly the one that needs to verify,
#: and it must also be able to recover a forgotten password.
RECOVERABLE_STATUSES = frozenset({"active", "pending_verification"})

#: Frontend routes the links point at. Here rather than inline so the two
#: halves of a link — the path the backend mints and the route the SPA
#: serves — are one grep apart; an E2E test asserts they agree.
VERIFY_EMAIL_PATH = "/verify-email"
RESET_PASSWORD_PATH = "/reset-password"

#: The two token tables, which are structurally identical and are treated
#: identically. Naming the union rather than accepting `type` keeps the
#: shared helpers honest: they may only touch columns BOTH tables have.
RecoveryTokenModel = type[EmailVerificationToken] | type[PasswordResetToken]

#: What a verified account becomes. The single transition verification performs.
VERIFIED_STATUS = "active"


class RecoveryEvent(StrEnum):
    """The account-security events this subsystem records.

    A CLOSED SET, so the audit trail can be queried for "what happened to this
    account" without knowing which free-form string a given call site invented.
    They go to `audit.security_event` rather than `identity.login_event`: that
    table's `event_type` carries a CHECK constraint naming the four
    authentication outcomes, and widening it to carry recovery would be
    stretching a login-attempt log into a security log when the schema already
    has a security log. `audit.security_event` is also the one the
    de-identification path already rewrites at deletion, so these rows stop
    naming a person at exactly the moment every other audit row does.
    """

    VERIFICATION_REQUESTED = "email_verification_requested"
    EMAIL_VERIFIED = "email_verified"
    PASSWORD_RESET_REQUESTED = "password_reset_requested"
    PASSWORD_RESET_COMPLETED = "password_reset_completed"
    SESSION_FAMILIES_REVOKED = "session_families_revoked"


@dataclass(frozen=True, slots=True)
class PendingEmail:
    """A rendered message waiting for its transaction to commit.

    THIS TYPE EXISTS TO MAKE THE ORDERING UNAVOIDABLE. Every recovery flow
    mints its token inside the caller's unit of work and returns one of these
    instead of sending; the caller commits, then calls `deliver`. Two things
    fall out of that, and both matter:

    1. A send that fails leaves a committed token nobody received, which the
       customer fixes by asking again — the next mint supersedes it. The other
       ordering puts a live link in an inbox for a row that got rolled back,
       and the only thing the customer can do with it is click a dead link.
    2. No provider call happens with a database transaction open. An SES call
       with retries can take seconds; holding a connection and the token row's
       lock across it is how a slow provider becomes a database incident.
    """

    to: str
    kind: TransactionalEmail
    message: RenderedEmail


class RecoveryTokenInvalid(DomainError):
    """The token is unknown, expired, already used, or its account cannot use it.

    ONE ERROR FOR ALL FOUR, and the reasons are not distinguished to the caller.
    "Expired" versus "unknown" tells a holder of a guessed token whether they
    guessed a real one, and "this account is suspended" tells anyone holding a
    stale link something about a person's standing with the product.

    400 rather than 401: no credential was presented and none would help. The
    caller needs a new link, not a login.
    """

    status_code = 400
    error_type = "https://onyx.ledger/errors/recovery-token-invalid"
    title = "Link No Longer Valid"

    def __init__(self) -> None:
        super().__init__(
            "This link is no longer valid. Request a new one and try again."
        )


class AccountRecoveryService:
    """Verification and password recovery for one request."""

    def __init__(
        self, session: AsyncSession, email_provider: EmailProvider | None = None
    ) -> None:
        self.s = session
        self.settings = get_settings()
        self._provider = email_provider

    @property
    def provider(self) -> EmailProvider:
        """The port, resolved on first use.

        Lazily, because `get_email_provider` refuses to build a production
        provider from an incomplete configuration — and a service constructed
        for a flow that sends nothing should not be the thing that discovers it.
        """
        if self._provider is None:
            from app.integrations.email import get_email_provider

            self._provider = get_email_provider()
        return self._provider

    # ------------------------------------------------------------ helpers --
    def _link(self, path: str, raw_token: str) -> str:
        """A recovery link, built from CONFIGURATION and nothing else.

        Never from a request header. `Host` and `X-Forwarded-Host` are
        attacker-supplied, and a reset link whose host the caller chooses is a
        single-use credential addressed to the attacker — the classic host-header
        password-reset poisoning. The origin comes from settings, which
        production requires to be https.
        """
        origin = (self.settings.app_public_url or "http://localhost:5173").rstrip("/")
        return f"{origin}{path}?token={raw_token}"

    async def _usable_account(self, user_id: uuid.UUID) -> UserAccount | None:
        """The account, if it may complete a recovery flow. Otherwise None.

        Checks BOTH halves of the lifecycle, because they are different things
        and a token must survive neither: `status` covers suspended and closed,
        and `identity.account_deletion_state` covers an account past its
        deletion cutoff. The second goes through the SECURITY DEFINER function
        rather than reading `account_lifecycle` directly — this session is
        anonymous for the reset flows, so RLS would correctly hide the row and a
        direct read would silently see nothing and admit every deleting account.
        """
        user = await self.s.scalar(
            select(UserAccount).where(
                UserAccount.id == user_id, UserAccount.deleted_at.is_(None)
            )
        )
        if user is None or user.status not in RECOVERABLE_STATUSES:
            return None
        state = await self.s.scalar(
            text("SELECT identity.account_deletion_state(:u)"), {"u": user_id}
        )
        if state is not None:
            return None
        return user

    async def _mint(
        self, model: RecoveryTokenModel, user_id: uuid.UUID, ttl_minutes: int
    ) -> str:
        """One fresh token, superseding every unused one of the same kind.

        SUPERSESSION IS THE POINT of doing this in one place. Without it, a
        customer who clicks "resend" three times has three live links, each of
        which stays a working credential in a mailbox until it expires on its
        own. Marking the previous ones used means the newest link is the only
        one that works — which is also what a customer already assumes.
        """
        now = datetime.now(tz=UTC)
        await self.s.execute(
            update(model)
            .where(model.user_id == user_id, model.used_at.is_(None))
            .values(used_at=now)
        )
        raw = generate_recovery_token()
        self.s.add(
            model(
                user_id=user_id,
                token_hash=hash_token(raw),
                expires_at=now + timedelta(minutes=ttl_minutes),
            )
        )
        await self.s.flush()
        return raw

    async def _claim(
        self, model: RecoveryTokenModel, raw_token: str
    ) -> uuid.UUID | None:
        """Consume a token atomically, or return None.

        THE CONDITIONAL UPDATE IS WHAT MAKES CONCURRENT SUBMISSIONS SAFE. Two
        requests carrying the same token issue the same statement, PostgreSQL
        serialises them on the row, and the second matches nothing because
        `used_at` is no longer NULL. A read-then-write would let both through.

        Expiry is in the WHERE clause and evaluated by the database, so a token
        cannot be admitted by a Python process whose clock has drifted.

        `synchronize_session=False` because nothing in this unit of work holds
        the row in memory — the token is looked up by digest and never loaded.
        """
        claimed: uuid.UUID | None = await self.s.scalar(
            update(model)
            .where(
                model.token_hash == hash_token(raw_token),
                model.used_at.is_(None),
                model.expires_at > func.now(),
            )
            .values(used_at=func.now())
            .returning(model.user_id)
            .execution_options(synchronize_session=False)
        )
        return claimed

    # ------------------------------------------------------- verification --
    async def request_verification(self, user_id: uuid.UUID) -> PendingEmail | None:
        """Mint a verification link for an account that needs one.

        Returns None — and mints nothing — for an account that is already
        verified or cannot recover. The caller is authenticated as this account,
        so there is no enumeration to worry about; there is simply nothing
        useful to say and no reason to send mail.
        """
        user = await self._usable_account(user_id)
        if user is None or user.email_verified_at is not None:
            return None
        ttl = self.settings.verification_token_ttl_minutes
        raw = await self._mint(EmailVerificationToken, user_id, ttl)
        await self._audit(user_id, RecoveryEvent.VERIFICATION_REQUESTED)
        return PendingEmail(
            to=user.email,
            kind=TransactionalEmail.EMAIL_VERIFICATION,
            message=render_email_verification(
                link=self._link(VERIFY_EMAIL_PATH, raw), ttl_minutes=ttl
            ),
        )

    async def verify_email(self, raw_token: str) -> None:
        """Complete verification: the smallest authorized transition and no more.

        `pending_verification` becomes `active` and `email_verified_at` is
        stamped. An account that is suspended, closed or deleting is refused by
        `_usable_account` BEFORE the transition, so a verification link can
        never be the thing that brings an account back.
        """
        user_id = await self._claim(EmailVerificationToken, raw_token)
        if user_id is None:
            raise RecoveryTokenInvalid()
        user = await self._usable_account(user_id)
        if user is None:
            raise RecoveryTokenInvalid()
        if user.email_verified_at is None:
            user.email_verified_at = datetime.now(tz=UTC)
        if user.status == "pending_verification":
            user.status = VERIFIED_STATUS
        await self.s.flush()
        await self._audit(user_id, RecoveryEvent.EMAIL_VERIFIED)

    # ---------------------------------------------------- password  reset --
    async def request_password_reset(self, email: str) -> PendingEmail | None:
        """Mint a reset token for a real account, or do nothing.

        NON-ENUMERATING BY CONSTRUCTION: this returns None for an address that
        has no usable account, and the route answers identically either way.
        Nothing about which branch was taken reaches the response.
        """
        user = await self.s.scalar(
            select(UserAccount).where(
                UserAccount.email == email, UserAccount.deleted_at.is_(None)
            )
        )
        if user is None:
            return None
        if await self._usable_account(user.id) is None:
            # A suspended or deleting account gets the same silence an unknown
            # address gets. Telling the requester "that account is suspended"
            # would answer a question they have no standing to ask.
            return None
        ttl = self.settings.password_reset_token_ttl_minutes
        raw = await self._mint(PasswordResetToken, user.id, ttl)
        await self._audit(user.id, RecoveryEvent.PASSWORD_RESET_REQUESTED)
        return PendingEmail(
            to=user.email,
            kind=TransactionalEmail.PASSWORD_RESET,
            message=render_password_reset(
                link=self._link(RESET_PASSWORD_PATH, raw), ttl_minutes=ttl
            ),
        )

    async def complete_password_reset(self, raw_token: str, new_password: str) -> str:
        """Set a new password, revoke every session, return the address to notify.

        SESSION REVOCATION IS NOT OPTIONAL. The reason to reset a password is
        usually that somebody else may know the old one, and a refresh token
        issued before the reset would keep that access alive for its whole
        lifetime. Every `auth_session` row for the account is revoked here.

        THE HONEST LIMIT: an access token already issued stays valid until it
        expires. Access tokens are self-contained JWTs and this system has no
        server-side denylist; inventing one for this flow would put a database
        read on every authenticated request to shorten a window measured in
        minutes. The window is the access-token lifetime, and it is stated
        rather than papered over.
        """
        user_id = await self._claim(PasswordResetToken, raw_token)
        if user_id is None:
            raise RecoveryTokenInvalid()
        user = await self._usable_account(user_id)
        if user is None:
            raise RecoveryTokenInvalid()

        cred = await self.s.scalar(
            select(UserCredential).where(UserCredential.user_id == user_id)
        )
        if cred is None:
            raise RecoveryTokenInvalid()
        # The SAME password authority ordinary credential creation uses. A
        # second hashing path is how two parts of a system end up disagreeing
        # about what a stored credential means.
        cred.password_hash = hash_password(new_password)
        cred.password_changed_at = datetime.now(tz=UTC)
        cred.must_reset = False

        from app.services.auth.service import AuthService

        await AuthService(self.s)._revoke_user_sessions(user_id)
        await self.s.flush()
        await self._audit(user_id, RecoveryEvent.PASSWORD_RESET_COMPLETED)
        await self._audit(user_id, RecoveryEvent.SESSION_FAMILIES_REVOKED)
        return user.email

    # ------------------------------------------------------------- audit --
    async def _audit(self, user_id: uuid.UUID, event: RecoveryEvent) -> None:
        """Record that a security event happened, and nothing about its secret.

        THE EVENT NAME AND THE ACCOUNT. Not the token, not its digest, not the
        link, not the rendered body, not the address. `detail` is left NULL on
        purpose: it is unvalidated JSONB, which makes it the one field on this
        row where a future call site could put a secret without any check
        noticing, and there is nothing about a recovery event worth the risk.

        `subject_key` is also left NULL. `identity.subject_key_for` is
        deliberately NOT executable by the request role — granting it would
        hand the request path an account-existence oracle — and the deletion
        path stamps the key itself when it severs `user_id`, which is the only
        moment the key is actually needed.
        """
        self.s.add(SecurityEvent(user_id=user_id, event_type=event.value))

    # ----------------------------------------------------------- delivery --
    def deliver(self, pending: PendingEmail | None) -> None:
        """Send a pending message. CALL THIS AFTER THE COMMIT, never before.

        A raised `EmailDeliveryFailed` reaches the caller: the customer asked
        for a link, no link is coming, and telling them so is what makes them
        ask again. The token is already committed and the next request
        supersedes it.

        `None` is accepted and does nothing, so a non-enumerating route can
        write one unconditional line rather than branching on an outcome it is
        supposed to keep to itself.
        """
        if pending is None:
            return
        self.provider.send(pending.to, pending.kind, pending.message)

    def notify_password_changed(self, to: str) -> None:
        """Tell the customer their password changed. Never undo it if this fails.

        The password IS changed and every session IS revoked by the time this
        runs, both committed. If the provider is down, the alternatives are:
        roll back a completed password change — leaving the customer holding a
        new password that does not work and an old one they were told was
        replaced — or deliver the change and lose the notification. The second
        is plainly better, so the failure is swallowed and logged.

        This is the one place in the recovery flow where a delivery failure is
        deliberately not surfaced, and it is stated here so it does not read as
        an oversight.
        """
        from app.integrations.email import EmailDeliveryFailed

        try:
            self.provider.send(
                to, TransactionalEmail.PASSWORD_CHANGED, render_password_changed()
            )
        except EmailDeliveryFailed as exc:
            log.warning(
                "password_changed_notice_undelivered", transient=exc.transient
            )
