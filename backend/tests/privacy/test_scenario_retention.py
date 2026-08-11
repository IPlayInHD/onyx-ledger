"""SCENARIO_RETENTION: purge what never sealed, keep what did (Entry 11B6E).

Migration 0060 detached `ioe.scenario` from `identity.user_account` so a sealed
scenario would survive account removal. It cannot tell a sealed scenario from a
draft, so drafts survive too — 0060's analysis measured a `pending` scenario
carrying free text outliving its owner and recorded it as
BRANCH_CLEANUP_PENDING. This is that cleanup, and these are its proofs.

THE DISCRIMINATOR IS THE SEAL. `scenario_result_hash IS NOT NULL` means the row
sealed something replay can verify; NULL means it never produced evidence,
whatever its workflow status says. Keying on the seal rather than on
`workflow_status` is what makes a `failed` scenario purgeable and a `completed`
one retained.

WHAT THIS PHASE DOES NOT CLAIM. Retained rows keep their historical `user_id`,
and while `identity.user_account` still holds that account they remain
attributable to it. This is preparation for terminal removal, not
de-identification that has already happened.
"""

from __future__ import annotations

import uuid

import psycopg2
import pytest

from app.database.models import UserAccount
from app.database.session import unit_of_work
from tests.conftest import owner_dsn

PHASE = "SCENARIO_RETENTION"
EARLIER_PHASES = ("SOURCE_DATA",)


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


async def _account(tag: str = "r5") -> uuid.UUID:
    async with unit_of_work(user_id=None, actor_type="system") as s:
        account = UserAccount(
            email=f"{tag}-{uuid.uuid4().hex[:10]}@example.test", status="active"
        )
        s.add(account)
        await s.flush()
        return account.id


def _analysis(cur, uid) -> str:
    cur.execute("INSERT INTO analysis.analysis_run (user_id, tax_year, status, "
                "engine_version) VALUES (%s, 2025, 'completed', 'r5-probe') "
                "RETURNING id", (str(uid),))
    return str(cur.fetchone()[0])


def _digest() -> str:
    """A value shaped like the digests this schema stores.

    64 hex characters, not `"spec-abc"`. `test_the_opaque_classification_matches_
    the_database` asserts that every column classified as a digest holds one,
    across the whole database — so a fixture that writes a readable placeholder
    fails an unrelated security test whenever it happens to run first.
    """
    return uuid.uuid4().hex + uuid.uuid4().hex


