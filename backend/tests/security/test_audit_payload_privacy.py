"""PD-4 — what `audit.audit_log` actually accumulates (Entry 11B0).

THE DEFECT, as Entry 11A recorded it (§23, gap register):

    `audit.audit_log` holds whole copies of financial/profile rows, has no user
    FK, is append-only, and account deletion *adds* to it.

Three properties that are each defensible alone and are not defensible together:

  * `audit.log_change` copies the WHOLE row — `to_jsonb(NEW)` — for all 32
    audited tables, so a salary lands in the audit log verbatim;
  * the table has NO foreign key to `identity.user_account`, so no cascade
    reaches it and the account-deletion work closed in Entry 11B2 cannot touch
    it;
  * a `reject_mutation` trigger makes it append-only, so nothing can remove a
    row through any ordinary path.

Entry 11A already fixed the credential half of this (PD-4a): named secret
columns are redacted. The financial and profile half was left for 11B0, and is
what these tests are about.

These go through the REAL production path — register, authenticate, POST the
value — because the question is what the running system writes, not what a
trigger does when poked directly. The markers are synthetic and belong to
nobody.
"""
from __future__ import annotations

import contextlib
import json
import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn

PASSWORD = "supersecret1"

#: Distinctive enough that finding it anywhere is unambiguous, and shaped like
#: the thing it stands in for. No real person's figures appear in this file.
MARKER_SALARY = "134217.73"
MARKER_EMPLOYER = "ZZQX Marker Employer Incorporated"
MARKER_EXPENSE = "8675.31"


@contextlib.contextmanager
def owner_cursor():
    """The audit log is readable by its owner only — `onyx_app_rw` has no
    SELECT on it, which is least privilege working correctly."""
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        yield conn.cursor()
    finally:
        conn.close()


def audit_rows_for(entity_table: str, entity_id: str) -> list[tuple]:
    """Rows for an entity, matched on a table-name PREFIX.

    `TG_TABLE_NAME` is the PARTITION for a partitioned relation, so a financial
    write is recorded against `income_source_y2025` rather than
    `income_source`. Matching the parent name exactly finds nothing and makes
    the test look like it passed.
    """
    with owner_cursor() as cur:
        cur.execute(
            "SELECT action, previous_value, new_value, actor_id "
            "  FROM audit.audit_log "
            " WHERE entity_table LIKE %s AND entity_id = %s "
            " ORDER BY created_at",
            (f"{entity_table}%", entity_id))
        return cur.fetchall()


def audit_text_contains(needle: str) -> int:
    """How many audit rows carry this string anywhere in their payload."""
    with owner_cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM audit.audit_log "
            " WHERE previous_value::text LIKE %s OR new_value::text LIKE %s",
            (f"%{needle}%", f"%{needle}%"))
        return cur.fetchone()[0]


