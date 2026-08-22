"""Request and response contracts for email verification and password recovery.

`extra="forbid"` ON EVERY REQUEST, and it is the security control this module
exists to apply. Each of these endpoints writes to an account, and every field
that decides WHICH account or WHAT changes is derived on the server — from a
bearer token, or from a token digest looked up in the database. A client that
submits `user_id`, `status`, `email_verified_at`, `used_at`, `expires_at`, a
token digest or a password hash is trying to supply one of those, and the
answer is 422 rather than a silent ignore. Silent ignoring is the same
behaviour as a working attack right up until the day a field is added with a
matching name.

The responses are deliberately uninformative. `RecoveryAccepted` is the SAME
object for an address with an account and an address without one, and it
carries no field that could ever differ between them — see the password-reset
route for why that is a contract and not a convenience.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field

#: The password rule, quoted from `RegisterRequest` rather than re-decided.
#: A reset that accepted a weaker password than registration would be a way to
#: install one, and two independently written bounds drift.
_PASSWORD = Field(min_length=8, max_length=200)

#: A recovery token as it arrives from a link. Bounded because
#: `secrets.token_urlsafe(48)` is 64 characters and nothing legitimate is
#: longer; an unbounded string here would be hashed, which is cheap, but it
#: would also be logged by anything that logs request sizes.
_TOKEN = Field(min_length=16, max_length=512)


class PasswordResetRequest(BaseModel):
    """Ask for a reset link. Answered identically whatever the address is."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class PasswordResetCompletion(BaseModel):
    """Present a link's token and choose a new password."""

    model_config = ConfigDict(extra="forbid")

    token: str = _TOKEN
    new_password: str = _PASSWORD


class EmailVerificationRequest(BaseModel):
    """Present a verification link's token.

    No account identifier: possession of the token IS the claim, and accepting
    a user id alongside it would create a second, weaker way to say which
    account is being verified.
    """

    model_config = ConfigDict(extra="forbid")

    token: str = _TOKEN


class RecoveryAccepted(BaseModel):
    """The one answer every message-sending recovery endpoint gives.

    A FIXED STRING, not a rendered outcome. The moment this carries a field
    that varies — "sent": true, an address, a masked address, a count — the
    endpoint starts answering "does this address have an account?", which is
    the single thing these routes exist not to answer.
    """

    detail: str = (
        "If that address has an account, a message is on its way. "
        "Check your inbox, including spam."
    )


class VerificationResult(BaseModel):
    """What a completed verification tells the client.

    `status` so the SPA can drop its "unverified" banner without a second
    round trip. It is the account's own status, which the caller just changed
    and is entitled to see.
    """

    status: str


__all__ = [
    "EmailVerificationRequest",
    "PasswordResetCompletion",
    "PasswordResetRequest",
    "RecoveryAccepted",
    "VerificationResult",
]
