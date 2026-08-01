"""0011_ai — applies backend/db/sql/10_ai.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0011_ai"
down_revision = "0010_reco"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("10_ai.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
