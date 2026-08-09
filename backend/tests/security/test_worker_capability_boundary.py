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

#: A real application login: `onyx_test` is a member of `onyx_app_rw`, which is
#: the NOLOGIN base role every HTTP request runs as.
APP_DSN = "postgresql://onyx_test:test@/onyx_test?host=/var/run/postgresql&port=5432"


def _app_session():
    conn = psycopg2.connect(APP_DSN)
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
    would do it too. Asserted against the functions themselves."""
    for fn in ("identity.count_remaining_source_data",
               "identity.purge_source_data",
               "identity.start_lifecycle_phase",
               "identity.complete_lifecycle_phase",
               "identity.fail_lifecycle_phase"):
        with _app_session() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT has_function_privilege('onyx_app_rw', p.oid, 'EXECUTE') "
                "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                " WHERE n.nspname || '.' || p.proname = %s LIMIT 1", (fn,))
            row = cur.fetchone()
        assert row is not None, f"{fn} does not exist"
        assert row[0] is False, f"the application role can EXECUTE {fn}"


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
PRIVACY_DSN = (
    "postgresql://onyx_privacy_test:test@/onyx_test"
    "?host=/var/run/postgresql&port=5432"
)


def _privacy_session():
    conn = psycopg2.connect(PRIVACY_DSN)
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
