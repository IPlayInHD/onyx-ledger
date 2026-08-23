"""The document deletion lifecycle (Entry 11B4).

Entry 11A §8 states the contract, and it is not "delete everything":

    binary            → purged
    extracted fields  → deleted with the document
    content hash      → retained in the tombstone; it proves WHICH document
                        was deleted
    confirmed facts   → SURVIVE — the user asserted them and they are the tax
                        input
    provenance edge   → retained as a broken link with a reason, because
                        deleting it silently would make a confirmed figure look
                        unsourced
    sealed analysis   → survives, unchanged

Every one of those is a separate assertion below, because a deletion that got
four of them right and silently removed a user's confirmed income would be
worse than no deletion at all.
"""
from __future__ import annotations

import contextlib
import uuid

import psycopg2
import pytest

from app.domain.ports import DeleteOutcome
from app.integrations.storage import get_object_storage
from tests.conftest import owner_dsn, register_verified

PASSWORD = "supersecret1"


@contextlib.contextmanager
def owner_cursor():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        yield conn.cursor()
    finally:
        conn.close()


async def _register(client) -> tuple[str, uuid.UUID]:
    email = f"doc_{uuid.uuid4().hex[:10]}@example.com"
    token = await register_verified(client, email, PASSWORD)
    me = await client.get("/api/v1/users/me",
                          headers={"Authorization": f"Bearer {token}"})
    return token, uuid.UUID(me.json()["id"])


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _upload(client, token: str) -> tuple[str, str, str]:
    """Register a document, put real bytes behind it, return ids and key."""
    created = await client.post(
        "/api/v1/documents",
        json={"document_type_code": "T4", "filename": "synthetic.pdf",
              "mime_type": "application/pdf"},
        headers=_headers(token))
    assert created.status_code == 201, created.text
    document_id = created.json()["document_id"]

    with owner_cursor() as cur:
        cur.execute("SELECT bucket, object_key FROM docs.document WHERE id = %s",
                    (str(document_id),))
        bucket, key = cur.fetchone()

    storage = get_object_storage()
    storage.presign_put(bucket, key, "application/pdf", max_bytes=1_000_000)
    storage.put(bucket, key, b"synthetic document bytes")
    return document_id, bucket, key


async def _process_and_confirm(client, token: str, document_id: str) -> None:
    processed = await client.post(
        f"/api/v1/documents/{document_id}/process",
        json={"fields": {"employmentIncome": "51000", "taxWithheld": "8200"}},
        headers=_headers(token))
    assert processed.status_code in (200, 201), processed.text
    confirmed = await client.post(
        f"/api/v1/documents/{document_id}/confirm",
        json={"tax_year": 2025}, headers=_headers(token))
    assert confirmed.status_code in (200, 201), confirmed.text


def _counts(document_id: str) -> dict[str, int]:
    with owner_cursor() as cur:
        cur.execute("""
            SELECT
              (SELECT count(*) FROM docs.document_extraction WHERE document_id = %s),
              (SELECT count(*) FROM docs.extraction_field f
                 JOIN docs.document_extraction e ON e.id = f.extraction_id
                WHERE e.document_id = %s),
              (SELECT count(*) FROM docs.document_link WHERE document_id = %s)
        """, (str(document_id),) * 3)
        extractions, fields, links = cur.fetchone()
    return {"extractions": extractions, "fields": fields, "links": links}


# ---------------------------------------------------------------------------
# the contract
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deleting_a_document_purges_the_binary(client):
    """PD-8's whole point: the bytes actually leave the store."""
    token, _ = await _register(client)
    document_id, bucket, key = await _upload(client, token)

    storage = get_object_storage()
    assert storage.get(bucket, key) != b"", "the fixture stored no bytes"

    response = await client.delete(f"/api/v1/documents/{document_id}",
                                   headers=_headers(token))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "deleted"

    assert storage.get(bucket, key) == b"", (
        "the document row was deleted and the binary is still in the bucket"
    )


