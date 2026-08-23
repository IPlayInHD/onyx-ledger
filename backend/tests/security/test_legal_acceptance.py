"""Durable legal acceptance: what it records, and what it refuses to record.

THE ONE CLAIM THIS SUBSYSTEM MUST NEVER MAKE is that somebody agreed to
something. Almost everything below is a way of trying to make it make that
claim falsely — on another person's behalf, at a version they were never
shown, at a time they did not choose, or twice for one act — and requiring it
to refuse.

The happy path is three tests. The rest is the perimeter.
"""
from __future__ import annotations

import asyncio
import uuid

import psycopg2
import pytest

from app.domain.legal import (
    CURRENT_DOCUMENTS,
    LegalDocumentType,
    documents_requiring_acceptance,
    production_blockers,
)
from tests.conftest import owner_dsn, register_verified, verify_account

PASSWORD = "supersecret1"
CURRENT = CURRENT_DOCUMENTS[LegalDocumentType.TERMS].version


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _rows(user_id: uuid.UUID) -> list[tuple]:
    conn = _owner()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT document_type, document_version FROM identity.legal_acceptance "
                "WHERE user_id = %s ORDER BY accepted_at",
                (str(user_id),),
            )
            return cur.fetchall()
    finally:
        conn.close()


async def _pending_account(client) -> tuple[str, str]:
    """A verified account that has accepted NOTHING. email, access token.

    Verified through `verify_account`, which opens the link registration
    already sent — a first version of this asked for another one and was
    correctly refused by B3's resend floor, which is the product working.

    Deliberately NOT `register_verified`: that one goes on to clear the legal
    gate, and an account with nothing outstanding is exactly the state these
    tests need to be missing.
    """
    email = f"legal_{uuid.uuid4().hex[:12]}@example.com"
    created = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert created.status_code == 201, created.text
    signed_in = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    token = signed_in.json()["access_token"]

    await verify_account(client, email, token)
    return email, token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _uid(client, token: str) -> uuid.UUID:
    """The account the token names. Read from the token, not the database: the
    account id is the one thing the caller's own credential already states."""
    from app.core.security.jwt import decode_access_token

    return uuid.UUID(decode_access_token(token)["sub"])


def _set_status(user_id: uuid.UUID, status: str) -> None:
    """Put the account into an operator-set state, out of band."""
    conn = _owner()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE identity.user_account SET status = %s WHERE id = %s",
                (status, str(user_id)),
            )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# §11, §15 — what the legal exemption relaxes, and what it does NOT
# --------------------------------------------------------------------------- #
#
# `db_authed_legal_exempt` is named in the deletion-cutoff allow-list that
# `tests/unit/test_lifecycle_boundaries.py` enforces over every authenticated
# route. That entry is a CLAIM — that the dependency relaxes the legal check
# and nothing else — and a docstring is not evidence for it. These four tests
# are the evidence: each removes a different reason to be refused and requires
# the two legal endpoints to keep refusing.

LEGAL_ROUTES = [
    ("GET", "/api/v1/legal/state"),
    ("POST", "/api/v1/legal/acceptances"),
]


async def _call(client, method: str, path: str, token: str):
    if method == "GET":
        return await client.get(path, headers=_auth(token))
    return await client.post(
        path,
        headers=_auth(token),
        json={
            "document_type": LegalDocumentType.TERMS.value,
            "document_version": CURRENT,
        },
    )


@pytest.mark.parametrize(("method", "path"), LEGAL_ROUTES)
async def test_a_deleting_account_is_refused_the_legal_endpoints(client, method, path):
    """THE CUTOFF STILL APPLIES. An account that asked to be deleted must not
    write a new row anywhere, and an acceptance is a row — one dated after the
    cutoff, which a purge bounded on the request time would leave behind."""
    email = f"legal_del_{uuid.uuid4().hex[:10]}@example.com"
    token = await register_verified(client, email, PASSWORD)

    asked = await client.post("/api/v1/account/deletion", headers=_auth(token))
    assert asked.status_code in (200, 202), asked.text

    refused = await _call(client, method, path, token)
    assert refused.status_code == 403, refused.text
    assert refused.json()["type"].endswith("deletion-in-progress"), refused.text


