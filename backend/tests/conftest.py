"""Integration test harness — runs the real app against a real PostgreSQL.

Requires ONYX_DATABASE_URL to point at a database with the schema applied and
connecting as a NON-superuser role (so RLS is enforced). The test runner
(scripts/run_backend_tests.sh) provisions this.
"""
from __future__ import annotations

import os

import httpx
import pytest_asyncio

# Ensure settings pick up the test DB before app import.
os.environ.setdefault(
    "ONYX_DATABASE_URL",
    "postgresql+asyncpg://onyx_test:test@/onyx_test?host=/tmp&port=54329",
)
os.environ.setdefault("ONYX_JWT_SECRET", "test-secret-at-least-32-bytes-long-000")


@pytest_asyncio.fixture
async def client():
    from app.database.session import engine
    from app.main import app  # imported after env is set

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    # Dispose the pool so connections aren't reused across per-test event loops.
    await engine.dispose()


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
