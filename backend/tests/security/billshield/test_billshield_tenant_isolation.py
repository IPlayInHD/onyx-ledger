"""Cross-tenant traffic against every BillShield tenant table, as a real role.

Asserted by becoming `onyx_app_rw` and being allowed or refused — never by
reading DDL, and never by observing a zero-row result where a PRIVILEGE is the
control, because a zero-row read is what a POLICY does rather than what a
missing grant does.

Which principal each proof runs as matters. `onyx_app_rw` exists today and
holds real BillShield verbs, so every claim about it is provable now. The
worker LOGIN `onyx_billshield` does not exist until Slice 3; its behavioural
cross-tenant matrix is Slice 3 acceptance, and the group role's zero-privilege
posture is proven instead (see test_billshield_privileges.py). Writing a test
that "becomes" a login the migration never created would prove nothing.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from tests.security.billshield.conftest import (
    EVIDENCE,
    TENANT_TABLES,
    as_role,
    owner_cursor,
)

#: How to count one tenant's rows in each table, from the table itself.
_OWN_ROW = {
    "billshield.bill": "SELECT count(*) FROM billshield.bill WHERE id = %s",
    "billshield.extraction_run":
        "SELECT count(*) FROM billshield.extraction_run WHERE id = %s",
    "billshield.charge_candidate":
        "SELECT count(*) FROM billshield.charge_candidate WHERE id = %s",
    "billshield.promotion_candidate":
        "SELECT count(*) FROM billshield.promotion_candidate WHERE id = %s",
    "billshield.job_outbox":
        "SELECT count(*) FROM billshield.job_outbox WHERE id = %s",
}


def _row_id(tenant, table: str) -> uuid.UUID:
    return {
        "billshield.bill": tenant.bill_id,
        "billshield.extraction_run": tenant.run_id,
        "billshield.charge_candidate": tenant.charge_id,
        "billshield.promotion_candidate": tenant.promotion_id,
        "billshield.job_outbox": tenant.outbox_id,
    }[table]


def test_the_five_tenant_tables_are_discovered_from_the_catalogue(tenants):
    """The table list is not a hand-kept constant anyone can forget to extend.

    Every base table in the schema is either one of the five tenant-derived
    tables carrying RLS, or one of the two global catalogue tables that
    deliberately carry none. A sixth tenant table added without RLS fails here
    before it reaches the protected-set guard.
    """
    with owner_cursor() as cur:
        cur.execute(
            "SELECT n.nspname || '.' || c.relname, c.relrowsecurity, c.relforcerowsecurity"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'billshield' AND c.relkind = 'r'"
        )
        state = dict((row[0], (row[1], row[2])) for row in cur.fetchall())

    assert len(state) == 7, f"expected seven billshield tables, found {sorted(state)}"
    secured = {t for t, (enable, force) in state.items() if enable and force}
    assert secured == set(TENANT_TABLES), (
        "the set of ENABLE+FORCE tables is not the five tenant-derived tables: "
        f"{sorted(secured)}"
    )


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_a_tenant_reads_its_own_rows(tenants, table):
    """The positive control. A policy that denies everything is not isolation."""
    a, _ = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        cur.execute(_OWN_ROW[table], (_row_id(a, table),))
        assert cur.fetchone()[0] == 1, f"{table}: tenant A cannot see its own row"


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_one_tenant_cannot_read_anothers_rows(tenants, table):
    a, b = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        cur.execute(_OWN_ROW[table], (_row_id(b, table),))
        assert cur.fetchone()[0] == 0, f"{table}: tenant A can read tenant B's row"


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_an_anonymous_session_sees_nothing(tenants, table):
    """No `app.user_id` means no rows: NULL = anything is never true."""
    with as_role("onyx_app_rw", None) as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        assert cur.fetchone()[0] == 0, f"{table}: visible with no tenant context"


def test_a_tenant_cannot_update_anothers_bill(tenants):
    a, b = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        cur.execute(
            "UPDATE billshield.bill SET status = 'confirmed' WHERE id = %s",
            (b.bill_id,),
        )
        assert cur.rowcount == 0, "tenant A updated tenant B's bill"


def test_a_tenant_cannot_forge_a_bill_into_another_tenants_tree(tenants):
    """WITH CHECK, not USING. A USING-only policy leaves this open."""
    a, b = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as excinfo:
            cur.execute(
                "INSERT INTO billshield.bill (user_id) VALUES (%s)", (b.user_id,))
        assert "row-level security" in str(excinfo.value)


def test_the_api_cannot_even_attempt_to_move_a_bill_between_tenants(tenants):
    """Refused by the COLUMN grant, before RLS or the trigger is consulted.

    `onyx_app_rw` holds UPDATE on the bill's mutable columns only; `user_id` is
    not among them. So this is a privilege refusal — "permission denied for
    table bill" — and not a policy one, which is a strictly stronger result: the
    request path cannot form the statement at all. Asserted on the message
    because a zero-row UPDATE would be the weaker outcome and must not pass
    silently as if it were this one.
    """
    a, b = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as excinfo:
            cur.execute(
                "UPDATE billshield.bill SET user_id = %s WHERE id = %s",
                (b.user_id, a.bill_id),
            )
        assert "permission denied" in str(excinfo.value).lower()

    with owner_cursor() as cur:
        cur.execute("SELECT user_id FROM billshield.bill WHERE id = %s", (a.bill_id,))
        assert cur.fetchone()[0] == a.user_id, "ownership moved after a refused update"


def test_ownership_is_immutable_even_for_a_role_that_holds_the_column(tenants):
    """The other half, and the reason the trigger exists beside the grant.

    The test above proves the API cannot reach the column. This one proves the
    column cannot be rewritten even by a principal that CAN reach it — the
    owner, which FORCE RLS subjects to the policies but which holds every
    column privilege. Without this, "ownership is immutable" would be a claim
    about one role's grants rather than about the data.
    """
    a, b = tenants
    with owner_cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
            cur.execute(
                "UPDATE billshield.bill SET user_id = %s WHERE id = %s",
                (b.user_id, a.bill_id),
            )
        assert "identity is immutable" in str(excinfo.value)

    with owner_cursor() as cur:
        cur.execute("SELECT user_id FROM billshield.bill WHERE id = %s", (a.bill_id,))
        assert cur.fetchone()[0] == a.user_id


def test_a_tenant_cannot_attach_an_extraction_to_another_tenants_bill(tenants):
    """The child policy resolves through the bill, so the parent decides."""
    a, b = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        with pytest.raises(psycopg2.Error) as excinfo:
            cur.execute(
                "INSERT INTO billshield.extraction_run (bill_id, input_sha256)"
                " VALUES (%s, %s)",
                (b.bill_id, b.file_sha256),
            )
        # The API holds no INSERT on this table at all, so the privilege refusal
        # arrives first — which is a STRONGER refusal than the policy's, and the
        # message says which one spoke.
        assert "permission denied" in str(excinfo.value).lower()


def test_the_mixed_tenant_outbox_edge_is_refused(tenants):
    """A row naming tenant A beside tenant B's bill.

    This is the case a `user_id`-only policy accepts: the WITH CHECK passes,
    because the row really does claim tenant A. The composite foreign key to
    `bill (id, user_id)` is what refuses it, and this test is the reason that
    constraint exists rather than the ordinary single-column one.
    """
    a, b = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        with pytest.raises(psycopg2.errors.ForeignKeyViolation) as excinfo:
            cur.execute(
                "INSERT INTO billshield.job_outbox (user_id, bill_id, task_code,"
                " dedupe_key) VALUES (%s, %s, 'EXTRACT_BILL', %s)",
                # B's SPARE bill: it carries no intent yet, so the composite
                # ownership FK is the only constraint that can refuse this and
                # the assertion names the control actually under test.
                (a.user_id, b.spare_bill_id, str(uuid.uuid4())),
            )
        assert "fk_billshield_job_outbox_bill_owner" in str(excinfo.value)


def test_the_same_tenant_outbox_insert_succeeds(tenants):
    """The control that makes the refusal above non-vacuous.

    Without this, a constraint that refused EVERY enqueue would pass the test
    above and break the product.
    """
    a, _ = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        cur.execute(
            "INSERT INTO billshield.job_outbox (user_id, bill_id, task_code,"
            " dedupe_key) VALUES (%s, %s, 'EXTRACT_BILL', %s) RETURNING id",
            (a.user_id, a.spare_bill_id, str(uuid.uuid4())),
        )
        assert cur.fetchone()[0] is not None


def test_a_tenant_cannot_enqueue_against_another_tenants_bill(tenants):
    """Naming B's bill AND B's user is refused by the policy instead."""
    a, b = tenants
    with as_role("onyx_app_rw", a.user_id) as cur:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as excinfo:
            cur.execute(
                "INSERT INTO billshield.job_outbox (user_id, bill_id, task_code,"
                " dedupe_key) VALUES (%s, %s, 'EXTRACT_BILL', %s)",
                (b.user_id, b.bill_id, str(uuid.uuid4())),
            )
        assert "row-level security" in str(excinfo.value)


def test_every_tenant_policy_is_for_all_with_both_halves(tenants):
    """Catalogue shape, asserted for tables no runtime can yet write.

    The behavioural WITH CHECK proof for the extraction children needs a role
    holding INSERT on them, which arrives in Slice 3. Until then this is the
    honest evidence: the policy exists, covers every command, and carries both
    a USING and a WITH CHECK expression.
    """
    with owner_cursor() as cur:
        cur.execute(
            "SELECT n.nspname || '.' || c.relname, p.polname, p.polcmd,"
            "       p.polqual IS NOT NULL, p.polwithcheck IS NOT NULL"
            " FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'billshield'"
        )
        rows = cur.fetchall()

    by_table = {}
    for table, name, cmd, has_using, has_check in rows:
        by_table.setdefault(table, []).append((name, cmd, has_using, has_check))

    assert set(by_table) == set(TENANT_TABLES), (
        f"policies exist on {sorted(by_table)}, expected exactly the tenant tables")
    for table, policies in by_table.items():
        assert len(policies) == 1, f"{table} carries {len(policies)} policies"
        name, cmd, has_using, has_check = policies[0]
        assert cmd == "*", f"{table}: policy {name} is not FOR ALL (polcmd={cmd})"
        assert has_using, f"{table}: policy {name} has no USING"
        assert has_check, f"{table}: policy {name} has no WITH CHECK"


def test_the_evidence_domain_refuses_a_text_snippet(tenants):
    """Bill text has nowhere to go, and that is structural.

    The closed key set is what makes "evidence carries no snippet" a property of
    the database rather than a habit of the writer.
    """
    a, _ = tenants
    leaking = EVIDENCE.replace("}]", ', "text": "ACME WIRELESS"}]')
    with owner_cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
            cur.execute(
                "INSERT INTO billshield.extraction_run (bill_id, input_sha256,"
                " amount_due_value, amount_due_confidence, amount_due_evidence)"
                " VALUES (%s, %s, 1.00, 0.5, %s::billshield.evidence_locators)",
                (a.spare_bill_id, "b" * 64, leaking),
            )
        assert "evidence_locators_have_no_other_key" in str(excinfo.value)
