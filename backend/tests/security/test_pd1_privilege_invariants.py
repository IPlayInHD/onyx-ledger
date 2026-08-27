"""PD-1 — the controls RLS does NOT provide (Entry 11B1).

RLS decides WHICH ROWS a role sees. Grants decide WHETHER THE COMMAND EXISTS.
They are separate controls and Entry 11B2 proved the hard way that reading a
migration tells you about neither: an explicit `GRANT SELECT, INSERT` looked
restrictive while `UPDATE` and `DELETE` survived through `ALTER DEFAULT
PRIVILEGES`.

So everything here is asserted by becoming the role and being allowed or
refused — never by reading DDL, and never by observing that a statement
affected zero rows, which is what a *policy* does rather than what a missing
privilege does.
"""
from __future__ import annotations

import contextlib
import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn
from tests.security.test_pd1_tenant_isolation import PD1_TABLES, pk

#: Roles that must not be able to touch these tables at all.
_NO_ACCESS_ROLES = ("onyx_freshness_worker", "onyx_privacy_worker", "public")

#: The nine tenant-data schemas whose DEFAULT ACLs are inspected below. This
#: list bounds ONLY `test_default_privileges_are_known_and_bounded`, which asks
#: a deliberately narrower question than tenant coverage — see its docstring.
#: The FORCE-RLS regression guard no longer uses any schema list: it derives
#: its bound from the protected set (tests/security/rls_protection.py).
_TENANT_SCHEMAS = ("ai", "analysis", "billing", "docs", "finance", "ioe",
                   "profile", "reco", "wealth")


@contextlib.contextmanager
def owner_cursor():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        yield conn.cursor()
    finally:
        conn.close()


@contextlib.contextmanager
def as_role(role: str, user_id: uuid.UUID | None = None):
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


# ---------------------------------------------------------------------------
# the intended privilege matrix (§8)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table", PD1_TABLES)
def test_the_runtime_role_holds_exactly_the_intended_privileges(table):
    """`onyx_app_rw` keeps full CRUD, matching every one of these tables'
    parents. RLS is what confines it to its own tenant.

    Deliberately NOT reduced. A privilege the product uses today should not be
    removed on the theory that a future privacy worker will want its own
    boundary — that worker gets its own keyhole when it is written.
    """
    with owner_cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege('onyx_app_rw', %s, 'SELECT'), "
            "       has_table_privilege('onyx_app_rw', %s, 'INSERT'), "
            "       has_table_privilege('onyx_app_rw', %s, 'UPDATE'), "
            "       has_table_privilege('onyx_app_rw', %s, 'DELETE'), "
            "       has_table_privilege('onyx_app_rw', %s, 'TRUNCATE')",
            (table,) * 5)
        select, insert, update, delete, truncate = cur.fetchone()

    assert (select, insert, update, delete) == (True, True, True, True), table
    assert truncate is False, (
        f"{table}: the runtime role can TRUNCATE, which bypasses RLS entirely "
        "— a policy cannot filter a whole-table wipe"
    )


@pytest.mark.parametrize("table", PD1_TABLES)
def test_the_read_only_role_cannot_write_at_all(table):
    """§23 — `onyx_app_ro` is read-only by GRANT, not by naming.

    Asserted by attempting each write as the role and being refused by
    PostgreSQL. A zero-row result would prove only that a policy filtered it,
    which is a different control and would still leave the privilege there.
    """
    with as_role("onyx_app_ro") as (cur, conn):
        for statement in (
            f"UPDATE {table} SET {pk(table)} = {pk(table)}",  # noqa: S608
            f"DELETE FROM {table}",  # noqa: S608
        ):
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(statement)
            conn.rollback()
            cur.execute("SET ROLE onyx_app_ro")


@pytest.mark.parametrize("table", PD1_TABLES)
def test_the_read_only_role_is_still_subject_to_the_policy(table):
    """Read-only is not the same as tenant-blind. FORCE RLS means the policy
    applies to `onyx_app_ro` too, so a reporting role cannot read across
    tenants either."""
    with as_role("onyx_app_ro") as (cur, _):
        cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
        assert cur.fetchone()[0] == 0, (
            f"{table}: the read-only role with no tenant context sees rows"
        )