def _scenario(cur, uid, analysis_id, *, sealed: bool, label=None, note=None,
              status="completed") -> str:
    """A scenario in one of the two states that matter.

    Sealed means `scenario_result_hash` is set — the seal, not the workflow
    status, is what the phase keys on, so the two are varied independently here.
    """
    cur.execute("""
        INSERT INTO ioe.scenario
               (user_id, base_analysis_id, workflow_status, label, note,
                scenario_spec_hash, scenario_result_hash, completed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (str(uid), analysis_id, status, label, note,
          _digest() if sealed else None,
          _digest() if sealed else None,
          "now()" if sealed else None))
    return str(cur.fetchone()[0])


def _remaining(cur, uid) -> int:
    cur.execute("SELECT identity.count_remaining_scenario_privacy_work(%s)", (str(uid),))
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
    """The real Celery task body, through the certified worker runtime.

    In a thread because `run_task` builds the loop a task invocation runs in,
    which it cannot do inside the one pytest-asyncio is already running.
    """
    import asyncio

    from workers.tasks.privacy import run_account_deletion_phases

    return await asyncio.to_thread(run_account_deletion_phases, worker_id=worker_id)


def _seal(cur, scenario_id) -> tuple:
    cur.execute("SELECT scenario_spec_hash, scenario_result_hash, user_id, "
                "       base_analysis_id, workflow_status "
                "  FROM ioe.scenario WHERE id=%s", (scenario_id,))
    return cur.fetchone()


# ------------------------------------------------------------ the state model -

async def test_the_seal_and_not_the_workflow_status_decides():
    """§4 — a `failed` scenario sealed nothing and goes; a sealed one stays.

    Written first because everything else depends on the discriminator being
    right. Keying on `workflow_status` would retain a failed run that produced
    no evidence and — worse — purge a sealed one whose status was later moved.
    """
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    failed_unsealed = _scenario(cur, uid, analysis, sealed=False, status="failed",
                                label="draft that failed")
    completed_sealed = _scenario(cur, uid, analysis, sealed=True, status="completed",
                                 label="kept")

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()

    cur.execute("SELECT count(*) FROM ioe.scenario WHERE id=%s", (failed_unsealed,))
    assert cur.fetchone()[0] == 0, "a scenario that sealed nothing was retained"
    cur.execute("SELECT count(*) FROM ioe.scenario WHERE id=%s", (completed_sealed,))
    assert cur.fetchone()[0] == 1, "a sealed scenario was purged"
    conn.close()


# -------------------------------------------------------------- the guard -----

async def test_the_guard_counts_both_obligations_independently():
    """§17 — an unsealed scenario alone blocks; free text alone blocks."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    assert _remaining(cur, uid) == 0, "a fresh account already owes scenario work"

    unsealed = _scenario(cur, uid, analysis, sealed=False)
    assert _remaining(cur, uid) == 1, "an unsealed scenario is not counted"
    cur.execute("DELETE FROM ioe.scenario WHERE id=%s", (unsealed,))

    sealed_with_text = _scenario(cur, uid, analysis, sealed=True, note="private note")
    assert _remaining(cur, uid) == 1, "free text on a retained scenario is not counted"

    cur.execute("UPDATE ioe.scenario SET note=NULL WHERE id=%s", (sealed_with_text,))
    assert _remaining(cur, uid) == 0, (
        "a sealed scenario with no free text is still counted as outstanding; "
        "retained evidence is not work owed"
    )
    conn.close()


async def test_completion_is_refused_while_either_obligation_remains():
    """§17 — the database refuses, not just the worker."""
    for label, make in (
        ("unsealed scenario", lambda cur, uid, a: _scenario(cur, uid, a, sealed=False)),
        ("retained free text",
         lambda cur, uid, a: _scenario(cur, uid, a, sealed=True, label="mine")),
    ):
        uid = await _account()
        conn = _owner()
        cur = conn.cursor()
        analysis = _analysis(cur, uid)
        make(cur, uid, analysis)
        _walk_to_purging(cur, uid)
        token = _claim(cur, uid)
        cur.execute("SELECT identity.start_lifecycle_phase(%s,%s,%s,'probe')",
                    (str(uid), PHASE, str(token)))

        with pytest.raises(psycopg2.Error) as excinfo:
            cur.execute("SELECT identity.complete_lifecycle_phase(%s,%s,%s,'probe')",
                        (str(uid), PHASE, str(token)))
        assert "SCENARIO_RETENTION cannot complete" in str(excinfo.value), (
            f"{label}: {excinfo.value}"
        )
        assert _phase_status(cur, uid) != "COMPLETE"
        conn.close()


# ------------------------------------------------------------- the phase ------

async def test_a_mixed_account_purges_drafts_and_sanitizes_retained():
    """§22 — the defining R5 proof, on one account holding both kinds."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    u1 = _scenario(cur, uid, analysis, sealed=False, label="draft one", status="pending")
    u2 = _scenario(cur, uid, analysis, sealed=False, note="draft two", status="running")
    v1 = _scenario(cur, uid, analysis, sealed=True, label="my divorce settlement")
    v2 = _scenario(cur, uid, analysis, sealed=True, note="second marriage planning")
    v1_seal, v2_seal = _seal(cur, v1), _seal(cur, v2)

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    assert _remaining(cur, uid) == 4

    await _run_worker()

    assert _phase_status(cur, uid) == "COMPLETE", _phase_status(cur, uid)
    assert _remaining(cur, uid) == 0

    for drafted in (u1, u2):
        cur.execute("SELECT count(*) FROM ioe.scenario WHERE id=%s", (drafted,))
        assert cur.fetchone()[0] == 0, "an unsealed scenario survived the phase"
    for retained, before in ((v1, v1_seal), (v2, v2_seal)):
        cur.execute("SELECT label, note FROM ioe.scenario WHERE id=%s", (retained,))
        label, note = cur.fetchone()
        assert label is None and note is None, (
            "free text survived on a retained scenario"
        )
        assert _seal(cur, retained) == before, (
            "a sealed column changed; only label and note may be written"
        )
    conn.close()


async def test_the_dependent_rows_of_a_purged_draft_go_with_it():
    """§8 — the branch, not just the header."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    draft = _scenario(cur, uid, analysis, sealed=False)
    cur.execute("INSERT INTO ioe.scenario_lever (scenario_id, lever_code, apply_order) "
                "VALUES (%s,'RRSP_CONTRIBUTION',1)", (draft,))
    cur.execute("INSERT INTO ioe.scenario_event (scenario_id, from_status, to_status,"
                " reason_code) VALUES (%s,'pending','running','PROBE')", (draft,))

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()

    for table in ("ioe.scenario_lever", "ioe.scenario_event"):
        cur.execute(f"SELECT count(*) FROM {table} WHERE scenario_id=%s", (draft,))
        assert cur.fetchone()[0] == 0, f"{table} outlived its purged scenario"
    conn.close()


async def test_the_retained_scenario_keeps_its_historical_owner():
    """§34 — `user_id` is deliberately untouched; that is the R1 architecture."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    kept = _scenario(cur, uid, analysis, sealed=True, label="named")

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()

    cur.execute("SELECT user_id FROM ioe.scenario WHERE id=%s", (kept,))
    assert str(cur.fetchone()[0]) == str(uid), (
        "the historical subject was rewritten; retained evidence keeps its "
        "owner until terminal removal"
    )
    conn.close()


async def test_running_the_phase_twice_changes_nothing_the_second_time():
    """§23 — idempotency."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    _scenario(cur, uid, analysis, sealed=False)
    kept = _scenario(cur, uid, analysis, sealed=True, label="x", note="y")

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()
    first = _seal(cur, kept)
    cur.execute("SELECT count(*) FROM ioe.scenario WHERE user_id=%s", (str(uid),))
    count_first = cur.fetchone()[0]

    _release(cur, uid)
    cur.execute("UPDATE identity.account_lifecycle_phase SET status='RUNNING',"
                " completed_at=NULL WHERE user_id=%s AND phase=%s", (str(uid), PHASE))
    await _run_worker()

    assert _seal(cur, kept) == first, "the second run touched retained evidence"
    cur.execute("SELECT count(*) FROM ioe.scenario WHERE user_id=%s", (str(uid),))
    assert cur.fetchone()[0] == count_first
    assert _phase_status(cur, uid) == "COMPLETE"
    conn.close()


# ------------------------------------------------------------- recovery ------

async def test_a_crash_before_any_mutation_leaves_work_owed():
    """§24 — claimed, then nothing."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    _scenario(cur, uid, analysis, sealed=False)
    _walk_to_purging(cur, uid)
    token = _claim(cur, uid, "crasher")
    cur.execute("SELECT identity.start_lifecycle_phase(%s,%s,%s,'crasher')",
                (str(uid), PHASE, str(token)))
    # ... crash here.
    assert _phase_status(cur, uid) == "RUNNING"
    assert _remaining(cur, uid) > 0

    _mark_earlier_phases_complete(cur, uid)
    _release(cur, uid)
    await _run_worker()
    assert _phase_status(cur, uid) == "COMPLETE"
    assert _remaining(cur, uid) == 0
    conn.close()


async def test_a_crash_between_the_purge_and_the_sanitize_still_converges():
    """§25 — the half-done case, built by doing exactly half.

    The drafts are gone and the free text is not yet cleared. The guard must
    still report work owed, the retry must finish it, and nothing deleted may
    come back.
    """
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    draft = _scenario(cur, uid, analysis, sealed=False)
    kept = _scenario(cur, uid, analysis, sealed=True, label="still here")
    _walk_to_purging(cur, uid)

    # Half the keyhole's work, by hand: the purge without the sanitize.
    cur.execute("SET LOCAL app.allow_evidence_purge='on'")
    cur.execute("DELETE FROM ioe.scenario WHERE id=%s", (draft,))
    assert _remaining(cur, uid) == 1, "the outstanding free text is not counted"

    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()

    assert _phase_status(cur, uid) == "COMPLETE"
    assert _remaining(cur, uid) == 0
    cur.execute("SELECT count(*) FROM ioe.scenario WHERE id=%s", (draft,))
    assert cur.fetchone()[0] == 0, "the retry recreated a purged scenario"
    cur.execute("SELECT label FROM ioe.scenario WHERE id=%s", (kept,))
    assert cur.fetchone()[0] is None
    conn.close()


async def test_a_crash_after_all_mutation_still_reaches_complete():
    """§26 — work done, completion not recorded."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    _scenario(cur, uid, analysis, sealed=False)
    kept = _scenario(cur, uid, analysis, sealed=True, label="x")
    _walk_to_purging(cur, uid)
    token = _claim(cur, uid, "crasher")
    cur.execute("SELECT identity.start_lifecycle_phase(%s,%s,%s,'crasher')",
                (str(uid), PHASE, str(token)))
    cur.execute("SELECT identity.prepare_scenario_retention(%s,%s,'crasher')",
                (str(uid), str(token)))
    # ... crash before completion.
    assert _phase_status(cur, uid) == "RUNNING"
    assert _remaining(cur, uid) == 0
    seal_before = _seal(cur, kept)

    _mark_earlier_phases_complete(cur, uid)
    _release(cur, uid)
    await _run_worker()

    assert _phase_status(cur, uid) == "COMPLETE"
    assert _seal(cur, kept) == seal_before, "the retry redid destructive work"
    conn.close()


async def test_a_stale_claim_token_cannot_run_the_keyhole():
    """§27 — worker B holds the subject; A's token is dead."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    _scenario(cur, uid, analysis, sealed=False)
    _walk_to_purging(cur, uid)
    stale = _claim(cur, uid, "worker-a")
    fresh = _claim(cur, uid, "worker-b")

    with pytest.raises(psycopg2.Error) as excinfo:
        cur.execute("SELECT identity.prepare_scenario_retention(%s,%s,'worker-a')",
                    (str(uid), str(stale)))
    assert "no live claim" in str(excinfo.value), excinfo.value
    assert _remaining(cur, uid) > 0, "the stale worker mutated anyway"

    cur.execute("SELECT identity.prepare_scenario_retention(%s,%s,'worker-b')",
                (str(uid), str(fresh)))
    assert _remaining(cur, uid) == 0
    conn.close()


# ------------------------------------------------------------ isolation ------

async def test_one_accounts_phase_does_not_touch_another():
    """§30 — no broad scenario cleanup."""
    a_uid = await _account("tenant-a")
    b_uid = await _account("tenant-b")
    conn = _owner()
    cur = conn.cursor()
    a_analysis, b_analysis = _analysis(cur, a_uid), _analysis(cur, b_uid)
    _scenario(cur, a_uid, a_analysis, sealed=False, label="a draft")
    _scenario(cur, a_uid, a_analysis, sealed=True, label="a kept")
    b_draft = _scenario(cur, b_uid, b_analysis, sealed=False, label="b draft")
    b_kept = _scenario(cur, b_uid, b_analysis, sealed=True, label="b kept", note="b note")
    b_before = (_seal(cur, b_draft), _seal(cur, b_kept))
    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) "
                "FROM ioe.scenario t WHERE user_id=%s", (str(b_uid),))
    b_digest = cur.fetchone()[0]

    _walk_to_purging(cur, a_uid)
    _mark_earlier_phases_complete(cur, a_uid)
    await _run_worker()

    assert _remaining(cur, a_uid) == 0
    assert _remaining(cur, b_uid) == 2, "B's outstanding work changed"
    assert (_seal(cur, b_draft), _seal(cur, b_kept)) == b_before
    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) "
                "FROM ioe.scenario t WHERE user_id=%s", (str(b_uid),))
    assert cur.fetchone()[0] == b_digest, "B's scenarios changed byte-wise"
    conn.close()


async def test_the_other_retained_roots_are_not_disturbed():
    """§33 — analysis, optimization and integrity history are not this phase's."""
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    _scenario(cur, uid, analysis, sealed=False)
    cur.execute("INSERT INTO ioe.optimization_run (user_id, analysis_id, tax_year) "
                "VALUES (%s,%s,2025) RETURNING id", (str(uid), analysis))
    run_id = str(cur.fetchone()[0])
    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) FROM ("
                " SELECT * FROM analysis.analysis_run WHERE user_id=%s) t", (str(uid),))
    analysis_before = cur.fetchone()[0]

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()

    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) FROM ("
                " SELECT * FROM analysis.analysis_run WHERE user_id=%s) t", (str(uid),))
    assert cur.fetchone()[0] == analysis_before, "the analysis run changed"
    cur.execute("SELECT count(*) FROM ioe.optimization_run WHERE id=%s", (run_id,))
    assert cur.fetchone()[0] == 1, "an optimization run was destroyed"
    conn.close()


