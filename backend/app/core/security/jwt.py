"""JWT access tokens + opaque refresh tokens.

Access tokens are short-lived signed JWTs (stateless authorization). Refresh
tokens are opaque random strings; only their hash is persisted in
identity.auth_session, enabling rotation + reuse detection.
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


def generate_refresh_token() -> str:
    """A high-entropy opaque token (client stores raw; we store the hash)."""
    return secrets.token_urlsafe(48)


def create_admin_token(admin_id: uuid.UUID) -> str:
    """Access token scoped to the admin plane (separate principal namespace)."""
    return create_access_token(admin_id, extra={"scope": "admin"})


def decode_admin_token(token: str) -> dict:
    payload = decode_access_token(token)
    if payload.get("scope") != "admin":
        raise Unauthorized("Admin scope required")
    return payload
