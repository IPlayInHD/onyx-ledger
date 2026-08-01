"""0002_ref — applies backend/db/sql/01_ref.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0002_ref"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("01_ref.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
