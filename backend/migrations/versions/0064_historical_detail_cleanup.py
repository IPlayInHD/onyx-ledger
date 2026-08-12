"""0064_historical_detail_cleanup — applies backend/db/sql/58_historical_detail_cleanup.sql

Entry 11B6I. Adds the fifth lifecycle phase, HISTORICAL_DETAIL_CLEANUP: the
account-scoped keyhole that removes the fifteen tables of derived historical
detail no retained evidence contract requires, and the guard that refuses to
record the phase complete while any of those rows survive.

These fifteen survived all four certified phases and, since 0060 detached the
sealed roots from `identity.user_account`, would outlive the account carrying a
`user_id` that resolves to nobody.

The keyhole names every table explicitly and never walks a parent, because
walking `ioe.optimization_run` reaches the eight tables Entry 11B6H proved
integrity verification reads.

DOWNGRADE removes the phase's functions, narrows `ck_lifecycle_phase_name` back
to the four certified phases, and restores the pre-11B6I completion guard. It
refuses once the phase has run: the deleted detail is not recoverable, so a
downgrade afterwards would leave a schema that denies the phase exists while its
irreversible effects remain — the same reasoning as 0062 and 0063.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0064_historical_detail_cleanup"
down_revision = "0063_document_phase"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("58_historical_detail_cleanup.sql")


def downgrade() -> None:
    connection = op.get_bind()
    used = connection.exec_driver_sql(
        "SELECT count(*) FROM identity.account_lifecycle_phase"
        " WHERE phase = 'HISTORICAL_DETAIL_CLEANUP'"
    ).scalar_one()
    if used:
        raise RuntimeError(
            "0064_historical_detail_cleanup cannot be downgraded: the phase has "
            f"run for {used} account(s). It deletes derived historical tax "
            "detail, and that is not recoverable."
        )

    op.execute("DROP FUNCTION IF EXISTS "
               "identity.purge_historical_detail(uuid, uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS "
               "identity.count_remaining_historical_detail(uuid)")
    # Narrow the phase name back to the four certified phases. Safe because the
    # guard above proved no row names the fifth.
    op.execute("ALTER TABLE identity.account_lifecycle_phase"
               " DROP CONSTRAINT IF EXISTS ck_lifecycle_phase_name")
    op.execute(
        "ALTER TABLE identity.account_lifecycle_phase"
        " ADD CONSTRAINT ck_lifecycle_phase_name"
        " CHECK (phase IN ('SOURCE_DATA', 'DOCUMENTS',"
        " 'AUDIT_AUTH_DEIDENTIFICATION', 'SCENARIO_RETENTION'))"
    )
    # Restores `complete_lifecycle_phase` without the HISTORICAL_DETAIL_CLEANUP
    # branch. 57 is the most recent definition, and it carries the DOCUMENTS,
    # SCENARIO_RETENTION, SOURCE_DATA and AUDIT_AUTH branches.
    apply_sql_file("57_document_phase.sql")
