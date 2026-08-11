"""0060_retained_root_detachment — applies backend/db/sql/54_retained_evidence_detachment.sql

Entry 11B6C. Detaches the four retained evidence roots from
`identity.user_account` so the account row can be removed without destroying
evidence that is proven to require survival, and withdraws DELETE on the
account table from the application role so removing the accidental blocker does
not hand the HTTP role a terminal delete.

DOWNGRADE IS CONDITIONALLY IMPOSSIBLE, and the condition is measured rather than
assumed. Re-adding the foreign keys is perfectly valid until the first account
is deleted; after that, retained rows name accounts that no longer exist and no
ordinary foreign key can be satisfied. The only ways to force it through would
be to delete the evidence, rewrite the historical UUIDs, or invent parent
accounts — all three destroy exactly what 0060 exists to protect.

So the downgrade looks for orphans first. Finding none it restores the previous
topology exactly; finding any it refuses and says which table crossed the line.
That keeps `alembic downgrade base` honest on a fresh database — which is what
the migration smoke gate runs — without pretending the operation is reversible
in production.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0060_retained_root_detachment"
down_revision = "0059_subject_severance"
branch_labels = None
depends_on = None

#: (table, constraint) exactly as the catalog reported them before detachment.
DETACHED = (
    ("analysis.analysis_run", "analysis_run_user_id_fkey"),
    ("ioe.optimization_run", "optimization_run_user_id_fkey"),
    ("ioe.scenario", "scenario_user_id_fkey"),
    ("ioe.integrity_check", "integrity_check_user_id_fkey"),
)


def upgrade() -> None:
    apply_sql_file("54_retained_evidence_detachment.sql")


def orphan_report(connection) -> list[str]:
    """Retained rows whose account is already gone, per table.

    Separated from `downgrade()` so the refusal can be tested against a real
    database without driving Alembic: the condition is the interesting part, and
    a test that could only reach it through a migration run would not be written.
    """
    orphaned = []
    for table, _constraint in DETACHED:
        count = connection.exec_driver_sql(
            f"SELECT count(*) FROM {table} t"
            "  WHERE NOT EXISTS (SELECT 1 FROM identity.user_account u"
            "                     WHERE u.id = t.user_id)"
        ).scalar_one()
        if count:
            orphaned.append(f"{table}: {count}")
    return orphaned


def downgrade() -> None:
    connection = op.get_bind()
    orphaned = orphan_report(connection)

    if orphaned:
        raise RuntimeError(
            "0060_retained_root_detachment cannot be downgraded: retained "
            "evidence already references deleted accounts, so the foreign keys "
            "it removed can no longer be satisfied.\n  "
            + "\n  ".join(orphaned)
            + "\nRestoring them would require deleting that evidence, rewriting "
            "its historical subject ids, or recreating the accounts. All three "
            "destroy what this migration exists to protect, so none is done "
            "automatically."
        )

    # No account has been deleted yet, so the previous topology is still
    # satisfiable and is restored exactly as it was.
    for table, constraint in DETACHED:
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
            "FOREIGN KEY (user_id) REFERENCES identity.user_account(id) "
            "ON DELETE CASCADE"
        )
    op.execute("GRANT DELETE ON identity.user_account TO onyx_app_rw")
