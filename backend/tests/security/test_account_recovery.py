"""Email verification and password recovery, end to end through the API.

WHAT THESE TESTS ARE ACTUALLY ABOUT. A recovery link is a credential that can
set a password, and it travels through a channel the product does not control.
Almost every assertion here is about the ways that credential must stop
working: after one use, after its window, after it is superseded, and for an
account whose lifecycle says no. The happy path is three tests; the rest is the
perimeter.

No socket is opened. The capture provider records what would have been sent and
the tests read the link out of it, which is also how a customer gets it.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from app.domain.ports import TransactionalEmail
from app.integrations.email import captured_emails
from tests.conftest import owner_dsn, register_verified, token_from_link

PASSWORD = "supersecret1"
NEW_PASSWORD = "a-completely-different-one-9"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _owner_cursor():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _sql(statement: str, params: tuple) -> None:
    """Arrange state the runtime role has no business arranging itself.

    Expiry and account status are set out of band on purpose: the alternative
    is sleeping through a real TTL, or inventing an application path that lets
    a request age its own token — which would be a worse thing to exist than
    the test is worth.
    """
    conn = _owner_cursor()
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
    finally:
        conn.close()


def _age_tokens(table: str, user_id: uuid.UUID, *, minutes: int) -> None:
    _sql(
        f"UPDATE identity.{table} SET created_at = created_at - %s::interval, "
        f"expires_at = expires_at - %s::interval WHERE user_id = %s",
        (f"{minutes} minutes", f"{minutes} minutes", str(user_id)),
    )


def _set_status(user_id: uuid.UUID, status: str) -> None:
    _sql("UPDATE identity.user_account SET status = %s WHERE id = %s",
         (status, str(user_id)))


async def _register_pending(client) -> tuple[str, str, uuid.UUID]:
    """A registered, UNVERIFIED account: email, access token, id."""
    email = f"rec_{uuid.uuid4().hex[:12]}@example.com"
    created = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "pending_verification"
    signed_in = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    return email, signed_in.json()["access_token"], uuid.UUID(created.json()["id"])


async def _ask_for_verification(client, token: str) -> None:
    sent = await client.post(
        "/api/v1/auth/verification", headers={"Authorization": f"Bearer {token}"})
    assert sent.status_code == 202, sent.text


async def _ask_for_reset(client, email: str) -> None:
    asked = await client.post("/api/v1/auth/password-reset", json={"email": email})
    assert asked.status_code == 202, asked.text


def _link_token(email: str, kind: TransactionalEmail) -> str:
    messages = captured_emails(to=email, kind=kind)
    assert messages, f"no {kind.value} captured for {email}"
    return token_from_link(messages[-1].message)


async def _reset_token_for(client, email: str) -> str:
    await _ask_for_reset(client, email)
    return _link_token(email, TransactionalEmail.PASSWORD_RESET)


# --------------------------------------------------------------------------- #
# §11 — verification
# --------------------------------------------------------------------------- #

async def test_a_valid_link_verifies_the_account(client):
    email, token, _uid = await _register_pending(client)
    await _ask_for_verification(client, token)

    done = await client.post(
        "/api/v1/auth/verification/confirm",
        json={"token": _link_token(email, TransactionalEmail.EMAIL_VERIFICATION)})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "active"

    # And the application opens.
    me = await client.get("/api/v1/users/me",
                          headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text


async def test_a_verification_link_works_exactly_once(client):
    """§30-A. Replay is the whole reason `used_at` exists."""
    email, token, _uid = await _register_pending(client)
    await _ask_for_verification(client, token)
    raw = _link_token(email, TransactionalEmail.EMAIL_VERIFICATION)

    first = await client.post("/api/v1/auth/verification/confirm", json={"token": raw})
    assert first.status_code == 200, first.text

    replayed = await client.post("/api/v1/auth/verification/confirm", json={"token": raw})
    assert replayed.status_code == 400, replayed.text


async def test_an_expired_verification_link_is_refused(client):
    """§30-B."""
    email, token, uid = await _register_pending(client)
    await _ask_for_verification(client, token)
    raw = _link_token(email, TransactionalEmail.EMAIL_VERIFICATION)

    _age_tokens("email_verification_token", uid, minutes=60 * 25)

    refused = await client.post("/api/v1/auth/verification/confirm", json={"token": raw})
    assert refused.status_code == 400, refused.text


@pytest.mark.parametrize("presented", [
    pytest.param("not-a-real-token-value-at-all", id="invented"),
    pytest.param("", id="empty"),
])
async def test_an_invalid_verification_token_is_refused(client, presented):
    refused = await client.post(
        "/api/v1/auth/verification/confirm", json={"token": presented})
    assert refused.status_code in (400, 422), refused.text


async def test_a_modified_verification_token_is_refused(client):
    """One flipped character. The digest is over the whole value."""
    email, token, _uid = await _register_pending(client)
    await _ask_for_verification(client, token)
    raw = _link_token(email, TransactionalEmail.EMAIL_VERIFICATION)
    tampered = ("a" if raw[0] != "a" else "b") + raw[1:]

    refused = await client.post(
        "/api/v1/auth/verification/confirm", json={"token": tampered})
    assert refused.status_code == 400, refused.text


async def test_resending_supersedes_the_previous_link(client):
    """§26. Three clicks on "resend" must not leave three live credentials in a
    mailbox, each working until it expires on its own."""
    email, token, uid = await _register_pending(client)
    await _ask_for_verification(client, token)
    first = _link_token(email, TransactionalEmail.EMAIL_VERIFICATION)

    # Past the resend floor, so the second request actually mints.
    _age_tokens("email_verification_token", uid, minutes=10)
    await _ask_for_verification(client, token)
    second = _link_token(email, TransactionalEmail.EMAIL_VERIFICATION)
    assert second != first

    stale = await client.post("/api/v1/auth/verification/confirm", json={"token": first})
    assert stale.status_code == 400, "the superseded link still worked"

    fresh = await client.post("/api/v1/auth/verification/confirm", json={"token": second})
    assert fresh.status_code == 200, fresh.text


async def test_an_already_verified_account_is_answered_the_same_and_mints_nothing(client):
    email = f"already_{uuid.uuid4().hex[:10]}@example.com"
    token = await register_verified(client, email, PASSWORD)
    before = len(captured_emails(to=email, kind=TransactionalEmail.EMAIL_VERIFICATION))

    again = await client.post(
        "/api/v1/auth/verification", headers={"Authorization": f"Bearer {token}"})
    assert again.status_code == 202, again.text
    assert len(captured_emails(
        to=email, kind=TransactionalEmail.EMAIL_VERIFICATION)) == before


async def test_a_second_resend_inside_the_floor_is_refused(client):
    """§10. Admission bounds the minute; this bounds the hour."""
    _email, token, _uid = await _register_pending(client)
    await _ask_for_verification(client, token)

    too_soon = await client.post(
        "/api/v1/auth/verification", headers={"Authorization": f"Bearer {token}"})
    assert too_soon.status_code == 429, too_soon.text
    assert too_soon.json()["type"].endswith("recovery-throttled")


# --------------------------------------------------------------------------- #
# §17 — recovery must not reopen a closed account
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status", ["suspended", "closed"])
async def test_a_verification_link_cannot_revive_a_suspended_account(client, status):
    """§30-E. The link is minted while the account is fine and redeemed after it
    is not — which is exactly the sequence an operator's suspension creates."""
    email, token, uid = await _register_pending(client)
    await _ask_for_verification(client, token)
    raw = _link_token(email, TransactionalEmail.EMAIL_VERIFICATION)

    _set_status(uid, status)

    refused = await client.post("/api/v1/auth/verification/confirm", json={"token": raw})
    assert refused.status_code == 400, refused.text

    conn = _owner_cursor()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM identity.user_account WHERE id = %s",
                        (str(uid),))
            assert cur.fetchone()[0] == status, "verification reactivated the account"
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["suspended", "closed"])
async def test_a_reset_link_cannot_revive_a_suspended_account(client, status):
    """§30-E, the other half. A password-reset token must never be a way back in."""
    email = f"revive_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, email, PASSWORD)
    raw = await _reset_token_for(client, email)

    me = await client.post("/api/v1/auth/login",
                           json={"email": email, "password": PASSWORD})
    uid = uuid.UUID((await client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {me.json()['access_token']}"}
    )).json()["id"])
    _set_status(uid, status)

    refused = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw, "new_password": NEW_PASSWORD})
    assert refused.status_code == 400, refused.text

    # The old password still works as a credential, and the account is still
    # refused for what it is — the reset changed nothing at all.
    attempt = await client.post("/api/v1/auth/login",
                                json={"email": email, "password": NEW_PASSWORD})
    assert attempt.status_code in (401, 403), attempt.text


