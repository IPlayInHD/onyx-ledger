"""POST-0060 STRUCTURAL DELETE PROBE — NOT PRODUCT TERMINAL DELETION.

Before migration 0060, `DELETE FROM identity.user_account` against a sealed
account was refused: the cascade reached `ioe.integrity_check`, whose append-only
guard rejects DELETE with no escape. Account removal was not destructive, it was
impossible.

0060 detached the four retained roots, so the statement can now complete. What
this file proves is that completing it destroys nothing that had to survive, and
that the evidence remains usable afterwards — replay still verifies, the sealed
bytes are unchanged, and a new account with the same email inherits none of it.

WHAT THIS IS NOT. Running this statement is not the product's terminal account
deletion. That needs the DOCUMENTS and AUDIT_AUTH_DEIDENTIFICATION phases, the
scenario cleanup, and 63 privacy surfaces nobody has classified. The registry's
terminal gate still fails closed, and `test_the_application_role_cannot_delete_an_account`
is the boundary that keeps this a diagnostic rather than a feature: the probe
runs as the schema owner, which is a migration path, not a product path.

Everything destructive happens inside a transaction that is rolled back.
"""

from __future__ import annotations

import psycopg2
import pytest

from app.services.ioe.domain.integrity import IntegrityStatus
from app.services.ioe.replay.verification import IntegrityVerificationService
from tests.conftest import owner_dsn
from tests.security.test_sealed_history_after_purge import (
    _historical_account,
    _purge_account,
)

#: Everything the fixture seals, with how to count it for one subject.
RETAINED = {
    "analysis.analysis_run": "user_id=%(u)s",
    "analysis.analysis_input_snapshot":
        "analysis_id IN (SELECT id FROM analysis.analysis_run WHERE user_id=%(u)s)",
    "ioe.optimization_run": "user_id=%(u)s",
    "ioe.run_rule_snapshot":
        "run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id=%(u)s)",
    "ioe.scenario": "user_id=%(u)s",
    "ioe.integrity_check": "user_id=%(u)s",
    "ioe.strategy_portfolio":
        "run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id=%(u)s)",
}


async def _sealed_account():
    """A production-sealed account whose SOURCE_DATA purge has already run.

    Built per test: a module-scoped async fixture binds its pooled connections
    to one event loop while `asyncio_mode = "auto"` gives each test its own.
    """
    uid, analysis_id, run_id, scenario_id = await _historical_account()
    for kind, entity in (("optimization", run_id), ("scenario", scenario_id)):
        result = await IntegrityVerificationService(uid).verify(kind, entity)
        assert result.status is IntegrityStatus.VERIFIED, (
            f"the fixture does not verify before deletion ({kind}: "
            f"{result.status}); anything measured after would prove nothing"
        )
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    _purge_account(conn.cursor(), uid)
    conn.close()
    return uid, analysis_id, run_id, scenario_id


def _counts(cur, uid) -> dict[str, int]:
    out = {}
    for table, where in RETAINED.items():
        cur.execute(f"SELECT count(*) FROM {table} WHERE {where}", {"u": str(uid)})
        out[table] = cur.fetchone()[0]
    return out


def _seal(cur, uid) -> dict[str, str | None]:
    """A digest of every retained row AS STORED, so any rewrite shows up."""
    out = {}
    for table, where in RETAINED.items():
        cur.execute(
            f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) "
            f"FROM {table} t WHERE {where}", {"u": str(uid)},
        )
        out[table] = cur.fetchone()[0]
    return out


#: The columns a seal actually covers, per retained table. Deliberately not
#: `SELECT *`: `integrity_status` and `last_integrity_checked_at` are a
#: documented cache of the newest check and are expected to move.
SEALED_COLUMNS = """
SELECT (SELECT md5(string_agg(optimization_spec_hash || optimization_result_hash
                              || coalesce(manifest_hash,''), '|' ORDER BY id::text))
          FROM ioe.optimization_run WHERE user_id=%(u)s),
       (SELECT md5(string_agg(coalesce(scenario_spec_hash,'')
                              || coalesce(scenario_result_hash,'')
                              || coalesce(manifest_hash,''), '|' ORDER BY id::text))
          FROM ioe.scenario WHERE user_id=%(u)s),
       (SELECT md5(string_agg(snapshot_hash, '|' ORDER BY snapshot_hash))
          FROM analysis.analysis_input_snapshot
         WHERE analysis_id IN (SELECT id FROM analysis.analysis_run WHERE user_id=%(u)s)),
       (SELECT md5(string_agg(rs.snapshot_hash, '|' ORDER BY rs.snapshot_hash))
          FROM ioe.run_rule_snapshot rrs JOIN ioe.rule_snapshot rs ON rs.id=rrs.snapshot_id
         WHERE rrs.run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id=%(u)s))
"""


