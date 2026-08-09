"""0051_source_data_purge — applies backend/db/sql/45_source_data_purge.sql

Entry 11B5. Durable per-phase progress for account deletion, plus the keyhole
that performs and verifies the source-data purge.

WHY A TABLE AND NOT MORE LIFECYCLE STATES. `account_lifecycle.state` is one
scalar, and account privacy deletion is several phases. A worker that crashed
between purging expenses and purging income would restart unable to tell which
half it had done. Widening the top-level state machine instead would mean every
future phase edits the CHECK constraint, the transition trigger and every reader
of `state`; the account stays in PURGING and a row per phase carries progress.

NO FOREIGN KEY TO identity.user_account, deliberately — that is PD-9. The FK
points at `account_lifecycle`, which Entry 11B3 made durable, so a purge that
ends by removing the account row does not destroy the record of the phases that
still have work to do.

ADDITIVE. No existing row is read, rewritten or deleted. The functions are new;
the only privilege change is EXECUTE for `onyx_privacy_worker` and SELECT for
the application roles. Nothing gains DELETE on a tenant table.

DOWNGRADE drops the phase table and the functions. It is safe in the sense that
nothing else references them, and lossy in the sense that in-flight phase
progress is the thing being dropped — an account mid-purge would resume with no
memory of which phase it had reached. Development and test environments only.
"""
from __future__ import annotations

from alembic import op

from app.database.sql_migrations import apply_sql_file

revision = "0051_source_data_purge"
down_revision = "0050_pd9_durable_deletion_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("45_source_data_purge.sql")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS identity.fail_lifecycle_phase("
               "uuid, text, uuid, text, text)")
    op.execute("DROP FUNCTION IF EXISTS identity.complete_lifecycle_phase("
               "uuid, text, uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS identity.start_lifecycle_phase("
               "uuid, text, uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS identity.purge_source_data("
               "uuid, uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS identity.count_remaining_source_data(uuid)")
    # The trigger goes with the table.
    op.execute("DROP TABLE IF EXISTS identity.account_lifecycle_phase")
    op.execute("DROP FUNCTION IF EXISTS identity.reject_lifecycle_phase_delete()")
