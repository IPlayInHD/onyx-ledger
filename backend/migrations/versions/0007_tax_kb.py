"""0007_tax_kb — applies backend/db/sql/06_tax_kb.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0007_tax_kb"
down_revision = "0006_wealth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("06_tax_kb.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
