"""BillShield Slice 3A — the restricted principal, proved by being it.

WHO PROVES WHAT (plan §5.4.8, caller matrix). Every *success* claim in this
file is made by the non-superuser `onyx_billshield` login, because a success is
only evidence when the principal that will make it in production makes it here.
Every *refusal* is made by the principal that would have been refused. Only
catalogue **facts** — ownership, role attributes, memberships, ACLs, schema
grants, `prosecdef` sets — are read through a privileged connection, and that
connection never stands in for a runtime behaviour.

WHY A REAL LOGIN AND NOT `SET ROLE`. The harness's owner is a cluster
superuser, and a superuser bypasses RLS unconditionally. A test that reached
the group role by `SET ROLE` from that connection would still be running in a
session whose `session_user` is a superuser; the repository already models the
production topology instead, with `onyx_privacy_test` and `onyx_freshness_test`
holding their own logins. `onyx_billshield_test` joins them.

WHAT SLICE 3A DOES NOT HAVE. No keyhole, no keyhole-owner role, no outbox
privilege, no task, no queue. Several assertions below are therefore *emptiness*
assertions, and they are the point: they pin the posture now so the slice that
adds a keyhole has to change a set equality rather than slip past a subset check.
"""
from __future__ import annotations

import contextlib
import subprocess
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.conftest import app_dsn, billshield_dsn, owner_dsn
from tests.security.billshield.conftest import owner_cursor

WORKER_GROUP = "onyx_billshield_worker"

#: The approved schema-USAGE set, as SET EQUALITY (plan §5.4.6). `ref` is name
#: resolution for `ref.current_app_user()`; `identity` is name resolution for
#: the deletion-cutoff function and nothing else.
APPROVED_SCHEMAS = {"billshield", "ref", "identity"}

#: The approved SECURITY DEFINER executable set at THIS sub-slice. Row 4 of
#: §5.4.11 is set equality against whatever keyholes exist, and at 3A none do.
APPROVED_DEFINER_FUNCTIONS = {"identity.account_deletion_state"}

#: The four BillShield tables the worker may touch at all. Named because two
#: assertions below are about the ABSENCE of a table-level grant on exactly
#: these, which a query over "whatever the worker holds" cannot express.
WORKER_TABLES = (
    "billshield.bill",
    "billshield.extraction_run",
    "billshield.charge_candidate",
    "billshield.promotion_candidate",
)

#: THE WHOLE CAPABILITY, COLUMN BY COLUMN — `(table, verb) -> (columns, reason)`.
#:
#: EVERY entry is column-scoped. There is no table-level `SELECT`, `INSERT` or
#: `UPDATE` anywhere in this map, and a separate test asserts the table-level
#: DML set for these four tables is EMPTY. That is the difference between a
#: capability and a category: a table-level verb silently authorizes every
#: column a later migration adds, so the boundary would widen without anybody
#: writing a grant. Requiring a reviewed migration for each newly writable
#: column is the point, not an inconvenience to be designed around.
#:
#: Each column carries one CURRENT, CONCRETE reason. Where PostgreSQL itself
#: demands the column — a policy subquery, a read-modify-write, a `RETURNING`
#: clause — the reason says so and the measurement that established it is named.
APPROVED_COLUMN_PRIVILEGES: dict[tuple[str, str], tuple[frozenset[str], str]] = {
    ("billshield.bill", "SELECT"): (
        frozenset({
            "id",            # locate the bill, and the WHERE clause of its own UPDATE
            "user_id",       # REQUIRED BY POSTGRESQL, not by worker code: the
                             # parent-derived policies on extraction_run and both
                             # candidate tables evaluate `b.user_id =
                             # ref.current_app_user()` as the CALLER, so the
                             # subquery is refused without this column even
                             # though the worker never selects it itself
            "status",        # inspect lifecycle state before transitioning it
            "storage_key",   # obtain the artifact — the generated locator is the
                             # only address the bytes have
            "file_sha256",   # verify the artifact: this is the digest an
                             # extraction_run's input_sha256 is bound to by the
                             # composite foreign key
            "byte_size",     # verify the artifact against the accepted bounds
            "artifact_format",  # verify the artifact: which parse path is legal
            "page_count",    # verify the artifact against the page ceiling
            "deleted_at",    # enforce deletion refusal — a tombstoned bill is
            "erased_at",     # never processed, and an erased one has no bytes
            "row_version",   # read-modify-write: `row_version = row_version + 1`
                             # is a READ of the column, measured as refused
                             # without SELECT on it
        }),
        "the exact current read set: locate, inspect lifecycle, obtain and "
        "verify finalized artifact metadata, enforce deletion refusal, and "
        "perform an optimistic transition. `created_at` and `updated_at` are "
        "withheld — the worker has no use for either, and a runtime that could "
        "read the trigger's stamp is one step from wanting to write it"),
    ("billshield.bill", "UPDATE"): (
        frozenset({"status", "row_version"}),
        "the worker-owned §7.3 lifecycle transitions and their optimistic "
        "concurrency stamp. Everything else on the row belongs to somebody "
        "else: `deleted_at` to the customer's request, `erased_at` to the "
        "privacy worker's claim that erasure happened, the four artifact facts "
        "to upload completion, `user_id`/`storage_key` to immutable identity"),
    ("billshield.extraction_run", "SELECT"): (
        frozenset({
            "id",       # the INSERT's RETURNING, and the WHERE clause of the
                        # finalizing UPDATE
            "bill_id",  # REQUIRED BY POSTGRESQL: both candidate policies join
                        # `extraction_run r JOIN bill b ON b.id = r.bill_id` as
                        # the caller, so a candidate INSERT is refused without
                        # it — measured, not assumed
        }),
        "the two columns PostgreSQL demands: one to get the new run's identity "
        "back and address it, one for the parent-derived candidate policies. "
        "Every extraction VALUE stays unreadable — the API serves review"),
    ("billshield.extraction_run", "INSERT"): (
        frozenset({"bill_id", "input_sha256"}),
        "CREATE A RUNNING ATTEMPT, which is exactly these two columns: the "
        "parent and the finalized digest it is bound to. `status` defaults to "
        "'running', `id` and `created_at` are server-owned, `completed_at` "
        "starts NULL — none may be client-supplied, so a worker cannot insert "
        "a row that is already terminal. Adapter identity is deliberately NOT "
        "here: the schema permits a running row to acquire it later, so it is "
        "written by the finalizing UPDATE and this grant stays at two columns"),
    ("billshield.extraction_run", "UPDATE"): (
        frozenset({
            # The terminal decision and its stamp.
            "status", "completed_at",
            # The two closed outcome vocabularies.
            "refusal_code", "failure_code",
            # Adapter provenance, established when the attempt finalizes.
            "adapter_code", "model_version", "prompt_version",
            # Success identity.
            "extraction_schema_version", "currency", "response_hash",
            # The nine bill-level candidate triples the contract writes.
            "issuer_name_value", "issuer_name_confidence", "issuer_name_evidence",
            "service_category_value", "service_category_confidence",
            "service_category_evidence",
            "statement_date_value", "statement_date_confidence",
            "statement_date_evidence",
            "billing_period_start", "billing_period_end",
            "billing_period_confidence", "billing_period_evidence",
            "amount_due_value", "amount_due_confidence", "amount_due_evidence",
            "previous_balance_value", "previous_balance_confidence",
            "previous_balance_evidence",
            "payments_applied_value", "payments_applied_confidence",
            "payments_applied_evidence",
            "subtotal_before_tax_value", "subtotal_before_tax_confidence",
            "subtotal_before_tax_evidence",
            "total_tax_value", "total_tax_confidence", "total_tax_evidence",
        }),
        "the exact terminal-finalization set of the current extraction "
        "contract. `id`, `bill_id`, `input_sha256` and `created_at` are absent, "
        "so an attempt to rewrite a run's identity is refused by the ACL "
        "BEFORE any trigger runs; `reject_extraction_run_rewrite` remains as "
        "defence in depth, not as the boundary. A field added to this table by "
        "a later slice is unwritable until a reviewed migration grants it"),
    ("billshield.charge_candidate", "INSERT"): (
        frozenset({
            "extraction_run_id", "position",
            "label_text", "label_confidence", "label_evidence",
            "amount", "amount_confidence", "amount_evidence",
            "kind", "kind_confidence",
            "cadence_value", "cadence_confidence", "cadence_evidence",
            "service_period_start", "service_period_end",
            "service_period_confidence", "service_period_evidence",
        }),
        "the extracted candidate fields and nothing else. `id` and "
        "`created_at` are server-owned: a runtime that could supply either "
        "could choose a candidate's identity or backdate the record of when "
        "the extraction happened"),
    ("billshield.promotion_candidate", "INSERT"): (
        frozenset({
            "extraction_run_id", "position", "charge_position",
            "expiry_date", "expiry_confidence", "expiry_evidence",
        }),
        "the extracted candidate fields and nothing else, for the same reason "
        "as the charge candidate above"),
}