async def test_the_phase_enqueues_no_freshness_work():
    """§31 — 0060's fan-out defect must not have a sequel here.

    Deleting drafts and clearing labels are privacy operations, not changes to
    anybody's tax position. Emitting freshness events for a deleting account
    would put work on the queue for a subject nobody can serve.
    """
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    _scenario(cur, uid, analysis, sealed=False)
    _scenario(cur, uid, analysis, sealed=True, label="kept")
    cur.execute("SELECT count(*) FROM ioe.freshness_outbox")
    before = cur.fetchone()[0]

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()

    cur.execute("SELECT count(*) FROM ioe.freshness_outbox WHERE user_id=%s", (str(uid),))
    assert cur.fetchone()[0] == 0, "the phase queued freshness work for a deleting account"
    cur.execute("SELECT count(*) FROM ioe.freshness_outbox")
    assert cur.fetchone()[0] == before, "the phase queued freshness work at all"
    conn.close()


# ------------------------------------------------------------ boundaries -----

def test_only_the_privacy_worker_may_run_the_scenario_keyhole():
    """§15 — PD-16, from genuine LOGINs."""
    probe = uuid.uuid4()
    for login, allowed in (("onyx_test", False), ("onyx_privacy_test", True)):
        conn = psycopg2.connect(owner_dsn().replace("onyx_migrator", login))
        conn.autocommit = False
        try:
            cur = conn.cursor()
            cur.execute("SELECT session_user, current_setting('is_superuser')")
            principal, superuser = cur.fetchone()
            assert superuser == "off", f"{principal} is a superuser"
            try:
                cur.execute("SELECT identity.prepare_scenario_retention(%s,%s,'probe')",
                            (str(probe), str(uuid.uuid4())))
                refused = None
            except psycopg2.Error as exc:
                refused = str(exc)
            if allowed:
                assert refused is None or "no live claim" in refused, (
                    f"the privacy worker cannot execute its own keyhole: {refused}"
                )
            else:
                assert refused and "permission denied" in refused.lower(), (
                    f"{principal} can execute the scenario retention keyhole"
                )
        finally:
            conn.rollback()
            conn.close()