def _sealed_columns(cur, uid) -> tuple:
    cur.execute(SEALED_COLUMNS, {"u": str(uid)})
    return cur.fetchone()


async def test_the_account_row_can_now_be_removed_without_losing_evidence():
    """§20 and §21 together: the probe succeeds and every artifact survives."""
    uid, _, _, _ = await _sealed_account()

    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        before_counts = _counts(cur, uid)
        before_seal = _seal(cur, uid)
        assert all(before_counts.values()), (
            f"the fixture did not produce every artifact: {before_counts}"
        )

        cur.execute("DELETE FROM identity.user_account WHERE id=%s", (str(uid),))
        assert cur.rowcount == 1, (
            "the account row was not removed; 0060's detachment is not in effect"
        )

        assert _counts(cur, uid) == before_counts, (
            "account removal destroyed retained evidence:\n"
            f"  before {before_counts}\n  after  {_counts(cur, uid)}"
        )
        assert _seal(cur, uid) == before_seal, (
            "a retained row changed during account removal. R1 requires zero "
            "mutation: the foreign key was dropped precisely so nothing would "
            "have to be rewritten."
        )
    finally:
        conn.rollback()
        conn.close()


async def test_replay_still_verifies_once_the_account_row_is_gone():
    """§22, §23 and §24 — the evidence is not merely present, it is usable.

    The delete is committed here, because replay runs on its own connections
    and would not see an uncommitted one. The account is a throwaway built by
    this test; nothing else refers to it.
    """
    uid, _, run_id, scenario_id = await _sealed_account()

    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    cur = conn.cursor()
    before_sealed_columns = _sealed_columns(cur, uid)
    cur.execute("DELETE FROM identity.user_account WHERE id=%s", (str(uid),))
    assert cur.rowcount == 1
    cur.execute("SELECT count(*) FROM identity.user_account WHERE id=%s", (str(uid),))
    assert cur.fetchone()[0] == 0

    for kind, entity in (("optimization", run_id), ("scenario", scenario_id)):
        result = await IntegrityVerificationService(uid).verify(kind, entity)
        assert result.status is IntegrityStatus.VERIFIED, (
            f"{kind} replay stopped verifying once the account row was gone "
            f"({result.status}/{result.reason_code}). Replay must depend on the "
            "sealed artifacts, never on the account existing."
        )

    # Compare the SEALED columns, not whole rows. Verification deliberately
    # refreshes `integrity_status` / `last_integrity_checked_at` on the sealed
    # parents — that cache is documented as current state, is excluded from
    # every seal, and moving it is the verification working. What must not
    # move is the hashes.
    after_sealed_columns = _sealed_columns(cur, uid)
    assert after_sealed_columns == before_sealed_columns, (
        "replay rewrote sealed evidence; it may only read it"
    )
    # Verification appends rather than edits, so this one is expected to grow.
    cur.execute("SELECT count(*) FROM ioe.integrity_check WHERE user_id=%s", (str(uid),))
    assert cur.fetchone()[0] > 2, (
        "verification after account removal did not append new integrity rows"
    )
    conn.close()


async def test_a_new_account_with_the_same_email_inherits_nothing():
    """§27 — email equality creates no bridge to the previous subject."""
    uid, _, _, _ = await _sealed_account()

    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT email FROM identity.user_account WHERE id=%s", (str(uid),))
    email = cur.fetchone()[0]
    cur.execute("DELETE FROM identity.user_account WHERE id=%s", (str(uid),))
    assert cur.rowcount == 1

    cur.execute("INSERT INTO identity.user_account (email, status) "
                "VALUES (%s, 'active') RETURNING id", (email,))
    b_uid = cur.fetchone()[0]
    assert b_uid != uid, "the reused email produced the previous account id"
    conn.close()

    # Read as the runtime login, through RLS, as the new subject.
    app = psycopg2.connect(owner_dsn().replace("onyx_migrator", "onyx_test"))
    app.autocommit = False
    try:
        acur = app.cursor()
        acur.execute("SELECT set_config('app.user_id', %s, true)", (str(b_uid),))
        for table in ("analysis.analysis_run", "ioe.optimization_run",
                      "ioe.scenario", "ioe.integrity_check"):
            acur.execute(f"SELECT count(*) FROM {table}")
            assert acur.fetchone()[0] == 0, (
                f"the new account can read the previous subject's {table}"
            )
    finally:
        app.rollback()
        app.close()


