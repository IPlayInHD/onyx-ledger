"""0015_audit — applies backend/db/sql/14_audit.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0015_audit"
down_revision = "0014_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("14_audit.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
