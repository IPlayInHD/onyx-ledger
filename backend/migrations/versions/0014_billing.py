"""0014_billing — applies backend/db/sql/13_billing.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0014_billing"
down_revision = "0013_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("13_billing.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
