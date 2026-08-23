"""Integration test harness — runs the real app against a real PostgreSQL.

Requires ONYX_DATABASE_URL to point at a database with the schema applied and
connecting as a NON-superuser role (so RLS is enforced). The test runner
(scripts/run_backend_tests.sh) provisions this.
"""
from __future__ import annotations

import itertools
import os
import re

import httpx
import pytest_asyncio

# Ensure settings pick up the test DB before app import.
os.environ.setdefault(
    "ONYX_DATABASE_URL",
    "postgresql+asyncpg://onyx_test:test@/onyx_test?host=/tmp&port=54329",
)
os.environ.setdefault("ONYX_JWT_SECRET", "test-secret-at-least-32-bytes-long-000")


#: Distinct source address per test client. `ASGITransport` otherwise reports
#: every request as coming from 127.0.0.1, which — now that AUTH_ATTEMPT is
#: throttled per source address — would make the whole suite one caller sharing
#: one allowance, and tests would start failing on each other's login traffic.
#:
#: This makes the fixture REALISTIC, not permissive: the production limit is
#: unchanged and still enforced, and a test that wants to exercise the address
#: limit uses `client_from_one_address` below to pin two clients together.
_source_addresses = itertools.count(1)


def next_source_address() -> str:
    n = next(_source_addresses)
    return f"10.{n // 65536 % 256}.{n // 256 % 256}.{n % 256}"


