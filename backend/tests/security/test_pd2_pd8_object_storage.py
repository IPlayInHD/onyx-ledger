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


#: Filenames chosen to break a key builder that concatenates. Traversal in both
#: slash directions, a control character, an overlong name, and a Unicode
#: right-to-left override — the classic way to make a display name lie about the
#: extension it ends with.
HOSTILE_FILENAMES = [
    "../../../../etc/passwd",
    "..\\..\\windows\\system32\\config\\sam",
    "a/b/c/nested.pdf",
    "trailing/",
    "/absolute.pdf",
    "nul\x00byte.pdf",
    "carriage\rreturn.pdf",
    "new\nline.pdf",
    "\u202egnp.exe.pdf",
    "\u0000\u0001\u0002.pdf",
    "sp ace.pdf",
    "%2e%2e%2fescaped.pdf",
    "x" * 400 + ".pdf",
    ".",
    "..",
]


@pytest.mark.parametrize("filename", HOSTILE_FILENAMES)
def test_no_filename_can_reach_the_key_builder(filename):
    """The unit-level reason traversal is not a risk here: the builder takes
    two UUIDs and there is no parameter a filename could arrive through.

    This is stronger than sanitising. A sanitiser has to be right about every
    encoding; a signature with nowhere to put the string has to be right once.
    """
    user_id, document_id = uuid.uuid4(), uuid.uuid4()
    key = _opaque_object_key(user_id, document_id)
    assert key == f"{user_id}/{OBJECT_KEY_VERSION}/{document_id}"
    assert filename not in key


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", HOSTILE_FILENAMES)
async def test_a_hostile_filename_cannot_shape_the_stored_key(client, filename):
    """End to end: whatever the client sends, the stored key is three internal
    identifiers and the object stays inside the caller's own prefix.

    A key that escaped its prefix would be the real damage — `{user_id}/` is
    what makes an account-level purge a single prefix operation, so an object
    written outside it would survive the account that owns it.
    """
    token, user_id = await _register(client)
    created = await client.post(
        "/api/v1/documents",
        json={"document_type_code": "T4", "filename": filename},
        headers=_headers(token),
    )
    if created.status_code == 422:
        # Refused outright is also a correct answer — but see
        # `test_the_hostile_filename_set_is_not_all_rejected`, which stops this
        # branch from quietly turning the whole parametrization into a no-op.
        return
    assert created.status_code == 201, created.text

    document_id = created.json()["document_id"]
    row = _stored_key(document_id)
    assert row == f"{user_id}/{OBJECT_KEY_VERSION}/{document_id}"
    assert ".." not in row and "\\" not in row
    assert row.startswith(f"{user_id}/"), "the object escaped its owner's prefix"
    assert row.count("/") == 2, f"the key gained a path segment: {row!r}"


@pytest.mark.asyncio
async def test_the_hostile_filename_set_is_not_all_rejected(client):
    """Guards the parametrization above.

    That test returns early on a 422, which is correct — refusing the input is
    a fine answer. But if the route began refusing EVERY name in the set, every
    case would take the early return and the key assertions would stop running
    while the suite stayed green. So at least one hostile name must actually be
    accepted and reach the assertion that the key is unaffected.
    """
    token, user_id = await _register(client)
    accepted = []
    for filename in HOSTILE_FILENAMES:
        created = await client.post(
            "/api/v1/documents",
            json={"document_type_code": "T4", "filename": filename},
            headers=_headers(token),
        )
        if created.status_code == 201:
            accepted.append(filename)

    assert accepted, (
        "every hostile filename was refused, so the key assertions above never "
        "ran; either loosen the set or drop the early return"
    )
    for filename in accepted:
        assert filename not in _stored_key_for_user(user_id), filename


