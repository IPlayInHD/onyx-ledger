"""Migration 0060 is reversible until it is used, and then it is not.

The usual choices for an irreversible migration are a downgrade that raises
unconditionally, or one that quietly destroys whatever is in the way. Both are
wrong here. Re-adding the four foreign keys is perfectly valid on a database
where no account has been deleted — which is exactly what the migration smoke
gate runs — and becomes impossible the moment retained evidence outlives its
account.

So the downgrade measures the condition instead of assuming it: no orphans, and
it restores the previous topology exactly; any orphans, and it refuses and names
the table that crossed the line. That keeps `alembic downgrade base` honest on a
fresh database without pretending production is reversible, and it means the
irreversibility is a fact about the data rather than a flag someone set.
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid

import pytest
from sqlalchemy import create_engine

from tests.conftest import owner_dsn

# The revision module is not on an importable package path (the versions
# directory has no __init__), so it is loaded by file location the way Alembic
# itself does.
_spec = importlib.util.spec_from_file_location(
    "_rev_0060",
    pathlib.Path(__file__).resolve().parents[2]
    / "migrations/versions/0060_retained_root_detachment.py",
)
_rev_0060 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rev_0060)
DETACHED = _rev_0060.DETACHED
orphan_report = _rev_0060.orphan_report


@pytest.fixture
def connection():
    """A SQLAlchemy connection whose transaction is always rolled back."""
    engine = create_engine(owner_dsn().replace("postgresql://", "postgresql+psycopg2://"))
    conn = engine.connect()
    transaction = conn.begin()
    try:
        yield conn
    finally:
        transaction.rollback()
        conn.close()
        engine.dispose()


def test_the_detached_constraints_are_named_exactly_as_the_catalog_had_them(connection):
    """The downgrade recreates constraints by name; the names must be real.

    Checked against the raw-SQL file rather than the live catalog, because the
    constraints are gone from the catalog by definition — what matters is that
    the migration removes and would restore the same four names.
    """
    sql = (pathlib.Path(__file__).resolve().parents[2]
           / "db/sql/54_retained_evidence_detachment.sql").read_text()
    for table, constraint in DETACHED:
        assert f"DROP CONSTRAINT IF EXISTS {constraint}" in sql, (
            f"{constraint} is in the migration's DETACHED list but the SQL file "
            f"never drops it"
        )
        assert table in sql, f"{table} is not touched by the SQL file"


def test_a_clean_database_reports_no_orphans_so_downgrade_is_permitted(connection):
    """The smoke-gate path: nothing deleted yet, so the FKs can come back."""
    assert orphan_report(connection) == [], (
        "this database already holds retained evidence for deleted accounts, so "
        "it cannot demonstrate the reversible case"
    )


def test_evidence_outliving_its_account_makes_the_downgrade_refuse(connection):
    """The production path, built rather than described.

    An `analysis.analysis_run` is inserted naming an account id that does not
    exist — which is precisely the state 0060 permits and every earlier schema
    forbade. The report must see it, because that is what stands between a
    downgrade and silently deleting somebody's retained evidence to satisfy a
    constraint.
    """
    ghost = uuid.uuid4()
    connection.exec_driver_sql(
        "INSERT INTO analysis.analysis_run (user_id, tax_year, status, engine_version) "
        f"VALUES ('{ghost}', 2025, 'completed', 'irreversibility-probe')"
    )

    report = orphan_report(connection)
    assert report, (
        "retained evidence now references a deleted account and the downgrade "
        "check did not notice; it would drop the evidence to restore the FK"
    )
    assert any(entry.startswith("analysis.analysis_run:") for entry in report), report


def test_the_insert_that_proves_it_was_impossible_before_0060(connection):
    """Guard on the guard: the probe above only means something post-detachment.

    If `analysis.analysis_run` still had its foreign key, the insert would raise
    and the previous test would fail for the wrong reason — never reaching the
    orphan check at all. So the absence of the constraint is asserted directly.
    """
    connection.exec_driver_sql("SAVEPOINT probe")
    rows = connection.exec_driver_sql(
        "SELECT count(*) FROM pg_constraint"
        " WHERE contype='f' AND confrelid='identity.user_account'::regclass"
        "   AND conrelid='analysis.analysis_run'::regclass"
    ).scalar_one()
    assert rows == 0, (
        "analysis.analysis_run is bound to identity.user_account again; the "
        "orphan probe cannot run and 0060's detachment has been undone"
    )
