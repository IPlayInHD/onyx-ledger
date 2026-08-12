"""The DOCUMENTS account-level deletion phase (Entry 11B6F).

`DocumentProcessingService.delete_document` has been the authoritative
per-document operation since Entry 11B4 — binary first, then extraction rows,
then a tombstone that keeps the content hash and the opaque object key. Nothing
ran it for an account being deleted. `DOCUMENTS` was a legal phase value and a
`SourceDataPhase` member with no worker branch behind it.

THE ORDERING IS THE DESIGN. PostgreSQL and the object store are not one
transaction. The binary goes first, per document, because it is the
irreversible part with no transaction to roll back: a crash after it leaves a
live row whose retry converges, while the reverse order would leave a binary
nobody can find again. That is also why the completion guard can be pure SQL —
a tombstone is only written once its binary is gone, so zero live rows is a
durable record that zero objects remain.

WHAT SURVIVES ON PURPOSE, per Entry 11A §8: the content hash (which document
was deleted), the opaque object key (deleted-on-purpose versus vanished), the
provenance edge in `docs.document_link` (so a confirmed figure never looks
unsourced), and the confirmed facts the user asserted.
"""

from __future__ import annotations

import uuid

import psycopg2
import pytest

from app.database.models import UserAccount
from app.database.session import unit_of_work
from app.integrations.storage import DeleteOutcome, get_object_storage
from tests.conftest import owner_dsn

PHASE = "DOCUMENTS"
EARLIER_PHASES = ("SOURCE_DATA",)


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


async def _account(tag: str = "docs") -> uuid.UUID:
    async with unit_of_work(user_id=None, actor_type="system") as s:
        account = UserAccount(
            email=f"{tag}-{uuid.uuid4().hex[:10]}@example.test", status="active"
        )
        s.add(account)
        await s.flush()
        return account.id


def _document(cur, uid, *, with_extraction: bool = True) -> tuple[str, str, str]:
    """One uploaded document with a real object behind it.

    The object key is opaque by construction (11B4): an account id, a version
    and a document id, and no filename — the schema stores none, which is why
    this phase has no filename to sanitize.
    """
    document_id = uuid.uuid4()
    bucket = "onyx-documents"
    object_key = f"u/{uid}/v1/{document_id}"
    cur.execute("""
        INSERT INTO docs.document
               (id, user_id, storage_provider, bucket, object_key, content_hash,
                mime_type, byte_size, status, uploaded_at)
        VALUES (%s, %s, 'local', %s, %s, %s, 'application/pdf', 1024,
                'uploaded', now())
    """, (str(document_id), str(uid), bucket, object_key,
          uuid.uuid4().hex + uuid.uuid4().hex))
    if with_extraction:
        cur.execute("INSERT INTO docs.document_extraction (document_id, engine, "
                    "status) VALUES (%s,'probe','processed') RETURNING id",
                    (str(document_id),))
        extraction_id = cur.fetchone()[0]
        cur.execute("INSERT INTO docs.extraction_field (extraction_id, field_name, "
                    "value_text, value_number) VALUES (%s,'box_14','52310.00',52310)",
                    (str(extraction_id),))

    storage = get_object_storage()
    storage.put(bucket, object_key, b"%PDF-1.4 pretend T4 with a salary on it")
    return str(document_id), bucket, object_key


def _object_exists(bucket: str, key: str) -> bool:
    """Does the object still hold bytes?

    `get` returns `b""` for a missing key rather than raising, so "no exception"
    is not the same as "present" — a helper that only caught exceptions reported
    every deleted object as still there.
    """
    return bool(get_object_storage().get(bucket, key))


def _remaining(cur, uid) -> int:
    cur.execute("SELECT identity.count_remaining_document_privacy_work(%s)", (str(uid),))
    return cur.fetchone()[0]


