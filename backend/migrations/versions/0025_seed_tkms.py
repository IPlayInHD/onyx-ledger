"""0025_seed_tkms — applies backend/db/sql/94_seed_tkms.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0025_seed_tkms"
down_revision = "0024_tkms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("94_seed_tkms.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
