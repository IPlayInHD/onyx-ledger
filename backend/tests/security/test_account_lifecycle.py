"""Account deletion lifecycle (Entry 11B1).

The property under test is not "data is gone" — 11B1 deletes nothing. It is that
a deletion request cannot be lost, bypassed or forged, and that an account past
its cutoff stops producing new data.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid

import psycopg2
import pytest
from sqlalchemy import text

from app.database.session import unit_of_work
from app.services.privacy import (
    AccountLifecycleService,
    LifecycleFailureCode,
    LifecycleState,
)
from tests.conftest import owner_dsn

PASSWORD = "supersecret1"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


async def _register(client) -> tuple[str, uuid.UUID, str]:
    email = f"lifecycle_{uuid.uuid4().hex[:10]}@example.com"
    assert (await client.post("/api/v1/auth/register",
                              json={"email": email, "password": PASSWORD})
            ).status_code == 201
    login = await client.post("/api/v1/auth/login",
                              json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    me = await client.get("/api/v1/users/me",
                          headers={"Authorization": f"Bearer {token}"})
    return token, uuid.UUID(me.json()["id"]), email


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _state_of(user_id: uuid.UUID) -> str | None:
    async with unit_of_work(actor_type="admin") as session:
        return await session.scalar(
            text("SELECT identity.account_deletion_state(:uid)"), {"uid": user_id})


@contextlib.contextmanager
def owner_cursor():
    """A superuser cursor, for out-of-band verification only.

    Lifecycle rows are under FORCE ROW LEVEL SECURITY and the runtime role has
    no UPDATE privilege, so a test that inspects or arranges the table through
    an ordinary `unit_of_work` reads an empty result and writes nothing —
    silently, because both controls are designed to be silent. Every assertion
    about what is actually stored therefore goes through the owner.
    """
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        yield conn.cursor()
    finally:
        conn.close()


def _lifecycle_row(user_id: uuid.UUID, columns: str) -> tuple | None:
    with owner_cursor() as cur:
        cur.execute(
            f"SELECT {columns} FROM identity.account_lifecycle WHERE user_id = %s",
            (str(user_id),))
        return cur.fetchone()


def _count(sql: str, *params) -> int:
    with owner_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def _age_claim(user_id: uuid.UUID, interval: str = "2 hours") -> None:
    """Backdate a claim so it looks like one a dead worker left behind.

    Through the owner, because the runtime role has no UPDATE on this table —
    an ordinary session would report success and change nothing, and the test
    would then be asserting against a claim that was never actually stale.
    """
    with owner_cursor() as cur:
        cur.execute(
            f"UPDATE identity.account_lifecycle "
            f"SET claimed_at = now() - interval '{interval}' WHERE user_id = %s",
            (str(user_id),))
        assert cur.rowcount == 1, "the claim was not aged"


# ---------------------------------------------------------------------------
# request lifecycle
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_user_can_request_deletion_of_their_own_account(client):
    token, user_id, _ = await _register(client)

    response = await client.post("/api/v1/account/deletion", headers=_headers(token))
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "deletion_requested"

    assert await _state_of(user_id) == LifecycleState.DELETION_REQUESTED.value


@pytest.mark.asyncio
async def test_the_deletion_request_is_durable_and_carries_a_database_cutoff(client):
    """The cutoff must come from the database clock, not an API process.

    Later purge phases compare user data against it, so a worker-local
    timestamp would make the comparison depend on whichever machine happened to
    handle the request.
    """
    token, user_id, _ = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    row = _lifecycle_row(user_id, """
        requested_at, state, revision, completed_at,
        requested_at BETWEEN now() - interval '1 minute' AND now()
    """)

    assert row is not None, "the deletion request was not durable"
    assert row[1] == LifecycleState.DELETION_REQUESTED.value
    assert row[2] == 1
    assert row[3] is None, "a fresh request must not carry a completion time"
    assert row[4] is True, "requested_at did not come from the database clock"


@pytest.mark.asyncio
async def test_repeating_the_request_yields_one_lifecycle(client):
    token, user_id, _ = await _register(client)

    for _ in range(20):
        response = await client.post("/api/v1/account/deletion",
                                     headers=_headers(token))
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "deletion_requested"

    rows = _count(
        "SELECT count(*) FROM identity.account_lifecycle WHERE user_id = %s",
        str(user_id))
    requested_events = _count(
        "SELECT count(*) FROM identity.account_lifecycle_event "
        "WHERE user_id = %s AND event_code = 'DELETION_REQUESTED'", str(user_id))
    assert rows == 1
    assert requested_events == 1, (
        "a repeated request produced a second lifecycle event; the second "
        "request must be a no-op, not a re-run"
    )


@pytest.mark.asyncio
async def test_concurrent_requests_yield_one_lifecycle(client):
    """Ten at once. Idempotency is the primary key, not a check-then-insert, so
    there is no window between looking and creating."""
    token, user_id, _ = await _register(client)

    responses = await asyncio.gather(*(
        client.post("/api/v1/account/deletion", headers=_headers(token))
        for _ in range(10)
    ))
    assert {r.status_code for r in responses} == {202}, (
        [r.status_code for r in responses]
    )

    assert _count(
        "SELECT count(*) FROM identity.account_lifecycle WHERE user_id = %s",
        str(user_id)) == 1


# ---------------------------------------------------------------------------
# tenant isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_user_cannot_delete_or_read_another_account(client):
    """There is no account parameter to abuse — the endpoint resolves the
    account from the token and nowhere else. This asserts the consequence: B's
    lifecycle is untouched and invisible to A."""
    token_a, user_a, _ = await _register(client)
    token_b, user_b, _ = await _register(client)

    await client.post("/api/v1/account/deletion", headers=_headers(token_a))

    assert await _state_of(user_a) == LifecycleState.DELETION_REQUESTED.value
    assert await _state_of(user_b) is None, "B's account was affected by A's request"

    # B is unaffected and still fully usable.
    status_b = await client.get("/api/v1/account/deletion", headers=_headers(token_b))
    assert status_b.status_code == 200
    assert status_b.json()["status"] == "active"


@pytest.mark.asyncio
async def test_rls_hides_another_accounts_lifecycle_row():
    """The database boundary, independent of the API."""
    victim = uuid.uuid4()
    with owner_cursor() as cur:
        cur.execute("INSERT INTO identity.user_account (id, email, status) "
                    "VALUES (%s, %s, 'active')",
                    (str(victim), f"rlsvictim_{uuid.uuid4().hex[:8]}@example.com"))
        cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                    "VALUES (%s, 'DELETION_REQUESTED')", (str(victim),))

    stranger = uuid.uuid4()
    async with unit_of_work(user_id=stranger, actor_type="user") as session:
        seen = await session.scalar(
            text("SELECT count(*) FROM identity.account_lifecycle"))
    assert seen == 0, "a user session could see another account's lifecycle row"


# ---------------------------------------------------------------------------
# forgery
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_ordinary_session_cannot_advance_or_complete_a_lifecycle(client):
    """The runtime role has SELECT and INSERT and NO UPDATE. Even with a valid
    `app.user_id` for the row's own owner, progress cannot be forged."""
    token, user_id, _ = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        with pytest.raises(Exception) as caught:
            await session.execute(
                text("""UPDATE identity.account_lifecycle
                           SET state = 'COMPLETE', completed_at = now()
                         WHERE user_id = :uid"""), {"uid": user_id})
        assert "permission denied" in str(caught.value).lower(), str(caught.value)

    assert await _state_of(user_id) == LifecycleState.DELETION_REQUESTED.value