def _walk_to_purging(cur, uid) -> None:
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s,'DELETION_REQUESTED')", (str(uid),))
    for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
        cur.execute("UPDATE identity.account_lifecycle SET state=%s WHERE user_id=%s",
                    (state, str(uid)))


def _mark_earlier_phases_complete(cur, uid) -> None:
    for phase in EARLIER_PHASES:
        cur.execute("""
            INSERT INTO identity.account_lifecycle_phase
                   (user_id, phase, status, attempts, started_at, completed_at)
            VALUES (%s, %s, 'COMPLETE', 1, now(), now())
            ON CONFLICT (user_id, phase) DO UPDATE
               SET status='COMPLETE', completed_at=now()
        """, (str(uid), phase))


def _phase_status(cur, uid, phase=PHASE):
    cur.execute("SELECT status FROM identity.account_lifecycle_phase"
                " WHERE user_id=%s AND phase=%s", (str(uid), phase))
    row = cur.fetchone()
    return row[0] if row else None


def _claim(cur, uid, worker="probe") -> uuid.UUID:
    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by=%s,"
                " claim_token=%s, claimed_at=now() WHERE user_id=%s",
                (worker, str(token), str(uid)))
    return token


def _release(cur, uid) -> None:
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by=NULL,"
                " claim_token=NULL, claimed_at=NULL WHERE user_id=%s", (str(uid),))


async def _run_worker(worker_id="privacy-worker") -> dict[str, int]:
    import asyncio

    from workers.tasks.privacy import run_account_deletion_phases

    return await asyncio.to_thread(run_account_deletion_phases, worker_id=worker_id)


# ------------------------------------------------------------- the phase -----

async def test_the_binary_the_extraction_and_the_row_all_go():
    """§1 — nothing the user uploaded is retrievable afterwards."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    document_id, bucket, object_key = _document(cur, uid)
    assert _object_exists(bucket, object_key), "the fixture stored no object"
    assert _remaining(cur, uid) == 3, "document + extraction + field expected"

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()

    assert _phase_status(cur, uid) == "COMPLETE", _phase_status(cur, uid)
    assert _remaining(cur, uid) == 0
    assert not _object_exists(bucket, object_key), (
        "the binary is still retrievable from object storage"
    )
    cur.execute("SELECT count(*) FROM docs.document_extraction WHERE document_id=%s",
                (document_id,))
    assert cur.fetchone()[0] == 0, "extracted content survived"
    cur.execute("SELECT deleted_at IS NOT NULL, content_hash IS NOT NULL, "
                "       object_key IS NOT NULL "
                "  FROM docs.document WHERE id=%s", (document_id,))
    tombstoned, has_hash, has_key = cur.fetchone()
    assert tombstoned, "the document row was not tombstoned"
    assert has_hash, "the content hash was discarded; it proves which document went"
    assert has_key, "the opaque object key was cleared; orphan detection needs it"
    conn.close()


async def test_extracted_values_are_treated_as_the_content_they_came_from():
    """§26 — deleting the PDF while keeping its numbers is not deletion.

    `docs.extraction_field.value_text` holds what was read off the page — a
    salary, a name on a slip. It is checked by value rather than by row count,
    because "the row is gone" and "the number is gone" are not the same claim.
    """
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    document_id, _, _ = _document(cur, uid)
    cur.execute("SELECT count(*) FROM docs.extraction_field f "
                " JOIN docs.document_extraction e ON e.id=f.extraction_id "
                " WHERE e.document_id=%s AND f.value_text='52310.00'", (document_id,))
    assert cur.fetchone()[0] == 1

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()

    cur.execute("SELECT count(*) FROM docs.extraction_field f "
                " JOIN docs.document_extraction e ON e.id=f.extraction_id "
                " WHERE e.document_id=%s", (document_id,))
    assert cur.fetchone()[0] == 0, "extracted values survived the phase"
    conn.close()


async def test_many_documents_are_all_purged_in_one_pass():
    """The account, not a document. Twenty at once, binaries included."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    objects = [_document(cur, uid, with_extraction=False)[1:] for _ in range(20)]
    assert _remaining(cur, uid) == 20

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()

    assert _remaining(cur, uid) == 0
    assert not any(_object_exists(b, k) for b, k in objects), (
        "at least one binary survived the account pass"
    )
    conn.close()