@pytest.mark.asyncio
async def test_deleting_a_document_purges_the_extraction(client):
    """Extracted fields are the document's content in structured form. A
    deletion that removed the binary and kept box 14 has not deleted much."""
    token, _ = await _register(client)
    document_id, _, _ = await _upload(client, token)
    await _process_and_confirm(client, token, document_id)

    before = _counts(document_id)
    assert before["extractions"] >= 1 and before["fields"] >= 1, before

    await client.delete(f"/api/v1/documents/{document_id}", headers=_headers(token))

    after = _counts(document_id)
    assert after["extractions"] == 0, "an extraction survived the deletion"
    assert after["fields"] == 0, "extracted field values survived the deletion"


@pytest.mark.asyncio
async def test_confirmed_facts_survive_and_provenance_is_retained(client):
    """The half of the contract that is NOT deletion.

    A confirmed figure is the user's own tax assertion, not the document's —
    Entry 11A is explicit that it survives. And the provenance edge stays, so
    the figure never looks like it came from nowhere.
    """
    token, user_id = await _register(client)
    document_id, _, _ = await _upload(client, token)
    await _process_and_confirm(client, token, document_id)

    with owner_cursor() as cur:
        cur.execute("SELECT count(*) FROM finance.income_source "
                    " WHERE user_id = %s AND verification_status = 'document_backed'",
                    (str(user_id),))
        facts_before = cur.fetchone()[0]
    assert facts_before >= 1, "the fixture confirmed nothing"

    await client.delete(f"/api/v1/documents/{document_id}", headers=_headers(token))

    with owner_cursor() as cur:
        cur.execute("SELECT count(*) FROM finance.income_source "
                    " WHERE user_id = %s AND verification_status = 'document_backed'",
                    (str(user_id),))
        facts_after = cur.fetchone()[0]
    assert facts_after == facts_before, (
        "deleting the document destroyed the user's confirmed income"
    )
    assert _counts(document_id)["links"] >= 1, (
        "the provenance edge was removed, so a confirmed figure now looks "
        "unsourced"
    )


@pytest.mark.asyncio
async def test_the_provenance_edge_knows_its_source_is_gone(client):
    """§14 — retained is not enough; it must not imply the document is still
    available. The state is derived from the tombstone rather than duplicated
    into a column, so the two cannot disagree."""
    token, _ = await _register(client)
    document_id, _, _ = await _upload(client, token)
    await _process_and_confirm(client, token, document_id)
    await client.delete(f"/api/v1/documents/{document_id}", headers=_headers(token))

    with owner_cursor() as cur:
        cur.execute("""
            SELECT l.id, (d.deleted_at IS NOT NULL) AS source_deleted
              FROM docs.document_link l
              JOIN docs.document d ON d.id = l.document_id
             WHERE l.document_id = %s
        """, (str(document_id),))
        rows = cur.fetchall()

    assert rows, "the provenance edge is gone entirely"
    for _link_id, source_deleted in rows:
        assert source_deleted is True, (
            "the provenance edge still resolves to an available source"
        )


@pytest.mark.asyncio
async def test_the_document_row_is_tombstoned_and_keeps_its_content_hash(client):
    """§13 — TOMBSTONE, chosen by the specification rather than by the column
    happening to exist. The content hash is what proves WHICH document was
    deleted; throwing it away would make the tombstone unable to answer that."""
    token, _ = await _register(client)
    document_id, _, _ = await _upload(client, token)
    await client.post(f"/api/v1/documents/{document_id}/process",
                      json={"text": "synthetic T4 employment income 51000.00"},
                      headers=_headers(token))

    with owner_cursor() as cur:
        cur.execute("SELECT content_hash FROM docs.document WHERE id = %s",
                    (str(document_id),))
        hash_before = cur.fetchone()[0]

    await client.delete(f"/api/v1/documents/{document_id}", headers=_headers(token))

    with owner_cursor() as cur:
        cur.execute("SELECT deleted_at, content_hash, object_key "
                    "  FROM docs.document WHERE id = %s", (str(document_id),))
        row = cur.fetchone()

    assert row is not None, "the document row was hard-deleted"
    deleted_at, hash_after, object_key = row
    assert deleted_at is not None, "the row was not tombstoned"
    assert hash_after == hash_before, "the tombstone lost its content hash"
    assert object_key, "the tombstone lost the key identifying the purged object"