def test_illegal_transitions_are_refused_by_the_database():
    """Enforced in the database as well as the service, because the service is
    only the API that *should* be used."""
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        cur = conn.cursor()
        user = uuid.uuid4()
        cur.execute(
            "INSERT INTO identity.user_account (id, email, status) "
            "VALUES (%s, %s, 'active')",
            (str(user), f"trans_{uuid.uuid4().hex[:8]}@example.com"))

        # A lifecycle may only begin at its beginning.
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                        "VALUES (%s, 'PURGING')", (str(user),))

        cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                    "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))

        # The jump that matters: requested straight to complete.
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE identity.account_lifecycle SET state = 'COMPLETE', "
                        "completed_at = now() WHERE user_id = %s", (str(user),))

        # The cutoff is immutable.
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE identity.account_lifecycle "
                        "SET requested_at = now() - interval '1 day' "
                        "WHERE user_id = %s", (str(user),))

        # A legal step is allowed, and bumps the revision.
        cur.execute("UPDATE identity.account_lifecycle SET state = 'ACCESS_DISABLED' "
                    "WHERE user_id = %s RETURNING revision", (str(user),))
        assert cur.fetchone()[0] == 2

        # The row cannot be deleted — it is the deletion record.
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("DELETE FROM identity.account_lifecycle WHERE user_id = %s",
                        (str(user),))
    finally:
        conn.close()


