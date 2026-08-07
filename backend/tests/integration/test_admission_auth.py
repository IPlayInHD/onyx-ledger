"""Credential-surface throttling (Entry 10 Phase 2, §2–§6).

The login endpoint is the only surface where admission has to decide before it
knows who the caller is, and the only one where the work being bounded is
deliberately expensive by design — Argon2id is slow on purpose. These tests
assert the two things that follow from that: the throttle engages, and it
engages BEFORE the expensive work rather than after it.

WHY THE WINDOW IS PINNED
------------------------
Rate control is a fixed one-minute window keyed on wall clock. A test that
pre-fills an allowance at 12:00:59 and asserts a refusal at 12:01:00 asserts
against a window that no longer exists, and fails on a system that behaved
perfectly. `_wait_for_room_in_the_window` moves the start of each such test far
enough from the boundary that the fill and the assertion cannot straddle it.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.database.session import unit_of_work
from app.services.admission.identity import auth_subject_scope, source_ip_scope
from app.services.admission.policy import POLICIES, OperationClass
from app.services.admission.service import AdmissionService

POLICY = POLICIES[OperationClass.AUTH_ATTEMPT]
PASSWORD = "supersecret1"


async def _wait_for_room_in_the_window(seconds_needed: int = 10) -> None:
    """Start clear of the next window boundary."""
    remaining = 60 - datetime.now(tz=UTC).second
    if remaining < seconds_needed:
        await asyncio.sleep(remaining + 0.2)


async def _fill(*, subject: str | None = None, source: str | None = None,
                to: int | None = None) -> None:
    """Spend an allowance directly, through the production code path.

    Only one scope is charged per call — the other gets a throwaway key — so a
    test can exhaust the identity budget without touching the address budget,
    and vice versa. That separation is what makes it possible to prove the two
    limits are actually independent rather than one counter wearing two names.
    """
    async with unit_of_work(actor_type="system") as session:
        service = AdmissionService(session)
        for _ in range(to or 0):
            await service.charge_auth_attempt(
                source_scope_id=(
                    source_ip_scope(source) if source
                    else f"throwaway-{uuid.uuid4().hex}"
                ),
                subject_scope_id=(
                    auth_subject_scope(subject) if subject
                    else f"throwaway-{uuid.uuid4().hex}"
                ),
            )


async def _auth_session_count(email: str) -> int:
    """Live sessions for one account. A successful login creates one, so this
    counts "the handler got as far as issuing tokens"."""
    async with unit_of_work(actor_type="admin") as session:
        count = await session.scalar(
            text("""
                SELECT count(*)
                  FROM identity.auth_session s
                  JOIN identity.user_account u ON u.id = s.user_id
                 WHERE u.email = :e
            """),
            {"e": email},
        )
        return int(count or 0)


def _address_of(client) -> str:
    return client._transport.client[0]  # the ASGI peer the fixture assigned


async def _register(client, email: str) -> None:
    response = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_guessing_at_one_account_is_refused_after_its_allowance(client):
    """The per-identity limit engages, with the auth-specific code."""
    await _wait_for_room_in_the_window()
    email = f"guessed_{uuid.uuid4().hex[:10]}@test.ca"
    await _register(client, email)

    allowance = POLICY.rate_allowance
    assert allowance is not None
    await _fill(subject=email, to=allowance)

    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "wrong-password"}
    )

    assert response.status_code == 429, response.text
    assert response.headers["Retry-After"] == str(POLICY.retry_after_seconds)
    body = response.json()
    assert body["operation_code"] == "AUTH_ATTEMPT"
    assert body["error_code"] == "AUTH_RATE_LIMIT"


@pytest.mark.asyncio
async def test_a_refused_attempt_never_reaches_the_password_hash(client):
    """THE ordering invariant, asserted on the status code rather than a timer.

    The sharpest available signal: send a WRONG password to a throttled
    identity. If the credential path had run first, Argon2 would have rejected
    the password and the answer would be 401. A 429 can only be produced by
    something that ran BEFORE the verification.

    Reinforced with a second attempt using the CORRECT password: it is refused
    too, and no session is issued — so the handler did not reach the point of
    minting tokens either.

    Asserting on elapsed time instead would measure the runner, not the code.
    A failed-login marker row is no use here either: `AuthService` adds the
    `login_event` and then raises, so the row rolls back with the request's
    transaction and a failed attempt records nothing to count.
    """
    await _wait_for_room_in_the_window()
    email = f"ordering_{uuid.uuid4().hex[:10]}@test.ca"
    await _register(client, email)

    # Control: while there is allowance, a wrong password is answered by the
    # credential path — 401, not 429. This is what must stop happening.
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "wrong-password"}
    )
    assert wrong.status_code == 401, wrong.text

    sessions_before = await _auth_session_count(email)

    allowance = POLICY.rate_allowance
    assert allowance is not None
    await _fill(subject=email, to=allowance)

    throttled = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "wrong-password"}
    )
    assert throttled.status_code == 429, (
        f"expected the throttle to answer before Argon2, got {throttled.status_code}"
    )

    with_right_password = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert with_right_password.status_code == 429, with_right_password.text
    assert await _auth_session_count(email) == sessions_before, (
        "a refused attempt still issued a session"
    )


@pytest.mark.asyncio
async def test_a_refusal_does_not_disclose_whether_the_account_exists(client):
    """A real address and an invented one are refused identically.

    If the throttle consulted the user table — or if the limit differed for a
    known address — the login surface would answer "does this account exist?"
    faster and more cheaply than logging in does.
    """
    await _wait_for_room_in_the_window()
    real = f"exists_{uuid.uuid4().hex[:10]}@test.ca"
    await _register(client, real)
    invented = f"absent_{uuid.uuid4().hex[:10]}@test.ca"

    allowance = POLICY.rate_allowance
    assert allowance is not None
    await _fill(subject=real, to=allowance)
    await _fill(subject=invented, to=allowance)

    real_body = (await client.post(
        "/api/v1/auth/login", json={"email": real, "password": PASSWORD})).json()
    invented_body = (await client.post(
        "/api/v1/auth/login", json={"email": invented, "password": PASSWORD})).json()

    real_body.pop("correlation_id")
    invented_body.pop("correlation_id")
    assert real_body == invented_body, (
        "the rejection differs between a real and an invented address"
    )


@pytest.mark.asyncio
async def test_stuffing_many_accounts_from_one_address_is_refused(client):
    """The per-address limit engages where the per-identity limit cannot.

    Every attempt names a DIFFERENT address, so no identity counter ever gets
    past one. Only the source counter can catch this shape — which is the whole
    reason the second scope exists.
    """
    await _wait_for_room_in_the_window(seconds_needed=20)
    source = _address_of(client)

    source_allowance = POLICY.source_rate_allowance
    assert source_allowance is not None
    await _fill(source=source, to=source_allowance)

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": f"never_seen_{uuid.uuid4().hex[:10]}@test.ca",
              "password": PASSWORD},
    )
    assert response.status_code == 429, response.text
    assert response.json()["error_code"] == "AUTH_RATE_LIMIT"


@pytest.mark.asyncio
async def test_an_attempt_refused_by_one_scope_still_charges_the_other(client):
    """Both counters advance on every attempt.

    This is the regression test for a real defect in the first draft: the
    rejection was raised inside the transaction that had just incremented the
    source counter, so the rollback undid it. An attacker parked on one account
    would have filled that account's counter and then run indefinitely without
    the source counter — the one that catches stuffing — ever moving.
    """
    await _wait_for_room_in_the_window()
    email = f"paired_{uuid.uuid4().hex[:10]}@test.ca"
    source = _address_of(client)

    identity_allowance = POLICY.rate_allowance
    assert identity_allowance is not None
    await _fill(subject=email, to=identity_allowance)

    async def source_count() -> int:
        async with unit_of_work(actor_type="admin") as session:
            value = await session.scalar(
                text("""
                    SELECT coalesce(sum(request_count), 0)
                      FROM admission.rate_counter
                     WHERE scope_type = 'IP' AND scope_id = :s
                       AND operation_code = 'AUTH_ATTEMPT'
                """),
                {"s": source_ip_scope(source)},
            )
            return int(value or 0)

    before = await source_count()
    refused = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert refused.status_code == 429, refused.text
    assert await source_count() == before + 1, (
        "the identity limit refused the attempt and the source counter never moved"
    )


@pytest.mark.asyncio
async def test_a_forwarded_header_cannot_change_the_rate_limit_key(client):
    """A caller must not be able to name its own scope.

    Two requests carrying different `X-Forwarded-For` values must land in the
    SAME source bucket, because the key comes from the transport peer. If the
    header were trusted, a fresh value per request would be a fresh allowance
    per request, which is the same as no limit at all.
    """
    await _wait_for_room_in_the_window()
    source = _address_of(client)

    async def source_count() -> int:
        async with unit_of_work(actor_type="admin") as session:
            value = await session.scalar(
                text("""
                    SELECT coalesce(sum(request_count), 0)
                      FROM admission.rate_counter
                     WHERE scope_type = 'IP' AND scope_id = :s
                       AND operation_code = 'AUTH_ATTEMPT'
                """),
                {"s": source_ip_scope(source)},
            )
            return int(value or 0)

    before = await source_count()
    for spoofed in ("203.0.113.7", "198.51.100.4", "2001:db8::1"):
        await client.post(
            "/api/v1/auth/login",
            json={"email": f"spoof_{uuid.uuid4().hex[:8]}@test.ca", "password": PASSWORD},
            headers={"X-Forwarded-For": spoofed, "X-Real-IP": spoofed,
                     "Forwarded": f"for={spoofed}"},
        )

    assert await source_count() == before + 3, (
        "a forwarded header moved the attempts out of the real source bucket"
    )


@pytest.mark.asyncio
async def test_the_admission_store_holds_no_address_in_the_clear(client):
    """Nothing in the admission tables is an email or a source address.

    The counters are ordinary rows with ordinary backups. Written in the clear,
    they would be a second copy of the account list — plus, for failed logins, a
    list of addresses that have NO account, which the product never had a reason
    to keep.
    """
    email = f"opaque_{uuid.uuid4().hex[:10]}@test.ca"
    source = _address_of(client)
    await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )

    async with unit_of_work(actor_type="admin") as session:
        rows = (await session.execute(
            text("SELECT scope_id FROM admission.rate_counter "
                 "WHERE operation_code = 'AUTH_ATTEMPT'")
        )).scalars().all()

    stored = set(rows)
    assert stored, "the attempt was not recorded at all"
    assert email not in stored
    assert source not in stored
    assert not any("@" in value for value in stored), (
        "an email address reached the admission store"
    )
    # And the digest for exactly this attempt IS there, so the assertion above
    # is not passing merely because nothing was written.
    assert auth_subject_scope(email) in stored
    assert source_ip_scope(source) in stored


@pytest.mark.asyncio
async def test_spellings_of_one_address_share_one_allowance(client):
    """`Alice@Example.CA ` and `alice@example.ca` are one budget, not two.

    Otherwise an attacker gets the identity allowance once per spelling, and the
    per-identity limit bounds nothing.
    """
    base = f"folded_{uuid.uuid4().hex[:10]}@test.ca"
    variants = [base, base.upper(), f"  {base}  ", base.capitalize()]
    assert len({auth_subject_scope(v) for v in variants}) == 1

    # A different address must still be a different budget — the folding must
    # merge spellings, not collapse distinct identities.
    assert auth_subject_scope(base) != auth_subject_scope(f"x{base}")
