"""PD-16 — can the request path assume a privileged worker capability?

THE PROOF MUST USE A GENUINE LOGIN. PostgreSQL derives `SET ROLE` authority
from `session_user`, so a check run through an `onyx_migrator` session shows
that a superuser can assume anything and proves nothing about the application.
An earlier version of this investigation made exactly that mistake and reported
an escalation it had not demonstrated. Every assertion here connects directly as
a role whose `session_user` genuinely represents the runtime under test.
"""
from __future__ import annotations

import psycopg2
import pytest

from tests.conftest import app_dsn, freshness_dsn, owner_dsn, privacy_dsn

#: A real application login, a member of `onyx_app_rw` — the NOLOGIN base role
#: every HTTP request runs as.
#:
#: DERIVED FROM THE RUNTIME ENVIRONMENT, never named. These constants used to
#: hardcode the `onyx_test` database, and the security gate runs the suite
#: against `onyx_sec_proof`: every assertion in this file was therefore reading
#: a database the gate had not touched. Entry 11B5J's authoritative gate is how
#: that surfaced — it granted the lifecycle-worker keyholes to `onyx_app_rw` in
#: the proof database, verified the grant had taken effect, and the suite passed
#: anyway. A guard aimed at a fixed database certifies nothing about the
#: database under certification.


def _app_session():
    conn = psycopg2.connect(app_dsn())
    conn.autocommit = True
    return conn


def test_the_application_login_really_is_the_application_role():
    """Guard on the guard: if this login stopped being a member of
    `onyx_app_rw`, every assertion below would pass for the wrong reason."""
    with _app_session() as conn:
        cur = conn.cursor()
        cur.execute("SELECT session_user, "
                    "pg_has_role(session_user, 'onyx_app_rw', 'MEMBER')")
        session_user, is_app = cur.fetchone()
    assert session_user == "onyx_test"
    assert is_app, "the proof login is not an application-role member"


def test_the_application_cannot_assume_the_privacy_capability():
    """The one that must never regress.

    `GRANT onyx_privacy_worker TO onyx_app_rw` would make the whole Entry 11B5E
    worker suite green in a single line, and would hand every request path the
    authority to purge any account's financial data. This is the invariant that
    makes that shortcut fail loudly instead of silently.
    """
    with _app_session() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg2.Error) as caught:
            cur.execute("SET ROLE onyx_privacy_worker")
        assert "permission denied" in str(caught.value).lower(), caught.value


def test_the_application_cannot_invoke_the_privacy_keyholes_directly():
    """Membership is not the only way in — EXECUTE granted to the wrong role
    would do it too. Asserted against the functions themselves.

    THE LIFECYCLE WORKER TRIO WAS MISSING FROM THIS LIST UNTIL ENTRY 11B5J, and
    that omission is the whole reason the defect survived. PD-16 was a role
    MEMBERSHIP (`GRANT onyx_freshness_worker TO onyx_app_rw`) and its gate
    watched memberships; `claim_account_lifecycle`, `advance_account_lifecycle`
    and `fail_account_lifecycle` were DIRECT grants made under the pre-11B5E
    assumption that the application role also ran the privacy worker.

    Reproduced from a genuine application LOGIN before the fix: claiming
    returned another tenant's user id, state and claim token, and advancing with
    that token moved the victim's account to ACCESS_DISABLED. A list that omits
    a keyhole is not a weaker guard — it is no guard at all for that keyhole.
    """
    for fn in ("identity.count_remaining_source_data",
               "identity.purge_source_data",
               "identity.start_lifecycle_phase",
               "identity.complete_lifecycle_phase",
               "identity.fail_lifecycle_phase",
               "identity.claim_account_lifecycle",
               "identity.advance_account_lifecycle",
               "identity.fail_account_lifecycle"):
        with _app_session() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT has_function_privilege('onyx_app_rw', p.oid, 'EXECUTE') "
                "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                " WHERE n.nspname || '.' || p.proname = %s LIMIT 1", (fn,))
            row = cur.fetchone()
        assert row is not None, f"{fn} does not exist"
        assert row[0] is False, f"the application role can EXECUTE {fn}"


