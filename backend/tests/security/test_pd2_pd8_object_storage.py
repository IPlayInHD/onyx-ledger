"""PD-2 and PD-8 — what a document key says, and whether a binary can be removed.

THE TWO DEFECTS, as Entry 11A recorded them (§23, gap register):

    PD-2 — Document object keys embed the user-supplied filename; no `filename`
           column exists.

    PD-8 — `ObjectStorage` port has no `delete` method; binaries are
           unreachable by any cascade.

They belong together because they are the same surface. A filename in a key
leaks personal content into bucket listings, CDN and access logs, support
tooling and provider consoles — places with entirely different access control
from the RLS-protected row. And until a binary can be deleted at all, none of
those copies can ever be removed.

The filenames below are synthetic and belong to nobody. They are shaped like the
real problem — a person's name, an employer, what the document is about — because
a key that survives "t4.pdf" but leaks "Ali Abbas medical.pdf" is not fixed.
"""
from __future__ import annotations

import uuid

import pytest

from app.domain.ports import DeleteOutcome
from app.integrations.storage import LocalObjectStorage
from app.services.document_processing.service import (
    OBJECT_KEY_VERSION,
    _opaque_object_key,
)

#: Every token here must be absent from a generated key.
LEAKY_FILENAME = "Zorana Marchetti 2025 T4 medical private.pdf"
LEAKY_TOKENS = ("Zorana", "Marchetti", "medical", "private", "T4", ".pdf")

PASSWORD = "supersecret1"


async def _register(client) -> tuple[str, uuid.UUID]:
    email = f"pd2_{uuid.uuid4().hex[:10]}@example.com"
    assert (await client.post("/api/v1/auth/register",
                              json={"email": email, "password": PASSWORD})
            ).status_code == 201
    login = await client.post("/api/v1/auth/login",
                              json={"email": email, "password": PASSWORD})
    token = login.json()["access_token"]
    me = await client.get("/api/v1/users/me",
                          headers={"Authorization": f"Bearer {token}"})
    return token, uuid.UUID(me.json()["id"])


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# PD-2 — opaque keys
# ---------------------------------------------------------------------------
def test_a_generated_key_carries_no_part_of_the_filename():
    """The unit form: the key builder is given nothing but internal ids, so
    there is nothing user-supplied for it to leak."""
    user_id, document_id = uuid.uuid4(), uuid.uuid4()
    key = _opaque_object_key(user_id, document_id)

    assert key == f"{user_id}/{OBJECT_KEY_VERSION}/{document_id}"
    for token in LEAKY_TOKENS:
        assert token.lower() not in key.lower()


@pytest.mark.asyncio
async def test_uploading_a_revealing_filename_produces_an_opaque_key(client):
    """The production path. The route still ACCEPTS a filename — clients send
    one and it is still validated — and the system stores none of it."""
    token, user_id = await _register(client)

    response = await client.post(
        "/api/v1/documents",
        json={"document_type_code": "T4", "filename": LEAKY_FILENAME,
              "mime_type": "application/pdf"},
        headers=_headers(token))
    assert response.status_code in (200, 201), response.text
    document_id = response.json()["document_id"]

    from tests.security.test_pd1_tenant_isolation import owner_cursor
    with owner_cursor() as cur:
        cur.execute("SELECT object_key FROM docs.document WHERE id = %s",
                    (str(document_id),))
        key = cur.fetchone()[0]

    for term in LEAKY_TOKENS:
        assert term.lower() not in key.lower(), (
            f"the object key still carries {term!r}. Keys appear in bucket "
            "listings, CDN and access logs and provider consoles — none of "
            "which is reached by a database deletion."
        )
    assert key == f"{user_id}/{OBJECT_KEY_VERSION}/{document_id}"


@pytest.mark.asyncio
async def test_the_filename_is_not_stored_anywhere(client):
    """§9 — classified ORIGINAL_FILENAME_NOT_REQUIRED.

    No endpoint returns a filename, no column holds one, and the product never
    displayed it: it existed only as a side effect of the key. So it is not
    moved to a new column, it stops being retained. Entry 11B4 §9 is explicit
    that a column should not be introduced merely because the value was
    accidentally present before.
    """
    token, _ = await _register(client)
    await client.post(
        "/api/v1/documents",
        json={"document_type_code": "T4", "filename": LEAKY_FILENAME,
              "mime_type": "application/pdf"},
        headers=_headers(token))

    from tests.security.test_pd1_tenant_isolation import owner_cursor
    with owner_cursor() as cur:
        # Every text-ish column on the document row, checked as a whole rather
        # than column by column, so a future column cannot quietly reintroduce
        # the filename without failing here.
        cur.execute("SELECT to_jsonb(d)::text FROM docs.document d "
                    " ORDER BY created_at DESC LIMIT 5")
        rows = " ".join(r[0] for r in cur.fetchall())

    for term in ("Zorana", "Marchetti", "private"):
        assert term.lower() not in rows.lower(), (
            f"{term!r} from the filename is stored on the document row"
        )