# ------------------------------------------------------------- the guard -----

async def test_completion_is_refused_for_each_class_of_residue():
    """§44 — guard-on-the-guard, one residue class at a time.

    Each is injected on its own, so a guard that only noticed live documents
    would fail the extraction cases rather than passing by luck.
    """
    cases = {
        "live document row": lambda cur, uid: _document(cur, uid, with_extraction=False),
        "extraction row": lambda cur, uid: _document(cur, uid, with_extraction=True),
    }
    for label, make in cases.items():
        uid = await _account()
        conn = _owner()
        cur = conn.cursor()
        make(cur, uid)
        _walk_to_purging(cur, uid)
        token = _claim(cur, uid)
        cur.execute("SELECT identity.start_lifecycle_phase(%s,%s,%s,'probe')",
                    (str(uid), PHASE, str(token)))
        with pytest.raises(psycopg2.Error) as excinfo:
            cur.execute("SELECT identity.complete_lifecycle_phase(%s,%s,%s,'probe')",
                        (str(uid), PHASE, str(token)))
        assert "DOCUMENTS cannot complete" in str(excinfo.value), f"{label}: {excinfo.value}"
        conn.close()


async def test_an_extraction_row_that_outlives_its_tombstone_still_blocks():
    """The residue the ordering cannot rule out, and the reason it is counted.

    A tombstoned document whose extraction rows survived would look finished if
    the guard counted only live documents. It is built by hand because the
    keyhole never produces it — which is exactly why the guard has to.
    """
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    document_id, _, _ = _document(cur, uid)
    cur.execute("UPDATE docs.document SET deleted_at=now() WHERE id=%s", (document_id,))
    assert _remaining(cur, uid) == 2, (
        "a tombstoned document with surviving extracted content reads as finished"
    )
    conn.close()


# ---------------------------------------------------- storage failure modes --

class _FailingStorage:
    """A provider that refuses one key and behaves for every other."""

    def __init__(self, doomed_key: str, outcome: DeleteOutcome):
        self._real = get_object_storage()
        self._doomed = doomed_key
        self._outcome = outcome
        self.calls: list[str] = []

    def delete(self, bucket: str, key: str) -> DeleteOutcome:
        self.calls.append(key)
        if key == self._doomed:
            return self._outcome
        return self._real.delete(bucket, key)


async def _run_phase_with_storage(uid, storage):
    """Drive the phase directly so a failing provider can be injected."""
    from app.database.privacy_session import privacy_unit_of_work
    from app.services.privacy import AccountLifecycleService, DocumentPurgeService

    async with privacy_unit_of_work() as session:
        claimed = await AccountLifecycleService(session).claim(
            worker_id="storage-probe", batch_size=10)
    mine = [c for c in claimed if c.user_id == uid]
    assert mine, "the account was not claimable"
    async with privacy_unit_of_work() as session:
        return await DocumentPurgeService(session, storage=storage).run(
            mine[0], worker_id="storage-probe")


@pytest.mark.parametrize(
    "outcome", [DeleteOutcome.RETRYABLE_FAILURE, DeleteOutcome.PERMANENT_FAILURE]
)
async def test_a_failing_object_keeps_the_phase_incomplete(outcome):
    """§29 — one bad object must not let the account finish.

    And the good ones must still go: a provider error on document three is not
    a reason to leave documents one and two behind.
    """
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    good = _document(cur, uid, with_extraction=False)
    doomed = _document(cur, uid, with_extraction=False)
    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)

    storage = _FailingStorage(doomed[2], outcome)
    result = await _run_phase_with_storage(uid, storage)

    assert not result.completed, "the phase completed with an undeleted object"
    assert result.failure_code == f"STORAGE_{outcome.name}"
    assert _phase_status(cur, uid) != "COMPLETE"
    assert _remaining(cur, uid) == 1, "only the failing document should remain"
    assert not _object_exists(good[1], good[2]), (
        "a healthy object was skipped because another one failed"
    )
    assert _object_exists(doomed[1], doomed[2])
    conn.close()