def test_the_application_login_cannot_drive_another_tenants_deletion():
    """The behavioural half of the ACL assertion above.

    An ACL check proves what the catalog says; this proves what the database
    does when the request-path role actually tries it. Both are kept because
    they fail differently: a future GRANT restores the capability silently,
    while a future change to the function's own guards would not show up in an
    ACL at all.
    """
    with _app_session() as conn:
        cur = conn.cursor()
        for statement, label in (
            ("SELECT * FROM identity.claim_account_lifecycle(10, 'probe')",
             "claim another tenant's lifecycle"),
            ("SELECT identity.advance_account_lifecycle("
             "'00000000-0000-0000-0000-000000000001'::uuid,"
             "'00000000-0000-0000-0000-000000000002'::uuid,"
             "'ACCESS_DISABLED', 'probe')", "disable another tenant's account"),
            ("SELECT identity.fail_account_lifecycle("
             "'00000000-0000-0000-0000-000000000001'::uuid,"
             "'00000000-0000-0000-0000-000000000002'::uuid,"
             "'WORKER_CLAIM_LOST', 'probe')", "fail another tenant's lifecycle"),
        ):
            try:
                cur.execute(statement)
                cur.fetchall()
            except psycopg2.errors.InsufficientPrivilege:
                conn.rollback()
            else:
                conn.rollback()
                raise AssertionError(
                    f"the application role could {label}; the lifecycle worker "
                    "capability is reachable from the request path")