def test_no_role_gained_direct_scenario_mutation():
    """§14 — the keyhole did not become a grant."""
    conn = _owner()
    try:
        cur = conn.cursor()
        for role in ("onyx_privacy_worker", "onyx_freshness_worker"):
            for privilege in ("DELETE", "UPDATE"):
                cur.execute("SELECT has_table_privilege(%s,'ioe.scenario',%s)",
                            (role, privilege))
                assert cur.fetchone()[0] is False, (
                    f"{role} gained direct {privilege} on ioe.scenario; the "
                    "mutation must stay inside the SECURITY DEFINER keyhole"
                )
        cur.execute("SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles"
                    " WHERE rolname='onyx_privacy_worker'")
        superuser, bypassrls, canlogin = cur.fetchone()
        assert not superuser and not bypassrls and not canlogin
    finally:
        conn.close()


def test_terminal_deletion_is_still_blocked():
    """§37 — finishing scenarios does not finish deletion."""
    from tests.privacy.account_delete_registry import (
        assert_terminal_account_delete_ready,
        terminal_delete_blockers,
    )

    with pytest.raises(AssertionError):
        assert_terminal_account_delete_ready()
    assert len(terminal_delete_blockers()) >= 55, (
        "blockers dropped further than this slice's evidence justifies"
    )