async def test_a_deleting_account_is_refused_a_reset_link(client):
    """The lifecycle half of `_usable_account`, which a direct read would miss:
    the reset session is anonymous, so RLS hides the lifecycle row."""
    email = f"deleting_{uuid.uuid4().hex[:10]}@example.com"
    token = await register_verified(client, email, PASSWORD)
    asked = await client.post("/api/v1/account/deletion",
                              headers={"Authorization": f"Bearer {token}"})
    assert asked.status_code in (200, 202), asked.text

    before = len(captured_emails(to=email, kind=TransactionalEmail.PASSWORD_RESET))
    await _ask_for_reset(client, email)
    assert len(captured_emails(
        to=email, kind=TransactionalEmail.PASSWORD_RESET)) == before


# --------------------------------------------------------------------------- #
# §12, §28 — the reset request must not enumerate
# --------------------------------------------------------------------------- #

async def test_a_reset_request_answers_identically_for_a_stranger(client):
    """§30-D. Status, body, and headers — nothing distinguishes the two."""
    known = f"known_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, known, PASSWORD)
    unknown = f"nobody_{uuid.uuid4().hex[:10]}@example.com"

    real = await client.post("/api/v1/auth/password-reset", json={"email": known})
    fake = await client.post("/api/v1/auth/password-reset", json={"email": unknown})

    assert real.status_code == fake.status_code == 202
    assert real.json() == fake.json()
    assert real.headers.get("content-type") == fake.headers.get("content-type")

    # Non-vacuous: one of them actually produced a message.
    assert captured_emails(to=known, kind=TransactionalEmail.PASSWORD_RESET)
    assert not captured_emails(to=unknown)