#: Column-scoped UPDATE on `bill`: exactly the two columns a lifecycle
#: transition writes. Everything else is another principal's or nobody's.
APPROVED_BILL_UPDATE_COLUMNS = APPROVED_COLUMN_PRIVILEGES[
    ("billshield.bill", "UPDATE")][0]

#: Server-owned columns on `extraction_run` that a worker INSERT must never be
#: able to name, with the value a forged insert would try to supply.
FORBIDDEN_RUN_INSERT_COLUMNS = {
    "id": "gen_random_uuid()",
    "status": "'succeeded'",
    "completed_at": "now()",
    "created_at": "now()",
}

#: Identity columns on `extraction_run` that must be refused by the ACL, before
#: the immutability trigger is ever consulted.
FROZEN_RUN_COLUMNS = {
    "id": "gen_random_uuid()",
    "bill_id": "gen_random_uuid()",
    "input_sha256": "repeat('c', 64)",
    "created_at": "now()",
}


@contextlib.contextmanager
def as_billshield(user_id: uuid.UUID | None = None):
    """A real session as the non-superuser BillShield login, always rolled back.

    The GUC is transaction-local, exactly as `billshield_unit_of_work` sets it.
    """
    conn = psycopg2.connect(billshield_dsn())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        if user_id is not None:
            cur.execute(
                "SELECT set_config('app.actor_type', 'system', true),"
                "       set_config('app.user_id', %s, true)", (str(user_id),))
        yield cur
    finally:
        conn.rollback()
        conn.close()


@contextlib.contextmanager
def as_app(user_id: uuid.UUID | None = None):
    """A real session as the ordinary application login."""
    conn = psycopg2.connect(app_dsn())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        if user_id is not None:
            cur.execute("SELECT set_config('app.user_id', %s, true)",
                        (str(user_id),))
        yield cur
    finally:
        conn.rollback()
        conn.close()


def _refused(cur, statement: str, params=None) -> str:
    """Run `statement` expecting PostgreSQL to refuse it, and return the error."""
    with pytest.raises(psycopg2.Error) as caught:
        cur.execute(statement, params)
    return str(caught.value)


# --------------------------------------------------------------------------- #
# E. Identity: the login, its membership, and what it cannot become
# --------------------------------------------------------------------------- #
def test_the_worker_group_still_cannot_log_in():
    with owner_cursor() as cur:
        cur.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s",
                    (WORKER_GROUP,))
        assert cur.fetchone() == (False,), (
            "the group role is a capability, not a session"
        )


def test_the_billshield_login_holds_no_dangerous_role_attribute():
    with as_billshield() as cur:
        cur.execute("SELECT current_user")
        login = cur.fetchone()[0]
    with owner_cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole"
            " FROM pg_roles WHERE rolname = %s", (login,))
        superuser, bypassrls, createdb, createrole = cur.fetchone()
    assert superuser is False, "a superuser bypasses RLS unconditionally"
    assert bypassrls is False, (
        "BYPASSRLS would make every tenant-isolation assertion in this file "
        "vacuous (§5.4.8)"
    )
    assert createdb is False and createrole is False


def test_the_billshield_login_is_a_member_of_the_worker_group_and_nothing_else():
    with as_billshield() as cur:
        cur.execute("SELECT current_user")
        login = cur.fetchone()[0]
    with owner_cursor() as cur:
        cur.execute(
            "SELECT r.rolname FROM pg_auth_members m"
            " JOIN pg_roles r ON r.oid = m.roleid"
            " JOIN pg_roles g ON g.oid = m.member"
            " WHERE g.rolname = %s", (login,))
        memberships = {row[0] for row in cur.fetchall()}
    assert memberships == {WORKER_GROUP}, (
        f"{login} inherits {sorted(memberships)}; it must inherit exactly the "
        "BillShield worker capability"
    )


def test_the_application_role_is_not_a_member_of_the_billshield_group():
    """PD-16 by name: a request-path session must not be able to assume the
    worker's capability."""
    with owner_cursor() as cur:
        cur.execute(
            "SELECT g.rolname FROM pg_auth_members m"
            " JOIN pg_roles r ON r.oid = m.roleid"
            " JOIN pg_roles g ON g.oid = m.member"
            " WHERE r.rolname = %s", (WORKER_GROUP,))
        members = {row[0] for row in cur.fetchall()}
    assert "onyx_app_rw" not in members
    assert "onyx_app_ro" not in members
    assert not (members & {"onyx_privacy_worker", "onyx_freshness_worker"})