@pytest_asyncio.fixture
async def client():
    from app.database.privacy_session import dispose_all_engines
    from app.main import app  # imported after env is set

    transport = httpx.ASGITransport(app=app, client=(next_source_address(), 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    # EVERY engine, not just the application one. Pooled connections are bound
    # to the event loop that opened them, and a test that also touched a
    # privileged runtime leaves that pool alive for the next test's loop —
    # which then fails with "attached to a different loop" somewhere unrelated.
    # This fixture named only `engine` until Entry 11B5J, which is the fifth
    # time that shape has cost a debugging session.
    await dispose_all_engines()


@pytest_asyncio.fixture
def client_from_one_address():
    """Factory for clients that all appear to come from the SAME address.

    For the throttling tests, where sharing a source address is the point.
    """
    import contextlib

    from app.main import app

    address = next_source_address()

    @contextlib.asynccontextmanager
    async def _make():
        transport = httpx.ASGITransport(app=app, client=(address, 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    return _make


# ---------------------------------------------------------------------------
# Account setup
# ---------------------------------------------------------------------------
#
# B3 made registration create `pending_verification`, which is refused by
# `db_authed` — so a test that registers an account and immediately calls an
# authenticated endpoint now gets 403 instead of its subject. Almost every such
# test is about something else entirely (documents, RLS, admission, the audit
# log) and wants a verified account as a PRECONDITION, not as a subject.
#
# `verify_account` is how they get one, THROUGH THE REAL ENDPOINTS. Setting the
# status column directly would be one line shorter and would mean the suite
# stopped exercising the transition that every one of those tests now depends
# on — the kind of shortcut that makes a whole flow untested by making it
# invisible.

#: The token as it appears in a link. Matches the query parameter rather than
#: the surrounding copy, so restyling a template cannot break sixteen test
#: files. `_link` in `app.services.auth.recovery` owns the format.
_LINK_TOKEN = re.compile(r"[?&]token=([A-Za-z0-9_-]+)")


def token_from_link(message) -> str:
    """The raw token out of a captured message's plain-text part."""
    found = _LINK_TOKEN.search(message.text)
    assert found, (
        "no ?token= in the captured message; the link format changed and this "
        f"helper has to change with it. Body was:\n{message.text}"
    )
    return found.group(1)


def verification_messages(email: str):
    """Verification messages for one address, oldest first.

    CASE-INSENSITIVE on the recipient, because the outbox holds the address the
    ACCOUNT stores and that is not always the string the caller typed:
    pydantic's `EmailStr` lowercases the domain. An exact comparison reported
    "no message captured" for an address that had one, which is a confusing way
    to learn about a normalization the product documents.

    Scoped BY RECIPIENT, never by position. `captured_emails()[-1]` would make
    every test depend on whatever ran beside it, which is the isolation failure
    this repository has hit repeatedly; addresses here are unique per test.
    """
    from app.domain.ports import TransactionalEmail
    from app.integrations.email import captured_emails

    wanted = email.casefold()
    return [
        m for m in captured_emails(kind=TransactionalEmail.EMAIL_VERIFICATION)
        if m.to.casefold() == wanted
    ]


async def verify_account(client, email: str, token: str) -> None:
    """Take a freshly registered account from pending_verification to active.

    USES THE LINK REGISTRATION ALREADY SENT. Asking for another would be
    refused by the resend floor — correctly — and it is also not what a
    customer does: they open the message that arrived.

    `token` is only needed for the fallback, for a caller whose account was
    made some other way and has no message waiting.
    """
    messages = verification_messages(email)
    if not messages:
        sent = await client.post(
            "/api/v1/auth/verification", headers={"Authorization": f"Bearer {token}"}
        )
        assert sent.status_code == 202, sent.text
        messages = verification_messages(email)
    assert messages, f"no verification message for {email}"

    confirmed = await client.post(
        "/api/v1/auth/verification/confirm",
        json={"token": token_from_link(messages[-1].message)},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "active"


async def register_verified(
    client, email: str, password: str
) -> str:
    """Register, verify, and return an access token for an ACTIVE account.

    The one-call form for the many tests whose subject is not authentication.
    Returns a token minted AFTER verification, because the account's status is
    read per request rather than carried in the token — but a caller that logs
    in again gets the same answer, and this saves them the round trip.
    """
    created = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "pending_verification", created.text

    signed_in = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert signed_in.status_code == 200, signed_in.text
    token = signed_in.json()["access_token"]

    await verify_account(client, email, token)
    return token


def owner_dsn() -> str:
    """Owner-role DSN for the database the harness actually provisioned.

    Derived from ONYX_DATABASE_URL rather than hardcoded: a test that names a
    database the harness never creates passes only where someone happens to
    have created it by hand, and fails on a clean machine.
    """
    import os
    from urllib.parse import urlparse

    url = os.environ.get("ONYX_DATABASE_URL", "")
    parsed = urlparse(url)
    database = (parsed.path or "/onyx_test").lstrip("/").split("?")[0]
    return f"postgresql://onyx_migrator@localhost:5432/{database}"


def _runtime_dsn(env_var: str, fallback_user: str) -> str:
    """A psycopg2 DSN for the SAME database and login the runtime is using.

    WHY THIS EXISTS. `test_worker_capability_boundary.py` hardcoded

        postgresql://onyx_test:test@/onyx_test?host=/var/run/postgresql&port=5432

    naming a database by name. The security gate runs the suite against
    `onyx_sec_proof`, so every assertion in that file read a DIFFERENT database
    than the one under test — and Entry 11B5J's authoritative gate proved the
    consequence: the lifecycle-worker ACL was granted to `onyx_app_rw` in the
    proof database, the injection verified its own effect, and the suite passed
    anyway because the guard was looking at `onyx_test`.

    A guard pointed at a fixed database cannot certify anything about the
    database being certified. `owner_dsn` above already said so in its
    docstring; this is the same rule applied to the runtime logins.
    """
    import os
    from urllib.parse import parse_qs, urlparse

    url = os.environ.get(env_var, "")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    database = (parsed.path or "/onyx_test").lstrip("/").split("?")[0]
    host = query.get("host", ["/var/run/postgresql"])[0]
    port = query.get("port", ["5432"])[0]
    user = parsed.username or fallback_user
    password = parsed.password or "test"
    return (f"postgresql://{user}:{password}@/{database}"
            f"?host={host}&port={port}")


def app_dsn() -> str:
    """The ordinary application login — a member of `onyx_app_rw`."""
    return _runtime_dsn("ONYX_DATABASE_URL", "onyx_test")


def privacy_dsn() -> str:
    """The dedicated privacy-worker login. Not a member of `onyx_app_rw`."""
    return _runtime_dsn("ONYX_PRIVACY_DATABASE_URL", "onyx_privacy_test")


def freshness_dsn() -> str:
    """The dedicated freshness-worker login. Not a member of `onyx_app_rw`."""
    return _runtime_dsn("ONYX_FRESHNESS_DATABASE_URL", "onyx_freshness_test")


def frozen_snapshot(
    *, tax_year: int = 2025, jurisdiction: str = "ON", **fields
) -> tuple[dict, str]:
    """A COMPLETE frozen analysis snapshot plus its content hash.

    Optimizations are now calculated exclusively from the pinned snapshot and
    fail closed on an incomplete one, so a fixture that writes a stub cannot
    produce a run. This uses the production codec, so a fixture snapshot is
    reconstructible by the same code that reads a real one — a test cannot
    accidentally prove something about a payload shape production never writes.
    """
    from decimal import Decimal

    from app.services.ioe.frozen.models import canonical_snapshot, snapshot_hash
    from app.services.tax_engine.core.engine import TaxInput

    values = {
        k: (Decimal(str(v)) if not isinstance(v, (str, int)) or k not in
            ("province", "marital_status", "year", "age") else v)
        for k, v in fields.items()
    }
    tax_input = TaxInput(
        province=jurisdiction, year=tax_year,
        marital_status=values.pop("marital_status", "single"),
        **values,
    )
    payload = canonical_snapshot(
        tax_input, tax_year=tax_year, jurisdiction=jurisdiction)
    return payload, snapshot_hash(payload)