@pytest.mark.parametrize("status", ["suspended", "closed"])
async def test_a_suspended_account_gets_a_strangers_answer(client, status):
    """"That account is suspended" answers a question the requester has no
    standing to ask, and the requester is unauthenticated by definition."""
    email = f"susp_{uuid.uuid4().hex[:10]}@example.com"
    token = await register_verified(client, email, PASSWORD)
    uid = uuid.UUID((await client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})).json()["id"])
    _set_status(uid, status)

    unknown = f"nobody_{uuid.uuid4().hex[:10]}@example.com"
    suspended = await client.post("/api/v1/auth/password-reset", json={"email": email})
    stranger = await client.post("/api/v1/auth/password-reset", json={"email": unknown})

    assert suspended.status_code == stranger.status_code == 202
    assert suspended.json() == stranger.json()
    assert not captured_emails(to=email, kind=TransactionalEmail.PASSWORD_RESET)


async def test_a_second_reset_request_inside_the_floor_is_silent_not_throttled(client):
    """A 429 here would say "this address has an account AND somebody just asked
    to reset it" — strictly more than an unknown address discloses."""
    email = f"floor_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, email, PASSWORD)

    await _ask_for_reset(client, email)
    sent_once = len(captured_emails(to=email, kind=TransactionalEmail.PASSWORD_RESET))

    again = await client.post("/api/v1/auth/password-reset", json={"email": email})
    assert again.status_code == 202, "the floor must not be visible in the status"
    assert len(captured_emails(
        to=email, kind=TransactionalEmail.PASSWORD_RESET)) == sent_once


# --------------------------------------------------------------------------- #
# §14, §15 — reset completion and session invalidation
# --------------------------------------------------------------------------- #

