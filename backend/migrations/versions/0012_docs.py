"""0012_docs — applies backend/db/sql/11_docs.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0012_docs"
down_revision = "0011_ai"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("11_docs.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
