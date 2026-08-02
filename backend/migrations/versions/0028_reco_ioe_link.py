"""0028_reco_ioe_link — applies backend/db/sql/22_reco_ioe_link.sql

Phase P1: additive link from reco.recommendation to the IOE. Ordered AFTER
0027_ioe because the foreign key requires ioe.optimization_run to exist.
"""
from __future__ import annotations

from app.database.sql_migrations import apply_sql_file

revision = "0028_reco_ioe_link"
down_revision = "0027_ioe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_sql_file("22_reco_ioe_link.sql")


def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
