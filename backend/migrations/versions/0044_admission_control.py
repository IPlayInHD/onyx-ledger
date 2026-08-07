"""0044_admission_control — applies backend/db/sql/38_admission_control.sql

Entry 10. Adds the `admission` schema: fixed-window rate counters and
concurrency leases for expensive operations.

ADDITIVE ONLY. One new schema, two new tables, four indexes. No existing table,
column, constraint, trigger, policy or grant is touched, so the migration cannot
affect any sealed result or any existing query plan.

The tables carry a principal id, an operation code, bounded counters and
timestamps. No financial value, no document content, no specification payload —
an admission row says that work happened, never what was in it.

No SECURITY DEFINER function is added.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0044_admission_control"
down_revision = "0043_schema_comment_convergence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("38_admission_control.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