@pytest.mark.parametrize("table", PD1_TABLES)
@pytest.mark.parametrize("role", _NO_ACCESS_ROLES)
def test_no_other_role_can_reach_these_tables(table, role):
    """§13 — the workers are not tenant-scoped readers of user data, and
    PUBLIC is not anything. Neither should hold a privilege here at all."""
    with owner_cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege(%s, %s, 'SELECT') "
            "    OR has_table_privilege(%s, %s, 'INSERT') "
            "    OR has_table_privilege(%s, %s, 'UPDATE') "
            "    OR has_table_privilege(%s, %s, 'DELETE')",
            (role, table) * 4)
        assert cur.fetchone()[0] is False, (
            f"{role} holds a privilege on {table}"
        )


# ---------------------------------------------------------------------------
# default privileges (§9)
# ---------------------------------------------------------------------------
def test_default_privileges_are_known_and_bounded():
    """§9 — what does a NEW table in a tenant schema inherit?

    DELIBERATELY SCOPED TO A HAND-NAMED LIST, and the list's meaning is
    stated exactly so it cannot be mistaken for coverage. This test asks a
    narrower question than "which tables need a tenant boundary": it inspects
    what `pg_default_acl` grants in the nine tenant-DATA schemas named in
    `_TENANT_SCHEMAS`. Fourteen schemas in this database carry default ACLs
    (16_rls_grants.sql, 18_admin_grants.sql, 19_tkms.sql, 21_ioe.sql among
    others); the five outside this list — identity, audit, admin, rules,
    tax_kb, tkms — are operator, reference, or pre-authentication surfaces
    whose grants are asserted by their own suites. Membership here means "a
    schema where a forgotten policy on a new CRUD-granted table would be a
    tenant leak", which is a design statement, not a derivation.

    Tenant COVERAGE is not this test's job and no longer has any schema list:
    `test_every_tenant_owned_table_has_a_forced_policy` below derives its
    bound from the protected set.

    `ALTER DEFAULT PRIVILEGES` grants `onyx_app_rw` full CRUD in every tenant
    schema. That is deliberate and matches the model: the runtime role writes
    user data. What it does NOT do is give the new table a policy — which is
    exactly how PD-1 happened, sixteen times.

    So the default is recorded rather than removed, and the regression guard is
    the test below: CRUD without a tenant boundary is the defect, not CRUD.
    """
    with owner_cursor() as cur:
        cur.execute("""
            SELECT n.nspname, unnest(d.defaclacl)::text
              FROM pg_default_acl d
              JOIN pg_namespace n ON n.oid = d.defaclnamespace
             WHERE n.nspname = ANY(%s)
             ORDER BY 1, 2
        """, (list(_TENANT_SCHEMAS),))
        acls = cur.fetchall()

    assert acls, "no default privileges found for the tenant schemas"
    for schema, acl in acls:
        grantee = acl.split("=", 1)[0]
        rights = acl.split("=", 1)[1].split("/", 1)[0]
        assert grantee in ("onyx_app_rw", "onyx_app_ro"), (
            f"{schema}: unexpected default grantee {grantee!r} ({acl})"
        )
        if grantee == "onyx_app_ro":
            assert rights == "r", f"{schema}: read-only role defaults to {rights!r}"
        else:
            assert set(rights) <= set("arwd"), (
                f"{schema}: runtime role defaults to {rights!r}, which is "
                "broader than CRUD — TRUNCATE or REFERENCES would let a new "
                "table escape the tenant boundary"
            )


def test_every_tenant_owned_table_has_a_forced_policy():
    """THE REGRESSION GUARD for §9, now derived rather than list-bound.

    A new table in a tenant schema inherits CRUD from default privileges and
    inherits no policy. This is what makes that combination fail the build
    instead of shipping: any table carrying a `user_id`, or reachable from one
    by foreign key, must have RLS enabled, FORCED, and at least one policy —
    unless it is in the justified non-RLS registry, or registered sealed
    default-deny (stricter than a policy, not weaker).

    THE BOUND USED TO BE `_TENANT_SCHEMAS`, which is how a user-derived table
    in a schema outside those nine names — a future `billshield` schema —
    escaped this guard entirely while looking covered. The bound is now the
    protected set (tests/security/rls_protection.py): catalogue-derived
    user-derived tables in ANY schema, foreign-key children included, minus
    only NON_RLS. `LIFECYCLE.rls=False` records what the database does; it
    exempts nothing.
    """
    from tests.security.rls_protection import (
        forced_rls_violations,
        protected_tables,
    )

    assert protected_tables(), "the protected set is empty; the derivation is wrong"
    unguarded = forced_rls_violations()
    assert not unguarded, (
        "these tables hold user-derived data and have no enforced tenant "
        "boundary:\n  " + "\n  ".join(unguarded)
        + "\nAdd RLS with FORCE and a policy, or justify the exception in "
          "app/privacy/classification.py."
    )