@pytest.mark.parametrize("status", ["suspended", "closed"])
async def test_a_suspended_account_is_refused_the_legal_endpoints(client, status):
    """Asking a suspended customer to accept new Terms would be a false
    promise: accepting them would change nothing about why they are refused."""
    _, token = await _pending_account(client)
    _set_status(await _uid(client, token), status)

    for method, path in LEGAL_ROUTES:
        refused = await _call(client, method, path, token)
        assert refused.status_code == 403, refused.text
        assert refused.json()["type"].endswith("account-not-active"), refused.text


async def test_an_unverified_account_is_refused_the_legal_endpoints(client):
    """Confirming an address comes FIRST. Two blockers on one customer at once
    is how they end up unable to tell which screen they are looking at."""
    email = f"legal_unv_{uuid.uuid4().hex[:10]}@example.com"
    created = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert created.status_code == 201, created.text
    signed_in = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    token = signed_in.json()["access_token"]

    for method, path in LEGAL_ROUTES:
        refused = await _call(client, method, path, token)
        assert refused.status_code == 403, refused.text
        assert refused.json()["type"].endswith("email-verification-required"), refused.text


async def test_the_exemption_relaxes_the_legal_check_and_nothing_else(client):
    """The other half of the claim: with every OTHER reason absent, an account
    whose only problem is outstanding acceptance reaches both endpoints. A
    dependency that refused here would make the state permanent."""
    _, token = await _pending_account(client)

    state = await client.get("/api/v1/legal/state", headers=_auth(token))
    assert state.status_code == 200, state.text
    assert state.json()["application_access_blocked"] is True

    accepted = await client.post(
        "/api/v1/legal/acceptances",
        headers=_auth(token),
        json={
            "document_type": LegalDocumentType.TERMS.value,
            "document_version": CURRENT,
        },
    )
    assert accepted.status_code == 200, accepted.text


# --------------------------------------------------------------------------- #
# §8, §9 — the state contract and the happy path
# --------------------------------------------------------------------------- #

async def test_a_new_account_is_told_exactly_what_is_outstanding(client):
    _email, token = await _pending_account(client)

    state = await client.get("/api/v1/legal/state", headers=_auth(token))
    assert state.status_code == 200, state.text
    body = state.json()

    assert body["application_access_blocked"] is True
    by_type = {d["document_type"]: d for d in body["documents"]}

    # EVERY document, not just the outstanding ones: a settings screen needs
    # the accepted ones too, and a client that had to ask twice would render
    # half a page while it waited.
    assert set(by_type) == {t.value for t in LegalDocumentType}

    required = {d.type.value for d in documents_requiring_acceptance()}
    for document_type, document in by_type.items():
        assert document["requires_acceptance"] is (document_type in required)
        assert document["acceptance_outstanding"] is (document_type in required)
        assert document["accepted_version"] is None
        assert document["accepted_at"] is None


