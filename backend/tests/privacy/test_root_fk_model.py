"""The facts migration 0060's design rests on, pinned so they cannot drift.

Entry 11B6C's FK-model analysis concluded that all four retained roots take
`R1_DROP_FK_PRESERVE_UUID`: drop the foreign key to `identity.user_account`,
leave the historical `user_id` value untouched. Every alternative was eliminated
by measurement rather than preference, and each elimination rests on a property
of the live schema. If one of those properties changes, the conclusion changes
with it — so they are asserted here rather than left in a document.

What is NOT here: the end-to-end model (drop the four FKs, delete the account,
replay from the sealed artifacts). That needs a disposable committed database
rather than a rolled-back transaction, and building one belongs to the slice
that writes 0060. The measurement is recorded in
`docs/privacy/11b6c-retained-root-fk-model.md`.
"""

from __future__ import annotations

import psycopg2
import pytest

from tests.conftest import owner_dsn

ROOTS = (
    "analysis.analysis_run",
    "ioe.optimization_run",
    "ioe.scenario",
    "ioe.integrity_check",
)

#: The constraints migration 0060 removed, kept so the downgrade path and the
#: history stay legible. Nothing should recreate them.
DETACHED_FK_NAMES = {
    "analysis.analysis_run": "analysis_run_user_id_fkey",
    "ioe.optimization_run": "optimization_run_user_id_fkey",
    "ioe.scenario": "scenario_user_id_fkey",
    "ioe.integrity_check": "integrity_check_user_id_fkey",
}

@pytest.fixture(scope="module")
def cur():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        yield conn.cursor()
    finally:
        conn.close()


@pytest.fixture
def tx():
    """A cursor whose work is always rolled back."""
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = False
    try:
        yield conn.cursor()
    finally:
        conn.rollback()
        conn.close()


def test_no_retained_root_holds_a_live_foreign_key_to_the_account(cur):
    """§15 — the detachment invariant, and it is deliberately stronger than
    "not CASCADE".

    `NO ACTION` and `RESTRICT` are not severance: they leave the account row
    undeletable, which is the same dead end 0060 exists to remove. So the
    assertion is that NO foreign key from these tables reaches
    `identity.user_account` at all, whatever its delete action.
    """
    cur.execute("""
        SELECT cn.nspname || '.' || cc.relname, con.conname,
               pg_get_constraintdef(con.oid)
          FROM pg_constraint con
          JOIN pg_class cc ON cc.oid = con.conrelid
          JOIN pg_namespace cn ON cn.oid = cc.relnamespace
         WHERE con.contype = 'f'
           AND con.confrelid = 'identity.user_account'::regclass
           AND cn.nspname || '.' || cc.relname = ANY(%s)
         ORDER BY 1
    """, (list(ROOTS),))
    attached = cur.fetchall()
    assert attached == [], (
        "a retained evidence root is referentially bound to the account again:\n  "
        + "\n  ".join(f"{t}: {n} {d}" for t, n, d in attached)
        + "\nAny such constraint makes terminal account removal impossible "
        "again — CASCADE destroys the evidence, NO ACTION/RESTRICT refuse the "
        "parent delete."
    )


def test_the_ownership_column_survived_the_detachment_intact(cur):
    """R1 preserves the historical UUID; only the constraint was removed."""
    cur.execute("""
        SELECT cn.nspname || '.' || cc.relname, a.attnotnull, t.typname
          FROM pg_attribute a
          JOIN pg_class cc ON cc.oid = a.attrelid
          JOIN pg_namespace cn ON cn.oid = cc.relnamespace
          JOIN pg_type t ON t.oid = a.atttypid
         WHERE a.attname = 'user_id' AND a.attnum > 0 AND NOT a.attisdropped
           AND cn.nspname || '.' || cc.relname = ANY(%s)
         ORDER BY 1
    """, (list(ROOTS),))
    columns = {table: (notnull, typ) for table, notnull, typ in cur.fetchall()}
    assert sorted(columns) == sorted(ROOTS), (
        f"a retained root lost its ownership column: "
        f"{sorted(set(ROOTS) - set(columns))}"
    )
    for table, (notnull, typ) in columns.items():
        assert typ == "uuid", f"{table}.user_id is {typ}, not uuid"
        assert notnull, (
            f"{table}.user_id became nullable. R1 keeps the historical subject "
            "on every retained row; a nullable column invites a SET NULL that "
            "would erase it."
        )