def test_a_lifecycle_cannot_be_completed_without_a_completion_time():
    """The constraint that makes "marked done before the purge ran" a database
    error rather than a support ticket."""
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        cur = conn.cursor()
        user = uuid.uuid4()
        cur.execute("INSERT INTO identity.user_account (id, email, status) "
                    "VALUES (%s, %s, 'active')",
                    (str(user), f"complete_{uuid.uuid4().hex[:8]}@example.com"))
        cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                    "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
        for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
            cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                        "WHERE user_id = %s", (state, str(user)))
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE identity.account_lifecycle SET state = 'COMPLETE' "
                        "WHERE user_id = %s", (str(user),))
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_the_service_refuses_to_mark_a_lifecycle_complete():
    """Belt to the trigger's braces. Entry 11B1 implements no purge, so nothing
    in this codebase has earned the right to say an account's data is gone."""
    async with unit_of_work(actor_type="admin") as session:
        service = AccountLifecycleService(session)
        claimed = type("C", (), {
            "user_id": uuid.uuid4(), "claim_token": uuid.uuid4(),
            "state": LifecycleState.PURGING, "requested_at": None, "revision": 1,
        })()
        with pytest.raises(ValueError, match="COMPLETE"):
            await service.advance(claimed, LifecycleState.COMPLETE, worker_id="w")


# ---------------------------------------------------------------------------
# authentication cutoff
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_login_is_denied_after_deletion_is_requested(client):
    token, _, email = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    response = await client.post("/api/v1/auth/login",
                                 json={"email": email, "password": PASSWORD})
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_an_existing_session_stops_authorizing_normal_endpoints(client):
    """Access tokens are self-contained and cannot be recalled, which is exactly
    why the cutoff lives on the authenticated dependency rather than only at
    login."""
    token, _, _ = await _register(client)
    before = await client.get("/api/v1/users/me", headers=_headers(token))
    assert before.status_code == 200

    await client.post("/api/v1/account/deletion", headers=_headers(token))

    after = await client.get("/api/v1/users/me", headers=_headers(token))
    assert after.status_code == 403, after.text
    body = after.json()
    assert "deleted" in body["detail"].lower()


