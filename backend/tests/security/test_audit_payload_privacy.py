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


# ---------------------------------------------------------------------------
# historical rows (§13)
# ---------------------------------------------------------------------------
def _insert_legacy_audit_row(cur, *, relation: str, payload: dict) -> str:
    """A row shaped like one the PRE-FIX trigger wrote.

    Inserted directly because that is the only way to have one: the fixed
    trigger cannot produce it, which is the point.
    """
    schema, table = relation.split(".")
    cur.execute(
        "INSERT INTO audit.audit_log (actor_type, actor_id, action, "
        "  entity_schema, entity_table, entity_id, new_value) "
        "VALUES ('user', %s, 'INSERT', %s, %s, %s, %s::jsonb) RETURNING id",
        (str(uuid.uuid4()), schema, table, str(uuid.uuid4()),
         json.dumps(payload)))
    return cur.fetchone()[0]


def _payload_of(cur, row_id: str) -> dict:
    cur.execute("SELECT new_value FROM audit.audit_log WHERE id = %s", (row_id,))
    return cur.fetchone()[0]


def test_a_legacy_row_is_minimized_and_the_repair_is_idempotent():
    """§13 — new writes being safe does not make the defect closed.

    Rows written before migration 0048 keep whatever they contain, and the log
    is append-only, so the repair is a deliberate privileged operation rather
    than something a deployment does by itself.
    """
    from scripts import audit_payload_scan as scanner

    legacy = {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "amount": MARKER_SALARY,
        "source_name": MARKER_EMPLOYER,
        "notes": "a free-text note that should never have been copied",
        "tax_year": 2025,
        "created_at": "2026-01-01T00:00:00+00:00",
    }

    with owner_cursor() as cur:
        row_id = _insert_legacy_audit_row(
            cur, relation="finance.income_source_y2025", payload=legacy)

        assert scanner.scan(cur), "the scan did not see the legacy row"

        cur.execute(
            "ALTER TABLE audit.audit_log DISABLE TRIGGER trg_audit_immutable")
        try:
            scanner.minimize(cur)
            after_first = _payload_of(cur, row_id)
            scanner.minimize(cur)          # idempotency: a second pass
            after_second = _payload_of(cur, row_id)
        finally:
            cur.execute(
                "ALTER TABLE audit.audit_log ENABLE TRIGGER trg_audit_immutable")

    # The values are gone...
    assert after_first["amount"] == scanner.REDACTED
    assert after_first["source_name"] == scanner.REDACTED
    assert after_first["notes"] == scanner.REDACTED
    assert after_first["tax_year"] == scanner.REDACTED

    # ...the record of WHICH columns existed is not...
    for key in ("amount", "source_name", "notes", "tax_year"):
        assert key in after_first, f"the repair dropped the {key} key"

    # ...ownership and lifecycle survive, so the row is still attributable...
    assert after_first["user_id"] == legacy["user_id"]
    assert after_first["id"] == legacy["id"]
    assert after_first["created_at"] == legacy["created_at"]

    # ...and running it again changes nothing.
    assert after_second == after_first, "the repair is not idempotent"


def test_the_repair_leaves_the_operator_plane_alone():
    """Minimizing must not scrub the governance evidence Entry 3A reads."""
    from scripts import audit_payload_scan as scanner

    governance = {
        "id": str(uuid.uuid4()),
        "max_amount": "3000",
        "rule_code": "ZZQX_MARKER_RULE",
        "created_at": "2026-01-01T00:00:00+00:00",
    }

    with owner_cursor() as cur:
        row_id = _insert_legacy_audit_row(
            cur, relation="rules.rule_outcome", payload=governance)
        cur.execute(
            "ALTER TABLE audit.audit_log DISABLE TRIGGER trg_audit_immutable")
        try:
            scanner.minimize(cur)
            after = _payload_of(cur, row_id)
        finally:
            cur.execute(
                "ALTER TABLE audit.audit_log ENABLE TRIGGER trg_audit_immutable")

    assert after == governance, (
        "the repair scrubbed an operator-plane payload; four-eyes governance "
        "needs to read what a rule changed from and to"
    )


