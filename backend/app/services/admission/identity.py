"""Scope keys for callers who are not yet a principal.

Every other operation class is billed to an authenticated principal, so its
scope key is a UUID the server issued. `AUTH_ATTEMPT` cannot be: the operation
is the act of finding out whether the caller is anybody, so the only things
available to key on are what the caller CLAIMS to be (an email address) and
where the connection came from (a peer address).

Both are personal data, and neither is written down here.

WHY A KEYED DIGEST, NOT THE VALUE
---------------------------------
`admission.rate_counter.scope_id` is an ordinary table with ordinary backups. An
email address in it is a second copy of the account list, held under a different
retention regime than `identity.user_account`, readable by anyone who can read
the admission schema, and — for failed logins — a record of addresses that do
NOT have accounts, which the product never had a reason to keep.

WHY NOT A PLAIN SHA-256
-----------------------
Email addresses are low-entropy. The candidate space a real attacker searches is
a leaked address list, not 2^256, so an unsalted digest of an email is a lookup
away from the address. HMAC-SHA256 under a secret the attacker does not have
makes the digest useless without also stealing the secret, and the secret lives
in the environment rather than in the database being protected.

Deliberately NOT Python's `hash()`: it is randomized per process, so two API
replicas would compute different keys for the same address and each would get
its own full allowance.

DOMAIN SEPARATION
-----------------
The subject digest and the address digest use different prefixes, so a value
that happens to be both cannot spend one budget as the other, and neither can
collide with a key from some future scope.
"""
from __future__ import annotations

import hmac
import unicodedata
from hashlib import sha256

from app.core.config import get_settings

_SUBJECT_DOMAIN = b"onyx.admission.auth-subject.v1"
_SOURCE_DOMAIN = b"onyx.admission.source-ip.v1"

#: 128 bits of the digest. Ample against collision for a counter key, and it
#: keeps the stored scope id short enough to index comfortably.
_KEY_HEX_LEN = 32


def _keyed(domain: bytes, value: str) -> str:
    secret = get_settings().admission_identity_secret.encode("utf-8")
    mac = hmac.new(secret, domain + b"|" + value.encode("utf-8"), sha256)
    return mac.hexdigest()[:_KEY_HEX_LEN]


def canonical_auth_subject(identifier: str) -> str:
    """Fold the trivially-different spellings of one identity together.

    Without this, `Alice@Example.CA `, `alice@example.ca` and a Unicode-width
    variant of the same address are three separate budgets, and an attacker gets
    the identity allowance once per spelling. NFKC first (so compatibility forms
    collapse), then strip, then casefold — casefold rather than `lower()`
    because `lower()` leaves several non-ASCII pairs distinct.

    This is a THROTTLING key, not an authentication decision: `AuthService` still
    matches the address exactly. Folding more aggressively here is safe in the
    direction that matters — it can only merge budgets, never split them.
    """
    return unicodedata.normalize("NFKC", identifier).strip().casefold()


def auth_subject_scope(identifier: str) -> str:
    """Scope key for a claimed login identity. Never the identity itself."""
    return _keyed(_SUBJECT_DOMAIN, canonical_auth_subject(identifier))


def source_ip_scope(ip: str) -> str:
    """Scope key for a source address. Never the address itself.

    The address arrives from the ASGI transport, not from a header — see
    `app.api.deps.client_ip`. A caller that could name its own address could
    mint a fresh budget per request, which is the same as having none.
    """
    return _keyed(_SOURCE_DOMAIN, ip.strip())


__all__ = ["auth_subject_scope", "canonical_auth_subject", "source_ip_scope"]
