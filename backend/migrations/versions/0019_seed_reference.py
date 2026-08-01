"""0019_seed_reference — applies backend/db/sql/90_seed_reference.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0019_seed_reference"
down_revision = "0018_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("90_seed_reference.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