def test_the_repair_does_not_erase_sealed_evidence_hashes():
    """§8 — PD-4 remediation must not weaken replay integrity.

    The append-only audit copy of a content address is what makes the live
    sealed row tamper-evident.
    """
    from scripts import audit_payload_scan as scanner

    sealed = {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "manifest_hash": "a" * 64,
        "optimization_result_hash": "b" * 64,
        "engine_version": "1.4.2",
        "estimated_savings": MARKER_SALARY,
    }

    with owner_cursor() as cur:
        row_id = _insert_legacy_audit_row(
            cur, relation="ioe.optimization_run", payload=sealed)
        cur.execute(
            "ALTER TABLE audit.audit_log DISABLE TRIGGER trg_audit_immutable")
        try:
            scanner.minimize(cur)
            after = _payload_of(cur, row_id)
        finally:
            cur.execute(
                "ALTER TABLE audit.audit_log ENABLE TRIGGER trg_audit_immutable")

    assert after["manifest_hash"] == "a" * 64
    assert after["optimization_result_hash"] == "b" * 64
    assert after["engine_version"] == "1.4.2"
    assert after["estimated_savings"] == scanner.REDACTED, (
        "a derived tax result survived minimization"
    )


# ---------------------------------------------------------------------------
# the registries agree with the database (§10)
# ---------------------------------------------------------------------------
def test_the_scan_script_matches_the_trigger():
    """Two copies of the same policy — one in SQL for new writes, one in Python
    for historical rows. Drift means either scrubbing governance evidence or
    missing personal data, and neither announces itself."""
    from scripts import audit_payload_scan as scanner

    with owner_cursor() as cur:
        cur.execute("SELECT audit.audit_structural_columns()")
        sql_structural = set(cur.fetchone()[0])
        for relation in scanner.RETAINED_RELATIONS:
            schema, table = relation.split(".", 1)
            cur.execute("SELECT audit.audit_retains_values(%s, %s)",
                        (schema, table))
            assert cur.fetchone()[0] is True, (
                f"{relation} is retained by the script but filtered by the "
                "trigger"
            )

    assert sql_structural == set(scanner.STRUCTURAL_COLUMNS), (
        "the structural-column allowlist differs between "
        "db/sql/42_audit_payload_minimization.sql and "
        "scripts/audit_payload_scan.py: "
        f"sql-only={sorted(sql_structural - set(scanner.STRUCTURAL_COLUMNS))}, "
        f"py-only={sorted(set(scanner.STRUCTURAL_COLUMNS) - sql_structural)}"
    )


def test_every_audited_relation_is_classified_one_way_or_the_other():
    """Deny-by-default means an unlisted relation is FILTERED, which is the safe
    direction. This asserts the operator-plane list contains only relations that
    are actually audited — a stale entry would silently exempt nothing, but a
    typo in it would silently exempt the wrong thing."""
    from scripts import audit_payload_scan as scanner

    with owner_cursor() as cur:
        cur.execute("""
            SELECT n.nspname || '.' || c.relname
              FROM pg_trigger t
              JOIN pg_class c ON c.oid = t.tgrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE t.tgfoid = 'audit.log_change'::regproc
               AND NOT t.tgisinternal
        """)
        audited = {r[0] for r in cur.fetchall()}

    assert audited, "no relation is audited; this test is checking nothing"
    unknown = set(scanner.RETAINED_RELATIONS) - audited
    assert not unknown, (
        f"{sorted(unknown)} are on the value-retaining list but are not audited"
    )