# ------------------------------------- production fixtures, not synthetic ----

async def _production_sealed_scenario():
    """A scenario sealed by the real services, with label and note set.

    The tests above build rows directly, which is right for exercising the
    state model — it is the only way to get a `failed` scenario that sealed
    nothing, or to vary the seal and the workflow status independently. But the
    claim that clearing free text cannot move a hash or break replay has to be
    made against evidence the engine actually produced.
    """
    from app.services.ioe.replay.verification import IntegrityVerificationService
    from tests.security.test_sealed_history_after_purge import _historical_account

    uid, analysis_id, run_id, scenario_id = await _historical_account()
    verified = await IntegrityVerificationService(uid).verify("scenario", scenario_id)
    conn = _owner()
    conn.cursor().execute(
        "UPDATE ioe.scenario SET label=%s, note=%s WHERE id=%s",
        ("my divorce settlement", "spouse earns less this year", str(scenario_id)),
    )
    conn.close()
    return uid, scenario_id, verified.status


async def test_clearing_free_text_moves_no_hash_and_breaks_no_replay():
    """§10 — the load-bearing claim, on engine-produced evidence.

    `ScenarioSpec` documents that label and note are excluded from
    `canonical_for_hash()` so that renaming a scenario cannot invalidate it.
    That is the reason this phase is allowed to write those two columns at all,
    so it is proven rather than cited.
    """
    from app.services.ioe.domain.integrity import IntegrityStatus
    from app.services.ioe.replay.verification import IntegrityVerificationService

    uid, scenario_id, status = await _production_sealed_scenario()
    assert status is IntegrityStatus.VERIFIED, (
        f"the fixture does not verify before sanitization ({status})"
    )

    conn = _owner()
    cur = conn.cursor()
    cur.execute("SELECT scenario_spec_hash, scenario_result_hash, manifest_hash, "
                "       baseline_input_snapshot_hash, baseline_result_hash, baseline_tax "
                "  FROM ioe.scenario WHERE id=%s", (str(scenario_id),))
    hashes_before = cur.fetchone()
    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) "
                "FROM ioe.scenario_result t WHERE scenario_id=%s", (str(scenario_id),))
    result_before = cur.fetchone()[0]

    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()

    cur.execute("SELECT label, note FROM ioe.scenario WHERE id=%s", (str(scenario_id),))
    assert cur.fetchone() == (None, None), "the free text was not cleared"
    cur.execute("SELECT scenario_spec_hash, scenario_result_hash, manifest_hash, "
                "       baseline_input_snapshot_hash, baseline_result_hash, baseline_tax "
                "  FROM ioe.scenario WHERE id=%s", (str(scenario_id),))
    assert cur.fetchone() == hashes_before, "a sealed hash moved when free text was cleared"
    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) "
                "FROM ioe.scenario_result t WHERE scenario_id=%s", (str(scenario_id),))
    assert cur.fetchone()[0] == result_before, "the sealed result changed"
    conn.close()

    # §32: and it still replays, with no dependence on the text that is gone.
    after = await IntegrityVerificationService(uid).verify("scenario", scenario_id)
    assert after.status is IntegrityStatus.VERIFIED, (
        f"scenario replay stopped verifying after sanitization "
        f"({after.status}/{after.reason_code})"
    )


