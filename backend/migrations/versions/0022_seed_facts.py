"""0022_seed_facts — applies backend/db/sql/92_seed_facts.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0022_seed_facts"
down_revision = "0021_admin_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("92_seed_facts.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