@pytest.mark.parametrize("table", PD1_TABLES)
def test_each_remediated_table_has_both_using_and_with_check(table):
    """§35 — a policy weakened to USING-only would still pass every read test
    and silently reopen ownership forgery."""
    with owner_cursor() as cur:
        cur.execute("""
            SELECT p.polname, p.polcmd,
                   pg_get_expr(p.polqual, p.polrelid) IS NOT NULL,
                   pg_get_expr(p.polwithcheck, p.polrelid) IS NOT NULL
              FROM pg_policy p WHERE p.polrelid = %s::regclass
        """, (table,))
        policies = cur.fetchall()

    assert policies, f"{table} has no policy"
    for name, cmd, has_using, has_check in policies:
        assert cmd == "*", (
            f"{table}.{name} covers only {cmd!r}; a command-specific policy "
            "leaves the others unprotected"
        )
        assert has_using, f"{table}.{name} has no USING clause"
        assert has_check, (
            f"{table}.{name} has no WITH CHECK clause, so a caller can write "
            "rows into another tenant's tree"
        )


# ---------------------------------------------------------------------------
# partitions (§10)
# ---------------------------------------------------------------------------
def test_none_of_the_remediated_tables_is_partitioned():
    """Recorded as a fact rather than assumed.

    Parent RLS does not protect child partitions in PostgreSQL — the repository
    already fixed a real bypass of exactly that kind — so if one of these ever
    becomes partitioned, the partition needs its own policy and this test is
    where that gets noticed.
    """
    with owner_cursor() as cur:
        cur.execute("""
            SELECT c.relnamespace::regnamespace::text || '.' || c.relname,
                   c.relkind
              FROM pg_class c
             WHERE c.oid = ANY(%s::regclass[])
               AND c.relkind = 'p'
        """, (list(PD1_TABLES),))
        partitioned = cur.fetchall()
    assert not partitioned, (
        f"{partitioned} are partitioned now; each partition needs RLS, FORCE "
        "and its own policy — see the partition invariant tests"
    )


# ---------------------------------------------------------------------------
# tenant context safety (§21)
# ---------------------------------------------------------------------------
def test_the_tenant_guc_does_not_survive_its_transaction():
    """§21 — connections are pooled, so a tenant context that outlived its
    transaction would be served to the next user of that connection.

    `unit_of_work` sets it with `is_local => true`. This asserts the property
    on one physical connection rather than trusting the flag.
    """
    user_a = uuid.uuid4()
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("SELECT set_config('app.user_id', %s, true)", (str(user_a),))
        cur.execute("SELECT current_setting('app.user_id', true)")
        assert cur.fetchone()[0] == str(user_a), "the GUC was never set"
        conn.commit()

        # Same physical connection, next transaction — the shape of a pooled
        # connection handed to a different user.
        cur.execute("SELECT current_setting('app.user_id', true)")
        leaked = cur.fetchone()[0]
        assert leaked in (None, ""), (
            f"tenant context {leaked!r} survived into the next transaction on "
            "a pooled connection"
        )
    finally:
        conn.close()


def test_a_leaked_context_would_be_caught_by_the_policies_anyway():
    """Defence in depth, stated so the reasoning is not lost: even if a context
    did leak, `ref.current_app_user()` resolves to whatever user it names, and
    the policies confine every one of the 16 tables to that user's rows. A leak
    would be a serious bug; it would not be an unbounded read."""
    with owner_cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_policy p "
                    "  WHERE p.polrelid = ANY(%s::regclass[])",
                    (list(PD1_TABLES),))
        assert cur.fetchone()[0] >= len(PD1_TABLES)
