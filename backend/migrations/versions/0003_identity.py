"""0003_identity — applies backend/db/sql/02_identity.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0003_identity"
down_revision = "0002_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("02_identity.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
