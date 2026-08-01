"""0020_seed_example — applies backend/db/sql/91_seed_example_rule.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0020_seed_example"
down_revision = "0019_seed_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("91_seed_example_rule.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
