"""PD-9 — the deletion record must outlive the account it records (Entry 11B3).

THE DEFECT, as Entry 11A recorded it (§23, gap register):

    `audit.data_deletion_request` FK is `CASCADE`, so the deletion record dies
    with the account it must outlive.

WHAT THE SCHEMA ACTUALLY SAYS
That wording names a table Entry 11A itself described as "EXISTS AND IS UNUSED",
and it still holds zero rows. Entry 11B2 then built the deletion lifecycle
somewhere else — `identity.account_lifecycle` — and gave it the SAME defect in a
stronger form: the account id is not merely a cascading foreign key there, it is
the PRIMARY KEY, so the ledger cannot exist without the account row.

So PD-9's defect is real and its address in the documentation is stale. The
tests below are written against the table that actually holds deletion records.

WHY IT MATTERS
The privacy system must not erase its own evidence of deletion. Two consequences
follow from losing it:

  * a purge phase that removes the account row destroys the record saying a
    purge was owed, so a half-finished deletion becomes indistinguishable from
    an account that never asked;
  * a backup taken before the deletion, restored afterwards, has no surviving
    ledger to replay — the deleted account silently reappears, which Entry 11A
    called the failure that "undoes every guarantee in this document in one
    operation".

`identity.account_lifecycle_event` already survives — Entry 11B2 deliberately
gave it no foreign key for exactly this reason. The aggregate did not follow.
"""
from __future__ import annotations

import contextlib
import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn

LEDGER = "identity.account_lifecycle"


@contextlib.contextmanager
def owner_cursor():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        yield conn.cursor()
    finally:
        conn.close()


def _account(cur) -> uuid.UUID:
    account = uuid.uuid4()
    cur.execute(
        "INSERT INTO identity.user_account (id, email, status) "
        "VALUES (%s, %s, 'active')",
        (str(account), f"pd9_{uuid.uuid4().hex[:10]}@example.com"))
    return account


def _request_deletion(cur, account: uuid.UUID) -> None:
    cur.execute(
        "INSERT INTO identity.account_lifecycle (user_id, state) "
        "VALUES (%s, 'DELETION_REQUESTED')", (str(account),))


def _ledger_rows(cur, account: uuid.UUID) -> int:
    cur.execute(f"SELECT count(*) FROM {LEDGER} WHERE user_id = %s",  # noqa: S608
                (str(account),))
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# the defining invariant (§18)
# ---------------------------------------------------------------------------
def test_the_deletion_ledger_survives_removal_of_the_account_row():
    """THE PD-9 invariant, asserted against the database directly.

    A future purge phase ends by removing or de-identifying the account row.
    When it does, the record proving a deletion was requested — and how far it
    got — has to still be there.
    """
    with owner_cursor() as cur:
        account = _account(cur)
        _request_deletion(cur, account)
        assert _ledger_rows(cur, account) == 1, "the fixture never created one"

        cur.execute("DELETE FROM identity.user_account WHERE id = %s",
                    (str(account),))

        assert _ledger_rows(cur, account) == 1, (
            "the deletion ledger was destroyed by removing the account it "
            "records. A purge that finishes by deleting the account row would "
            "erase the evidence that the purge was ever owed, and a restored "
            "backup would have nothing to replay."
        )


def test_the_lifecycle_event_log_also_survives():
    """Entry 11B2 already got this right for the append-only event log, which
    carries no foreign key on purpose. Asserted so the aggregate and its history
    cannot drift apart again."""
    with owner_cursor() as cur:
        account = _account(cur)
        _request_deletion(cur, account)
        cur.execute(
            "INSERT INTO identity.account_lifecycle_event "
            "(user_id, event_code, to_state) "
            "VALUES (%s, 'DELETION_REQUESTED', 'DELETION_REQUESTED')",
            (str(account),))

        cur.execute("DELETE FROM identity.user_account WHERE id = %s",
                    (str(account),))

        cur.execute("SELECT count(*) FROM identity.account_lifecycle_event "
                    " WHERE user_id = %s", (str(account),))
        assert cur.fetchone()[0] == 1, "the event history died with the account"