@pytest.mark.parametrize("role", [
    "onyx_app_rw", "onyx_privacy_worker", "onyx_freshness_worker",
    "onyx_migrator", "onyx_kb_admin", "onyx_audit_writer", "onyx_app_ro",
])
def test_the_billshield_login_cannot_assume_any_other_runtime_role(role):
    with as_billshield() as cur:
        message = _refused(cur, f"SET ROLE {role}")
    assert "permission denied" in message.lower()


def test_no_billshield_keyhole_owner_role_exists_yet():
    """3A creates no definer-owner role. Asserted as absence so the slice that
    adds one has to change this test deliberately (§17 Slice 3A exclusions)."""
    with owner_cursor() as cur:
        cur.execute(
            "SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
            ("%billshield%keyhole%",))
        assert cur.fetchall() == []


# --------------------------------------------------------------------------- #
# F. The operation-level allowlist, catalogue-derived
# --------------------------------------------------------------------------- #
def _direct_table_grants(grantee: str) -> set[tuple[str, str]]:
    """(qualified table, verb) pairs granted DIRECTLY to `grantee`.

    Read from `pg_class.relacl` rather than through an effective-privilege
    function: test logins are members of the group roles and inherit their
    privileges, so an effective sweep manufactures findings by construction
    (§5.4.6, proof 1).
    """
    with owner_cursor() as cur:
        cur.execute(
            "SELECT n.nspname || '.' || c.relname, a.privilege_type"
            " FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " CROSS JOIN LATERAL aclexplode(c.relacl) a"
            " JOIN pg_roles g ON g.oid = a.grantee"
            " WHERE g.rolname = %s AND c.relkind IN ('r','p','v','m','S')",
            (grantee,))
        return {(row[0], row[1]) for row in cur.fetchall()}


def _direct_column_grants(grantee: str) -> set[tuple[str, str, str]]:
    with owner_cursor() as cur:
        cur.execute(
            "SELECT n.nspname || '.' || c.relname, att.attname, a.privilege_type"
            " FROM pg_attribute att"
            " JOIN pg_class c ON c.oid = att.attrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " CROSS JOIN LATERAL aclexplode(att.attacl) a"
            " JOIN pg_roles g ON g.oid = a.grantee"
            " WHERE g.rolname = %s", (grantee,))
        return {(row[0], row[1], row[2]) for row in cur.fetchall()}


def _column_grant_map(grantee: str) -> dict[tuple[str, str], set[str]]:
    by_column: dict[tuple[str, str], set[str]] = {}
    for table, column, verb in _direct_column_grants(grantee):
        by_column.setdefault((table, verb), set()).add(column)
    return by_column


def test_the_worker_holds_no_table_level_dml_on_any_billshield_table():
    """THE CENTRAL CLAIM of the column-scoped design, stated as emptiness.

    A table-level `SELECT`/`INSERT`/`UPDATE` authorizes every column the table
    has AND every column a later migration adds. That is the failure this whole
    map exists to prevent, and it is not something the column-equality test
    below can see on its own: a table-level grant would sit in `pg_class.relacl`
    while the column map stayed exactly as approved.
    """
    offenders = sorted(
        f"{table}: {verb}"
        for table, verb in _direct_table_grants(WORKER_GROUP)
        if table in WORKER_TABLES and verb in {"SELECT", "INSERT", "UPDATE"}
    )
    assert offenders == [], (
        "the worker holds table-level DML, which authorizes every present and "
        "future column of that table:\n  " + "\n  ".join(offenders))


def test_the_worker_column_privileges_equal_the_approved_allowlist():
    """Set equality, per (table, verb), column by column, reason per entry."""
    granted = _column_grant_map(WORKER_GROUP)
    approved = {entry: set(columns)
                for entry, (columns, _) in APPROVED_COLUMN_PRIVILEGES.items()}
    assert set(granted) == set(approved), (
        f"unexpected (table, verb): {sorted(set(granted) - set(approved))}; "
        f"missing: {sorted(set(approved) - set(granted))}"
    )
    wrong = [
        f"{entry}: holds {sorted(granted[entry])}, approved {sorted(columns)}"
        for entry, columns in approved.items() if granted[entry] != columns
    ]
    assert wrong == [], "\n  ".join(wrong)
    assert all(reason.strip()
               for _, reason in APPROVED_COLUMN_PRIVILEGES.values()), (
        "an allow-list nobody prunes becomes a deny-list with extra steps"
    )


def test_the_table_level_guard_is_non_vacuous():
    """A planted TABLE-level grant must make the guard fail.

    Granted inside a transaction that is always rolled back, so the plant never
    outlives the test. Without this, `test_..._no_table_level_dml_...` would
    pass just as happily against an enumeration that could not see a
    table-level grant at all.
    """
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("GRANT SELECT ON billshield.bill TO onyx_billshield_worker")
        cur.execute(
            "SELECT a.privilege_type FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " CROSS JOIN LATERAL aclexplode(c.relacl) a"
            " JOIN pg_roles g ON g.oid = a.grantee"
            " WHERE g.rolname = %s AND n.nspname = 'billshield'"
            "   AND c.relname = 'bill'", (WORKER_GROUP,))
        assert "SELECT" in {row[0] for row in cur.fetchall()}, (
            "the enumeration cannot see a table-level grant made in this "
            "transaction — it would not see a real one either")
    finally:
        conn.rollback()
        conn.close()


def test_the_column_guard_is_non_vacuous():
    """And a planted extra COLUMN must be caught too."""
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute(
            "GRANT UPDATE (erased_at) ON billshield.bill"
            " TO onyx_billshield_worker")
        cur.execute(
            "SELECT att.attname FROM pg_attribute att"
            " JOIN pg_class c ON c.oid = att.attrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " CROSS JOIN LATERAL aclexplode(att.attacl) a"
            " JOIN pg_roles g ON g.oid = a.grantee"
            " WHERE g.rolname = %s AND n.nspname = 'billshield'"
            "   AND c.relname = 'bill' AND a.privilege_type = 'UPDATE'",
            (WORKER_GROUP,))
        assert "erased_at" in {row[0] for row in cur.fetchall()}, (
            "the column enumeration missed a grant made in this transaction")
    finally:
        conn.rollback()
        conn.close()


