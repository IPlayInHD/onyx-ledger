"""0010_reco — applies backend/db/sql/09_reco.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0010_reco"
down_revision = "0009_analysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("09_reco.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