async def test_a_missing_object_converges_instead_of_failing_forever():
    """§18/§32 — ALREADY_ABSENT is success.

    This is the crash-after-storage-delete case: the binary went, the worker
    died before the tombstone, and the retry finds nothing at the key. A phase
    that treated that as an error would never converge, and a binary that
    outlives its document is the only thing worse.
    """
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    document_id, bucket, object_key = _document(cur, uid, with_extraction=False)
    get_object_storage().delete(bucket, object_key)   # the crash's other half
    assert not _object_exists(bucket, object_key)
    assert _remaining(cur, uid) == 1, "the live row survived the crash, as designed"

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()

    assert _phase_status(cur, uid) == "COMPLETE"
    assert _remaining(cur, uid) == 0
    conn.close()


async def test_running_the_phase_twice_deletes_nothing_the_second_time():
    """§35 — idempotency, counted at the provider."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    _document(cur, uid)
    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()
    assert _phase_status(cur, uid) == "COMPLETE"

    _release(cur, uid)
    cur.execute("UPDATE identity.account_lifecycle_phase SET status='RUNNING',"
                " completed_at=NULL WHERE user_id=%s AND phase=%s", (str(uid), PHASE))
    storage = _FailingStorage("no-such-key", DeleteOutcome.PERMANENT_FAILURE)
    result = await _run_phase_with_storage(uid, storage)

    assert result.completed, "the second run did not converge"
    assert storage.calls == [], (
        f"the second run made {len(storage.calls)} provider call(s); a tombstoned "
        "document must not be offered for deletion again"
    )
    conn.close()


# ------------------------------------------------------------- recovery ------

async def test_a_crash_before_any_deletion_leaves_the_work_owed():
    """§31."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    _document(cur, uid)
    _walk_to_purging(cur, uid)
    token = _claim(cur, uid, "crasher")
    cur.execute("SELECT identity.start_lifecycle_phase(%s,%s,%s,'crasher')",
                (str(uid), PHASE, str(token)))
    assert _phase_status(cur, uid) == "RUNNING"
    assert _remaining(cur, uid) > 0

    _mark_earlier_phases_complete(cur, uid)
    _release(cur, uid)
    await _run_worker()
    assert _phase_status(cur, uid) == "COMPLETE"
    assert _remaining(cur, uid) == 0
    conn.close()


async def test_a_crash_after_all_deletion_still_reaches_complete():
    """§33 — content gone, phase row not updated."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    document_id, bucket, object_key = _document(cur, uid)
    _walk_to_purging(cur, uid)
    token = _claim(cur, uid, "crasher")
    cur.execute("SELECT identity.start_lifecycle_phase(%s,%s,%s,'crasher')",
                (str(uid), PHASE, str(token)))
    get_object_storage().delete(bucket, object_key)
    cur.execute("SELECT identity.finalize_document_purge(%s,%s,%s,'crasher')",
                (str(uid), document_id, str(token)))
    assert _remaining(cur, uid) == 0
    assert _phase_status(cur, uid) == "RUNNING"

    _mark_earlier_phases_complete(cur, uid)
    _release(cur, uid)
    await _run_worker()
    assert _phase_status(cur, uid) == "COMPLETE"
    conn.close()


async def test_a_stale_claim_token_cannot_reach_either_keyhole():
    """§34."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    document_id, _, _ = _document(cur, uid)
    _walk_to_purging(cur, uid)
    stale = _claim(cur, uid, "worker-a")
    _claim(cur, uid, "worker-b")

    for statement, params in (
        ("SELECT * FROM identity.list_account_documents_for_purge(%s,%s,'worker-a',10)",
         (str(uid), str(stale))),
        ("SELECT identity.finalize_document_purge(%s,%s,%s,'worker-a')",
         (str(uid), document_id, str(stale))),
    ):
        with pytest.raises(psycopg2.Error) as excinfo:
            cur.execute(statement, params)
        assert "no live claim" in str(excinfo.value), excinfo.value
    assert _remaining(cur, uid) > 0, "the stale worker mutated anyway"
    conn.close()


