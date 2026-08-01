"""0021_admin_grants — applies backend/db/sql/18_admin_grants.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0021_admin_grants"
down_revision = "0020_seed_example"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("18_admin_grants.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
