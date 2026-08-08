"""Do the security effects of a FAILED authentication survive? (PD-6, §29)

`unit_of_work` wraps the request in `session.begin()`, which rolls back when the
body raises. `AuthService` stages its failure handling and then raises inside
that transaction, so everything it staged is discarded on the way out.

That was recorded in Entry 11A as PD-6, a missing audit record. Looking at the
same two lines while wiring the deletion cutoff through them shows it is not
only a log line: refresh-token REUSE DETECTION revokes the session family and
then raises, and the revocation goes with it. Single-use refresh tokens exist so
that a stolen token gets the whole family killed the moment the real owner's
token is replayed; a revocation that rolls back is the feature not working.

These tests fail against the unfixed code, which is the only reason to trust
them.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn

PASSWORD = "supersecret1"


def _owner_cursor():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn, conn.cursor()


def _count(sql: str, *params) -> int:
    conn, cur = _owner_cursor()
    try:
        cur.execute(sql, params)
        return cur.fetchone()[0]
    finally:
        conn.close()


async def _register(client) -> tuple[str, str, str]:
    email = f"authdur_{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post("/api/v1/auth/register",
                                 json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    login = await client.post("/api/v1/auth/login",
                              json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    body = login.json()
    return email, body["access_token"], body["refresh_token"]


@pytest.mark.asyncio
async def test_a_failed_login_is_actually_recorded(client):
    """PD-6. Without a durable record, a brute-force run against one account
    leaves no trace in `login_event` at all — the table would only ever contain
    the successes."""
    email, _, _ = await _register(client)

    for _ in range(3):
        response = await client.post("/api/v1/auth/login",
                                     json={"email": email, "password": "wrong-one"})
        assert response.status_code == 401, response.status_code

    failures = _count(
        "SELECT count(*) FROM identity.login_event "
        "WHERE email_tried = %s AND event_type = 'failure'", email)
    assert failures == 3, (
        f"{failures} of 3 failed logins were recorded; the rest were rolled "
        "back with the transaction that raised Unauthorized"
    )


@pytest.mark.asyncio
async def test_a_login_failure_for_an_unknown_address_is_recorded_too(client):
    """The account-enumeration case. An attacker probing addresses that do not
    exist is exactly who this record is for, and `user_id` is NULL there — so
    the row has to be written on a path that has no account to hang it on."""
    unknown = f"nobody_{uuid.uuid4().hex[:10]}@example.com"

    response = await client.post("/api/v1/auth/login",
                                 json={"email": unknown, "password": PASSWORD})
    assert response.status_code == 401

    assert _count(
        "SELECT count(*) FROM identity.login_event "
        "WHERE email_tried = %s AND event_type = 'failure'", unknown) == 1


@pytest.mark.asyncio
async def test_replaying_a_rotated_refresh_token_really_revokes_the_family(client):
    """The one that is not about logging.

    Refresh tokens are single use. Presenting one that has already been rotated
    means either the client is confused or somebody else has the token, and the
    response is to revoke every session the account holds. That revocation was
    staged and then thrown away by the raise that followed it, so a replayed
    token produced a 401 and left every other session working.
    """
    _, _, refresh = await _register(client)

    rotated = await client.post("/api/v1/auth/refresh",
                                json={"refresh_token": refresh})
    assert rotated.status_code == 200, rotated.text
    live = rotated.json()["refresh_token"]

    # The stolen token is replayed after the real owner has rotated it.
    replay = await client.post("/api/v1/auth/refresh",
                               json={"refresh_token": refresh})
    assert replay.status_code == 401, replay.status_code

    # Every session in the family must now be dead, including the good one.
    after = await client.post("/api/v1/auth/refresh",
                              json={"refresh_token": live})
    assert after.status_code == 401, (
        "the session family survived a detected token replay, so a stolen "
        "refresh token keeps working after the theft has been detected"
    )
