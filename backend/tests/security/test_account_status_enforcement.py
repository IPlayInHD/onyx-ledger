"""A suspended or closed account must stop working — everywhere.

THE GAP THIS CLOSES. `assert_may_act` read exactly one thing: the account's
deletion lifecycle state. `identity.user_account.status` was never consulted on
any authenticated path. An operator could suspend an account and it would keep
its session, keep authenticating, and keep writing tax data, because nothing in
the request path ever looked at the column the suspension was written to. The
control existed in the schema and nowhere in the code.

Three surfaces have to agree, and they are tested separately because they fail
separately:

  login    — a suspended account must not obtain a token
  refresh  — a token family already issued must not be renewable
  requests — an access token still inside its lifetime must stop working

The third is the one that matters most. Access tokens here are self-contained
and short-lived but not instant: without a check on the request path, suspending
an account leaves it fully operational until the token happens to expire.
"""
from __future__ import annotations

import contextlib
import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn

PASSWORD = "supersecret1"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


@contextlib.contextmanager
def owner_cursor():
    """A superuser cursor, for arranging state the runtime cannot set itself.

    The runtime role has no business suspending accounts, so the test does what
    an operator would do — change the row out of band — rather than inventing an
    application path that does not exist.
    """
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        yield conn.cursor()
    finally:
        conn.close()


async def _register(client) -> tuple[str, uuid.UUID, str]:
    email = f"status_{uuid.uuid4().hex[:10]}@example.com"
    assert (
        await client.post(
            "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
        )
    ).status_code == 201
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    body = login.json()
    me = await client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    return body["access_token"], uuid.UUID(me.json()["id"]), email, body["refresh_token"]


def _set_status(user_id: uuid.UUID, status: str) -> None:
    with owner_cursor() as cur:
        cur.execute(
            "UPDATE identity.user_account SET status = %s WHERE id = %s",
            (status, str(user_id)),
        )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("status", ["suspended", "closed"])
async def test_a_live_token_stops_working_when_the_account_is_suspended(client, status):
    """The request path, which had no check at all."""
    token, user_id, _email, _refresh = await _register(client)

    # Non-vacuity: the very same token works before the status changes, so a
    # failure below is the suspension and not a broken fixture.
    assert (await client.get("/api/v1/users/me", headers=_headers(token))).status_code == 200

    _set_status(user_id, status)

    response = await client.get("/api/v1/users/me", headers=_headers(token))
    assert response.status_code == 403, response.text
    # 403 rather than 401: the caller authenticated fine and is refused for what
    # the account is. A 401 would invite a client to retry a login that is also
    # going to be refused.


@pytest.mark.parametrize("status", ["suspended", "closed"])
async def test_a_suspended_account_cannot_log_in(client, status):
    _token, user_id, email, _refresh = await _register(client)
    _set_status(user_id, status)

    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("status", ["suspended", "closed"])
async def test_a_suspended_account_cannot_refresh_an_existing_session(client, status):
    """Revoking future access is not enough if the session renews itself."""
    _token, user_id, _email, refresh = await _register(client)
    _set_status(user_id, status)

    response = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert response.status_code == 403, response.text


async def test_the_refusal_does_not_say_which_state_the_account_is_in(client):
    """"Suspended" and "closed" are operational facts, not the customer's to
    read out of an error body. The distinction is also exactly the kind of
    detail that turns an error into an enumeration oracle."""
    token, user_id, _email, _refresh = await _register(client)
    _set_status(user_id, "suspended")

    body = (await client.get("/api/v1/users/me", headers=_headers(token))).text.lower()
    assert "suspend" not in body
    assert "closed" not in body


async def test_an_active_account_is_unaffected(client):
    """The control. Every assertion above is "this stops working", which an
    application that refused everybody would satisfy completely."""
    token, user_id, email, refresh = await _register(client)

    _set_status(user_id, "active")

    assert (await client.get("/api/v1/users/me", headers=_headers(token))).status_code == 200
    assert (
        await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    ).status_code == 200
    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    ).status_code == 200


async def test_pending_verification_is_deliberately_still_allowed(client):
    """A decision, recorded as a test so it cannot drift into being an accident.

    `pending_verification` is NOT in the blocking set. Registration currently
    creates accounts `active` and there is no email-verification flow, so
    blocking the status would lock out any account that reached it with no way
    to clear it. When verification ships, this test is where the policy changes:
    it should be inverted, not deleted.
    """
    from app.services.privacy.lifecycle import BLOCKING_ACCOUNT_STATUSES

    assert "pending_verification" not in BLOCKING_ACCOUNT_STATUSES

    token, user_id, _email, _refresh = await _register(client)
    _set_status(user_id, "pending_verification")
    assert (await client.get("/api/v1/users/me", headers=_headers(token))).status_code == 200
