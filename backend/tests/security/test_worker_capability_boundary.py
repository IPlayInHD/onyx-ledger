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