@pytest.mark.asyncio
async def test_refresh_is_denied_and_sessions_are_revoked(client):
    email = f"refresh_{uuid.uuid4().hex[:10]}@example.com"
    assert (await client.post("/api/v1/auth/register",
                              json={"email": email, "password": PASSWORD})
            ).status_code == 201
    login = await client.post("/api/v1/auth/login",
                              json={"email": email, "password": PASSWORD})
    tokens = login.json()
    me = await client.get("/api/v1/users/me",
                          headers=_headers(tokens["access_token"]))
    user_id = uuid.UUID(me.json()["id"])

    await client.post("/api/v1/account/deletion",
                      headers=_headers(tokens["access_token"]))

    refreshed = await client.post("/api/v1/auth/refresh",
                                  json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code in (401, 403), refreshed.text

    async with unit_of_work(actor_type="admin") as session:
        live = await session.scalar(
            text("""SELECT count(*) FROM identity.auth_session
                     WHERE user_id = :uid AND revoked_at IS NULL"""),
            {"uid": user_id})
    assert live == 0, "sessions were not revoked at the deletion cutoff"


# ---------------------------------------------------------------------------
# admission + write cutoff
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/v1/analysis", {"tax_year": 2025}),
    ("post", "/api/v1/financials/income",
     {"tax_year": 2025, "income_type_code": "EMPLOYMENT", "amount": "1000.00"}),
    ("post", "/api/v1/financials/expenses",
     {"tax_year": 2025, "expense_category_code": "MEDICAL", "amount": "10.00"}),
    ("post", "/api/v1/documents",
     {"document_type_code": "T4", "filename": "a.pdf", "mime_type": "application/pdf"}),
    ("post", "/api/v1/ai/ask", {"question": "why", "tax_year": 2025}),
    ("post", "/api/v1/ai/conversations", None),
    ("put", "/api/v1/users/me/tax-profile", {"province_code": "ON"}),
])
async def test_no_new_user_work_is_admitted_after_the_cutoff(client, method, path, body):
    """Every currently reachable authenticated write, refused by ONE boundary.

    They are blocked by the lifecycle check on `db_authed`, which is why a route
    added tomorrow is covered without anyone remembering.
    """
    token, _, _ = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    call = getattr(client, method)
    response = await (call(path, json=body, headers=_headers(token)) if body is not None
                      else call(path, headers=_headers(token)))
    assert response.status_code == 403, f"{path} -> {response.status_code}"


@pytest.mark.asyncio
async def test_reads_are_refused_too(client):
    """A deleting account is frozen, not read-only: leaving reads open would
    keep the product usable for an account that asked to be gone."""
    token, _, _ = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    for path in ("/api/v1/users/me", "/api/v1/analysis", "/api/v1/documents"):
        response = await client.get(path, headers=_headers(token))
        assert response.status_code == 403, f"{path} -> {response.status_code}"


@pytest.mark.asyncio
async def test_admission_refuses_with_its_own_reason_not_a_rate_limit(client):
    """Worker-initiated admission has no HTTP dependency to protect it, so the
    guard carries the cutoff too — with a code that does not blame the caller."""
    from app.services.admission import AdmissionRejected, OperationClass
    from app.services.admission.guard import admission_guard, user_scope

    token, user_id, _ = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    with pytest.raises(AdmissionRejected) as caught:
        async with admission_guard(OperationClass.OPTIMIZATION_RUN,
                                   scope_id=user_scope(user_id)):
            pass
    assert caught.value.reason.value == "ACCOUNT_DELETION_IN_PROGRESS"


@pytest.mark.asyncio
async def test_a_refused_admission_does_not_consume_the_accounts_rate_budget(client):
    """A deleting account did nothing wrong. Charging it would both mislabel the
    event and make it look like an attacker in the metrics."""
    from app.services.admission import AdmissionRejected, OperationClass
    from app.services.admission.guard import admission_guard, user_scope

    token, user_id, _ = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    for _ in range(5):
        with pytest.raises(AdmissionRejected):
            async with admission_guard(OperationClass.OPTIMIZATION_RUN,
                                       scope_id=user_scope(user_id)):
                pass

    async with unit_of_work(actor_type="admin") as session:
        spent = await session.scalar(
            text("""SELECT coalesce(sum(request_count), 0)
                      FROM admission.rate_counter WHERE scope_id = :s"""),
            {"s": str(user_id)})
    assert int(spent or 0) == 0, (
        "the deletion cutoff consumed the account's abuse quota"
    )