def test_a_future_column_is_not_authorized_by_any_existing_grant():
    """Catalogue semantics: a column added later starts with NO privilege.

    This is the property that makes the column-scoped design worth its length.
    Under a table-level grant `has_column_privilege` would answer TRUE for a
    column nobody reviewed, the moment it existed.

    The ALTER and the plant both run inside one rolled-back transaction, so no
    probe column, grant or residue survives the test — asserted afterwards.
    """
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = False
    probe = "probe_future_column"
    try:
        cur = conn.cursor()
        cur.execute(f"ALTER TABLE billshield.bill ADD COLUMN {probe} text")

        cur.execute(
            "SELECT has_column_privilege(%s, 'billshield.bill', %s, 'SELECT'),"
            "       has_column_privilege(%s, 'billshield.bill', %s, 'UPDATE'),"
            "       has_column_privilege(%s, 'billshield.bill', 'status', 'UPDATE')",
            (WORKER_GROUP, probe, WORKER_GROUP, probe, WORKER_GROUP))
        may_read_new, may_write_new, may_write_granted = cur.fetchone()
        assert may_read_new is False, (
            "a column added by a later migration is readable with no grant; "
            "the worker holds table-level SELECT somewhere")
        assert may_write_new is False, (
            "a column added by a later migration is writable with no grant")
        assert may_write_granted is True, (
            "the probe is vacuous — the worker cannot write an APPROVED column "
            "either, so the two FALSE answers above prove nothing")

        # Non-vacuity: granting it flips the answer, so the check can detect
        # authorization when it is really there.
        cur.execute(
            f"GRANT SELECT ({probe}) ON billshield.bill TO onyx_billshield_worker")
        cur.execute(
            "SELECT has_column_privilege(%s, 'billshield.bill', %s, 'SELECT')",
            (WORKER_GROUP, probe))
        assert cur.fetchone()[0] is True, (
            "has_column_privilege cannot see a grant made in this transaction")
    finally:
        conn.rollback()
        conn.close()

    with owner_cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_attribute att"
            " JOIN pg_class c ON c.oid = att.attrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'billshield' AND c.relname = 'bill'"
            "   AND att.attname = %s AND NOT att.attisdropped", (probe,))
        assert cur.fetchone()[0] == 0, "the probe column outlived the test"


def test_the_bill_update_is_column_scoped_to_the_lifecycle_columns():
    """A table-level UPDATE would let the worker write `deleted_at` (the
    customer's request), `erased_at` (the privacy worker's claim), or the
    finalized artifact facts."""
    columns = {
        column for table, column, verb in _direct_column_grants(WORKER_GROUP)
        if table == "billshield.bill" and verb == "UPDATE"
    }
    assert columns == set(APPROVED_BILL_UPDATE_COLUMNS), (
        f"bill UPDATE columns are {sorted(columns)}"
    )


@pytest.mark.parametrize("verb", ["DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"])
def test_the_worker_never_holds_a_destructive_or_structural_verb(verb):
    """§5.4.6: DELETE is withheld by default because BillShield's deletion model
    is a tombstone plus privacy-worker erasure; TRUNCATE bypasses RLS entirely.

    Checked at both scopes: `REFERENCES` is column-grantable, so a table-level
    query alone would miss it.
    """
    granted = {(table, held) for table, held in _direct_table_grants(WORKER_GROUP)
               if held == verb}
    granted |= {(table, held)
                for table, _, held in _direct_column_grants(WORKER_GROUP)
                if held == verb}
    assert granted == set()


def test_the_worker_holds_no_privilege_at_all_on_the_outbox():
    """The keyholes are the only outbox interface, and they do not exist yet."""
    tables = {table for table, _ in _direct_table_grants(WORKER_GROUP)}
    assert "billshield.job_outbox" not in tables
    columns = {table for table, _, _ in _direct_column_grants(WORKER_GROUP)}
    assert "billshield.job_outbox" not in columns


def test_the_worker_holds_zero_privileges_outside_the_approved_allowlist():
    """Catalogue-derived across every non-system schema — no hardcoded schema
    list, per §4.1. This is the Tax Assurance denial, stated as the precise
    first claim of §5.4.6's three."""
    approved_tables = {table for table, _ in APPROVED_COLUMN_PRIVILEGES}
    strays = {
        (table, verb) for table, verb in _direct_table_grants(WORKER_GROUP)
        if table not in approved_tables
    }
    assert strays == set(), f"privileges outside the allowlist: {sorted(strays)}"

    stray_columns = {
        (table, column, verb)
        for table, column, verb in _direct_column_grants(WORKER_GROUP)
        if table not in approved_tables
    }
    assert stray_columns == set()


def test_the_worker_holds_no_sequence_privilege():
    with owner_cursor() as cur:
        cur.execute(
            "SELECT n.nspname || '.' || c.relname"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " CROSS JOIN LATERAL aclexplode(c.relacl) a"
            " JOIN pg_roles g ON g.oid = a.grantee"
            " WHERE g.rolname = %s AND c.relkind = 'S'", (WORKER_GROUP,))
        assert cur.fetchall() == []


def test_the_global_catalogue_tables_are_not_readable_by_the_worker():
    """§7.2: a provider is never resolved automatically from extracted issuer
    text, so no use case at this slice proves the read. Withheld until one does.
    """
    tables = {table for table, _ in _direct_table_grants(WORKER_GROUP)}
    assert "billshield.provider" not in tables
    assert "billshield.provider_category" not in tables


# --------------------------------------------------------------------------- #
# G. Schema and function access
# --------------------------------------------------------------------------- #
def test_the_worker_schema_usage_equals_exactly_the_approved_set():
    """Set equality, not containment — the second of §5.4.6's three claims."""
    with owner_cursor() as cur:
        cur.execute(
            "SELECT n.nspname FROM pg_namespace n"
            " CROSS JOIN LATERAL aclexplode(n.nspacl) a"
            " JOIN pg_roles g ON g.oid = a.grantee"
            " WHERE g.rolname = %s AND a.privilege_type = 'USAGE'",
            (WORKER_GROUP,))
        granted = {row[0] for row in cur.fetchall()}
    assert granted == APPROVED_SCHEMAS, (
        f"schema USAGE is {sorted(granted)}, approved is "
        f"{sorted(APPROVED_SCHEMAS)}"
    )


def test_the_worker_holds_no_create_privilege_on_any_schema():
    with owner_cursor() as cur:
        cur.execute(
            "SELECT n.nspname FROM pg_namespace n"
            " CROSS JOIN LATERAL aclexplode(n.nspacl) a"
            " JOIN pg_roles g ON g.oid = a.grantee"
            " WHERE g.rolname = %s AND a.privilege_type = 'CREATE'",
            (WORKER_GROUP,))
        assert cur.fetchall() == []