def test_ownership_is_not_null_on_every_root_so_set_null_is_impossible(cur):
    """Eliminates R2 structurally rather than by preference.

    `ON DELETE SET NULL` against a `NOT NULL` column does not degrade
    gracefully — the cascade raises. So R2 could only be adopted by first
    dropping the NOT NULL, which weakens the ownership column on retained
    evidence to buy nothing R1 does not already give.
    """
    cur.execute("""
        SELECT cn.nspname || '.' || cc.relname, a.attnotnull
          FROM pg_attribute a
          JOIN pg_class cc ON cc.oid = a.attrelid
          JOIN pg_namespace cn ON cn.oid = cc.relnamespace
         WHERE a.attname = 'user_id' AND a.attnum > 0 AND NOT a.attisdropped
           AND cn.nspname || '.' || cc.relname = ANY(%s)
         ORDER BY 1
    """, (list(ROOTS),))
    nullable = [table for table, notnull in cur.fetchall() if not notnull]
    assert nullable == [], f"user_id became nullable on {nullable}; R2 is back on the table"


def _probe_account(tx) -> str:
    tx.execute("INSERT INTO identity.user_account (email, status) "
               "VALUES ('fk-model-probe@example.test', 'active') RETURNING id")
    return str(tx.fetchone()[0])


def test_a_completed_scenario_seals_its_ownership_column(tx):
    """Eliminates R3 for `ioe.scenario`, on a row in the state that matters.

    The seal only applies once `workflow_status` is `completed`, so the probe
    row is inserted already completed — the guard is BEFORE UPDATE, so it does
    not object to arriving that way, and that is precisely the state a retained
    scenario is in.
    """
    user_id = _probe_account(tx)
    tx.execute("INSERT INTO analysis.analysis_run (user_id, tax_year, status, engine_version) "
               "VALUES (%s, 2025, 'completed', 'probe') RETURNING id", (user_id,))
    analysis_id = str(tx.fetchone()[0])
    tx.execute("INSERT INTO ioe.scenario (user_id, base_analysis_id, workflow_status) "
               "VALUES (%s, %s, 'completed') RETURNING id", (user_id, analysis_id))
    scenario_id = str(tx.fetchone()[0])

    with pytest.raises(psycopg2.Error) as excinfo:
        tx.execute("UPDATE ioe.scenario SET user_id = gen_random_uuid() WHERE id = %s",
                   (scenario_id,))
    assert "sealed" in str(excinfo.value), excinfo.value


def test_a_terminal_integrity_check_refuses_every_ownership_rewrite(tx):
    """Eliminates R3 for `ioe.integrity_check`, which is stricter still.

    It does not seal named columns — once the row leaves `running` it refuses
    UPDATE outright, so there is no version of a subject-key rewrite that
    reaches it without disabling the guard.
    """
    user_id = _probe_account(tx)
    tx.execute("INSERT INTO analysis.analysis_run (user_id, tax_year, status, engine_version) "
               "VALUES (%s, 2025, 'completed', 'probe') RETURNING id", (user_id,))
    analysis_id = str(tx.fetchone()[0])
    tx.execute("INSERT INTO ioe.optimization_run (user_id, analysis_id, tax_year) "
               "VALUES (%s, %s, 2025) RETURNING id", (user_id, analysis_id))
    run_id = str(tx.fetchone()[0])
    tx.execute("""
        INSERT INTO ioe.integrity_check
               (user_id, entity_type, optimization_run_id, expected_result_hash,
                verifier_version, canonical_serialization_version,
                integrity_check_policy_version, status, completed_at)
        VALUES (%s, 'optimization', %s, 'probe-hash', '1.0.0', '1', '1',
                'verified', now())
        RETURNING id
    """, (user_id, run_id))
    check_id = str(tx.fetchone()[0])

    with pytest.raises(psycopg2.Error) as excinfo:
        tx.execute("UPDATE ioe.integrity_check SET user_id = gen_random_uuid() WHERE id = %s",
                   (check_id,))
    assert "terminal" in str(excinfo.value), excinfo.value