async def test_a_reset_sets_the_new_password_and_retires_the_old(client):
    email = f"complete_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, email, PASSWORD)
    raw = await _reset_token_for(client, email)

    done = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw, "new_password": NEW_PASSWORD})
    assert done.status_code == 204, done.text

    assert (await client.post("/api/v1/auth/login",
                              json={"email": email, "password": NEW_PASSWORD})
            ).status_code == 200
    assert (await client.post("/api/v1/auth/login",
                              json={"email": email, "password": PASSWORD})
            ).status_code == 401


async def test_every_refresh_token_dies_with_the_password(client):
    """§15, §30-C. The reason to reset a password is usually that somebody else
    may know the old one; a refresh token issued before the reset would keep
    that access alive for its whole lifetime."""
    email = f"sessions_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, email, PASSWORD)

    # Two separate sessions, because "all families" is the claim.
    first = (await client.post("/api/v1/auth/login",
                               json={"email": email, "password": PASSWORD})).json()
    second = (await client.post("/api/v1/auth/login",
                                json={"email": email, "password": PASSWORD})).json()

    # Non-vacuity: both refresh fine before the reset.
    for session in (first, second):
        probe = await client.post("/api/v1/auth/refresh",
                                  json={"refresh_token": session["refresh_token"]})
        assert probe.status_code == 200, probe.text
        session["refresh_token"] = probe.json()["refresh_token"]

    raw = await _reset_token_for(client, email)
    assert (await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw, "new_password": NEW_PASSWORD})).status_code == 204

    for session in (first, second):
        dead = await client.post("/api/v1/auth/refresh",
                                 json={"refresh_token": session["refresh_token"]})
        assert dead.status_code == 401, dead.text

    # And a fresh sign-in works, so the account is usable rather than bricked.
    assert (await client.post("/api/v1/auth/login",
                              json={"email": email, "password": NEW_PASSWORD})
            ).status_code == 200


async def test_a_reset_link_works_exactly_once(client):
    """§30-A for the reset half. A used link must not set a second password."""
    email = f"once_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, email, PASSWORD)
    raw = await _reset_token_for(client, email)

    assert (await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw, "new_password": NEW_PASSWORD})).status_code == 204

    replayed = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw, "new_password": "yet-another-password-11"})
    assert replayed.status_code == 400, replayed.text

    # The replay changed nothing.
    assert (await client.post("/api/v1/auth/login",
                              json={"email": email, "password": NEW_PASSWORD})
            ).status_code == 200


async def test_an_expired_reset_link_is_refused(client):
    """§30-B for the reset half."""
    email = f"expired_{uuid.uuid4().hex[:10]}@example.com"
    token = await register_verified(client, email, PASSWORD)
    uid = uuid.UUID((await client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})).json()["id"])
    raw = await _reset_token_for(client, email)

    _age_tokens("password_reset_token", uid, minutes=61)

    refused = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw, "new_password": NEW_PASSWORD})
    assert refused.status_code == 400, refused.text
    assert (await client.post("/api/v1/auth/login",
                              json={"email": email, "password": PASSWORD})
            ).status_code == 200


async def test_a_verification_token_is_not_a_reset_token(client):
    """Single purpose. The two tables are structurally identical, so nothing but
    the lookup's table keeps a verification link from setting a password."""
    email, token, _uid = await _register_pending(client)
    await _ask_for_verification(client, token)
    raw = _link_token(email, TransactionalEmail.EMAIL_VERIFICATION)

    refused = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw, "new_password": NEW_PASSWORD})
    assert refused.status_code == 400, refused.text


async def test_a_reset_token_is_not_a_verification_token(client):
    email = f"crossuse_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, email, PASSWORD)
    raw = await _reset_token_for(client, email)

    refused = await client.post("/api/v1/auth/verification/confirm", json={"token": raw})
    assert refused.status_code == 400, refused.text