def test_the_shared_multi_schema_grant_was_not_edited():
    """Decision 21.1(15). The roles file grants USAGE on fourteen schemas to
    `onyx_app_rw` and `onyx_app_ro` in one statement; adding the worker there
    would hand it name resolution across every tax schema."""
    with owner_cursor() as cur:
        cur.execute(
            "SELECT n.nspname FROM pg_namespace n"
            " CROSS JOIN LATERAL aclexplode(n.nspacl) a"
            " JOIN pg_roles g ON g.oid = a.grantee"
            " WHERE g.rolname = %s AND a.privilege_type = 'USAGE'"
            "   AND n.nspname NOT IN ('billshield','ref','identity')",
            (WORKER_GROUP,))
        assert cur.fetchall() == [], (
            "the worker reached a tax schema; the shared grant line was edited"
        )


def test_the_worker_definer_executable_set_equals_its_approved_baseline():
    """Row 4 of §5.4.11, per principal and as set equality.

    `ref.current_app_user()` is deliberately absent: it is not SECURITY
    DEFINER, so a `prosecdef` query correctly never returns it.
    """
    with owner_cursor() as cur:
        cur.execute(
            "SELECT n.nspname || '.' || p.proname"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE p.prosecdef"
            "   AND has_function_privilege(%s, p.oid, 'EXECUTE')",
            (WORKER_GROUP,))
        executable = {row[0] for row in cur.fetchall()}
    assert executable == APPROVED_DEFINER_FUNCTIONS, (
        f"definer set is {sorted(executable)}, approved is "
        f"{sorted(APPROVED_DEFINER_FUNCTIONS)}"
    )


def test_the_application_role_holds_no_billshield_keyhole_execute():
    with owner_cursor() as cur:
        cur.execute(
            "SELECT n.nspname || '.' || p.proname"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE p.prosecdef AND n.nspname = 'billshield'"
            "   AND has_function_privilege('onyx_app_rw', p.oid, 'EXECUTE')")
        assert cur.fetchall() == []


def test_no_billshield_security_definer_function_exists_yet():
    with owner_cursor() as cur:
        cur.execute(
            "SELECT p.proname FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'billshield' AND p.prosecdef")
        assert cur.fetchall() == [], (
            "3A creates no keyhole; the outbox interface arrives in 3C"
        )


def test_current_app_user_resolves_the_transaction_and_leaks_nothing_after():
    """Behavioural, as the worker login. A different question from the definer
    set: the function is PUBLIC-executable and schema USAGE is what makes it
    reachable."""
    tenant = uuid.uuid4()
    with as_billshield() as cur:
        cur.execute("SELECT set_config('app.user_id', %s, true)", (str(tenant),))
        cur.execute("SELECT ref.current_app_user()")
        # Compared as text: this driver is not configured with a UUID adapter,
        # so the column arrives as a string and an object comparison would fail
        # on representation rather than on the value under test.
        assert str(cur.fetchone()[0]) == str(tenant)
    with as_billshield() as cur:
        cur.execute("SELECT ref.current_app_user()")
        assert cur.fetchone()[0] is None, (
            "the GUC survived its transaction on a pooled connection"
        )


def test_the_worker_can_call_the_deletion_cutoff_function(tenants):
    """Positive control: an active account returns NULL, which is the state a
    task must be able to distinguish from a deleting one."""
    tenant_a, _ = tenants
    with as_billshield() as cur:
        cur.execute("SELECT identity.account_deletion_state(%s)",
                    (str(tenant_a.user_id),))
        assert cur.fetchone()[0] is None


def test_the_deletion_cutoff_function_reports_a_deleting_account():
    """The other half. Without it the test would pass against a function that
    always returned NULL.

    ON ITS OWN THROWAWAY ACCOUNT, not on a shared fixture tenant. A lifecycle
    row is deliberately undeletable — `trg_account_lifecycle_no_delete` refuses
    every DELETE, because a restored backup needs that record to know what to
    re-delete — so the only way to remove one is to remove the account it
    belongs to. Marking a module-scoped tenant would therefore leave it marked
    for every test that ran afterwards.
    """
    with owner_cursor() as cur:
        cur.execute(
            "INSERT INTO identity.user_account (email) VALUES (%s) RETURNING id",
            (f"billshield-cutoff-{uuid.uuid4().hex[:8]}@example.test",))
        subject = cur.fetchone()[0]
    try:
        with as_billshield() as cur:
            cur.execute("SELECT identity.account_deletion_state(%s)",
                        (str(subject),))
            assert cur.fetchone()[0] is None, (
                "the account is active; a non-NULL state here would mean the "
                "positive control below proves nothing")

        with owner_cursor() as cur:
            cur.execute(
                "INSERT INTO identity.account_lifecycle (user_id, state)"
                " VALUES (%s, 'DELETION_REQUESTED')", (str(subject),))

        with as_billshield() as cur:
            cur.execute("SELECT identity.account_deletion_state(%s)",
                        (str(subject),))
            assert cur.fetchone()[0] == "DELETION_REQUESTED", (
                "the worker must be able to see the cutoff it is required to "
                "recheck before producing any user data")
    finally:
        # The account's own removal is what takes the lifecycle row with it.
        with owner_cursor() as cur:
            cur.execute("DELETE FROM identity.user_account WHERE id = %s",
                        (str(subject),))


@pytest.mark.parametrize("table", [
    "identity.account_lifecycle", "identity.user_account",
    "identity.auth_session", "identity.user_credential",
])
def test_the_worker_is_refused_direct_reads_of_identity_tables(table):
    """Refused by a MISSING PRIVILEGE, not by a policy returning zero rows.
    §5.4.7 makes that distinction the whole point of the test."""
    with as_billshield() as cur:
        message = _refused(cur, f"SELECT 1 FROM {table} LIMIT 1")
    assert "permission denied" in message.lower(), message


@pytest.mark.parametrize("table", [
    "finance.expense_record", "analysis.analysis_run", "docs.document",
    "reco.recommendation", "ioe.scenario", "profile.tax_profile",
])
def test_the_worker_is_refused_every_tax_assurance_table(table):
    with as_billshield() as cur:
        message = _refused(cur, f"SELECT 1 FROM {table} LIMIT 1")
    assert "permission denied" in message.lower(), message


# --------------------------------------------------------------------------- #
# H. RLS and real cross-tenant traffic, as the real login
# --------------------------------------------------------------------------- #
def test_the_worker_reads_only_its_own_tenants_bill(tenants):
    tenant_a, tenant_b = tenants
    with as_billshield(tenant_a.user_id) as cur:
        cur.execute("SELECT id FROM billshield.bill")
        visible = {row[0] for row in cur.fetchall()}
    assert tenant_a.bill_id in visible
    assert tenant_b.bill_id not in visible