# ------------------------------------------------------------ isolation -----

async def test_one_accounts_documents_are_not_anothers():
    """§36 — rows, objects and extracted content all untouched for B."""
    a_uid = await _account("tenant-a")
    b_uid = await _account("tenant-b")
    conn = _owner()
    cur = conn.cursor()
    _document(cur, a_uid)
    b_doc, b_bucket, b_key = _document(cur, b_uid)
    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) "
                "FROM docs.document t WHERE user_id=%s", (str(b_uid),))
    b_digest = cur.fetchone()[0]

    _walk_to_purging(cur, a_uid)
    _mark_earlier_phases_complete(cur, a_uid)
    await _run_worker()

    assert _remaining(cur, a_uid) == 0
    assert _remaining(cur, b_uid) == 3, "B's outstanding document work changed"
    assert _object_exists(b_bucket, b_key), "B's binary was deleted"
    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) "
                "FROM docs.document t WHERE user_id=%s", (str(b_uid),))
    assert cur.fetchone()[0] == b_digest, "B's document rows changed"
    conn.close()


# ------------------------------------------------------------ boundaries ----

def test_only_the_privacy_worker_may_reach_the_document_keyholes():
    """§45 — PD-16, from genuine LOGINs."""
    probe, doc = uuid.uuid4(), uuid.uuid4()
    for login, allowed in (("onyx_test", False), ("onyx_privacy_test", True)):
        conn = psycopg2.connect(owner_dsn().replace("onyx_migrator", login))
        conn.autocommit = False
        try:
            cur = conn.cursor()
            cur.execute("SELECT session_user, current_setting('is_superuser')")
            principal, superuser = cur.fetchone()
            assert superuser == "off", f"{principal} is a superuser"
            try:
                cur.execute("SELECT identity.finalize_document_purge(%s,%s,%s,'probe')",
                            (str(probe), str(doc), str(uuid.uuid4())))
                refused = None
            except psycopg2.Error as exc:
                refused = str(exc)
            if allowed:
                assert refused is None or "no live claim" in refused, (
                    f"the privacy worker cannot reach its own keyhole: {refused}"
                )
            else:
                assert refused and "permission denied" in refused.lower(), (
                    f"{principal} can reach the document purge keyhole"
                )
        finally:
            conn.rollback()
            conn.close()


def test_no_role_gained_direct_document_mutation():
    """§20/§45 — the keyhole did not become a grant."""
    conn = _owner()
    try:
        cur = conn.cursor()
        for role in ("onyx_privacy_worker", "onyx_freshness_worker"):
            for table in ("docs.document", "docs.document_extraction",
                          "docs.extraction_field"):
                for privilege in ("DELETE", "UPDATE"):
                    cur.execute("SELECT has_table_privilege(%s,%s,%s)",
                                (role, table, privilege))
                    assert cur.fetchone()[0] is False, (
                        f"{role} gained direct {privilege} on {table}"
                    )
        cur.execute("SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles"
                    " WHERE rolname='onyx_privacy_worker'")
        assert cur.fetchone() == (False, False, False)
        cur.execute("SELECT has_table_privilege('onyx_app_rw',"
                    "'identity.user_account','DELETE')")
        assert cur.fetchone()[0] is False, "0060's guard was undone"
    finally:
        conn.close()