async def _register(client) -> tuple[str, uuid.UUID]:
    email = f"pd4_{uuid.uuid4().hex[:10]}@example.com"
    assert (await client.post("/api/v1/auth/register",
                              json={"email": email, "password": PASSWORD})
            ).status_code == 201
    login = await client.post("/api/v1/auth/login",
                              json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    me = await client.get("/api/v1/users/me",
                          headers={"Authorization": f"Bearer {token}"})
    return token, uuid.UUID(me.json()["id"])


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# the defect
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_recorded_salary_does_not_reach_the_audit_log(client):
    """The core of PD-4.

    A user records an income source through the ordinary API. The amount is
    theirs, it is in the table designed to hold it, and it is protected there by
    RLS. The audit trigger then copies the entire row — amount included — into a
    store with no owner, no cascade and no delete.
    """
    token, user_id = await _register(client)

    created = await client.post(
        "/api/v1/financials/income",
        json={"tax_year": 2025, "income_type_code": "employment",
              "amount": MARKER_SALARY, "source_name": MARKER_EMPLOYER},
        headers=_headers(token))
    assert created.status_code == 201, created.text
    income_id = created.json()["id"]

    rows = audit_rows_for("income_source", income_id)
    assert rows, "the write was not audited at all; this test is checking nothing"

    for action, previous, new in ((r[0], r[1], r[2]) for r in rows):
        payload = json.dumps({"previous": previous, "new": new})
        assert MARKER_SALARY not in payload, (
            f"the audited {action} carries the amount verbatim: an audit row "
            "with no user FK, no cascade and no delete path now holds this "
            "user's salary"
        )
        assert MARKER_EMPLOYER not in payload, (
            f"the audited {action} carries the employer name verbatim"
        )


@pytest.mark.asyncio
async def test_the_audit_row_still_proves_who_changed_what(client):
    """The other half of the requirement, and the reason this cannot be fixed by
    simply not auditing.

    Entry 3A's guarantees rest on the audit log answering who, what and when.
    Removing the values must not remove the record.
    """
    token, user_id = await _register(client)

    created = await client.post(
        "/api/v1/financials/income",
        json={"tax_year": 2025, "income_type_code": "employment",
              "amount": MARKER_SALARY},
        headers=_headers(token))
    income_id = created.json()["id"]

    rows = audit_rows_for("income_source", income_id)
    assert len(rows) >= 1
    action, previous, new, actor_id = rows[0]
    assert action == "INSERT"
    assert str(actor_id) == str(user_id), "the audit row lost the actor"
    assert new is not None, (
        "the whole payload was dropped; an auditor can no longer see that the "
        "row was created or which columns it had"
    )
    assert "amount" in new, (
        "the column list was dropped, so a created row is indistinguishable "
        "from one that was never written"
    )


@pytest.mark.asyncio
async def test_an_expense_amount_does_not_reach_the_audit_log(client):
    """`finance.expense_record` is audited too, and is partitioned — the
    trigger fires on the partition, so a fix on the parent only would miss it."""
    token, _ = await _register(client)

    created = await client.post(
        "/api/v1/financials/expenses",
        json={"tax_year": 2025, "expense_category_code": "medical",
              "amount": MARKER_EXPENSE, "description": MARKER_EMPLOYER},
        headers=_headers(token))
    assert created.status_code == 201, created.text

    assert audit_text_contains(MARKER_EXPENSE) == 0, (
        "an expense amount reached the audit log"
    )


@pytest.mark.asyncio
async def test_a_tax_profile_is_not_copied_into_the_audit_log(client):
    """`profile.tax_profile` is audited and carries profile data — the
    specification calls out `employer_name` by name (§13).

    Scoped to THIS user's rows. An earlier version of this test read the most
    recent tax_profile audit row in the table regardless of who wrote it, and
    so passed against the unfixed trigger by reading somebody else's.
    """
    token, user_id = await _register(client)

    updated = await client.put(
        "/api/v1/users/me/tax-profile",
        json={"province_code": "ON", "marital_status": "single"},
        headers=_headers(token))
    assert updated.status_code in (200, 201), updated.text

    with owner_cursor() as cur:
        cur.execute(
            "SELECT action, new_value FROM audit.audit_log "
            " WHERE entity_table = 'tax_profile' AND actor_id = %s "
            " ORDER BY created_at",
            (str(user_id),))
        rows = cur.fetchall()

    assert rows, "the tax-profile write was not audited; this test checks nothing"
    for action, payload in rows:
        assert payload is not None, f"the {action} payload was dropped entirely"
        assert "province_code" in payload, "the column list was lost"
        assert payload.get("province_code") != "ON", (
            f"the audited {action} copied the tax profile's values verbatim"
        )
        assert payload.get("marital_status") != "single", (
            f"the audited {action} copied the marital status verbatim"
        )


# ---------------------------------------------------------------------------
# why it matters for deletion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_audit_log_is_unreachable_by_account_deletion(client):
    """Not a defect on its own — it is what makes the copy permanent.

    Stated as a test so that if a future phase adds a cascade or a delete path,
    the reasoning behind the PD-4 fix can be revisited deliberately rather than
    silently.
    """
    with owner_cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM information_schema.table_constraints tc
              JOIN information_schema.constraint_column_usage ccu
                ON tc.constraint_name = ccu.constraint_name
             WHERE tc.table_schema = 'audit' AND tc.table_name = 'audit_log'
               AND tc.constraint_type = 'FOREIGN KEY'
               AND ccu.table_name = 'user_account'
        """)
        assert cur.fetchone()[0] == 0, (
            "audit.audit_log now has a foreign key to the account — the PD-4 "
            "reasoning assumed it did not, and should be re-examined"
        )

    # And it is append-only, so a row written today cannot be edited away.
    token, _ = await _register(client)
    created = await client.post(
        "/api/v1/financials/income",
        json={"tax_year": 2025, "income_type_code": "employment",
              "amount": "1.00"},
        headers=_headers(token))
    income_id = created.json()["id"]

    with owner_cursor() as cur, pytest.raises(psycopg2.Error):
        # LIKE, not `=`: the row is recorded against the PARTITION, so an
        # exact match updates nothing, no trigger fires, and the test would
        # conclude the log is mutable when it never touched it.
        cur.execute("UPDATE audit.audit_log SET new_value = NULL "
                    " WHERE entity_table LIKE 'income_source%%' "
                    "   AND entity_id = %s",
                    (income_id,))