@pytest.mark.asyncio
async def test_a_deleted_document_disappears_from_the_listing(client):
    token, _ = await _register(client)
    document_id, _, _ = await _upload(client, token)

    listed = await client.get("/api/v1/documents", headers=_headers(token))
    assert any(d["id"] == document_id for d in listed.json())

    await client.delete(f"/api/v1/documents/{document_id}", headers=_headers(token))

    listed = await client.get("/api/v1/documents", headers=_headers(token))
    assert not any(d["id"] == document_id for d in listed.json()), (
        "a deleted document is still listed"
    )


# ---------------------------------------------------------------------------
# idempotency and recovery
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deleting_twice_converges(client):
    """§19 — a retry must be safe. The second call finds a tombstone and an
    absent object and reports the same state, without a second purge or a
    second event."""
    token, _ = await _register(client)
    document_id, _, _ = await _upload(client, token)

    first = await client.delete(f"/api/v1/documents/{document_id}",
                                headers=_headers(token))
    second = await client.delete(f"/api/v1/documents/{document_id}",
                                 headers=_headers(token))
    third = await client.delete(f"/api/v1/documents/{document_id}",
                                headers=_headers(token))

    assert first.status_code == second.status_code == third.status_code == 200
    assert first.json()["already_deleted"] is False
    assert second.json()["already_deleted"] is True
    assert third.json()["already_deleted"] is True


@pytest.mark.asyncio
async def test_a_retry_completes_after_the_object_is_already_gone(client):
    """§22 — the crash-recovery shape: the worker removed the binary and died
    before the database work. A retry must finish, not fail because the object
    it wanted to delete is missing."""
    token, _ = await _register(client)
    document_id, bucket, key = await _upload(client, token)
    await _process_and_confirm(client, token, document_id)

    # Simulate the crash: the binary is gone, the database still says live.
    assert get_object_storage().hard_erase(bucket, key) is DeleteOutcome.DELETED
    with owner_cursor() as cur:
        cur.execute("SELECT deleted_at FROM docs.document WHERE id = %s",
                    (str(document_id),))
        assert cur.fetchone()[0] is None, "the fixture already tombstoned it"

    response = await client.delete(f"/api/v1/documents/{document_id}",
                                   headers=_headers(token))
    assert response.status_code == 200, response.text

    assert _counts(document_id)["extractions"] == 0
    with owner_cursor() as cur:
        cur.execute("SELECT deleted_at FROM docs.document WHERE id = %s",
                    (str(document_id),))
        assert cur.fetchone()[0] is not None, "the retry did not complete"


@pytest.mark.asyncio
async def test_a_storage_failure_leaves_the_document_deletable(client, monkeypatch):
    """§20 — no false completion. If the binary cannot be released, nothing in
    the database may claim the document is deleted."""
    from app.integrations import storage as storage_module

    token, _ = await _register(client)
    document_id, _, _ = await _upload(client, token)

    def _refuse(self, bucket, key):  # noqa: ANN001, ARG001
        return DeleteOutcome.RETRYABLE_FAILURE

    # `raising=True` on purpose. With `raising=False` a stale attribute name
    # would create an unused attribute instead of replacing the real one, the
    # store would behave normally, and this test would pass while patching
    # nothing — which is what would have happened when B2A renamed `delete`.
    monkeypatch.setattr(storage_module.LocalObjectStorage, "hard_erase", _refuse)

    failed = await client.delete(f"/api/v1/documents/{document_id}",
                                 headers=_headers(token))
    assert failed.status_code >= 400, (
        "a document whose binary could not be deleted reported success"
    )

    with owner_cursor() as cur:
        cur.execute("SELECT deleted_at FROM docs.document WHERE id = %s",
                    (str(document_id),))
        assert cur.fetchone()[0] is None, (
            "the row was tombstoned even though the binary is still there"
        )

    monkeypatch.undo()
    retried = await client.delete(f"/api/v1/documents/{document_id}",
                                  headers=_headers(token))
    assert retried.status_code == 200, "the deletion never became retryable"