# ---------------------------------------------------------------------------
# queued work
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_worker_starting_after_the_cutoff_creates_no_user_data(client):
    """THE queue race. The task was published before the request; the worker
    picks it up afterwards. Celery revoke cannot close this — a reserved task is
    already past the queue — so the refusal has to be in the database."""
    from app.services.privacy.preflight import refuse_if_deleting

    token, user_id, _ = await _register(client)

    async with unit_of_work(actor_type="system") as session:
        assert not await refuse_if_deleting(session, user_id, task="probe"), (
            "an active account was refused"
        )

    await client.post("/api/v1/account/deletion", headers=_headers(token))

    async with unit_of_work(actor_type="system") as session:
        assert await refuse_if_deleting(session, user_id, task="probe"), (
            "a worker would have proceeded for a deleting account"
        )

    # And the real task body stops without writing anything.
    from app.database.session import engine
    from workers.tasks.analysis import run_analysis

    runs = "SELECT count(*) FROM analysis.analysis_run WHERE user_id = %s"
    before = _count(runs, str(user_id))

    # The task body calls `asyncio.run` — that is how Celery executes it in
    # production — which cannot nest inside this test's loop. Running it in a
    # worker thread reproduces the real execution shape rather than a test-only
    # one. The engine is disposed either side because its pooled asyncpg
    # connections belong to whichever loop opened them.
    await engine.dispose()
    assert await asyncio.to_thread(run_analysis.run, str(user_id), 2025) == ""
    await engine.dispose()

    assert _count(runs, str(user_id)) == before, (
        "a queued task created user data after the cutoff"
    )


# ---------------------------------------------------------------------------
# worker claim and recovery
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_worker_claims_advances_and_recovers(client):
    token, user_id, _ = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    async with unit_of_work(actor_type="admin") as session:
        service = AccountLifecycleService(session)
        claimed = await service.claim(worker_id="worker-a", batch_size=50)
    mine = [c for c in claimed if c.user_id == user_id]
    assert len(mine) == 1, "the requested account was not claimable"
    assert mine[0].requested_at is not None, "the cutoff was not handed to the worker"

    # A second worker cannot take a live claim.
    async with unit_of_work(actor_type="admin") as session:
        again = await AccountLifecycleService(session).claim(
            worker_id="worker-b", batch_size=50)
    assert user_id not in {c.user_id for c in again}

    async with unit_of_work(actor_type="admin") as session:
        assert await AccountLifecycleService(session).advance(
            mine[0], LifecycleState.ACCESS_DISABLED, worker_id="worker-a")
    assert await _state_of(user_id) == LifecycleState.ACCESS_DISABLED.value


@pytest.mark.asyncio
async def test_an_abandoned_claim_is_recovered_by_another_worker(client):
    """A worker that dies must not strand the account forever."""
    token, user_id, _ = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    async with unit_of_work(actor_type="admin") as session:
        claimed = await AccountLifecycleService(session).claim(
            worker_id="doomed-worker", batch_size=50)
    assert user_id in {c.user_id for c in claimed}

    # Age the claim past the timeout, as a dead worker's claim would age.
    _age_claim(user_id)

    async with unit_of_work(actor_type="admin") as session:
        recovered = await AccountLifecycleService(session).claim(
            worker_id="rescue-worker", batch_size=50)
    assert user_id in {c.user_id for c in recovered}, (
        "an abandoned claim was never released"
    )

    assert _count(
        "SELECT count(*) FROM identity.account_lifecycle_event "
        "WHERE user_id = %s AND event_code = 'CLAIM_RECOVERED'",
        str(user_id)) >= 1, "claim recovery was not recorded"


@pytest.mark.asyncio
async def test_a_stale_claim_token_cannot_advance_the_lifecycle(client):
    """The claim token is the authority: a worker whose claim expired and was
    taken by another cannot advance the account it no longer holds."""
    token, user_id, _ = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    async with unit_of_work(actor_type="admin") as session:
        first = (await AccountLifecycleService(session).claim(
            worker_id="worker-a", batch_size=50))
    mine = next(c for c in first if c.user_id == user_id)

    _age_claim(user_id)
    async with unit_of_work(actor_type="admin") as session:
        stolen = await AccountLifecycleService(session).claim(
            worker_id="worker-b", batch_size=50)
    assert user_id in {c.user_id for c in stolen}, "worker-b never took the claim"

    async with unit_of_work(actor_type="admin") as session:
        assert not await AccountLifecycleService(session).advance(
            mine, LifecycleState.ACCESS_DISABLED, worker_id="worker-a"), (
            "a worker advanced a lifecycle it no longer held"
        )