def test_a_new_sealed_hash_column_cannot_appear_unlisted():
    """The one direction deny-by-default fails unsafely.

    A new personal column defaults to redacted, which is right. A new
    sealed-evidence content address would ALSO default to redacted, which would
    quietly erode the tamper-evidence Entry 11A preserved. This turns that into
    a build failure.
    """
    from scripts import audit_payload_scan as scanner

    with owner_cursor() as cur:
        cur.execute("""
            SELECT c.relname, a.attname
              FROM pg_trigger t
              JOIN pg_class c ON c.oid = t.tgrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0
             WHERE t.tgfoid = 'audit.log_change'::regproc
               AND NOT t.tgisinternal
               AND n.nspname = 'ioe'
               AND a.attname LIKE '%%hash%%'
        """)
        sealed_hash_columns = {r[1] for r in cur.fetchall()}

    missing = sealed_hash_columns - set(scanner.STRUCTURAL_COLUMNS)
    assert not missing, (
        f"{sorted(missing)} are sealed-evidence content addresses on an "
        "audited relation but are not on the structural allowlist, so the "
        "audit trail would stop being able to prove the sealed row was not "
        "altered. Add them to audit.audit_structural_columns() and to "
        "scripts/audit_payload_scan.py."
    )


# ---------------------------------------------------------------------------
# deletion readiness (§16)
# ---------------------------------------------------------------------------
#: The authoritative ownership key for an audit row (PD-15).
#:
#: NOT `actor_id` alone. Registration and login run on an ANONYMOUS session —
#: `db_anon`, actor_type `system`, no `app.user_id` — so the trigger records no
#: actor for the rows that create the account and its credential. Those rows
#: carry the identity in their PAYLOAD instead: `id` on `identity.user_account`,
#: `user_id` on everything else.
#:
#: A de-identification phase keyed on `actor_id` alone would leave a deleted
#: user's account-creation and credential rows behind. Stated here so that
#: Entry 11B3+ inherits the real key rather than the obvious one.
AUDIT_OWNERSHIP_KEY = """
    actor_id = %(uid)s
    OR new_value ->> 'user_id' = %(uid)s
    OR previous_value ->> 'user_id' = %(uid)s
    OR (entity_table = 'user_account'
        AND (new_value ->> 'id' = %(uid)s OR previous_value ->> 'id' = %(uid)s))
"""


@pytest.mark.asyncio
async def test_a_users_audit_rows_are_locatable_by_the_ownership_key(client):
    """§16 — a later deletion phase must be able to FIND this deterministically.

    The audit log has no foreign key, so it is not reachable by cascade and the
    Entry 11A inventory's foreign-key derivation cannot see it. What it does
    have is an ownership key — a composite one, see above.

    This asserts locatability, not deletion. Nothing is deleted here.
    """
    token, user_id = await _register(client)
    assert (await client.post(
        "/api/v1/financials/income",
        json={"tax_year": 2025, "income_type_code": "employment",
              "amount": MARKER_SALARY},
        headers=_headers(token))).status_code == 201
    assert (await client.put(
        "/api/v1/users/me/tax-profile",
        json={"province_code": "ON"},
        headers=_headers(token))).status_code in (200, 201)

    with owner_cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM audit.audit_log WHERE {AUDIT_OWNERSHIP_KEY}",
            {"uid": str(user_id)})
        located = cur.fetchone()[0]

        # Registration alone writes user_account + user_credential, and those
        # are exactly the rows `actor_id` does not cover.
        cur.execute(
            "SELECT count(*) FROM audit.audit_log WHERE actor_id = %(uid)s",
            {"uid": str(user_id)})
        by_actor_only = cur.fetchone()[0]

        # Nothing carrying this user's identity escapes the composite key.
        cur.execute(f"""
            SELECT count(*) FROM audit.audit_log
             WHERE (new_value::text LIKE %(like)s
                    OR previous_value::text LIKE %(like)s)
               AND NOT ({AUDIT_OWNERSHIP_KEY})
        """, {"uid": str(user_id), "like": f"%{user_id}%"})
        escaped = cur.fetchone()[0]

    assert located > by_actor_only, (
        "actor_id happened to cover every row, so this test is not "
        "demonstrating the composite key it exists to justify"
    )
    assert escaped == 0, (
        f"{escaped} audit rows carry this user's id somewhere the ownership "
        "key does not look, so a de-identification phase would leave them"
    )


