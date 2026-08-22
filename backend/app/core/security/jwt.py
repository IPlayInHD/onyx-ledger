"""JWT access tokens + the opaque tokens that are not JWTs.

Access tokens are short-lived signed JWTs (stateless authorization). Refresh
tokens are opaque random strings; only their hash is persisted in
identity.auth_session, enabling rotation + reuse detection. Verification and
password-reset links carry the same kind of opaque string, hashed the same way
into their own tables.

WHY THOSE THREE ARE NOT JWTs. A JWT is verifiable without a database read,
which is exactly wrong for a credential that has to be revocable, single-use,
or both. A refresh token must die the moment it is rotated; a reset link must
die the moment it is clicked. Neither is expressible in a self-contained token,
so all three are random strings whose authority is a row.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import get_settings
from app.core.exceptions import Unauthorized


def _now() -> datetime:
    return datetime.now(tz=UTC)


def create_access_token(user_id: uuid.UUID, extra: dict | None = None) -> str:
    s = get_settings()
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": int(_now().timestamp()),
        "exp": int((_now() + timedelta(seconds=s.access_token_ttl_seconds)).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.ExpiredSignatureError as e:
        raise Unauthorized("Access token expired") from e
    except jwt.InvalidTokenError as e:
        raise Unauthorized("Invalid access token") from e
    if payload.get("type") != "access":
        raise Unauthorized("Wrong token type")
    return payload


#: Bytes of OS entropy behind every opaque token this system issues. ONE
#: decision, in one place: a later argument about whether 48 is enough should
#: change this constant, not one of the callers below, because a system with two
#: different token strengths has whichever is weaker.
_TOKEN_ENTROPY_BYTES = 48


def _opaque_token() -> str:
    """A high-entropy random string (client holds raw; we store the hash)."""
    return secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)


def generate_refresh_token() -> str:
    """The opaque half of a session: rotated on use, hashed in auth_session."""
    return _opaque_token()


def generate_recovery_token() -> str:
    """The secret inside a verification or password-reset link.

    Same primitive and same strength as a refresh token, and deliberately so —
    a reset link is a credential that can set a new password, so it has no
    business being weaker than the session token it replaces.
    """
    return _opaque_token()


def create_admin_token(admin_id: uuid.UUID) -> str:
    """Access token scoped to the admin plane (separate principal namespace)."""
    return create_access_token(admin_id, extra={"scope": "admin"})


def decode_admin_token(token: str) -> dict:
    payload = decode_access_token(token)
    if payload.get("scope") != "admin":
        raise Unauthorized("Admin scope required")
    return payload