def test_the_worker_sees_nothing_without_a_tenant_context(tenants):
    """Deny-by-default depends on the GUC's ABSENCE (session.py:44-58)."""
    tenant_a, tenant_b = tenants
    with as_billshield() as cur:
        cur.execute("SELECT count(*) FROM billshield.bill")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM billshield.extraction_run")
        assert cur.fetchone()[0] == 0


def test_the_worker_advances_its_own_tenants_bill_lifecycle(tenants):
    """The positive control for the column-scoped UPDATE — without it, every
    refusal below would be consistent with a role that can do nothing."""
    tenant_a, _ = tenants
    with as_billshield(tenant_a.user_id) as cur:
        cur.execute(
            "UPDATE billshield.bill SET status = 'extracting',"
            " row_version = row_version + 1 WHERE id = %s",
            (str(tenant_a.bill_id),))
        assert cur.rowcount == 1


def test_the_worker_cannot_advance_another_tenants_bill(tenants):
    tenant_a, tenant_b = tenants
    with as_billshield(tenant_a.user_id) as cur:
        cur.execute(
            "UPDATE billshield.bill SET status = 'extracting' WHERE id = %s",
            (str(tenant_b.bill_id),))
        assert cur.rowcount == 0, (
            "the policy's USING clause must hide the other tenant's row"
        )


def test_the_worker_cannot_reassign_bill_ownership(tenants):
    """Ownership forgery is what `WITH CHECK` closes, and `user_id` is not in
    the column-scoped grant, so PostgreSQL refuses before RLS is consulted."""
    tenant_a, tenant_b = tenants
    with as_billshield(tenant_a.user_id) as cur:
        message = _refused(
            cur, "UPDATE billshield.bill SET user_id = %s WHERE id = %s",
            (str(tenant_b.user_id), str(tenant_a.bill_id)))
    assert "permission denied" in message.lower(), message


@pytest.mark.parametrize("column", [
    "deleted_at", "erased_at", "file_sha256", "byte_size", "page_count",
    "artifact_format", "updated_at", "created_at",
])
def test_the_worker_cannot_write_a_bill_column_outside_its_lifecycle_grant(column, tenants):
    """Each withheld column belongs to somebody else: `deleted_at` to the
    customer's request, `erased_at` to the privacy worker, the artifact facts
    to upload completion, `updated_at` to the trigger that owns it."""
    tenant_a, _ = tenants
    value = "now()" if column.endswith("_at") else "NULL"
    with as_billshield(tenant_a.user_id) as cur:
        message = _refused(
            cur,
            f"UPDATE billshield.bill SET {column} = {value} WHERE id = %s",
            (str(tenant_a.bill_id),))
    assert "permission denied" in message.lower(), message


def test_the_worker_cannot_create_or_delete_a_bill(tenants):
    tenant_a, _ = tenants
    with as_billshield(tenant_a.user_id) as cur:
        insert = _refused(
            cur, "INSERT INTO billshield.bill (user_id) VALUES (%s)",
            (str(tenant_a.user_id),))
    assert "permission denied" in insert.lower()
    with as_billshield(tenant_a.user_id) as cur:
        delete = _refused(cur, "DELETE FROM billshield.bill WHERE id = %s",
                          (str(tenant_a.bill_id),))
    assert "permission denied" in delete.lower()


def test_the_worker_writes_an_extraction_run_for_its_own_tenant(tenants):
    tenant_a, _ = tenants
    with as_billshield(tenant_a.user_id) as cur:
        cur.execute(
            "INSERT INTO billshield.extraction_run (bill_id, input_sha256)"
            " VALUES (%s, %s)",
            (str(tenant_a.bill_id), tenant_a.file_sha256))
        assert cur.rowcount == 1


def test_the_worker_cannot_write_an_extraction_run_for_another_tenant(tenants):
    """The parent-derived policy's WITH CHECK, exercised as real traffic."""
    tenant_a, tenant_b = tenants
    with as_billshield(tenant_a.user_id) as cur:
        message = _refused(
            cur,
            "INSERT INTO billshield.extraction_run (bill_id, input_sha256)"
            " VALUES (%s, %s)",
            (str(tenant_b.bill_id), tenant_b.file_sha256))
    assert "row-level security" in message.lower(), message


def test_the_worker_finalizes_its_own_running_extraction_run(tenants):
    tenant_a, _ = tenants
    with as_billshield(tenant_a.user_id) as cur:
        cur.execute(
            "INSERT INTO billshield.extraction_run (bill_id, input_sha256)"
            " VALUES (%s, %s) RETURNING id",
            (str(tenant_a.bill_id), tenant_a.file_sha256))
        run_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE billshield.extraction_run SET status = 'refused',"
            " refusal_code = 'UNREADABLE', adapter_code = 'fixture',"
            " model_version = 'fixture-1.0.0', completed_at = now()"
            " WHERE id = %s", (str(run_id),))
        assert cur.rowcount == 1


def test_the_worker_cannot_delete_an_extraction_run(tenants):
    tenant_a, _ = tenants
    with as_billshield(tenant_a.user_id) as cur:
        message = _refused(
            cur, "DELETE FROM billshield.extraction_run WHERE id = %s",
            (str(tenant_a.run_id),))
    assert "permission denied" in message.lower()


@pytest.mark.parametrize("column", sorted(FORBIDDEN_RUN_INSERT_COLUMNS))
def test_the_worker_cannot_supply_a_server_owned_run_column_on_insert(
        column, tenants):
    """The INSERT grant must express "create a RUNNING attempt", not "insert
    any valid extraction row".

    Each of these would be a different forgery: `id` chooses the run's
    identity, `status` and `completed_at` together manufacture a terminal
    attempt that never ran, and `created_at` backdates the record of when the
    extraction happened. The refusal must be `insufficient_privilege` — a CHECK
    or trigger rejection would mean the ACL had allowed the write and something
    downstream caught it.
    """
    tenant_a, _ = tenants
    value = FORBIDDEN_RUN_INSERT_COLUMNS[column]
    with as_billshield(tenant_a.user_id) as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as caught:
            cur.execute(
                f"INSERT INTO billshield.extraction_run (bill_id, input_sha256,"
                f" {column}) VALUES (%s, %s, {value})",
                (str(tenant_a.bill_id), tenant_a.file_sha256))
    assert "permission denied" in str(caught.value).lower()


