"""0018_indexes — applies backend/db/sql/17_indexes.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0018_indexes"
down_revision = "0017_rls_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("17_indexes.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
