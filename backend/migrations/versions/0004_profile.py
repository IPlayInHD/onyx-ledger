"""0004_profile — applies backend/db/sql/03_profile.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0004_profile"
down_revision = "0003_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("03_profile.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