async def test_one_accounts_link_cannot_touch_another(client):
    """Account binding. The token names its own row; nothing in the request
    says which account, so there is nothing to substitute."""
    victim = f"victim_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, victim, PASSWORD)
    attacker = f"attacker_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, attacker, PASSWORD)

    stolen = await _reset_token_for(client, attacker)
    assert (await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": stolen, "new_password": NEW_PASSWORD})).status_code == 204

    # The victim is untouched: their password still works and the attacker's
    # new one does not open their account.
    assert (await client.post("/api/v1/auth/login",
                              json={"email": victim, "password": PASSWORD})
            ).status_code == 200
    assert (await client.post("/api/v1/auth/login",
                              json={"email": victim, "password": NEW_PASSWORD})
            ).status_code == 401


async def test_the_new_password_must_meet_the_registration_policy(client):
    """§14. A reset that accepted a weaker password than registration would be
    a way to install one."""
    email = f"policy_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, email, PASSWORD)
    raw = await _reset_token_for(client, email)

    refused = await client.post(
        "/api/v1/auth/password-reset/confirm", json={"token": raw, "new_password": "short"})
    assert refused.status_code == 422, refused.text

    # And the token survived a rejected attempt, so the customer can try again.
    assert (await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw, "new_password": NEW_PASSWORD})).status_code == 204


# --------------------------------------------------------------------------- #
# §16 — the password-changed notice
# --------------------------------------------------------------------------- #

async def test_a_completed_reset_notifies_the_account(client):
    email = f"notice_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, email, PASSWORD)
    raw = await _reset_token_for(client, email)
    assert (await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw, "new_password": NEW_PASSWORD})).status_code == 204

    notices = captured_emails(to=email, kind=TransactionalEmail.PASSWORD_CHANGED)
    assert len(notices) == 1
    body = notices[0].message
    assert "?token=" not in body.text and "?token=" not in body.html
    assert NEW_PASSWORD not in body.text and NEW_PASSWORD not in body.html


# --------------------------------------------------------------------------- #
# §19 — nothing secret is recorded
# --------------------------------------------------------------------------- #

async def test_the_audit_trail_records_the_event_and_not_the_secret(client):
    """The event name and the account. Not the token, its digest, the link, the
    body, or the address."""
    email = f"audit_{uuid.uuid4().hex[:10]}@example.com"
    token = await register_verified(client, email, PASSWORD)
    uid = uuid.UUID((await client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})).json()["id"])
    raw = await _reset_token_for(client, email)
    assert (await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw, "new_password": NEW_PASSWORD})).status_code == 204

    conn = _owner_cursor()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT event_type, detail::text FROM audit.security_event "
                "WHERE user_id = %s ORDER BY created_at", (str(uid),))
            rows = cur.fetchall()
    finally:
        conn.close()

    kinds = [r[0] for r in rows]
    assert "email_verification_requested" in kinds
    assert "email_verified" in kinds
    assert "password_reset_requested" in kinds
    assert "password_reset_completed" in kinds
    assert "session_families_revoked" in kinds

    import hashlib
    digest = hashlib.sha256(raw.encode()).hexdigest()
    rendered = " ".join(f"{r[0]} {r[1]}" for r in rows)
    assert raw not in rendered
    assert digest not in rendered
    assert email not in rendered
    assert NEW_PASSWORD not in rendered
    assert "http" not in rendered


async def test_no_recovery_token_is_stored_in_a_readable_form(client):
    """§8, §13. What the database holds is a digest; the raw value exists only
    in the link."""
    email = f"digest_{uuid.uuid4().hex[:10]}@example.com"
    token = await register_verified(client, email, PASSWORD)
    uid = uuid.UUID((await client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})).json()["id"])
    raw = await _reset_token_for(client, email)

    import hashlib
    conn = _owner_cursor()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT token_hash FROM identity.password_reset_token "
                        "WHERE user_id = %s", (str(uid),))
            stored = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    assert raw not in stored
    assert hashlib.sha256(raw.encode()).hexdigest() in stored