def _stored_key_for_user(user_id) -> str:
    """Every key this account owns, concatenated — cheap containment check."""
    from tests.security.test_pd1_tenant_isolation import owner_cursor

    with owner_cursor() as cur:
        cur.execute("SELECT string_agg(object_key, '|') FROM docs.document "
                    "WHERE user_id = %s", (str(user_id),))
        return cur.fetchone()[0] or ""


def _stored_key(document_id: str) -> str:
    """The key as PERSISTED.

    Read from the database rather than from a response, because the API
    deliberately does not hand the object key back — see
    `test_the_api_does_not_hand_the_object_key_back`.

    Through `owner_cursor`, the same way every other assertion in this file
    reads a document row. An earlier version of this helper opened a plain
    `unit_of_work()`, which sets no tenant GUC — so RLS correctly hid the row
    and the helper returned `None` for every filename the route accepted. The
    test failed while the product was right, which is the wrong way round.
    """
    from tests.security.test_pd1_tenant_isolation import owner_cursor

    with owner_cursor() as cur:
        cur.execute("SELECT object_key FROM docs.document WHERE id = %s",
                    (str(document_id),))
        row = cur.fetchone()
    assert row is not None, f"no document row for {document_id}"
    return row[0]


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


# ---------------------------------------------------------------------------
# regression guards (§46)
# ---------------------------------------------------------------------------
def test_the_key_builder_takes_no_user_supplied_argument():
    """§46 — the structural guarantee behind PD-2.

    A key builder that cannot SEE a filename cannot leak one. Asserted on the
    signature rather than on the output, because an output test only covers the
    filenames somebody thought to try.
    """
    import inspect

    parameters = list(
        inspect.signature(_opaque_object_key).parameters.values()
    )
    assert [p.name for p in parameters] == ["user_id", "document_id"], (
        "the object-key builder gained an argument. If it is user-supplied, "
        "the key can carry user content again (PD-2)."
    )
    for parameter in parameters:
        assert parameter.annotation in (uuid.UUID, "uuid.UUID"), (
            f"{parameter.name} is not a UUID, so it may carry free text"
        )


def test_no_production_code_builds_a_document_key_from_a_filename():
    """The other half: the builder is safe, and nothing bypasses it.

    A future `f"{user_id}/{uuid4()}/{filename}"` somewhere else would reopen
    PD-2 while every test above kept passing.
    """
    import pathlib
    import re

    app_root = pathlib.Path(__file__).resolve().parents[2] / "app"
    pattern = re.compile(r"object_key\s*=\s*f?[\"'].*\{filename\}")
    offenders = [
        f"{path.relative_to(app_root.parent)}:{number}"
        for path in app_root.rglob("*.py")
        if "__pycache__" not in path.parts
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, (
        f"{offenders} build a document object key from a filename (PD-2)"
    )


def test_the_document_service_deletes_through_the_port():
    """§42 — a delete method that exists but is unused does not close PD-8.

    Asserted on the service source: the production lifecycle must actually
    reach the storage boundary, not merely be able to.
    """
    import inspect

    from app.services.document_processing.service import DocumentService

    body = inspect.getsource(DocumentService.delete_document)
    assert "self.storage.delete(" in body, (
        "DocumentService.delete_document does not call the storage port, so "
        "the binary survives the document (PD-8)"
    )
    assert "doc.bucket" in body and "doc.object_key" in body, (
        "the deletion does not resolve bucket and key from the OWNED row; a "
        "client-supplied key would be authority to delete an arbitrary object"
    )


def test_the_delete_route_never_accepts_a_bucket_or_key():
    """§7 — object identity is server-authoritative.

    The route takes a document id and nothing else. A caller able to name its
    own key could ask the platform to delete an arbitrary object, and the
    ownership check would never see it.
    """
    import inspect

    from app.api.v1.documents import routes

    signature = inspect.signature(routes.delete_document)
    assert set(signature.parameters) == {"document_id", "user_id", "session"}, (
        f"the delete route accepts {sorted(signature.parameters)} — a bucket "
        "or key parameter would make the client the authority on what to "
        "delete"
    )
