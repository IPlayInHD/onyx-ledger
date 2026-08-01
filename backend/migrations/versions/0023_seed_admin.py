"""0023_seed_admin — applies backend/db/sql/93_seed_admin.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0023_seed_admin"
down_revision = "0022_seed_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("93_seed_admin.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
