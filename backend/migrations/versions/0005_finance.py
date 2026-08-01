"""0005_finance — applies backend/db/sql/04_finance.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0005_finance"
down_revision = "0004_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("04_finance.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