def test_no_foreign_key_makes_the_ledger_depend_on_the_account_row():
    """The structural form of the same statement.

    Stated separately because a future migration could reintroduce the cascade
    without any test noticing until a purge phase ran — and by then the evidence
    is what has been lost.
    """
    with owner_cursor() as cur:
        cur.execute("""
            SELECT con.conname, pg_get_constraintdef(con.oid)
              FROM pg_constraint con
              JOIN pg_class c ON c.oid = con.conrelid
              JOIN pg_class f ON f.oid = con.confrelid
             WHERE con.contype = 'f'
               AND c.relname = 'account_lifecycle'
               AND f.relname = 'user_account'
        """)
        cascading = [
            f"{name}: {definition}"
            for name, definition in cur.fetchall()
            if "ON DELETE CASCADE" in definition
        ]
    assert not cascading, (
        f"{cascading} — the deletion ledger cascades with the account again"
    )


# ---------------------------------------------------------------------------
# the record PD-9 literally names (§1)
# ---------------------------------------------------------------------------
def test_the_unused_request_tables_cannot_become_the_same_trap():
    """`audit.data_deletion_request` is what PD-9's wording names.

    Entry 11A called it "EXISTS AND IS UNUSED" and it still holds no rows —
    Entry 11B2 built the real ledger elsewhere. It is fixed anyway rather than
    left armed: it is the table a future author would reach for when asked
    where deletion requests live, and finding a cascading FK there is how this
    defect gets rebuilt.
    """
    with owner_cursor() as cur:
        cur.execute("""
            SELECT c.relname, pg_get_constraintdef(con.oid)
              FROM pg_constraint con
              JOIN pg_class c ON c.oid = con.conrelid
              JOIN pg_class f ON f.oid = con.confrelid
             WHERE con.contype = 'f'
               AND c.relname = 'data_deletion_request'
               AND f.relname = 'user_account'
        """)
        rows = cur.fetchall()
    cascading = [f"{t}: {d}" for t, d in rows if "ON DELETE CASCADE" in d]
    assert not cascading, (
        f"{cascading} — the table PD-9 names still destroys its own deletion "
        "records when the account goes"
    )


# ---------------------------------------------------------------------------
# what must NOT change (§19)
# ---------------------------------------------------------------------------
def test_removing_the_account_leaves_the_ledger_state_untouched():
    """§19 — survival is not enough; the record has to be unchanged.

    A cutoff that moved, a state that reset or a revision that rolled back
    would each be a different way of losing the evidence while appearing to
    keep it.
    """
    columns = ("state", "requested_at", "state_changed_at", "attempts",
               "revision", "claimed_by", "claim_token", "completed_at")
    with owner_cursor() as cur:
        account = _account(cur)
        _request_deletion(cur, account)
        cur.execute(
            f"SELECT {', '.join(columns)} FROM {LEDGER} WHERE user_id = %s",  # noqa: S608
            (str(account),))
        before = cur.fetchone()

        cur.execute("DELETE FROM identity.user_account WHERE id = %s",
                    (str(account),))

        cur.execute(
            f"SELECT {', '.join(columns)} FROM {LEDGER} WHERE user_id = %s",  # noqa: S608
            (str(account),))
        after = cur.fetchone()

    assert after is not None, "the ledger row is gone"
    for name, was, now in zip(columns, before, after, strict=True):
        assert was == now, (
            f"removing the account changed {name}: {was!r} -> {now!r}"
        )