def test_public_cannot_execute_the_privacy_keyholes():
    with _app_session() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT n.nspname || '.' || p.proname
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'identity'
               AND p.proname IN ('purge_source_data',
                                 'count_remaining_source_data',
                                 'start_lifecycle_phase',
                                 'complete_lifecycle_phase',
                                 'fail_lifecycle_phase')
               AND has_function_privilege('public', p.oid, 'EXECUTE')
        """)
        leaked = [r[0] for r in cur.fetchall()]
    assert not leaked, f"PUBLIC can execute privacy keyholes: {leaked}"


# ---------------------------------------------------------------------------
# The other half: the dedicated privacy runtime CAN do what the app cannot
# ---------------------------------------------------------------------------
#: A login that models the production privacy worker: member of the capability
#: role and of nothing else. Not a member of `onyx_app_rw`.



def _privacy_session():
    conn = psycopg2.connect(privacy_dsn())
    conn.autocommit = True
    return conn


def test_the_privacy_runtime_is_not_an_application_role():
    """Without this the positive proofs below could be passing because the
    privacy login happens to inherit ordinary application rights, which is
    exactly the topology this separation exists to prevent."""
    with _privacy_session() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT session_user, "
            "pg_has_role(session_user, 'onyx_privacy_worker', 'MEMBER'), "
            "pg_has_role(session_user, 'onyx_app_rw', 'MEMBER')")
        session_user, has_capability, has_app = cur.fetchone()
    assert session_user == "onyx_privacy_test"
    assert has_capability, "the privacy runtime lacks its own capability"
    assert not has_app, "the privacy runtime inherits application privileges"


def test_the_privacy_runtime_can_execute_every_keyhole_the_worker_needs():
    """The exact 11B5 contract, no wider. Asserted as effective privilege from
    a genuine login rather than read off the GRANT statements."""
    needed = ("count_remaining_source_data", "purge_source_data",
              "start_lifecycle_phase", "complete_lifecycle_phase",
              "fail_lifecycle_phase", "claim_account_lifecycle")
    with _privacy_session() as conn:
        cur = conn.cursor()
        for fn in needed:
            cur.execute(
                "SELECT has_function_privilege(session_user, p.oid, 'EXECUTE') "
                "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                " WHERE n.nspname = 'identity' AND p.proname = %s LIMIT 1", (fn,))
            row = cur.fetchone()
            assert row is not None, f"identity.{fn} does not exist"
            assert row[0] is True, f"the privacy runtime cannot execute {fn}"


def test_the_privacy_runtime_holds_no_dangerous_role_attributes():
    with _privacy_session() as conn:
        cur = conn.cursor()
        cur.execute("SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
                    "  FROM pg_roles WHERE rolname = session_user")
        superuser, bypassrls, createdb, createrole = cur.fetchone()
    assert not superuser, "the privacy runtime is a superuser"
    assert not bypassrls, "the privacy runtime bypasses row-level security"
    assert not createdb and not createrole


# ---------------------------------------------------------------------------
# PD-16 — the freshness capability, after remediation
# ---------------------------------------------------------------------------



def _freshness_session():
    conn = psycopg2.connect(freshness_dsn())
    conn.autocommit = True
    return conn


def test_the_application_cannot_assume_the_freshness_capability():
    """PD-16's defining closure evidence.

    BEFORE remediation this exact sequence, from this exact login, succeeded —
    and went on to claim a real event through `ioe.claim_freshness_events`. The
    grant that allowed it was `GRANT onyx_freshness_worker TO onyx_app_rw`.
    """
    with _app_session() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg2.Error) as caught:
            cur.execute("SET ROLE onyx_freshness_worker")
        assert "permission denied" in str(caught.value).lower(), caught.value


def test_no_capability_reaches_the_application_role_even_transitively():
    """`pg_has_role` walks the whole graph, so a reintroduction through some
    intermediate role fails here too — not only the literal original grant."""
    with _app_session() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_has_role('onyx_app_rw', 'onyx_freshness_worker',"
                    " 'USAGE'), pg_has_role('onyx_app_rw', "
                    "'onyx_privacy_worker', 'USAGE')")
        freshness, privacy = cur.fetchone()
    assert not freshness, "onyx_app_rw can use the freshness capability"
    assert not privacy, "onyx_app_rw can use the privacy capability"


def test_the_application_cannot_execute_the_freshness_keyholes():
    for fn in ("claim_freshness_events", "complete_freshness_event",
               "fail_freshness_event", "fan_out_freshness_event"):
        with _app_session() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT has_function_privilege('onyx_app_rw', p.oid, 'EXECUTE'),"
                "       has_function_privilege('public', p.oid, 'EXECUTE') "
                "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                " WHERE n.nspname = 'ioe' AND p.proname = %s LIMIT 1", (fn,))
            row = cur.fetchone()
        # Assert the function EXISTS before asserting denial, or a renamed
        # keyhole would make this pass by never being reached.
        assert row is not None, f"ioe.{fn} does not exist"
        assert row[0] is False, f"the application role can EXECUTE ioe.{fn}"
        assert row[1] is False, f"PUBLIC can EXECUTE ioe.{fn}"


def test_the_freshness_runtime_is_isolated_and_capable():
    with _freshness_session() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT session_user, "
            "pg_has_role(session_user, 'onyx_freshness_worker', 'MEMBER'), "
            "pg_has_role(session_user, 'onyx_app_rw', 'MEMBER'), "
            "pg_has_role(session_user, 'onyx_privacy_worker', 'MEMBER')")
        session_user, has_fresh, has_app, has_privacy = cur.fetchone()
        cur.execute("SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
                    "  FROM pg_roles WHERE rolname = session_user")
        superuser, bypassrls, createdb, createrole = cur.fetchone()
    assert session_user == "onyx_freshness_test"
    assert has_fresh, "the freshness runtime lacks its own capability"
    assert not has_app, "the freshness runtime inherits application privileges"
    assert not has_privacy, "the freshness runtime holds the privacy capability"
    assert not superuser and not bypassrls and not createdb and not createrole


def test_the_freshness_runtime_can_execute_its_keyholes():
    with _freshness_session() as conn:
        cur = conn.cursor()
        for fn in ("claim_freshness_events", "complete_freshness_event",
                   "fail_freshness_event", "fan_out_freshness_event"):
            cur.execute(
                "SELECT has_function_privilege(session_user, p.oid, 'EXECUTE') "
                "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                " WHERE n.nspname = 'ioe' AND p.proname = %s LIMIT 1", (fn,))
            row = cur.fetchone()
            assert row is not None, f"ioe.{fn} does not exist"
            assert row[0] is True, f"the freshness runtime cannot execute {fn}"


def test_the_freshness_runtime_has_no_direct_queue_table_access():
    """It does not need any, and must not acquire any. Reaching queue rows only
    through the narrow SECURITY DEFINER function is what makes the keyhole a
    keyhole; a table grant would turn it into a door."""
    with _freshness_session() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT has_table_privilege(session_user, 'ioe.freshness_outbox', "
            "'SELECT'), has_table_privilege(session_user, "
            "'ioe.freshness_outbox', 'UPDATE'), has_table_privilege("
            "session_user, 'ioe.freshness_outbox', 'DELETE')")
        select_, update_, delete_ = cur.fetchone()
    assert not select_ and not update_ and not delete_, (
        "the freshness runtime gained direct table access to the queue")


# ---------------------------------------------------------------------------
# Failure injection — prove the guards above would actually catch a regression
# ---------------------------------------------------------------------------
# A guard nobody has watched fail is a guard nobody knows works. Each case here
# recreates the unsafe topology, proves INDEPENDENTLY that it now exists (the
# guard-on-the-guard, without which a broken injection produces a vacuous
# pass), asserts the boundary is breached, and restores the secure state in a
# `finally` so a failing assertion cannot leave an escalation applied.
#
# The migrator is used ONLY to inject and revert catalog state. Every
# authorization CLAIM is re-proven from a fresh connection whose `session_user`
# is the genuine runtime login, because PostgreSQL derives SET ROLE authority
# from `session_user` — a migrator-mediated check would show that a superuser
# can assume anything and prove nothing about the application.



def _admin():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _app_can_assume(capability: str) -> bool:
    """Ask from a GENUINE application login, not through the migrator."""
    with _app_session() as conn:
        cur = conn.cursor()
        try:
            cur.execute(f"SET ROLE {capability}")
            return True
        except psycopg2.Error:
            return False


@pytest.mark.parametrize("capability", ["onyx_privacy_worker",
                                        "onyx_freshness_worker"])
def test_granting_a_worker_capability_to_the_app_role_is_detectable(capability):
    """The exact one-line regression this whole sub-entry exists to prevent.

    `GRANT onyx_privacy_worker TO onyx_app_rw` turns the SOURCE_DATA worker
    suite green in a single line and hands every request path account-purge
    authority. PD-16 is the same grant, already shipped, for freshness.
    """
    assert not _app_can_assume(capability), "topology was already unsafe"

    admin = _admin()
    try:
        admin.cursor().execute(f"GRANT {capability} TO onyx_app_rw")
        # Guard on the guard: prove the unsafe condition really exists now.
        assert _app_can_assume(capability), (
            f"the injection did not actually grant {capability}; a failure to "
            "detect it below would have been vacuous"
        )
        # And prove the reachability check sees it too, not only SET ROLE.
        with _app_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT pg_has_role('onyx_app_rw', %s, 'USAGE')",
                        (capability,))
            assert cur.fetchone()[0] is True
    finally:
        admin.cursor().execute(f"REVOKE {capability} FROM onyx_app_rw")
        admin.close()

    # Restored: the secure topology holds again.
    assert not _app_can_assume(capability), (
        f"{capability} survived the revert — the suite left an escalation in "
        "place"
    )


def test_granting_public_execute_on_a_keyhole_is_detectable():
    fn = "ioe.claim_freshness_events(integer, text)"
    admin = _admin()
    try:
        admin.cursor().execute(f"GRANT EXECUTE ON FUNCTION {fn} TO PUBLIC")
        with _app_session() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT has_function_privilege('public', p.oid, 'EXECUTE') "
                "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                " WHERE n.nspname = 'ioe' AND p.proname = "
                "'claim_freshness_events' LIMIT 1")
            assert cur.fetchone()[0] is True, "the injection did not take"
    finally:
        admin.cursor().execute(f"REVOKE EXECUTE ON FUNCTION {fn} FROM PUBLIC")
        admin.close()

    with _app_session() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT has_function_privilege('public', p.oid, 'EXECUTE') "
            "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            " WHERE n.nspname = 'ioe' AND p.proname = 'claim_freshness_events' "
            "LIMIT 1")
        assert cur.fetchone()[0] is False, "PUBLIC EXECUTE survived the revert"


def test_granting_the_freshness_worker_direct_table_access_is_detectable():
    """The tempting fix when a test hits `permission denied for table
    freshness_outbox`. It would work, and it would turn the keyhole into a
    door: reaching queue rows only through the narrow SECURITY DEFINER function
    is the entire control."""
    admin = _admin()
    try:
        admin.cursor().execute(
            "GRANT SELECT ON ioe.freshness_outbox TO onyx_freshness_worker")
        with _freshness_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT has_table_privilege(session_user, "
                        "'ioe.freshness_outbox', 'SELECT')")
            assert cur.fetchone()[0] is True, "the injection did not take"
    finally:
        admin.cursor().execute(
            "REVOKE SELECT ON ioe.freshness_outbox FROM onyx_freshness_worker")
        admin.close()

    with _freshness_session() as conn:
        cur = conn.cursor()
        cur.execute("SELECT has_table_privilege(session_user, "
                    "'ioe.freshness_outbox', 'SELECT')")
        assert cur.fetchone()[0] is False, "table access survived the revert"


def test_neither_worker_runtime_can_assume_the_other_capability():
    with _privacy_session() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg2.Error):
            cur.execute("SET ROLE onyx_freshness_worker")
    with _freshness_session() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg2.Error):
            cur.execute("SET ROLE onyx_privacy_worker")


def test_the_read_only_role_holds_no_worker_capability():
    """Read-only is not the same as safe: these keyholes MUTATE."""
    with _app_session() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_has_role('onyx_app_ro', 'onyx_privacy_worker', "
                    "'USAGE'), pg_has_role('onyx_app_ro', "
                    "'onyx_freshness_worker', 'USAGE')")
        privacy, freshness = cur.fetchone()
    assert not privacy and not freshness


def test_no_worker_runtime_owns_a_user_table():
    """Workers enter through keyholes, not ownership. An owner can ALTER the
    table's policies, which would make RLS advisory."""
    with _app_session() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT n.nspname || '.' || c.relname
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_roles r ON r.oid = c.relowner
             WHERE r.rolname IN ('onyx_privacy_worker', 'onyx_freshness_worker',
                                 'onyx_privacy_test', 'onyx_freshness_test')
               AND c.relkind IN ('r', 'p')
        """)
        owned = [row[0] for row in cur.fetchall()]
    assert not owned, f"a worker role owns tables: {owned}"


def test_a_missing_worker_dsn_fails_closed_rather_than_falling_back():
    """Exercised through the real session-construction path, not the docs.

    A fallback to `database_url` would mean the purge quietly runs as
    `onyx_app_rw` on any host where the operator forgot the setting —
    reintroducing PD-16 by configuration rather than by grant.
    """
    import asyncio

    from app.core.config import get_settings
    from app.database.privacy_session import (
        WorkerRuntimeUnavailable,
        get_worker_engine,
    )

    settings = get_settings()
    saved = (settings.privacy_database_url, settings.freshness_database_url)
    try:
        settings.privacy_database_url = None
        settings.freshness_database_url = None
        asyncio.run(_expect_unavailable(get_worker_engine,
                                        WorkerRuntimeUnavailable))
    finally:
        settings.privacy_database_url, settings.freshness_database_url = saved


async def _expect_unavailable(get_worker_engine, unavailable) -> None:
    for runtime in ("privacy", "freshness"):
        try:
            get_worker_engine(runtime)
        except unavailable as exc:
            # A closed code and no DSN: the message reaches logs and task
            # failure records, and a connection string names a host, a
            # database and a role.
            assert "postgresql" not in str(exc).lower()
            assert "@" not in str(exc)
        else:
            raise AssertionError(
                f"{runtime} runtime built an engine with no DSN configured — "
                "it fell back instead of failing closed")


def test_every_capability_session_is_on_the_database_under_test():
    """GUARD ON THE GUARD, for the defect that hid all the others.

    Every assertion in this file is about privileges in a specific database.
    If any of these logins connects somewhere else, the assertions still pass —
    they just stop being about the system under test. That is not theoretical:
    these DSNs named `onyx_test` outright, and under
    `scripts/prove_security_gate.sh`, which runs the suite against
    `onyx_sec_proof`, this whole file was reading a database the gate never
    injected into. The gate granted `onyx_app_rw` EXECUTE on the lifecycle
    keyholes, proved the grant had landed, and the suite reported green.

    So: all four sessions must report the same `current_database()`, and it
    must be the one the harness provisioned.
    """
    seen = {}
    for label, session in (("app", _app_session),
                           ("privacy", _privacy_session),
                           ("freshness", _freshness_session),
                           ("owner", _admin)):
        with session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT current_database(), session_user")
            seen[label] = cur.fetchone()

    databases = {label: row[0] for label, row in seen.items()}
    assert len(set(databases.values())) == 1, (
        "these logins are not all on the same database, so the privilege "
        f"assertions in this file are not all about one system: {databases}")

    import os
    from urllib.parse import urlparse

    expected = (urlparse(os.environ.get("ONYX_DATABASE_URL", "")).path
                or "/onyx_test").lstrip("/").split("?")[0]
    actual = next(iter(databases.values()))
    assert actual == expected, (
        f"this file is asserting privileges in {actual!r} while the harness "
        f"provisioned {expected!r}; the guards are aimed at the wrong database")