async def test_accepting_the_required_documents_opens_the_application(client):
    _email, token = await _pending_account(client)
    assert (await client.get("/api/v1/users/me", headers=_auth(token))).status_code == 403

    for document in documents_requiring_acceptance():
        accepted = await client.post(
            "/api/v1/legal/acceptances",
            headers=_auth(token),
            json={
                "document_type": document.type.value,
                "document_version": document.version,
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["accepted_version"] == document.version
        assert accepted.json()["acceptance_outstanding"] is False

    assert (await client.get("/api/v1/users/me", headers=_auth(token))).status_code == 200
    after = await client.get("/api/v1/legal/state", headers=_auth(token))
    assert after.json()["application_access_blocked"] is False


async def test_a_notice_only_document_may_be_accepted_and_changes_no_gate(client):
    """Thoroughness is not an error.

    Acknowledging the AI notice costs one true row. What it must NOT do is
    become a gate — what is required comes from the registry, never from what
    happens to be in the table.
    """
    _email, token = await _pending_account(client)
    accepted = await client.post(
        "/api/v1/legal/acceptances",
        headers=_auth(token),
        json={"document_type": "ai-transparency", "document_version": CURRENT})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["acceptance_outstanding"] is False

    # Still blocked: the notice was never what was required.
    assert (await client.get("/api/v1/users/me", headers=_auth(token))).status_code == 403


# --------------------------------------------------------------------------- #
# §6, §27 — idempotency and concurrency
# --------------------------------------------------------------------------- #

async def test_accepting_the_same_version_twice_writes_one_row(client):
    _email, token = await _pending_account(client)
    uid = await _uid(client, token)
    body = {"document_type": "terms", "document_version": CURRENT}

    first = await client.post("/api/v1/legal/acceptances", headers=_auth(token), json=body)
    second = await client.post("/api/v1/legal/acceptances", headers=_auth(token), json=body)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json(), "a repeat must be indistinguishable"
    assert [r for r in _rows(uid) if r[0] == "terms"] == [("terms", CURRENT)]


async def test_concurrent_acceptance_of_the_same_document_is_one_row(client):
    """§27. Two tabs, a double-click and a network retry are the same event.

    The unique key turns every request after the first into a no-op inside
    PostgreSQL rather than a race in Python, so no caller sees an integrity
    error and exactly one row exists.
    """
    _email, token = await _pending_account(client)
    uid = await _uid(client, token)
    body = {"document_type": "terms", "document_version": CURRENT}

    results = await asyncio.gather(*[
        client.post("/api/v1/legal/acceptances", headers=_auth(token), json=body)
        for _ in range(6)
    ], return_exceptions=True)

    for result in results:
        assert not isinstance(result, BaseException), result
        assert result.status_code == 200, result.text

    assert [r for r in _rows(uid) if r[0] == "terms"] == [("terms", CURRENT)]


# --------------------------------------------------------------------------- #
# §7, §29, §30 — what the client may not decide
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("field,value", [
    ("user_id", "00000000-0000-0000-0000-000000000001"),
    ("accepted_at", "2020-01-01T00:00:00Z"),
    ("created_at", "2020-01-01T00:00:00Z"),
    ("requires_acceptance", False),
    ("review_status", "COUNSEL_APPROVED"),
    ("id", "00000000-0000-0000-0000-000000000002"),
])
async def test_the_client_cannot_author_the_record(client, field, value):
    """Rejected, not ignored. Every one of these decides whose agreement it is,
    when it happened, or whether it was needed — and all three are the
    server's. Silent ignoring is indistinguishable from acceptance until a
    field is added with a matching name."""
    _email, token = await _pending_account(client)
    refused = await client.post(
        "/api/v1/legal/acceptances",
        headers=_auth(token),
        json={"document_type": "terms", "document_version": CURRENT, field: value})
    assert refused.status_code == 422, f"{field} was accepted: {refused.text}"


async def test_the_recorded_time_is_the_servers(client):
    """There is no field to spoof, so this asserts the positive: the stored
    time is now, not anything a client could have suggested."""
    from datetime import UTC, datetime

    _email, token = await _pending_account(client)
    before = datetime.now(tz=UTC)
    accepted = await client.post(
        "/api/v1/legal/acceptances", headers=_auth(token),
        json={"document_type": "terms", "document_version": CURRENT})
    after = datetime.now(tz=UTC)

    recorded = datetime.fromisoformat(accepted.json()["accepted_at"])
    assert before <= recorded <= after


async def test_an_unknown_document_type_is_refused(client):
    _email, token = await _pending_account(client)
    refused = await client.post(
        "/api/v1/legal/acceptances", headers=_auth(token),
        json={"document_type": "definitely-not-a-document", "document_version": CURRENT})
    assert refused.status_code == 404, refused.text


@pytest.mark.parametrize("version", ["0.0.1", "9.9.9", "0.1.0", ""])
async def test_a_version_the_registry_does_not_publish_is_refused(client, version):
    """§29's stale case and §9's future case, which are the same refusal.

    A stale tab submits an old version; a client guessing forward submits a new
    one. Neither is what the registry says is in force, and recording either
    would be a durable record of agreement to text nobody was shown.
    """
    _email, token = await _pending_account(client)
    uid = await _uid(client, token)
    refused = await client.post(
        "/api/v1/legal/acceptances", headers=_auth(token),
        json={"document_type": "terms", "document_version": version})
    assert refused.status_code in (409, 422), refused.text
    if refused.status_code == 409:
        assert refused.json()["type"].endswith("legal-version-stale")
    assert _rows(uid) == [], "a refused acceptance still wrote a row"


# --------------------------------------------------------------------------- #
# §22, §30 — tenancy
# --------------------------------------------------------------------------- #

async def test_one_account_cannot_accept_for_another(client):
    """Not refused — UNREPRESENTABLE. The account is the bearer token's and
    appears in no field, so there is no request that means "accept for them"."""
    victim_email = f"victim_{uuid.uuid4().hex[:10]}@example.com"
    victim_token = await register_verified(client, victim_email, PASSWORD)
    victim = await _uid(client, victim_token)

    _attacker_email, attacker_token = await _pending_account(client)
    attacker = await _uid(client, attacker_token)

    await client.post(
        "/api/v1/legal/acceptances", headers=_auth(attacker_token),
        json={"document_type": "terms", "document_version": CURRENT})

    # The attacker's own row exists; the victim's history is untouched by it.
    assert ("terms", CURRENT) in _rows(attacker)
    victim_terms = [r for r in _rows(victim) if r[0] == "terms"]
    assert victim_terms == [("terms", CURRENT)], "the victim's own acceptance changed"


async def test_the_state_endpoint_shows_only_the_callers_own_history(client):
    other_email = f"other_{uuid.uuid4().hex[:10]}@example.com"
    await register_verified(client, other_email, PASSWORD)

    _email, token = await _pending_account(client)
    state = await client.get("/api/v1/legal/state", headers=_auth(token))

    # This account has accepted nothing, and the other account's acceptances
    # are invisible rather than merely unlabelled.
    assert all(d["accepted_version"] is None for d in state.json()["documents"])


async def test_rls_refuses_a_row_written_for_another_account(client):
    """The WITH CHECK half of the policy, exercised directly.

    A USING-only policy would let a caller insert a row for somebody else that
    it could then never see — writing consent in another person's name,
    invisibly. This proves the write side is closed too.
    """
    from sqlalchemy import text

    from app.database.session import unit_of_work

    _email, token = await _pending_account(client)
    mine = await _uid(client, token)
    someone_else = uuid.uuid4()

    with pytest.raises(Exception) as caught:
        async with unit_of_work(user_id=mine, actor_type="user") as session:
            await session.execute(
                text(
                    "INSERT INTO identity.legal_acceptance "
                    "(user_id, document_type, document_version) "
                    "VALUES (:u, 'terms', :v)"
                ),
                {"u": str(someone_else), "v": CURRENT},
            )
    assert "row-level security" in str(caught.value).lower()


# --------------------------------------------------------------------------- #
# §5, §23 — append-only
# --------------------------------------------------------------------------- #

async def test_the_runtime_role_holds_no_update_or_delete(client):
    """§23. An append-only table whose writer can UPDATE is append-only by
    convention. The grant is the first of three layers."""
    conn = _owner()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT grantee, privilege_type FROM information_schema.table_privileges "
                "WHERE table_schema='identity' AND table_name='legal_acceptance'")
            grants = {(g, p) for g, p in cur.fetchall()}
    finally:
        conn.close()

    app_rw = {p for g, p in grants if g == "onyx_app_rw"}
    assert app_rw == {"SELECT", "INSERT"}, f"onyx_app_rw holds {sorted(app_rw)}"

    # §24: the reporting role gets legal history when somebody names the
    # report, not by inheriting a wildcard.
    assert not {p for g, p in grants if g == "onyx_app_ro"}


async def test_an_acceptance_cannot_be_rewritten_into_another_version(client):
    """§5, stated as the invariant it protects. Even as the table owner."""
    _email, token = await _pending_account(client)
    uid = await _uid(client, token)
    await client.post(
        "/api/v1/legal/acceptances", headers=_auth(token),
        json={"document_type": "terms", "document_version": CURRENT})

    conn = _owner()
    try:
        with conn.cursor() as cur, pytest.raises(psycopg2.Error) as caught:
            cur.execute(
                "UPDATE identity.legal_acceptance SET document_version = '99.0' "
                "WHERE user_id = %s", (str(uid),))
        assert "append-only" in str(caught.value)
    finally:
        conn.close()

    assert _rows(uid) == [("terms", CURRENT)]


async def test_history_cannot_be_deleted_while_the_account_exists(client):
    _email, token = await _pending_account(client)
    uid = await _uid(client, token)
    await client.post(
        "/api/v1/legal/acceptances", headers=_auth(token),
        json={"document_type": "terms", "document_version": CURRENT})

    conn = _owner()
    try:
        with conn.cursor() as cur, pytest.raises(psycopg2.Error) as caught:
            cur.execute("DELETE FROM identity.legal_acceptance WHERE user_id = %s",
                        (str(uid),))
        assert "append-only" in str(caught.value)
    finally:
        conn.close()

    assert _rows(uid) == [("terms", CURRENT)]


async def test_the_account_cascade_is_still_permitted(client):
    """THE ONE DELETION THAT MUST WORK, and the reason the delete guard is
    row-level rather than statement-level.

    A first attempt blocked this outright and broke account deletion — a
    privacy right the product has already shipped — so this asserts the
    exception explicitly rather than trusting the trigger's shape.
    """
    _email, token = await _pending_account(client)
    uid = await _uid(client, token)
    await client.post(
        "/api/v1/legal/acceptances", headers=_auth(token),
        json={"document_type": "terms", "document_version": CURRENT})
    assert _rows(uid) == [("terms", CURRENT)]

    conn = _owner()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM identity.user_account WHERE id = %s", (str(uid),))
    finally:
        conn.close()

    assert _rows(uid) == []


async def test_the_surviving_evidence_is_the_de_identifiable_audit_row(client):
    """§21, and the reason the acceptance table may cascade away.

    Every acceptance also writes audit.consent_log, which the deletion pipeline
    severs rather than destroys. That row is the proof an agreement was given;
    the acceptance table is live state answering the gate.
    """
    _email, token = await _pending_account(client)
    uid = await _uid(client, token)
    await client.post(
        "/api/v1/legal/acceptances", headers=_auth(token),
        json={"document_type": "terms", "document_version": CURRENT})

    conn = _owner()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT consent_type, granted, version, ip_address "
                "FROM audit.consent_log WHERE user_id = %s", (str(uid),))
            rows = cur.fetchall()
    finally:
        conn.close()

    assert ("terms", True, CURRENT, None) in rows, rows
    # §4: no network address, here or in the acceptance table.
    assert all(r[3] is None for r in rows)


# --------------------------------------------------------------------------- #
# §17 — draft status
# --------------------------------------------------------------------------- #

def test_production_refuses_to_require_an_unreviewed_draft():
    """A draft nobody must accept is fine in production — it is published
    information, clearly marked. A draft production REQUIRES people to sign is
    not, and this is what says so.

    Non-vacuous in both directions: the blockers name exactly today's two
    required documents, so the rule neither passes trivially nor fires on the
    four notice-only ones.
    """
    blockers = production_blockers()
    required = {d.type.value for d in documents_requiring_acceptance()}
    assert required, "no document requires acceptance; this test proves nothing"
    for document_type in required:
        assert any(document_type in b for b in blockers), (
            f"{document_type} is required and is a draft, but production does "
            "not refuse it"
        )
    notice_only = {t.value for t in LegalDocumentType} - required
    for document_type in notice_only:
        assert not any(b.startswith(document_type) for b in blockers), (
            f"{document_type} requires no acceptance; production must not "
            "refuse it for being a draft"
        )