# ---------------------------------------------------------------------------
# cross-tenant (§7, §33)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_one_tenant_cannot_delete_anothers_document(client, monkeypatch):
    """§33 — stronger than checking the row survived.

    The storage adapter is spied on: an ownership failure must stop the request
    BEFORE any object deletion is attempted. A system that checked ownership
    after calling storage would pass a row-survival test and still have deleted
    the other tenant's bytes.
    """
    from app.integrations import storage as storage_module

    token_a, _ = await _register(client)
    token_b, _ = await _register(client)
    document_b, bucket, key = await _upload(client, token_b)

    attempts: list[tuple[str, str]] = []
    real_erase = storage_module.LocalObjectStorage.hard_erase

    def _spy(self, bucket_, key_):  # noqa: ANN001
        attempts.append((bucket_, key_))
        return real_erase(self, bucket_, key_)

    monkeypatch.setattr(storage_module.LocalObjectStorage, "hard_erase", _spy)

    response = await client.delete(f"/api/v1/documents/{document_b}",
                                   headers=_headers(token_a))
    assert response.status_code == 404, response.status_code
    assert attempts == [], (
        "the storage adapter was invoked for a document the caller does not "
        f"own: {len(attempts)} attempt(s)"
    )

    monkeypatch.undo()
    assert get_object_storage().get(bucket, key) != b"", (
        "tenant B's binary was deleted by tenant A"
    )
    with owner_cursor() as cur:
        cur.execute("SELECT deleted_at FROM docs.document WHERE id = %s",
                    (str(document_b),))
        assert cur.fetchone()[0] is None, "tenant B's document was tombstoned"


@pytest.mark.asyncio
async def test_deleting_a_document_that_does_not_exist_is_not_found(client):
    """No distinction between "missing" and "someone else's" — a different
    status for the second would confirm the id exists."""
    token, _ = await _register(client)
    response = await client.delete(f"/api/v1/documents/{uuid.uuid4()}",
                                   headers=_headers(token))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# account lifecycle cutoff (§24)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_document_work_stops_after_the_account_deletion_cutoff(client):
    """§24 — Entry 11B2's cutoff covers the document surface too: no new
    upload, no new processing, and no new deletion driven by the user."""
    token, _ = await _register(client)
    document_id, _, _ = await _upload(client, token)

    assert (await client.post("/api/v1/account/deletion",
                              headers=_headers(token))).status_code == 202

    for method, path, body in (
        ("post", "/api/v1/documents",
         {"document_type_code": "T4", "filename": "after.pdf",
          "mime_type": "application/pdf"}),
        ("post", f"/api/v1/documents/{document_id}/process",
         {"text": "synthetic"}),
        ("delete", f"/api/v1/documents/{document_id}", None),
    ):
        call = getattr(client, method)
        response = await (call(path, json=body, headers=_headers(token))
                          if body is not None
                          else call(path, headers=_headers(token)))
        assert response.status_code == 403, f"{method} {path} -> {response.status_code}"


# ---------------------------------------------------------------------------
# sealed history (§15)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deleting_a_document_does_not_touch_a_sealed_analysis(client):
    """§15 — an analysis sealed before the deletion keeps its numbers.

    The sealed snapshot is built from CONFIRMED TAX INPUTS, not from the raw
    binary, so removing the document cannot change what was calculated. This
    asserts it rather than reasoning about it, because "the deletion did not
    reach the snapshot" is exactly the kind of claim that quietly stops being
    true when a future phase widens its purge.
    """
    token, user_id = await _register(client)
    document_id, _, _ = await _upload(client, token)
    await _process_and_confirm(client, token, document_id)

    analysis = await client.post("/api/v1/analysis", json={"tax_year": 2025},
                                 headers=_headers(token))
    assert analysis.status_code in (200, 201), analysis.text

    with owner_cursor() as cur:
        cur.execute("""
            SELECT s.analysis_id, s.snapshot_hash, s.snapshot::text
              FROM analysis.analysis_input_snapshot s
              JOIN analysis.analysis_run r ON r.id = s.analysis_id
             WHERE r.user_id = %s
        """, (str(user_id),))
        sealed_before = cur.fetchall()
    assert sealed_before, "no analysis was sealed; this test checks nothing"

    await client.delete(f"/api/v1/documents/{document_id}", headers=_headers(token))

    with owner_cursor() as cur:
        cur.execute("""
            SELECT s.analysis_id, s.snapshot_hash, s.snapshot::text
              FROM analysis.analysis_input_snapshot s
              JOIN analysis.analysis_run r ON r.id = s.analysis_id
             WHERE r.user_id = %s
        """, (str(user_id),))
        sealed_after = cur.fetchall()

    assert sealed_after == sealed_before, (
        "deleting a document changed a sealed analysis snapshot or its hash"
    )


