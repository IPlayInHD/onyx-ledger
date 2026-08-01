"""0016_triggers — applies backend/db/sql/15_triggers.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0016_triggers"
down_revision = "0015_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("15_triggers.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