@pytest.mark.parametrize("column", sorted(FROZEN_RUN_COLUMNS))
def test_the_worker_cannot_rewrite_a_run_identity_column(column, tenants):
    """Refused AT THE ACL BOUNDARY, not inside `reject_extraction_run_rewrite`.

    The trigger freezes these four as well, and that is deliberate defence in
    depth — but a boundary made only of a trigger is one `ALTER TABLE ...
    DISABLE TRIGGER` away from gone, and it cannot cover a column added later.
    `psycopg2.errors.InsufficientPrivilege` is what distinguishes the two: the
    trigger raises a CHECK violation instead.
    """
    tenant_a, _ = tenants
    value = FROZEN_RUN_COLUMNS[column]
    with as_billshield(tenant_a.user_id) as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as caught:
            cur.execute(
                f"UPDATE billshield.extraction_run SET {column} = {value}"
                f" WHERE id = %s", (str(tenant_a.run_id),))
    assert "permission denied" in str(caught.value).lower()


def test_the_worker_cannot_read_an_extraction_value_it_wrote(tenants):
    """`SELECT` on this table is two columns — identity and parent — and the
    extraction VALUES are not among them. The API serves the review screens
    under its own grant; the worker has no read-back use case, and a column
    list is the only thing that can express that."""
    tenant_a, _ = tenants
    with as_billshield(tenant_a.user_id) as cur:
        cur.execute("SELECT id, bill_id FROM billshield.extraction_run")  # allowed
        message = _refused(cur, "SELECT status FROM billshield.extraction_run")
    assert "permission denied" in message.lower(), message


def test_the_worker_writes_candidates_for_its_own_tenant(tenants):
    tenant_a, _ = tenants
    evidence = ('[{"page": 1, "x0": "0.100000", "y0": "0.200000",'
                ' "x1": "0.900000", "y1": "0.300000"}]')
    with as_billshield(tenant_a.user_id) as cur:
        cur.execute(
            "INSERT INTO billshield.charge_candidate (extraction_run_id,"
            " position, label_text, label_confidence, label_evidence, amount,"
            " amount_confidence, amount_evidence, kind, kind_confidence)"
            " VALUES (%s, 99, 'Late fee', 0.900000,"
            " %s::billshield.evidence_locators, 5.00, 0.950000,"
            " %s::billshield.evidence_locators, 'ONE_TIME', 0.900000)",
            (str(tenant_a.run_id), evidence, evidence))
        assert cur.rowcount == 1


@pytest.mark.parametrize("table,columns,values", [
    ("billshield.charge_candidate",
     "extraction_run_id, position, label_text, label_confidence,"
     " label_evidence, amount, amount_confidence, amount_evidence, kind,"
     " kind_confidence",
     "%s, 98, 'Forged', 0.900000, %s::billshield.evidence_locators, 1.00,"
     " 0.900000, %s::billshield.evidence_locators, 'ONE_TIME', 0.900000"),
    ("billshield.promotion_candidate",
     "extraction_run_id, position, expiry_date, expiry_confidence,"
     " expiry_evidence",
     "%s, 98, DATE '2026-01-31', 0.900000, %s::billshield.evidence_locators"),
])
@pytest.mark.parametrize("forged,value", [("id", "gen_random_uuid()"),
                                          ("created_at", "now()")])
def test_the_worker_cannot_supply_a_server_owned_candidate_column(
        table, columns, values, forged, value, tenants):
    """A candidate's identity and its creation time are the database's.

    Supplying `id` would let the component that parses untrusted files choose a
    row's primary key; supplying `created_at` would backdate the record of when
    an extraction produced it. Both are refused by the ACL because the INSERT
    grant names neither column.
    """
    tenant_a, _ = tenants
    evidence = ('[{"page": 1, "x0": "0.100000", "y0": "0.200000",'
                ' "x1": "0.900000", "y1": "0.300000"}]')
    params = [str(tenant_a.run_id)] + [evidence] * values.count("evidence_locators")
    with as_billshield(tenant_a.user_id) as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as caught:
            cur.execute(
                f"INSERT INTO {table} ({forged}, {columns})"
                f" VALUES ({value}, {values})", params)
    assert "permission denied" in str(caught.value).lower()


@pytest.mark.parametrize("column", ["created_at", "updated_at"])
def test_the_worker_cannot_read_a_bill_column_outside_its_read_set(
        column, tenants):
    """`SELECT` on `bill` is eleven named columns, not the table.

    `created_at` is history the worker has no use for, and `updated_at` is the
    `ref.set_updated_at()` trigger's — a runtime that could read the trigger's
    stamp is one step from wanting to write it.
    """
    tenant_a, _ = tenants
    with as_billshield(tenant_a.user_id) as cur:
        message = _refused(cur, f"SELECT {column} FROM billshield.bill")
    assert "permission denied" in message.lower(), message


def test_a_star_select_on_bill_is_refused(tenants):
    """The one-line proof that the grant is not table-wide. `SELECT *` expands
    to every column, including the two withheld above, so a table-level grant
    would make this succeed."""
    tenant_a, _ = tenants
    with as_billshield(tenant_a.user_id) as cur:
        message = _refused(cur, "SELECT * FROM billshield.bill")
    assert "permission denied" in message.lower(), message


@pytest.mark.parametrize("table", [
    "billshield.charge_candidate", "billshield.promotion_candidate",
])
def test_the_worker_cannot_read_or_delete_candidates(table, tenants):
    """No read use case is proven at this slice — the API serves review
    screens — and candidates are immutable extracted facts (§21.1(11))."""
    tenant_a, _ = tenants
    with as_billshield(tenant_a.user_id) as cur:
        read = _refused(cur, f"SELECT 1 FROM {table} LIMIT 1")
    assert "permission denied" in read.lower()
    with as_billshield(tenant_a.user_id) as cur:
        delete = _refused(cur, f"DELETE FROM {table}")
    assert "permission denied" in delete.lower()


@pytest.mark.parametrize("statement", [
    "SELECT 1 FROM billshield.job_outbox LIMIT 1",
    "INSERT INTO billshield.job_outbox (user_id, bill_id, task_code, dedupe_key)"
    " VALUES (gen_random_uuid(), gen_random_uuid(), 'EXTRACT_BILL',"
    " gen_random_uuid())",
    "UPDATE billshield.job_outbox SET claim_state = 'claimed'",
    "DELETE FROM billshield.job_outbox",
])
def test_the_worker_is_refused_every_outbox_operation(statement, tenants):
    """The keyholes are the only outbox interface, and none exists yet."""
    tenant_a, _ = tenants
    with as_billshield(tenant_a.user_id) as cur:
        message = _refused(cur, statement)
    assert "permission denied" in message.lower(), message


@pytest.mark.parametrize("table", [
    "billshield.provider", "billshield.provider_category",
])
def test_the_worker_is_refused_the_global_catalogue(table):
    with as_billshield() as cur:
        message = _refused(cur, f"SELECT 1 FROM {table} LIMIT 1")
    assert "permission denied" in message.lower(), message