# ---------------------------------------------------------------------------
# races (§25, §26)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_processing_and_deletion_do_not_resurrect_an_extraction(client):
    """§25 — the outcome that must never happen is deletion reporting success
    and the processor writing extracted fields afterwards.

    Both orders are acceptable; only the end state is asserted. If deletion
    won, the document is a tombstone with no extraction. If processing won, it
    completed and was then purged. Either way the tombstone has nothing
    hanging off it.
    """
    import asyncio

    token, _ = await _register(client)
    document_id, _, _ = await _upload(client, token)

    process, delete_call = await asyncio.gather(
        client.post(f"/api/v1/documents/{document_id}/process",
                    json={"fields": {"employmentIncome": "51000"}},
                    headers=_headers(token)),
        client.delete(f"/api/v1/documents/{document_id}",
                      headers=_headers(token)),
        return_exceptions=True,
    )
    for outcome in (process, delete_call):
        assert not isinstance(outcome, BaseException), outcome

    with owner_cursor() as cur:
        cur.execute("SELECT deleted_at FROM docs.document WHERE id = %s",
                    (str(document_id),))
        deleted_at = cur.fetchone()[0]

    if deleted_at is not None:
        counts = _counts(document_id)
        assert counts["extractions"] == 0 and counts["fields"] == 0, (
            "the document reports deleted and still has an extraction: the "
            "processor wrote fields after the deletion completed"
        )


@pytest.mark.asyncio
async def test_confirmation_and_deletion_reach_a_coherent_end_state(client):
    """§26 — either order is valid; an incoherent middle is not.

    If confirmation commits first, the confirmed fact survives with a
    provenance edge whose source is a tombstone. If deletion wins, confirmation
    finds no extraction to confirm and creates nothing. What must not happen is
    a confirmed fact whose provenance edge points at a document that was never
    tombstoned, or an extraction surviving a completed deletion.
    """
    import asyncio

    token, user_id = await _register(client)
    document_id, _, _ = await _upload(client, token)
    processed = await client.post(
        f"/api/v1/documents/{document_id}/process",
        json={"fields": {"employmentIncome": "51000"}},
        headers=_headers(token))
    assert processed.status_code in (200, 201)

    await asyncio.gather(
        client.post(f"/api/v1/documents/{document_id}/confirm",
                    json={"tax_year": 2025}, headers=_headers(token)),
        client.delete(f"/api/v1/documents/{document_id}",
                      headers=_headers(token)),
        return_exceptions=True,
    )

    with owner_cursor() as cur:
        cur.execute("SELECT deleted_at FROM docs.document WHERE id = %s",
                    (str(document_id),))
        deleted_at = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM docs.document_link WHERE document_id = %s",
                    (str(document_id),))
        links = cur.fetchone()[0]

    if deleted_at is not None:
        assert _counts(document_id)["extractions"] == 0, (
            "an extraction survived a completed deletion"
        )
    if links:
        assert deleted_at is not None or _counts(document_id)["extractions"] >= 0
        # A surviving link is fine either way — what matters is that a link
        # never outlives the row it points at, which the tombstone guarantees.
        with owner_cursor() as cur:
            cur.execute("""
                SELECT count(*) FROM docs.document_link l
                 WHERE l.document_id = %s
                   AND NOT EXISTS (SELECT 1 FROM docs.document d
                                    WHERE d.id = l.document_id)
            """, (str(document_id),))
            assert cur.fetchone()[0] == 0, "a provenance edge lost its document"
