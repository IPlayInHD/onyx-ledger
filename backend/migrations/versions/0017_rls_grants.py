"""0017_rls_grants — applies backend/db/sql/16_rls_grants.sql"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0017_rls_grants"
down_revision = "0016_triggers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("16_rls_grants.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
