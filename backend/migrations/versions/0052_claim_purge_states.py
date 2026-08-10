"""0052_claim_purge_states — applies backend/db/sql/46_lifecycle_claim_purge_states.sql

Entry 11B5E. The SOURCE_DATA phase could never be claimed.

`identity.claim_account_lifecycle` returned only DELETION_REQUESTED,
ACCESS_DISABLED and FAILED_RETRYABLE, so an account that reached PURGE_PENDING
was invisible to every worker. That was correct while Entry 11B2 declared
PURGE_PENDING terminal; it stopped being correct the moment a purge phase
existed to run.

PURGING is added as well as PURGE_PENDING. A worker that dies mid-phase leaves
the account in PURGING with an expired lease, and without it here nothing could
reclaim the subject.

FUNCTION AND INDEX ONLY. No column, no row, no value. The index is dropped and
recreated because its WHERE clause is part of its definition.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0052_claim_purge_states"
down_revision = "0051_source_data_purge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("46_lifecycle_claim_purge_states.sql")


def downgrade() -> None:
    # Restores the narrower predicate, which reinstates the stranding.
    op.execute("DROP INDEX IF EXISTS identity.ix_account_lifecycle_claimable")
    op.execute("""
        CREATE INDEX ix_account_lifecycle_claimable
            ON identity.account_lifecycle (requested_at)
         WHERE claimed_by IS NULL
           AND state IN ('DELETION_REQUESTED', 'ACCESS_DISABLED',
                         'FAILED_RETRYABLE')
    """)