def test_terminal_deletion_is_still_blocked():
    """§53."""
    from tests.privacy.account_delete_registry import (
        assert_terminal_account_delete_ready,
        terminal_delete_blockers,
    )

    with pytest.raises(AssertionError):
        assert_terminal_account_delete_ready()
    assert len(terminal_delete_blockers()) == 19


# ------------------------------------ the retained-evidence boundary ---------

async def test_historical_replay_does_not_need_the_documents_it_came_from():
    """§6/§7/§37 — the claim that makes purging documents safe at all.

    The model is: uploaded document → extraction → candidate fact → user
    confirmation → frozen tax fact → sealed analysis. If the frozen system
    reproduces from the confirmed facts, the source binary is not a replay
    dependency and can go. That is a technical finding, not a legal one — §8's
    TECHNICALLY_DELETABLE, and the retention policy is a separate question.

    Proven by replaying a sealed chain AFTER the phase has removed every
    document the account had.
    """
    from app.services.ioe.domain.integrity import IntegrityStatus
    from app.services.ioe.replay.verification import IntegrityVerificationService
    from tests.security.test_sealed_history_after_purge import _historical_account

    uid, analysis_id, run_id, scenario_id = await _historical_account()
    before = await IntegrityVerificationService(uid).verify("optimization", run_id)
    assert before.status is IntegrityStatus.VERIFIED

    conn = _owner()
    cur = conn.cursor()
    document_id, bucket, object_key = _document(cur, uid)
    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) FROM ("
                " SELECT * FROM analysis.analysis_input_snapshot"
                "  WHERE analysis_id=%s) t", (str(analysis_id),))
    snapshot_before = cur.fetchone()[0]

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()

    assert _remaining(cur, uid) == 0
    assert not _object_exists(bucket, object_key)
    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) FROM ("
                " SELECT * FROM analysis.analysis_input_snapshot"
                "  WHERE analysis_id=%s) t", (str(analysis_id),))
    assert cur.fetchone()[0] == snapshot_before, (
        "the frozen snapshot changed when documents were purged"
    )
    conn.close()

    for kind, entity in (("optimization", run_id), ("scenario", scenario_id)):
        after = await IntegrityVerificationService(uid).verify(kind, entity)
        assert after.status is IntegrityStatus.VERIFIED, (
            f"{kind} replay stopped verifying once the documents were deleted "
            f"({after.status}/{after.reason_code}). The document surfaces were "
            "misclassified — replay evidently needs one of them."
        )


async def test_the_provenance_edge_survives_a_purged_document():
    """§38 — a confirmed figure must never look unsourced.

    Entry 11A §8 keeps `docs.document_link` on purpose: the document is gone,
    the fact the user confirmed from it remains, and the edge records that it
    HAD a source. The link points at a tombstone rather than at nothing.
    """
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    document_id, _, _ = _document(cur, uid, with_extraction=False)
    # A real edge: the link table requires the income or expense record it
    # sourced, which is the point — the edge exists to say a confirmed FIGURE
    # came from a document.
    cur.execute("INSERT INTO finance.income_source (user_id, tax_year, income_type_id,"
                " amount) SELECT %s, 2025, id, 52310 FROM ref.income_type LIMIT 1"
                " RETURNING id, tax_year", (str(uid),))
    income_id, income_year = cur.fetchone()
    cur.execute("INSERT INTO docs.document_link (document_id, income_source_id,"
                " income_tax_year) VALUES (%s,%s,%s) RETURNING id",
                (document_id, str(income_id), income_year))
    link_id = cur.fetchone()[0]

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()

    cur.execute("SELECT count(*) FROM docs.document_link WHERE id=%s", (str(link_id),))
    assert cur.fetchone()[0] == 1, "the provenance edge was destroyed"
    cur.execute("SELECT deleted_at IS NOT NULL FROM docs.document WHERE id=%s",
                (document_id,))
    assert cur.fetchone()[0] is True, "the edge points at a live document"
    conn.close()