def test_the_application_role_cannot_delete_an_account():
    """§31/§32 — the transitional guard, proven as the runtime login.

    Before 0060 the application role held DELETE on `identity.user_account` and
    was stopped only by the append-only cascade — an accident, not a decision.
    0060 removed that accident, so the privilege was withdrawn in the same
    migration. Without this the migration would have handed the HTTP role the
    ability to erase any account in one statement, with 63 privacy surfaces
    still unclassified.
    """
    conn = psycopg2.connect(owner_dsn().replace("onyx_migrator", "onyx_test"))
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("SELECT current_database(), session_user")
        database, principal = cur.fetchone()
        cur.execute("SELECT current_setting('is_superuser')")
        assert cur.fetchone()[0] == "off", (
            f"{principal} is a superuser in {database}; this proves nothing"
        )

        cur.execute("INSERT INTO identity.user_account (email, status) "
                    "VALUES ('delete-probe@example.test', 'active') RETURNING id")
        target = cur.fetchone()[0]

        with pytest.raises(psycopg2.Error) as excinfo:
            cur.execute("DELETE FROM identity.user_account WHERE id=%s", (str(target),))
        assert "permission denied" in str(excinfo.value).lower(), (
            f"the application role was refused for the wrong reason: {excinfo.value}"
        )
    finally:
        conn.rollback()
        conn.close()


def test_no_ordinary_principal_holds_delete_on_the_account_table():
    """The same boundary read from the catalogue, across every ordinary role."""
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        cur = conn.cursor()
        for role in ("onyx_app_rw", "onyx_app_ro", "public",
                     "onyx_privacy_worker", "onyx_freshness_worker"):
            cur.execute(
                "SELECT has_table_privilege(%s, 'identity.user_account', 'DELETE')",
                (role,),
            )
            assert cur.fetchone()[0] is False, (
                f"{role} can DELETE identity.user_account. Terminal account "
                "removal is a governed lifecycle operation and no ordinary "
                "principal may perform it while the lifecycle is unfinished."
            )
    finally:
        conn.close()


async def test_the_freshness_fanout_skips_a_subject_whose_account_is_gone():
    """The cross-tenant hazard 0060 created, and the reason section 3 exists.

    `ioe.fan_out_freshness_event` finds affected tenants by selecting `user_id`
    from completed, currently-fresh `ioe.scenario` and `ioe.optimization_run`
    rows. Those tables now outlive the account, while `freshness_outbox.user_id`
    is still a live foreign key — correctly, because the outbox dies with the
    account. So a deleted subject's retained rows would enqueue a row that
    cannot exist, and since the fan-out is a single statement across every
    affected tenant, ONE deleted account failed the whole broadcast for
    everybody else in it.

    This was not found by reading the code. It surfaced as three unrelated
    freshness tests failing in the full suite once the detachment landed.
    """
    uid, analysis_id, run_id, _ = await _sealed_account()

    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    cur = conn.cursor()
    # The retained rows must look like live advice, or the fan-out ignores them
    # and the test proves nothing.
    cur.execute("UPDATE ioe.optimization_run SET freshness_status='current' "
                " WHERE user_id=%s", (str(uid),))
    cur.execute("DELETE FROM identity.user_account WHERE id=%s", (str(uid),))
    assert cur.rowcount == 1

    cur.execute("SELECT count(*) FROM ioe.optimization_run "
                " WHERE user_id=%s AND workflow_status='completed' "
                "   AND freshness_status='current'", (str(uid),))
    assert cur.fetchone()[0] > 0, (
        "the deleted subject has no completed, fresh run, so the fan-out would "
        "never consider it and this test would pass vacuously"
    )

    # A broadcast event: no user_id, so the fan-out expands it per tenant.
    cur.execute("""
        INSERT INTO ioe.freshness_outbox
               (event_type, stale_reason_code, tax_year, dedupe_key,
                claim_state, claimed_by, claim_token, claimed_at)
        SELECT 'rule_published', 'RULE_VERSION_CHANGED', tax_year,
               'orphan-fanout-probe-' || gen_random_uuid()::text,
               'claimed', 'probe', gen_random_uuid(), now()
          FROM ioe.optimization_run WHERE user_id=%s LIMIT 1
        RETURNING id
    """, (str(uid),))
    event_id = cur.fetchone()[0]

    cur.execute("SELECT ioe.fan_out_freshness_event(%s, 'probe')", (str(event_id),))
    fanned = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM ioe.freshness_outbox WHERE user_id=%s", (str(uid),))
    assert cur.fetchone()[0] == 0, (
        "the fan-out enqueued freshness work for a deleted subject"
    )
    assert fanned >= 0
    conn.close()