async def test_tokens_do_not_repeat(client):
    """§8. Cryptographic randomness, asserted at the only level a test can see:
    distinct high-entropy values, and enough of them to be a real token."""
    seen = set()
    for _ in range(5):
        email = f"entropy_{uuid.uuid4().hex[:12]}@example.com"
        await register_verified(client, email, PASSWORD)
        raw = await _reset_token_for(client, email)
        assert len(raw) >= 43, f"token is only {len(raw)} characters"
        seen.add(raw)
    assert len(seen) == 5


# --------------------------------------------------------------------------- #
# §21 — the link's host comes from configuration
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("header", ["Host", "X-Forwarded-Host"])
async def test_the_reset_link_ignores_a_caller_supplied_host(client, header):
    """Host-header reset poisoning: if the caller chooses the origin, the link
    is a single-use credential addressed to the attacker."""
    email = f"host_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, email, PASSWORD)

    asked = await client.post(
        "/api/v1/auth/password-reset",
        json={"email": email},
        headers={header: "attacker.example.net"})
    assert asked.status_code == 202, asked.text

    message = captured_emails(to=email, kind=TransactionalEmail.PASSWORD_RESET)[-1]
    assert "attacker.example.net" not in message.message.text
    assert "attacker.example.net" not in message.message.html


# --------------------------------------------------------------------------- #
# §29 — mass assignment
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("field,value", [
    ("user_id", "00000000-0000-0000-0000-000000000001"),
    ("status", "active"),
    ("email_verified_at", "2020-01-01T00:00:00Z"),
    ("used_at", None),
    ("expires_at", "2099-01-01T00:00:00Z"),
    ("token_hash", "deadbeef"),
    ("password_hash", "$argon2id$v=19$m=65536,t=3,p=4$x$y"),
    ("refresh_family_id", "00000000-0000-0000-0000-000000000002"),
    ("is_admin", True),
])
async def test_a_recovery_request_refuses_server_owned_fields(client, field, value):
    """Rejected, not silently ignored. Ignoring is indistinguishable from
    accepting until a field is added with a matching name."""
    email = f"mass_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, email, PASSWORD)

    refused = await client.post(
        "/api/v1/auth/password-reset", json={"email": email, field: value})
    assert refused.status_code == 422, f"{field} was accepted: {refused.text}"


@pytest.mark.parametrize("field,value", [
    ("status", "active"),
    ("email_verified_at", "2020-01-01T00:00:00Z"),
    ("is_admin", True),
])
async def test_registration_refuses_server_owned_fields(client, field, value):
    """The one that matters most now that registration decides a status."""
    refused = await client.post(
        "/api/v1/auth/register",
        json={"email": f"reg_{uuid.uuid4().hex[:10]}@example.com",
              "password": PASSWORD, field: value})
    assert refused.status_code == 422, f"{field} was accepted: {refused.text}"


# --------------------------------------------------------------------------- #
# §18 — rate limiting
# --------------------------------------------------------------------------- #

async def test_reset_requests_are_bounded_per_mailbox(client):
    """The identity scope of ACCOUNT_RECOVERY, which is what stops one victim's
    inbox being used as a dead drop."""
    from app.services.admission.policy import POLICIES, OperationClass

    allowance = POLICIES[OperationClass.ACCOUNT_RECOVERY].rate_allowance
    assert allowance is not None

    email = f"flood_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, email, PASSWORD)

    statuses = []
    for _ in range(allowance + 2):
        response = await client.post(
            "/api/v1/auth/password-reset", json={"email": email})
        statuses.append(response.status_code)

    assert 429 in statuses, f"no request was ever refused: {statuses}"


async def test_the_recovery_limit_is_tighter_than_the_login_limit(client):
    """The reason ACCOUNT_RECOVERY exists at all. A login flood is a denial of
    service against Onyx; a reset flood is one against a customer, delivered by
    Onyx — so it cannot share AUTH_ATTEMPT's much looser allowance."""
    from app.services.admission.policy import POLICIES, OperationClass

    recovery = POLICIES[OperationClass.ACCOUNT_RECOVERY]
    auth = POLICIES[OperationClass.AUTH_ATTEMPT]
    assert recovery.rate_allowance < auth.rate_allowance
    assert recovery.source_rate_allowance < auth.source_rate_allowance