def test_the_application_role_still_reads_the_catalogue_it_was_granted():
    """A control on the previous test: the catalogue is readable, just not by
    the worker. Without this, a dropped grant would look like a passing denial.
    """
    with as_app() as cur:
        cur.execute("SELECT count(*) FROM billshield.provider")
        assert cur.fetchone()[0] >= 0


# --------------------------------------------------------------------------- #
# I. The production bootstrap script, executed
# --------------------------------------------------------------------------- #
#: `infra/modules/database/bootstrap_runtime_logins.sql` — the SECOND
#: provisioning pass, run by an operator as the RDS master after the first
#: migration has created the group roles. Nothing in Terraform executes it;
#: `docs/operations/production-architecture.md` describes the two passes and
#: `infra/modules/secrets/main.tf:118` stores the raw passwords it needs as
#: psql variables. It is therefore a script whose only guard is a test.
BOOTSTRAP_SQL = (Path(__file__).resolve().parents[4]
                 / "infra" / "modules" / "database"
                 / "bootstrap_runtime_logins.sql")

#: The four credentials that exist today. `billshield_password` is deliberately
#: NOT here: its generation and injection belong to the deployment sub-slice,
#: and the whole point of the test below is that the script is valid before it.
PRE_BILLSHIELD_VARIABLES = {
    "api_password": "probe-api",
    "privacy_password": "probe-privacy",
    "freshness_password": "probe-freshness",
    "reporting_password": "probe-reporting",
}

BOOTSTRAP_LOGINS = ("onyx_api", "onyx_privacy", "onyx_freshness",
                    "onyx_reporting", "onyx_billshield")


def _run_bootstrap(variables: dict[str, str]) -> subprocess.CompletedProcess:
    """Execute the real script inside ONE transaction that is always rolled back.

    `CREATE ROLE` is transactional in PostgreSQL, so the rollback removes every
    role the script creates — which matters more than usual here, because roles
    are CLUSTER-wide and would otherwise outlive this database. `\\i` runs the
    committed file itself rather than a copy, so the test cannot drift from the
    thing it is guarding.
    """
    dsn = owner_dsn()
    command = ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q"]
    for name, value in variables.items():
        command += ["-v", f"{name}={value}"]
    script = f"BEGIN;\n\\i {BOOTSTRAP_SQL}\n{_ROLE_PROBE}\nROLLBACK;\n"
    return subprocess.run(command, input=script, capture_output=True, text=True)


#: Printed inside the transaction, so the assertions can see roles that are
#: about to be rolled away.
_ROLE_PROBE = (
    "SELECT 'PROBE ' || r.rolname || ' login=' || r.rolcanlogin"
    "     || ' super=' || r.rolsuper || ' bypassrls=' || r.rolbypassrls"
    "     || ' member_of=' || COALESCE("
    "          (SELECT string_agg(g.rolname, ',' ORDER BY g.rolname)"
    "             FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid"
    "            WHERE m.member = r.oid), '-')"
    "  FROM pg_roles r WHERE r.rolname = 'onyx_billshield';"
)


def _bootstrap_probe_lines(result: subprocess.CompletedProcess) -> list[str]:
    return [line.strip() for line in result.stdout.splitlines()
            if line.strip().startswith("PROBE ")]


def test_the_bootstrap_script_is_valid_before_the_billshield_credential_exists():
    """SLICE-INDEPENDENCE, executed rather than asserted.

    The login declaration lands in this sub-slice; the generated password, its
    secret and its injection land in the deployment one. If the script required
    the variable the moment the declaration appeared, every pre-deployment
    provisioning run would fail on a feature nobody has enabled — a sub-slice
    that breaks the tree until its successor merges is not independently
    reviewable.

    So: run the real file with exactly the four variables that exist today.
    """
    result = _run_bootstrap(PRE_BILLSHIELD_VARIABLES)
    assert result.returncode == 0, (
        f"the bootstrap script failed without `billshield_password`:\n"
        f"{result.stderr}")
    assert _bootstrap_probe_lines(result) == [], (
        "the script created `onyx_billshield` with no credential supplied; a "
        "login must never exist without a password an operator chose")
    assert "billshield" not in result.stderr.lower(), result.stderr


def test_the_bootstrap_script_creates_the_login_when_the_credential_is_given():
    """The other half. Without it, "succeeds without the variable" would be
    equally satisfied by a script that had simply dropped the declaration."""
    result = _run_bootstrap(
        {**PRE_BILLSHIELD_VARIABLES, "billshield_password": "probe-billshield"})
    assert result.returncode == 0, result.stderr
    probes = _bootstrap_probe_lines(result)
    assert len(probes) == 1, (
        f"expected exactly one onyx_billshield row, got {probes}")
    line = probes[0]
    assert "login=t" in line, f"the role was created NOLOGIN: {line}"
    assert "super=f" in line and "bypassrls=f" in line, (
        f"a runtime login must hold neither attribute: {line}")
    assert "member_of=onyx_billshield_worker" in line, (
        f"membership must be exactly the worker capability: {line}")


def test_the_bootstrap_script_never_echoes_a_password():
    """psql does not echo `-v` values, and the script must not print them
    either. This runs with a distinctive marker so a leak is unmistakable."""
    marker = "s3cr3t-marker-do-not-echo"
    result = _run_bootstrap(
        {**PRE_BILLSHIELD_VARIABLES, "billshield_password": marker})
    assert result.returncode == 0, result.stderr
    assert marker not in result.stdout and marker not in result.stderr, (
        "the bootstrap script echoed a credential")


def test_the_bootstrap_script_reuses_no_existing_credential_variable():
    """A guarded declaration must not quietly fall back to another runtime's
    password — that would give two services one credential and make the
    separation these roles exist for untrue."""
    body = BOOTSTRAP_SQL.read_text()
    billshield_lines = [line for line in body.splitlines()
                        if "onyx_billshield" in line and not line.strip().startswith("--")]
    assert billshield_lines, "the login declaration disappeared from the script"
    for other in ("api_password", "privacy_password", "freshness_password",
                  "reporting_password"):
        assert not any(other in line for line in billshield_lines), (
            f"the BillShield declaration references {other}")


def test_the_bootstrap_probe_left_no_role_behind():
    """Roles are CLUSTER-wide, so a leaked one would outlive this database and
    reach every other. Asserted after the runs above, not trusted to them."""
    with owner_cursor() as cur:
        cur.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            (list(BOOTSTRAP_LOGINS),))
        assert cur.fetchall() == [], (
            "a bootstrap probe role survived its rolled-back transaction")
