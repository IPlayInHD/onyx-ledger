"""Password + token hashing. Argon2id for passwords; SHA-256 for opaque tokens.

Refresh/reset/verification tokens are stored ONLY as hashes; the raw value
exists only in transit to the client.
"""
from __future__ import annotations

import hashlib
import hmac

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()  # Argon2id defaults (memory/time cost tuned in prod)


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, plaintext)
    except VerifyMismatchError:
        return False


def needs_rehash(hashed: str) -> bool:
    return _hasher.check_needs_rehash(hashed)


def hash_token(raw_token: str) -> str:
    """Deterministic hash for opaque tokens (session/reset/verification)."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