@pytest.mark.asyncio
async def test_a_failed_phase_records_a_closed_code_and_stays_retryable(client):
    token, user_id, _ = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    async with unit_of_work(actor_type="admin") as session:
        claimed = next(c for c in await AccountLifecycleService(session).claim(
            worker_id="worker-a", batch_size=50) if c.user_id == user_id)

    async with unit_of_work(actor_type="admin") as session:
        assert await AccountLifecycleService(session).fail(
            claimed, LifecycleFailureCode.PHASE_RETRY_REQUIRED, worker_id="worker-a")

    assert await _state_of(user_id) == LifecycleState.FAILED_RETRYABLE.value

    # And it is claimable again — a failure must not strand the account.
    async with unit_of_work(actor_type="admin") as session:
        again = await AccountLifecycleService(session).claim(
            worker_id="worker-c", batch_size=50)
    assert user_id in {c.user_id for c in again}


# ---------------------------------------------------------------------------
# privacy of the lifecycle itself
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_lifecycle_records_contain_no_personal_data(client):
    """The lifecycle must not become another place user data accumulates."""
    token, user_id, email = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    async with unit_of_work(actor_type="admin") as session:
        blob = await session.scalar(
            text("""
                SELECT coalesce(string_agg(t::text, ' '), '') FROM (
                    SELECT l.* FROM identity.account_lifecycle l
                     WHERE user_id = :uid
                ) t
            """), {"uid": user_id})
        events = await session.scalar(
            text("""
                SELECT coalesce(string_agg(t::text, ' '), '') FROM (
                    SELECT e.* FROM identity.account_lifecycle_event e
                     WHERE user_id = :uid
                ) t
            """), {"uid": user_id})

    for payload in (blob or "", events or ""):
        assert email not in payload
        assert "@" not in payload
        assert "$argon2" not in payload


@pytest.mark.asyncio
async def test_the_status_response_reveals_no_machinery(client):
    token, _, _ = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    body = (await client.get("/api/v1/account/deletion",
                             headers=_headers(token))).json()
    assert set(body) <= {"status", "requested_at"}
    serialized = str(body).lower()
    for leak in ("worker", "claim", "phase", "attempt", "purge", "table", "queue"):
        assert leak not in serialized, f"status response leaked {leak!r}"


@pytest.mark.asyncio
async def test_deletion_is_never_reported_complete_in_11b1(client):
    """11B1 deletes nothing. Reporting completion would be a lie in the one
    direction that matters."""
    token, user_id, _ = await _register(client)
    await client.post("/api/v1/account/deletion", headers=_headers(token))

    async with unit_of_work(actor_type="admin") as session:
        service = AccountLifecycleService(session)
        for _ in range(3):
            for claimed in await service.claim(worker_id="w", batch_size=50):
                if claimed.user_id != user_id:
                    continue
                nxt = {
                    LifecycleState.DELETION_REQUESTED: LifecycleState.ACCESS_DISABLED,
                    LifecycleState.ACCESS_DISABLED: LifecycleState.PURGE_PENDING,
                }.get(claimed.state)
                if nxt:
                    await service.advance(claimed, nxt, worker_id="w")

    state = await _state_of(user_id)
    assert state in (LifecycleState.ACCESS_DISABLED.value,
                     LifecycleState.PURGE_PENDING.value), state

    body = (await client.get("/api/v1/account/deletion",
                             headers=_headers(token))).json()
    assert body["status"] == "deletion_requested", (
        "the API reported completion before any purge phase ran"
    )
