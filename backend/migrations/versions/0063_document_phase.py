"""0063_document_phase — applies backend/db/sql/57_document_phase.sql

Entry 11B6F. Wires the DOCUMENTS lifecycle phase: the two account-scoped
keyholes the privacy worker needs to remove one account's document binaries and
extraction rows, and the guard that refuses to record the phase complete while
any live document or extraction row survives.

`DOCUMENTS` was already a legal phase value and a `SourceDataPhase` member, and
`DocumentProcessingService.delete_document` has been the authoritative
per-document operation since 11B4. Nothing ran it for a deleting account.

DOWNGRADE removes the phase's functions and restores the pre-11B6F completion
guard, and refuses once the phase has run. Deleted binaries, purged extraction
rows and written tombstones are not recoverable, so a downgrade afterwards
would leave a schema that denies the phase exists while its irreversible
effects remain — the same reasoning as 0062.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0063_document_phase"
down_revision = "0062_scenario_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("57_document_phase.sql")


def downgrade() -> None:
    connection = op.get_bind()
    used = connection.exec_driver_sql(
        "SELECT count(*) FROM identity.account_lifecycle_phase"
        " WHERE phase = 'DOCUMENTS'"
    ).scalar_one()
    if used:
        raise RuntimeError(
            "0063_document_phase cannot be downgraded: the phase has run for "
            f"{used} account(s). It deletes document binaries from object "
            "storage and purges extraction rows, and neither is recoverable."
        )

    op.execute("DROP FUNCTION IF EXISTS "
               "identity.finalize_document_purge(uuid, uuid, uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS "
               "identity.list_account_documents_for_purge(uuid, uuid, text, integer)")
    op.execute("DROP FUNCTION IF EXISTS "
               "identity.count_remaining_document_privacy_work(uuid)")
    # Restores `complete_lifecycle_phase` without the DOCUMENTS branch.
    apply_sql_file("56_scenario_retention.sql")
