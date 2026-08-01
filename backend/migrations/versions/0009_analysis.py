"""0009_analysis — applies backend/db/sql/08_analysis.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0009_analysis"
down_revision = "0008_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("08_analysis.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