def test_no_action_does_not_permit_the_parent_delete(tx):
    """§27, proven rather than asserted.

    Changing `ON DELETE CASCADE` to `ON DELETE NO ACTION` is not severance. It
    converts an automatic child delete into a refused parent delete, which is
    the same dead end the append-only guard already produces. Only removing or
    re-pointing the child FK lets the account row go.
    """
    tx.execute("INSERT INTO identity.user_account (email, status) "
               "VALUES ('no-action-probe@example.test', 'active') RETURNING id")
    parent = tx.fetchone()[0]
    # A real table, not a temporary one: PostgreSQL refuses a constraint from a
    # temp table to a permanent one. DDL is transactional, so the rollback in
    # the fixture removes it as completely as ON COMMIT DROP would have.
    tx.execute("""
        CREATE TABLE identity.no_action_probe (
            id uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
            user_id uuid NOT NULL
                REFERENCES identity.user_account(id) ON DELETE NO ACTION
        )
    """)
    tx.execute("INSERT INTO identity.no_action_probe (user_id) VALUES (%s)", (str(parent),))

    with pytest.raises(psycopg2.Error) as excinfo:
        tx.execute("DELETE FROM identity.user_account WHERE id = %s", (str(parent),))
    assert "violates foreign key constraint" in str(excinfo.value), excinfo.value


def test_every_root_policy_is_guc_based_and_never_reads_the_account_table(cur):
    """§20 — the reason the account row can disappear without breaking access.

    Every root's policy compares `user_id` to `ref.current_app_user()`, which
    reads the `app.user_id` setting and nothing else. No policy contains an
    `EXISTS (SELECT 1 FROM identity.user_account ...)`, so deleting the account
    row neither grants access nor withdraws it — a governed reader that sets the
    historical subject still resolves the rows, and an ordinary session with no
    setting still sees none.
    """
    cur.execute("""
        SELECT c.relnamespace::regnamespace::text || '.' || c.relname, p.polname,
               coalesce(pg_get_expr(p.polqual, p.polrelid), ''),
               coalesce(pg_get_expr(p.polwithcheck, p.polrelid), '')
          FROM pg_policy p
          JOIN pg_class c ON c.oid = p.polrelid
         WHERE c.relnamespace::regnamespace::text || '.' || c.relname = ANY(%s)
         ORDER BY 1
    """, (list(ROOTS),))
    policies = cur.fetchall()
    assert sorted({row[0] for row in policies}) == sorted(ROOTS), (
        "a retained root lost its RLS policy"
    )
    for table, policy, using, check in policies:
        expression = f"{using} {check}"
        assert "current_app_user()" in expression, (
            f"{table}.{policy} no longer resolves ownership through the GUC: {using}"
        )
        assert "user_account" not in expression, (
            f"{table}.{policy} reads identity.user_account, so deleting the "
            f"account row would change who can read retained evidence: {using}"
        )


def test_the_ownership_indexes_are_not_merely_backing_the_foreign_key(cur):
    """§28 — dropping the FK must not take these with it.

    They serve the RLS predicate and idempotency uniqueness, both of which
    outlive the constraint. Recorded by name so a future migration that removes
    one has to argue for it.
    """
    cur.execute("""
        SELECT c.relnamespace::regnamespace::text || '.' || c.relname, i.relname
          FROM pg_index x
          JOIN pg_class c ON c.oid = x.indrelid
          JOIN pg_class i ON i.oid = x.indexrelid
         WHERE c.relnamespace::regnamespace::text || '.' || c.relname = ANY(%s)
           AND pg_get_indexdef(i.oid) LIKE '%%user_id%%'
         ORDER BY 1, 2
    """, (list(ROOTS),))
    by_table: dict[str, list[str]] = {}
    for table, index in cur.fetchall():
        by_table.setdefault(table, []).append(index)

    for table in ROOTS:
        assert by_table.get(table), (
            f"{table} has no index on user_id; the RLS predicate on every read "
            "would become a sequential scan"
        )


def test_no_orm_relationship_can_delete_a_retained_root():
    """§29/§30 — the ORM cannot cascade, because it declares no relationships.

    A database migration alone would be insufficient if SQLAlchemy carried
    `relationship(..., cascade="all, delete")` anywhere: the ORM would keep
    deleting children in Python after the FK stopped doing it in SQL. It does
    not — the models are plain column mappings throughout. Asserted rather than
    noted, because adding one relationship later is easy and silent.
    """
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[2]
    offenders = [
        path.relative_to(backend).as_posix()
        for path in list((backend / "app").rglob("*.py"))
        + list((backend / "workers").rglob("*.py"))
        if "relationship(" in path.read_text()
    ]
    assert offenders == [], (
        f"ORM relationships now exist in {offenders}. Check every one for a "
        "delete cascade reaching a retained root before trusting the FK model."
    )