# ---------------------------------------------------------------------------
# no resurrection linkage (§20)
# ---------------------------------------------------------------------------
def test_a_new_account_with_the_same_email_does_not_inherit_the_old_ledger():
    """§20 — an Entry 11A privacy invariant, restated where it now bites.

    The durable subject identifier is the account's internal UUID, which is
    never reused. Registering the same address again produces a new identity,
    so it cannot pick up the previous subject's deletion record — and a ledger
    keyed on the email address would have done exactly that.
    """
    email = f"pd9_reuse_{uuid.uuid4().hex[:10]}@example.com"
    with owner_cursor() as cur:
        first = uuid.uuid4()
        cur.execute("INSERT INTO identity.user_account (id, email, status) "
                    "VALUES (%s, %s, 'active')", (str(first), email))
        _request_deletion(cur, first)
        cur.execute("DELETE FROM identity.user_account WHERE id = %s",
                    (str(first),))

        # The address is free again, and the new account is a new subject.
        second = uuid.uuid4()
        cur.execute("INSERT INTO identity.user_account (id, email, status) "
                    "VALUES (%s, %s, 'active')", (str(second), email))

        assert first != second
        assert _ledger_rows(cur, first) == 1, "the old subject's record is gone"
        assert _ledger_rows(cur, second) == 0, (
            "the new account inherited the previous subject's deletion ledger"
        )

        # And the new account can request its own deletion, independently.
        _request_deletion(cur, second)
        assert _ledger_rows(cur, second) == 1
        cur.execute(f"SELECT count(*) FROM {LEDGER} "  # noqa: S608
                    " WHERE user_id IN (%s, %s)", (str(first), str(second)))
        assert cur.fetchone()[0] == 2, "two subjects, two ledgers"


def test_two_accounts_get_two_distinct_durable_subjects():
    """§21 — distinctness and stability of the subject identifier.

    It is the account's own UUID rather than anything derived, so this is
    really a check that nothing collapses two subjects into one ledger row.
    """
    with owner_cursor() as cur:
        a, b = _account(cur), _account(cur)
        _request_deletion(cur, a)
        _request_deletion(cur, b)
        assert a != b

        cur.execute(f"SELECT user_id FROM {LEDGER} "  # noqa: S608
                    " WHERE user_id IN (%s, %s)", (str(a), str(b)))
        subjects = {str(r[0]) for r in cur.fetchall()}
        assert subjects == {str(a), str(b)}

        # Stable: the identifier does not change as the lifecycle advances.
        cur.execute(f"UPDATE {LEDGER} SET state = 'ACCESS_DISABLED' "  # noqa: S608
                    " WHERE user_id = %s", (str(a),))
        assert _ledger_rows(cur, a) == 1


# ---------------------------------------------------------------------------
# integrity that still makes sense (§6)
# ---------------------------------------------------------------------------
def test_a_ledger_row_cannot_be_created_for_an_account_that_never_existed():
    """§6 — the ledger must survive account removal without becoming a table
    anyone can write arbitrary rows into.

    The foreign key used to provide this and cannot any more, because it also
    provided the cascade. The check moves to the only moment it is meaningful:
    creation.
    """
    with owner_cursor() as cur, pytest.raises(psycopg2.Error) as caught:
        cur.execute(
            "INSERT INTO identity.account_lifecycle (user_id, state) "
            "VALUES (%s, 'DELETION_REQUESTED')", (str(uuid.uuid4()),))
    assert "account" in str(caught.value).lower(), caught.value


# ---------------------------------------------------------------------------
# the worker, after the account is gone (§26, §27)
# ---------------------------------------------------------------------------
def test_the_worker_can_claim_a_lifecycle_whose_account_no_longer_exists():
    """§26 — the critical one.

    A purge phase ends by removing the account row, and the phases after it
    still have work to do. If any authoritative lifecycle query joined
    `identity.user_account`, every one of them would stop seeing the account it
    is supposed to be finishing.
    """
    with owner_cursor() as cur:
        account = _account(cur)
        _request_deletion(cur, account)
        cur.execute("DELETE FROM identity.user_account WHERE id = %s",
                    (str(account),))

        cur.execute(
            "SELECT out_user_id, out_state, out_requested_at, out_claim_token "
            "  FROM identity.claim_account_lifecycle(50, 'pd9-worker')")
        claimed = {str(r[0]): r for r in cur.fetchall()}

    assert str(account) in claimed, (
        "the worker cannot claim a lifecycle whose account has been removed, "
        "so no phase after account removal could ever run"
    )
    _, state, requested_at, token = claimed[str(account)]
    assert state == "DELETION_REQUESTED"
    assert requested_at is not None, "the cutoff did not survive"
    assert token is not None, "no claim token was issued"


