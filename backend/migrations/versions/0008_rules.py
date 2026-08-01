"""0008_rules — applies backend/db/sql/07_rules.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0008_rules"
down_revision = "0007_tax_kb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("07_rules.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