@pytest.mark.asyncio
async def test_two_documents_with_the_same_filename_get_different_keys(client):
    """Keys are derived from the document id, so they are unique whatever the
    client calls the file — and a second upload cannot overwrite the first."""
    token, _ = await _register(client)
    keys = []
    for _ in range(2):
        response = await client.post(
            "/api/v1/documents",
            json={"document_type_code": "T4", "filename": "same-name.pdf",
                  "mime_type": "application/pdf"},
            headers=_headers(token))
        keys.append(response.json()["document_id"])
    assert keys[0] != keys[1]

    from tests.security.test_pd1_tenant_isolation import owner_cursor
    with owner_cursor() as cur:
        cur.execute("SELECT object_key FROM docs.document WHERE id = ANY(%s::uuid[])",
                    ([str(k) for k in keys],))
        stored = {r[0] for r in cur.fetchall()}
    assert len(stored) == 2, "two documents share one object key"


@pytest.mark.asyncio
async def test_the_api_does_not_hand_the_object_key_back(client):
    """§28 — the key is infrastructure, not an identifier for clients, and it
    is not authorization. It used to be returned by the list endpoint, which
    put the filename in front of every client that listed their documents."""
    token, _ = await _register(client)
    await client.post(
        "/api/v1/documents",
        json={"document_type_code": "T4", "filename": LEAKY_FILENAME,
              "mime_type": "application/pdf"},
        headers=_headers(token))

    listed = await client.get("/api/v1/documents", headers=_headers(token))
    assert listed.status_code == 200
    body = listed.text
    assert "object_key" not in body, "the API still returns the storage key"
    for term in LEAKY_TOKENS:
        assert term.lower() not in body.lower(), (
            f"the documents listing leaks {term!r}"
        )


# ---------------------------------------------------------------------------
# PD-8 — the binary can be removed
# ---------------------------------------------------------------------------
def test_the_port_declares_a_delete():
    """A capability that does not exist on the port cannot be relied on by any
    service, whatever a particular adapter happens to offer."""
    from app.domain.ports import ObjectStorage

    assert hasattr(ObjectStorage, "delete"), (
        "ObjectStorage has no delete; binaries stay unreachable (PD-8)"
    )


def test_deleting_an_object_removes_the_bytes():
    storage = LocalObjectStorage()
    bucket, key = "documents", f"{uuid.uuid4()}/v2/{uuid.uuid4()}"
    storage.presign_put(bucket, key, "application/pdf", max_bytes=1024)
    storage.put(bucket, key, b"synthetic document bytes")
    assert storage.get(bucket, key) == b"synthetic document bytes"

    assert storage.delete(bucket, key) is DeleteOutcome.DELETED
    assert storage.get(bucket, key) == b"", "the bytes survived deletion"


def test_deleting_a_missing_object_is_success_not_failure():
    """The property that makes a deletion lifecycle able to converge.

    A retry that finds the object already gone must be able to advance. If
    'already absent' were an error, a worker that crashed after deleting the
    binary but before recording it would retry forever and the document could
    never finish being deleted.
    """
    storage = LocalObjectStorage()
    outcome = storage.delete("documents", f"{uuid.uuid4()}/v2/{uuid.uuid4()}")
    assert outcome is DeleteOutcome.ALREADY_ABSENT
    assert outcome is not DeleteOutcome.RETRYABLE_FAILURE


def test_deletion_is_idempotent():
    storage = LocalObjectStorage()
    bucket, key = "documents", f"{uuid.uuid4()}/v2/{uuid.uuid4()}"
    storage.presign_put(bucket, key, "application/pdf", max_bytes=1024)
    storage.put(bucket, key, b"bytes")

    first = storage.delete(bucket, key)
    second = storage.delete(bucket, key)
    third = storage.delete(bucket, key)

    assert first is DeleteOutcome.DELETED
    assert second is third is DeleteOutcome.ALREADY_ABSENT


def test_deleting_releases_the_upload_authorization():
    """The per-key ceiling is authorization state for an upload that can no
    longer happen. Leaving it behind would let a replayed presign inherit a
    bound from an object that no longer exists."""
    storage = LocalObjectStorage()
    bucket, key = "documents", f"{uuid.uuid4()}/v2/{uuid.uuid4()}"
    storage.presign_put(bucket, key, "application/pdf", max_bytes=8)
    storage.put(bucket, key, b"12345678")
    storage.delete(bucket, key)

    # Without a fresh authorization there is no ceiling to inherit, so a write
    # that the old 8-byte bound would have refused is now simply unbounded —
    # which is correct, because a new upload needs a new presign.
    storage.put(bucket, key, b"much longer than eight bytes")
    assert storage.get(bucket, key) != b""


def test_the_delete_outcomes_are_a_closed_set():
    """§27 — a lifecycle branches on these, so they must be enumerable rather
    than provider strings. Two mean the object is gone; two do not."""
    assert {o.value for o in DeleteOutcome} == {
        "DELETED", "ALREADY_ABSENT", "RETRYABLE_FAILURE", "PERMANENT_FAILURE",
    }
