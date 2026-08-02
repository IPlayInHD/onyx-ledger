"""0027_ioe — applies backend/db/sql/21_ioe.sql

Phase P1: the `ioe` bounded context. Must precede 0028_reco_ioe_link, whose
foreign key targets ioe.optimization_run (Revision 2.1 §F).
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0027_ioe"
down_revision = "0026_rules_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("21_ioe.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
