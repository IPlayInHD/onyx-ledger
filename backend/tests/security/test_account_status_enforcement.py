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

from tests.conftest import owner_dsn, register_verified

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


async def _register(client) -> tuple[str, uuid.UUID, str, str]:
    email = f"status_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, email, PASSWORD)
    # Signed in again after verification, because these tests need a REFRESH
    # token as well and want both halves of one pair.
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


async def test_pending_verification_signs_in_and_cannot_act(client):
    """The decision this test used to record, INVERTED — as it said it should be.

    Its predecessor asserted `pending_verification` was fully allowed, because
    registration created `active` accounts and there was no flow to clear the
    status; it said in as many words that when verification shipped, this test
    was where the policy would change and that it should be inverted rather
    than deleted. B3 shipped it, so here is the inversion.

    THE POLICY IS TWO-SIDED and both sides are load-bearing:

      signs in    — because an account that cannot authenticate cannot ask for
                    another link except through an anonymous endpoint keyed on
                    a typed address, which is an enumeration oracle and an
                    email-flood amplifier
      cannot act  — because an unverified address is an unproven one, and an
                    account that can file tax data against it is a product
                    asserting something it has not checked

    Asserting only the second half would be satisfied by an application that
    refused the account outright, which is the failure this shape prevents.
    """
    from app.services.privacy.lifecycle import (
        BLOCKING_ACCOUNT_STATUSES,
        UNVERIFIED_ACCOUNT_STATUSES,
    )

    assert "pending_verification" in UNVERIFIED_ACCOUNT_STATUSES
    assert "pending_verification" not in BLOCKING_ACCOUNT_STATUSES, (
        "an unverified account must still be able to sign in; see the "
        "docstring above and BLOCKING_ACCOUNT_STATUSES for why"
    )

    token, user_id, email, _refresh = await _register(client)
    _set_status(user_id, "pending_verification")

    # Authentication still works, both ways in.
    signed_in = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert signed_in.status_code == 200, signed_in.text
    assert (
        await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": signed_in.json()["refresh_token"]},
        )
    ).status_code == 200

    # The application does not.
    refused = await client.get("/api/v1/users/me", headers=_headers(token))
    assert refused.status_code == 403, refused.text
    assert refused.json()["type"].endswith("email-verification-required"), refused.json()


async def test_the_unverified_refusal_is_distinguishable_from_suspension(client):
    """§22: an unverified account must not look like a generic failure.

    A frontend that cannot tell these apart shows "contact support" to somebody
    who needs "check your email", and the account is stuck for a reason nobody
    can see. Distinct `type` values are what make that impossible.
    """
    token, user_id, _email, _refresh = await _register(client)

    _set_status(user_id, "pending_verification")
    unverified = await client.get("/api/v1/users/me", headers=_headers(token))

    _set_status(user_id, "suspended")
    suspended = await client.get("/api/v1/users/me", headers=_headers(token))

    assert unverified.status_code == suspended.status_code == 403
    assert unverified.json()["type"] != suspended.json()["type"]
    assert unverified.json()["type"].endswith("email-verification-required")
    assert suspended.json()["type"].endswith("account-not-active")


async def test_suspension_beats_unverified_when_an_account_is_both(client):
    """Ordering, and it is not cosmetic.

    "Confirm your email address" offered to a suspended account is a false
    promise: confirming it would change nothing, and the customer would spend
    the afternoon clicking links instead of contacting the operator who
    suspended them.
    """
    token, user_id, _email, _refresh = await _register(client)
    _set_status(user_id, "suspended")

    # `suspended` is not `pending_verification`, so make the account genuinely
    # both by clearing the verification stamp too — the status column holds one
    # value, and suspension is the one an operator sets.
    with owner_cursor() as cur:
        cur.execute(
            "UPDATE identity.user_account SET email_verified_at = NULL WHERE id = %s",
            (str(user_id),),
        )

    refused = await client.get("/api/v1/users/me", headers=_headers(token))
    assert refused.status_code == 403
    assert refused.json()["type"].endswith("account-not-active"), refused.json()


# ---------------------------------------------------------------------------
# Email lookup is case-insensitive, everywhere it decides identity
# ---------------------------------------------------------------------------

async def test_every_identity_lookup_is_case_insensitive(client):
    """A REGRESSION GUARD for a measured defect, not a style preference.

    `identity.user_account.email` has been `citext` since the schema was
    written, so the unique index and any hand-written SQL compare addresses
    case-insensitively. The ORM mapped it as `String`, which made SQLAlchemy
    bind the parameter as varchar — and PostgreSQL resolves `citext = varchar`
    by casting the citext side DOWN to text. Every ORM lookup keyed on email
    was therefore case sensitive against a database that was not.

    Three surfaces were affected and they are tested together because they
    have to agree; a fix that repaired one and not the others would leave an
    account that can register but not sign in.

    The third is why this is a security test. The password-reset endpoint is
    non-enumerating, so silence IS its correct answer for an address it does
    not recognise — which means a customer typing their own address in a
    different case got no email, no error, and no way to tell the difference
    between "we don't know you" and "we mis-compared".
    """
    from app.domain.ports import TransactionalEmail
    from app.integrations.email import captured_emails

    local = f"Case.Guard_{uuid.uuid4().hex[:8]}"
    registered_as = f"{local}@Example.com"
    stored = f"{local}@example.com"  # EmailStr lowercases the domain only

    created = await client.post(
        "/api/v1/auth/register", json={"email": registered_as, "password": PASSWORD})
    assert created.status_code == 201, created.text
    assert created.json()["email"] == stored

    # 1. REGISTRATION refuses a duplicate that differs only in case, and does it
    #    as a 409 from the service rather than as an integrity error from the
    #    index — which would surface as a 500.
    duplicate = await client.post(
        "/api/v1/auth/register",
        json={"email": f"{local.upper()}@EXAMPLE.COM", "password": PASSWORD})
    assert duplicate.status_code == 409, duplicate.text

    # 2. LOGIN finds the account whatever case was typed.
    signed_in = await client.post(
        "/api/v1/auth/login",
        json={"email": f"{local.lower()}@example.com", "password": PASSWORD})
    assert signed_in.status_code == 200, signed_in.text

    # 3. PASSWORD RESET finds it too, and the proof is a message rather than a
    #    status code — the status is 202 either way, by design.
    asked = await client.post(
        "/api/v1/auth/password-reset",
        json={"email": f"{local.upper()}@example.com"})
    assert asked.status_code == 202, asked.text
    assert captured_emails(to=stored, kind=TransactionalEmail.PASSWORD_RESET), (
        "no reset message: the lookup missed an account that exists"
    )
