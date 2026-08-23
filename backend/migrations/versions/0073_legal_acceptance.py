"""0073_legal_acceptance — applies backend/db/sql/66_legal_acceptance.sql

Entry: B4 — durable legal acceptance.

Creates `identity.legal_acceptance`: append-only, RLS-protected, unique per
(user, document, version), readable and insertable by the runtime role and by
nothing else.

NO BACKFILL, AND THAT IS THE POINT. This migration writes no acceptance rows
for accounts that already exist. There is no durable record of what anybody
accepted before it ran, so a backfill would be the software inventing consent
in the one table whose entire purpose is to be true. Unknown history stays
unknown; existing accounts simply have outstanding acceptance and are asked on
their next session.

The downgrade drops the table, which discards real acceptances. That is stated
rather than hidden: there is nowhere else for the rows to go, and a downgrade
that left an orphaned table behind would be worse. The de-identified evidence
in audit.consent_log is unaffected either way.
"""
from app.database.sql_migrations import apply_sql_file

revision = "0073_legal_acceptance"
down_revision = "0072_reference_data_period"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("66_legal_acceptance.sql")


def downgrade() -> None:
    op_execute = __import__("alembic").op.execute
    op_execute("DROP TABLE IF EXISTS identity.legal_acceptance")
    op_execute(
        "DROP FUNCTION IF EXISTS identity.reject_legal_acceptance_mutation()"
    )
