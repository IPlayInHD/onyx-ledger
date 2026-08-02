"""0024_tkms — applies backend/db/sql/19_tkms.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0024_tkms"
down_revision = "0023_seed_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("19_tkms.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
