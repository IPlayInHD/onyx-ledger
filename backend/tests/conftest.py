"""Integration test harness — runs the real app against a real PostgreSQL.

Requires ONYX_DATABASE_URL to point at a database with the schema applied and
connecting as a NON-superuser role (so RLS is enforced). The test runner
(scripts/run_backend_tests.sh) provisions this.
"""
from __future__ import annotations

import itertools
import os

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
