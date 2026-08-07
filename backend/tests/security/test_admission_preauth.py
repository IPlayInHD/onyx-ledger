"""Security review of the pre-authentication scopes and the ordering (§18–§20).

Phase 2 added the first admission scopes that an UNAUTHENTICATED caller
influences. That is a real widening of the attack surface and it deserves its
own file rather than a paragraph: a scope key an attacker chooses is a scope key
an attacker can point at somebody else, and a counter table an attacker can grow
is a denial of service delivered through the limiter.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.database.session import unit_of_work
from app.services.admission.identity import auth_subject_scope, source_ip_scope
from app.services.admission.policy import UNWIRED_BY_DESIGN, OperationClass, ScopeType
from app.services.admission.service import AdmissionService

PASSWORD = "supersecret1"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    """One connection pool per test.

    pytest-asyncio gives each test its own event loop, and asyncpg connections
    are bound to the loop that created them. A pool built in one test's loop and
    reused in the next fails with "attached to a different loop" — which is a
    harness artifact, not a finding about the code. The same fixture guards the
    other admission suites.
    """
    yield
    from app.database.session import engine

    await engine.dispose()


# ---------------------------------------------------------------------------
# §18 — authorization and admission ordering
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_unauthenticated_caller_cannot_spend_an_admin_allowance(client):
    """Authorization comes FIRST on every principal-scoped surface.

    If admission ran before the token check, an anonymous caller could exhaust
    an operator's import or publish allowance from outside — denial of service
    against the operator plane with no credentials at all. The 401 has to come
    first, and it has to come before any counter moves.
    """
    async def import_run_rows() -> int:
        async with unit_of_work(actor_type="admin") as session:
            value = await session.scalar(
                text("""
                    SELECT count(*) FROM admission.rate_counter
                     WHERE scope_type = 'ADMIN'
                       AND operation_code IN ('IMPORT_RUN', 'ADMIN_RULE_PUBLISH')
                """)
            )
            return int(value or 0)

    before = await import_run_rows()
    for path, body in (
        ("/api/v1/tkms/imports", {"source_org": "x", "format": "json", "payload": "[]"}),
        (f"/api/v1/admin/rules/{uuid.uuid4()}/publish", {}),
        ("/api/v1/admin/ingestion/jobs", {"format": "json", "payload": "[]"}),
    ):
        response = await client.post(path, json=body)
        assert response.status_code in (401, 403), (
            f"{path} answered {response.status_code} without a token"
        )

    assert await import_run_rows() == before, (
        "an unauthenticated request moved an operator-scoped counter"
    )


@pytest.mark.asyncio
async def test_an_operator_without_permission_spends_no_allowance(client):
    """Authorization is checked before admission on the import path too.

    A token that is valid but lacks `tkms.import` must not be able to consume
    the import allowance of the operator plane, and must not learn anything
    from the difference between "refused for size" and "not permitted".
    """
    from app.core.security.jwt import create_admin_token

    stranger = uuid.uuid4()  # a well-formed admin id with no permission rows
    token = create_admin_token(stranger)

    async def rows_for(admin_id: uuid.UUID) -> int:
        async with unit_of_work(actor_type="admin") as session:
            value = await session.scalar(
                text("""
                    SELECT count(*) FROM admission.rate_counter
                     WHERE scope_type = 'ADMIN' AND scope_id = :s
                """),
                {"s": str(admin_id)},
            )
            return int(value or 0)

    response = await client.post(
        "/api/v1/tkms/imports",
        json={"source_org": "x", "format": "json", "payload": "[]"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code in (401, 403), response.status_code
    assert await rows_for(stranger) == 0, (
        "an unauthorized operator still spent an admission allowance"
    )


# ---------------------------------------------------------------------------
# §19 — the new unauthenticated scopes
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_pre_authentication_scope_can_never_hold_a_lease():
    """The database enforces it, not a comment.

    A lease is a slot held by in-flight work. `AUTH_ATTEMPT` declares no
    concurrency, so an unauthenticated caller should have no way to occupy one —
    and if a future change gives the class a concurrency limit without thinking
    that through, this should fail loudly rather than quietly let an anonymous
    caller hold platform capacity.
    """
    from sqlalchemy.exc import DBAPIError

    async with unit_of_work(actor_type="admin") as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text("""
                    INSERT INTO admission.lease
                        (id, scope_type, scope_id, operation_code,
                         acquired_at, expires_at)
                    VALUES (:id, 'IP', 'anything', 'AUTH_ATTEMPT',
                            now(), now() + interval '60 seconds')
                """),
                {"id": uuid.uuid4()},
            )


@pytest.mark.asyncio
async def test_the_two_pre_authentication_scopes_do_not_share_a_budget():
    """Domain separation, asserted rather than assumed.

    Without distinct prefixes, a value that is both a plausible address and a
    plausible identity would spend one budget as the other — and, worse, two
    different scopes could collide onto one row and refuse each other.
    """
    value = "192.0.2.10"
    assert source_ip_scope(value) != auth_subject_scope(value)


@pytest.mark.asyncio
async def test_the_digest_depends_on_the_secret():
    """A digest anyone can recompute is not a protection.

    If the keyed digest were really an unsalted hash, changing the secret would
    not change the output, and `admission.rate_counter` would be reversible to
    an email address by anyone holding a candidate list.
    """
    from app.core.config import get_settings

    settings = get_settings()
    original = settings.admission_identity_secret
    address = "victim@example.ca"
    try:
        first = auth_subject_scope(address)
        settings.admission_identity_secret = "a-different-secret-entirely-000000"
        assert auth_subject_scope(address) != first
    finally:
        settings.admission_identity_secret = original
    assert auth_subject_scope(address) == first


@pytest.mark.asyncio
async def test_the_production_secret_default_is_refused_at_startup():
    """The dev default must not be able to reach production.

    Checked at construction rather than left to a deployment checklist: a
    checklist failure is silent, and this one is a boot failure.
    """
    from pydantic import ValidationError as PydanticValidationError

    from app.core.config import Settings

    with pytest.raises(PydanticValidationError):
        Settings(environment="production",
                 admission_identity_secret="dev-insecure-change-me")
    with pytest.raises(PydanticValidationError):
        Settings(environment="production", admission_identity_secret="too-short")

    # A real secret is accepted, so the test is not passing because every
    # production Settings raises.
    Settings(environment="production",
             admission_identity_secret="x" * 48)


@pytest.mark.asyncio
async def test_a_rejection_still_names_no_scope(client):
    """The 429 says AUTH_RATE_LIMIT and nothing about which limit fired.

    Telling the caller "the identity limit refused you" would say the platform
    is tracking that address, which is one bit more than it should learn from a
    login form.
    """
    email = f"quiet_{uuid.uuid4().hex[:10]}@test.ca"
    async with unit_of_work(actor_type="system") as session:
        service = AdmissionService(session)
        for _ in range(40):
            await service.charge_auth_attempt(
                source_scope_id=f"throwaway-{uuid.uuid4().hex}",
                subject_scope_id=auth_subject_scope(email),
            )

    body = (await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD})).json()

    assert body["error_code"] == "AUTH_RATE_LIMIT"
    serialized = str(body).lower()
    for leak in ("subject", "identity", "ip", "address", "scope", "counter", email):
        assert leak.lower() not in serialized, f"rejection body leaked {leak!r}"


# ---------------------------------------------------------------------------
# §20 — growth and retention
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_closed_windows_are_purged_and_live_ones_are_not():
    """Retention is load-bearing now, not housekeeping.

    The AUTH_SUBJECT scope is keyed on what the caller TYPES, so a stuffing run
    through a million addresses writes a million rows. The purge must remove
    what no decision can depend on — and must not remove a window that is still
    deciding, which would hand an attacker a fresh allowance.
    """
    now = datetime.now(tz=UTC)
    stale_scope = f"stale-{uuid.uuid4().hex}"
    live_scope = f"live-{uuid.uuid4().hex}"

    async with unit_of_work(actor_type="system") as session:
        await session.execute(
            text("""
                INSERT INTO admission.rate_counter
                    (scope_type, scope_id, operation_code, window_start,
                     request_count, updated_at)
                VALUES ('AUTH_SUBJECT', :stale, 'AUTH_ATTEMPT', :old, 9, now()),
                       ('AUTH_SUBJECT', :live,  'AUTH_ATTEMPT', :new, 9, now())
            """),
            {"stale": stale_scope, "live": live_scope,
             "old": now - timedelta(hours=6), "new": now},
        )

    async def remaining(scope: str) -> int:
        async with unit_of_work(actor_type="admin") as session:
            value = await session.scalar(
                text("SELECT count(*) FROM admission.rate_counter "
                     "WHERE scope_id = :s"),
                {"s": scope},
            )
            return int(value or 0)

    assert await remaining(stale_scope) == 1
    async with unit_of_work(actor_type="system") as session:
        removed = await AdmissionService(session).purge(now=now)

    assert removed["rate_counters_deleted"] >= 1
    assert await remaining(stale_scope) == 0, "a dead window survived the purge"
    assert await remaining(live_scope) == 1, (
        "the purge removed a window that was still deciding"
    )


@pytest.mark.asyncio
async def test_the_purge_is_bounded_per_call():
    """One delete over a very large table would hold locks across the hot path.

    A `batch` of one must remove exactly one row, which is the observable form
    of "this is bounded and can be drained over several runs".
    """
    now = datetime.now(tz=UTC)
    marker = f"batched-{uuid.uuid4().hex}"
    async with unit_of_work(actor_type="system") as session:
        await session.execute(
            text("""
                INSERT INTO admission.rate_counter
                    (scope_type, scope_id, operation_code, window_start,
                     request_count, updated_at)
                SELECT 'AUTH_SUBJECT', :s, 'AUTH_ATTEMPT',
                       CAST(:old AS timestamptz) - (n * interval '1 minute'),
                       1, now()
                  FROM generate_series(1, 5) AS n
            """),
            {"s": marker, "old": now - timedelta(hours=6)},
        )

    async with unit_of_work(actor_type="system") as session:
        removed = await AdmissionService(session).purge(now=now, batch=1)
    assert removed["rate_counters_deleted"] == 1


@pytest.mark.asyncio
async def test_a_live_lease_is_never_purged():
    """Purging a lease that is still holding a slot would return quota to a
    caller whose expensive work is still running."""
    now = datetime.now(tz=UTC)
    scope = str(uuid.uuid4())
    async with unit_of_work(actor_type="system") as session:
        ticket = await AdmissionService(session).admit(
            OperationClass.OPTIMIZATION_RUN, scope_id=scope,
            scope_type=ScopeType.USER,
        )
        assert ticket.holds_lease

    async with unit_of_work(actor_type="system") as session:
        service = AdmissionService(session)
        await service.purge(now=now + timedelta(days=365))
        assert await service.active_count(
            OperationClass.OPTIMIZATION_RUN, scope_id=scope) == 1

    async with unit_of_work(actor_type="system") as session:
        await AdmissionService(session).release_ticket(ticket)


def test_the_unwired_ledger_gives_a_reason_not_just_a_name():
    """An entry with an empty reason would be closure by enum count wearing a
    dict. Each one has to say what was inspected and what was concluded."""
    for operation, reason in UNWIRED_BY_DESIGN.items():
        assert len(reason) > 120, f"{operation} is listed without a real reason"
        assert any(
            verdict in reason for verdict in (
                "REACHABLE_ADMISSION_NOT_NEEDED",
                "RESERVED_FUTURE_CAPABILITY",
                "UNSUPPORTED_NO_PRODUCTION_CALL_SITE",
            )
        ), f"{operation} carries no classification verdict"