def test_the_worker_can_advance_and_fail_a_subject_with_no_account():
    """§27 — claim semantics keep working on the ledger's own identity."""
    with owner_cursor() as cur:
        account = _account(cur)
        _request_deletion(cur, account)
        cur.execute("DELETE FROM identity.user_account WHERE id = %s",
                    (str(account),))

        cur.execute("SELECT out_user_id, out_claim_token "
                    "  FROM identity.claim_account_lifecycle(50, 'pd9-worker')")
        token = next(t for u, t in cur.fetchall() if str(u) == str(account))

        cur.execute("SELECT identity.advance_account_lifecycle("
                    "  %s, %s, 'ACCESS_DISABLED', 'pd9-worker')",
                    (str(account), str(token)))
        assert cur.fetchone()[0] is True, "the phase could not advance"

        cur.execute(f"SELECT state FROM {LEDGER} WHERE user_id = %s",  # noqa: S608
                    (str(account),))
        assert cur.fetchone()[0] == "ACCESS_DISABLED"


def test_the_deletion_state_lookup_still_answers_for_a_removed_account():
    """`identity.account_deletion_state` is how login, admission and the worker
    preflight ask whether a subject is being deleted. It must keep answering
    after the account row is gone, or a restored backup's account would be
    treated as active."""
    with owner_cursor() as cur:
        account = _account(cur)
        _request_deletion(cur, account)
        cur.execute("DELETE FROM identity.user_account WHERE id = %s",
                    (str(account),))
        cur.execute("SELECT identity.account_deletion_state(%s)", (str(account),))
        assert cur.fetchone()[0] == "DELETION_REQUESTED"


# ---------------------------------------------------------------------------
# runtime privileges on the surviving record (§16, §17)
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _as_role(role: str, user_id: uuid.UUID | None = None):
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        if user_id is not None:
            cur.execute("SELECT set_config('app.user_id', %s, true)",
                        (str(user_id),))
        cur.execute(f"SET ROLE {role}")
        yield cur, conn
    finally:
        conn.rollback()
        conn.close()


def test_the_runtime_role_cannot_delete_the_ledger():
    """§17 — central. The record survives the account; it must also survive the
    application.

    Asserted as a PostgreSQL refusal rather than a zero-row result: RLS
    filtering a DELETE and the privilege being absent are different controls,
    and only one of them survives someone adding a policy.
    """
    with owner_cursor() as cur:
        account = _account(cur)
        _request_deletion(cur, account)

    # TWO independent refusals, and the stronger one is the grant. Entry 11B2
    # revoked DELETE from the runtime role entirely, so PostgreSQL refuses
    # before the no-delete trigger is ever reached — and the trigger is still
    # there for anyone who does hold the privilege.
    with owner_cursor() as cur:
        cur.execute("SELECT has_table_privilege('onyx_app_rw', %s, 'DELETE')",
                    (LEDGER,))
        assert cur.fetchone()[0] is False, (
            "the runtime role holds DELETE on the deletion ledger; the record "
            "that survives the account must also survive the application"
        )

    with _as_role("onyx_app_rw", account) as (cur, _):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as caught:
            cur.execute(f"DELETE FROM {LEDGER} WHERE user_id = %s",  # noqa: S608
                        (str(account),))
        assert "permission denied" in str(caught.value).lower(), caught.value

    # And the trigger refuses even a role that does hold the privilege.
    with owner_cursor() as cur, pytest.raises(psycopg2.Error) as caught:
        cur.execute(f"DELETE FROM {LEDGER} WHERE user_id = %s",  # noqa: S608
                    (str(account),))
    assert "not deletable" in str(caught.value).lower(), caught.value

    with owner_cursor() as cur:
        assert _ledger_rows(cur, account) == 1


