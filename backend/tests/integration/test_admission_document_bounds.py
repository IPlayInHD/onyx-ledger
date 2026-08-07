"""Document size bounds — the enforced kind, not the declared kind (§14–§15).

Phase 1 refused an oversized `byte_size` in the request body and called that a
document bound. It is not one. The bytes never pass through the API, so the
number in the body is a CLAIM by the client, and a client that wants to store
more than the limit simply claims less. These tests are the difference between
a limit and a request to please stay under one.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.integrations.storage import ObjectTooLarge, get_object_storage
from app.services.admission.limits import MAX_DOCUMENT_BYTES, MAX_EXTRACTION_TEXT_BYTES

PASSWORD = "supersecret1"


async def _authed(client) -> dict[str, str]:
    email = f"docbound_{uuid.uuid4().hex[:10]}@test.ca"
    assert (await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )).status_code == 201
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _key_from(upload_url: str) -> tuple[str, str]:
    """Split `local://bucket/key?...` back into bucket and key."""
    without_scheme = upload_url.removeprefix("local://").split("?", 1)[0]
    bucket, _, key = without_scheme.partition("/")
    return bucket, key


@pytest.mark.asyncio
async def test_a_lying_declaration_is_still_refused_at_the_storage_boundary(client):
    """THE test this section exists for.

    Declared: 1 MB. Actually sent: 30 MB. Platform limit: 25 MB.

    The declaration passes every check the API can make — it is well under the
    limit — so a system whose only bound is the declared size accepts all 30 MB.
    Here the presigned authorization carries a ceiling, and the store refuses
    the object that actually arrives.
    """
    headers = await _authed(client)
    declared = 1024 * 1024
    assert declared < MAX_DOCUMENT_BYTES < 30 * 1024 * 1024

    created = await client.post(
        "/api/v1/documents",
        json={"document_type_code": "T4", "filename": "small.pdf",
              "mime_type": "application/pdf", "byte_size": declared},
        headers=headers,
    )
    assert created.status_code == 201, created.text

    bucket, key = _key_from(created.json()["upload_url"])
    storage = get_object_storage()

    with pytest.raises(ObjectTooLarge):
        storage.put(bucket, key, b"x" * (30 * 1024 * 1024))

    assert storage.get(bucket, key) == b"", (
        "the refused object was stored anyway"
    )


@pytest.mark.asyncio
async def test_the_ceiling_is_the_tighter_of_the_declaration_and_the_limit(client):
    """A client that declares 1 MB is held to 1 MB, not to the platform max.

    Honouring the declaration costs nothing and is strictly better: the client
    told us what it was about to send, and there is no reason to authorize more
    than that. Under-declaring therefore buys the caller nothing.
    """
    headers = await _authed(client)
    declared = 1024 * 1024

    created = await client.post(
        "/api/v1/documents",
        json={"document_type_code": "T4", "filename": "small.pdf",
              "mime_type": "application/pdf", "byte_size": declared},
        headers=headers,
    )
    bucket, key = _key_from(created.json()["upload_url"])
    storage = get_object_storage()

    # Over the declaration, well under the platform maximum.
    with pytest.raises(ObjectTooLarge):
        storage.put(bucket, key, b"x" * (declared + 1))

    # At the declaration: accepted.
    storage.put(bucket, key, b"x" * declared)
    assert len(storage.get(bucket, key)) == declared


@pytest.mark.asyncio
async def test_an_undeclared_upload_is_bounded_by_the_platform_maximum(client):
    """`byte_size` is optional, and omitting it must not mean 'unbounded'."""
    headers = await _authed(client)
    created = await client.post(
        "/api/v1/documents",
        json={"document_type_code": "T4", "filename": "unknown.pdf",
              "mime_type": "application/pdf"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    bucket, key = _key_from(created.json()["upload_url"])

    with pytest.raises(ObjectTooLarge):
        get_object_storage().put(bucket, key, b"x" * (MAX_DOCUMENT_BYTES + 1))


@pytest.mark.asyncio
async def test_extraction_text_is_bounded_in_bytes_not_characters(client):
    """A character-count limit on a byte-named bound admits four times too much.

    The string below is under `MAX_EXTRACTION_TEXT_BYTES` characters and over it
    in UTF-8, which is exactly the shape `max_length` alone would have let
    through — and the regex extraction that runs over it scales with the
    encoded size, not the character count.
    """
    headers = await _authed(client)
    created = await client.post(
        "/api/v1/documents",
        json={"document_type_code": "T4", "filename": "t4.pdf",
              "mime_type": "application/pdf"},
        headers=headers,
    )
    document_id = created.json()["document_id"]

    # Three bytes per character, so 40% of the limit in characters is 120% of it
    # in bytes.
    oversized = "€" * int(MAX_EXTRACTION_TEXT_BYTES * 0.4)
    assert len(oversized) < MAX_EXTRACTION_TEXT_BYTES
    assert len(oversized.encode("utf-8")) > MAX_EXTRACTION_TEXT_BYTES

    response = await client.post(
        f"/api/v1/documents/{document_id}/process",
        json={"text": oversized}, headers=headers,
    )
    assert response.status_code == 422, response.status_code


@pytest.mark.asyncio
async def test_twenty_retries_against_one_in_flight_extraction_all_refuse(client):
    """§15 — the retry-storm shape against one document, asserted deterministically.

    Deliberately NOT "fire 20 requests with gather and expect some refusals".
    Nothing forces those requests to overlap: each one completes and releases
    its lease before the next begins, twenty extractions run SEQUENTIALLY, and
    the assertion fails on a system that behaved perfectly. That version was
    written first and it failed for exactly that reason.

    Instead the state a still-running extraction leaves — a live lease on the
    document's dedupe key — is created directly, and then twenty concurrent
    requests are fired at it. Every one must be refused, and the extraction
    count must not move.

    The key is the DOCUMENT, which is the immutable identity of the stored
    object: bucket and key are assigned once at upload and never change, so
    "the same document" is not a moving target the way a mutable version field
    would be.
    """
    from app.database.session import unit_of_work
    from app.services.admission.guard import owned_dedupe_key
    from app.services.admission.policy import OperationClass
    from app.services.admission.service import AdmissionService

    headers = await _authed(client)
    me = await client.get("/api/v1/users/me", headers=headers)
    user_id = uuid.UUID(me.json()["id"])

    created = await client.post(
        "/api/v1/documents",
        json={"document_type_code": "T4", "filename": "t4.pdf",
              "mime_type": "application/pdf"},
        headers=headers,
    )
    document_id = created.json()["document_id"]

    async def extraction_count() -> int:
        async with unit_of_work(actor_type="admin") as session:
            value = await session.scalar(
                text("SELECT count(*) FROM docs.document_extraction "
                     "WHERE document_id = :d"),
                {"d": uuid.UUID(document_id)},
            )
            return int(value or 0)

    # Exactly the state a running extraction leaves behind.
    async with unit_of_work(actor_type="admin") as session:
        held = await AdmissionService(session).admit(
            OperationClass.DOCUMENT_PROCESS,
            scope_id=str(user_id),
            dedupe_key=owned_dedupe_key(user_id, "process", document_id),
        )
        assert held.holds_lease

    before = await extraction_count()
    responses = await asyncio.gather(*(
        client.post(f"/api/v1/documents/{document_id}/process",
                    json={"fields": {"box_14": "50000.00"}}, headers=headers)
        for _ in range(20)
    ))
    codes = sorted(r.status_code for r in responses)

    assert set(codes) == {409}, (
        f"a retry against an in-flight extraction was admitted: {codes}"
    )
    assert await extraction_count() == before, (
        "twenty refused retries still produced extraction rows"
    )

    # Once the in-flight extraction finishes, the document is processable again.
    async with unit_of_work(actor_type="admin") as session:
        await AdmissionService(session).release_ticket(held)

    after = await client.post(
        f"/api/v1/documents/{document_id}/process",
        json={"fields": {"box_14": "50000.00"}}, headers=headers,
    )
    assert after.status_code == 200, after.text