@pytest.mark.asyncio
async def test_the_ownership_key_does_not_reach_another_account(client):
    """The key must be exact. A phase that over-matches would de-identify
    somebody else's audit history while deleting this account."""
    token_a, user_a = await _register(client)
    token_b, user_b = await _register(client)
    for token in (token_a, token_b):
        await client.post(
            "/api/v1/financials/income",
            json={"tax_year": 2025, "income_type_code": "employment",
                  "amount": MARKER_SALARY},
            headers=_headers(token))

    with owner_cursor() as cur:
        cur.execute(
            f"""SELECT count(*) FROM audit.audit_log
                 WHERE ({AUDIT_OWNERSHIP_KEY})
                   AND (actor_id = %(other)s
                        OR new_value ->> 'user_id' = %(other)s)""",
            {"uid": str(user_a), "other": str(user_b)})
        overlap = cur.fetchone()[0]

    assert overlap == 0, (
        "the ownership key for one account also selects another account's rows"
    )


@pytest.mark.asyncio
async def test_the_runtime_role_cannot_read_the_audit_log_at_all(client):
    """Cross-tenant, and the §14 pattern: the DATABASE refuses, not a filter.

    `audit.audit_log` has no RLS — deliberately, it is an operator store — so
    the control is grants. Asserted by becoming the runtime role and being
    refused, rather than by reading the migration and believing it. Entry 11B2
    learned that lesson the expensive way: an explicit GRANT looked restrictive
    while broader rights survived through default privileges.
    """
    token_a, _ = await _register(client)
    await client.post(
        "/api/v1/financials/income",
        json={"tax_year": 2025, "income_type_code": "employment",
              "amount": MARKER_SALARY},
        headers=_headers(token_a))

    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        # onyx_app_rw is NOLOGIN, so SET ROLE is how a test can act as it.
        cur.execute("SET ROLE onyx_app_rw")
        for statement in (
            "SELECT count(*) FROM audit.audit_log",
            "INSERT INTO audit.audit_log (actor_type, action, entity_schema, "
            "  entity_table) VALUES ('user','INSERT','finance','income_source')",
            "UPDATE audit.audit_log SET new_value = NULL",
            "DELETE FROM audit.audit_log",
        ):
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(statement)
            conn.rollback()
            cur.execute("SET ROLE onyx_app_rw")
    finally:
        conn.rollback()
        conn.close()


def test_the_new_functions_are_not_executable_by_public():
    """§14 — the payload policy must not be callable by anyone who happens to
    have a connection. Effective privilege, not the REVOKE statement's text."""
    with owner_cursor() as cur:
        for signature in (
            "audit.log_change()",
            "audit.audit_retains_values(text, text)",
            "audit.audit_structural_columns()",
        ):
            cur.execute(
                "SELECT has_function_privilege('public', %s, 'EXECUTE')",
                (signature,))
            assert cur.fetchone()[0] is False, (
                f"PUBLIC can execute {signature}"
            )


def test_the_audit_trigger_function_is_owned_by_the_migrator():
    """SECURITY DEFINER runs as the OWNER. If ownership ever moved to a role
    with fewer or different rights, auditing would break silently — or, worse,
    a lower-privileged owner would make the definer rights meaningless."""
    with owner_cursor() as cur:
        cur.execute("""
            SELECT r.rolname, p.prosecdef, p.proconfig
              FROM pg_proc p
              JOIN pg_roles r ON r.oid = p.proowner
              JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'audit' AND p.proname = 'log_change'
        """)
        owner, secdef, config = cur.fetchone()
    assert secdef is True, "audit.log_change stopped being SECURITY DEFINER"
    assert owner == "onyx_migrator", f"unexpected owner: {owner}"
    assert any(c.startswith("search_path=") for c in (config or [])), (
        "audit.log_change has no pinned search_path"
    )