async def test_a_new_scenario_cannot_be_created_once_deletion_has_started():
    """§28 — the cutoff, through the production service rather than raw SQL.

    Nothing may appear after the phase has run, and the guarantee comes from
    the lifecycle cutoff that already exists: an account past
    DELETION_REQUESTED cannot write. Proven rather than assumed, because "the
    admission control probably covers it" is exactly the assumption that lets a
    draft reappear after its owner is gone.
    """
    from app.core.exceptions import DomainError
    from app.services.ioe.scenario.service import ScenarioService

    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()
    assert _phase_status(cur, uid) == "COMPLETE"

    with pytest.raises((DomainError, Exception)) as excinfo:
        async with unit_of_work(user_id=uid, actor_type="user") as session:
            await ScenarioService(session, uid).create(
                base_analysis_id=uuid.UUID(analysis),
                levers=[], label="after the cutoff",
            )
    assert excinfo.value is not None

    cur.execute("SELECT count(*) FROM ioe.scenario WHERE user_id=%s", (str(uid),))
    assert cur.fetchone()[0] == 0, (
        "a scenario appeared for an account whose retention phase is complete"
    )
    assert _remaining(cur, uid) == 0
    conn.close()


async def test_the_cleared_free_text_cannot_be_written_back():
    """§29 — resurrection, through the ordinary writer.

    The refusal was expected to come from the existing account-lifecycle
    cutoff. It did not: that cutoff is raised by application code, so it does
    not bind a statement issued straight at the database, and the write
    succeeded. Asserting it rather than assuming it is what found the hole —
    the same shape 11B6D found on `identity.login_event`.
    """
    uid = await _account()
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    kept = _scenario(cur, uid, analysis, sealed=True, label="restore me")
    _walk_to_purging(cur, uid)
    _mark_earlier_phases_complete(cur, uid)
    await _run_worker()
    cur.execute("SELECT label FROM ioe.scenario WHERE id=%s", (kept,))
    assert cur.fetchone()[0] is None

    app = psycopg2.connect(owner_dsn().replace("onyx_migrator", "onyx_test"))
    app.autocommit = False
    try:
        acur = app.cursor()
        acur.execute("SELECT set_config('app.user_id', %s, true)", (str(uid),))
        with pytest.raises(psycopg2.Error) as excinfo:
            acur.execute("UPDATE ioe.scenario SET label='restored' WHERE id=%s", (kept,))
        assert "cannot be set while the account is being deleted" in str(excinfo.value), (
            f"refused for the wrong reason: {excinfo.value}"
        )
    finally:
        app.rollback()
        app.close()
    cur.execute("SELECT label FROM ioe.scenario WHERE id=%s", (kept,))
    assert cur.fetchone()[0] is None
    assert _remaining(cur, uid) == 0
    conn.close()


async def test_an_ordinary_account_can_still_rename_its_scenarios():
    """Guard on the guard: the invariant must not become "nobody may rename".

    It keys on a lifecycle row existing, so an account that has never asked to
    be deleted is entirely unaffected. Without this, the previous test would
    pass just as well against a trigger that refused every write.
    """
    uid = await _account("ordinary")
    conn = _owner()
    cur = conn.cursor()
    analysis = _analysis(cur, uid)
    scenario = _scenario(cur, uid, analysis, sealed=True, label="original")

    app = psycopg2.connect(owner_dsn().replace("onyx_migrator", "onyx_test"))
    app.autocommit = False
    try:
        acur = app.cursor()
        acur.execute("SELECT set_config('app.user_id', %s, true)", (str(uid),))
        acur.execute("UPDATE ioe.scenario SET label='renamed' WHERE id=%s", (scenario,))
        assert acur.rowcount == 1, "an ordinary account cannot rename its own scenario"
        app.commit()
    finally:
        app.close()

    cur.execute("SELECT label FROM ioe.scenario WHERE id=%s", (scenario,))
    assert cur.fetchone()[0] == "renamed"
    conn.close()