# --------------------------------------- the whole pre-terminal lifecycle ----

async def test_every_pre_terminal_phase_runs_to_completion_in_order():
    """§56 — the first end-to-end proof, on one account with everything.

    Financial and profile data, a sealed analysis and optimization, a mixed set
    of scenarios, audit and authentication history, documents with extracted
    content. The real worker is run repeatedly — one phase per claim is the
    design — until it stops making progress, and every implemented phase must
    have reached COMPLETE with the account row still present.
    """
    from tests.security.test_sealed_history_after_purge import _historical_account

    uid, analysis_id, run_id, scenario_id = await _historical_account()
    conn = _owner()
    cur = conn.cursor()

    _, bucket, object_key = _document(cur, uid)
    cur.execute("INSERT INTO ioe.scenario (user_id, base_analysis_id, workflow_status,"
                " label) VALUES (%s,%s,'pending','a draft') ",
                (str(uid), str(analysis_id)))
    cur.execute("UPDATE ioe.scenario SET label='sealed label' WHERE id=%s",
                (str(scenario_id),))
    cur.execute("INSERT INTO identity.account_subject (user_id) VALUES (%s) "
                "ON CONFLICT DO NOTHING", (str(uid),))
    cur.execute("INSERT INTO identity.login_event (user_id, email_tried, event_type,"
                " ip_address) SELECT %s, email, 'success', '203.0.113.9'"
                "   FROM identity.user_account WHERE id=%s", (str(uid), str(uid)))

    _walk_to_purging(cur, uid)

    # One phase per claim, so drive it until it stops moving.
    for _ in range(8):
        _release(cur, uid)
        await _run_worker()

    phases = {}
    cur.execute("SELECT phase, status FROM identity.account_lifecycle_phase"
                " WHERE user_id=%s", (str(uid),))
    for phase, status in cur.fetchall():
        phases[phase] = status

    for phase in ("SOURCE_DATA", "DOCUMENTS", "SCENARIO_RETENTION",
                  "AUDIT_AUTH_DEIDENTIFICATION"):
        assert phases.get(phase) == "COMPLETE", (
            f"{phase} did not complete: {phases.get(phase)!r} (all: {phases})"
        )

    # Every phase's own guarantee, measured independently of its status row.
    cur.execute("SELECT identity.count_remaining_source_data(%s)", (str(uid),))
    assert cur.fetchone()[0] == 0
    assert _remaining(cur, uid) == 0
    assert not _object_exists(bucket, object_key)
    cur.execute("SELECT identity.count_remaining_scenario_privacy_work(%s)", (str(uid),))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT identity.count_attributable_audit_auth(%s)", (str(uid),))
    assert cur.fetchone()[0] == 0

    # And the account is still here. That is the point of "pre-terminal".
    cur.execute("SELECT count(*) FROM identity.user_account WHERE id=%s", (str(uid),))
    assert cur.fetchone()[0] == 1, "the account was removed; no phase may do that"
    cur.execute("SELECT state FROM identity.account_lifecycle WHERE user_id=%s",
                (str(uid),))
    assert cur.fetchone()[0] != "COMPLETE", (
        "the lifecycle reached COMPLETE while privacy surfaces are unclassified"
    )
    conn.close()

    # The sealed history still replays, after all four phases.
    from app.services.ioe.domain.integrity import IntegrityStatus
    from app.services.ioe.replay.verification import IntegrityVerificationService

    result = await IntegrityVerificationService(uid).verify("optimization", run_id)
    assert result.status is IntegrityStatus.VERIFIED, (
        f"sealed history stopped replaying after the full pre-terminal "
        f"lifecycle ({result.status}/{result.reason_code})"
    )