def test_the_runtime_role_cannot_forge_progress_or_move_the_subject():
    """§16 — the ordinary role may open a lifecycle and read its own status. It
    may not advance one, complete one, or repoint it at another subject."""
    with owner_cursor() as cur:
        account = _account(cur)
        _request_deletion(cur, account)
        other = _account(cur)

    with _as_role("onyx_app_rw", account) as (cur, conn):
        for statement, params in (
            (f"UPDATE {LEDGER} SET state = 'ACCESS_DISABLED' "  # noqa: S608
             " WHERE user_id = %s", (str(account),)),
            (f"UPDATE {LEDGER} SET state = 'COMPLETE', completed_at = now() "  # noqa: S608
             " WHERE user_id = %s", (str(account),)),
            (f"UPDATE {LEDGER} SET user_id = %s WHERE user_id = %s",  # noqa: S608
             (str(other), str(account))),
        ):
            with pytest.raises(psycopg2.Error):
                cur.execute(statement, params)
            conn.rollback()
            cur.execute("SET ROLE onyx_app_rw")

    with owner_cursor() as cur:
        cur.execute(f"SELECT state FROM {LEDGER} WHERE user_id = %s",  # noqa: S608
                    (str(account),))
        assert cur.fetchone()[0] == "DELETION_REQUESTED", "progress was forged"


def test_the_read_only_role_and_public_cannot_reach_the_ledger():
    """§16 — the read-only role may not mutate, and PUBLIC has nothing."""
    with owner_cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege('onyx_app_ro', %s, 'SELECT'), "
            "       has_table_privilege('onyx_app_ro', %s, 'INSERT'), "
            "       has_table_privilege('onyx_app_ro', %s, 'UPDATE'), "
            "       has_table_privilege('onyx_app_ro', %s, 'DELETE'), "
            "       has_table_privilege('public', %s, 'SELECT'), "
            "       has_table_privilege('public', %s, 'INSERT')",
            (LEDGER,) * 6)
        ro_s, ro_i, ro_u, ro_d, pub_s, pub_i = cur.fetchone()

    assert ro_s is True, "the read-only role lost SELECT"
    assert (ro_i, ro_u, ro_d) == (False, False, False), (
        "the read-only role can mutate the deletion ledger"
    )
    assert (pub_s, pub_i) == (False, False), "PUBLIC can reach the ledger"


def test_one_tenant_cannot_see_anothers_deletion_ledger():
    """Cross-tenant, unchanged by PD-9. The policies stay keyed on
    `app.user_id`; once the account is gone no session can present that id, so
    the row becomes invisible to every ordinary role by construction."""
    with owner_cursor() as cur:
        a, b = _account(cur), _account(cur)
        _request_deletion(cur, a)
        _request_deletion(cur, b)

    with _as_role("onyx_app_rw", a) as (cur, _):
        cur.execute(f"SELECT count(*) FROM {LEDGER} WHERE user_id = %s",  # noqa: S608
                    (str(b),))
        assert cur.fetchone()[0] == 0, "a tenant read another's deletion ledger"
        cur.execute(f"SELECT count(*) FROM {LEDGER} WHERE user_id = %s",  # noqa: S608
                    (str(a),))
        assert cur.fetchone()[0] == 1, "a tenant cannot read its own status"


def test_a_removed_accounts_ledger_is_invisible_to_ordinary_roles():
    """§15 — after the subject's account is gone, no ordinary session can ever
    present that `app.user_id` again. The record is reachable only through the
    privileged worker interface, which is the intended end state."""
    with owner_cursor() as cur:
        account = _account(cur)
        _request_deletion(cur, account)
        cur.execute("DELETE FROM identity.user_account WHERE id = %s",
                    (str(account),))

    # Even naming the subject explicitly, an ordinary role with a DIFFERENT
    # tenant context sees nothing.
    with owner_cursor() as cur:
        stranger = _account(cur)
    with _as_role("onyx_app_rw", stranger) as (cur, _):
        cur.execute(f"SELECT count(*) FROM {LEDGER} WHERE user_id = %s",  # noqa: S608
                    (str(account),))
        assert cur.fetchone()[0] == 0
